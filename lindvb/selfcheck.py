# SPDX-License-Identifier: GPL-2.0-or-later
"""``lindvb --self-check``: is this installation of LinDVB able to play TV?

Every package (AppImage, Flatpak, .deb, Snap) runs this in CI before it is
released. It starts GTK and lays out text (this is where a wrong HarfBuzz or
Pango shows up), looks for the GStreamer elements live TV needs and plays a
short MPEG-TS sample (H.264 video, MPEG-1 Layer II audio, as on DVB) through
playbin into fake sinks, so the decoders are not only installed but work.

Then it does what an IPTV channel does: it checks that GIO has a TLS backend
and plays a public HTTPS HLS stream (H.264 and AAC) until enough frames are
decoded. Last it takes the way LinDVB really plays such a channel: the
stream is read into a transport stream (lindvb.ipstream, as for the
timeshift buffer) and that is played back. ``--offline`` leaves the network
checks out.

Decoded sound is measured too: a test stream whose sound comes out silent
fails, so a decoder or remuxer that only produces buffers of silence is
noticed, not just one that produces none.

It needs a display (CI uses xvfb-run) but no sound server or tuner. The exit
status is 0 when everything works and 1 otherwise; WARN lines do not fail it.
"""

import os
import sys
import time
from pathlib import Path

SAMPLE = Path(__file__).with_name("selfcheck.ts")
# 2 s at 25 fps; a few frames may be dropped at the start of a stream
MIN_VIDEO_FRAMES = 40
TIMEOUT = 60

# Elements live TV needs beyond compat.PLUGINS
ELEMENTS = ["h264parse", "mpegaudioparse", "aacparse", "videoconvert", "audioconvert",
            "decodebin3",
            "level",           # loudness meter (gst-plugins-good)
            "capssetter"]      # AAC and subtitles in MP4/MKV recordings (good)
# Closed captions (CEA-608/708, gst-plugins-bad). The packages that bring their
# own GStreamer must have it; GStreamer 1.28 of the distributions (e.g. Arch)
# no longer has it, so there a missing overlay is only a warning.
CC_OVERLAY = "cc708overlay"
# cc708overlay draws only what ccextractor takes off the frames (lindvb.captions)
CC_ELEMENTS = (CC_OVERLAY, "ccextractor", "ccconverter")
# Elements without which LinDVB still plays, but worse: missing, they are a
# warning, with what the user loses ...
OPTIONAL_ELEMENTS = {
    # playbin draws text subtitles (WebVTT, TTML, SRT) with it
    # (gst-plugins-base "pango", Debian/Ubuntu package gstreamer1.0-x)
    "textoverlay": "text subtitles (WebVTT, TTML, SRT) are not shown",
    # the picture goes to GTK without a copy of each frame in Python
    # (gst-plugins-rs "gtk4", package gstreamer1.0-gtk4 / gst-plugin-gtk4;
    # Ubuntu 24.04, which the Snap and the AppImage are built from, has none)
    "gtk4paintablesink": "each video frame is copied on its way to the screen (more CPU)",
}
# ... except in the packages that bring these themselves: there a missing one
# is a packaging mistake and fails the check
REQUIRED_WHEN_BUNDLED = {"textoverlay": ("Snap", "AppImage")}
# Audio output: one of these must exist; it only plays when a sound server runs
AUDIO_SINKS = ["pulsesink", "pipewiresink", "alsasink"]

# Public HTTPS HLS test streams (H.264 + AAC in MPEG-TS segments, like most
# IPTV). One of them has to play; LINDVB_SELFCHECK_STREAM replaces the list.
STREAMS = [
    "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
    "https://devstreaming-cdn.apple.com/videos/streaming/examples/bipbop_4x3/bipbop_4x3_variant.m3u8",
]
STREAM_FRAMES = 50
STREAM_TIMEOUT = 45
# the IPTV path: seconds of stream read into the transport stream
IPTV_SECONDS = 8
# decoded sound quieter than this (peak, dBFS) counts as silence; the test
# streams have tones and speech far above it
SILENCE_DB = -60.0


class Report:
    def __init__(self, out):
        self.out = out
        self.failed = False

    def ok(self, what):
        self.out.write(f"OK    {what}\n")

    def fail(self, what):
        self.failed = True
        self.out.write(f"FAIL  {what}\n")

    def warn(self, what):
        self.out.write(f"WARN  {what}\n")


