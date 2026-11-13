# SPDX-License-Identifier: GPL-2.0-or-later
"""EPG from other providers' data streams (like DVBViewer's "MediaHighway/Freesat
frequencies"), plus the raw DVB helpers they need.

Some satellite platforms send their programme guide in a private format on
one "home" transponder, next to (or instead of) the standard DVB EIT:

  * MediaHighway 1 (old Canal+ platforms): PIDs 0xD2 (titles) and 0xD3
    (channels, themes, summaries).
  * MediaHighway 2 (e.g. Movistar+ on Astra 19.2E, 10847 V): PIDs 0x231
    (channels and themes), 0x234 (titles) and 0x236 (summaries).
  * Freesat (Astra 28.2E, 11428 H): a standard EIT on PID 3842 whose texts
    are Huffman compressed.

The user keeps a list of (frequency, polarisation, type) entries. When a
tuned channel lies on one of those transponders, the capture also receives
those PIDs and feeds the packets to :class:`ProviderEPG`, which turns them
into :class:`~lindvb.epg.Event` objects keyed like DVB EIT ("tsid:sid").

The section layouts follow the VDR EEPG plugin (GPL-2.0-or-later), which is
based on mhwepg by Jean-Claude Repetto and the Freesat patch by dom.
Everything here is pure Python so it can be unit tested without hardware.
"""

import calendar
import re
import time
import unicodedata
import zlib

from .epg import GENRES, Event

# -- provider list -----------------------------------------------------------

PROVIDER_TYPES = {
    "mhw1": "MediaHighway 1",
    "mhw2": "MediaHighway 2",
    "freesat": "Freesat",
}

PROVIDER_PIDS = {
    "mhw1": (0xD2, 0xD3),
    "mhw2": (0x231, 0x234, 0x236),
    "freesat": (3842, 3843),
}

# The entries DVBViewer ships with: frequency in MHz, polarisation, type.
DEFAULT_PROVIDER_FREQS = [
    [10847, "v", "mhw2"],
    [11428, "h", "freesat"],
]

FREQ_TOLERANCE_MHZ = 3


def provider_types_for(channel, entries):
    """Return the provider EPG types to read on the transponder of *channel*."""
    if not entries or not getattr(channel, "is_sat", False):
        return []
    mhz = channel.frequency / 1000
    pol = (channel.polarity or "")[:1].lower()
    found = []
    for entry in entries:
        try:
            freq, epol, kind = int(entry[0]), str(entry[1]).lower()[:1], str(entry[2])
        except (IndexError, TypeError, ValueError):
            continue
        if kind not in PROVIDER_TYPES or abs(freq - mhz) > FREQ_TOLERANCE_MHZ:
            continue
        if epol and pol and epol != pol:
            continue
        if kind not in found:
            found.append(kind)
    return found


def provider_pids(kinds):
    return sorted({p for k in kinds for p in PROVIDER_PIDS.get(k, ())})


# -- languages ----------------------------------------------------------------

# Preferred EPG language choices: (ISO 639-2 code, label). "" = no preference.
LANGUAGES = [
    ("", "Automatic (as broadcast)"),
    ("deu", "German (deu)"), ("eng", "English (eng)"), ("fra", "French (fra)"),
    ("ita", "Italian (ita)"), ("spa", "Spanish (spa)"), ("nld", "Dutch (nld)"),
    ("por", "Portuguese (por)"), ("pol", "Polish (pol)"), ("ces", "Czech (ces)"),
    ("slk", "Slovak (slk)"), ("hun", "Hungarian (hun)"), ("dan", "Danish (dan)"),
    ("swe", "Swedish (swe)"), ("nor", "Norwegian (nor)"), ("fin", "Finnish (fin)"),
    ("tur", "Turkish (tur)"), ("ell", "Greek (ell)"), ("rus", "Russian (rus)"),
    ("ara", "Arabic (ara)"),
]

