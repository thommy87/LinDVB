# SPDX-License-Identifier: GPL-2.0-or-later
"""CEA-608/708 closed captions of the video (ATSC, US cable, some IPTV).

The decoders put the captions of the video stream (H.264/H.265 SEI, MPEG-2
user data) on the decoded frames as GstVideoCaptionMeta. ``CaptionOverlay``
takes them off the frames with ccextractor; LinDVB's own CEA-708 renderer
(lindvb.subrender, stage F) decodes them and the player draws the text over
the picture like teletext subtitles::

    ccextractor ── src ── (middle: deinterlacer, videobalance, videoconvert) ──
         └─ caption (once captions show up) ─ queue ─ ccconverter ── fakesink
                  (the renderers listen here: CaptionOverlay.listen)

GStreamer's cc708overlay drew them before. It draws nothing when a DTVCC
packet is spread over several frames, as broadcasters send them (PBS WJXT,
27.11.2026; reproduced with GStreamer 1.24.2), and GStreamer 1.28 of Ubuntu
26.04 and Arch no longer has it. With ``LINDVB_CC708OVERLAY=1`` it still
draws instead (:func:`old_path`), at the end of the video filter, fed on its
``cc_sink`` pad in place of the fakesink: there it blends the captions into
the frames videoconvert just wrote (plain, cached system memory). In front
of the converter it blended into the decoder's frames, which with a GPU
decoder can be memory that is slow to read back, and the picture stuttered
whenever captions showed.

The old path's off is ``service-number`` 0 (no service has that number) together with
``silent``: the decoder then never has a caption waiting to be shown. A
silent overlay that still decodes service 1 keeps its next caption until a
frame takes it, which no frame does while it is silent; meanwhile the queue
in front of it ran full and threw caption data away, so lines went missing
and old captions flashed up when they were switched on again.

The caption pad of ccextractor appears with the first frame that carries
captions, so ``CaptionOverlay.present`` (and with it Subtitles › Closed
Captions and the OSD's subtitle icon) is only true for a channel that really
sends them. The CEA-708 (DTVCC) services are offered; a stream with nothing
but CEA-608 captions offers the CEA-608 channels that carry captions
(:func:`caption_channels_608`), decoded by LinDVB's CEA-608 renderer. With
both, CEA-708 alone is offered: by convention its services carry the same
captions (service 1: CC1, service 2: CC3). The old path draws CEA-708 only,
so with it CEA-608 is not offered.
"""

import os
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

# cc_data triplets: marker bits, cc_valid, cc_type; 2 = DTVCC data, 3 = DTVCC start
_CC_VALID = 0x04
_CC_TYPE = 0x03


# CEA-608 commands that start captions (RCL, RU2-RU4, RDC): first byte 0x14/0x15
# (data channel 1) or 0x1C/0x1D (data channel 2), second byte below
_608_CAPTION_MODES = (0x20, 0x25, 0x26, 0x27, 0x29)


def caption_channels_608(data):
    """The CEA-608 channels (1-4: CC1-CC4) whose captions start in these
    cc_data triplets (a command that starts pop-on, roll-up or paint-on
    captions). Text mode and XDS programme data are not captions."""
    out = set()
    for i in range(0, len(data) - 2, 3):
        b0, b1, b2 = data[i], data[i + 1] & 0x7f, data[i + 2] & 0x7f
        if not b0 & _CC_VALID or b0 & _CC_TYPE >= 2:
            continue
        if b1 & 0xf7 in (0x14, 0x15) and b2 in _608_CAPTION_MODES:
            out.add(2 * (b0 & _CC_TYPE) + (2 if b1 & 0x08 else 1))
    return out