def check_gtk(report):
    from gi.repository import GLib, Gtk
    version = f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
    if not Gtk.init_check():
        report.fail(f"GTK {version}: no display (run under xvfb-run)")
        return
    win = Gtk.Window(title="LinDVB self-check")
    label = Gtk.Label(label="LinDVB · ÄÖÜ · 中文 · العربية")
    win.set_child(label)
    win.present()
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and label.get_allocated_width() <= 0:
        ctx.iteration(False)
        time.sleep(0.01)
    width = label.get_allocated_width()
    win.destroy()
    if width > 0:
        report.ok(f"GTK {version}: window shown, text laid out ({width} px)")
    else:
        report.fail(f"GTK {version}: the window was not laid out")


def check_elements(report):
    from gi.repository import Gst
    from . import compat
    for name in list(compat.PLUGINS) + ELEMENTS:
        if Gst.ElementFactory.find(name):
            report.ok(f"GStreamer element {name}")
        else:
            report.fail(f"GStreamer element {name} is missing")
    missing = [e for e in CC_ELEMENTS if not Gst.ElementFactory.find(e)]
    if not missing:
        report.ok("GStreamer elements " + ", ".join(CC_ELEMENTS))
    elif compat.bundled():
        report.fail("GStreamer element " + ", ".join(missing) + " is missing")
    else:
        report.warn(f"closed captions unavailable: GStreamer {Gst.version_string().split()[-1]} "
                    f"has no " + ", ".join(missing))
    for name, without in OPTIONAL_ELEMENTS.items():
        if Gst.ElementFactory.find(name):
            report.ok(f"GStreamer element {name}")
        elif compat.bundled() in REQUIRED_WHEN_BUNDLED.get(name, ()):
            report.fail(f"GStreamer element {name} is missing: {without}")
        else:
            report.warn(f"GStreamer element {name} is missing: {without}")
    sinks = [s for s in AUDIO_SINKS if Gst.ElementFactory.find(s)]
    if sinks:
        report.ok("audio output: " + ", ".join(sinks))
    else:
        report.fail("no audio output element (" + ", ".join(AUDIO_SINKS) + ")")


def _factories(bin_):
    from gi.repository import Gst
    names = []
    it = bin_.iterate_recurse()
    while True:
        res, el = it.next()
        if res != Gst.IteratorResult.OK:
            break
        f = el.get_factory()
        if f:
            names.append(f.get_name())
    return names


