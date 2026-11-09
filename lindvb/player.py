# SPDX-License-Identifier: GPL-2.0-or-later
"""Playback with GStreamer playbin.

DVB channels are played from the capture segments through an appsrc, which
gives pause-and-resume timeshift and jumping back and forth for free. Files
and internet streams go to playbin directly.
"""

import re
import threading
import time
import urllib.request

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gst, GstVideo  # noqa: E402

from .nowplaying import RadioTextDecoder, ancillary_tail  # noqa: E402
from .media_info import (AudioTrack, audio_track_labels, hls_has_alternate_audio,  # noqa: E402
                         language_name)
from .audioviz import AudioAnalysis, parse_spectrum  # noqa: E402
from .loudness import LoudnessMeter, k_weighting  # noqa: E402
from . import hwaccel  # noqa: E402
from .teletext import (DEFAULT_SUBTITLE_PAGES, TeletextPage, subtitle_pages_from_pmt,  # noqa: E402
                       teletext_in_pmt)
from .tuner import SegmentReader, TS_PACKET  # noqa: E402

try:
    gi.require_version("GstMpegts", "1.0")
    from gi.repository import GstMpegts  # noqa: E402
    GstMpegts.initialize()
except (ImportError, ValueError):
    GstMpegts = None

PLAY_FLAGS_VIDEO = 1
PLAY_FLAGS_AUDIO = 2
PLAY_FLAGS_TEXT = 4
PLAY_FLAGS_VIS = 8

SPECTRUM_BINS = 256
SPECTRUM_INTERVAL = 33_333_333      # ns, 30 updates per second

EQ_BANDS = ["29 Hz", "59 Hz", "119 Hz", "237 Hz", "474 Hz",
            "947 Hz", "1.9 kHz", "3.8 kHz", "7.5 kHz", "15 kHz"]


def subtitle_font_desc(settings, base_points=18):
    """Pango font for the text subtitles GStreamer draws into the video, or
    None to keep GStreamer's own choice (both preferences untouched)."""
    family = str(settings.get("subtitle_font") or "").strip()
    try:
        scale = max(50, min(300, int(settings.get("subtitle_scale", 100))))
    except (TypeError, ValueError):
        scale = 100
    if not family and scale == 100:
        return None
    return f"{family or 'Sans'} {max(6, round(base_points * scale / 100))}"


def _has_playbin3():
    return Gst.ElementFactory.find("playbin3") is not None


def _is_hls_url(uri):
    return uri.startswith(("http://", "https://")) and "m3u8" in uri.lower()


