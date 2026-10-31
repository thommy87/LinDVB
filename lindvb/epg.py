"""Electronic programme guide: DVB EIT parsing (via GstMpegts) and XMLTV import."""

import gzip
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .config import CACHE_DIR

GENRES = {
    0x1: "Movie/Drama", 0x2: "News/Current affairs", 0x3: "Show/Game show",
    0x4: "Sports", 0x5: "Children's/Youth", 0x6: "Music/Ballet/Dance",
    0x7: "Arts/Culture", 0x8: "Social/Political/Economics",
    0x9: "Education/Science", 0xA: "Leisure/Hobbies", 0xB: "Special",
}


@dataclass
class Event:
    start: int            # unix time
    duration: int         # seconds
    title: str
    subtitle: str = ""
    description: str = ""
    genre: str = ""
    event_id: int = 0
    country: str = ""     # production country (XMLTV <country>)
    year: int = 0         # production year (XMLTV <date>)

    @property
    def end(self):
        return self.start + self.duration

    def is_now(self, t=None):
        t = t or time.time()
        return self.start <= t < self.end


class EPGStore:
    """Events keyed by "tsid:sid" (DVB) or "url:<url>" / XMLTV id (IPTV)."""

    path = CACHE_DIR / "epg.json"

    def __init__(self):
        self.events: dict[str, dict[int, Event]] = {}
        self.listeners = []
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            now = time.time()
            for key, evs in data.items():
                self.events[key] = {e["start"]: Event(**e) for e in evs
                                    if e["start"] + e["duration"] > now}
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        data = {k: [asdict(e) for e in v.values() if e.end > now]
                for k, v in self.events.items()}
        self.path.write_text(json.dumps(data))

    def add(self, key, ev):
        bucket = self.events.setdefault(key, {})
        # drop overlapping older entries (rescheduled programmes)
        for s in [s for s, e in bucket.items()
                  if s != ev.start and s < ev.end and e.end > ev.start]:
            del bucket[s]
        bucket[ev.start] = ev

    def notify(self):
        for cb in self.listeners:
            cb()

    def for_key(self, key, since=None):
        since = since if since is not None else time.time()
        return sorted((e for e in self.events.get(key, {}).values() if e.end > since),
                      key=lambda e: e.start)

    def now_next(self, key):
        evs = self.for_key(key)
        now = next((e for e in evs if e.is_now()), None)
        nxt = next((e for e in evs if e.start >= (now.end if now else time.time())), None)
        return now, nxt

    def at(self, key, t):
        return next((e for e in self.for_key(key, t) if e.start <= t < e.end), None)

    def search(self, text, include_description=True, case=False):
        needle = text if case else text.lower()
        for key, evs in self.events.items():
            for e in evs.values():
                hay = e.title + " " + e.subtitle
                if include_description:
                    hay += " " + e.description
                if not case:
                    hay = hay.lower()
                if needle in hay and e.end > time.time():
                    yield key, e


# ----------------------------------------------------------------------------
# DVB EIT
# ----------------------------------------------------------------------------

