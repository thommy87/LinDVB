"""What is playing on a radio channel: RDS RadioText, song parsing, covers, logos.

* DVB radio (ARD and many others on satellite and cable) carries RDS in the
  ancillary data at the end of each MPEG audio PES packet, stored back to front
  and closed by ``<length> 0xFD``. The bytes form UECP frames (``0xFE`` …
  ``0xFF``); message code ``0x0A`` is the RadioText, e.g. "Artist - Title".
* Internet radio sends the song as ICY ``StreamTitle``, which GStreamer turns
  into a title tag ("Artist - Title").
* Album covers come from the file's tags, or are looked up online (iTunes
  Search API) when the user allows it.
* Station logos are image files named after the channel in a logo folder
  (logos named after the channel, as DVBViewer keeps them, work as they are).

No GTK in here, so it can be tested on its own.
"""

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

# EBU RDS character set (EN 50067 annex E), bytes 0x80..0xFE
_RDS_HIGH = ("áàéèíìóòúùÑÇŞß¡Ĳ"
             "âäêëîïôöûüñçşğıĳ"
             "ªα©‰Ğěňőπ€£$←↑→↓"
             "º¹²³±İńűµ¿÷°¼½¾§"
             "ÁÀÉÈÍÌÓÒÚÙŘČŠŽĐĿ"
             "ÂÄÊËÎÏÔÖÛÜřčšžđŀ"
             "ÃÅÆŒŷÝÕØÞŊŔĆŚŹŦð"
             "ãåæœŵýõøþŋŕćśźŧ ")

UECP_START, UECP_STOP, UECP_STUFF = 0xFE, 0xFF, 0xFD
MEC_RADIOTEXT = 0x0A
_MAX_FRAME = 263            # 255 message + ADD/SQC/MFL + CRC + start/stop


def rds_to_str(data):
    """Decode RDS text bytes to a str."""
    out = []
    for b in data:
        if b in (0x0D, 0x00):           # end of text
            break
        if b < 0x20:
            continue
        out.append(chr(b) if b < 0x80 else (_RDS_HIGH[b - 0x80] if b - 0x80 < len(_RDS_HIGH)
                                             else " "))
    return " ".join("".join(out).split())


class RadioTextDecoder:
    """Collects UECP frames from the RDS bytes at the end of MPEG audio PES
    packets and returns each new RadioText."""

    def __init__(self):
        self.text = ""
        self._buf = None
        self._stuffed = False

    def feed_tail(self, tail):
        """``tail``: the last bytes of a PES payload (or all of it).
        Returns the RadioText when a new one is complete, else None."""
        if len(tail) < 3 or tail[-1] != UECP_STUFF:
            return None
        n = tail[-2]
        if n == 0 or n > len(tail) - 2:
            return None
        new = None
        for val in reversed(tail[-2 - n:-2]):
            got = self._byte(val)
            if got is not None:
                new = got
        return new

    def _byte(self, val):
        if val == UECP_START:
            self._buf, self._stuffed = [], False
        buf = self._buf
        if buf is None:
            return None
        if self._stuffed:
            self._stuffed = False
            mapped = {0: 0xFD, 1: 0xFE, 2: 0xFF}.get(val)
            if mapped is not None:
                buf[-1] = mapped
            else:
                buf.append(val)
        else:
            buf.append(val)
            if val == UECP_STUFF and len(buf) > 1:
                self._stuffed = True
        # byte 5 is the message element code: only RadioText is wanted
        if len(buf) == 6 and buf[5] != MEC_RADIOTEXT:
            self._buf = None
            return None
        if len(buf) > _MAX_FRAME:
            self._buf = None
            return None
        if val == UECP_STOP and not self._stuffed and len(buf) > 10:
            self._buf = None
            # 0 start, 1-2 ADD, 3 SQC, 4 MFL, 5 MEC, 6 DSN, 7 PSN, 8 MEL, 9 flags, 10.. text
            length = buf[8] - 1
            text = rds_to_str(buf[10:min(10 + length, len(buf) - 3)])
            if text and text != self.text:
                self.text = text
                return text
        return None


def ancillary_tail(size, extract):
    """Read just the RDS bytes at the end of a buffer of ``size`` bytes.
    ``extract(offset, n)`` returns ``n`` bytes from ``offset``."""
    if size < 3:
        return None
    end = extract(size - 2, 2)
    if len(end) != 2 or end[1] != UECP_STUFF or end[0] == 0 or end[0] > size - 2:
        return None
    return extract(size - 2 - end[0], end[0] + 2)


# ---------------------------------------------------------------- song parsing

_PREFIX = re.compile(r"^\s*(?:now playing|now on air|on air|jetzt läuft|jetzt|gerade läuft|"
                     r"sie hören|ihr hört|du hörst|aktuell|nowplaying|playing)\s*[:\-–]?\s*",
                     re.IGNORECASE)