def _fetch_text(url, limit=256 * 1024, timeout=4):
    req = urllib.request.Request(url, headers={"User-Agent": "LinDVB"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit).decode("utf-8", "replace")


def enable_ttml():
    """EBU-TT-D subtitles in DASH (DTVP IT-3, IT-16). GStreamer's TTML parser
    and renderer have rank "none", so playbin leaves TTML subtitle streams
    out; with a rank they are found and drawn over the video. Returns whether
    both elements are installed."""
    ok = True
    for name in ("ttmlparse", "ttmlrender"):
        f = Gst.ElementFactory.find(name)
        if f is None:
            ok = False
        elif f.get_rank() < Gst.Rank.PRIMARY:
            f.set_rank(Gst.Rank.PRIMARY)
    if ok:
        from . import contentguide
        contentguide.INTERNET_SUBTITLES = True
    return ok


def _sink_candidates(sink):
    """The video sink and the sinks inside it (glsinkbin's "sink", appsink in a bin)."""
    out = [sink]
    if sink.find_property("sink"):
        inner = sink.get_property("sink")
        if inner is not None:
            out.append(inner)
    if isinstance(sink, Gst.Bin):
        it = sink.iterate_sinks()
        while True:
            res, el = it.next()
            if res != Gst.IteratorResult.OK:
                break
            out.append(el)
    return out


def black_borders(data, w, h, stride, threshold=24):
    """(top, bottom, left, right) fractions of rows/columns darker than *threshold*
    (luma 0..255) at the edges of a grey picture: letterbox and pillarbox bars."""
    if w <= 0 or h <= 0 or len(data) < stride * h:
        return None

    def row_dark(y):
        row = data[y * stride:y * stride + w]
        return max(row) < threshold + 16 and sum(row) / w < threshold

    def col_dark(x):
        col = data[x:stride * h:stride]
        return max(col) < threshold + 16 and sum(col) / h < threshold

    if all(row_dark(y) for y in range(0, h, max(1, h // 8))):
        return None                     # a black picture says nothing about bars
    top = 0
    while top < h // 2 and row_dark(top):
        top += 1
    bottom = 0
    while bottom < h // 2 and row_dark(h - 1 - bottom):
        bottom += 1
    left = 0
    while left < w // 2 and col_dark(left):
        left += 1
    right = 0
    while right < w // 2 and col_dark(w - 1 - right):
        right += 1
    return top / h, bottom / h, left / w, right / w


def describe_error(err, debug=""):
    """A readable text for a GStreamer error; HTTP refusals get their reason."""
    text = f"{err.message} {debug or ''}"
    m = re.search(r"\((40[134]|410|451)\)", text)
    if m:
        code = m.group(1)
        if code in ("401", "403", "451"):
            return (f"The server refused the stream (HTTP {code}). It is probably "
                    "only available in certain countries.")
        return f"The stream was not found on the server (HTTP {code})."
    return err.message


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
        # Opaque formats: visualisers such as GOOM leave junk in the padding
        # byte, which RGBA would turn into transparency.
        if hasattr(Gdk.MemoryFormat, "R8G8B8X8"):
            self._format, caps = Gdk.MemoryFormat.R8G8B8X8, "RGBx"
        else:
            self._format, caps = Gdk.MemoryFormat.R8G8B8, "RGB"
        appsink.set_property("caps", Gst.Caps.from_string(f"video/x-raw,format={caps}"))
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
            tex = Gdk.MemoryTexture.new(w, h, self._format, data, stride)
            self.picture.set_paintable(tex)
        return False


class Player(GObject.Object):
    cc = None                    # cc708overlay of the current pipeline
    __gsignals__ = {
        "state": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # a GStreamer visualiser failed (message); playback goes on without it
        "vis-error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "tracks": (GObject.SignalFlags.RUN_LAST, None, ()),
        "eos": (GObject.SignalFlags.RUN_LAST, None, ()),
        # a GPU video decoder failed and was switched off (interface name, e.g.
        # "VA-API"); playback goes on with the next one or in software
        "hw-fallback": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # teletext subtitle text to show over the video ("" clears it)
        "subtitle-text": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # what is playing (dict: title, artist, album, year, track, image,
        # organization, radiotext); sent again whenever it changes
        "metadata": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        # how full the network buffer of an internet stream is (0..100)
        "buffering": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(self, picture, settings):
        super().__init__()
        self.settings = settings
        enable_ttml()
        self.apply_decoder_setting()
        self.video_decoder = None       # (factory, backend) decoding the video now
        self.video = VideoBin(picture)
        self.playbin = None
        self.video_off = False          # "Video Off (audio only)": kept across rebuilds
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
        # teletext subtitles: decoded inside playbin from tsdemux's teletext pad
        self.ttx_page = 0               # selected subtitle page, 0 = off
        self._ttx_dec = None
        self._ttx_present = False       # the stream carries teletext
        self._ttx_pages = []            # TeletextPage from the PMT
        self._ttx_in_pmt = False        # the PMT announces a teletext stream
        self._ttx_last = ""
        # Internet streams with the audio in separate HLS renditions run on
        # playbin3: the old hlsdemux in playbin lines those renditions up by
        # playlist position instead of by timestamp, which puts sound and
        # picture apart by up to a segment (seconds). hlsdemux2 syncs on PTS.
        self._pb3 = False
        self._collection = None         # playbin3: Gst.StreamCollection
        self._selected = set()          # playbin3: ids of the selected streams
        self._token = 0                 # bumps on every play/stop, cancels stale probes
        self._hls_alt_audio = {}        # url -> master playlist has audio renditions
        self._fallback_done = False
        self._error_token = -1          # playback that already reported an error
        self._hw_error_token = -1       # playback whose GPU decoder failed
        self._tracks_pending = False
        self.audio_delay_ms = 0         # + = audio later, - = audio earlier
        # radio visualisations: audio analysis tap, or a GStreamer visualiser
        self.analysis = AudioAnalysis()
        self._analyze = False
        self.spectrum = None
        self.vis_factory = None         # () -> Gst.Element for playbin's vis-plugin
        self._vis_element = None
        self._vis_error_token = None
        self._pending_seek = None
        self.metadata = {}
        # channel analysis (lindvb.analysis): the transport stream of internet
        # channels as it reaches tsdemux, the loudness of the sound and the
        # statistics of adaptive streams
        self._ts_taps = ()
        self.loudness = LoudnessMeter()
        self._measure_loudness = False
        self._level_elements = []
        self.fragments = []              # recent (uri, bytes, seconds) of HLS/DASH
        self.rebuffers = 0
        self._played = False             # this stream reached PLAYING once

    # -- pipeline -----------------------------------------------------------

    def _build(self, pb3=False):
        self._teardown()
        self.video_decoder = None
        self._pb3 = bool(pb3)
        self._collection, self._selected = None, set()
        self.fragments = []
        self.rebuffers = 0
        self._played = False
        pb = Gst.ElementFactory.make("playbin3" if pb3 else "playbin", "player")
        pb.set_property("video-sink", self.video.sink)
        vf = Gst.Bin.new("vfilter")
        chain = [Gst.ElementFactory.make("videoconvert")]
        # ATSC / US cable closed captions (CEA-608/708 in the video stream);
        # Subtitles > Closed Captions switches them on and off
        self.cc = None
        if Gst.ElementFactory.find("cc708overlay"):
            self.cc = Gst.ElementFactory.make("cc708overlay")
            self.cc.set_property("silent", not self.settings.get("closed_captions", False))
            chain.insert(0, self.cc)
        if self.settings["deinterlace"]:
            gpu = hwaccel.preferred(self._decoder_mode(), hwaccel.available())
            deint = hwaccel.make_deinterlacer(gpu)
            if deint is not None and deint.get_factory().get_name() == "vadeinterlace":
                # first in the chain: it takes the VA-API decoder's frames
                # straight from the graphics card and deinterlaces them there
                chain.insert(0, deint)
            elif deint is not None:
                chain.append(deint)
        self.vbal = Gst.ElementFactory.make("videobalance")
        chain += [self.vbal, Gst.ElementFactory.make("videoconvert")]
        for e in chain:
            vf.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        first = chain[0].get_static_pad("sink") or chain[0].get_static_pad("video_sink")
        vf.add_pad(Gst.GhostPad.new("sink", first))
        vf.add_pad(Gst.GhostPad.new("src", chain[-1].get_static_pad("src")))
        pb.set_property("video-filter", vf)
        pb.set_property("audio-filter", self._audio_filter())
        if self.vis_factory is not None:
            try:
                vis = self.vis_factory()
            except Exception as e:      # a plugin's visualiser must not stop playback
                vis = None
                self.emit("error", f"Visualisation not available: {e}")
            if vis is not None:
                self._vis_element = vis
                pb.set_property("vis-plugin", vis)
                pb.set_property("flags", pb.get_property("flags") | PLAY_FLAGS_VIS)
        pb.connect("source-setup", self._on_source_setup)
        pb.connect("element-setup", self._on_element_setup)
        if not pb3:
            pb.connect("audio-tags-changed", lambda *_: self._tracks_changed())
            pb.connect("text-tags-changed", lambda *_: self._tracks_changed())
        bus = pb.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self.playbin = pb
        self._apply_settings()
        return pb

    def _decoder_mode(self):
        mode = self.settings.get("video_decoder", "auto")
        return mode if mode in hwaccel.BACKENDS else "auto"

    def apply_decoder_setting(self):
        """Rank the video decoders after the "video_decoder" setting; pipelines
        built from now on use it (restart() for the one playing)."""
        return hwaccel.apply(self._decoder_mode())

    def _audio_filter(self):
        """audioconvert ! equalizer ! spectrum, with a probe for the meters."""
        self.eq = Gst.ElementFactory.make("equalizer-10bands")
        self.spectrum = Gst.ElementFactory.make("spectrum")
        if self.spectrum is not None:
            self.spectrum.set_property("bands", SPECTRUM_BINS)
            self.spectrum.set_property("threshold", -80)
            self.spectrum.set_property("interval", SPECTRUM_INTERVAL)
            self.spectrum.set_property("post-messages", self._analyze)
        tee = Gst.ElementFactory.make("tee")
        chain = [e for e in (Gst.ElementFactory.make("audioconvert"), tee, self.eq, self.spectrum)
                 if e is not None]
        if self.eq is None and self.spectrum is None:
            return None
        bin_ = Gst.Bin.new("afilter")
        for e in chain:
            bin_.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        if tee is not None:
            self._add_loudness_branch(bin_, tee)
        bin_.add_pad(Gst.GhostPad.new("sink", chain[0].get_static_pad("sink")))
        src = chain[-1].get_static_pad("src")
        bin_.add_pad(Gst.GhostPad.new("src", src))
        src.add_probe(Gst.PadProbeType.BUFFER, self._on_audio_buffer)
        return bin_

    def _add_loudness_branch(self, bin_, tee):
        """tee ! queue ! audioconvert ! level (peak) ! K-weighting ! level ! fakesink:
        the loudness of the sound before the equaliser (lindvb.loudness)."""
        self._level_elements = []
        names = ("queue", "audioconvert", "capsfilter", "level", "audioiirfilter", "level",
                 "fakesink")
        els = [Gst.ElementFactory.make(n) for n in names]
        if any(e is None for e in els):
            return
        q, conv, caps, peak, iir, level, sink = els
        q.set_property("leaky", 2)
        q.set_property("max-size-buffers", 20)
        q.set_property("max-size-time", 0)
        q.set_property("max-size-bytes", 0)
        caps.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=F64LE"))
        for lv, name in ((peak, "lv-peak"), (level, "lv-loudness")):
            lv.set_property("interval", 100 * Gst.MSECOND)
            lv.set_property("post-messages", self._measure_loudness)
            lv.set_name(name)
            self._level_elements.append(lv)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        for e in els:
            bin_.add(e)
        for a, b in zip(els, els[1:]):
            a.link(b)
        tee.request_pad_simple("src_%u").link(q.get_static_pad("sink"))

        def on_caps(pad, info):
            ev = info.get_event()
            if ev.type == Gst.EventType.CAPS:
                ok, rate = ev.parse_caps().get_structure(0).get_int("rate")
                if ok and rate:
                    b, a = k_weighting(rate)
                    iir.set_property("b", b)
                    iir.set_property("a", a)
            return Gst.PadProbeReturn.OK
        iir.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_caps)

    @property
    def measure_loudness(self):
        return self._measure_loudness

    @measure_loudness.setter
    def measure_loudness(self, on):
        """Post the level messages the loudness meter needs (channel analysis)."""
        on = bool(on)
        if on != self._measure_loudness:
            self._measure_loudness = on
            self.loudness.reset()
        for lv in self._level_elements:
            lv.set_property("post-messages", on)

    def add_ts_tap(self, fn):
        """Hand the transport stream of internet channels to fn (streaming thread)."""
        self._ts_taps = tuple(self._ts_taps) + (fn,)

    def remove_ts_tap(self, fn):
        self._ts_taps = tuple(t for t in self._ts_taps if t is not fn)

    def _ts_probe(self, _pad, info):
        taps = self._ts_taps
        if taps and not self.capture:
            buf = info.get_buffer()
            if buf is not None:
                data = buf.extract_dup(0, buf.get_size())
                for fn in taps:
                    try:
                        fn(data)
                    except Exception as e:      # noqa: BLE001 - never stop playback
                        print(f"Stream analysis: {e}")
        return Gst.PadProbeReturn.OK

    def _on_audio_buffer(self, pad, info):
        """Streaming thread: hand the decoded audio to the analysis."""
        if not self._analyze:
            return Gst.PadProbeReturn.OK
        buf = info.get_buffer()
        caps = pad.get_current_caps()
        if buf is None or caps is None:
            return Gst.PadProbeReturn.OK
        st = caps.get_structure(0)
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                self.analysis.feed_buffer(mapinfo.data, st.get_string("format"),
                                          st.get_int("channels")[1], st.get_int("rate")[1])
            finally:
                buf.unmap(mapinfo)
        return Gst.PadProbeReturn.OK

    @property
    def analyze(self):
        return self._analyze

    @analyze.setter
    def analyze(self, on):
        """Run the audio analysis (only while a visualisation is shown)."""
        on = bool(on)
        if on != self._analyze:
            self._analyze = on
            self.analysis.reset()
            if self.spectrum is not None:
                self.spectrum.set_property("post-messages", on)

    def _from_visualizer(self, src):
        vis = getattr(self, "_vis_element", None)
        while vis is not None and src is not None:
            if src == vis:
                return True
            src = src.get_parent()
        return False

    def _drop_visualizer(self, message):
        self.vis_factory = None
        self._vis_element = None
        self.emit("vis-error", message)
        if self.playbin:
            self.restart()
        return False

    def set_visualizer(self, factory):
        """Use a GStreamer element as visualisation (``None`` = drawn by LinDVB).

        playbin only takes a new visualiser when the pipeline is built, so a
        running audio-only stream is restarted where it is.
        """
        if factory is self.vis_factory:
            return
        self.vis_factory = factory
        if self.playbin and not self.has_video():
            self.restart()

    def _count(self, kind):
        """Number of video/audio streams, on playbin or playbin3."""
        if not self.playbin:
            return 0
        if self._pb3:
            stype = Gst.StreamType.VIDEO if kind == "video" else Gst.StreamType.AUDIO
            return len(self._streams(stype))
        return self.playbin.get_property(f"n-{kind}")

    def has_video(self):
        return self._count("video") > 0

    def is_audio_only(self):
        """True once a running stream turned out to have audio but no video."""
        if not self.playbin:
            return False
        _ret, state, _pending = self.playbin.get_state(0)
        return (state in (Gst.State.PAUSED, Gst.State.PLAYING)
                and self._count("video") == 0 and self._count("audio") > 0)

    def restart(self):
        """Rebuild the pipeline and continue at the same position."""
        if self.capture:
            self.play_capture(self.capture, self.sid, self.played_offset())
        elif self.uri:
            pos = self.position()
            self.play_uri(self.uri)
            self._pending_seek = pos[0] if pos else None

    def _teardown(self):
        self._ttx_dec = None
        self._ttx_present = False
        self._ttx_pages = []
        self._ttx_in_pmt = False
        self._ttx_last = ""
        self.emit("subtitle-text", "")
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
        # playbin's av-offset > 0 plays the audio earlier, so it is the negated delay
        pb.set_property("av-offset", -int(self.audio_delay_ms) * Gst.MSECOND)
        self.apply_subtitle_font()
        if self.video_off:
            # set before the pipeline starts, so playbin and playbin3 both
            # leave the video out (internet streams rebuild the pipeline)
            pb.set_property("flags", pb.get_property("flags") & ~PLAY_FLAGS_VIDEO)
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
        dec = hwaccel.factory_backend(element)
        if dec is not None:
            self.video_decoder = dec
        if f and f.get_name() == "tsdemux":
            element.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._ts_probe)
            if self.sid:
                element.set_property("program-number", self.sid)
            element.connect("pad-added", self._on_demux_audio_pad)
            if not self._pb3:       # teletext is linked by hand into decodebin only
                element.connect("pad-added", self._on_demux_pad)

    # -- teletext subtitles -------------------------------------------------

    def _on_demux_pad(self, demux, pad):
        """Decode the teletext stream next to decodebin, synced to the video."""
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or caps.get_structure(0).get_name() != "application/x-teletext":
            return
        if self._ttx_dec is not None or pad.is_linked():
            return
        if Gst.ElementFactory.find("teletextdec") is None:
            return
        parent = demux.get_parent()
        q = Gst.ElementFactory.make("queue")
        q.set_property("max-size-buffers", 0)
        q.set_property("max-size-time", 0)
        q.set_property("max-size-bytes", 0)
        dec = Gst.ElementFactory.make("teletextdec")
        dec.set_property("page", self.ttx_page or DEFAULT_SUBTITLE_PAGES[0])
        dec.set_property("subtitles-mode", True)
        dec.set_property("subtitles-template", "%s\n")
        cf = Gst.ElementFactory.make("capsfilter")
        cf.set_property("caps", Gst.Caps.from_string("text/x-raw,format=utf-8"))
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("sync", True)
        sink.set_property("async", False)
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 4)
        sink.connect("new-sample", self._on_ttx_sample)
        chain = (q, dec, cf, sink)
        for e in chain:
            parent.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        for e in reversed(chain):
            e.sync_state_with_parent()
        if pad.link(q.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            return
        self._ttx_dec = dec
        self._ttx_present = True
        GLib.idle_add(lambda: (self.emit("tracks"), False)[1])

    # -- now playing --------------------------------------------------------

    def _reset_metadata(self):
        if self.metadata:
            self.metadata = {}
            self.emit("metadata", {})

    def _update_metadata(self, new, token):
        """Main thread: merge new song data; a new title drops the old song's."""
        if token != self._token:
            return False
        md = dict(self.metadata)
        if "title" in new and new["title"] != md.get("title"):
            for k in ("artist", "album", "year", "track", "image"):
                md.pop(k, None)
        md.update(new)
        if md != self.metadata:
            self.metadata = md
            self.emit("metadata", dict(md))
        return False

    def _on_demux_audio_pad(self, _demux, pad):
        """Read the RDS RadioText that DVB radio sends with MPEG audio."""
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or caps.is_empty():
            return
        st = caps.get_structure(0)
        if st.get_name() != "audio/mpeg" or st.get_int("mpegversion")[1] != 1:
            return
        dec, token = RadioTextDecoder(), self._token

        def probe(_pad, info):
            buf = info.get_buffer()
            if buf is not None:
                tail = ancillary_tail(buf.get_size(), buf.extract_dup)
                text = dec.feed_tail(tail) if tail else None
                if text:
                    GLib.idle_add(self._update_metadata, {"radiotext": text}, token)
            return Gst.PadProbeReturn.OK
        pad.add_probe(Gst.PadProbeType.BUFFER, probe)

    def _on_tags(self, msg):
        tags = msg.parse_tag()
        new = {}
        for key, tag in (("title", Gst.TAG_TITLE), ("artist", Gst.TAG_ARTIST),
                         ("album", Gst.TAG_ALBUM), ("organization", Gst.TAG_ORGANIZATION)):
            ok, v = tags.get_string(tag)
            if ok and v and v.strip():
                new[key] = v.strip()
        ok, n = tags.get_uint(Gst.TAG_TRACK_NUMBER)
        if ok and n:
            new["track"] = n
        ok, dt = tags.get_date_time(Gst.TAG_DATE_TIME)
        if ok and dt and dt.has_year():
            new["year"] = dt.get_year()
        else:
            ok, d = tags.get_date(Gst.TAG_DATE)
            if ok and d and d.valid():
                new["year"] = d.get_year()
        for tag in (Gst.TAG_IMAGE, Gst.TAG_PREVIEW_IMAGE):
            ok, sample = tags.get_sample(tag)
            if ok and sample and sample.get_buffer():
                buf = sample.get_buffer()
                new["image"] = buf.extract_dup(0, buf.get_size())
                break
        if new:
            self._update_metadata(new, self._token)

    def _on_ttx_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            text = bytes(info.data).decode("utf-8", "replace").replace("\x00", "")
            buf.unmap(info)
            text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
            if self.ttx_page and text != self._ttx_last:
                self._ttx_last = text
                GLib.idle_add(self._show_subtitle, text)
        return Gst.FlowReturn.OK

    def _show_subtitle(self, text):
        if self.ttx_page:
            self.emit("subtitle-text", text)
        return False

    def teletext_pages(self):
        """Teletext subtitle pages offered for the current stream."""
        if self._ttx_pages:
            return list(self._ttx_pages)
        if self._ttx_present:
            return [TeletextPage(p) for p in DEFAULT_SUBTITLE_PAGES]
        return []

    def has_teletext(self):
        """The current stream carries teletext (seen in the PMT or demuxed)."""
        return self._ttx_present or self._ttx_in_pmt or bool(self._ttx_pages)

    def _on_section(self, msg):
        if GstMpegts is None:
            return
        section = GstMpegts.message_parse_mpegts_section(msg)
        if section is None or section.section_type != GstMpegts.SectionType.PMT:
            return
        data = section.get_data()
        raw = data.get_data() if hasattr(data, "get_data") else bytes(data)
        try:
            program, pages = subtitle_pages_from_pmt(bytes(raw))
            _program, present = teletext_in_pmt(bytes(raw))
        except ValueError:
            return
        if self.sid and program != self.sid:
            return
        changed = present != self._ttx_in_pmt
        self._ttx_in_pmt = present
        if pages and pages != self._ttx_pages:
            self._ttx_pages = pages
            changed = True
        if changed:
            self.emit("tracks")

    # -- sources ------------------------------------------------------------

    def play_capture(self, capture, sid, offset=None):
        """Play a DVB capture, starting at the live end or at a byte offset."""
        if capture is not self.capture or sid != self.sid:
            self._reset_metadata()
        self.capture, self.sid, self.uri = capture, sid, None
        self._ssm_source = None
        self._token += 1
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
        if uri != self.uri:
            self._reset_metadata()
        self.uri = uri
        self._pending_seek = None
        self._token += 1
        self._fallback_done = False
        engine = self.settings.get("stream_engine", "auto")
        if engine == "playbin3" and _has_playbin3():
            self._start_uri(True)
        elif engine == "auto" and _is_hls_url(uri) and _has_playbin3():
            known = self._hls_alt_audio.get(uri)
            if known is not None:
                self._start_uri(known)
            else:
                # look at the master playlist first (in the background)
                self._teardown()
                threading.Thread(target=self._probe_hls, args=(uri, self._token),
                                 daemon=True).start()
        else:
            self._start_uri(False)

    def _probe_hls(self, uri, token):
        try:
            alt = hls_has_alternate_audio(_fetch_text(uri))
        except (OSError, ValueError):
            alt = False
        GLib.idle_add(self._probe_done, uri, token, alt)

    def _probe_done(self, uri, token, alt):
        self._hls_alt_audio[uri] = alt
        if token == self._token and self.uri == uri and not self.capture:
            self._start_uri(alt)
        return False

    def _start_uri(self, pb3):
        uri = self.uri
        real, self._ssm_source = ssm_rtp_to_udp(uri) if uri.startswith(("rtp://", "udp://")) \
            else (uri, None)
        pb = self._build(pb3)
        pb.set_property("uri", real)
        self.paused = False
        pb.set_state(Gst.State.PLAYING)

    def _fallback_to_playbin(self, token):
        if token == self._token and self.uri and not self.capture:
            self._hls_alt_audio[self.uri] = False
            self._start_uri(False)
        return False

    def _hw_fallback(self, token, gpu):
        if token == self._token and (self.capture or self.uri):
            self.restart()
            self.emit("hw-fallback", hwaccel.NAMES.get(gpu, gpu))
        return False

    def set_audio_delay(self, ms):
        """Shift the sound against the picture (ms, + = sound later)."""
        self.audio_delay_ms = int(ms)
        if self.playbin:
            self.playbin.set_property("av-offset", -self.audio_delay_ms * Gst.MSECOND)

    def stop(self):
        self._token += 1
        self._teardown()
        self.capture = None
        self._reset_metadata()
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

    def set_closed_captions(self, on):
        """Show the CEA-608/708 captions of the video (ATSC, US cable, some IPTV)."""
        self.settings["closed_captions"] = bool(on)
        if self.cc is not None:
            self.cc.set_property("silent", not on)

    def has_closed_captions(self):
        return self.cc is not None

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

    def _tracks_changed(self):
        """Tags arrive from streaming threads; tell the UI once, on the main loop."""
        if not self._tracks_pending:
            self._tracks_pending = True

            def fire():
                self._tracks_pending = False
                self.emit("tracks")
                return False
            GLib.idle_add(fire)

    def _streams(self, stype):
        """playbin3: the streams of one type in the current collection."""
        c = self._collection
        if c is None:
            return []
        out = []
        for i in range(c.get_size()):
            st = c.get_stream(i)
            if st.get_stream_type() & stype:
                out.append(st)
        return out

    def _select_streams(self, stype, stream):
        """playbin3: keep the other selections, put `stream` (or none) for `stype`."""
        keep = [st.get_stream_id() for st in self._streams(~stype & 0xff)
                if st.get_stream_id() in self._selected]
        if stream is not None:
            keep.append(stream.get_stream_id())
        if keep:
            self.playbin.send_event(Gst.Event.new_select_streams(keep))

    @staticmethod
    def _track_from(tags, caps, codec_tag):
        t = AudioTrack()
        if tags:
            for attr, tag in (("language", Gst.TAG_LANGUAGE_CODE), ("title", Gst.TAG_TITLE),
                              ("codec", codec_tag)):
                ok, v = tags.get_string(tag)
                if ok and v:
                    setattr(t, attr, v)
        if caps and caps.get_size():
            st = caps.get_structure(0)
            ok, ch = st.get_int("channels")
            if ok:
                t.channels = ch
            if not t.codec and not st.get_name().startswith(("audio/x-raw", "text/x-raw")):
                t.codec = st.get_name().split("/")[-1]
        return t

    def _decoded_channels(self):
        """Channels of the audio that is actually playing (after decoding)."""
        pad = self.eq.get_static_pad("sink") if self.eq is not None else None
        caps = pad.get_current_caps() if pad else None
        if caps and caps.get_size():
            ok, ch = caps.get_structure(0).get_int("channels")
            if ok:
                return ch
        return 0

    def audio_track_info(self):
        """([AudioTrack], index of the current one or -1)."""
        if not self.playbin:
            return [], -1
        tracks, cur = [], -1
        if self._pb3:
            for i, st in enumerate(self._streams(Gst.StreamType.AUDIO)):
                tracks.append(self._track_from(st.get_tags(), st.get_caps(), Gst.TAG_AUDIO_CODEC))
                if st.get_stream_id() in self._selected:
                    cur = i
        else:
            for i in range(self.playbin.get_property("n-audio")):
                pad = self.playbin.emit("get-audio-pad", i)
                caps = pad.get_current_caps() if pad else None
                tracks.append(self._track_from(self.playbin.emit("get-audio-tags", i), caps,
                                               Gst.TAG_AUDIO_CODEC))
            cur = self.playbin.get_property("current-audio") if tracks else -1
        if 0 <= cur < len(tracks) and not tracks[cur].channels:
            tracks[cur].channels = self._decoded_channels()
        return tracks, cur

    def _text_labels(self):
        if not self.playbin:
            return [], -1
        if self._pb3:
            streams = self._streams(Gst.StreamType.TEXT)
            items = [st.get_tags() for st in streams]
            cur = next((i for i, st in enumerate(streams) if st.get_stream_id() in self._selected), -1)
        else:
            items = [self.playbin.emit("get-text-tags", i)
                     for i in range(self.playbin.get_property("n-text"))]
            on = bool(self.playbin.get_property("flags") & PLAY_FLAGS_TEXT)
            cur = self.playbin.get_property("current-text") if on else -1
        out = []
        for i, tags in enumerate(items):
            label = f"Subtitles {i + 1}"
            if tags:
                ok, lang = tags.get_string(Gst.TAG_LANGUAGE_CODE)
                ok2, codec = tags.get_string(Gst.TAG_SUBTITLE_CODEC)
                name = language_name(lang) if ok else None
                if name:
                    label = name
                if ok2 and codec:
                    label += f" ({codec})"
            out.append(label)
        return out, cur

    def audio_tracks(self):
        """Readable labels ('German, Stereo', ...) and the index of the current track."""
        tracks, cur = self.audio_track_info()
        return audio_track_labels(tracks), cur

    def subtitle_tracks(self):
        """Labels of the DVB/text subtitle streams followed by the teletext
        subtitle pages, and the index of the active one (-1 = off)."""
        tracks, text_cur = self._text_labels()
        pages = self.teletext_pages()
        if self.ttx_page:
            idx = next((i for i, p in enumerate(pages) if p.page == self.ttx_page), -1)
            cur = len(tracks) + idx if idx >= 0 else -1
        else:
            cur = text_cur
        return tracks + [p.label for p in pages], cur

    def select_audio(self, i):
        if not self.playbin:
            return
        if self._pb3:
            streams = self._streams(Gst.StreamType.AUDIO)
            if 0 <= i < len(streams):
                self._select_streams(Gst.StreamType.AUDIO, streams[i])
        else:
            self.playbin.set_property("current-audio", i)
            self._tracks_changed()

    def select_subtitle(self, i):
        if not self.playbin:
            return
        flags = self.playbin.get_property("flags")
        n = len(self._text_labels()[0])
        pages = self.teletext_pages()
        if 0 <= i - n < len(pages):
            self.ttx_page = pages[i - n].page
            self._ttx_last = ""
            if self._ttx_dec is not None:
                self._ttx_dec.set_property("page", self.ttx_page)
            self.playbin.set_property("flags", flags & ~PLAY_FLAGS_TEXT)
            self.emit("subtitle-text", "")
            return
        self.ttx_page = 0
        self.emit("subtitle-text", "")
        if i < 0:
            self.playbin.set_property("flags", flags & ~PLAY_FLAGS_TEXT)
            if self._pb3:
                self._select_streams(Gst.StreamType.TEXT, None)
        else:
            self.playbin.set_property("flags", flags | PLAY_FLAGS_TEXT)
            if self._pb3:
                streams = self._streams(Gst.StreamType.TEXT)
                if i < len(streams):
                    self._select_streams(Gst.StreamType.TEXT, streams[i])
            else:
                self.playbin.set_property("current-text", i)

    def apply_subtitle_font(self):
        """Font of the text subtitles (files, streams) from the preferences."""
        if self.playbin:
            # None: GStreamer's own font and size
            self.playbin.set_property("subtitle-font-desc", subtitle_font_desc(self.settings))

    def disable_video(self, off):
        self.video_off = bool(off)
        if not self.playbin:
            return
        flags = self.playbin.get_property("flags")
        flags = flags & ~PLAY_FLAGS_VIDEO if off else flags | PLAY_FLAGS_VIDEO
        self.playbin.set_property("flags", flags)
        if self._pb3 and self._collection is not None:
            # playbin3 does not follow the flag while playing: select the streams
            video = self._streams(Gst.StreamType.VIDEO)
            self._select_streams(Gst.StreamType.VIDEO, None if off or not video else video[0])

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
        sample = self.playbin.get_property("sample")
        caps = sample.get_caps() if sample else None
        if caps is None and not self._pb3:
            pad = self.playbin.emit("get-video-pad", 0)
            caps = pad.get_current_caps() if pad else None
        if caps:
            s = caps.get_structure(0)
            return s.get_int("width")[1], s.get_int("height")[1]
        return None

    # -- channel analysis ---------------------------------------------------

    def _level_message(self, src, st):
        if not self._measure_loudness or src is None:
            return
        name = src.get_name()
        try:
            if name == "lv-loudness":
                rms = list(st.get_value("rms"))
                self.loudness.feed([10 ** (r / 10) if r > -200 else 0.0 for r in rms])
            elif name == "lv-peak":
                peaks = list(st.get_value("peak"))
                if peaks:
                    self.loudness.feed_peak(max(peaks))
        except (TypeError, ValueError):
            pass

    def _fragment_message(self, st):
        def get(k, conv):
            return conv(st.get_value(k)) if st.has_field(k) else None
        try:
            frag = {"uri": get("uri", str) or "", "bytes": get("fragment-size", int) or 0,
                    "seconds": (get("fragment-download-time", int) or 0) / Gst.SECOND,
                    "time": time.monotonic()}
        except (TypeError, ValueError):
            return
        self.fragments = (self.fragments + [frag])[-30:]

    def audio_bitrate(self):
        """Bit rate of the audio track playing, from its tags (bit/s), or None.

        Internet radio sends it (e.g. icy-br, or the MP3/AAC headers); it is
        what the stream needs, unlike the download rate of the network buffer.
        """
        if not self.playbin:
            return None
        tags = None
        if self._pb3:
            for st in self._streams(Gst.StreamType.AUDIO):
                if st.get_stream_id() in self._selected or tags is None:
                    tags = st.get_tags()
        else:
            cur = max(0, self.playbin.get_property("current-audio"))
            if self.playbin.get_property("n-audio") > cur:
                tags = self.playbin.emit("get-audio-tags", cur)
        if not tags:
            return None
        for tag in (Gst.TAG_NOMINAL_BITRATE, Gst.TAG_BITRATE):
            ok, br = tags.get_uint(tag)
            if ok and 1000 <= br <= 2_000_000:
                return br
        return None

    def video_info(self):
        """What the picture on screen is: codec, size, rate, scan, colour, decoder."""
        if not self.playbin:
            return {}
        out = {}
        caps = None
        tags = None
        if self._pb3:
            for stv in self._streams(Gst.StreamType.VIDEO):
                if stv.get_stream_id() in self._selected or caps is None:
                    caps, tags = stv.get_caps(), stv.get_tags()
        else:
            if self.playbin.get_property("n-video") > 0:
                tags = self.playbin.emit("get-video-tags", 0)
                pad = self.playbin.emit("get-video-pad", 0)
                caps = pad.get_current_caps() if pad else None
        sample = self.playbin.get_property("sample")
        raw = sample.get_caps() if sample else None
        if tags:
            for key, tag in (("codec", Gst.TAG_VIDEO_CODEC), ("container", Gst.TAG_CONTAINER_FORMAT)):
                ok, v = tags.get_string(tag)
                if ok and v:
                    out[key] = v
            ok, br = tags.get_uint(Gst.TAG_BITRATE)
            if ok and br:
                out["bitrate"] = br
            ok, br = tags.get_uint(Gst.TAG_NOMINAL_BITRATE)
            if ok and br:
                out["nominal_bitrate"] = br
        for c in (caps, raw):
            if c is None or not c.get_size():
                continue
            s = c.get_structure(0)
            if not s.get_name().startswith("video/x-raw"):
                out.setdefault("codec_caps", s.get_name())
                for k in ("profile", "level", "tier"):
                    if s.has_field(k):
                        out.setdefault(k, s.get_string(k))
                continue
            ok, w = s.get_int("width")
            ok2, h = s.get_int("height")
            if ok and ok2:
                out["width"], out["height"] = w, h
            ok, n, d = s.get_fraction("framerate")
            if ok and d:
                out["fps"] = n / d
            ok, n, d = s.get_fraction("pixel-aspect-ratio")
            if ok and d:
                out["par"] = (n, d)
            for k in ("interlace-mode", "field-order", "colorimetry", "format", "chroma-site",
                      "mastering-display-info", "content-light-level", "multiview-mode"):
                if s.has_field(k):
                    v = s.get_value(k)
                    out[k] = v if isinstance(v, str) else str(v)
        if self.video_decoder:
            out["decoder"] = hwaccel.describe(self.video_decoder)
            out["hardware"] = self.video_decoder[1] not in (None, "", "software")
        stats = None
        for el in _sink_candidates(self.video.sink):
            if el.find_property("stats"):
                stats = el.get_property("stats")
                break
        if stats is not None:
            for k in ("rendered", "dropped"):
                ok, v = stats.get_uint64(k)
                if ok:
                    out[k] = v
        return out

    def picture_bars(self):
        """(top, bottom, left, right) black borders of the picture as fractions,
        from a small grey copy of the frame on screen; None when there is none."""
        if not self.playbin or not self.has_video():
            return None
        w, h = 160, 90
        caps = Gst.Caps.from_string(
            f"video/x-raw,format=GRAY8,width={w},height={h},pixel-aspect-ratio=1/1")
        sample = self.playbin.emit("convert-sample", caps)
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
            stride = info.stride[0]
            data = bytes(m.data)
        finally:
            buf.unmap(m)
        return black_borders(data, info.width, info.height, stride)

    def buffer_info(self):
        """Buffering of an internet stream: dict percent, in/out rates (bytes/s), left (ms)."""
        if not self.playbin or self.capture:
            return {}
        q = Gst.Query.new_buffering(Gst.Format.TIME)
        if not self.playbin.query(q):
            return {}
        _busy, pct = q.parse_buffering_percent()
        _mode, avg_in, avg_out, left = q.parse_buffering_stats()
        return {"percent": pct, "in": avg_in, "out": avg_out, "left_ms": left}

    # -- bus ----------------------------------------------------------------

    def _on_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            if self._from_visualizer(msg.src):
                # the visualiser (e.g. projectM without a usable OpenGL) must not
                # stop the radio: drop it and play on
                if self._vis_error_token != self._token:
                    self._vis_error_token = self._token
                    print(f"Visualisation failed: {err.message} ({_dbg})")
                    GLib.idle_add(self._drop_visualizer, err.message)
                return
            gpu = hwaccel.failed_backend(msg.src)
            if gpu is not None and self._hw_error_token == self._token:
                return              # more errors of the pipeline being replaced
            if gpu is not None and gpu not in hwaccel.disabled():
                self._hw_error_token = self._token
                # a GPU decoder (driver bug, unsupported profile): switch that
                # interface off for this session and play again without it
                print(f"{hwaccel.NAMES.get(gpu, gpu)} video decoding failed: "
                      f"{err.message} ({_dbg}); trying the next decoder")
                hwaccel.disable(gpu, self._decoder_mode())
                GLib.idle_add(self._hw_fallback, self._token, gpu)
                return
            if self._pb3 and self.uri and not self.capture and not self._fallback_done:
                # e.g. hlsdemux2 refusing a stream: try the classic pipeline once
                self._fallback_done = True
                GLib.idle_add(self._fallback_to_playbin, self._token)
                return
            # one error per playback: the first one names the cause, the ones
            # after it ("not enough data", "can't typefind") only follow from it
            if self.uri and not self.capture:
                if self._error_token == self._token:
                    return
                self._error_token = self._token
            self.emit("error", describe_error(err, _dbg))
        elif t == Gst.MessageType.EOS:
            self.emit("eos")
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.playbin:
            _old, new, _p = msg.parse_state_changed()
            if new == Gst.State.PLAYING:
                self._played = True
                if self._pending_seek is not None:
                    target, self._pending_seek = self._pending_seek, None
                    self.seek_to(target)
                self.emit("state", "playing")
                self.emit("tracks")
        elif t == Gst.MessageType.STREAMS_SELECTED:
            if self._pb3:
                self._collection = msg.parse_streams_selected()
                self._selected = {msg.streams_selected_get_stream(i).get_stream_id()
                                  for i in range(msg.streams_selected_get_size())}
            self.emit("tracks")
        elif t == Gst.MessageType.STREAM_COLLECTION:
            if self._pb3:
                f = msg.src.get_factory() if isinstance(msg.src, Gst.Element) else None
                if msg.src is self.playbin or (f and f.get_name() in ("decodebin3", "uridecodebin3")):
                    self._collection = msg.parse_stream_collection()
            self.emit("tracks")
        elif t == Gst.MessageType.ELEMENT:
            st = msg.get_structure()
            if st is not None and st.get_name() == "spectrum":
                if self._analyze:
                    self.analysis.feed_spectrum(parse_spectrum(st.to_string()))
                return
            if st is not None and st.get_name() == "level":
                self._level_message(msg.src, st)
                return
            if st is not None and st.get_name() == "adaptive-streaming-statistics":
                self._fragment_message(st)
                return
            self._on_section(msg)
        elif t == Gst.MessageType.TAG:
            self._on_tags(msg)
        elif t == Gst.MessageType.BUFFERING:
            pct = msg.parse_buffering()
            if pct < 100 and self._played and not self.capture and self.uri and \
                    getattr(self, "_last_buffering", 100) >= 100:
                self.rebuffers += 1
            self._last_buffering = pct
            if not self.capture and self.uri:
                self.emit("buffering", pct)
            if not self.capture and self.uri and not self.paused:
                self.playbin.set_state(Gst.State.PAUSED if pct < 100 else Gst.State.PLAYING)
