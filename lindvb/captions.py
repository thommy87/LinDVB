# SPDX-License-Identifier: GPL-2.0-or-later
"""CEA-608/708 closed captions of the video (ATSC, US cable, some IPTV).

The decoders put the captions of the video stream (H.264/H.265 SEI, MPEG-2
user data) on the decoded frames as GstVideoCaptionMeta. cc708overlay does not
read that meta: it draws only what reaches its ``cc_sink`` pad, so
``CaptionOverlay`` takes the captions off the frames with ccextractor and
feeds them to that pad::

    ccextractor ── src ─────────────────────────────────── video_sink ┐
         └─ caption (once captions show up) ─ queue ─ ccconverter ─ cc_sink ┴ cc708overlay

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


def available():
    return all(Gst.ElementFactory.find(f) for f in ("cc708overlay", "ccextractor", "ccconverter"))


class CaptionOverlay:
    """A bin with ``sink`` and ``src`` pads for the video filter."""

    def __init__(self, silent=True, on_found=None, log=None):
        self.present = False            # CEA-708 captions seen in this pipeline
        self.kinds = set()
        self._on_found = on_found
        self._log = log or (lambda _msg: None)
        self.bin = Gst.Bin.new("captions")
        self.extractor = Gst.ElementFactory.make("ccextractor")
        self.overlay = Gst.ElementFactory.make("cc708overlay")
        self.overlay.set_property("silent", silent)
        # plain system memory behind the overlay: it then draws the captions
        # into the picture instead of attaching them as overlay meta, which
        # the video filter after it would not carry on to the sink
        blend = Gst.ElementFactory.make("capsfilter")
        blend.set_property("caps", Gst.Caps.from_string("video/x-raw"))
        for e in (self.extractor, self.overlay, blend):
            self.bin.add(e)
        self.extractor.link_pads("src", self.overlay, "video_sink")
        self.overlay.link(blend)
        self.bin.add_pad(Gst.GhostPad.new("sink", self.extractor.get_static_pad("sink")))
        self.bin.add_pad(Gst.GhostPad.new("src", blend.get_static_pad("src")))
        self.extractor.connect("pad-added", self._on_pad_added)

    def set_silent(self, silent):
        self.overlay.set_property("silent", silent)

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
        if ok:
            new = caption_kinds(bytes(m.data)) - self.kinds
            buf.unmap(m)
            if new:
                self.kinds |= new
                self._log("closed captions in the video: " + ", ".join(
                    f"CEA-{k}" for k in sorted(self.kinds)))
                if "708" in new:
                    self.present = True
                    if self._on_found:
                        self._on_found()
                elif not self.present:
                    self._log("only CEA-608 captions, which cc708overlay does not draw")
        return Gst.PadProbeReturn.REMOVE if self.present else Gst.PadProbeReturn.OK