def _gst_dt_to_unix(dt):
    try:
        return int(datetime(dt.get_year(), dt.get_month(), dt.get_day(),
                            dt.get_hour(), dt.get_minute(), dt.get_second(),
                            tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return 0


def parse_eit_section(section, GstMpegts):
    """Return (key, [Event]) for an EIT section, or None."""
    if section.section_type not in (GstMpegts.SectionType.EIT,):
        return None
    eit = section.get_eit()
    if eit is None:
        return None
    key = f"{eit.transport_stream_id}:{section.subtable_extension}"
    events = []
    for ev in eit.events:
        start = _gst_dt_to_unix(ev.start_time) if ev.start_time else 0
        if not start:
            continue
        title = sub = desc = genre = ""
        ext = []
        for d in ev.descriptors:
            tag = d.tag
            try:
                if tag == 0x4D:
                    ok, _lang, name, text = d.parse_dvb_short_event()
                    if ok:
                        title, sub = name or "", text or ""
                elif tag == 0x4E:
                    ok, e = d.parse_dvb_extended_event()
                    if ok and e.text:
                        ext.append(e.text)
                elif tag == 0x54:
                    ok, contents = d.parse_dvb_content()
                    if ok and contents:
                        genre = GENRES.get(contents[0].content_nibble_1, "")
            except (TypeError, ValueError, AttributeError):
                pass
        desc = "".join(ext)
        if len(sub) > 80 and not desc:
            sub, desc = "", sub
        if title:
            events.append(Event(start, ev.duration, title, sub, desc, genre, ev.event_id))
    return key, events


# ----------------------------------------------------------------------------
# XMLTV (for IPTV)
# ----------------------------------------------------------------------------

def _xmltv_time(s):
    s = s.strip()
    fmt = "%Y%m%d%H%M%S %z" if " " in s else "%Y%m%d%H%M%S"
    try:
        return int(datetime.strptime(s, fmt).timestamp())
    except ValueError:
        return 0


def parse_xmltv(data: bytes):
    """Yield (channel display-name lowercased, Event)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    root = ET.fromstring(data)
    names = {}
    for ch in root.findall("channel"):
        dn = ch.findtext("display-name") or ch.get("id")
        names[ch.get("id")] = dn.strip().lower()
    for p in root.findall("programme"):
        start, stop = _xmltv_time(p.get("start", "")), _xmltv_time(p.get("stop", ""))
        if not start or stop <= start:
            continue
        date = (p.findtext("date") or "").strip()
        yield names.get(p.get("channel"), p.get("channel", "").lower()), Event(
            start, stop - start, p.findtext("title") or "", p.findtext("sub-title") or "",
            p.findtext("desc") or "", (p.findtext("category") or "").strip(),
            country=(p.findtext("country") or "").strip(),
            year=int(date[:4]) if date[:4].isdigit() else 0)


# ----------------------------------------------------------------------------
# Details for the info bar: genre, country, year
# ----------------------------------------------------------------------------

# "Show, Deutschland 2026", "Spielfilm, USA 1995", "Deutschland 2025",
# "Krimiserie, D/A 2019-2024" -- how German broadcasters write it in the EIT
_DETAILS = re.compile(
    r"^(?:(?P<genre>[^,\d]{2,40}),\s*)?(?P<country>[A-ZÄÖÜ][^,\d]{0,60}?)\s+"
    r"(?P<year>(?:19|20)\d\d)(?:\s*[-–/]\s*(?:19|20)?\d\d)?\.?$")


def _short_genre(genre):
    """'Show/Game show' (EIT content nibble) -> 'Show'."""
    return genre.split("/")[0].strip() if genre else ""


def programme_details(ev):
    """(genre, country, year, used_subtitle) for the info bar.

    Takes the XMLTV fields when present, else reads a "Genre, Country Year"
    line from the subtitle or the start of the description. used_subtitle
    says the subtitle was just that line (so it need not be shown again)."""
    genre, country, year = _short_genre(ev.genre), ev.country, ev.year
    used = False
    if not (country and year):
        first_desc = (ev.description or "").strip().splitlines()[:1]
        for i, text in enumerate([ev.subtitle] + first_desc):
            m = _DETAILS.match((text or "").strip())
            if m:
                genre = genre or (m.group("genre") or "").strip()
                if m.group("genre") and not ev.genre:
                    genre = m.group("genre").strip()
                country = country or m.group("country").strip()
                year = year or int(m.group("year"))
                used = i == 0
                break
    return genre, country, year, used


def details_line(ev):
    """'Show, Deutschland 2026' -- empty when the EPG has none of it."""
    genre, country, year, _ = programme_details(ev)
    origin = " ".join(p for p in (country, str(year) if year else "") if p)
    return ", ".join(p for p in (genre, origin) if p)
