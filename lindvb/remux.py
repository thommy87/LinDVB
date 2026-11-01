"""Remux a recording into MPG, MP4, MKV, TS or a native audio file with GStreamer.

The source is either a URL (internet TV) or MPEG-TS bytes pushed by the caller
(DVB recordings read from the capture's segment store). Nothing is re-encoded:
parsebin splits the stream, each elementary stream goes to the container's muxer
if the muxer can hold it, and the rest is dropped.
"""

import os

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from .recformat import audio_extension  # noqa: E402

MUXERS = {"TS": "mpegtsmux", "MPG": "mpegpsmux", "MP4": "mp4mux", "MKV": "matroskamux"}
GAP_STEP = Gst.SECOND
# A second parser after parsebin converts the stream format when the muxer
# needs it (H.264/HEVC byte-stream -> avc/hvc1 for MKV and MP4, ADTS/LATM -> raw AAC).
REPARSE = {"video/x-h264": "h264parse", "video/x-h265": "h265parse", "audio/mpeg": "aacparse"}


def available(fmt):
    """True when the GStreamer elements for this format are installed."""
    names = ["parsebin"] + ([MUXERS[fmt]] if fmt in MUXERS else [])
    return all(Gst.ElementFactory.find(n) is not None for n in names)


class Remuxer:
    """One recording pipeline. ``path`` is the output file without extension
    for "Audio" (the extension follows the stream), with extension otherwise."""

    def __init__(self, fmt, path, url=None, program=0, all_audio=True,
                 subtitle_page=0, on_done=None):
        self.fmt = fmt
        self.path = str(path)
        self.all_audio = all_audio
        self.subtitle_page = subtitle_page
        self.program = program
        self.on_done = on_done
        self.have_video = False
        self.audio_count = 0
        self.dropped = []
        self.finished = False
        self._sub_queue = None
        self._sub_ready = False
        self._sub_last = -1

        self.pipeline = Gst.Pipeline.new("recording")
        self.pipeline.connect("deep-element-added", self._element_added)
        self.parse = Gst.ElementFactory.make("parsebin")
        self.pipeline.add(self.parse)
        self.parse.connect("pad-added", self._parsed_pad)
        if url:
            self.appsrc = None
            src = Gst.ElementFactory.make("urisourcebin")
            src.set_property("uri", url)
            self.pipeline.add(src)
            src.connect("pad-added", lambda _e, pad: pad.link(self.parse.get_static_pad("sink")))
        else:
            self.appsrc = Gst.ElementFactory.make("appsrc")
            self.appsrc.set_property("caps", Gst.Caps.from_string(
                "video/mpegts,systemstream=(boolean)true,packetsize=(int)188"))
            self.appsrc.set_property("block", True)
            self.appsrc.set_property("max-bytes", 8 << 20)
            self.pipeline.add(self.appsrc)
            self.appsrc.link(self.parse)
        self.mux = None
        if fmt in MUXERS:
            self.mux = Gst.ElementFactory.make(MUXERS[fmt])
            if fmt == "MP4":
                # Fragmented MP4 stays playable even if the recording is cut off.
                self.mux.set_property("fragment-duration", 2000)
            sink = self.sink = Gst.ElementFactory.make("filesink")
            sink.set_property("location", self.path)
            sink.set_property("sync", False)
            self.pipeline.add(self.mux)
            self.pipeline.add(sink)
            self.mux.link(sink)
        self.fallback = ""
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    # -- control --------------------------------------------------------------
    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("could not start the recording pipeline")

    def push(self, data):
        """Feed MPEG-TS bytes (DVB source). Blocks while the pipeline is busy."""
        if data and self.appsrc is not None and not self.finished:
            self.appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(data))

    def stop(self):
        """Finish the file (muxers write their index on EOS)."""
        if self.finished:
            return
        if self.appsrc is not None:
            self.appsrc.emit("end-of-stream")
        else:
            self.pipeline.send_event(Gst.Event.new_eos())
        GLib.timeout_add_seconds(15, self._finish)

    def _finish(self, *_):
        if not self.finished:
            self.finished = True
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_bus().remove_signal_watch()
            if self.on_done:
                self.on_done(self)
        return False

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.EOS:
            self._finish()
        elif msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            print(f"LinDVB: recording error: {err.message}")
            self._finish()

    # -- pipeline building ----------------------------------------------------
    def _element_added(self, _bin, _sub, element):
        factory = element.get_factory()
        if factory and factory.get_name() == "tsdemux" and self.program:
            element.set_property("program-number", self.program)

    def _branch(self, pad, *elements):
        """Link pad -> elements... and return the last element's src pad."""
        prev = None
        for e in elements:
            self.pipeline.add(e)
            e.sync_state_with_parent()
            if prev is None:
                pad.link(e.get_static_pad("sink"))
            else:
                prev.link(e)
            prev = e
        return prev.get_static_pad("src")

    def _fakesink(self, pad):
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", False)
        sink.set_property("async", False)
        self._branch(pad, sink)

    def _drop(self, pad, what):
        """Streams the recording does not keep still need a sink."""
        self.dropped.append(what)
        self._fakesink(pad)

    def _to_mux(self, pad, what, *elements):
        """pad -> elements -> queue -> muxer. Returns the queue, or None if the
        muxer cannot hold the stream (it is dropped then)."""
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        if not elements and name in REPARSE:
            if name == "audio/mpeg" and not self._is_aac(caps):
                pass
            elif not self._fits(caps):
                pass
            else:
                parser = Gst.ElementFactory.make(REPARSE[name])
                if parser is not None:
                    elements = (parser,)
        q = Gst.ElementFactory.make("queue")
        q.set_property("max-size-time", 5 * Gst.SECOND)
        q.set_property("max-size-buffers", 0)
        q.set_property("max-size-bytes", 0)
        out = self._branch(pad, *elements, q)
        mpad = self.mux.get_compatible_pad(out, out.query_caps(None))
        if mpad is not None and out.link(mpad) == Gst.PadLinkReturn.OK:
            return q
        if mpad is not None:
            self.mux.release_request_pad(mpad)
        self.dropped.append(what)
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", False)
        sink.set_property("async", False)
        self.pipeline.add(sink)
        sink.sync_state_with_parent()
        out.link(sink.get_static_pad("sink"))
        return None

    @staticmethod
    def _is_aac(caps):
        ok, ver = caps.get_structure(0).get_int("mpegversion")
        if ok:
            return ver in (2, 4)
        return not caps.get_structure(0).has_field("layer")

    def _fits(self, caps):
        """Can the muxer hold this kind of stream (codec, ignoring stream format)?"""
        st = caps.get_structure(0)
        ok, ver = st.get_int("mpegversion")
        caps = Gst.Caps.from_string(st.get_name() + (f",mpegversion=(int){ver}" if ok and ver else ""))
        for tmpl in self.mux.get_pad_template_list():
            if tmpl.direction == Gst.PadDirection.SINK and tmpl.get_caps().can_intersect(caps):
                return True
        return False

    def _switch_to_mkv(self, what):
        """MP4 cannot hold MPEG-2 video and MPG cannot hold HEVC: rather than
        losing the picture, the recording becomes MKV. This happens while the
        streams are announced, before any data reaches the muxer."""
        new = Gst.ElementFactory.make("matroskamux")
        if new is None:
            return
        old = self.mux
        self.pipeline.add(new)
        for mpad in list(old.sinkpads):
            peer = mpad.get_peer()
            if peer is not None:
                peer.unlink(mpad)
            old.release_request_pad(mpad)
            npad = new.get_compatible_pad(peer, peer.query_caps(None)) if peer else None
            if npad is not None:
                peer.link(npad)
        old.unlink(self.sink)
        for e in (old, self.sink):
            e.set_locked_state(True)
            e.set_state(Gst.State.NULL)
            self.pipeline.remove(e)
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self.path = self.path.rsplit(".", 1)[0] + ".mkv"
        self.sink = Gst.ElementFactory.make("filesink")
        self.sink.set_property("location", self.path)
        self.sink.set_property("sync", False)
        self.sink.set_property("async", False)
        self.pipeline.add(self.sink)
        new.link(self.sink)
        self.mux = new
        self.fallback = f"{self.fmt} cannot hold {what}, recorded as MKV"
        self.fmt = "MKV"
        new.sync_state_with_parent()
        self.sink.sync_state_with_parent()
        print(f"LinDVB: {self.fallback}")

    def _parsed_pad(self, _e, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        s = caps.get_structure(0) if caps and caps.get_size() else None
        name = s.get_name() if s else ""
        if name.startswith("video/") and name != "video/mpegts" and self.mux is not None:
            if self.have_video:
                return self._drop(pad, name)
            if not self._fits(caps) and self.fmt != "MKV":
                self._switch_to_mkv(name)
            q = self._to_mux(pad, name)
            self.have_video = q is not None
            if q is not None and self.subtitle_page:
                q.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._video_probe)
        elif name.startswith("audio/"):
            if self.audio_count and (not self.all_audio or self.mux is None):
                return self._drop(pad, name)
            if self.mux is None:
                self._audio_file(pad, s, name)
            elif self._to_mux(pad, name) is not None:
                self.audio_count += 1
        elif name == "application/x-teletext" and self.fmt == "MKV" and self.subtitle_page \
                and self._sub_queue is None and Gst.ElementFactory.find("teletextdec"):
            self._teletext_subtitles(pad)
        else:
            self._drop(pad, name or "unknown")

    def _audio_file(self, pad, _s, _name):
        """Native audio file. The extension follows the codec, which is only
        known once the stream's caps arrive, so the file sink is made then."""
        self.audio_count += 1

        def on_event(p, info):
            ev = info.get_event()
            if ev.type != Gst.EventType.CAPS:
                return Gst.PadProbeReturn.OK
            st = ev.parse_caps().get_structure(0)
            ok, ver = st.get_int("mpegversion")
            ok2, layer = st.get_int("layer")
            self.path = self.path + audio_extension(st.get_name(), ver if ok else 0,
                                                    layer if ok2 else 0)
            sink = Gst.ElementFactory.make("filesink")
            sink.set_property("location", self.path)
            sink.set_property("sync", False)
            sink.set_property("async", False)
            self._branch(p, Gst.ElementFactory.make("queue"), sink)
            return Gst.PadProbeReturn.REMOVE

        pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_event)

    def _teletext_subtitles(self, pad):
        """Teletext subtitle page -> text track (MKV)."""
        dec = Gst.ElementFactory.make("teletextdec")
        dec.set_property("page", self.subtitle_page)
        dec.set_property("subtitles-mode", True)
        want = Gst.ElementFactory.make("capsfilter")
        want.set_property("caps", Gst.Caps.from_string("text/x-raw,format=utf-8"))
        setter = Gst.ElementFactory.make("capssetter")
        setter.set_property("caps", Gst.Caps.from_string("text/x-raw,format=utf8"))
        q = self._to_mux(pad, "teletext subtitles", dec, want, setter)
        if q is None:
            return
        self._sub_queue = q
        q.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, self._sub_probe)

    # Subtitles are sparse. Without data the muxer would wait for the subtitle
    # track and stall the recording, so GAP events keep it moving.
    def _sub_probe(self, _pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            buf = info.get_buffer()
            if buf.pts != Gst.CLOCK_TIME_NONE:
                self._sub_last = max(self._sub_last, buf.pts)
        else:
            ev = info.get_event()
            if ev.type == Gst.EventType.SEGMENT:
                self._sub_ready = True
        return Gst.PadProbeReturn.OK

    def _video_probe(self, _pad, info):
        buf = info.get_buffer()
        pts = buf.pts if buf else Gst.CLOCK_TIME_NONE
        q = self._sub_queue
        if q is not None and self._sub_ready and pts != Gst.CLOCK_TIME_NONE \
                and pts > self._sub_last + GAP_STEP:
            start = max(self._sub_last, 0) if self._sub_last >= 0 else pts - GAP_STEP
            self._sub_last = pts
            q.get_static_pad("sink").send_event(Gst.Event.new_gap(max(start, 0), pts - max(start, 0)))
        return Gst.PadProbeReturn.OK