_SEPARATORS = (" - ", " – ", " — ", " / ", " | ")


def split_song(text):
    """'Artist - Title' (or 'Title von/by Artist') -> (artist, title).
    Text that is no song comes back as ("", text)."""
    t = " ".join((text or "").split())
    t = _PREFIX.sub("", t)
    if not t:
        return "", ""
    for sep in _SEPARATORS:
        if sep in t:
            a, b = t.split(sep, 1)
            if a.strip() and b.strip():
                return a.strip(), b.strip()
    m = re.match(r"^(.+?)\s+(?:von|by)\s+(.+)$", t)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return "", t


def song_from(meta):
    """Combine player metadata into the song shown over the visualisation.

    ``meta`` keys (all optional): title, artist, album, year, track, radiotext,
    organization. Returns a dict with artist, title, album, year, track."""
    artist = (meta.get("artist") or "").strip()
    title = (meta.get("title") or "").strip()
    if title and not artist:
        artist, title = split_song(title)
    if not title and meta.get("radiotext"):
        artist, title = split_song(meta["radiotext"])
    return {"artist": artist, "title": title, "album": (meta.get("album") or "").strip(),
            "year": str(meta.get("year") or ""), "track": meta.get("track") or 0}


# ---------------------------------------------------------------- online cover lookup

ITUNES_SEARCH = "https://itunes.apple.com/search"
USER_AGENT = "LinDVB (https://github.com/thommy87/lindvb)"


def _simple(s):
    return re.sub(r"[^0-9a-z]+", "", (s or "").casefold())


def pick_itunes_result(results, artist, title):
    """Best search hit for artist and title, or None."""
    want_a, want_t = _simple(artist), _simple(title)
    best, best_score = None, 0
    for r in results:
        a, t = _simple(r.get("artistName")), _simple(r.get("trackName"))
        score = 0
        if a and want_a and (a == want_a or a in want_a or want_a in a):
            score += 2
        if t and want_t and (t == want_t or t.startswith(want_t) or want_t.startswith(t)):
            score += 1
        if score > best_score and score >= 2 and r.get("artworkUrl100"):
            best, best_score = r, score
    if best is None:
        return None
    return {"album": best.get("collectionName") or "",
            "year": (best.get("releaseDate") or "")[:4],
            "track": best.get("trackNumber") or 0,
            "artwork": best["artworkUrl100"].replace("100x100bb", "600x600bb")}


def _get(url, timeout=6, limit=5 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def lookup_cover(artist, title, cache_dir=None):
    """Album, year and cover image (bytes) for a song, or None.
    Results (also misses) are kept in ``cache_dir``."""
    if not artist or not title:
        return None
    key = hashlib.sha1(f"{_simple(artist)}|{_simple(title)}".encode()).hexdigest()
    meta_file = img_file = None
    if cache_dir:
        cache_dir = Path(cache_dir)
        meta_file, img_file = cache_dir / f"{key}.json", cache_dir / f"{key}.img"
        try:
            info = json.loads(meta_file.read_text())
            if not info:
                return None
            info["image"] = img_file.read_bytes() if img_file.exists() else None
            return info
        except (OSError, ValueError):
            pass
    q = urllib.parse.urlencode({"term": f"{artist} {title}", "media": "music",
                                "entity": "song", "limit": 10})
    data = json.loads(_get(f"{ITUNES_SEARCH}?{q}"))
    info = pick_itunes_result(data.get("results", []), artist, title)
    image = _get(info["artwork"]) if info else None
    if meta_file:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            meta_file.write_text(json.dumps(info or {}))
            if image:
                img_file.write_bytes(image)
        except OSError:
            pass
    if info:
        info["image"] = image
    return info


# ---------------------------------------------------------------- station logos

LOGO_EXTS = (".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif")


def logo_names(name):
    """File stems a logo for channel ``name`` may have."""
    base = (name or "").strip()
    if not base:
        return []
    safe = re.sub(r'[\\/:*?"<>|]', "_", base)
    stems = [base, safe, safe.replace(" ", "_"), safe.replace(" ", ""),
             re.sub(r"\s+(HD|SD|UHD)$", "", safe, flags=re.IGNORECASE)]
    out = []
    for s in stems:
        if s and s.casefold() not in out:
            out.append(s.casefold())
    return out


def find_logo(name, folders):
    """Path of the logo for channel ``name`` in one of ``folders``, or None."""
    stems = logo_names(name)
    if not stems:
        return None
    for folder in folders:
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        index = {}
        for e in entries:
            stem, ext = os.path.splitext(e)
            if ext.lower() in LOGO_EXTS:
                index.setdefault(stem.casefold(), e)
        for s in stems:
            if s in index:
                return os.path.join(folder, index[s])
    return None
