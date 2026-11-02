"""DVB-I content guide (ETSI TS 103 770 clause 6, TV-Anytime metadata).

The service list names content guide sources with a ScheduleInfoEndpoint
and a ProgramInfoEndpoint. The schedule endpoint answers

* ``?sid=<service>&now_next=true`` with the programme now and next
* ``?sid=<service>&start=<unix time>&end=<unix time>`` with the schedule of
  that time span

and the programme endpoint ``?pid=<programme id>`` with the details of one
programme. Answers are TVAMain documents: a ProgramInformationTable with
titles, synopses, genres, parental guidance, images and accessibility
attributes, and a ProgramLocationTable whose Schedule lists the events.

DTVP profile IT-6: now/next, today plus 7 days ahead (7 days back is
recommended), programme details with images (default width 640 px), long
synopsis; IT-16: tell the user which accessibility features a programme has.
The service is named by its ContentGuideServiceRef, else by its
UniqueIdentifier.
"""

import re
import time
import urllib.parse
from calendar import timegm
from dataclasses import dataclass, field

from . import dvbi
from .dvbi import DVBIError, _all, _kid, _kids, _local, _parse, _text, pick_text
from .epg import GENRES, Event

DAY = 24 * 3600
DAYS_AHEAD = 7
DAYS_BACK = 1                  # the store keeps no past events beyond today
IMAGE_WIDTH = 640
HOW_PROMO_IMAGE = ":19"        # urn:tva:metadata:cs:HowRelatedCS:2012:19

# accessibility features LinDVB can offer (IT-16: list nothing else)
AUDIO_DESCRIPTION = "Audio description"
SUBTITLES = "Subtitles"
SIGN_LANGUAGE = "Sign language"
ACCESS_SHORT = {AUDIO_DESCRIPTION: "AD", SUBTITLES: "ST", SIGN_LANGUAGE: "SL"}


def _q(endpoint, params):
    sep = "&" if "?" in endpoint else "?"
    return endpoint + sep + urllib.parse.urlencode(params)


def now_next_url(endpoint, sid):
    return _q(endpoint, [("sid", sid), ("now_next", "true")])


def schedule_url(endpoint, sid, start, end):
    return _q(endpoint, [("sid", sid), ("start", int(start)), ("end", int(end))])


def program_url(endpoint, pid):
    return _q(endpoint, [("pid", pid)])


def image_url(url, width=IMAGE_WIDTH):
    """IT-6: images can be asked for in a width (?w=)."""
    if not url or url.startswith("data:") or re.search(r"[?&]w=\d", url):
        return url
    return _q(url, [("w", width)])


