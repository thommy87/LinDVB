"""Playback with GStreamer playbin.

DVB channels are played from the capture segments through an appsrc, which
gives pause-and-resume timeshift and jumping back and forth for free. Files
and internet streams go to playbin directly.
"""

import re
import threading
import time

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gst, GstVideo  # noqa: E402

from .tuner import SegmentReader, TS_PACKET  # noqa: E402

PLAY_FLAGS_VIDEO = 1
PLAY_FLAGS_AUDIO = 2
PLAY_FLAGS_TEXT = 4

EQ_BANDS = ["29 Hz", "59 Hz", "119 Hz", "237 Hz", "474 Hz",
            "947 Hz", "1.9 kHz", "3.8 kHz", "7.5 kHz", "15 kHz"]


def ssm_rtp_to_udp(url):
    """rtp://source@group:port (source-specific multicast) -> (udp uri, source)."""
    m = re.match(r"(rtp|udp)://(?:([^@/]+)@)?([^:/]+):(\d+)", url)
    if not m:
        return url, None
    return f"udp://{m.group(3)}:{m.group(4)}", m.group(2)


class Feeder(threading.Thread):
    """Pushes TS data from a SegmentReader into an appsrc, blocking when full."""

    def __init__(self, reader, appsrc):
        super().__init__(daemon=True)
        self.reader, self.appsrc = reader, appsrc
        self.running = True

    def run(self):
        while self.running:
            data = self.reader.read()
            if data is None:
                self.appsrc.emit("end-of-stream")
                break
            if not data:
                time.sleep(0.03)
                continue
            if self.appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(data)) != Gst.FlowReturn.OK:
                break
        self.reader.close()

    def stop(self):
        self.running = False


class VideoBin:
    """Video sink for GTK 4: gtk4paintablesink if present, else appsink -> GdkTexture."""

    def __init__(self, picture):
        self.picture = picture
        sink = Gst.ElementFactory.make("gtk4paintablesink")
        if sink is not None:
            picture.set_paintable(sink.get_property("paintable"))
            glsink = Gst.ElementFactory.make("glsinkbin")
            if glsink is not None:
                glsink.set_property("sink", sink)
                self.sink = glsink
            else:
                self.sink = sink
            return
        bin_ = Gst.Bin.new("videoout")
        conv = Gst.ElementFactory.make("videoconvert")
        appsink = Gst.ElementFactory.make("appsink")
        appsink.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGBA"))
        appsink.set_property("emit-signals", True)
        appsink.set_property("max-buffers", 2)
        appsink.set_property("drop", True)
        appsink.connect("new-sample", self._on_sample)
        bin_.add(conv)
        bin_.add(appsink)
        conv.link(appsink)
        bin_.add_pad(Gst.GhostPad.new("sink", conv.get_static_pad("sink")))
        self.sink = bin_
        self._pending = None

    def _on_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps()
        info = GstVideo.VideoInfo.new_from_caps(caps)
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            data = GLib.Bytes.new(mapinfo.data)
            buf.unmap(mapinfo)
            first = self._pending is None
            self._pending = (data, info.width, info.height, info.stride[0])
            if first:
                GLib.idle_add(self._show)
        return Gst.FlowReturn.OK

    def _show(self):
        pending, self._pending = self._pending, None
        if pending:
            data, w, h, stride = pending
            tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, data, stride)
            self.picture.set_paintable(tex)
        return False