# ISO 639-2 bibliographic codes (used by many broadcasters) -> terminology codes.
_LANG_ALIASES = {
    "ger": "deu", "fre": "fra", "dut": "nld", "cze": "ces", "slo": "slk",
    "gre": "ell", "chi": "zho", "per": "fas", "rum": "ron", "ice": "isl",
    "wel": "cym", "alb": "sqi", "arm": "hye", "baq": "eus", "geo": "kat",
    "mac": "mkd", "may": "msa", "bur": "mya", "tib": "bod", "mao": "mri",
}
# ISO 639-1 (XMLTV usually uses these) -> ISO 639-2/T.
_LANG_2TO3 = {
    "de": "deu", "en": "eng", "fr": "fra", "it": "ita", "es": "spa", "nl": "nld",
    "pt": "por", "pl": "pol", "cs": "ces", "sk": "slk", "hu": "hun", "da": "dan",
    "sv": "swe", "no": "nor", "nb": "nor", "nn": "nor", "fi": "fin", "tr": "tur",
    "el": "ell", "ru": "rus", "ar": "ara", "ja": "jpn", "ka": "kat",
    "zh": "zho",
}


def norm_lang(code):
    """Normalise a language code (2 or 3 letters, any case) to ISO 639-2/T."""
    code = (code or "").strip().lower()
    code = code.split("-")[0].split("_")[0]
    if len(code) == 2:
        return _LANG_2TO3.get(code, code)
    return _LANG_ALIASES.get(code, code)


def pick_language(options, preferred):
    """*options* is a list of (language, value). Return the value in the
    preferred language, else the first one."""
    if not options:
        return None
    want = norm_lang(preferred)
    if want:
        for lang, value in options:
            if norm_lang(lang) == want:
                return value
    return options[0][1]


# -- DVB text (EN 300 468 annex A) ----------------------------------------------

# ISO 6937 non-spacing diacritics (0xC1..0xCF) -> Unicode combining marks.
_ISO6937_DIACRITICS = {
    0xC1: "̀", 0xC2: "́", 0xC3: "̂", 0xC4: "̃", 0xC5: "̄",
    0xC6: "̆", 0xC7: "̇", 0xC8: "̈", 0xCA: "̊", 0xCB: "̧",
    0xCD: "̋", 0xCE: "̨", 0xCF: "̌",
}
_ISO6937_SPECIAL = {
    0xA4: "$", 0xA6: "#", 0xA8: "¤", 0xA9: "‘", 0xAA: "“", 0xAC: "←", 0xAD: "↑",
    0xAE: "→", 0xAF: "↓", 0xB4: "×", 0xB8: "÷", 0xB9: "’", 0xBA: "”", 0xD0: "―",
    0xD1: "¹", 0xD2: "®", 0xD3: "©", 0xD4: "™", 0xD5: "♪", 0xD6: "¬", 0xD7: "¦",
    0xDC: "⅛", 0xDD: "⅜", 0xDE: "⅝", 0xDF: "⅞", 0xE0: "Ω", 0xE1: "Æ", 0xE2: "Đ",
    0xE3: "ª", 0xE4: "Ħ", 0xE6: "Ĳ", 0xE7: "Ŀ", 0xE8: "Ł", 0xE9: "Ø", 0xEA: "Œ",
    0xEB: "º", 0xEC: "Þ", 0xED: "Ŧ", 0xEE: "Ŋ", 0xEF: "ŉ", 0xF0: "ĸ", 0xF1: "æ",
    0xF2: "đ", 0xF3: "ð", 0xF4: "ħ", 0xF5: "ı", 0xF6: "ĳ", 0xF7: "ŀ", 0xF8: "ł",
    0xF9: "ø", 0xFA: "œ", 0xFB: "ß", 0xFC: "þ", 0xFD: "ŧ", 0xFE: "ŋ", 0xFF: "­",
}


def _iso6937(data):
    out, mark = [], None
    for b in data:
        if b == 0x8A:
            out.append("\n")
            continue
        if b in _ISO6937_DIACRITICS:
            mark = _ISO6937_DIACRITICS[b]
            continue
        if b < 0x80 or 0xA0 <= b < 0xC0 and b not in _ISO6937_SPECIAL:
            ch = chr(b)
        else:
            ch = _ISO6937_SPECIAL.get(b, "")
        if mark:
            ch = unicodedata.normalize("NFC", ch + mark)
            mark = None
        out.append(ch)
    return "".join(out)


def _clean(text):
    # 0x86/0x87 emphasis on/off and 0x8A line break (EN 300 468 control codes)
    text = text.replace("\x8a", "\n").replace("\x86", "").replace("\x87", "")
    text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
    return re.sub(r" {2,}", " ", text).strip()


