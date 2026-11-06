# SPDX-License-Identifier: GPL-2.0-or-later
"""Live analysis of an MPEG transport stream, for the channel analysis window.

TsAnalyzer takes the transport stream packet by packet (from the tuner's
capture or from the player's demuxer, see lindvb.analysis) and counts for each
PID the packets, continuity errors, transport errors and scrambled packets. It
reads the PCR, the PSI/SI tables (PAT, CAT, PMT, NIT, SDT, EIT, TDT/TOT) and the
start of every video and audio PES packet, which give codecs, nominal bit rates,
frame sizes and picture types (lindvb.esparse).

feed() runs in the GStreamer streaming thread; snapshot() is called from the
main loop about once a second and returns plain data. Both hold one lock.
"""

import struct
import threading
import time
from collections import deque

from . import esparse
from .ca import SectionAssembler

TS_PACKET = 188
TS_SYNC = 0x47
NULL_PID = 0x1FFF
PCR_HZ = 27_000_000
PCR_WRAP = (1 << 33) * 300
FRAMES_KEPT = 240                     # video frames remembered for the frame chart
ES_HEAD_BYTES = 6144                  # start of each video PES read for headers
AUDIO_PARSE_EVERY = 2.0               # seconds between audio header reads

# --------------------------------------------------------------------------
#  names
# --------------------------------------------------------------------------

STREAM_TYPES = {
    0x01: ("video", "MPEG-1"), 0x02: ("video", "MPEG-2"), 0x03: ("audio", "MPEG-1 Audio"),
    0x04: ("audio", "MPEG-2 Audio"), 0x05: ("data", "Private sections"),
    0x06: ("data", "PES private data"), 0x0B: ("data", "DSM-CC U-N"),
    0x0C: ("data", "DSM-CC stream descriptors"), 0x0D: ("data", "DSM-CC sections"),
    0x0F: ("audio", "AAC"), 0x11: ("audio", "HE-AAC (LATM)"), 0x15: ("data", "Metadata"),
    0x1B: ("video", "H.264"), 0x24: ("video", "HEVC"), 0x33: ("video", "VVC"),
    0x42: ("video", "AVS"), 0x81: ("audio", "AC-3"), 0x86: ("data", "SCTE-35"),
    0x87: ("audio", "E-AC-3"), 0xD2: ("video", "AVS2"),
}

CA_SYSTEMS = (
    (0x0100, 0x01FF, "Seca / Mediaguard"), (0x0500, 0x05FF, "Viaccess"),
    (0x0600, 0x06FF, "Irdeto"), (0x0900, 0x09FF, "NDS Videoguard"),
    (0x0B00, 0x0BFF, "Conax"), (0x0D00, 0x0DFF, "Cryptoworks"),
    (0x0E00, 0x0EFF, "PowerVu"), (0x1000, 0x10FF, "Tandberg"),
    (0x1700, 0x17FF, "BetaCrypt"), (0x1800, 0x18FF, "Nagravision"),
    (0x2200, 0x22FF, "Codicrypt"), (0x2600, 0x26FF, "BISS"),
    (0x4A00, 0x4AFF, "DreCrypt / Tongfang"), (0x4AE0, 0x4AE1, "DRE-Crypt"),
    (0x5581, 0x5581, "Bulcrypt"), (0x56B0, 0x56B0, "Verimatrix"),
    (0x7BE0, 0x7BE1, "DRE-Crypt"), (0xA100, 0xA1FF, "RusCrypto"),
)

