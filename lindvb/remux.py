"""Remux a recording into MPG, MP4, MKV, TS or a native audio file with GStreamer.

The source is either a URL (internet TV) or MPEG-TS bytes pushed by the caller
(DVB recordings read from the capture's segment store). Nothing is re-encoded:
parsebin splits the stream, each elementary stream goes to the container's muxer
if the muxer can hold it, and the rest is dropped.

Internet TV: HLS and DASH streams often carry video and audio as separate
streams (an audio rendition per language, fragmented MP4 or TS segments), so
the URL source can expose several pads. Each one gets its own parsebin. The
muxers only accept new streams before the first data, so the parsed streams are
held until every source pad has announced its streams.
"""

import os
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from .recformat import audio_extension, choose_streams  # noqa: E402

MUXERS = {"TS": "mpegtsmux", "MPG": "mpegpsmux", "MP4": "mp4mux", "MKV": "matroskamux"}
GAP_STEP = Gst.SECOND
# Internet TV: how long the parsed streams wait for the other source pads.
SETTLE_MS = 500
SETTLE_LIMIT_S = 10
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
                 subtitle_page=0, on_done=None, audio_mode=None, language=""):
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
        # Internet TV (see the module docstring)
        self.audio_mode = audio_mode or ("all" if all_audio else "first")
        self.language = language
        self.selected = None           # stream ids picked from the stream collection
        self._lock = threading.RLock()
        self._sources = []             # [source pad, has announced a stream]
        self._held = []                # (parsed pad, probe id) waiting for the others
        self._released = False
        self._stopping = False

        self.pipeline = Gst.Pipeline.new("recording")
        self.pipeline.connect("deep-element-added", self._element_added)
        if url:
            self.appsrc = None
            self.parse = None
            src = Gst.ElementFactory.make("urisourcebin")
            src.set_property("uri", url)
            self.pipeline.add(src)
            src.connect("pad-added", self._source_pad)
        else:
            self.parse = Gst.ElementFactory.make("parsebin")
            self.pipeline.add(self.parse)
            self.parse.connect("pad-added", self._parsed_pad)
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
        if url:
            bus.enable_sync_message_emission()
            bus.connect("sync-message", self._sync_message)

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
            self._end_url()
        GLib.timeout_add_seconds(15, self._finish)

    def _end_url(self):
        """End an internet stream: the source keeps downloading, so the end of
        the stream is sent behind it (the source pads drop everything from now
        on) and the muxer finishes the file."""
        with self._lock:
            self._stopping = True
            self._release()
            sources = [pad for pad, _seen in self._sources]
        if not sources:
            self.pipeline.send_event(Gst.Event.new_eos())
        for pad in sources:
            peer = pad.get_peer()
            if peer is not None:
                peer.send_event(Gst.Event.new_eos())

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
            if not self._stopping:     # while stopping, the muxer is still finishing
                self._finish()

    # -- internet TV sources --------------------------------------------------
    def _sync_message(self, _bus, msg):
        """HLS / DASH: pick the video and audio streams before the demuxer
        exposes its default choice (runs in the demuxer's thread)."""
        if msg.type != Gst.MessageType.STREAM_COLLECTION or self.selected is not None:
            return
        factory = msg.src.get_factory() if isinstance(msg.src, Gst.Element) else None
        if factory is None or "Adaptive" not in (factory.get_metadata("klass") or ""):
            return                     # only HLS / DASH demuxers choose streams this way
        coll = msg.parse_stream_collection()
        streams = []
        for i in range(coll.get_size()):
            st = coll.get_stream(i)
            kind = st.get_stream_type()
            kind = ("video" if kind & Gst.StreamType.VIDEO else
                    "audio" if kind & Gst.StreamType.AUDIO else "other")
            tags = st.get_tags()
            lang = ""
            if tags is not None:
                ok, val = tags.get_string(Gst.TAG_LANGUAGE_CODE)
                lang = val if ok else ""
            default = bool(st.get_stream_flags() & Gst.StreamFlags.SELECT)
            streams.append((st.get_stream_id(), kind, lang, default))
        if not any(k == "video" for _i, k, _l, _d in streams) and \
                not any(k == "audio" for _i, k, _l, _d in streams):
            return
        # One audio stream: a live HLS demuxer stalls with two audio
        # renditions of the same group.
        ids = choose_streams(streams, self.audio_mode, self.language, single=True)
        if not ids:
            return
        if msg.src.send_event(Gst.Event.new_select_streams(ids)):
            with self._lock:
                self.selected = ids
                # The streams are chosen here, so every audio stream that comes is kept.
                self.all_audio = True

    def _source_pad(self, _src, pad):
        """A source pad of the URL: its own queue and parsebin. The queue lets
        the demuxer go on while the parsed streams are held (see _url_parsed)."""
        with self._lock:
            entry = [pad, False]
            self._sources.append(entry)
            q = Gst.ElementFactory.make("queue")
            q.set_property("max-size-buffers", 0)
            q.set_property("max-size-time", 0)
            q.set_property("max-size-bytes", 64 << 20)
            parse = Gst.ElementFactory.make("parsebin")
            parse.connect("pad-added", lambda _e, p: self._url_parsed(entry, p))
            for e in (q, parse):
                self.pipeline.add(e)
            q.link(parse)
            for e in (q, parse):
                e.sync_state_with_parent()
            pad.add_probe(Gst.PadProbeType.DATA_DOWNSTREAM, self._source_probe)
            pad.link(q.get_static_pad("sink"))
            if len(self._sources) == 1:
                GLib.timeout_add_seconds(SETTLE_LIMIT_S, self._release_later)

    def _source_probe(self, _pad, _info):
        return Gst.PadProbeReturn.DROP if self._stopping else Gst.PadProbeReturn.OK

    def _url_parsed(self, entry, pad):
        """A parsed stream of a URL source. Until every source has announced its
        streams it is held, unlinked: a live muxer starts as soon as one of
        its pads is linked and takes no new pads after that."""
        with self._lock:
            entry[1] = True
            if self._released:
                self._add_stream(pad)
                return
            pid = pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM,
                                lambda *_a: Gst.PadProbeReturn.OK)
            self._held.append((pad, pid))
            seen = sum(1 for _p, s in self._sources if s)
            if seen == len(self._sources) and seen >= len(self.selected or ()):
                # A source can expose several streams one after the other.
                GLib.timeout_add(SETTLE_MS, self._release_later)

    def _release_later(self):
        with self._lock:
            self._release()
        return False

    def _release(self):
        """Hand the held streams to the muxer, video first, then let them flow."""
        if self._released:
            return
        self._released = True
        held, self._held = self._held, []

        def is_video(item):
            caps = item[0].get_current_caps() or item[0].query_caps(None)
            name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
            return name.startswith("video/")
        held.sort(key=lambda item: not is_video(item))
        for pad, _pid in held:
            self._add_stream(pad)
        for pad, pid in held:
            pad.remove_probe(pid)

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
            elif self._mpeg2_aac(caps) and not self._fits(caps):
                # MPEG-2 AAC (common in ADTS streams) is the same bitstream as
                # MPEG-4 AAC; MP4 only takes the latter.
                parser = Gst.ElementFactory.make("aacparse")
                raw = Gst.ElementFactory.make("capsfilter")
                setter = Gst.ElementFactory.make("capssetter")
                if None not in (parser, raw, setter):
                    raw.set_property("caps", Gst.Caps.from_string(
                        "audio/mpeg,stream-format=(string)raw"))
                    setter.set_property("caps", Gst.Caps.from_string(
                        "audio/mpeg,mpegversion=(int)4"))
                    elements = (parser, raw, setter)
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
    def _mpeg2_aac(caps):
        st = caps.get_structure(0)
        ok, ver = st.get_int("mpegversion")
        return st.get_name() == "audio/mpeg" and ok and ver == 2

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
        with self._lock:
            self._add_stream(pad)

    def _add_stream(self, pad):
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