def dvb_text(data):
    """Decode a DVB string, including Freesat Huffman compressed ones."""
    data = bytes(data)
    if not data:
        return ""
    first = data[0]
    try:
        if first >= 0x20:
            return _clean(_iso6937(data))
        if first == 0x1F:
            return _clean(freesat_decode(data))
        if 0x01 <= first <= 0x0B:
            return _clean(data[1:].decode(f"iso8859-{first + 4}", "replace"))
        if first == 0x10 and len(data) >= 3:
            n = data[2]
            return _clean(data[3:].decode(f"iso8859-{n}", "replace"))
        if first == 0x11:
            return _clean(data[1:].decode("utf-16-be", "replace"))
        if first == 0x15:
            return _clean(data[1:].decode("utf-8", "replace"))
        if first == 0x13:
            return _clean(data[1:].decode("gb2312", "replace"))
        if first == 0x12:
            return _clean(data[1:].decode("euc-kr", "replace"))
        if first == 0x14:
            return _clean(data[1:].decode("big5", "replace"))
    except LookupError:
        pass
    return _clean(data[1:].decode("latin-1"))


# -- Freesat Huffman ------------------------------------------------------------

_STOP = 0
_ESCAPE = 1
_freesat = None


def _freesat_tables():
    global _freesat
    if _freesat is None:
        from .freesat_tables import T1, T2
        _freesat = [{k: dict(v) for k, v in t.items()} for t in (T1, T2)]
    return _freesat


def freesat_decode(data):
    """Decode a Freesat Huffman string (0x1F, table 1 or 2, compressed bits)."""
    data = bytes(data)
    if len(data) < 2 or data[0] != 0x1F or data[1] not in (1, 2):
        return data.decode("latin-1")
    table = _freesat_tables()[data[1] - 1]
    bits = "".join(f"{b:08b}" for b in data[2:])
    pos, last, out = 0, 0, bytearray()
    longest = 32
    while pos < len(bits):
        if last == _ESCAPE:
            if pos + 8 > len(bits):
                break
            ch = int(bits[pos:pos + 8], 2)
            pos += 8
            out.append(ch)
            if not ch & 0x80:
                last = ch
            continue
        codes = table.get(last)
        if not codes:
            break
        for n in range(1, longest + 1):
            nxt = codes.get(bits[pos:pos + n])
            if nxt is not None:
                pos += n
                break
        else:
            break
        if nxt == _STOP:
            break
        if nxt != _ESCAPE:
            out.append(nxt)
        last = nxt
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out.decode("latin-1")


def freesat_encode(text, table_id=1):
    """Encode *text* with the Freesat tables (used by the tests)."""
    table = _freesat_tables()[table_id - 1]
    rev = {k: {v: c for c, v in codes.items()} for k, codes in table.items()}
    bits, last = [], 0
    for ch in text.encode("latin-1") + b"\0":
        code = rev.get(last, {}).get(ch)
        if code is None:
            raise ValueError(f"cannot encode {ch!r} after {last!r}")
        bits.append(code)
        last = ch
    s = "".join(bits)
    s += "0" * (-len(s) % 8)
    return bytes([0x1F, table_id]) + bytes(int(s[i:i + 8], 2) for i in range(0, len(s), 8))


# -- raw sections -----------------------------------------------------------------

def crc_ok(section):
    """MPEG-2 CRC32 check of a long-form section."""
    return _crc32_mpeg(section) == 0


def _crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


def mpeg_crc(data):
    return _crc32_mpeg(data)


def bcd(b):
    return (b >> 4) * 10 + (b & 0x0F)


def mjd_utc(mjd, h=0, m=0, s=0, is_bcd=True):
    """Unix time of an MJD date with (BCD) hours, minutes and seconds."""
    if is_bcd:
        h, m, s = bcd(h), bcd(m), bcd(s)
    return (mjd - 40587) * 86400 + h * 3600 + m * 60 + s