def caption_kinds(data):
    """The kinds of captions in cc_data triplets: a set of '608' and '708'.
    Triplets that are not valid or carry only padding count for nothing."""
    kinds = set()
    for i in range(0, len(data) - 2, 3):
        b0, b1, b2 = data[i], data[i + 1], data[i + 2]
        if not b0 & _CC_VALID:
            continue
        if b0 & _CC_TYPE >= 2:
            kinds.add("708")
        elif (b1 & 0x7f) or (b2 & 0x7f):     # 608 null padding is 0x80 0x80
            kinds.add("608")
    return kinds


def dtvcc_blocks(packet):
    """The service blocks of one DTVCC packet (CEA-708 7.1), [(service,
    bytes)]: a header byte (sequence number, size) and service blocks, each a
    header (service number, block size; service 7 = extended number in the
    next byte) and that many bytes."""
    if not packet:
        return []
    code = packet[0] & 0x3f
    size = 127 if code == 0 else code * 2 - 1
    data = packet[1:1 + size]
    blocks, i = [], 0
    while i < len(data):
        svc, length = data[i] >> 5, data[i] & 0x1f
        i += 1
        if svc == 7:
            if i >= len(data):
                break
            svc = data[i] & 0x3f
            i += 1
        if svc == 0 or length == 0:         # null block: the rest is padding
            break
        blocks.append((svc, bytes(data[i:i + length])))
        i += length
    return blocks


def dtvcc_services(packet):
    """The service numbers with data in one DTVCC packet."""
    return {svc for svc, _block in dtvcc_blocks(packet)}


# -- what the captions carry (channel analysis) -------------------------------------

# CEA-708 C1 commands (0x80-0x9F): the parameter bytes that follow each
_C1_PARAMS = bytes([0] * 8 + [1, 1, 1, 1, 1, 1, 0, 0] + [2, 3, 2, 0, 0, 0, 0, 4] + [6] * 8)


def text_608(b1, b2):
    """The characters a CEA-608 byte pair writes (parity stripped): a pair
    starting with 0x10-0x1F is a command, except the special and extended
    characters, which write one."""
    b1, b2 = b1 & 0x7f, b2 & 0x7f
    if 0x10 <= b1 <= 0x1f:
        if b1 in (0x11, 0x19) and 0x30 <= b2 <= 0x3f:
            return 1
        if b1 in (0x12, 0x13, 0x1a, 0x1b) and 0x20 <= b2 <= 0x3f:
            return 1
        return 0
    return (b1 >= 0x20) + (b2 >= 0x20)


def _is_708_char(b):
    return b >= 0xa0 or 0x20 <= b < 0x80


def _708_ext1(block, i):
    """(characters, length) of the EXT1 code at *i*; length None: unknown."""
    if i + 1 >= len(block):
        return 0, None
    e = block[i + 1]
    if _is_708_char(e):                         # G2, G3
        return 1, 2
    if e < 0x20:                                # C2: 0, 1, 2 or 3 parameter bytes
        return 0, 2 + e // 8
    if e < 0x90:                                # C3: 4 or 5 parameter bytes
        return 0, 2 + (4 if e < 0x88 else 5)
    return 0, None


def _708_code(block, i):
    """(characters, length) of the code at *i* of a service block."""
    b = block[i]
    if _is_708_char(b):                         # G0, G1
        return 1, 1
    if b == 0x10:
        return _708_ext1(block, i)
    if b < 0x20:                                # C0: 0x10-0x17 take one byte, 0x18-0x1F two
        return 0, 1 + (b >= 0x10) + (b >= 0x18)
    return 0, 1 + _C1_PARAMS[b - 0x80]


def text_708(block):
    """The characters a CEA-708 service block writes (CEA-708 7.1.4): G0
    and G1 characters (and G2/G3 behind EXT1) count; commands and their
    parameters do not. A command of unknown length ends the count."""
    n, i = 0, 0
    while i < len(block):
        chars, length = _708_code(block, i)
        if length is None:
            break
        n, i = n + chars, i + length
    return n