DESCRIPTOR_NAMES = {
    0x02: "video_stream", 0x03: "audio_stream", 0x05: "registration", 0x09: "CA",
    0x0A: "ISO_639_language", 0x0E: "maximum_bitrate", 0x11: "STD", 0x13: "carousel_identifier",
    0x14: "association_tag", 0x26: "metadata", 0x28: "AVC_video", 0x2A: "AVC_timing_and_HRD",
    0x38: "HEVC_video", 0x40: "network_name", 0x41: "service_list", 0x42: "stuffing",
    0x43: "satellite_delivery_system", 0x44: "cable_delivery_system", 0x45: "VBI_data",
    0x47: "bouquet_name", 0x48: "service", 0x49: "country_availability", 0x4A: "linkage",
    0x4B: "NVOD_reference", 0x4D: "short_event", 0x4E: "extended_event", 0x50: "component",
    0x52: "stream_identifier", 0x53: "CA_identifier", 0x54: "content", 0x55: "parental_rating",
    0x56: "teletext", 0x58: "local_time_offset", 0x59: "subtitling",
    0x5A: "terrestrial_delivery_system", 0x5D: "multilingual_service_name",
    0x5F: "private_data_specifier", 0x62: "frequency_list", 0x64: "data_broadcast",
    0x66: "data_broadcast_id", 0x69: "PDC", 0x6A: "AC-3", 0x6F: "application_signalling",
    0x73: "default_authority", 0x74: "related_content", 0x76: "content_identifier",
    0x79: "S2_satellite_delivery_system", 0x7A: "enhanced_AC-3", 0x7B: "DTS", 0x7C: "AAC",
    0x7E: "FTA_content_management", 0x7F: "extension", 0x83: "logical_channel",
    0x86: "caption_service", 0x87: "content_advisory",
}

SERVICE_TYPES = {0x01: "TV (SD)", 0x02: "Radio", 0x0A: "Radio (AAC)", 0x0C: "Data",
                 0x11: "TV (MPEG-2 HD)", 0x16: "TV (H.264 SD)", 0x19: "TV (H.264 HD)",
                 0x1F: "TV (HEVC)", 0x20: "TV (HEVC UHD)"}


def ca_system_name(caid):
    for lo, hi, name in reversed(CA_SYSTEMS):
        if lo <= caid <= hi:
            return name
    return ""


def descriptors(data):
    """Yield (tag, body) of a descriptor loop."""
    i = 0
    while i + 2 <= len(data):
        tag, length = data[i], data[i + 1]
        yield tag, bytes(data[i + 2:i + 2 + length])
        i += 2 + length


def _lang(b):
    try:
        return bytes(b[:3]).decode("latin-1").strip()
    except Exception:
        return ""


def dvb_text(b):
    """A DVB string (EN 300 468 annex A) as str, for names in SDT/NIT/EIT."""
    if not b:
        return ""
    enc = "latin-1"
    if b[0] < 0x20:
        first = b[0]
        b = b[1:]
        if first == 0x15:
            enc = "utf-8"
        elif first == 0x10 and len(b) >= 2:
            enc = {1: "iso8859-1", 2: "iso8859-2", 5: "iso8859-5", 7: "iso8859-7",
                   9: "iso8859-9", 15: "iso8859-15"}.get(b[1], "latin-1")
            b = b[2:]
        elif 0x01 <= first <= 0x0B:
            enc = f"iso8859-{first + 4}"
        elif first == 0x11:
            enc = "utf-16-be"
    text = bytes(b).decode(enc, "replace")
    return "".join(ch for ch in text if ch >= " " or ch == "\n").replace("\x86", "").replace("\x87", "")


# --------------------------------------------------------------------------
#  PSI/SI tables
# --------------------------------------------------------------------------

TABLE_NAMES = {0x00: "PAT", 0x01: "CAT", 0x02: "PMT", 0x40: "NIT actual", 0x41: "NIT other",
               0x42: "SDT actual", 0x46: "SDT other", 0x4A: "BAT", 0x4E: "EIT p/f actual",
               0x4F: "EIT p/f other", 0x70: "TDT", 0x73: "TOT", 0x74: "AIT", 0x3B: "DSM-CC",
               0x3C: "DSM-CC", 0x3D: "DSM-CC"}


def table_name(tid):
    if tid in TABLE_NAMES:
        return TABLE_NAMES[tid]
    if 0x50 <= tid <= 0x5F:
        return "EIT schedule actual"
    if 0x60 <= tid <= 0x6F:
        return "EIT schedule other"
    return f"0x{tid:02X}"