def parse_eit_raw(section, language=""):
    """Parse a raw EIT section (bytes). Returns ("tsid:sid", [Event]) or None.

    Used for EIT on non-standard PIDs (Freesat) that GStreamer does not parse.
    """
    if len(section) < 18 or not (0x4E <= section[0] <= 0x6F):
        return None
    length = ((section[1] & 0x0F) << 8) | section[2]
    end = 3 + length - 4                          # without CRC
    if end > len(section):
        return None
    sid = (section[3] << 8) | section[4]
    tsid = (section[8] << 8) | section[9]
    pos, events = 14, []
    while pos + 12 <= end:
        event_id = (section[pos] << 8) | section[pos + 1]
        mjd = (section[pos + 2] << 8) | section[pos + 3]
        start = mjd_utc(mjd, *section[pos + 4:pos + 7])
        dur = bcd(section[pos + 7]) * 3600 + bcd(section[pos + 8]) * 60 + bcd(section[pos + 9])
        dlen = ((section[pos + 10] & 0x0F) << 8) | section[pos + 11]
        dpos, dend = pos + 12, min(pos + 12 + dlen, end)
        pos = dend
        shorts, extended, genre = [], {}, ""
        while dpos + 2 <= dend:
            tag, ln = section[dpos], section[dpos + 1]
            body = section[dpos + 2:dpos + 2 + ln]
            dpos += 2 + ln
            if tag == 0x4D and len(body) >= 5:
                lang = body[:3].decode("latin-1")
                n = body[3]
                name = dvb_text(body[4:4 + n])
                tl = body[4 + n] if len(body) > 4 + n else 0
                text = dvb_text(body[5 + n:5 + n + tl])
                shorts.append((lang, (name, text)))
            elif tag == 0x4E and len(body) >= 6:
                lang = body[1:4].decode("latin-1")
                items_len = body[4]
                tpos = 5 + items_len
                tl = body[tpos] if len(body) > tpos else 0
                extended.setdefault(lang, []).append(
                    (body[0] >> 4, dvb_text(body[tpos + 1:tpos + 1 + tl])))
            elif tag == 0x54 and body:
                genre = GENRES.get(body[0] >> 4, "")
        if not start or not shorts:
            continue
        title, sub = pick_language(shorts, language)
        ext = pick_language(list(extended.items()), language) or []
        desc = "".join(t for _n, t in sorted(ext, key=lambda x: x[0]))
        if len(sub) > 80 and not desc:
            sub, desc = "", sub
        if title:
            events.append(Event(start, dur, title, sub, desc, genre, event_id))
    return f"{tsid}:{sid}", events


# -- MediaHighway -------------------------------------------------------------------

def _local_to_utc(naive):
    """Convert broadcaster local time (seconds as if UTC) to real UTC, using the
    local time zone LinDVB runs in (manual offset or the system one)."""
    return int(time.mktime(time.gmtime(naive)[:8] + (-1,)))