class CaptionCount:
    """What the closed captions of a stream carry, for the channel analysis:
    bytes and characters of text per CEA-608 field and CEA-708 service.
    "Sends nothing" (no data, or only commands and padding) can so be told
    from "LinDVB draws nothing"."""

    def __init__(self):
        self.buffers = 0
        self.fields = {}                # 608 field (1, 2): [bytes, characters]
        self.services = {}              # 708 service: [bytes, characters]
        self.xds = 0                    # bytes of XDS programme data (608 field 2)
        self._xds = set()               # the fields inside an XDS packet
        self._packet = bytearray()

    def feed(self, data, fmt="cc_data"):
        """One buffer of the caption pad, in its format: cc_data triplets,
        s334-1a triplets (field flag, two bytes) or raw 608 pairs (field 1)."""
        self.buffers += 1
        if fmt == "raw":
            for i in range(0, len(data) - 1, 2):
                self._pair(1, data[i], data[i + 1])
            return
        for i in range(0, len(data) - 2, 3):
            b0, b1, b2 = data[i], data[i + 1], data[i + 2]
            if fmt == "s334-1a":
                self._pair(1 if b0 & 0x80 else 2, b1, b2)
            else:
                self._triplet(b0, b1, b2)

    def _triplet(self, b0, b1, b2):
        valid, kind = b0 & _CC_VALID, b0 & _CC_TYPE
        if kind < 2:
            if valid:
                self._pair(kind + 1, b1, b2)
        elif valid and kind == 3:
            self._end()
            self._packet += bytes([b1, b2])
        elif valid:
            if self._packet:
                self._packet += bytes([b1, b2])
        else:
            self._end()

    def _pair(self, field, b1, b2):
        c = b1 & 0x7f
        if not c and not (b2 & 0x7f):           # null padding
            return
        # XDS (programme data, field 2): from a class code 0x01-0x0E to its
        # end code 0x0F; a caption command ends it as well
        if 0x01 <= c <= 0x0f or (field in self._xds and not 0x10 <= c <= 0x1f):
            self.xds += 2
            if c == 0x0f:
                self._xds.discard(field)
            elif c <= 0x0e:
                self._xds.add(field)
            return
        self._xds.discard(field)
        f = self.fields.setdefault(field, [0, 0])
        f[0] += 2
        f[1] += text_608(b1, b2)

    def _end(self):
        if self._packet:
            for svc, block in dtvcc_blocks(bytes(self._packet)):
                s = self.services.setdefault(svc, [0, 0])
                s[0] += len(block)
                s[1] += text_708(block)
            self._packet = bytearray()

    def summary(self):
        """{"buffers", "xds", "608": {field: (bytes, characters)}, "708": {service: …}}."""
        return {"buffers": self.buffers, "xds": self.xds,
                "608": {k: tuple(v) for k, v in sorted(self.fields.items())},
                "708": {k: tuple(v) for k, v in sorted(self.services.items())}}


class DtvccScanner:
    """Collects the caption services of a stream from its cc_data."""

    def __init__(self):
        self.services = set()
        self._packet = bytearray()

    def feed(self, data):
        """Feeds cc_data triplets; returns the services seen for the first time."""
        before = set(self.services)
        for i in range(0, len(data) - 2, 3):
            b0 = data[i]
            valid, kind = b0 & _CC_VALID, b0 & _CC_TYPE
            if kind < 2:
                continue
            if valid and kind == 3:                 # a new packet starts
                self._end()
                self._packet += data[i + 1:i + 3]
            elif valid:
                if self._packet:
                    self._packet += data[i + 1:i + 3]
            else:
                self._end()
        return self.services - before

    def _end(self):
        if self._packet:
            self.services |= dtvcc_services(bytes(self._packet))
            self._packet = bytearray()