def _play(uri, enough=None, timeout=TIMEOUT):
    """Play ``uri`` through playbin into fake sinks.

    Stops at the end of the stream or, with ``enough``, once that many video
    frames and some audio were decoded. Returns (error or None, counts, decoders).
    """
    from gi.repository import GLib, Gst
    counts = {"video": 0, "audio": 0, "peak": 0}

    def sink(kind):
        s = Gst.ElementFactory.make("fakesink", f"{kind}sink")
        s.set_property("sync", False)
        s.set_property("signal-handoffs", True)
        s.connect("handoff", lambda *a: counts.__setitem__(kind, counts[kind] + 1))
        return s

    def audio_sink():
        """The sound as 16-bit samples into a fakesink that keeps the peak."""
        conv = Gst.ElementFactory.make("audioconvert")
        caps = Gst.ElementFactory.make("capsfilter")
        if conv is None or caps is None:
            return sink("audio")
        caps.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=S16LE"))
        s = sink("audio")
        s.connect("handoff", lambda _s, buf, _p: counts.__setitem__(
            "peak", max(counts["peak"], _peak(buf))))
        b = Gst.Bin.new("audiocheck")
        for e in (conv, caps, s):
            b.add(e)
        conv.link(caps)
        caps.link(s)
        b.add_pad(Gst.GhostPad.new("sink", conv.get_static_pad("sink")))
        return b

    play = Gst.ElementFactory.make("playbin", None)
    if play is None:
        return "playbin cannot be created", counts, []
    play.set_property("uri", uri)
    play.set_property("video-sink", sink("video"))
    play.set_property("audio-sink", audio_sink())
    bus = play.get_bus()
    play.set_state(Gst.State.PLAYING)
    error = None
    deadline = time.monotonic() + timeout
    while True:
        if enough and counts["video"] >= enough and counts["audio"] > 0:
            break
        left = deadline - time.monotonic()
        if left <= 0:
            error = f"not enough decoded within {timeout} s"
            break
        msg = bus.timed_pop_filtered(int(min(left, 0.2) * Gst.SECOND),
                                     Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if msg is None:
            continue
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            error = f"{err.message} ({dbg})" if dbg else err.message
        break
    used = [n for n in _factories(play) if "dec" in n and "decodebin" not in n]
    play.set_state(Gst.State.NULL)
    GLib.MainContext.default().iteration(False)
    return error, counts, list(dict.fromkeys(used))


def _peak(buf):
    """The highest sample of a buffer of 16-bit samples."""
    import array
    data = array.array("h", buf.extract_dup(0, buf.get_size() & ~1))
    return max(max(data, default=0), -min(data, default=0))


def peak_db(counts):
    """The peak of the decoded sound in dBFS (-inf for digital silence)."""
    import math
    p = counts.get("peak", 0)
    return 20 * math.log10(p / 32768) if p else float("-inf")


def silent(counts):
    return counts["audio"] > 0 and peak_db(counts) < SILENCE_DB


def _detail(counts, used):
    detail = f"{counts['video']} video frames, {counts['audio']} audio buffers"
    if counts["audio"]:
        detail += f" (peak {peak_db(counts):.0f} dBFS)"
    if used:
        detail += ", decoders: " + ", ".join(used)
    return detail


def check_playback(report, sample=SAMPLE):
    if not sample.is_file():
        report.fail(f"test stream {sample} is missing")
        return
    error, counts, used = _play(sample.resolve().as_uri())
    detail = _detail(counts, used)
    if error:
        report.fail(f"playing the H.264 test stream: {error}; {detail}")
    elif counts["video"] < MIN_VIDEO_FRAMES or counts["audio"] == 0:
        report.fail(f"playing the H.264 test stream: too little decoded; {detail}")
    elif silent(counts):
        report.fail(f"playing the H.264 test stream: the sound is silent; {detail}")
    else:
        report.ok(f"played the H.264/MPEG audio test stream: {detail}")


def check_tls(report):
    """GIO needs a TLS backend (glib-networking) for souphttpsrc and HTTPS."""
    from gi.repository import Gio
    backend = Gio.TlsBackend.get_default()
    if backend.supports_tls():
        report.ok("GIO TLS backend: " + type(backend).__gtype__.name)
    else:
        extra = os.environ.get("GIO_EXTRA_MODULES", "")
        report.fail("GIO has no TLS backend (glib-networking): every https:// stream fails"
                    f" (GIO_MODULE_DIR={os.environ.get('GIO_MODULE_DIR', '')!r},"
                    f" GIO_EXTRA_MODULES={extra!r})")


def check_proxy(report, uri="https://example.com/"):
    """Every HTTP request first asks GIO's proxy resolver. If that fails
    (in a snap: the desktop portal refusing the question), every stream fails
    at once with souphttpsrc's bare "Internal data stream error"."""
    from gi.repository import Gio, GLib
    from . import compat
    resolver = Gio.ProxyResolver.get_default()
    name = type(resolver).__gtype__.name
    if compat.bundled() == "Snap" and "Portal" in name:
        report.fail(f"proxy resolver {name}: the desktop portal refuses it to snaps without"
                    " network-status; GIO_USE_PROXY_RESOLVER is not in effect")
        return
    try:
        proxies = resolver.lookup(uri, None)
    except GLib.Error as e:
        report.fail(f"proxy resolver {name} fails, so every stream fails: {e.message}")
        return
    report.ok(f"proxy resolver {name}: {', '.join(proxies)}")


def check_stream(report, streams=None):
    """Play an HTTPS HLS stream the way an IPTV channel plays."""
    env = os.environ.get("LINDVB_SELFCHECK_STREAM")
    streams = streams or ([env] if env else STREAMS)
    problems = []
    for uri in streams:
        error, counts, used = _play(uri, enough=STREAM_FRAMES, timeout=STREAM_TIMEOUT)
        if not error and silent(counts):
            error = "the sound is silent"
        if not error:
            report.ok(f"played the HTTPS HLS stream {uri}: {_detail(counts, used)}")
            return
        problems.append(f"{uri}: {error}; {_detail(counts, used)}")
    report.fail("no HTTPS HLS stream played: " + " | ".join(problems))


def read_stream(uri, seconds=IPTV_SECONDS, timeout=STREAM_TIMEOUT):
    """(transport stream bytes, error or None, reader): *uri* read the way an
    IPTV channel is read into the timeshift buffer (lindvb.ipstream)."""
    from gi.repository import GLib
    from . import ipstream
    got, errors = [], []
    reader = ipstream.StreamSource(uri, got.append, errors.append, reconnect=False)
    loop = GLib.MainLoop()
    start = time.monotonic()
    first = []

    def tick():
        now = time.monotonic()
        if got and not first:
            first.append(now)
        if errors or (first and now - first[0] >= seconds) or now - start > timeout:
            loop.quit()
            return False
        return True
    GLib.timeout_add(100, tick)
    reader.start()
    loop.run()
    reader.close()
    data = b"".join(got)
    if not data:
        return data, errors[0] if errors else f"nothing arrived within {timeout} s", reader
    return data, None, reader


def check_iptv(report, streams=None):
    """Read an HTTPS HLS stream into a transport stream the way LinDVB records
    an IPTV channel into the timeshift buffer, and play that back: picture
    and sound that is not silent."""
    import tempfile
    env = os.environ.get("LINDVB_SELFCHECK_STREAM")
    streams = streams or ([env] if env else STREAMS)
    problems = []
    for uri in streams:
        data, error, reader = read_stream(uri)
        how = "passed on" if getattr(reader.reader, "passthrough", None) else "remuxed"
        if error:
            problems.append(f"{uri}: reading: {error}")
            continue
        with tempfile.NamedTemporaryFile(suffix=".ts") as f:
            f.write(data)
            f.flush()
            error, counts, used = _play(Path(f.name).as_uri())
        detail = f"{len(data) // 1024} KiB {how}, {_detail(counts, used)}"
        if not error and (counts["video"] < MIN_VIDEO_FRAMES or counts["audio"] == 0):
            error = "too little decoded"
        if not error and silent(counts):
            error = "the sound is silent"
        if not error:
            report.ok(f"IPTV path (timeshift buffer) for {uri}: {detail}")
            return
        problems.append(f"{uri}: {error}; {detail}")
    report.fail("IPTV path (timeshift buffer): " + " | ".join(problems))


def check_hbbtv(report):
    """HbbTV needs WebKitGTK 6 and a web process that starts.

    Snap and Flatpak bring WebKit, so there it is required; elsewhere it is
    optional (the .deb only recommends it) and its absence is a warning.
    """
    import gi
    from . import compat
    required = compat.bundled() in ("Snap", "Flatpak")
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit
    except (ImportError, ValueError) as e:
        (report.fail if required else report.warn)(f"HbbTV unavailable: no WebKitGTK 6 ({e})")
        return
    version = "{}.{}.{}".format(WebKit.get_major_version(), WebKit.get_minor_version(),
                                WebKit.get_micro_version())
    from . import hbbtv
    if not hbbtv.sandbox_ok():
        report.warn(f"WebKitGTK {version}: its sandbox cannot start here, HbbTV is off")
        return
    error, result = _run_page(WebKit)
    if error:
        report.fail(f"WebKitGTK {version}: a page does not run: {error}")
    else:
        report.ok(f"WebKitGTK {version} for HbbTV: page loaded, JavaScript returned {result}")


def _run_page(WebKit, timeout=30):
    """Load a page in a WebView and evaluate JavaScript in its web process."""
    from gi.repository import GLib
    state = {"done": False, "error": None, "result": None}
    view = WebKit.WebView()

    def evaluated(v, res):
        try:
            state["result"] = v.evaluate_javascript_finish(res).to_string()
        except GLib.Error as e:
            state["error"] = e.message
        state["done"] = True

    def changed(v, event):
        if event == WebKit.LoadEvent.FINISHED and not state["done"]:
            v.evaluate_javascript("6 * 7", -1, None, None, None, evaluated)

    def failed(v, event, uri, err):
        state["error"], state["done"] = err.message, True
        return True

    def crashed(v):
        state["error"], state["done"] = "the web process crashed", True
        return True

    view.connect("load-changed", changed)
    view.connect("load-failed", failed)
    view.connect("web-process-terminated", lambda v, reason: crashed(v))
    view.load_html("<!doctype html><title>LinDVB</title><p>self-check</p>", "about:blank")
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not state["done"] and time.monotonic() < deadline:
        ctx.iteration(False)
        time.sleep(0.01)
    if not state["done"]:
        state["error"] = f"no answer within {timeout} s"
    if state["result"] != "42" and not state["error"]:
        state["error"] = f"JavaScript returned {state['result']!r}"
    return state["error"], state["result"]


def run(argv=None, out=None):
    """Run all checks, print one line each, return the exit status."""
    argv = sys.argv[1:] if argv is None else argv
    out = out or sys.stdout
    report = Report(out)
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("Gtk", "4.0")
    from . import __version__, compat
    from gi.repository import Gst
    Gst.init(None)
    where = compat.bundled() or "system packages"
    out.write(f"LinDVB {__version__} self-check ({where}, {Gst.version_string()})\n")
    checks = [check_gtk, check_elements, check_playback, check_hbbtv]
    if "--offline" not in argv:
        checks += [check_tls, check_proxy, check_stream, check_iptv]
    for check in checks:
        try:
            check(report)
        except Exception as e:  # a crash in one check must not hide the others
            report.fail(f"{check.__name__}: {type(e).__name__}: {e}")
    out.write("self-check " + ("FAILED" if report.failed else "passed") + "\n")
    out.flush()
    return 1 if report.failed else 0