class MHWGrabber:
    """State machine for MediaHighway 1 and 2 sections.

    Feed it sections with :meth:`feed`; it returns a list of (key, [Event])
    whenever new events can be delivered.
    """

    def __init__(self, now=None):
        self.channels = {}          # channel index -> "tsid:sid"
        self.channel_names = {}
        self.themes = {}
        self.events = {}            # program/event id -> (key, Event)
        self.pending = []           # titles that arrived before the channel table
        self.summaries = {}         # event id -> text (arrived before the title)
        self._mhw1_base = None
        self._now = now

    # MHW1 -----------------------------------------------------------------
    def _mhw1_week_base(self, day, hours):
        """Port of EEPG's FF-packet handling: start of the title block in
        broadcaster local time (naive seconds)."""
        now = self._now if self._now is not None else time.time()
        yesterday = now - 86400
        tm = time.gmtime(yesterday)
        yday = (tm.tm_wday + 1) % 7                 # C: Sunday = 0
        base = calendar.timegm((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0))
        if hours > 15:
            hours -= 4
        elif hours > 7:
            hours -= 2
        else:
            day += 1
        if day > 6:
            day -= 7
        day -= yday
        if day < 1:
            day += 7
        if day == 0 and hours < 6:
            day = 7
        return day * 86400 + hours * 3600 + base

    def _mhw1_channels(self, sec):
        out = {}
        for i, off in enumerate(range(4, len(sec) - 21, 22)):
            tsid = (sec[off + 2] << 8) | sec[off + 3]
            sid = (sec[off + 4] << 8) | sec[off + 5]
            out[i] = f"{tsid}:{sid}"
            self.channel_names[i] = dvb_text(sec[off + 6:off + 22])
        return out

    def _mhw1_title(self, sec):
        if len(sec) < 42:
            return []
        if sec[3] == 0xFF:                          # separator: new time base
            self._mhw1_base = self._mhw1_week_base(sec[5] >> 5, sec[5] & 0x1F)
            return []
        if self._mhw1_base is None:
            return []
        minutes = sec[6] >> 2
        start = _local_to_utc(self._mhw1_base + minutes * 60)
        duration = ((sec[9] << 8) | sec[10]) * 60
        title = dvb_text(sec[11:34])
        prog_id = int.from_bytes(sec[38:42], "big")
        ev = Event(start, duration, title, "", self.summaries.pop(prog_id, ""),
                   self.themes.get(sec[4], ""), prog_id & 0xFFFF)
        return self._add(prog_id, sec[3] - 1, ev)

    def _mhw1_summary(self, sec):
        if len(sec) < 11 or not (sec[7] == sec[8] == sec[9] == 0xFF):
            return []
        replays = sec[10]
        offset = 11 + replays * 7
        if replays >= 10 or len(sec) < offset:
            return []
        prog_id = int.from_bytes(sec[3:7], "big")
        return self._summary(prog_id, dvb_text(sec[offset:]))

    # MHW2 -----------------------------------------------------------------
    def _mhw2_channels(self, sec):
        if len(sec) <= 120:
            return {}
        n = sec[120]
        pname = n * 8 + 121
        if len(sec) <= pname:
            return {}
        out = {}
        for i in range(n):
            off = 121 + i * 8
            tsid = (sec[off + 2] << 8) | sec[off + 3]
            sid = (sec[off + 4] << 8) | sec[off + 5]
            out[i] = f"{tsid}:{sid}"
            ln = sec[pname] & 0x0F if pname < len(sec) else 0
            self.channel_names[i] = dvb_text(sec[pname + 1:pname + 1 + ln])
            pname += ln + 1
        return out

    def _mhw2_themes(self, sec):
        if len(sec) <= 4:
            return
        for i in range(sec[4]):
            if 6 + i * 2 >= len(sec):
                return
            p1 = ((sec[5 + i * 2] << 8) | sec[6 + i * 2]) + 3
            if p1 >= len(sec):
                return
            name = ""
            for ii in range((sec[p1] & 0x3F) + 1):
                if p1 + 2 + ii * 2 >= len(sec):
                    return
                p2 = ((sec[p1 + 1 + ii * 2] << 8) | sec[p1 + 2 + ii * 2]) + 3
                if p2 >= len(sec):
                    return
                text = dvb_text(sec[p2 + 1:p2 + 1 + (sec[p2] & 0x1F)])
                if ii == 0:
                    name = text
                    self.themes[(i & 0x3F) << 6] = name
                else:
                    self.themes[((i & 0x3F) << 6) | (ii & 0x3F)] = f"{name} - {text}"

    def _mhw2_titles(self, sec):
        out, pos = [], 18
        while pos + 19 <= len(sec):
            ch = sec[pos]
            pos += 11
            mjd = (sec[pos] << 8) | sec[pos + 1]
            start = mjd_utc(mjd, sec[pos + 2], sec[pos + 3])
            duration = (((sec[pos + 5] << 8) | sec[pos + 6]) >> 4) * 60
            ln = sec[pos + 7] & 0x3F
            title = dvb_text(sec[pos + 8:pos + 8 + ln])
            pos += ln + 8
            if pos + 3 > len(sec):
                break
            theme = ((sec[7] & 0x3F) << 6) | (sec[pos] & 0x3F)
            event_id = (sec[pos + 1] << 8) | sec[pos + 2]
            pos += 3
            ev = Event(start, duration, title, "", "", self.themes.get(theme, ""), event_id)
            if event_id != 0xFFFF:
                ev.description = self.summaries.pop(event_id, "")
            out += self._add(event_id if event_id != 0xFFFF else None, ch, ev)
        return out

    def _mhw2_summary(self, sec):
        if len(sec) < 16 or len(sec) <= sec[14] + 17:
            return []
        event_id = (sec[3] << 8) | sec[4]
        ln = sec[14]
        pos = 15
        parts = [dvb_text(sec[pos:pos + ln])]
        loops = sec[pos + ln] & 0x0F
        pos += ln + 1
        while loops > 0 and pos < len(sec):
            ln = sec[pos]
            pos += 1
            if pos + ln >= len(sec):
                break
            parts.append(dvb_text(sec[pos:pos + ln]))
            pos += ln
            loops -= 1
        first, rest = parts[0], " ".join(p for p in parts[1:] if p)
        return self._summary(event_id, first + ("\n" + rest if rest else ""))

    # shared ---------------------------------------------------------------
    def _add(self, ident, channel_index, ev):
        if not ev.title or ev.start <= 0 or ev.duration <= 0:
            return []
        if not self.channels:
            self.pending.append((ident, channel_index, ev))
            return []
        key = self.channels.get(channel_index)
        if key is None:
            return []
        if ident is not None:
            self.events[ident] = (key, ev)
        return [(key, [ev])]

    def _summary(self, ident, text):
        if not text:
            return []
        hit = self.events.get(ident)
        if hit is None:
            self.summaries[ident] = text
            return []
        key, ev = hit
        if ev.description == text:
            return []
        ev.description = text
        return [(key, [ev])]

    def _set_channels(self, table):
        if not table:
            return []
        self.channels = table
        out, pending, self.pending = [], self.pending, []
        for ident, idx, ev in pending:
            out += self._add(ident, idx, ev)
        return out

    def feed(self, pid, sec):
        """Process one section from *pid*. Returns [(key, [Event])]."""
        if len(sec) < 4:
            return []
        tid = sec[0]
        if pid == 0xD2 and tid == 0x90:
            return self._mhw1_title(sec)
        if pid == 0xD3:
            if tid == 0x91:
                return self._set_channels(self._mhw1_channels(sec))
            if tid == 0x92:
                self._mhw1_themes(sec)
                return []
            if tid == 0x90:
                return self._mhw1_summary(sec)
        if pid == 0x231 and tid == 0xC8:
            if sec[3] == 0x00 and not self.channels:
                return self._set_channels(self._mhw2_channels(sec))
            if sec[3] == 0x01:
                self._mhw2_themes(sec)
            return []
        if pid == 0x234 and tid == 0xE6:
            return self._mhw2_titles(sec)
        if pid == 0x236 and tid == 0x96:
            return self._mhw2_summary(sec)
        return []

    def _mhw1_themes(self, sec):
        # 15-byte theme names from offset 19; groups are separated by an
        # index jump that EEPG computes from the header. Keep it simple and
        # number them in order, which matches the title theme byte for the
        # common case of one theme group.
        for i, off in enumerate(range(19, len(sec) - 14, 15)):
            name = dvb_text(sec[off:off + 15])
            if name:
                self.themes.setdefault(i, name)


