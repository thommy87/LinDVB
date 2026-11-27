# SPDX-License-Identifier: GPL-2.0-or-later
"""Playback with GStreamer playbin.

DVB channels are played from the capture segments through an appsrc, which
gives pause-and-resume timeshift and jumping back and forth for free. Files
and internet streams go to playbin directly.
"""

import os
import re
import threading
import time
import urllib.parse

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gst, GstVideo  # noqa: E402

from .nowplaying import RadioTextDecoder, ancillary_tail  # noqa: E402
from .media_info import (AudioTrack, audio_track_labels, hls_has_alternate_audio,  # noqa: E402
                         language_name, preferred_track, AUDIO_LANGUAGE_AUTO,
                         language_track, playback_language, is_audio_description)
from .audioviz import AudioAnalysis, parse_spectrum  # noqa: E402
from .loudness import LoudnessMeter, k_weighting  # noqa: E402
from . import captions, compat, debuglog, hwaccel, passthrough, subrender, trackcatalog  # noqa: E402
from .teletext import (DEFAULT_SUBTITLE_PAGES, TeletextPage, subtitle_pages_from_pmt,  # noqa: E402
                       teletext_in_pmt)
from .ca import parse_pmt  # noqa: E402
from .recformat import pmt_described_pids  # noqa: E402
from .tuner import SegmentReader, TS_PACKET  # noqa: E402
from .tvheadend import set_login  # noqa: E402

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
# DASH subtitles switched on during playback are fetched again with a seek
# (Player._fetch_subtitles_again) once the subtitle overlay stands: it is
# looked for this often (ms), for this long (s)
SUBTITLE_OVERLAY_POLL_MS = 50
SUBTITLE_OVERLAY_WAIT_S = 1.0

SPECTRUM_BINS = 256
SPECTRUM_INTERVAL = 33_333_333      # ns, 30 updates per second
# hlsdemux2 / dashdemux2: start on a good quality (see lindvb.ipstream)
from .ipstream import (ADAPTIVE_DEMUX_SETTINGS, META_REPEAT, REFUSED_CODES, Retry,  # noqa: E402
                       fetch_manifest, find_meta, host_addresses, rendition_for_pid,
                       resolve_redirects, setup_http_source)
from .streaminfo import handover_master, hls_stream_name, parse_hls, without_variant_sound  # noqa: E402
from . import i18n  # noqa: E402
from .i18n import _  # noqa: E402

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


def _codec_from_caps(st):
    """A codec name from caps alone (a stream without codec tag): MPEG audio
    by its version and layer, so the OSD says AAC or MP2 and not "mpeg"."""
    name = st.get_name()
    if name == "audio/mpeg":
        ok, version = st.get_int("mpegversion")
        if ok and version in (2, 4):
            return "MPEG-4 AAC"
        ok, layer = st.get_int("layer")
        if ok and layer in (1, 2, 3):
            return f"MPEG-1 Layer {layer}"
    return name.split("/")[-1]


def _describes_video(media):
    """An HLS rendition (EXT-X-MEDIA) marked as audio description."""
    return "public.accessibility.describes-video" in ((media or {}).get("characteristics") or "")


# the debug log's name for the track catalog lines (stage D, shadow mode)
_TRACKS_LOG = "Tracks"


def _pid_of(stream_id):
    """The PID at the end of a tsdemux stream id (".../00000101"), or -1."""
    tail = (stream_id or "").rsplit("/", 1)[-1].split(":")[-1]
    try:
        return int(tail, 16) if 1 <= len(tail) <= 8 else -1
    except ValueError:
        return -1


def _has_playbin3():
    return Gst.ElementFactory.find("playbin3") is not None


def _is_hls_url(uri):
    return uri.startswith(("http://", "https://")) and "m3u8" in uri.lower()


def _fetch_text(url, limit=256 * 1024, timeout=4):
    """(text, data: address to play instead or None, why) of an HLS
    playlist; the data: address when GStreamer cannot start from the address
    itself (lindvb.streaminfo.handover_master)."""
    text, final, compressed = fetch_manifest(url, limit, timeout)
    return (text, *handover_master(text, final, compressed))


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


def _track_gpu_element(_bin, _sub, element):
    """deep-element-added in debug mode: count the GPU elements (decoders,
    vapostproc, ... of any GPU interface) until they are finalized."""
    f = element.get_factory()
    if f is not None and hwaccel.backend_of(f.get_plugin_name()) != "software":
        debuglog.track(element, f.get_name())


# GPU connections the GPU elements share through a GstContext (the VA-API
# display, the GL display, ...): the object that holds the driver's resources
_GPU_CONTEXTS = {"gst.va.display.handle": ("gst-display", "VA display"),
                 "gst.vulkan.device": ("device", "Vulkan device"),
                 "gst.gl.GLDisplay": ("gst.gl.GLDisplay", "GL display")}


def _track_gpu_context(_bus, msg):
    """have-context in debug mode: count the GPU connection in it until it
    is finalized (does one stay while the pipelines go?)."""
    ctx = msg.parse_have_context()
    field, kind = _GPU_CONTEXTS.get(ctx.get_context_type() if ctx else "", (None, None))
    if field is None:
        return
    try:
        obj = ctx.get_structure().get_value(field)
    except (TypeError, ValueError):
        return
    if isinstance(obj, GObject.Object):
        debuglog.track(obj, kind)


def held_contexts(sink):
    """The GstContext types the video sink (kept from pipeline to pipeline)
    and the sinks inside it hold, for the debug log."""
    types = set()
    for el in _sink_candidates(sink):
        if hasattr(el, "get_contexts"):
            types.update(c.get_context_type() for c in el.get_contexts())
    return sorted(types)


def output_label(kind):
    """How the picture reaches the screen (VideoBin.kind), for the analyser."""
    if kind == "copy":
        return _("frame by frame copied")
    if "OpenGL" in kind:
        return _("through the graphics card (gtk4paintablesink, OpenGL)")
    return kind


def trial_output_error(video, msg, dbg):
    """The error *msg* (debug text *dbg*) is one of the GPU video output on
    trial in VideoBin *video*: from inside it, or a picture it refused."""
    return video.trial and (_inside(msg.src, video.sink) or "not-negotiated" in (dbg or ""))


def _inside(el, container):
    """*el* is *container* or one of the elements in it (at any depth)."""
    while el is not None:
        if el is container:
            return True
        el = el.get_parent()
    return False


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


def http_status(err, debug=""):
    """The HTTP status in a GStreamer error, or None."""
    m = re.search(r"\((40[134]|410|451)\)", f"{err.message} {debug or ''}")
    return int(m.group(1)) if m else None


def refused_host(debug=""):
    """The host of the address a GStreamer HTTP error names, or None."""
    m = re.search(r"URL:\s*(\S+?)(?:,|\s|$)", debug or "")
    return urllib.parse.urlsplit(m.group(1)).hostname if m else None


def redirect_target(debug=""):
    """The address souphttpsrc names in "Redirect to: …", or None."""
    m = re.search(r"Redirect to:\s*(\S+?)\)?\s*$", (debug or "").strip(), re.M)
    return m.group(1) if m else None


def describe_error(err, debug=""):
    """A readable text for a GStreamer error; HTTP refusals get their reason."""
    code = http_status(err, debug)
    if code is not None:
        if code in REFUSED_CODES:
            host = refused_host(debug)
            who = host or "The server"
            return (f"{who} refused the stream (HTTP {code}). It may be "
                    "limited to certain countries or devices.")
        return f"The stream was not found on the server (HTTP {code})."
    return err.message


def ssm_rtp_to_udp(url):
    """rtp://source@group:port (source-specific multicast) -> (udp uri, source)."""
    m = re.match(r"(rtp|udp)://(?:([^@/]+)@)?([^:/]+):(\d+)", url)
    if not m:
        return url, None
    return f"udp://{m.group(3)}:{m.group(4)}", m.group(2)


class Feeder(threading.Thread):
    """Pushes TS data from a SegmentReader into an appsrc, blocking when full.

    *on_meta(offset, fields)* gets the song title packets of internet radio
    (lindvb.ipstream.META_PID) with their byte offset in the capture."""

    def __init__(self, reader, appsrc, on_meta=None):
        super().__init__(daemon=True)
        self.reader, self.appsrc = reader, appsrc
        self.on_meta = on_meta
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
            if self.on_meta is not None:
                start = self.reader.offset - len(data)
                for pos, fields in find_meta(data):
                    self.on_meta(start + pos, fields)
            if self.appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(data)) != Gst.FlowReturn.OK:
                break
        self.reader.close()

    def stop(self):
        self.running = False


def capture_buffer(end, read_offset, level, limit, byte_rate):
    """Buffer of an internet channel played from its capture: the bytes in the
    capture not yet read by the Feeder plus those waiting in the appsrc.
    *percent* is that against the appsrc's size (a full appsrc = 100 %),
    *left_ms* the playing time it holds at the stream's byte rate."""
    ahead = max(0, int(end) - int(read_offset)) + max(0, int(level or 0))
    out = {"ahead_bytes": ahead}
    if limit:
        out["percent"] = min(100, round(ahead * 100 / limit))
    if byte_rate and byte_rate > 0:
        out["left_ms"] = int(ahead * 1000 / byte_rate)
        out["out"] = byte_rate
    return out