def caption_services_from_descriptors(info):
    """{service number: ISO 639 code} from an ATSC caption_service_descriptor
    (A/65, tag 0x86) in the descriptors of a PMT stream. CEA-608 entries
    (line 21) come as -1 (field 1, CC1) and -2 (field 2, CC3)."""
    out = {}
    i = 0
    while i + 2 <= len(info):
        tag, length = info[i], info[i + 1]
        body = info[i + 2:i + 2 + length]
        i += 2 + length
        if tag != 0x86 or not body:
            continue
        n = body[0] & 0x1f
        for k in range(n):
            e = body[1 + 6 * k:7 + 6 * k]
            if len(e) < 4:
                break
            lang = e[:3].decode("latin-1", "replace").strip("\0 ").lower()
            if e[3] & 0x80:                         # digital_cc: a CEA-708 service
                out[e[3] & 0x3f] = lang
            else:
                out[-1 - (e[3] & 0x01)] = lang
    return out


def instream_service(instream_id):
    """The CEA-708 service an HLS INSTREAM-ID names ("SERVICE2" -> 2). By
    convention the primary 608 channel (CC1) and service 1 carry the same
    captions, so CC1 counts as service 1 and CC3 (the second language) as 2."""
    iid = (instream_id or "").strip().upper()
    if iid.startswith("SERVICE") and iid[7:].isdigit():
        return int(iid[7:])
    return {"CC1": 1, "CC3": 2}.get(iid)


def _stream_time(pad, pts):
    """The stream time (the playback position, ns) of *pts* on *pad*, or None."""
    if pts == Gst.CLOCK_TIME_NONE:
        return None
    ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
    if ev is None:
        return None
    t = ev.parse_segment().to_stream_time(Gst.Format.TIME, pts)
    return t if t != Gst.CLOCK_TIME_NONE and t >= 0 else None


def caption_format(caps):
    """The format of a caption pad's caps: "cc_data", "cdp", "s334-1a" or
    "raw" (608 byte pairs); "" when unknown."""
    if caps is None or not caps.get_size():
        return ""
    st = caps.get_structure(0)
    fmt = st.get_string("format") or ""
    if st.get_name() == "closedcaption/x-cea-708":
        return fmt if fmt in ("cc_data", "cdp") else ""
    if st.get_name() == "closedcaption/x-cea-608":
        return fmt if fmt in ("raw", "s334-1a") else ""
    return ""


def cdp_cc_data(cdp):
    """The cc_data triplets of a caption distribution packet (SMPTE 334-2):
    a 7 byte header (0x96 0x69, length, frame rate, flags, sequence), an
    optional time code section (0x71, 5 bytes), then 0x72 and the count."""
    if len(cdp) < 9 or cdp[0] != 0x96 or cdp[1] != 0x69:
        return b""
    i = 7
    if cdp[i] == 0x71:
        i += 5
    if i + 1 >= len(cdp) or cdp[i] != 0x72:
        return b""
    n = cdp[i + 1] & 0x1f
    return bytes(cdp[i + 2:i + 2 + 3 * n])


def available():
    """The elements that take the captions off the frames are there."""
    return all(Gst.ElementFactory.find(f) for f in ("ccextractor", "ccconverter"))


def old_path():
    """cc708overlay draws the captions instead of LinDVB's renderer: only
    when asked for (``LINDVB_CC708OVERLAY=1``) and there."""
    return os.environ.get("LINDVB_CC708OVERLAY") == "1" and \
        Gst.ElementFactory.find("cc708overlay") is not None


