# SPDX-License-Identifier: GPL-2.0-or-later
"""Headers of the elementary streams in a transport stream, for the channel analysis.

Pure functions over bytes, no GStreamer: the stream analysis (lindvb.tsanalysis)
hands them the start of a PES packet and shows what they find, so the channel
analysis can name codec, profile, resolution, nominal bit rate and picture
types even for a channel that is not decoded (a second tuner, or before the
player has started).

Video: MPEG-2 sequence/picture headers, H.264 and HEVC parameter sets and
slice headers. Audio: MPEG-1/2 audio, AC-3, E-AC-3 and AAC (ADTS) frame headers.
"""

# --------------------------------------------------------------------------
#  bit reading
# --------------------------------------------------------------------------


class BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0                    # in bits

    def bits(self, n):
        v = 0
        for _ in range(n):
            byte = self.pos >> 3
            if byte >= len(self.data):
                raise ValueError("end of data")
            v = (v << 1) | ((self.data[byte] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def flag(self):
        return self.bits(1) == 1

    def skip(self, n):
        self.pos += n
        if self.pos > len(self.data) * 8:
            raise ValueError("end of data")

    def ue(self):
        """Unsigned Exp-Golomb code (H.264/HEVC)."""
        zeros = 0
        while self.bits(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad exp-golomb code")
        return (1 << zeros) - 1 + (self.bits(zeros) if zeros else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def unescape(nal):
    """Remove the emulation prevention bytes (00 00 03 -> 00 00) of a NAL unit."""
    if b"\x00\x00\x03" not in nal:
        return bytes(nal)
    out = bytearray()
    zeros = 0
    for b in nal:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def nal_units(data):
    """Yield the NAL units (without start code) of an Annex B byte stream."""
    i = data.find(b"\x00\x00\x01")
    while i >= 0:
        start = i + 3
        j = data.find(b"\x00\x00\x01", start)
        end = len(data) if j < 0 else j
        # a 4-byte start code leaves a 00 at the end of the previous unit
        nal = data[start:end]
        if j >= 0 and nal.endswith(b"\x00"):
            nal = nal.rstrip(b"\x00")
        if nal:
            yield nal
        i = j


# --------------------------------------------------------------------------
#  PES
# --------------------------------------------------------------------------


def pes_header(data):
    """(stream_id, decoding time stamp in 90 kHz or None, offset of the payload) of a
    PES packet start, or None when *data* does not start with one. The time stamp
    is the DTS when there is one (frames in decoding order), else the PTS."""
    if len(data) < 9 or data[0] != 0 or data[1] != 0 or data[2] != 1:
        return None
    sid = data[3]
    if sid in (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8):
        return sid, None, 6
    if (data[6] & 0xC0) != 0x80:
        return sid, None, 6             # MPEG-1 PES: rare in DVB
    flags = data[7]
    hlen = data[8]
    pts = None
    if flags & 0x80 and len(data) >= 14:
        at = 14 if (flags & 0xC0) == 0xC0 and len(data) >= 19 else 9
        p = data[at:at + 5]
        pts = (((p[0] >> 1) & 7) << 30) | (p[1] << 22) | ((p[2] >> 1) << 15) | \
              (p[3] << 7) | (p[4] >> 1)
    return sid, pts, 9 + hlen


# --------------------------------------------------------------------------
#  MPEG-2 video
# --------------------------------------------------------------------------

MPEG2_FRAME_RATES = {1: 24000 / 1001, 2: 24.0, 3: 25.0, 4: 30000 / 1001, 5: 30.0,
                     6: 50.0, 7: 60000 / 1001, 8: 60.0}
MPEG2_ASPECT = {1: "1:1", 2: "4:3", 3: "16:9", 4: "2.21:1"}
MPEG2_PROFILES = {5: "Simple", 4: "Main", 3: "SNR", 2: "Spatial", 1: "High", 0: "4:2:2"}
MPEG2_LEVELS = {10: "Low", 8: "Main", 6: "High 1440", 4: "High"}
MPEG2_ESCAPED = {0x85: ("4:2:2", "Main"), 0x82: ("4:2:2", "High"),
                 0x8E: ("Multi-view", "Low"), 0x8D: ("Multi-view", "Main"),
                 0x8B: ("Multi-view", "High 1440"), 0x8A: ("Multi-view", "High")}
PICTURE_TYPES = {1: "I", 2: "P", 3: "B"}


def parse_mpeg2(data):
    """Facts from the start of an MPEG-1/2 video access unit (dict, may be empty)."""
    out = {}
    i = data.find(b"\x00\x00\x01\xb3")
    if i >= 0 and i + 12 <= len(data):
        b = data[i + 4:i + 12]
        out["width"] = (b[0] << 4) | (b[1] >> 4)
        out["height"] = ((b[1] & 0x0F) << 8) | b[2]
        out["aspect"] = MPEG2_ASPECT.get(b[3] >> 4, "")
        fr = MPEG2_FRAME_RATES.get(b[3] & 0x0F)
        if fr:
            out["fps"] = fr
        rate = (b[4] << 10) | (b[5] << 2) | (b[6] >> 6)
        if rate and rate != 0x3FFFF:
            out["nominal_bps"] = rate * 400
        out["vbv_bytes"] = ((((b[6] & 0x1F) << 5) | (b[7] >> 3)) * 16 * 1024) // 8
    j = data.find(b"\x00\x00\x01\xb5", 0)
    while 0 <= j and j + 10 <= len(data):
        ext = data[j + 4] >> 4
        if ext == 1:                    # sequence extension
            pl = ((data[j + 4] & 0x0F) << 4) | (data[j + 5] >> 4)
            if pl & 0x80:               # escape: 4:2:2 and multi-view profiles
                out["profile"], out["level"] = MPEG2_ESCAPED.get(pl, ("", ""))
            else:
                out["profile"] = MPEG2_PROFILES.get((pl >> 4) & 7, "")
                out["level"] = MPEG2_LEVELS.get(pl & 0x0F, "")
            out["progressive_sequence"] = bool(data[j + 5] & 0x08)
            chroma = (data[j + 5] >> 1) & 3
            out["chroma"] = {1: "4:2:0", 2: "4:2:2", 3: "4:4:4"}.get(chroma, "")
            out["codec"] = "MPEG-2"
        elif ext == 8:                  # picture coding extension
            r = BitReader(data[j + 4:j + 10])
            try:
                r.skip(4 + 16 + 2)
                structure = r.bits(2)
                r.skip(6)
                rff = r.flag()
                r.skip(1)
                out["repeat_first_field"] = rff
                out["progressive_frame"] = r.flag()
                out["picture_structure"] = structure
            except ValueError:
                pass
        j = data.find(b"\x00\x00\x01\xb5", j + 4)
    k = data.find(b"\x00\x00\x01\x00")
    if 0 <= k and k + 6 <= len(data):
        out["picture"] = PICTURE_TYPES.get((data[k + 5] >> 3) & 7, "")
    if "codec" not in out and ("width" in out or "picture" in out):
        out["codec"] = "MPEG-2" if data.find(b"\x00\x00\x01\xb5") >= 0 else "MPEG-1"
    return out


# --------------------------------------------------------------------------
#  H.264 / AVC
# --------------------------------------------------------------------------

H264_PROFILES = {66: "Baseline", 77: "Main", 88: "Extended", 100: "High", 110: "High 10",
                 122: "High 4:2:2", 244: "High 4:4:4", 118: "Multiview High", 128: "Stereo High"}
H264_SAR = {1: (1, 1), 2: (12, 11), 3: (10, 11), 4: (16, 11), 5: (40, 33), 6: (24, 11),
            7: (20, 11), 8: (32, 11), 9: (80, 33), 10: (18, 11), 11: (15, 11), 12: (64, 33),
            13: (160, 99), 14: (4, 3), 15: (3, 2), 16: (2, 1)}
H264_SLICE = {0: "P", 1: "B", 2: "I", 3: "P", 4: "I"}
TRANSFER = {1: "BT.709", 6: "BT.601", 14: "BT.2020", 16: "PQ (HDR10)", 18: "HLG"}
PRIMARIES = {1: "BT.709", 5: "BT.601", 6: "BT.601", 9: "BT.2020"}


def _skip_scaling_list(r, size):
    last = nxt = 8
    for _ in range(size):
        if nxt:
            nxt = (last + r.se() + 256) % 256
        last = nxt or last


def parse_h264_sps(nal):
    """Profile, level and picture format from an H.264 SPS NAL unit (with header)."""
    r = BitReader(unescape(nal[1:]))
    out = {"codec": "H.264"}
    profile = r.bits(8)
    r.skip(8)
    level = r.bits(8)
    out["profile"] = H264_PROFILES.get(profile, str(profile))
    out["level"] = f"{level // 10}.{level % 10}"
    r.ue()
    chroma = 1
    out["bit_depth"] = 8
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = r.ue()
        if chroma == 3:
            r.skip(1)
        out["bit_depth"] = r.ue() + 8
        r.ue()
        r.skip(1)
        if r.flag():
            for i in range(8 if chroma != 3 else 12):
                if r.flag():
                    _skip_scaling_list(r, 16 if i < 6 else 64)
    out["chroma"] = {0: "4:0:0", 1: "4:2:0", 2: "4:2:2", 3: "4:4:4"}.get(chroma, "")
    r.ue()
    poc = r.ue()
    if poc == 0:
        r.ue()
    elif poc == 1:
        r.skip(1)
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()
    r.skip(1)
    w_mbs = r.ue() + 1
    h_units = r.ue() + 1
    frame_mbs_only = r.flag()
    if not frame_mbs_only:
        r.skip(1)
    r.skip(1)
    crop = (0, 0, 0, 0)
    if r.flag():
        crop = (r.ue(), r.ue(), r.ue(), r.ue())
    cx = 1 if chroma == 3 else 2
    cy = (1 if chroma in (2, 3) else 2) * (2 - frame_mbs_only)
    if chroma == 0:
        cx, cy = 1, 2 - frame_mbs_only
    out["width"] = w_mbs * 16 - cx * (crop[0] + crop[1])
    out["height"] = (2 - frame_mbs_only) * h_units * 16 - cy * (crop[2] + crop[3])
    out["interlaced"] = not frame_mbs_only
    try:
        if r.flag():                    # VUI
            if r.flag():
                idc = r.bits(8)
                sar = (r.bits(16), r.bits(16)) if idc == 255 else H264_SAR.get(idc)
                if sar and sar[1]:
                    out["sar"] = sar
            if r.flag():
                r.skip(1)
            if r.flag():
                r.skip(3)
                out["full_range"] = r.flag()
                if r.flag():
                    out["primaries"] = PRIMARIES.get(r.bits(8), "")
                    out["transfer"] = TRANSFER.get(r.bits(8), "")
                    r.skip(8)
            if r.flag():
                r.ue()
                r.ue()
            if r.flag():
                units, scale = r.bits(32), r.bits(32)
                if units and scale:
                    out["fps"] = scale / (2 * units)
    except ValueError:
        pass
    return out


def h264_slice_type(nal):
    """'I', 'P' or 'B' of a slice NAL unit (types 1 and 5), else None."""
    t = nal[0] & 0x1F
    if t not in (1, 5):
        return None
    try:
        r = BitReader(unescape(nal[1:16]))
        r.ue()
        return H264_SLICE.get(r.ue() % 5)
    except ValueError:
        return "I" if t == 5 else None


def parse_h264(data):
    out = {}
    pic = None
    for nal in nal_units(data):
        t = nal[0] & 0x1F
        if t == 7 and "width" not in out:
            try:
                out.update(parse_h264_sps(nal))
            except ValueError:
                pass
        elif t in (1, 5) and pic is None:
            pic = h264_slice_type(nal)
    if pic:
        out["picture"] = pic
    return out


# --------------------------------------------------------------------------
#  HEVC / H.265
# --------------------------------------------------------------------------

HEVC_PROFILES = {1: "Main", 2: "Main 10", 3: "Main Still Picture", 4: "Range Extensions"}


def parse_hevc_sps(nal):
    r = BitReader(unescape(nal[2:]))
    out = {"codec": "HEVC"}
    r.skip(4)
    max_sub = r.bits(3)
    r.skip(1)
    r.skip(2)
    tier = r.bits(1)
    profile = r.bits(5)
    r.skip(32)
    r.skip(48)
    level = r.bits(8)
    out["profile"] = HEVC_PROFILES.get(profile, str(profile))
    out["tier"] = "High" if tier else "Main"
    out["level"] = f"{level // 30}.{(level % 30) // 3}"
    present = [(r.flag(), r.flag()) for _ in range(max_sub)]
    if max_sub > 0:
        r.skip(2 * (8 - max_sub))
    for prof, lev in present:
        if prof:
            r.skip(88)
        if lev:
            r.skip(8)
    r.ue()
    chroma = r.ue()
    if chroma == 3:
        r.skip(1)
    w, h = r.ue(), r.ue()
    if r.flag():
        sx = 1 if chroma == 3 else 2
        sy = 2 if chroma == 1 else 1
        left, right, top, bottom = r.ue(), r.ue(), r.ue(), r.ue()
        w -= sx * (left + right)
        h -= sy * (top + bottom)
    out["width"], out["height"] = w, h
    out["bit_depth"] = r.ue() + 8
    out["chroma"] = {0: "4:0:0", 1: "4:2:0", 2: "4:2:2", 3: "4:4:4"}.get(chroma, "")
    return out


def parse_hevc(data, state=None):
    """*state* (a dict kept per stream) remembers the PPS fields slice headers need."""
    state = state if state is not None else {}
    out = {}
    pic = None
    for nal in nal_units(data):
        if len(nal) < 3:
            continue
        t = (nal[0] >> 1) & 0x3F
        if t == 33 and "width" not in out:
            try:
                out.update(parse_hevc_sps(nal))
            except ValueError:
                pass
        elif t == 34:
            try:
                r = BitReader(unescape(nal[2:12]))
                r.ue()
                r.ue()
                r.skip(2)
                state["extra_bits"] = r.bits(3)
            except ValueError:
                pass
        elif t <= 21 and pic is None:
            irap = 16 <= t <= 23
            try:
                r = BitReader(unescape(nal[2:20]))
                if not r.flag():
                    continue            # not the first slice of the picture
                if irap:
                    r.skip(1)
                r.ue()
                r.skip(state.get("extra_bits", 0))
                pic = {0: "B", 1: "P", 2: "I"}.get(r.ue())
            except ValueError:
                pic = "I" if irap else None
    if pic:
        out["picture"] = pic
    return out


# --------------------------------------------------------------------------
#  Active Format Description (ETSI TS 101 154: 'DTG1' user data)
# --------------------------------------------------------------------------

AFD_NAMES = {2: "16:9 top", 3: "14:9 top", 4: "> 16:9", 8: "full frame", 9: "4:3",
             10: "16:9", 11: "14:9", 13: "4:3 (14:9)", 14: "16:9 (14:9)", 15: "16:9 (4:3)"}


def find_afd(data):
    i = data.find(b"DTG1")
    if i < 0 or i + 6 > len(data):
        return None
    if not data[i + 4] & 0x40:
        return None
    return data[i + 5] & 0x0F


# --------------------------------------------------------------------------
#  Audio
# --------------------------------------------------------------------------

MPA_BITRATES = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
MPA_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
MPA_MODES = {0: "stereo", 1: "joint stereo", 2: "dual mono", 3: "mono"}
AC3_BITRATES = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448,
                512, 576, 640]
AC3_RATES = [48000, 44100, 32000]
AC3_CHANNELS = [2, 1, 2, 3, 3, 4, 4, 5]
AC3_LAYOUT = ["1+1", "1.0", "2.0", "3.0", "2.1", "3.1", "2.2", "3.2"]
AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025,
             8000, 7350]
AAC_PROFILES = {0: "AAC Main", 1: "AAC-LC", 2: "AAC SSR", 3: "AAC LTP"}


def _layout(acmod, lfe):
    name = AC3_LAYOUT[acmod]
    if name in ("3.2",):
        return "5.1" if lfe else "5.0"
    return name + (" +LFE" if lfe else "")


def parse_audio(data):
    """Facts from the first audio frame in *data*: codec, nominal bit rate,
    sample rate, channels and the AC-3 dialogue normalisation."""
    i = 0
    n = len(data)
    while i + 7 <= n:
        b0, b1 = data[i], data[i + 1]
        if b0 == 0x0B and b1 == 0x77:
            bsid = data[i + 5] >> 3
            if bsid <= 10:
                return _parse_ac3(data[i:i + 16])
            if 11 <= bsid <= 16:
                return _parse_eac3(data[i:i + 16])
        elif b0 == 0xFF and (b1 & 0xF6) == 0xF0:
            if i + 7 <= n:
                return _parse_adts(data[i:i + 7])
        elif b0 == 0xFF and (b1 & 0xE0) == 0xE0 and (b1 & 0x06):
            res = _parse_mpa(data[i:i + 4])
            if res:
                return res
        i += 1
        if i > 512:
            break
    return {}


def _parse_mpa(h):
    ver_bits = (h[1] >> 3) & 3
    layer = 4 - ((h[1] >> 1) & 3)
    if ver_bits == 1 or layer == 4:
        return None
    br_idx, sr_idx = h[2] >> 4, (h[2] >> 2) & 3
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    version = 1 if ver_bits == 3 else 2
    table = MPA_BITRATES[(1, layer)] if version == 1 else \
        MPA_BITRATES[(2, 1 if layer == 1 else 2)]
    mode = h[3] >> 6
    return {"codec": f"MPEG-{version} Layer {'I' * layer}" if layer < 3 else
            f"MPEG-{version} Layer III",
            "short": "MP2" if layer == 2 else ("MP3" if layer == 3 else "MP1"),
            "nominal_bps": table[br_idx] * 1000, "rate": MPA_RATES[ver_bits][sr_idx],
            "channels": 1 if mode == 3 else 2, "layout": "1.0" if mode == 3 else
            ("1+1" if mode == 2 else "2.0"), "mode": MPA_MODES[mode]}


def _parse_ac3(h):
    if len(h) < 8:
        return {}
    fscod, frm = h[4] >> 6, h[4] & 0x3F
    if fscod == 3 or frm >> 1 >= len(AC3_BITRATES):
        return {}
    r = BitReader(h[5:])
    out = {"codec": "AC-3", "short": "AC-3", "nominal_bps": AC3_BITRATES[frm >> 1] * 1000,
           "rate": AC3_RATES[fscod]}
    try:
        r.skip(5)
        out["bsmod"] = r.bits(3)
        acmod = r.bits(3)
        if (acmod & 1) and acmod != 1:
            r.skip(2)
        if acmod & 4:
            r.skip(2)
        if acmod == 2:
            r.skip(2)
        lfe = r.flag()
        out["dialnorm"] = -(r.bits(5) or 31)
        out["channels"] = AC3_CHANNELS[acmod] + lfe
        out["layout"] = _layout(acmod, lfe)
    except ValueError:
        pass
    return out


def _parse_eac3(h):
    r = BitReader(h[2:])
    try:
        r.skip(2 + 3)
        frmsiz = r.bits(11)
        fscod = r.bits(2)
        if fscod == 3:
            rate = [24000, 22050, 16000][min(2, r.bits(2))]
            blocks = 6
        else:
            rate = AC3_RATES[fscod]
            blocks = [1, 2, 3, 6][r.bits(2)]
        acmod = r.bits(3)
        lfe = r.flag()
        r.skip(5)
        dialnorm = -(r.bits(5) or 31)
    except (ValueError, IndexError):
        return {"codec": "E-AC-3", "short": "E-AC-3"}
    frame_bytes = (frmsiz + 1) * 2
    bps = frame_bytes * 8 * rate / (blocks * 256)
    return {"codec": "E-AC-3", "short": "E-AC-3", "nominal_bps": int(round(bps / 1000)) * 1000,
            "rate": rate, "channels": AC3_CHANNELS[acmod] + lfe,
            "layout": _layout(acmod, lfe), "dialnorm": dialnorm}


def _parse_adts(h):
    profile = h[2] >> 6
    sr = (h[2] >> 2) & 0x0F
    ch = ((h[2] & 1) << 2) | (h[3] >> 6)
    out = {"codec": AAC_PROFILES.get(profile, "AAC"), "short": "AAC",
           "rate": AAC_RATES[sr] if sr < len(AAC_RATES) else 0}
    if ch:
        out["channels"] = 8 if ch == 7 else ch
        out["layout"] = {1: "1.0", 2: "2.0", 3: "3.0", 4: "3.1", 5: "3.2", 6: "5.1",
                         7: "7.1"}.get(ch, str(ch))
    return out