def display_size(s):
    """(width, height) of a video caps structure as it is shown: the width
    stretched by the pixel aspect ratio, so 720x576 at 64/45 (SD 16:9) is
    1024x576. Without a pixel-aspect-ratio field the pixels count as square."""
    w, h = s.get_int("width")[1], s.get_int("height")[1]
    ok, n, d = s.get_fraction("pixel-aspect-ratio")
    if ok and n > 0 and d > 0 and n != d:
        w = round(w * n / d)
    return w, h


# Packages that bring gtk4paintablesink themselves, built from Ubuntu's Rust
# sources for them (concept §10). While it is tested there it is used only
# when switched on; the system packages use it whenever it is installed.
TRIAL_PACKAGES = ("AppImage", "Snap")
# LINDVB_VIDEO_OUTPUT=gl or =copy overrides the setting (for tests and bug reports)
VIDEO_OUTPUT_ENV = "LINDVB_VIDEO_OUTPUT"
_gl_output_failed = False       # the GPU video output failed in this session


def gtk4_sink_wanted(settings):
    """Whether VideoBin may use gtk4paintablesink: never without it, never after
    it failed in this session, only when switched on in TRIAL_PACKAGES, else
    always (as before)."""
    if _gl_output_failed or Gst.ElementFactory.find("gtk4paintablesink") is None:
        return False
    forced = os.environ.get(VIDEO_OUTPUT_ENV, "").strip().lower()
    if forced in ("gl", "copy"):
        return forced == "gl"
    if compat.bundled() in TRIAL_PACKAGES:
        return bool(settings.get("video_output_gl", False))
    return True


def gl_output_failed():
    """The GPU video output (on trial) failed: the rest of the session copies."""
    global _gl_output_failed
    _gl_output_failed = True