class CaptionOverlay:
    """A bin with ``sink`` and ``src`` pads for the video filter."""

    # caption data looked at for services after the first ones were found
    SCAN_BUFFERS = 900                  # 30 s at 30 frames a second

    def __init__(self, silent=True, on_found=None, log=None, middle=(), old=None):
        self.present = False            # captions offered: CEA-708, or CEA-608 alone
        self.kinds = set()
        self.service = 0                # the service shown (CEA-608: -channel), 0 = off
        self._channels = set()          # CEA-608 channels that carry captions
        self._scanner = DtvccScanner()
        self._scanned = 0
        self._on_found = on_found
        self._log = log or (lambda _msg: None)
        self.count = None               # CaptionCount while the channel analysis measures
        self._caption_pad = None        # ccextractor's caption pad, once captions came
        self._count_probe = None
        self._listeners = {}            # name -> fn: the caption data of each frame (stage F)
        self._lock = threading.Lock()
        self.old = old_path() if old is None else old    # cc708overlay draws
        self.bin = Gst.Bin.new("captions")
        self.extractor = Gst.ElementFactory.make("ccextractor")
        self.overlay = Gst.ElementFactory.make("cc708overlay") if self.old else None
        # plain system memory behind the overlay: it then draws the captions
        # into the picture instead of attaching them as overlay meta, which
        # the sink would not draw
        blend = Gst.ElementFactory.make("capsfilter")
        blend.set_property("caps", Gst.Caps.from_string("video/x-raw"))
        self._link_video([self.extractor, *middle, *([self.overlay] if self.overlay else []), blend])
        self.show(1 if not silent else 0)
        self.bin.add_pad(Gst.GhostPad.new("sink", self.extractor.get_static_pad("sink")))
        self.bin.add_pad(Gst.GhostPad.new("src", blend.get_static_pad("src")))
        self.extractor.connect("pad-added", self._on_pad_added)

    def _link_video(self, chain):
        """ccextractor's src, then the elements in order; the overlay takes
        the video on its video_sink."""
        for e in chain:
            self.bin.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link_pads("src" if a is self.extractor else None, b,
                        "video_sink" if b is self.overlay else None)

    @property
    def services(self):
        """The CEA-708 services with captions seen so far, sorted."""
        return sorted(self._scanner.services) or ([1] if "708" in self.kinds else [])

    @property
    def channels(self):
        """The CEA-608 channels offered (1-4), sorted: those that carry
        captions in a stream without CEA-708, none with the old path."""
        return [] if self.old or "708" in self.kinds else sorted(self._channels)

    def show(self, service):
        """The CEA-708 service shown (CEA-608: minus the channel), or none
        (0 / None); the old path draws CEA-708, else the player's renderers
        draw."""
        service = int(service or 0)
        self.service = service
        if self.overlay is None:
            return
        if service > 0:
            self.overlay.set_property("service-number", service)
            self.overlay.set_property("silent", False)
        else:
            self.overlay.set_property("silent", True)
            self.overlay.set_property("service-number", 0)

    def set_silent(self, silent):
        self.show(0 if silent else (self.services or [1])[0])

    # -- the channel analysis ----------------------------------------------------

    def measure(self, on):
        """Count what the captions carry (:class:`CaptionCount`) while *on*.
        The count sits on ccextractor's caption pad, in front of the queue
        that may drop data, and costs nothing while off: the probe is only
        there while measuring."""
        with self._lock:
            if on and self.count is None:
                self.count = CaptionCount()
            elif not on:
                self.count = None
            self._place_count_probe()

    def listen(self, fn, name="shadow"):
        """*fn(data, fmt, t)* gets the caption data of each frame (a
        streaming thread; t the stream time or None); None stops the
        listener of that *name*. The probe is there only while someone
        listens or the analysis counts."""
        with self._lock:
            listeners = dict(self._listeners)
            if fn is None:
                listeners.pop(name, None)
            else:
                listeners[name] = fn
            self._listeners = listeners
            self._place_count_probe()

    def caption_info(self):
        """None while not measuring; else whether captions came at all
        ("seen") and :meth:`CaptionCount.summary`."""
        count = self.count
        if count is None:
            return None
        return {"seen": self._caption_pad is not None, **count.summary()}

    def _place_count_probe(self):
        pad = self._caption_pad
        if pad is None:
            return
        wanted = self.count is not None or bool(self._listeners)
        if wanted and self._count_probe is None:
            self._count_probe = pad.add_probe(Gst.PadProbeType.BUFFER, self._on_count)
        elif not wanted and self._count_probe is not None:
            pad.remove_probe(self._count_probe)
            self._count_probe = None

    def _on_count(self, pad, info):
        count, listeners = self.count, self._listeners
        buf = info.get_buffer()
        if (count is None and not listeners) or buf is None:
            return Gst.PadProbeReturn.OK
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        data = bytes(m.data)
        buf.unmap(m)
        fmt = caption_format(pad.get_current_caps())
        if fmt == "cdp":
            data, fmt = cdp_cc_data(data), "cc_data"
        if count is not None:
            count.feed(data, fmt)
        if listeners:
            t = _stream_time(pad, buf.pts)
            for fn in listeners.values():
                fn(data, fmt, t)
        return Gst.PadProbeReturn.OK

    # -- drawing ---------------------------------------------------------------------

    def _on_pad_added(self, _el, pad):
        if pad.get_direction() != Gst.PadDirection.SRC or pad.get_name() != "caption":
            return
        with self._lock:
            self._caption_pad = pad
            self._place_count_probe()
        # The overlay waits on its caption pad while a caption is queued: a
        # queue of its own keeps that wait off the video thread, and leaking
        # keeps the picture going when the overlay is silent and drains nothing.
        q = Gst.ElementFactory.make("queue")
        q.set_property("leaky", 2)
        q.set_property("max-size-buffers", 0)
        q.set_property("max-size-bytes", 0)
        q.set_property("max-size-time", 2 * Gst.SECOND)
        conv = Gst.ElementFactory.make("ccconverter")
        caps = Gst.ElementFactory.make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string("closedcaption/x-cea-708,format=cc_data"))
        sink = None
        if self.overlay is None:
            # nothing draws here (the renderers listen on the caption pad);
            # the data only has to go somewhere, never waiting for the clock
            sink = Gst.ElementFactory.make("fakesink")
            sink.set_property("sync", False)
            sink.set_property("async", False)
        added = [e for e in (q, conv, caps, sink) if e is not None]
        for e in added:
            self.bin.add(e)
        q.link(conv)
        conv.link(caps)
        if sink is None:
            caps.link_pads("src", self.overlay, "cc_sink")
        else:
            caps.link(sink)
        caps.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_captions)
        for e in reversed(added):
            e.sync_state_with_parent()
        pad.link(q.get_static_pad("sink"))

    def _on_captions(self, pad, info):
        buf = info.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        data = bytes(m.data)
        buf.unmap(m)
        found = self._note_kinds(data) | self._note_channels(data)
        services = self._scanner.feed(data)
        if services:
            self._log("CEA-708 caption services: " + ", ".join(map(str, self.services)))
            found = found or self.present
        if found and self._on_found:
            self._on_found()
        if self.present:
            self._scanned += 1
        return Gst.PadProbeReturn.REMOVE if self._scanned > self.SCAN_BUFFERS else \
            Gst.PadProbeReturn.OK

    def _note_kinds(self, data):
        """CEA-608 or CEA-708 seen for the first time: logged; True when
        CEA-708 came (it is offered)."""
        new = caption_kinds(data) - self.kinds
        if not new:
            return False
        self.kinds |= new
        self._log("closed captions in the video: " + ", ".join(
            f"CEA-{k}" for k in sorted(self.kinds)))
        if "708" in new:
            self.present = True
            return True
        if self.old and not self.present:
            self._log("only CEA-608 captions, which cc708overlay does not draw")
        return False

    def _note_channels(self, data):
        """A CEA-608 channel's captions started for the first time: True when
        it is offered (no CEA-708, not the old path)."""
        new = caption_channels_608(data) - self._channels
        if not new:
            return False
        self._channels |= new
        if not self.channels:
            return False
        self.present = True
        self._log("CEA-608 caption channels offered: " + ", ".join(
            f"CC{c}" for c in self.channels))
        return True