def section_header(sec):
    """(table_id, table_id_extension, version, section_number, last_section_number)
    of a long section; short sections (TDT/TOT) give ext/version -1."""
    tid = sec[0]
    if not sec[1] & 0x80 or len(sec) < 8:
        return tid, -1, -1, 0, 0
    return tid, (sec[3] << 8) | sec[4], (sec[5] >> 1) & 0x1F, sec[6], sec[7]


def parse_pat(sec):
    """{program_number: pmt_pid} (program 0 = NIT PID)."""
    end = 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4
    out = {}
    for i in range(8, end - 3, 4):
        prog = (sec[i] << 8) | sec[i + 1]
        out[prog] = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
    return out


def parse_cat(sec):
    """[(caid, emm_pid)]"""
    end = 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4
    out = []
    for tag, body in descriptors(sec[8:end]):
        if tag == 0x09 and len(body) >= 4:
            out.append(((body[0] << 8) | body[1], ((body[2] & 0x1F) << 8) | body[3]))
    return out


def describe_stream(stream_type, info):
    """What an ES of a PMT carries: dict kind, codec, language, ... from its descriptors."""
    kind, codec = STREAM_TYPES.get(stream_type, ("data", f"type 0x{stream_type:02X}"))
    d = {"kind": kind, "codec": codec, "stream_type": stream_type, "ca": [],
         "descriptors": []}
    for tag, body in descriptors(info):
        d["descriptors"].append(tag)
        if tag == 0x0A and len(body) >= 4:
            d["language"] = _lang(body)
            d["audio_type"] = body[3]
        elif tag == 0x09 and len(body) >= 4:
            d["ca"].append(((body[0] << 8) | body[1], ((body[2] & 0x1F) << 8) | body[3]))
        elif tag == 0x0E and len(body) >= 3:
            d["max_bps"] = (((body[0] & 0x3F) << 16) | (body[1] << 8) | body[2]) * 50 * 8
        elif tag == 0x52 and body:
            d["component_tag"] = body[0]
        elif tag == 0x6A:
            d.update(kind="audio", codec="AC-3")
        elif tag == 0x7A:
            d.update(kind="audio", codec="E-AC-3")
        elif tag == 0x7B:
            d.update(kind="audio", codec="DTS")
        elif tag == 0x7C:
            d.update(kind="audio", codec="AAC")
        elif tag == 0x7F and body:
            if body[0] == 0x15:
                d.update(kind="audio", codec="AC-4")
            elif body[0] == 0x06 and len(body) >= 2:      # supplementary audio
                d["audio_description"] = ((body[1] >> 2) & 0x1F) == 1
                if len(body) >= 5 and body[1] & 1:
                    d.setdefault("language", _lang(body[2:5]))
        elif tag == 0x56:
            d.update(kind="teletext", codec="Teletext")
            pages = []
            for k in range(0, len(body) - 4, 5):
                ttype = body[k + 3] >> 3
                mag = body[k + 3] & 7 or 8
                pages.append((_lang(body[k:k + 3]), ttype, f"{mag}{body[k + 4]:02X}"))
            d["pages"] = pages
            if pages:
                d.setdefault("language", pages[0][0])
        elif tag == 0x59:
            d.update(kind="subtitle", codec="DVB-SUB")
            subs = []
            for k in range(0, len(body) - 7, 8):
                subs.append((_lang(body[k:k + 3]), body[k + 3]))
            d["subtitles"] = subs
            if subs:
                d["language"] = subs[0][0]
                d["hard_of_hearing"] = any(0x20 <= t <= 0x25 for _l, t in subs)
        elif tag == 0x6F:
            d.update(kind="data", codec="HbbTV AIT", ait=True)
        elif tag == 0x66 and len(body) >= 2:
            dbid = (body[0] << 8) | body[1]
            d["data_broadcast_id"] = dbid
            if dbid in (0x0006, 0x0007, 0x0106, 0x0123):
                d["codec"] = "DSM-CC carousel"
        elif tag == 0x13:
            d["codec"] = "DSM-CC carousel"
        elif tag == 0x45:
            d.update(kind="data", codec="VBI")
    if d.get("audio_type") == 3:
        d["audio_description"] = True
    if d.get("audio_type") == 2 and d["kind"] == "audio":
        d["hard_of_hearing"] = True
    return d