class Player(GObject.Object):
    __gsignals__ = {
        "state": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "tracks": (GObject.SignalFlags.RUN_LAST, None, ()),
        "eos": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, picture, settings):
        super().__init__()
        self.settings = settings
        self.video = VideoBin(picture)
        self.playbin = None
        self.feeder = None
        self.capture = None
        self.sid = 0
        self.uri = None
        self.paused = False
        self.rate = 1.0
        self._delay, self._paused_at = 0.0, None
        self.eq_gains = [0.0] * 10
        self.balance = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "hue": 0.0}
        self._volume = settings["volume"]
        self._mute = False

    # -- pipeline -----------------------------------------------------------

    def _build(self):
        self._teardown()
        pb = Gst.ElementFactory.make("playbin", "player")
        pb.set_property("video-sink", self.video.sink)
        vf = Gst.Bin.new("vfilter")
        chain = [Gst.ElementFactory.make("videoconvert")]
        if self.settings["deinterlace"] and Gst.ElementFactory.find("deinterlace"):
            chain.append(Gst.ElementFactory.make("deinterlace"))
        self.vbal = Gst.ElementFactory.make("videobalance")
        chain += [self.vbal, Gst.ElementFactory.make("videoconvert")]
        for e in chain:
            vf.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        vf.add_pad(Gst.GhostPad.new("sink", chain[0].get_static_pad("sink")))
        vf.add_pad(Gst.GhostPad.new("src", chain[-1].get_static_pad("src")))
        pb.set_property("video-filter", vf)
        self.eq = Gst.ElementFactory.make("equalizer-10bands")
        if self.eq is not None:
            pb.set_property("audio-filter", self.eq)
        pb.connect("source-setup", self._on_source_setup)
        pb.connect("element-setup", self._on_element_setup)
        bus = pb.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self.playbin = pb
        self._apply_settings()
        return pb

    def _teardown(self):
        if self.feeder:
            self.feeder.stop()
            self.feeder = None
        if self.playbin:
            self.playbin.set_state(Gst.State.NULL)
            self.playbin.get_bus().remove_signal_watch()
            self.playbin = None

    def _apply_settings(self):
        pb = self.playbin
        if not pb:
            return
        pb.set_property("volume", self._volume)
        pb.set_property("mute", self._mute)
        for k, v in self.balance.items():
            self.vbal.set_property(k, v)
        if self.eq is not None:
            for i, g in enumerate(self.eq_gains):
                self.eq.set_property(f"band{i}", g)

    def _on_source_setup(self, _pb, src):
        name = src.get_factory().get_name() if src.get_factory() else ""
        if name == "appsrc":
            src.set_property("caps", Gst.Caps.from_string(
                "video/mpegts,systemstream=(boolean)true,packetsize=(int)188"))
            Gst.util_set_object_arg(src, "stream-type", "stream")
            src.set_property("format", Gst.Format.BYTES)
            src.set_property("block", True)
            src.set_property("max-bytes", TS_PACKET * 512 * 16)
            self.feeder = Feeder(SegmentReader(self.capture.store, self._start_offset), src)
            self.feeder.start()
        elif name == "udpsrc" and self._ssm_source:
            if src.find_property("multicast-source"):
                src.set_property("multicast-source", self._ssm_source)
            src.set_property("caps", Gst.Caps.from_string(
                "application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T"))

    def _on_element_setup(self, _pb, element):
        f = element.get_factory()
        if f and f.get_name() == "tsdemux" and self.sid:
            element.set_property("program-number", self.sid)

    # -- sources ------------------------------------------------------------

    def play_capture(self, capture, sid, offset=None):
        """Play a DVB capture, starting at the live end or at a byte offset."""
        self.capture, self.sid, self.uri = capture, sid, None
        self._ssm_source = None
        _, end = capture.store.bounds()
        if offset is None:
            offset = max(0, end - int(capture.byte_rate() * 0.5))
        self._start_offset = offset
        self._delay = max(0.0, (end - offset) / capture.byte_rate())
        self._paused_at = None
        pb = self._build()
        pb.set_property("uri", "appsrc://")
        self.paused = False
        pb.set_state(Gst.State.PLAYING)

    def play_uri(self, uri):
        self.capture, self.sid = None, 0
        if "://" not in uri:
            uri = Gst.filename_to_uri(uri)
        self.uri = uri
        real, self._ssm_source = ssm_rtp_to_udp(uri) if uri.startswith(("rtp://", "udp://")) \
            else (uri, None)
        pb = self._build()
        pb.set_property("uri", real)
        self.paused = False
        pb.set_state(Gst.State.PLAYING)

    def stop(self):
        self._teardown()
        self.capture = None
        self.emit("state", "stopped")

    # -- transport ----------------------------------------------------------

    def toggle_pause(self):
        if not self.playbin:
            return
        self.paused = not self.paused
        if self.paused:
            self._paused_at = time.time()
        elif self._paused_at:
            self._delay += time.time() - self._paused_at
            self._paused_at = None
        self.playbin.set_state(Gst.State.PAUSED if self.paused else Gst.State.PLAYING)
        self.emit("state", "paused" if self.paused else "playing")

    def timeshift_delay(self):
        """Seconds behind live for DVB playback (grows while paused)."""
        if not self.capture:
            return 0.0
        d = self._delay
        if self._paused_at:
            d += time.time() - self._paused_at
        return d

    def played_offset(self):
        """Approximate byte offset of what is on screen in the capture."""
        if not self.capture:
            return 0
        _, end = self.capture.store.bounds()
        return max(0, end - int(self.timeshift_delay() * self.capture.byte_rate()))

    def jump(self, seconds):
        if self.capture:
            start, end = self.capture.store.bounds()
            off = self.played_offset() + int(seconds * self.capture.byte_rate())
            off = min(max(start, off), max(start, end - int(self.capture.byte_rate())))
            self.play_capture(self.capture, self.sid, off)
        elif self.playbin:
            ok, pos = self.playbin.query_position(Gst.Format.TIME)
            if ok:
                self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                         max(0, pos + int(seconds * Gst.SECOND)))

    def go_live(self):
        if self.capture:
            self.play_capture(self.capture, self.sid)

    def position(self):
        """(position, duration) in seconds for files, None for live."""
        if not self.playbin or self.capture:
            return None
        ok1, pos = self.playbin.query_position(Gst.Format.TIME)
        ok2, dur = self.playbin.query_duration(Gst.Format.TIME)
        if ok1 and ok2 and dur > 0:
            return pos / Gst.SECOND, dur / Gst.SECOND
        return None

    def seek_to(self, seconds):
        if self.playbin and not self.capture:
            self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                     int(seconds * Gst.SECOND))

    def set_rate(self, rate):
        """Play rate for files (0.5 .. 2.0)."""
        self.rate = rate
        if self.playbin and not self.capture:
            ok, pos = self.playbin.query_position(Gst.Format.TIME)
            if ok:
                self.playbin.seek(rate, Gst.Format.TIME,
                                  Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                  Gst.SeekType.SET, pos, Gst.SeekType.NONE, 0)

    # -- audio / video ------------------------------------------------------

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, v):
        self._volume = min(1.5, max(0.0, v))
        if self.playbin:
            self.playbin.set_property("volume", self._volume)

    @property
    def mute(self):
        return self._mute

    @mute.setter
    def mute(self, m):
        self._mute = m
        if self.playbin:
            self.playbin.set_property("mute", m)

    def set_balance(self, **kw):
        self.balance.update(kw)
        self._apply_settings()

    def set_eq(self, band, gain):
        self.eq_gains[band] = gain
        if self.eq is not None and self.playbin:
            self.eq.set_property(f"band{band}", gain)

    def _tags_list(self, kind):
        if not self.playbin:
            return []
        n = self.playbin.get_property(f"n-{kind}")
        out = []
        for i in range(n):
            tags = self.playbin.emit(f"get-{kind}-tags", i)
            label = f"Track {i + 1}"
            if tags:
                ok, lang = tags.get_string(Gst.TAG_LANGUAGE_CODE)
                ok2, codec = tags.get_string(
                    Gst.TAG_AUDIO_CODEC if kind == "audio" else Gst.TAG_SUBTITLE_CODEC)
                parts = [p for p, k in ((lang, ok), (codec, ok2)) if k and p]
                if parts:
                    label += " (" + ", ".join(parts) + ")"
            out.append(label)
        return out

    def audio_tracks(self):
        return self._tags_list("audio"), self.playbin.get_property("current-audio") if self.playbin else -1

    def subtitle_tracks(self):
        on = bool(self.playbin and self.playbin.get_property("flags") & PLAY_FLAGS_TEXT)
        cur = self.playbin.get_property("current-text") if self.playbin and on else -1
        return self._tags_list("text"), cur

    def select_audio(self, i):
        if self.playbin:
            self.playbin.set_property("current-audio", i)

    def select_subtitle(self, i):
        if not self.playbin:
            return
        flags = self.playbin.get_property("flags")
        if i < 0:
            self.playbin.set_property("flags", flags & ~PLAY_FLAGS_TEXT)
        else:
            self.playbin.set_property("flags", flags | PLAY_FLAGS_TEXT)
            self.playbin.set_property("current-text", i)

    def disable_video(self, off):
        if self.playbin:
            flags = self.playbin.get_property("flags")
            flags = flags & ~PLAY_FLAGS_VIDEO if off else flags | PLAY_FLAGS_VIDEO
            self.playbin.set_property("flags", flags)

    def screenshot(self, path):
        if not self.playbin:
            return False
        sample = self.playbin.emit("convert-sample", Gst.Caps.from_string("image/png"))
        if sample is None:
            return False
        buf = sample.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False
        with open(path, "wb") as f:
            f.write(m.data)
        buf.unmap(m)
        return True

    def video_size(self):
        if not self.playbin:
            return None
        pad = self.playbin.emit("get-video-pad", 0)
        caps = pad.get_current_caps() if pad else None
        if caps:
            s = caps.get_structure(0)
            return s.get_int("width")[1], s.get_int("height")[1]
        return None

    # -- bus ----------------------------------------------------------------

    def _on_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            self.emit("error", err.message)
        elif t == Gst.MessageType.EOS:
            self.emit("eos")
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.playbin:
            _old, new, _p = msg.parse_state_changed()
            if new == Gst.State.PLAYING:
                self.emit("state", "playing")
                self.emit("tracks")
        elif t in (Gst.MessageType.STREAM_COLLECTION, Gst.MessageType.STREAMS_SELECTED):
            self.emit("tracks")
        elif t == Gst.MessageType.BUFFERING:
            pct = msg.parse_buffering()
            if not self.capture and self.uri and not self.paused:
                self.playbin.set_state(Gst.State.PAUSED if pct < 100 else Gst.State.PLAYING)