# -- packet level -------------------------------------------------------------------

class ProviderEPG:
    """Collects provider EPG from TS packets of a capture.

    ``on_events(key, events)`` is called (from the streaming thread) for every
    batch of decoded events.
    """

    def __init__(self, kinds, on_events, language=""):
        from .ca import SectionAssembler
        self.kinds = list(kinds)
        self.language = language
        self.on_events = on_events
        self.pids = set(provider_pids(self.kinds))
        self._asm = {pid: SectionAssembler() for pid in self.pids}
        self._mhw = MHWGrabber() if any(k.startswith("mhw") for k in self.kinds) else None
        self._seen = set()

    def feed_packet(self, packet):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        asm = self._asm.get(pid)
        if asm is None:
            return
        for section in asm.feed(packet):
            try:
                self.feed_section(pid, section)
            except (IndexError, ValueError):
                pass                          # corrupt or unexpected section

    def feed_section(self, pid, section):
        if pid in (3842, 3843):
            if not (0x4E <= section[0] <= 0x6F) or not crc_ok(section):
                return
            # every schedule section repeats; skip the ones already decoded
            sig = (pid, zlib.crc32(section))
            if sig in self._seen:
                return
            if len(self._seen) > 200000:
                self._seen.clear()
            self._seen.add(sig)
            res = parse_eit_raw(section, self.language)
            if res and res[1]:
                self.on_events(*res)
        elif self._mhw is not None:
            for key, events in self._mhw.feed(pid, section):
                self.on_events(key, events)

    def feed_buffer(self, data):
        """Feed a buffer of whole TS packets (possibly not aligned)."""
        n, i = len(data), data.find(0x47)
        while 0 <= i and i + 188 <= n:
            if data[i] != 0x47:
                i += 1
                continue
            pid = ((data[i + 1] & 0x1F) << 8) | data[i + 2]
            if pid in self.pids:
                self.feed_packet(memoryview(data)[i:i + 188])
            i += 188