def parse_pmt_full(sec):
    """dict: sid, version, pcr_pid, ca [(caid, pid)], streams [(pid, describe_stream)]"""
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    end = 3 + length - 4
    sid = (sec[3] << 8) | sec[4]
    out = {"sid": sid, "version": (sec[5] >> 1) & 0x1F,
           "pcr_pid": ((sec[8] & 0x1F) << 8) | sec[9], "ca": [], "streams": []}
    pil = ((sec[10] & 0x0F) << 8) | sec[11]
    for tag, body in descriptors(sec[12:12 + pil]):
        if tag == 0x09 and len(body) >= 4:
            out["ca"].append(((body[0] << 8) | body[1], ((body[2] & 0x1F) << 8) | body[3]))
    i = 12 + pil
    while i + 5 <= end:
        st = sec[i]
        pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
        el = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
        out["streams"].append((pid, describe_stream(st, sec[i + 5:i + 5 + el])))
        i += 5 + el
    return out


def parse_sdt(sec):
    """{sid: {name, provider, type, scrambled}}"""
    end = 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4
    out = {}
    i = 11
    while i + 5 <= end:
        sid = (sec[i] << 8) | sec[i + 1]
        free_ca = bool(sec[i + 3] & 0x10)
        dl = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
        info = {"scrambled": free_ca}
        for tag, body in descriptors(sec[i + 5:i + 5 + dl]):
            if tag == 0x48 and len(body) >= 2:
                info["type"] = body[0]
                pl = body[1]
                info["provider"] = dvb_text(body[2:2 + pl])
                nl = body[2 + pl] if len(body) > 2 + pl else 0
                info["name"] = dvb_text(body[3 + pl:3 + pl + nl])
        out[sid] = info
        i += 5 + dl
    return out


def parse_nit_name(sec):
    nl = ((sec[8] & 0x0F) << 8) | sec[9]
    for tag, body in descriptors(sec[10:10 + nl]):
        if tag == 0x40:
            return dvb_text(body)
    return ""


def mjd_utc(b):
    """Unix time from the 40-bit MJD + BCD UTC field of TDT/TOT/EIT."""
    mjd = (b[0] << 8) | b[1]

    def bcd(x):
        return (x >> 4) * 10 + (x & 0x0F)
    return (mjd - 40587) * 86400 + bcd(b[2]) * 3600 + bcd(b[3]) * 60 + bcd(b[4])


# --------------------------------------------------------------------------
#  per-PID state
# --------------------------------------------------------------------------


class PidState:
    __slots__ = ("packets", "cc", "cc_errors", "tei", "scrambled", "parity", "parity_changes",
                 "parity_at", "crypto_periods", "pcr_last", "pcr_last_at", "pcr_intervals",
                 "pcr_jumps", "pcr_points", "first_seen", "last_seen", "dup")

    def __init__(self, now):
        self.packets = 0
        self.cc = -1
        self.dup = False
        self.cc_errors = 0
        self.tei = 0
        self.scrambled = 0
        self.parity = 0
        self.parity_changes = 0
        self.parity_at = 0.0
        self.crypto_periods = deque(maxlen=8)
        self.pcr_last = None
        self.pcr_last_at = 0.0
        self.pcr_intervals = deque(maxlen=200)
        self.pcr_jumps = 0
        self.pcr_points = deque(maxlen=300)
        self.first_seen = now
        self.last_seen = now


