"""ATSC programme guide (PSIP): VCT, EIT, ETT and STT.

ATSC (USA, Canada, Mexico, South Korea) carries its guide differently from
DVB: the virtual channel table (TVCT / CVCT) names each channel with its
major.minor number and a ``source_id``; the event information tables (EIT-0
to EIT-127, three hours each) list the programmes of a source with GPS start
times, and the extended text tables (ETT) carry the descriptions. The PIDs
come from the master guide table (MGT), which GStreamer's tsparse follows
itself. Events come out under the same "tsid:program_number" key as DVB EIT,
so the guide, timers and the info bar work unchanged.

The tables are read from the raw section bytes: reading the table lists of
GstMpegts' ATSC structures from Python frees them twice (a binding bug).
"""

import struct

from .epg import Event

GPS_EPOCH = 315964800          # 1980-01-06 00:00:00 UTC in unix time
DEFAULT_LEAP_SECONDS = 18      # GPS - UTC since 2017, until the STT tells

TVCT, CVCT, EIT, ETT, STT = 0xC8, 0xC9, 0xCB, 0xCC, 0xCD


def gps_to_unix(gps_seconds, leap=DEFAULT_LEAP_SECONDS):
    return int(gps_seconds) + GPS_EPOCH - int(leap)


def etm_id(source_id, event_id):
    """ETM id of an event's extended text (ATSC A/65, 6.6.1)."""
    return (source_id << 16) | (event_id << 2) | 0x2


def _segment_text(compression, mode, raw):
    if compression != 0:
        return ""            # Huffman coded (A/65 Annex C): not supported
    if mode == 0x3F:
        return raw.decode("utf-16-be", "replace")
    if mode <= 0x33:          # one Unicode page: (mode << 8) | byte
        return "".join(chr((mode << 8) | b) for b in raw)
    return ""


def read_multiple_string(data, pos=0):
    """Parse a multiple_string_structure: ([(language, text)], next position)."""
    out = []
    n = data[pos]
    pos += 1
    for _ in range(n):
        lang = data[pos:pos + 3].decode("latin-1").strip("\0 ").lower()
        segs = data[pos + 3]
        pos += 4
        text = ""
        for _ in range(segs):
            compression, mode, nbytes = data[pos], data[pos + 1], data[pos + 2]
            text += _segment_text(compression, mode, data[pos + 3:pos + 3 + nbytes])
            pos += 3 + nbytes
        if text:
            out.append((lang, text))
    return out, pos


def pick_text(texts, language=""):
    """The preferred language, else English, else the first text."""
    for want in (language.lower(), "eng"):
        for lang, text in texts:
            if want and lang == want:
                return text
    return texts[0][1] if texts else ""


class AtscGuide:
    """Collects PSIP sections of one transport stream and returns guide events."""

    def __init__(self, language=""):
        self.language = language
        self.leap = DEFAULT_LEAP_SECONDS
        self.sources = {}          # source_id -> (tsid, program_number)
        self.channels = {}         # program_number -> ("major.minor", short name)
        self.pending = {}          # source_id -> {event_id: Event} before the VCT came
        self.events = {}           # (source_id, event_id) -> Event
        self.texts = {}            # etm_id -> description

    def feed(self, section, _GstMpegts=None):
        """A GstMpegts.Section; returns a list of (key, [Event]) (often empty)."""
        try:
            data = section.get_data().get_data()
        except (AttributeError, TypeError):
            return []
        return self.feed_bytes(bytes(data))

    def feed_bytes(self, data):
        """One complete PSIP section (with header and CRC)."""
        if len(data) < 13:
            return []
        table = data[0]
        length = ((data[1] & 0x0F) << 8) | data[2]
        data = data[:3 + length - 4]              # without the CRC
        ext = (data[3] << 8) | data[4]
        try:
            if table in (TVCT, CVCT):
                return self._vct(data, ext)
            if table == STT:
                self.leap = data[13]
                return []
            if table == EIT:
                return self._eit(data, ext)
            if table == ETT:
                return self._ett(data)
        except (IndexError, struct.error):
            pass                                  # a broken section costs nothing
        return []

    def key(self, source_id):
        tsid, program = self.sources[source_id]
        return f"{tsid}:{program}"

    def _vct(self, data, tsid):
        out = []
        n = data[9]
        pos = 10
        for _ in range(n):
            name = data[pos:pos + 14].decode("utf-16-be", "replace").strip("\0 ")
            nums = int.from_bytes(data[pos + 14:pos + 17], "big")
            major, minor = (nums >> 10) & 0x3FF, nums & 0x3FF
            ch_tsid, program, _flags, source_id, desc_len = struct.unpack(
                ">HHHHH", data[pos + 22:pos + 32])
            desc_len &= 0x3FF
            pos += 32 + desc_len
            self.sources[source_id] = (ch_tsid or tsid, program)
            self.channels[program] = (f"{major}.{minor}", name)
            waiting = self.pending.pop(source_id, None)
            if waiting:
                out.append((self.key(source_id), list(waiting.values())))
        return out

    def _eit(self, data, source_id):
        n = data[9]
        pos = 10
        events = []
        for _ in range(n):
            event_id = ((data[pos] & 0x3F) << 8) | data[pos + 1]
            start = struct.unpack(">I", data[pos + 2:pos + 6])[0]
            length = int.from_bytes(data[pos + 6:pos + 9], "big") & 0xFFFFF
            title_len = data[pos + 9]
            titles, _end = read_multiple_string(data, pos + 10) if title_len else ([], 0)
            pos += 10 + title_len
            desc_len = ((data[pos] & 0x0F) << 8) | data[pos + 1]
            pos += 2 + desc_len
            title = pick_text(titles, self.language)
            if not title or not length:
                continue
            e = Event(gps_to_unix(start, self.leap), length, title, event_id=event_id,
                      description=self.texts.get(etm_id(source_id, event_id), ""))
            self.events[(source_id, event_id)] = e
            events.append(e)
        if not events:
            return []
        if source_id not in self.sources:
            self.pending.setdefault(source_id, {}).update({e.event_id: e for e in events})
            return []
        return [(self.key(source_id), events)]

    def _ett(self, data):
        eid = struct.unpack(">I", data[9:13])[0]
        if (eid & 0x3) != 0x2:                     # channel texts, not event texts
            return []
        texts, _end = read_multiple_string(data, 13)
        text = pick_text(texts, self.language)
        if not text:
            return []
        self.texts[eid] = text
        source_id, event_id = eid >> 16, (eid >> 2) & 0x3FFF
        e = self.events.get((source_id, event_id))
        if e is None or e.description == text:
            return []
        e.description = text
        if source_id not in self.sources:
            return []
        return [(self.key(source_id), [e])]