class VideoBin:
    """Video sink for GTK 4: gtk4paintablesink if present (and *gtk4*), else
    appsink -> GdkTexture.

    In TRIAL_PACKAGES gtk4paintablesink is on trial (``trial``): it is used only
    with OpenGL (glsinkbin) and goes back to the appsink when GDK has no GL
    context; Player falls back to the appsink when it fails while playing."""

    def __init__(self, picture, gtk4=True):
        self.picture = picture
        self.trial = False
        self.kind = "copy"          # how the picture reaches GTK, for the log
        sink = Gst.ElementFactory.make("gtk4paintablesink") if gtk4 else None
        if sink is not None and compat.bundled() in TRIAL_PACKAGES:
            sink = self._trial_sink(sink)
        if sink is not None:
            self.kind = "gtk4paintablesink"
            picture.set_paintable(sink.get_property("paintable"))
            glsink = Gst.ElementFactory.make("glsinkbin")
            if glsink is not None:
                glsink.set_property("sink", sink)
                self.sink = glsink
                self.kind += " (OpenGL)"
            else:
                self.sink = sink
            return
        bin_ = Gst.Bin.new("videoout")
        conv = Gst.ElementFactory.make("videoconvert")
        # A GdkMemoryTexture has square pixels. Anamorphic pictures (SD 16:9
        # as 720x576 with pixel-aspect-ratio 64/45, HD as 1440x1080 with 4/3)
        # would be shown squeezed, so videoscale stretches them to square
        # pixels first (720x576 16:9 -> 1024x576). gtk4paintablesink above
        # applies the pixel aspect ratio itself.
        scale = Gst.ElementFactory.make("videoscale")
        appsink = Gst.ElementFactory.make("appsink")
        # Opaque formats: visualisers such as GOOM leave junk in the padding
        # byte, which RGBA would turn into transparency.
        if hasattr(Gdk.MemoryFormat, "R8G8B8X8"):
            self._format, caps = Gdk.MemoryFormat.R8G8B8X8, "RGBx"
        else:
            self._format, caps = Gdk.MemoryFormat.R8G8B8, "RGB"
        appsink.set_property("caps", Gst.Caps.from_string(
            f"video/x-raw,format={caps},pixel-aspect-ratio=1/1"))
        appsink.set_property("emit-signals", True)
        appsink.set_property("max-buffers", 2)
        appsink.set_property("drop", True)
        appsink.connect("new-sample", self._on_sample)
        chain = [conv] + ([scale] if scale is not None else []) + [appsink]
        for e in chain:
            bin_.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        bin_.add_pad(Gst.GhostPad.new("sink", conv.get_static_pad("sink")))
        self.sink = bin_
        self._pending = None

    def _trial_sink(self, sink):
        """*sink* when GDK gave it an OpenGL context (it then gets the frames
        as GL textures, decoded on the GPU without a copy), else None: the
        appsink, which every system has used so far."""
        paintable = sink.get_property("paintable")
        if paintable is None or not paintable.find_property("gl-context") or \
                paintable.get_property("gl-context") is None or \
                Gst.ElementFactory.find("glsinkbin") is None:
            debuglog.write("Player", "video output through the graphics card: "
                                     "no OpenGL, the frames are copied")
            return None
        self.trial = True
        return sink

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
    cc = None                    # captions.CaptionOverlay of the current pipeline
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
        # something the user should know about what plays (status line)
        "notice": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # what is playing (dict: title, artist, album, year, track, image,
        # organization, radiotext); sent again whenever it changes
        "metadata": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        # how full the network buffer of an internet stream is (0..100)
        "buffering": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        # an internet stream played directly lost its connection: (seconds
        # until it is opened again, the reason); "reconnected" when it plays
        "reconnecting": (GObject.SignalFlags.RUN_LAST, None, (int, str)),
        "reconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, picture, settings):
        super().__init__()
        self.settings = settings
        enable_ttml()
        self.apply_decoder_setting()
        self.video_decoder = None       # (factory, backend) decoding the video now
        self.video_deinterlacer = None  # factory name of the deinterlacer in use
        self.video = VideoBin(picture, gtk4_sink_wanted(settings))
        debuglog.write("Player", f"video output: {self.video.kind}")
        self.playbin = None
        self.video_off = False          # "Video Off (audio only)": kept across rebuilds
        self._audio_pref_done = False   # preferred audio format applied for this stream
        self._auto_audio = -1           # the track chosen for the channel or file
        self.radio = False              # a radio channel plays: no passthrough (visualisations)
        self.passthrough_formats = ()   # formats going undecoded to the receiver now
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
        # stage F: the teletext renderer, in the shadow of this path while
        # debug mode is on (lindvb.subrender)
        self._ttx_shadow = None
        self._ttx_cmp = None
        self._cue_shadow = None         # and the text cue renderer (WebVTT)
        self._cue_cmp = None
        self._cue_drawer = None         # draws its cues at their time (stage F)
        # the subtitles the user picked for this channel or file: None (off),
        # ("text", stream name or index), ("ttx", page) or ("cc", service).
        # Every channel starts with them off.
        self._sub_choice = None
        self._want_rendition = None     # (TYPE, NAME) to pick once playbin3 offers it
        self.renditions = {}            # HLS address -> its EXT-X-MEDIA renditions
        self._cc_pmt = {}               # caption services the PMT names {service: language}
        self._ad_pids = set()           # audio description PIDs the PMT names
        self._pmt_raw = b""             # the PMT section of what plays (track catalog)
        self._catalog_logged = None     # what the debug log last said about the catalog
        self._catalog_timer = 0
        # Internet streams with the audio in separate HLS renditions run on
        # playbin3: the old hlsdemux in playbin lines those renditions up by
        # playlist position instead of by timestamp, which puts sound and
        # picture apart by up to a segment (seconds). hlsdemux2 syncs on PTS.
        self._pb3 = False
        self._collection = None         # playbin3: Gst.StreamCollection
        self._selected = set()          # playbin3: ids of the selected streams
        self._token = 0                 # bumps on every play/stop, cancels stale probes
        self._hls_alt_audio = {}        # url -> master playlist has audio renditions
        self._hls_unpacked = {}         # url -> data: address of its compressed playlist
        self._fallback_done = False
        self._error_token = -1          # playback that already reported an error
        self._resolve_token = -1        # playback whose address was resolved once
        self._refused_token = -1        # playback whose refusal was reported
        self._hw_error_token = -1       # playback whose GPU decoder failed
        self._video_error_token = -1    # playback whose GPU video output failed
        self._empty_start_capture = None  # capture started again after an empty start
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
        # reconnecting an internet stream that broke (lindvb.ipstream.Retry)
        self._retry = None
        self._reconnecting = False
        self._reconnect_timer = 0
        # song titles of internet radio from the buffer: (offset, fields) the
        # feeder found ahead of what plays, shown once playback gets there
        self._meta_queue = []
        self._meta_lock = threading.Lock()
        self._meta_timer = 0

    # -- pipeline -----------------------------------------------------------

    def _build(self, pb3=False):
        self._teardown()
        self.video_decoder = None
        self._pb3 = bool(pb3)
        self._dash = False              # dashdemux plays (classic playbin)
        self._collection, self._selected = None, set()
        self.fragments = []
        self.rebuffers = 0
        self._played = False
        pb = Gst.ElementFactory.make("playbin3" if pb3 else "playbin", "player")
        if debuglog.enabled():
            # do old pipelines and their GPU elements go? (graphics buffers
            # piled up with VA-API, most with vadeinterlace)
            debuglog.track(pb, "pipeline")
            pb.connect("deep-element-added", _track_gpu_element)
            self._cue_shadow_hook(pb)
        pb.set_property("video-sink", self.video.sink)
        # subtitles start switched off; the user turns them on in Subtitles
        if not (self._sub_choice and self._sub_choice[0] == "text") and \
                not (self._want_rendition and self._want_rendition[0] == "SUBTITLES"):
            pb.set_property("flags", pb.get_property("flags") & ~PLAY_FLAGS_TEXT)
        vf = Gst.Bin.new("vfilter")
        head, middle = [], [Gst.ElementFactory.make("videoconvert")]
        self.video_deinterlacer = None
        if self.settings["deinterlace"]:
            gpu = hwaccel.preferred(self._decoder_mode(), hwaccel.available())
            deint = hwaccel.make_deinterlacer(gpu)
            if deint is not None:
                self.video_deinterlacer = deint.get_factory().get_name()
                _track_gpu_element(None, None, deint)
            if deint is not None and deint.get_factory().get_name() == "vadeinterlace":
                # first in the chain: it takes the VA-API decoder's frames
                # straight from the graphics card and deinterlaces them there
                head.append(deint)
            elif deint is not None:
                middle.append(deint)
        self.vbal = Gst.ElementFactory.make("videobalance")
        middle += [self.vbal, Gst.ElementFactory.make("videoconvert")]
        # ATSC / US cable closed captions (CEA-708 in the video stream); they
        # are offered in Subtitles once a frame really carries them and start
        # switched off on every channel (lindvb.captions). The overlay goes
        # last, so it draws into the converted frames (see lindvb.captions).
        self.cc = None
        if captions.available():
            self.cc = captions.CaptionOverlay(
                silent=True, on_found=self._tracks_changed,
                log=lambda msg: debuglog.write("Player", msg), middle=middle)
            chain = head + [self.cc.bin]
            if self._sub_choice and self._sub_choice[0] == "cc":
                self.cc.show(self._sub_choice[1])
        else:
            chain = head + middle
        for e in chain:
            vf.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        first = chain[0].get_static_pad("sink") or chain[0].get_static_pad("video_sink")
        vf.add_pad(Gst.GhostPad.new("sink", first))
        vf.add_pad(Gst.GhostPad.new("src", chain[-1].get_static_pad("src")))
        pb.set_property("video-filter", vf)
        asink = self._passthrough_sink()
        if asink is not None:
            # no audio filter: playsink keeps it in the chain when a track
            # changes from PCM to AC-3, and the compressed caps then fail
            self.eq = self.spectrum = None
            self._level_elements = []
            pb.set_property("audio-sink", asink)
            if pb3:
                self._passthrough_pb3(pb)
        else:
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
        if debuglog.enabled():
            bus.connect("message::have-context", _track_gpu_context)
        if pb3:
            # the first choice of streams has to be made before playbin3
            # starts them: it would show a subtitle stream on its own, and
            # changing the subtitle stream while it plays is fragile
            bus.enable_sync_message_emission()
            bus.connect("sync-message::stream-collection", self._on_sync_collection)
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

    def _passthrough_sink(self):
        """The audio sink for passthrough (lindvb.passthrough), or None to play
        decoded sound as usual: passthrough off, a radio channel, or a sink that
        takes none of the chosen formats."""
        self.passthrough_formats = ()
        wanted = passthrough.codecs(self.settings)
        if not wanted or self.radio:
            return None
        factory, device = passthrough.sink_spec(self.settings.get("audio_passthrough_device", ""))
        sink = Gst.ElementFactory.make(factory)
        if sink is None:
            debuglog.write("Player", f"passthrough: no {factory}")
            return None
        if device:
            sink.set_property("device", device)
        accepted = self._sink_accepts(sink, wanted)
        if not accepted:
            debuglog.write("Player", f"passthrough: {factory} {device or ''} takes none of "
                                     f"{passthrough.describe(wanted)}, decoding")
            return None
        bin_ = Gst.Bin.new("passthrough-sink")
        caps = Gst.ElementFactory.make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(passthrough.caps_string(accepted)))
        bin_.add(caps)
        bin_.add(sink)
        caps.link(sink)
        bin_.add_pad(Gst.GhostPad.new("sink", caps.get_static_pad("sink")))
        self.passthrough_formats = tuple(accepted)
        debuglog.write("Player", f"passthrough: {passthrough.describe(accepted)} to "
                                 f"{factory} {device or ''}")
        return bin_

    @staticmethod
    def _sink_accepts(sink, wanted):
        """The formats of *wanted* the sink takes; it is asked in READY, where
        pulsesink and alsasink know what the output can do."""
        if sink.set_state(Gst.State.READY) == Gst.StateChangeReturn.FAILURE:
            sink.set_state(Gst.State.NULL)
            return []
        try:
            caps = sink.get_static_pad("sink").query_caps(None)
            return [c for c in wanted
                    if caps.can_intersect(Gst.Caps.from_string(passthrough.CAPS[c]))]
        finally:
            sink.set_state(Gst.State.NULL)

    def _passthrough_pb3(self, pb):
        """playbin3 decodes everything its uridecodebin3 is not told to stop
        at: add the passthrough formats to its caps."""
        dec = pb.get_by_name("uridecodebin3")
        if dec is None or dec.find_property("caps") is None:
            return
        caps = dec.get_property("caps")
        caps = caps.copy() if caps is not None else Gst.Caps.from_string(
            "video/x-raw(ANY); audio/x-raw(ANY); text/x-raw(ANY)")
        caps.append(Gst.Caps.from_string(passthrough.caps_string(self.passthrough_formats,
                                                                 raw=False)))
        dec.set_property("caps", caps)

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
        """Hand the transport stream of internet channels to fn (streaming thread),
        as it reaches tsdemux: from the network, or from the timeshift buffer of
        a channel read into a capture (lindvb.ipstream). DVB captures are
        tapped at the capture (Capture.add_tap)."""
        self._ts_taps = tuple(self._ts_taps) + (fn,)

    def remove_ts_tap(self, fn):
        self._ts_taps = tuple(t for t in self._ts_taps if t is not fn)

    def _ts_probe(self, _pad, info):
        taps = self._ts_taps
        if taps and (not self.capture or self.plays_stream_capture()):
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

    def _empty_start(self, src, dbg):
        """True when playing from the buffer ended before the first picture
        (a parser reached the end of its stream without one valid frame):
        start once more at the live end, instead of the error.

        3sat HD (DASH) did that 35 ms after the start in 3 of 5 starts (logs
        of 24. and 25.11.2026), while switching away and back played. The
        cause lies in the buffer's first bytes; the live end is past them."""
        factory = src.get_factory() if isinstance(src, Gst.Element) else None
        if self.capture is None or self._played or factory is None or \
                "Parser" not in (factory.get_metadata("klass") or "") or \
                "gst_base_parse_sink_event_default" not in (dbg or "") or \
                self._empty_start_capture is self.capture:
            return False
        self._empty_start_capture = self.capture
        debuglog.write("Player", "the buffer's start ended before the first picture: "
                                 "starting again at the live end")
        GLib.timeout_add(500, self._start_again_live, self._token)
        return True

    def _start_again_live(self, token):
        if token == self._token and self.capture is not None:
            self.play_capture(self.capture, self.sid)
        return False

    def restart(self):
        """Rebuild the pipeline and continue at the same position."""
        if self.capture:
            self.play_capture(self.capture, self.sid, self.played_offset())
        elif self.uri:
            pos = self.position()
            self.play_uri(self.uri)
            self._pending_seek = pos[0] if pos else None

    def _teardown(self):
        self._end_ttx_shadow()
        self._end_cue_shadow()
        self._ttx_dec = None
        self._ttx_present = False
        self._ttx_pages = []
        self._ttx_in_pmt = False
        self._ttx_last = ""
        self.emit("subtitle-text", "")
        if self.feeder:
            self.feeder.stop()
            self.feeder = None
        self._stop_meta()
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
            on_meta = None
            if getattr(self.capture, "meta_titles", False):
                token = self._token
                on_meta = lambda off, fields: self._queue_meta(off, fields, token)
            self.feeder = Feeder(SegmentReader(self.capture.store, self._start_offset), src,
                                 on_meta)
            self.feeder.start()
        elif name == "udpsrc" and self._ssm_source:
            if src.find_property("multicast-source"):
                src.set_property("multicast-source", self._ssm_source)
            src.set_property("caps", Gst.Caps.from_string(
                "application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T"))
        else:
            # an HTTP source: our user agent (a broadcaster CDN refuses an
            # unknown client with 403) and, for Tvheadend, the server login
            setup_http_source(src)
            if src.find_property("user-id") is not None and self.uri:
                set_login(src, self.uri)

    def _on_element_setup(self, _pb, element):
        f = element.get_factory()
        dec = hwaccel.factory_backend(element)
        if dec is not None:
            self.video_decoder = dec
        if f and f.get_name() == "dashdemux":
            self._dash = True
        if f and f.get_name() in ("hlsdemux2", "dashdemux2"):
            for k, v in ADAPTIVE_DEMUX_SETTINGS.items():
                if element.find_property(k) is not None:
                    element.set_property(k, v)
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
        first = self._ttx_shadow_branch(parent, q)
        if pad.link(first.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            return
        self._ttx_dec = dec
        self._ttx_present = True
        GLib.idle_add(lambda: (self.emit("tracks"), False)[1])

    # -- now playing --------------------------------------------------------

    def _reset_metadata(self):
        self._audio_pref_done = False       # a new channel or file: choose again
        # subtitles start switched off on every channel and file
        self._sub_choice = None
        self._want_rendition = None
        self.ttx_page = 0
        self._ttx_last = ""
        self._cc_pmt = {}
        self._ad_pids = set()
        self._pmt_raw = b""
        self._catalog_logged = None
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

    # internet radio in the buffer: the song titles at the place that plays

    def _start_meta(self, capture, offset):
        """Show the title that ran at *offset* now (the last title packet
        before it), and the later ones as playback reaches them."""
        window = int(capture.byte_rate() * (META_REPEAT + 2))
        start, _end = capture.store.bounds()
        reader = SegmentReader(capture.store, max(start, offset - window))
        last = None
        try:
            while reader.offset < offset:
                data = reader.read(min(TS_PACKET * 512, offset - reader.offset + TS_PACKET))
                if not data:
                    break
                for _pos, fields in find_meta(data):
                    last = fields
        finally:
            reader.close()
        if last:
            self._update_metadata(dict(last), self._token)
        self._meta_timer = GLib.timeout_add(250, self._apply_meta, self._token)

    def _stop_meta(self):
        if self._meta_timer:
            GLib.source_remove(self._meta_timer)
            self._meta_timer = 0
        with self._meta_lock:
            self._meta_queue = []

    def _queue_meta(self, offset, fields, token):
        """Feeder thread: a title packet ahead of what plays."""
        with self._meta_lock:
            if token != self._token:
                return                       # the feeder of an earlier start
            q = self._meta_queue
            if q and q[-1][1] == fields:
                return                       # a repeat: the title did not change
            q.append((offset, fields))
            del q[:-64]

    def _apply_meta(self, token):
        """Main thread: show the title of the place playback reached."""
        if token != self._token:
            return False                     # removed with its playback (_stop_meta)
        if not self.capture:
            self._meta_timer = 0
            return False
        now, fields = self.played_offset(), None
        with self._meta_lock:
            q = self._meta_queue
            while q and q[0][0] <= now:
                fields = q.pop(0)[1]
        if fields:
            self._update_metadata(dict(fields), token)
        return True

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
            text = subrender.teletext_text(info.data)
            buf.unmap(info)
            if self.ttx_page and text != self._ttx_last:
                self._ttx_last = text
                GLib.idle_add(self._show_subtitle, text)
        return Gst.FlowReturn.OK

    def _show_subtitle(self, text):
        if self.ttx_page:
            self.emit("subtitle-text", text)
            if self._ttx_cmp is not None:
                self._ttx_cmp.saw_old(text)
        return False

    # -- the teletext renderer in the shadow (stage F) ----------------------

    def _ttx_shadow_branch(self, parent, old_queue):
        """The element to link tsdemux's teletext pad to: the old queue, or
        with debug mode on a tee that also feeds the teletext renderer of
        stage F (behind its leaky queue, so it cannot hold the old path up)."""
        if not debuglog.enabled():
            return old_queue
        try:
            tee = Gst.ElementFactory.make("tee")
            shadow = subrender.TeletextRenderer(self._on_shadow_text, warm=True,
                                                page=DEFAULT_SUBTITLE_PAGES[0])
            parent.add(tee)
            parent.add(shadow.element)
            if not (tee.link(old_queue) and tee.link(shadow.element)):
                raise RuntimeError("could not link the tee")
            shadow.element.sync_state_with_parent()
            tee.sync_state_with_parent()
        except Exception as e:  # noqa: BLE001 - the shadow must never cost the subtitles
            debuglog.write("Player", f"teletext renderer (shadow) not started: {e}")
            return old_queue
        self._ttx_shadow = shadow
        return tee

    def _on_shadow_text(self, text):
        cmp = self._ttx_cmp
        if cmp is not None:
            GLib.idle_add(lambda: (cmp.saw_new(text), False)[1])

    def _ttx_shadow_follow(self, kind, key):
        """The shadow renderer shows what the user picked, like the old path."""
        shadow = self._ttx_shadow
        if shadow is None:
            return
        self._report_ttx_shadow()
        if kind == "ttx":
            track = trackcatalog.Track(trackcatalog.SUBTITLE, subrender.TELETEXT,
                                       origin=f"ttx:{key}")
            self._ttx_cmp = subrender.Shadow(track.origin)
            shadow.show(track)
        else:
            shadow.off()

    def _report_ttx_shadow(self):
        cmp, self._ttx_cmp = self._ttx_cmp, None
        if cmp is not None and self._ttx_shadow is not None:
            line = cmp.report(self._ttx_shadow.state())
            if line:
                debuglog.write("Player", line)

    def _end_ttx_shadow(self):
        if self._ttx_shadow is not None:
            self._report_ttx_shadow()
            self._ttx_shadow.off()
        self._ttx_shadow = None

    # -- the text cue renderer in the shadow (stage F) ----------------------

    def _cue_shadow_hook(self, pb):
        """Debug mode: the text cue renderer gets a copy of what the subtitle
        overlay's own parser (subparse, ttmlparse) gets, while text subtitles
        are shown, and what that parser decoded is noted beside it. Probes
        only copy; nothing waits for the renderer."""
        try:
            self._cue_shadow = subrender.TextCueRenderer(self._on_shadow_cue)
            # in the shadow it draws nothing yet, it says how soon it would have
            self._cue_drawer = subrender.CueDrawer(self._cue_position, lambda _t, _p: None,
                                                   lambda: self.rate,
                                                   on_shown=self._on_cue_drawn)
        except Exception as e:  # noqa: BLE001 - the shadow must never cost the subtitles
            debuglog.write("Player", f"text cue renderer (shadow) not started: {e}")
            return
        pb.connect("deep-element-added", self._on_cue_overlay)

    def _on_cue_overlay(self, _bin, _sub, element):
        f = element.get_factory()
        if f is not None and f.get_name() == "subtitleoverlay":
            element.connect("element-added", self._on_cue_overlay_child)

    def _on_cue_overlay_child(self, _overlay, element):
        f = element.get_factory()
        if f is not None and f.get_name() in ("subparse", "ttmlparse"):
            element.get_static_pad("sink").add_probe(
                Gst.PadProbeType.BUFFER, self._feed_cue_shadow, self._cue_shadow)
            element.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._old_cue)

    def _feed_cue_shadow(self, pad, info, shadow):
        """Streaming thread: on while playbin shows text subtitles (the old
        path draws them), off otherwise. Whatever happens in the shadow, the
        buffer goes on."""
        try:
            self._cue_shadow_buffer(pad, info, shadow)
        except Exception as e:  # noqa: BLE001 - the shadow must never cost the subtitles
            debuglog.write("Player", f"text cue renderer (shadow): {e}")
        return Gst.PadProbeReturn.OK

    def _cue_shadow_buffer(self, pad, info, shadow):
        pb = self.playbin
        if shadow is not self._cue_shadow or pb is None:
            return
        caps = pad.get_current_caps()
        name = caps.get_structure(0).get_name() if caps is not None and caps.get_size() else ""
        shown = name in subrender.CUE_PARSERS and bool(pb.get_property("flags") & PLAY_FLAGS_TEXT)
        if shown and not shadow.state().on:
            self._cue_cmp = subrender.CueShadow("text")
            shadow.show(trackcatalog.Track(trackcatalog.SUBTITLE, subrender.CUE_PARSERS[name][1],
                                           origin="text"))
            GLib.idle_add(self._start_cue_drawer)
        elif not shown and shadow.state().on:
            cmp, state = self._cue_cmp, shadow.state()
            self._cue_cmp = None
            shadow.off()
            GLib.idle_add(lambda: (self._write_cue_report(cmp, state), False)[1])
        if shown:
            ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
            shadow.feed(info.get_buffer(), caps, ev.parse_segment() if ev else None)

    def _cue_shadow_follow(self, kind):
        """Subtitles picked: text subtitles off, so the text cue renderer
        goes off too and reports."""
        shadow = self._cue_shadow
        if kind != "text" and shadow is not None and shadow.state().on:
            cmp, state = self._cue_cmp, shadow.state()
            self._cue_cmp = None
            shadow.off()
            self._write_cue_report(cmp, state)

    def _write_cue_report(self, cmp, state):
        """The shadow's lines in the debug log, for the cues due up to where
        the playback is (main loop; the cues noted from streaming threads
        are queued to it before this). The drawer stops with it."""
        until = self._cue_position()
        drawer = self._cue_drawer
        if drawer is not None:
            drawer.stop()
        if cmp is None:
            return
        for line in (cmp.report(state, until), cmp.drawing_report(state, until)):
            if line:
                debuglog.write("Player", line)

    def _cue_position(self):
        """The playback position (stream time, ns), None when there is none."""
        if self.playbin is None:
            return None
        ok, pos = self.playbin.query_position(Gst.Format.TIME)
        return pos if ok and pos >= 0 else None

    def _start_cue_drawer(self):
        """Main loop: the shadow went on, so the drawer draws its cues (into
        the comparison only; the old path still draws what is seen)."""
        if self._cue_drawer is not None and self._cue_cmp is not None:
            self._cue_drawer.start()
        return False

    def _on_cue_drawn(self, text, start, late):
        cmp = self._cue_cmp
        if cmp is not None:
            cmp.drew(text, start, late)

    def _old_cue(self, pad, info):
        try:
            self._note_old_cue(pad, info)
        except Exception as e:  # noqa: BLE001 - the shadow must never cost the subtitles
            debuglog.write("Player", f"text cue renderer (shadow): {e}")
        return Gst.PadProbeReturn.OK

    def _note_old_cue(self, pad, info):
        buf = info.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if ok:
            text = subrender.cue_text(m.data)
            buf.unmap(m)
            ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
            t = subrender.stream_time(ev.parse_segment() if ev else None, buf.pts, buf.duration)
            cmp = self._cue_cmp
            if cmp is not None and text:
                GLib.idle_add(lambda: (cmp.saw_old(text, t), False)[1])

    def _on_shadow_cue(self, text, t, end):
        cmp = self._cue_cmp
        if cmp is not None:
            GLib.idle_add(self._shadow_cue_arrived, cmp, text, t, end)

    def _shadow_cue_arrived(self, cmp, text, t, end):
        cmp.saw_new(text, t)
        drawer = self._cue_drawer
        if drawer is not None and cmp is self._cue_cmp:
            drawer.add(text, t, end)
        return False

    def _end_cue_shadow(self):
        shadow, self._cue_shadow = self._cue_shadow, None
        if shadow is not None:
            cmp, state = self._cue_cmp, shadow.state()
            self._cue_cmp = None
            shadow.close()
            self._write_cue_report(cmp, state)
        self._cue_drawer = None

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
            pmt = parse_pmt(bytes(raw))
            ad = pmt_described_pids(bytes(raw))
        except ValueError:
            return
        if self.sid and program != self.sid:
            return
        if bytes(raw) != self._pmt_raw:
            self._pmt_raw = bytes(raw)
            self._shadow_catalog_later()
        cc = captions.caption_services_from_descriptors(pmt.program_info)
        for _st, _pid, _ca, info in pmt.streams:
            cc.update(captions.caption_services_from_descriptors(info))
        changed = present != self._ttx_in_pmt or cc != self._cc_pmt
        changed = self._described_pids(ad) or changed
        self._cc_pmt = cc
        self._ttx_in_pmt = present
        if pages and pages != self._ttx_pages:
            self._ttx_pages = pages
            changed = True
        if changed:
            self.emit("tracks")

    def _described_pids(self, ad):
        """The PMT names the audio description PIDs *ad*. True when that is new."""
        if ad == self._ad_pids:
            return False
        self._ad_pids = ad
        if self._audio_pref_done and self.audio_track_info()[1] == self._auto_audio:
            # the sound was chosen before the PMT named the audio
            # description tracks, and not by the user: choose again
            self._audio_pref_done = False
            self._tracks_changed()
        return True

    # -- sources ------------------------------------------------------------

    def play_capture(self, capture, sid, offset=None):
        """Play a DVB capture, starting at the live end or at a byte offset."""
        debuglog.write("Player", f"play adapter {getattr(capture, 'adapter', '?')} sid {sid}"
                                 f"{'' if offset is None else f' from byte {offset}'}")
        self._cancel_reconnect()
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
        if getattr(capture, "meta_titles", False):
            self._start_meta(capture, offset)
        pb.set_property("uri", "appsrc://")
        self.paused = False
        pb.set_state(Gst.State.PLAYING)

    def play_uri(self, uri, live=False, rendition=None):
        """Play a file or an address; *live*: a channel's stream, opened again
        when its connection breaks (files and archives play to their end).
        *rendition*: (EXT-X-MEDIA TYPE, NAME) of an HLS audio or subtitle
        rendition to pick from the start (opens the stream with playbin3)."""
        debuglog.write("Player", f"play {'live stream' if live else 'file / address'} {uri} "
                                 f"(engine {self.settings.get('stream_engine', 'auto')})")
        self._cancel_reconnect()
        self._retry = Retry() if live else None
        self.capture, self.sid = None, 0
        if "://" not in uri:
            uri = Gst.filename_to_uri(uri)
        if uri != self.uri:
            self._reset_metadata()
        if rendition:
            self._want_rendition = tuple(rendition)
        self.uri = uri
        self._pending_seek = None
        self._token += 1
        self._fallback_done = False
        if _is_hls_url(uri) and uri not in self._hls_alt_audio:
            # look at the master playlist first (in the background): its
            # audio renditions pick the pipeline, and a playlist the server
            # sends compressed is handed to GStreamer unpacked
            self._teardown()
            threading.Thread(target=self._probe_hls, args=(uri, self._token),
                             daemon=True).start()
        else:
            self._start_uri(self._engine_pb3(uri))

    def _engine_pb3(self, uri):
        """True to open *uri* with playbin3."""
        engine = self.settings.get("stream_engine", "auto")
        if not _has_playbin3() or engine not in ("auto", "playbin3"):
            return False
        if engine == "playbin3":
            return True
        # only playbin3 (hlsdemux2) plays HLS subtitle renditions
        return bool(_is_hls_url(uri) and (self._hls_alt_audio.get(uri) or self._want_rendition))

    def _probe_hls(self, uri, token):
        media = []
        try:
            text, unpacked, why = _fetch_text(uri)
            alt = hls_has_alternate_audio(text)
            media = parse_hls(text).get("media", [])
        except (OSError, ValueError):
            alt, unpacked, why = False, None, ""
        GLib.idle_add(self._probe_done, uri, token, alt, unpacked, media, why)

    def _probe_done(self, uri, token, alt, unpacked=None, media=None, why=""):
        self._hls_alt_audio[uri] = alt
        if media is not None:
            self.renditions[uri] = media
        if unpacked:
            debuglog.write("Player", f"{why}; handing GStreamer the playlist itself")
            self._hls_unpacked[uri] = unpacked
        else:
            self._hls_unpacked.pop(uri, None)
        if token == self._token and self.uri == uri and not self.capture:
            self._start_uri(self._engine_pb3(uri))
        return False

    def _start_uri(self, pb3):
        uri = self._hls_unpacked.get(self.uri) or self.uri
        debuglog.write("Player", f"open with {'playbin3' if pb3 else 'playbin'}")
        real, self._ssm_source = ssm_rtp_to_udp(uri) if uri.startswith(("rtp://", "udp://")) \
            else (uri, None)
        pb = self._build(pb3)
        pb.set_property("uri", real)
        self.paused = False
        pb.set_state(Gst.State.PLAYING)

    # -- a refused stream: ask the CDN behind the gateway ------------------
    def _on_refused(self, err, debug=""):
        """401, 403 or 451 for an internet stream.

        The first refusal of a playback is answered by resolving the address
        (a DVB-I gateway redirects to the broadcaster's CDN) and playing what
        it points at. Until that answer comes, the errors that follow from the
        refusal ("Internal data stream error", "Can't typefind stream") are
        kept out of the way, so the message the user sees names the refusal
        and the host that sent it, not the noise after it.
        """
        if self.capture or not self.uri:
            return False
        if http_status(err, debug) not in REFUSED_CODES:
            return False
        if not self.uri.startswith(("http://", "https://")):
            return False
        text = describe_error(err, debug)
        if self._resolve_token != self._token:
            self._resolve_token = self._token
            self._error_token = self._token         # keep the follow-up errors quiet
            hint = redirect_target(debug)
            debuglog.write("Player", f"refused ({text}); resolving {self.uri}"
                                     + (f" (redirect to {hint})" if hint else ""))
            threading.Thread(target=self._resolve_worker, args=(self.uri, self._token, text),
                             daemon=True).start()
            return True
        # the address behind the gateway refuses us too: say so, once
        if self._refused_token != self._token:
            self._refused_token = self._token
            self.emit("error", text)
        return True

    def _resolve_worker(self, uri, token, text):
        trace = []
        try:
            target = resolve_redirects(uri, trace=trace)
        except OSError as e:
            debuglog.write("Player", f"resolving {uri} failed: {e}")
            target = uri
        if trace:
            debuglog.write("Player", f"resolve {uri}: " + "; ".join(trace))
        where = host_addresses(target)
        if where:
            debuglog.write("Player", f"addresses of {where}")
        GLib.idle_add(self._resolved, token, uri, target, text)

    def _resolved(self, token, uri, target, text):
        if token != self._token or self.uri != uri or self.capture:
            return False
        if target == uri:
            self._refused_token = self._token
            self.emit("error", text)
            return False
        debuglog.write("Player", f"playing the resolved address {target}")
        self.uri = target
        self._played = False
        self._fallback_done = False
        self._start_uri(self._pb3)
        return False

    def _fallback_to_playbin(self, token):
        if token == self._token and self.uri and not self.capture:
            self._hls_alt_audio[self.uri] = False
            self._start_uri(False)
        return False

    def _video_output_failed(self, msg, err, dbg):
        """An error of the GPU video output on trial (VideoBin.trial): from
        inside it, or a picture it refused (not-negotiated). The rest of the
        session shows the picture through the appsink, as before; playback
        goes on. True when the error is handled."""
        if self._video_error_token == self._token:
            return True             # more errors of the pipeline being replaced
        if not trial_output_error(self.video, msg, dbg):
            return False
        self._video_error_token = self._token
        print(f"Video output through the graphics card failed: {err.message} ({dbg}); "
              f"copying the frames again")
        debuglog.write("Player", "video output through the graphics card failed, "
                                 "back to copying the frames")
        gl_output_failed()
        self.video = VideoBin(self.video.picture, False)
        GLib.idle_add(self._video_fallback, self._token)
        return True

    def _video_fallback(self, token):
        if token == self._token and (self.capture or self.uri):
            self.restart()
            self.emit("notice", _("Video output through the graphics card failed; "
                                  "the picture is shown the usual way again."))
        return False

    def _hw_fallback(self, token, gpu):
        if token == self._token and (self.capture or self.uri):
            self.restart()
            self.emit("hw-fallback", hwaccel.NAMES.get(gpu, gpu))
        return False

    # -- reconnecting -------------------------------------------------------
    def _network_stream(self):
        return bool(self.uri and not self.capture and
                    self.uri.split("://", 1)[0].lower() in ("http", "https", "udp", "rtp"))

    def _live_stream(self):
        """No duration: a live stream (an ending file plays to its end)."""
        if not self.playbin:
            return True
        ok, dur = self.playbin.query_duration(Gst.Format.TIME)
        return not ok or dur <= 0

    def _try_reconnect(self, reason):
        """The connection of a played internet stream broke: open it again
        after a while (lindvb.ipstream.Retry). False when it is not tried."""
        if not self.settings.get("stream_reconnect", True) or not self._network_stream() or \
                self._retry is None or not (self._played or self._reconnecting):
            return False
        if self._reconnect_timer:
            return True                  # more errors of the same break
        delay = self._retry.broken()
        if delay is None:
            self._reconnecting = False
            return False
        self._reconnecting = True
        token = self._token
        self._reconnect_timer = GLib.timeout_add(int(delay * 1000), self._reconnect, token)
        debuglog.write("Player", f"connection broke ({reason}), reconnecting in {delay:.0f} s")
        self.emit("reconnecting", int(delay), reason)
        return True

    def _reconnect(self, token):
        self._reconnect_timer = 0
        if token == self._token and self.uri and not self.capture:
            self._start_uri(self._pb3)
        return False

    def _cancel_reconnect(self):
        if self._reconnect_timer:
            GLib.source_remove(self._reconnect_timer)
            self._reconnect_timer = 0
        self._reconnecting = False

    def set_audio_delay(self, ms):
        """Shift the sound against the picture (ms, + = sound later)."""
        self.audio_delay_ms = int(ms)
        if self.playbin:
            self.playbin.set_property("av-offset", -self.audio_delay_ms * Gst.MSECOND)

    def stop(self):
        debuglog.write("Player", "stop")
        self._cancel_reconnect()
        self._token += 1
        self._teardown()
        self.capture = None
        self._reset_metadata()
        self.emit("state", "stopped")

    # -- transport ----------------------------------------------------------

    def can_timeshift(self):
        """False for live TV whose capture has no timeshift buffer (off)."""
        return not self.capture or getattr(self.capture, "timeshift", True)

    def toggle_pause(self):
        if not self.playbin or not self.can_timeshift():
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
        if self.capture and not self.can_timeshift():
            return
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

    def _tracks_changed(self):
        """Tags arrive from streaming threads; tell the UI once, on the main loop."""
        if not self._tracks_pending:
            self._tracks_pending = True

            def fire():
                self._tracks_pending = False
                self._apply_audio_preference()
                self.emit("tracks")
                self._shadow_catalog_later()
                return False
            GLib.idle_add(fire)

    # -- track catalog in shadow mode (stage D) ------------------------------------

    def _shadow_catalog_later(self):
        """Debug mode: compare the track catalog with the menus once the
        tracks have settled (3 s after the last change)."""
        if not debuglog.enabled():
            return
        if self._catalog_timer:
            GLib.source_remove(self._catalog_timer)
        self._catalog_timer = GLib.timeout_add(3000, self._shadow_catalog, self._token)

    def _shadow_catalog(self, token):
        """Write the catalog of what plays to the debug log, and where it
        differs from the audio and subtitle menus. Only on a change."""
        self._catalog_timer = 0
        if token != self._token or not self.playbin or not debuglog.enabled():
            return False
        try:
            catalog = trackcatalog.catalog_for(self._pmt_raw, self._media(), rendition_for_pid,
                                               buffered=True if self.capture else None)
            tracks, _cur = self.audio_track_info()
            kinds = [kind for kind, _key, _label in self._subtitle_entries()]
            diff = trackcatalog.compare(catalog, tracks, kinds, is_audio_description)
        except (ValueError, IndexError) as e:
            debuglog.write(_TRACKS_LOG, f"catalog not built: {e}")
            return False
        said = (tuple(t.id for t in catalog), tuple(diff))
        if catalog and said != self._catalog_logged:
            self._catalog_logged = said
            debuglog.write(_TRACKS_LOG, "catalog: " + "; ".join(
                f"{t.id} = {trackcatalog.label(t, language_name)}"
                + ("" if t.in_buffer else " (not in the buffer)") for t in catalog))
            for line in diff:
                debuglog.write(_TRACKS_LOG, "the menu differs from the catalog: " + line)
        return False

    def _pb3_tracks_changed(self):
        """playbin3 has no audio-tags-changed signal: the stream collection,
        the selection and tags tell when the sound can be chosen."""
        if not self._audio_pref_done:
            self._tracks_changed()

    def _apply_audio_preference(self):
        """Once per channel or file: switch to the preferred audio format
        (Preferences › Playback) as soon as the tracks' codecs are known."""
        if self._audio_pref_done or not self.playbin:
            return
        tracks, cur = self.audio_track_info()
        if not tracks or not any(t.codec for t in tracks):
            return                          # the codecs come with later tags
        if cur < 0 and getattr(self, "_pb3", False):
            return                          # playbin3 has not chosen yet: it would undo the choice
        renditions = self._media("AUDIO")
        default = next((m.get("name") for m in renditions if m.get("default")), "")
        if default and len(tracks) < len(renditions) and \
                not any(t.title == default for t in tracks):
            return                          # the playlist's default track is still to come
        self._audio_pref_done = True
        idx = self._starting_track(tracks, cur, default)
        self._auto_audio = cur if idx is None else idx
        if idx is not None:
            self.select_audio(idx)

    def _starting_track(self, tracks, cur, default):
        """The track a channel or file starts with (None: the current one):
        the playlist's default, then the preferred language and audio
        description choice, then the preferred format."""
        start = cur
        if default and not (0 <= cur < len(tracks) and tracks[cur].title == default):
            # the timeshift buffer lists HLS audio renditions by PID: start
            # with the one the playlist marks as default, not the first PID
            start = next((i for i, t in enumerate(tracks) if t.title == default), cur)
        s = self.settings
        lang = language_track(
            tracks, start,
            playback_language(s.get("audio_language", AUDIO_LANGUAGE_AUTO), i18n.current),
            bool(s.get("audio_description_prefer", False)))
        if lang is not None:
            start = lang
        idx = preferred_track(tracks, s.get("audio_prefer", "off"), start)
        if idx is None and start != cur:
            idx = start
        return idx

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
        return without_variant_sound(out)

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
                t.codec = _codec_from_caps(st)
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

    def _media(self, kind=None):
        """The HLS renditions of what plays (EXT-X-MEDIA), optionally of one
        TYPE ("AUDIO", "SUBTITLES", "CLOSED-CAPTIONS")."""
        url = self.uri or getattr(getattr(self.capture, "channel", None), "url", "") or ""
        media = self.renditions.get(url, [])
        return [m for m in media if not kind or m.get("type") == kind]

    @staticmethod
    def _stream_name(stream_id, kind):
        """The rendition name hlsdemux2 puts at the end of its stream ids
        (".../audio-Klare Sprache", ".../text-Deutsch"), or ""."""
        return hls_stream_name(stream_id, kind)

    def audio_track_info(self):
        """([AudioTrack], index of the current one or -1)."""
        if not self.playbin:
            return [], -1
        tracks, cur = [], -1
        if self._pb3:
            for i, st in enumerate(self._streams(Gst.StreamType.AUDIO)):
                t = self._track_from(st.get_tags(), st.get_caps(), Gst.TAG_AUDIO_CODEC)
                if not t.title:
                    t.title = self._stream_name(st.get_stream_id(), "audio")
                m = next((m for m in self._media("AUDIO") if m.get("name") == t.title), None)
                t.description = _describes_video(m) or \
                    _pid_of(st.get_stream_id()) in self._ad_pids
                tracks.append(t)
                if st.get_stream_id() in self._selected:
                    cur = i
        else:
            audio = self._media("AUDIO")
            for i in range(self.playbin.get_property("n-audio")):
                pad = self.playbin.emit("get-audio-pad", i)
                caps = pad.get_current_caps() if pad else None
                t = self._track_from(self.playbin.emit("get-audio-tags", i), caps,
                                     Gst.TAG_AUDIO_CODEC)
                if audio and pad is not None and not t.title:
                    # an HLS audio rendition the timeshift reader put on a PID
                    # of its own (lindvb.ipstream.rendition_pid): its name
                    m = rendition_for_pid(audio, _pid_of(pad.get_stream_id()))
                    if m is not None:
                        t.title = m.get("name", "")
                        t.description = _describes_video(m)
                if pad is not None and _pid_of(pad.get_stream_id()) in self._ad_pids:
                    t.description = True
                tracks.append(t)
            cur = self.playbin.get_property("current-audio") if tracks else -1
        if 0 <= cur < len(tracks) and not tracks[cur].channels:
            tracks[cur].channels = self._decoded_channels()
        return tracks, cur

    def _text_streams(self):
        """[(key, label)] of the text and DVB subtitle streams: the key is the
        stream id (playbin3) or the index (playbin)."""
        if not self.playbin:
            return []
        if self._pb3:
            streams = self._streams(Gst.StreamType.TEXT)
            items = [(st.get_stream_id(), st.get_tags(),
                      self._stream_name(st.get_stream_id(), "text")) for st in streams]
        else:
            items = [(i, self.playbin.emit("get-text-tags", i), "")
                     for i in range(self.playbin.get_property("n-text"))]
        out = []
        for i, (key, tags, name) in enumerate(items):
            label = name or f"Subtitles {i + 1}"
            if tags:
                ok, lang = tags.get_string(Gst.TAG_LANGUAGE_CODE)
                ok2, codec = tags.get_string(Gst.TAG_SUBTITLE_CODEC)
                lname = language_name(lang) if ok else None
                if lname and not name:
                    label = lname
                if ok2 and codec:
                    label += f" ({codec})"
            out.append((key, label))
        return out

    def _text_labels(self):
        """Labels of the text streams and the index of the one shown (-1 = none)."""
        streams = self._text_streams()
        keys = [k for k, _l in streams]
        cur = -1
        if self._pb3:
            cur = next((i for i, k in enumerate(keys) if k in self._selected), -1)
        elif self.playbin and self.playbin.get_property("flags") & PLAY_FLAGS_TEXT and keys:
            cur = self.playbin.get_property("current-text")
        return [lab for _k, lab in streams], cur

    def caption_label(self, service):
        """'English (CC1)': the closed caption service with the name and language
        the HLS playlist (CLOSED-CAPTIONS) or the PMT (caption_service_descriptor)
        gives for it."""
        hls = next((m for m in self._media("CLOSED-CAPTIONS")
                    if captions.instream_service(m.get("instream_id")) == service), None)
        iid = (hls or {}).get("instream_id", "").upper()
        tag = iid if iid.startswith("CC") else f"CC{service}" if service == 1 else \
            _("service {n}").format(n=service)
        name = (hls or {}).get("name", "") or \
            language_name((hls or {}).get("language", "")) or \
            language_name(self._cc_pmt.get(service, "") or self._cc_pmt.get(-service, ""))
        return f"{name} ({tag})" if name else _("Closed captions ({tag})").format(tag=tag)

    def _subtitle_entries(self):
        """[(kind, key, label)]: text/DVB subtitle streams, teletext subtitle
        pages and the closed caption services of the video."""
        out = [("text", k, lab) for k, lab in self._text_streams()]
        out += [("ttx", p.page, p.label) for p in self.teletext_pages()]
        if self.cc is not None and self.cc.present:
            out += [("cc", svc, self.caption_label(svc)) for svc in self.cc.services]
        return out

    def audio_tracks(self):
        """Readable labels ('German, Stereo', ...) and the index of the current track."""
        tracks, cur = self.audio_track_info()
        return audio_track_labels(tracks), cur

    def subtitle_tracks(self):
        """Labels of the subtitle streams, teletext subtitle pages and closed
        caption services, and the index of the one shown (-1 = off)."""
        entries = self._subtitle_entries()
        labels = [lab for _k, _key, lab in entries]
        cur = -1
        if self.ttx_page:
            cur = next((i for i, e in enumerate(entries) if e[:2] == ("ttx", self.ttx_page)), -1)
        elif self.cc is not None and self.cc.service:
            cur = next((i for i, e in enumerate(entries) if e[:2] == ("cc", self.cc.service)), -1)
        else:
            _labels, text_cur = self._text_labels()
            cur = text_cur
        return labels, cur

    def has_closed_captions(self):
        """The video of the current stream carries CEA-708 captions."""
        return self.cc is not None and self.cc.present

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

    def _text_off(self):
        if not self.playbin:
            return
        self.playbin.set_property("flags", self.playbin.get_property("flags") & ~PLAY_FLAGS_TEXT)

    def select_subtitle(self, i):
        """Show entry *i* of subtitle_tracks(); -1 (Off) switches all of them off:
        text and DVB subtitles, teletext subtitles and closed captions."""
        if not self.playbin:
            return
        entries = self._subtitle_entries()
        kind, key = entries[i][:2] if 0 <= i < len(entries) else (None, None)
        if self._pb3 and self.uri and not self.capture:
            # playbin3 (1.24) hangs or crashes when its subtitle stream changes
            # while it plays: open the stream again with the new choice
            # (_on_sync_collection makes it before anything starts)
            shown = next((st.get_stream_id() for st in self._streams(Gst.StreamType.TEXT)
                          if st.get_stream_id() in self._selected), None)
            if (key if kind == "text" else None) != shown:
                self._want_rendition = ("SUBTITLES", self._stream_name(key, "text")) \
                    if kind == "text" else None
                self._sub_choice = (kind, key) if kind in ("cc", "ttx") else None
                if self.cc is not None:
                    self.cc.show(key if kind == "cc" else 0)
                self.ttx_page = key if kind == "ttx" else 0
                self._ttx_last = ""
                self.emit("subtitle-text", "")
                debuglog.write("Player", "subtitles changed: opening the stream again")
                self._start_uri(True)
                return
        # whatever showed so far goes off
        self.ttx_page = 0
        self._ttx_last = ""
        self.emit("subtitle-text", "")
        self._sub_choice = (kind, key) if kind else None
        if self.cc is not None and kind != "cc":
            self.cc.show(0)
        if kind != "text":
            self._text_off()
        if kind == "ttx":
            self.ttx_page = key
            if self._ttx_dec is not None:
                self._ttx_dec.set_property("page", key)
        elif kind == "cc":
            self.cc.show(key)
        elif kind == "text":
            shown = self._text_shown()
            self.playbin.set_property("flags", self.playbin.get_property("flags") | PLAY_FLAGS_TEXT)
            if self._pb3:
                st = next((st for st in self._streams(Gst.StreamType.TEXT)
                           if st.get_stream_id() == key), None)
                if st is not None and key not in self._selected:
                    self._select_streams(Gst.StreamType.TEXT, st)
            else:
                self.playbin.set_property("current-text", key)
                self._fetch_subtitles_again(shown, key)
        self._ttx_shadow_follow(kind, key)
        self._cue_shadow_follow(kind)
        self.emit("tracks")

    def _text_shown(self):
        """Classic playbin: the text stream shown (current-text), None when
        text subtitles are off."""
        if not self.playbin.get_property("flags") & PLAY_FLAGS_TEXT:
            return None
        return self.playbin.get_property("current-text")

    def _fetch_subtitles_again(self, shown, key):
        """Classic playbin with DASH, text stream *key* picked where *shown*
        was (None: off): subtitle data that came while it was not shown is
        gone, and TTML that comes as one file for the whole stream never
        comes again. A seek to where the picture is makes dashdemux fetch it
        again.

        The seek waits until the subtitle overlay stands: switching
        subtitles on makes playsink build it (subtitleoverlay plugs a parser
        and a renderer, the pipeline prerolls again), and a flushing seek in
        the middle of that left the playback standing in preroll now and
        then (1 in 8 in the tests; never with the seek after it)."""
        if key == shown or not self._dash or self.capture or self.uri is None:
            return
        pb = self.playbin
        if self._subtitle_overlay_ready(pb):
            self._seek_for_subtitles(pb)
            return
        GLib.timeout_add(SUBTITLE_OVERLAY_POLL_MS, self._fetch_when_overlay_ready, pb,
                         time.monotonic() + SUBTITLE_OVERLAY_WAIT_S)

    def _fetch_when_overlay_ready(self, pb, deadline):
        """Main loop: seek once the overlay stands; nothing when the stream or
        the subtitles changed meanwhile."""
        if pb is not self.playbin or not pb.get_property("flags") & PLAY_FLAGS_TEXT:
            return False
        if self._subtitle_overlay_ready(pb):
            self._seek_for_subtitles(pb)
            return False
        if time.monotonic() < deadline:
            return True
        # no data came to build it with (TTML as one file was read to its end
        # while the subtitles were off): only the seek brings it
        self._seek_for_subtitles(pb)
        return False

    @staticmethod
    def _subtitle_overlay_ready(pb):
        """playsink's subtitle overlay has its renderer and the playback is
        settled (no state change on its way)."""
        if pb.get_state(0)[0] != Gst.StateChangeReturn.SUCCESS:
            return False
        overlay = pb.get_by_name("suboverlay")
        if overlay is None:
            return False
        return any(overlay.get_by_name(n) is not None for n in ("overlay", "renderer"))

    def _seek_for_subtitles(self, pb):
        q = Gst.Query.new_seeking(Gst.Format.TIME)
        ok, pos = pb.query_position(Gst.Format.TIME)
        if not (pb.query(q) and q.parse_seeking()[1] and ok and pos >= 0):
            debuglog.write("Player", "subtitles on: the stream cannot seek, "
                                     "they come with the next segments")
            return
        debuglog.write("Player", f"subtitles on: fetching the stream again at {pos / Gst.SECOND:.1f} s "
                                 "for its subtitle data")
        pb.seek(self.rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                Gst.SeekType.SET, pos, Gst.SeekType.NONE, 0)

    def _on_sync_collection(self, _bus, msg):
        """Streaming thread: playbin3 offers its streams. Take the first video,
        the default (or first) audio rendition and a subtitle stream only when
        the user picked one."""
        f = msg.src.get_factory() if isinstance(msg.src, Gst.Element) else None
        # the adaptive demuxer (what it downloads; a subtitle track nobody
        # shows can hold picture and sound back, see lindvb.ipstream) and
        # decodebin3 (what it decodes)
        if not (msg.src is self.playbin or
                (f and f.get_name() in ("decodebin3", "hlsdemux2", "dashdemux2"))):
            return
        coll = msg.parse_stream_collection()
        streams = [coll.get_stream(i) for i in range(coll.get_size())]
        want = self._want_rendition
        want_audio = want[1] if want and want[0] == "AUDIO" else ""
        want_text = want[1] if want and want[0] == "SUBTITLES" else \
            self._stream_name(self._sub_choice[1], "text") \
            if self._sub_choice and self._sub_choice[0] == "text" else ""
        pick = []
        for kind, prefix, wanted in ((Gst.StreamType.VIDEO, "video", ""),
                                     (Gst.StreamType.AUDIO, "audio", want_audio),
                                     (Gst.StreamType.TEXT, "text", want_text)):
            of_kind = without_variant_sound([st for st in streams if st.get_stream_type() & kind])
            if not of_kind or (kind == Gst.StreamType.VIDEO and self.video_off):
                continue
            match = next((st for st in of_kind
                          if wanted and self._stream_name(st.get_stream_id(), prefix) == wanted), None)
            if kind == Gst.StreamType.TEXT:
                if match is None:
                    continue            # subtitles stay off
                if self._sub_choice != ("text", match.get_stream_id()):
                    self._sub_choice = ("text", match.get_stream_id())
                    GLib.idle_add(self._watch_subtitle_stall, self._token)
            if match is None and kind == Gst.StreamType.AUDIO and not wanted:
                match = self._preferred_audio_stream(of_kind)
            if match is None:
                # what the demuxer marks as its default (HLS DEFAULT=YES), else the first
                match = next((st for st in of_kind
                              if st.get_stream_flags() & Gst.StreamFlags.SELECT), of_kind[0])
            pick.append(match.get_stream_id())
        if pick:
            msg.src.send_event(Gst.Event.new_select_streams(pick))

    def _preferred_audio_stream(self, streams):
        """Of the audio streams playbin3 offers, the one to start with when it
        is not the demuxer's default: the preferred language, audio
        description only when it is wanted (see language_track). None keeps
        the default."""
        media = self._media("AUDIO")
        tracks, start = [], -1
        for i, st in enumerate(streams):
            t = self._track_from(st.get_tags(), None, Gst.TAG_AUDIO_CODEC)
            if not t.title:
                t.title = self._stream_name(st.get_stream_id(), "audio")
            m = next((m for m in media if m.get("name") == t.title), None)
            if m is not None and not t.language:
                t.language = m.get("language", "")
            t.description = _describes_video(m)
            tracks.append(t)
            if start < 0 and st.get_stream_flags() & Gst.StreamFlags.SELECT:
                start = i
        s = self.settings
        idx = language_track(
            tracks, max(start, 0),
            playback_language(s.get("audio_language", AUDIO_LANGUAGE_AUTO), i18n.current),
            bool(s.get("audio_description_prefer", False)))
        return streams[idx] if idx is not None else None

    def _watch_subtitle_stall(self, token, last=None, still=0.0, checks=0):
        """A subtitle rendition whose segments carry no usable time stamps
        (PBS's WebVTT) makes hlsdemux2 hold picture and sound back: when the
        picture stands still with subtitles on, open the stream again without."""
        if token != self._token or not self.playbin or not self._sub_choice or \
                self._sub_choice[0] != "text" or checks > 15:
            return False
        ok, pos = self.playbin.query_position(Gst.Format.TIME)
        if not ok or pos < 0:
            pos = -1
        if checks and not self.paused:
            moved = pos >= 0 and last is not None and last >= 0 and pos - last >= Gst.SECOND // 2
            still = 0.0 if moved else still + 2.0
        if still >= 6.0:
            debuglog.write("Player", "the picture stands still with the subtitles on: "
                                     "opening the stream again without them")
            self._sub_choice = None
            # a subtitle rendition picked from the start would be picked again
            # on the reopened stream, and stall it again
            if self._want_rendition and self._want_rendition[0] == "SUBTITLES":
                self._want_rendition = None
            self.emit("notice", _("The subtitles of this stream hold the picture back, "
                                  "so they were switched off."))
            self._start_uri(True)
            return False
        GLib.timeout_add(2000, self._watch_subtitle_stall, token, pos, still, checks + 1)
        return False

    def play_rendition(self, uri, kind, name):
        """Play the HLS stream *uri* directly with the audio or subtitle
        rendition (EXT-X-MEDIA TYPE, NAME) picked from the start."""
        self.play_uri(uri, live=True, rendition=(kind, name))

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
            return display_size(caps.get_structure(0))
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
        out["output"] = output_label(self.video.kind)
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
        """Buffering of an internet stream: dict percent, in/out rates (bytes/s), left (ms).

        An internet channel buffered for timeshift plays from its capture
        (lindvb.ipstream) through the appsrc: its buffer is what lies ready
        ahead of the playback there (capture_buffer). Adaptive streams
        (hlsdemux, hlsdemux2) answer no buffering query; the percentage of
        their last buffering message stands in for it."""
        if not self.playbin:
            return {}
        if self.capture:
            f = self.feeder
            if f is None or not self.plays_stream_capture():
                return {}
            try:
                _start, end = self.capture.store.bounds()
                level = f.appsrc.get_property("current-level-bytes")
                limit = f.appsrc.get_property("max-bytes")
            except (AttributeError, TypeError):
                return {}
            # nothing received yet: byte_rate() is only a guess (8 Mbit/s),
            # which the channel analysis would show as the stream's rate
            return capture_buffer(end, f.reader.offset, level, limit,
                                  self.capture.byte_rate() if end else None)
        q = Gst.Query.new_buffering(Gst.Format.TIME)
        if self.playbin.query(q):
            _busy, pct = q.parse_buffering_percent()
            _mode, avg_in, avg_out, left = q.parse_buffering_stats()
            return {"percent": pct, "in": avg_in, "out": avg_out, "left_ms": left}
        pct = getattr(self, "_last_buffering", None)
        if pct is None and self._played:
            pct = 100           # playing and never asked to wait: nothing missing
        return {"percent": pct} if pct is not None else {}

    def plays_stream_capture(self):
        """The capture played is an internet stream (or Tvheadend) read by LinDVB."""
        return getattr(self.capture, "_net", None) is not None

    # -- bus ----------------------------------------------------------------

    def _on_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            debuglog.write("Player", f"error from {msg.src.get_name() if msg.src else '?'}: "
                                     f"{err.message} ({_dbg})")
            if self._from_visualizer(msg.src):
                # the visualiser (e.g. one without a usable OpenGL) must not
                # stop the radio: drop it and play on
                if self._vis_error_token != self._token:
                    self._vis_error_token = self._token
                    print(f"Visualisation failed: {err.message} ({_dbg})")
                    GLib.idle_add(self._drop_visualizer, err.message)
                return
            if self._video_output_failed(msg, err, _dbg):
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
            if self._pb3 and self.uri and not self.capture and not self._fallback_done and \
                    not self._played and not self._reconnecting:
                # e.g. hlsdemux2 refusing a stream: try the classic pipeline once
                self._fallback_done = True
                GLib.idle_add(self._fallback_to_playbin, self._token)
                return
            if self._empty_start(msg.src, _dbg):
                return
            if self._on_refused(err, _dbg):
                return
            if self._try_reconnect(describe_error(err, _dbg)):
                return
            # one error per playback: the first one names the cause, the ones
            # after it ("not enough data", "can't typefind") only follow from it
            if self.uri and not self.capture:
                if self._error_token == self._token:
                    return
                self._error_token = self._token
            self.emit("error", describe_error(err, _dbg))
        elif t == Gst.MessageType.EOS:
            debuglog.write("Player", "end of stream")
            if self._network_stream() and self._live_stream() and \
                    self._try_reconnect(_("the server ended the stream")):
                return
            self.emit("eos")
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.playbin:
            _old, new, _p = msg.parse_state_changed()
            if new == Gst.State.PLAYING:
                debuglog.write("Player", "playing")
                self._played = True
                if self._retry is not None:
                    self._retry.running()
                if self._reconnecting:
                    self._reconnecting = False
                    self.emit("reconnected")
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
                self._pb3_tracks_changed()
            self.emit("tracks")
        elif t == Gst.MessageType.STREAM_COLLECTION:
            if self._pb3:
                f = msg.src.get_factory() if isinstance(msg.src, Gst.Element) else None
                if msg.src is self.playbin or (f and f.get_name() in ("decodebin3", "uridecodebin3")):
                    self._collection = msg.parse_stream_collection()
                self._pb3_tracks_changed()
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
            if self._pb3:
                self._pb3_tracks_changed()      # a codec may now be known
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