class EsState:
    """A video or audio stream whose PES packets are looked into."""

    def __init__(self, pid, kind, stream_type):
        self.pid, self.kind, self.stream_type = pid, kind, stream_type
        self.head = bytearray()
        self.size = 0
        self.pts = None
        self.frames = deque(maxlen=FRAMES_KEPT)     # (size bytes, picture type)
        self.pts_deltas = deque(maxlen=64)
        self.info = {}
        self.parsed_at = 0.0
        self.hevc = {}
        self.afd = None
        self.rff = 0
        self.progressive = 0
        self.frames_total = 0
        self.last_i = -1
        self.gops = deque(maxlen=16)

    def video_kind(self):
        if self.stream_type in (0x01, 0x02):
            return "mpeg2"
        if self.stream_type == 0x1B:
            return "h264"
        if self.stream_type == 0x24:
            return "hevc"
        return ""


class TsAnalyzer:
    """Counts and parses one transport stream (see module doc)."""

    def __init__(self, sid=0, clock=time.monotonic, wallclock=time.time):
        self.sid = sid
        self.clock = clock
        self.wallclock = wallclock
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        now = self.clock()
        self.started = now
        self.pids = {}
        self.packets = 0
        self.sync_losses = 0
        self.tei = 0
        self.cc_errors = 0
        self._rest = b""
        self._asm = {}                 # pid -> SectionAssembler (PSI)
        for pid in (0x00, 0x01, 0x10, 0x11, 0x12, 0x14):
            self._asm[pid] = SectionAssembler()
        self.tables = {}               # (tid, ext) -> dict
        self.raw = {}                  # name -> latest section bytes (for the tree view)
        self.pat = {}
        self.tsid = None
        self.pmts = {}                 # sid -> parse_pmt_full
        self.cat = []
        self.services = {}             # sid -> SDT info
        self.network = ""
        self.onid = None
        self.utc_offset = None         # TDT time - wall clock (s)
        self.local_offset = None
        self.es = {}                   # pid -> EsState
        self.events = deque(maxlen=500)  # (monotonic time, kind, detail)

    # -- feeding -----------------------------------------------------------
    def feed(self, data, now=None):
        """Feed raw transport stream bytes (any alignment)."""
        now = self.clock() if now is None else now
        with self.lock:
            if self._rest:
                data = self._rest + bytes(data)
                self._rest = b""
            n = len(data)
            i = 0
            mv = memoryview(data)
            while i + TS_PACKET <= n:
                if data[i] != TS_SYNC:
                    j = bytes(mv[i:]).find(b"\x47")
                    self.sync_losses += 1
                    if j < 0:
                        i = n
                        break
                    i += j
                    if i + TS_PACKET > n:
                        break
                    if i + TS_PACKET < n and data[i + TS_PACKET] != TS_SYNC:
                        i += 1
                        continue
                self._packet(mv[i:i + TS_PACKET], now)
                i += TS_PACKET
            if i < n:
                self._rest = bytes(mv[i:])

    def _packet(self, p, now):
        b1 = p[1]
        pid = ((b1 & 0x1F) << 8) | p[2]
        b3 = p[3]
        st = self.pids.get(pid)
        if st is None:
            st = self.pids[pid] = PidState(now)
        st.packets += 1
        st.last_seen = now
        self.packets += 1
        if b1 & 0x80:
            st.tei += 1
            self.tei += 1
            return
        if pid == NULL_PID:
            return
        sc = b3 >> 6
        if sc:
            st.scrambled += 1
            if sc != st.parity:
                if st.parity:
                    st.parity_changes += 1
                    if st.parity_at:
                        st.crypto_periods.append(now - st.parity_at)
                    st.parity_at = now
                st.parity = sc
        afc = (b3 >> 4) & 3
        cc = b3 & 0x0F
        disc = False
        if afc & 2 and p[4] > 0:
            flags = p[5]
            disc = bool(flags & 0x80)
            if flags & 0x10 and p[4] >= 7:
                base = (p[6] << 25) | (p[7] << 17) | (p[8] << 9) | (p[9] << 1) | (p[10] >> 7)
                ext = ((p[10] & 1) << 8) | p[11]
                self._pcr(st, base * 300 + ext, now, disc)
        if afc & 1:
            if st.cc >= 0 and not disc:
                expect = (st.cc + 1) & 0x0F
                if cc != expect:
                    if cc == st.cc and not st.dup:
                        st.dup = True
                    else:
                        st.cc_errors += 1
                        self.cc_errors += 1
                        self.events.append((now, "cc", pid))
                        st.dup = False
                else:
                    st.dup = False
            st.cc = cc
            asm = self._asm.get(pid)
            if asm is not None:
                if not sc:
                    for sec in asm.feed(p):
                        self._section(pid, sec, now)
                return
            es = self.es.get(pid)
            if es is not None and not sc:
                off = 4 if afc == 1 else 5 + p[4]
                if off < TS_PACKET:
                    self._es_payload(es, p[off:], bool(b1 & 0x40), now)

    def _pcr(self, st, pcr, now, disc):
        if st.pcr_last is not None and not disc:
            delta = (pcr - st.pcr_last) % PCR_WRAP
            if 0 < delta < PCR_HZ:          # under a second: a normal step
                st.pcr_intervals.append(delta / PCR_HZ)
            else:
                st.pcr_jumps += 1
                st.pcr_points.clear()
                self.events.append((now, "pcr", 0))
        elif disc:
            st.pcr_points.clear()
        st.pcr_last = pcr
        st.pcr_last_at = now
        st.pcr_points.append((now, pcr / PCR_HZ))

    # -- PSI ---------------------------------------------------------------
    def _section(self, pid, sec, now):
        if len(sec) < 3:
            return
        tid, ext, ver, num, _last = section_header(sec)
        if tid == 0xFF:
            return
        if 0x50 <= tid <= 0x6F:
            key = (tid & 0xF0 if tid < 0x60 else 0x60, -2)
        elif tid in (0x4E, 0x4F) and ext != self.sid:
            key = (0x4F if tid == 0x4F else 0x4E, -3)
        else:
            key = (tid, ext)
        t = self.tables.get(key)
        if t is None:
            t = self.tables[key] = {"tid": tid, "ext": ext, "pid": pid, "version": ver,
                                    "count": 0, "first": now, "last": now, "intervals": deque(maxlen=20),
                                    "versions": 0}
        elif num == 0:
            if t.get("last0"):
                t["intervals"].append(now - t["last0"])
        if num == 0:
            t["last0"] = now
        if ver != t["version"]:
            t["versions"] += 1
            t["version"] = ver
            self.events.append((now, "version", tid))
        t["count"] += 1
        t["last"] = now
        try:
            self._decode(tid, ext, pid, sec, now)
        except (IndexError, ValueError, struct.error):
            pass

    def _decode(self, tid, ext, pid, sec, now):
        if tid == 0x00:
            self.raw["PAT"] = sec
            self.tsid = ext
            pat = parse_pat(sec)
            if pat != self.pat:
                self.pat = pat
                for prog, pmt in pat.items():
                    if prog and pmt not in self._asm:
                        self._asm[pmt] = SectionAssembler()
        elif tid == 0x01:
            self.raw["CAT"] = sec
            self.cat = parse_cat(sec)
        elif tid == 0x02:
            pmt = parse_pmt_full(sec)
            old = self.pmts.get(pmt["sid"])
            self.pmts[pmt["sid"]] = pmt
            if pmt["sid"] == self.sid or (not self.sid and len(self.pat) <= 2):
                self.raw["PMT"] = sec
                if old is None or old["version"] != pmt["version"]:
                    self._watch_es(pmt)
        elif tid == 0x42:
            self.raw.setdefault("SDT", sec)
            if (sec[3] << 8 | sec[4]) == self.tsid or self.tsid is None:
                self.onid = (sec[8] << 8) | sec[9]
                self.services.update(parse_sdt(sec))
                self.raw["SDT"] = sec
        elif tid == 0x40:
            self.raw["NIT"] = sec
            self.network = parse_nit_name(sec) or self.network
        elif tid == 0x4E and ext == self.sid:
            self.raw["EIT p/f" if sec[6] == 0 else "EIT p/f (next)"] = sec
        elif tid in (0x70, 0x73):
            self.raw["TDT" if tid == 0x70 else "TOT"] = sec
            self.utc_offset = mjd_utc(sec[3:8]) - self.wallclock()
            if tid == 0x73:
                dl = ((sec[8] & 0x0F) << 8) | sec[9]
                for tag, body in descriptors(sec[10:10 + dl]):
                    if tag == 0x58 and len(body) >= 13:
                        pol = -1 if body[3] & 1 else 1
                        h, m = body[4], body[5]
                        self.local_offset = pol * (((h >> 4) * 10 + (h & 15)) * 60 +
                                                   (m >> 4) * 10 + (m & 15))
                        break
        elif tid == 0x74:
            self.raw["AIT"] = sec

    def _watch_es(self, pmt):
        keep = {}
        for pid, d in pmt["streams"]:
            if d["kind"] in ("video", "audio"):
                old = self.es.get(pid)
                keep[pid] = old if old is not None and old.stream_type == d["stream_type"] \
                    else EsState(pid, d["kind"], d["stream_type"])
            elif d.get("ait") and pid not in self._asm:
                self._asm[pid] = SectionAssembler()
        self.es = keep

    def watch_service(self, sid):
        with self.lock:
            self.sid = sid
            if sid in self.pmts:
                self._watch_es(self.pmts[sid])

    # -- elementary streams --------------------------------------------------
    def _es_payload(self, es, payload, start, now):
        if start:
            if es.size:
                self._frame_done(es, now)
            es.head = bytearray(payload)
            es.size = len(payload)
            return
        if not es.size:
            return
        es.size += len(payload)
        if len(es.head) < ES_HEAD_BYTES and (es.kind == "video" or len(es.head) < 512):
            es.head += payload

    def _frame_done(self, es, now):
        head = bytes(es.head)
        hdr = esparse.pes_header(head)
        if hdr is None:
            return
        _sid, pts, off = hdr
        data = head[off:]
        if pts is not None:
            if es.pts is not None:
                d = (pts - es.pts) % (1 << 33)
                if 0 < d < 90000:
                    es.pts_deltas.append(d)
            es.pts = pts
        if es.kind == "audio":
            if now - es.parsed_at >= AUDIO_PARSE_EVERY or not es.info:
                info = esparse.parse_audio(data)
                if info:
                    es.info = info
                    es.parsed_at = now
            return
        kind = es.video_kind()
        try:
            if kind == "mpeg2":
                info = esparse.parse_mpeg2(data)
            elif kind == "h264":
                info = esparse.parse_h264(data)
            elif kind == "hevc":
                info = esparse.parse_hevc(data, es.hevc)
            else:
                info = {}
        except (ValueError, IndexError):
            info = {}
        pic = info.pop("picture", "")
        if "repeat_first_field" in info:
            es.rff += info.pop("repeat_first_field")
            es.progressive += info.pop("progressive_frame", False)
        if "width" in info or "codec" in info:
            merged = dict(es.info)
            merged.update(info)
            es.info = merged
        afd = esparse.find_afd(data)
        if afd is not None:
            es.afd = afd
        es.frames.append((es.size, pic))
        es.frames_total += 1
        if pic == "I":
            if es.last_i >= 0:
                es.gops.append(es.frames_total - es.last_i)
            es.last_i = es.frames_total

    # -- results -------------------------------------------------------------
    def _service_pmt(self):
        if self.sid in self.pmts:
            return self.pmts[self.sid]
        if not self.sid and len(self.pmts) == 1:      # internet streams: the only one
            return next(iter(self.pmts.values()))
        return None

    def snapshot(self):
        """Plain data for the user interface (counters are totals since reset)."""
        now = self.clock()
        with self.lock:
            pids = {}
            for pid, st in self.pids.items():
                d = {"packets": st.packets, "cc_errors": st.cc_errors, "tei": st.tei,
                     "scrambled": st.scrambled, "parity_changes": st.parity_changes,
                     "crypto_period": (sum(st.crypto_periods) / len(st.crypto_periods))
                     if st.crypto_periods else None,
                     "age": now - st.last_seen}
                if st.pcr_intervals:
                    iv = list(st.pcr_intervals)
                    d["pcr_avg"] = sum(iv) / len(iv)
                    d["pcr_max"] = max(iv)
                    d["pcr_jitter"] = _pcr_jitter(st.pcr_points)
                    d["pcr_jumps"] = st.pcr_jumps
                pids[pid] = d
            es = {}
            for pid, e in self.es.items():
                d = dict(e.info)
                d["kind"] = e.kind
                if e.pts_deltas:
                    deltas = sorted(e.pts_deltas)
                    med = deltas[len(deltas) // 2]
                    d.setdefault("frame_ms", med / 90)
                if e.kind == "video":
                    d["frames"] = list(e.frames)
                    d["gop"] = (sorted(e.gops)[len(e.gops) // 2]) if e.gops else None
                    d["afd"] = e.afd
                    if e.rff or e.progressive:
                        d["telecine"] = e.rff > 0.3 * max(1, len(e.frames))
                    sizes_i = [s for s, t in e.frames if t == "I"]
                    if sizes_i:
                        d["i_frame_avg"] = sum(sizes_i) / len(sizes_i)
                es[pid] = d
            tables = []
            for (tid, ext), t in self.tables.items():
                iv = list(t["intervals"])
                tables.append({"tid": tid, "ext": ext, "name": table_name(tid), "pid": t["pid"],
                               "version": t["version"], "count": t["count"],
                               "versions": t["versions"], "age": now - t["last"],
                               "interval": sum(iv) / len(iv) if iv else None})
            tables.sort(key=lambda t: (t["tid"], t["ext"]))
            return {
                "time": now, "elapsed": now - self.started, "packets": self.packets,
                "sync_losses": self.sync_losses, "tei": self.tei, "cc_errors": self.cc_errors,
                "pids": pids, "es": es, "tables": tables, "pat": dict(self.pat),
                "tsid": self.tsid, "onid": self.onid, "pmts": dict(self.pmts),
                "pmt": self._service_pmt(), "cat": list(self.cat),
                "services": {k: dict(v) for k, v in self.services.items()},
                "network": self.network, "utc_offset": self.utc_offset,
                "local_offset": self.local_offset, "raw": dict(self.raw),
                "events": list(self.events),
            }


def _pcr_jitter(points):
    """Peak deviation (s) of the PCR from the arrival time, after a straight-line fit."""
    pts = list(points)
    if len(pts) < 8:
        return None
    t0, p0 = pts[0]
    xs = [t - t0 for t, _p in pts]
    ys = [p - p0 for _t, p in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    res = [y - (my + slope * (x - mx)) for x, y in zip(xs, ys)]
    return (max(res) - min(res)) / 2


def pid_owners(snap):
    """{pid: (sid, description)} for every PID named in a PMT, plus the PSI PIDs."""
    owners = {0: (None, "PAT"), 1: (None, "CAT"), 0x10: (None, "NIT"), 0x11: (None, "SDT/BAT"),
              0x12: (None, "EIT"), 0x14: (None, "TDT/TOT"), NULL_PID: (None, "Null")}
    for prog, pmt_pid in snap.get("pat", {}).items():
        if prog:
            owners[pmt_pid] = (prog, "PMT")
    for sid, pmt in snap.get("pmts", {}).items():
        for pid, d in pmt["streams"]:
            owners.setdefault(pid, (sid, d["codec"]))
        if pmt["pcr_pid"] not in owners:
            owners[pmt["pcr_pid"]] = (sid, "PCR")
        for _caid, ecm in pmt["ca"]:
            owners.setdefault(ecm, (sid, "ECM"))
        for _pid, d in pmt["streams"]:
            for _caid, ecm in d["ca"]:
                owners.setdefault(ecm, (sid, "ECM"))
    for _caid, emm in snap.get("cat", []):
        owners.setdefault(emm, (None, "EMM"))
    return owners
