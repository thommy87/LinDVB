# SPDX-License-Identifier: GPL-2.0-or-later
"""CEA-608/708 closed captions of the video (ATSC, US cable, some IPTV).

The decoders put the captions of the video stream (H.264/H.265 SEI, MPEG-2
user data) on the decoded frames as GstVideoCaptionMeta. cc708overlay does not
read that meta: it draws only what reaches its ``cc_sink`` pad, so
``CaptionOverlay`` takes the captions off the frames with ccextractor and
feeds them to that pad::

    ccextractor ── src ── (middle: deinterlacer, videobalance, videoconvert) ── video_sink ┐
         └─ caption (once captions show up) ─ queue ─ ccconverter ──────────────── cc_sink ┴ cc708overlay

The overlay sits at the end of the video filter: it blends the captions
into the frames videoconvert just wrote (plain, cached system memory). In
front of the converter it blended into the decoder's frames, which with a
GPU decoder can be memory that is slow to read back, and the picture
stuttered whenever captions showed.

Off is ``service-number`` 0 (no service has that number) together with
``silent``: the decoder then never has a caption waiting to be shown. A
silent overlay that still decodes service 1 keeps its next caption until a
frame takes it, which no frame does while it is silent; meanwhile the queue
in front of it ran full and threw caption data away, so lines went missing
and old captions flashed up when they were switched on again.

The caption pad of ccextractor appears with the first frame that carries
captions, so ``CaptionOverlay.present`` (and with it Subtitles › Closed
Captions and the OSD's subtitle icon) is only true for a channel that really
sends them. cc708overlay draws CEA-708 (DTVCC) services only; a stream with
nothing but CEA-608 captions is logged and not offered.
"""

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

# cc_data triplets: marker bits, cc_valid, cc_type; 2 = DTVCC data, 3 = DTVCC start
_CC_VALID = 0x04
_CC_TYPE = 0x03


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


def dtvcc_services(packet):
    """The service numbers with data in one DTVCC packet (CEA-708 7.1):
    a header byte (sequence number, size) and service blocks, each a header
    (service number, block size; service 7 = extended number in the next
    byte) and that many bytes."""
    if not packet:
        return set()
    code = packet[0] & 0x3f
    size = 127 if code == 0 else code * 2 - 1
    data = packet[1:1 + size]
    services, i = set(), 0
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
        services.add(svc)
        i += length
    return services


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


def available():
    return all(Gst.ElementFactory.find(f) for f in ("cc708overlay", "ccextractor", "ccconverter"))


class CaptionOverlay:
    """A bin with ``sink`` and ``src`` pads for the video filter."""

    # caption data looked at for services after the first ones were found
    SCAN_BUFFERS = 900                  # 30 s at 30 frames a second

    def __init__(self, silent=True, on_found=None, log=None, middle=()):
        self.present = False            # CEA-708 captions seen in this pipeline
        self.kinds = set()
        self.service = 0                # the service shown, 0 = off
        self._scanner = DtvccScanner()
        self._scanned = 0
        self._on_found = on_found
        self._log = log or (lambda _msg: None)
        self.bin = Gst.Bin.new("captions")
        self.extractor = Gst.ElementFactory.make("ccextractor")
        self.overlay = Gst.ElementFactory.make("cc708overlay")
        # plain system memory behind the overlay: it then draws the captions
        # into the picture instead of attaching them as overlay meta, which
        # the sink would not draw
        blend = Gst.ElementFactory.make("capsfilter")
        blend.set_property("caps", Gst.Caps.from_string("video/x-raw"))
        chain = [self.extractor, *middle, self.overlay, blend]
        for e in chain:
            self.bin.add(e)
        prev = self.extractor
        for e in middle:
            if prev is self.extractor:
                prev.link_pads("src", e, None)
            else:
                prev.link(e)
            prev = e
        if prev is self.extractor:
            prev.link_pads("src", self.overlay, "video_sink")
        else:
            prev.link_pads(None, self.overlay, "video_sink")
        self.overlay.link(blend)
        self.show(1 if not silent else 0)
        self.bin.add_pad(Gst.GhostPad.new("sink", self.extractor.get_static_pad("sink")))
        self.bin.add_pad(Gst.GhostPad.new("src", blend.get_static_pad("src")))
        self.extractor.connect("pad-added", self._on_pad_added)

    @property
    def services(self):
        """The CEA-708 services with captions seen so far, sorted."""
        return sorted(self._scanner.services) or ([1] if self.present else [])

    def show(self, service):
        """Draw the captions of one CEA-708 service, or none (0 / None)."""
        service = int(service or 0)
        self.service = service
        if service:
            self.overlay.set_property("service-number", service)
            self.overlay.set_property("silent", False)
        else:
            self.overlay.set_property("silent", True)
            self.overlay.set_property("service-number", 0)

    def set_silent(self, silent):
        self.show(0 if silent else (self.services or [1])[0])

    def _on_pad_added(self, _el, pad):
        if pad.get_direction() != Gst.PadDirection.SRC or pad.get_name() != "caption":
            return
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
        for e in (q, conv, caps):
            self.bin.add(e)
        q.link(conv)
        conv.link(caps)
        caps.link_pads("src", self.overlay, "cc_sink")
        caps.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_captions)
        for e in (caps, conv, q):
            e.sync_state_with_parent()
        pad.link(q.get_static_pad("sink"))

    def _on_captions(self, pad, info):
        buf = info.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        data = bytes(m.data)
        buf.unmap(m)
        found = False
        new = caption_kinds(data) - self.kinds
        if new:
            self.kinds |= new
            self._log("closed captions in the video: " + ", ".join(
                f"CEA-{k}" for k in sorted(self.kinds)))
            if "708" in new:
                self.present = found = True
            elif not self.present:
                self._log("only CEA-608 captions, which cc708overlay does not draw")
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