_DATE = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def parse_time(text):
    """xs:dateTime -> unix time (no zone = UTC)."""
    m = _DATE.match((text or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    t = timegm((int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, 0))
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        tz = tz[1:].replace(":", "")
        t -= sign * (int(tz[:2]) * 3600 + int(tz[2:]) * 60)
    return t


_DUR = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_duration(text):
    m = _DUR.match((text or "").strip())
    if not m:
        return None
    d, h, mi, s = m.groups()
    return int(d or 0) * DAY + int(h or 0) * 3600 + int(mi or 0) * 60 + int(float(s or 0))


def genre_name(href):
    """DVB content subject (urn:dvb:metadata:cs:ContentSubject:2019:3.1) or the
    DVB content nibbles of TV-Anytime ContentCS -> LinDVB's genre names."""
    m = re.search(r"ContentSubject:\d+:(\d+)", href or "")
    if m:
        return GENRES.get(int(m.group(1)), "")
    return ""


@dataclass
class Programme:
    pid: str
    title: str = ""
    subtitle: str = ""
    synopsis: str = ""
    genre: str = ""
    min_age: int = 0
    image: str = ""
    access: list = field(default_factory=list)


def _access(el):
    """Accessibility features named in AVAttributes / AccessibilityAttributes."""
    found = []
    for e in el.iter() if el is not None else ():
        name = _local(e.tag)
        if name in ("SubtitleAttributes", "CaptionLanguage", "SubtitleLanguage"):
            found.append(SUBTITLES)
        elif name == "AudioDescriptionAttributes":
            found.append(AUDIO_DESCRIPTION)
        elif name == "AudioPurpose" and (e.get("href") or "").endswith(":1"):
            found.append(AUDIO_DESCRIPTION)        # AudioPurposeCS 1: visually impaired
        elif name in ("SigningAttributes", "SignLanguage"):
            found.append(SIGN_LANGUAGE)
    return list(dict.fromkeys(found))


def _synopsis(desc):
    by_len = {}
    for s in _kids(desc, "Synopsis"):
        by_len.setdefault(s.get("length", "medium"), []).append(s)
    for length in ("long", "medium", "short"):       # IT-6: long synopsis preferred
        if length in by_len:
            text = pick_text(by_len[length])
            if text:
                return re.sub(r"\s+", " ", text)
    return ""


def parse_programme(pi):
    p = Programme(pid=pi.get("programId", ""))
    desc = _kid(pi, "BasicDescription")
    titles = _kids(desc, "Title")
    p.title = pick_text([t for t in titles if t.get("type", "main") == "main"]) or pick_text(titles)
    p.subtitle = pick_text([t for t in titles if t.get("type") in ("secondary", "episodeTitle")])
    p.synopsis = _synopsis(desc)
    for g in _kids(desc, "Genre"):
        p.genre = genre_name(g.get("href"))
        if p.genre:
            break
    ages = [dvbi._int(_text(a)) for a in _all(desc, "MinimumAge")]
    ages = [a for a in ages if a is not None]
    p.min_age = max(ages) if ages else 0
    for rm in _kids(desc, "RelatedMaterial"):
        how = _kid(rm, "HowRelated")
        if how is not None and (how.get("href") or "").endswith(HOW_PROMO_IMAGE):
            uri = next((_text(u) for u in _all(rm, "MediaUri") if _text(u)), "")
            if uri:
                p.image = uri
                break
    p.access = _access(_kid(pi, "AVAttributes")) + \
        [a for a in _access(_kid(desc, "AccessibilityAttributes")) if a]
    p.access = list(dict.fromkeys(p.access))
    return p


@dataclass
class GuideItem:
    service: str                 # Schedule@serviceIDRef
    event: Event


def parse_tva(data):
    """(programmes by id, [GuideItem]) from a TVAMain document."""
    root = _parse(data)
    if _local(root.tag) != "TVAMain":
        raise DVBIError(f"Not a TV-Anytime document ({_local(root.tag)})")
    programmes = {}
    for pi in _all(root, "ProgramInformation"):
        p = parse_programme(pi)
        if p.pid:
            programmes[p.pid] = p
    items = []
    for sched in _all(root, "Schedule"):
        service = sched.get("serviceIDRef", "")
        for se in _kids(sched, "ScheduleEvent"):
            prog = _kid(se, "Program")
            pid = prog.get("crid", "") if prog is not None else ""
            start = parse_time(_text(_kid(se, "PublishedStartTime")))
            dur = parse_duration(_text(_kid(se, "PublishedDuration")))
            if start is None:
                continue
            if dur is None:
                end = parse_time(_text(_kid(se, "PublishedEndTime")))
                dur = end - start if end else 0
            p = programmes.get(pid) or Programme(pid=pid)
            inst = _kid(se, "InstanceDescription")
            title = pick_text(_kids(inst, "Title")) if inst is not None else ""
            ev = Event(start=start, duration=max(0, dur), title=title or p.title or "",
                       subtitle=p.subtitle, description=p.synopsis, genre=p.genre,
                       programme_id=pid, image=p.image, min_age=p.min_age,
                       access=list(p.access))
            items.append(GuideItem(service, ev))
    return programmes, items


def complete(ev, programme):
    """Fill an event from programme details fetched separately."""
    ev.title = ev.title or programme.title
    ev.subtitle = ev.subtitle or programme.subtitle
    ev.description = ev.description or programme.synopsis
    ev.genre = ev.genre or programme.genre
    ev.image = ev.image or programme.image
    ev.min_age = ev.min_age or programme.min_age
    ev.access = ev.access or list(programme.access)


# Subtitles in internet streams (EBU-TT-D in DASH) play when GStreamer's TTML
# elements are installed; the player sets this (lindvb.player.enable_ttml).
INTERNET_SUBTITLES = False


def supported_access(access, broadcast):
    """IT-16: list only what LinDVB can play. Broadcast DVB subtitles and an
    audio description track work, subtitles in DASH (EBU-TT-D) with the TTML
    elements; sign language (HbbTV) does not."""
    out = []
    for a in access or []:
        if a == AUDIO_DESCRIPTION or (a == SUBTITLES and (broadcast or INTERNET_SUBTITLES)):
            out.append(a)
    return out


def badges(ev):
    return " ".join(f"[{ACCESS_SHORT.get(a, a)}]" for a in getattr(ev, "access", []) or [])


# ---------------------------------------------------------------- loading

@dataclass
class GuideSource:
    """Where to ask for one channel's guide."""
    key: str                     # EPG store key of the channel
    sid: str                     # ContentGuideServiceRef or UniqueIdentifier
    schedule: str
    program: str = ""
    broadcast: bool = False


def sources_for(channels):
    out = []
    for c in channels:
        g = getattr(c, "dvbi_guide", None) or {}
        if c.dvbi_id and g.get("schedule"):
            out.append(GuideSource(c.epg_key, c.dvbi_cg or c.dvbi_id, g["schedule"],
                                   g.get("program", ""), not c.is_iptv))
    return out


def day_spans(now=None, ahead=DAYS_AHEAD, back=DAYS_BACK):
    """(start, end) per day from *back* days ago to *ahead* days ahead, in UTC
    days (servers keep requests within whole days)."""
    now = time.time() if now is None else now
    today = int(now) // DAY * DAY
    return [(today + d * DAY, today + (d + 1) * DAY) for d in range(-back + 1, ahead + 1)]


def load_guide(src, fetcher=dvbi.fetch, now=None, days=True, details=True):
    """Events for one channel: now/next, then (when *days*) the schedule of
    each day. Programmes the schedule answer does not describe are asked for
    at the programme endpoint (now/next only, to keep the requests few).
    Returns ([Event], errors)."""
    events, errors = {}, []

    def take(url):
        try:
            programmes, items = parse_tva(fetcher(url).data)
        except DVBIError as e:
            errors.append(str(e))
            return []
        evs = []
        for it in items:
            if it.service and it.service != src.sid:
                continue
            it.event.access = supported_access(it.event.access, src.broadcast)
            old = events.get(it.event.start)
            if old is not None and old.programme_id == it.event.programme_id:
                complete(it.event, Programme(old.programme_id, old.title, old.subtitle,
                                             old.description, old.genre, old.min_age,
                                             old.image, old.access))
            events[it.event.start] = it.event
            evs.append(it.event)
        return evs

    nn = take(now_next_url(src.schedule, src.sid))
    if details and src.program:
        for ev in nn:
            if ev.programme_id and (not ev.title or not ev.description):
                try:
                    progs, _ = parse_tva(fetcher(program_url(src.program, ev.programme_id)).data)
                except DVBIError as e:
                    errors.append(str(e))
                    continue
                p = progs.get(ev.programme_id)
                if p:
                    complete(ev, p)
                    ev.access = supported_access(ev.access, src.broadcast)
    if days:
        for start, end in day_spans(now):
            take(schedule_url(src.schedule, src.sid, start, end))
    return sorted(events.values(), key=lambda e: e.start), errors
