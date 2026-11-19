# SPDX-License-Identifier: GPL-2.0-or-later
"""``lindvb --self-check``: is this installation of LinDVB able to play TV?

Every package (AppImage, Flatpak, .deb, Snap) runs this in CI before it is
released. It starts GTK and lays out text (this is where a wrong HarfBuzz or
Pango shows up), looks for the GStreamer elements live TV needs and plays a
short MPEG-TS sample (H.264 video, MPEG-1 Layer II audio, as on DVB) through
playbin into fake sinks, so the decoders are not only installed but work.

Then it does what an IPTV channel does: it checks that GIO has a TLS backend
and plays a public HTTPS HLS stream (H.264 and AAC) until enough frames are
decoded. ``--offline`` leaves the network checks out.

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
ELEMENTS = ["h264parse", "mpegaudioparse", "videoconvert", "audioconvert", "decodebin3"]
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
    counts = {"video": 0, "audio": 0}

    def sink(kind):
        s = Gst.ElementFactory.make("fakesink", f"{kind}sink")
        s.set_property("sync", False)
        s.set_property("signal-handoffs", True)
        s.connect("handoff", lambda *a: counts.__setitem__(kind, counts[kind] + 1))
        return s

    play = Gst.ElementFactory.make("playbin", None)
    if play is None:
        return "playbin cannot be created", counts, []
    play.set_property("uri", uri)
    play.set_property("video-sink", sink("video"))
    play.set_property("audio-sink", sink("audio"))
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


def _detail(counts, used):
    detail = f"{counts['video']} video frames, {counts['audio']} audio buffers"
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


def check_stream(report, streams=None):
    """Play an HTTPS HLS stream the way an IPTV channel plays."""
    env = os.environ.get("LINDVB_SELFCHECK_STREAM")
    streams = streams or ([env] if env else STREAMS)
    problems = []
    for uri in streams:
        error, counts, used = _play(uri, enough=STREAM_FRAMES, timeout=STREAM_TIMEOUT)
        if not error:
            report.ok(f"played the HTTPS HLS stream {uri}: {_detail(counts, used)}")
            return
        problems.append(f"{uri}: {error}; {_detail(counts, used)}")
    report.fail("no HTTPS HLS stream played: " + " | ".join(problems))


def check_hbbtv(report):
    """HbbTV needs WebKitGTK 6. Not a failure: the .deb only recommends it."""
    import gi
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit
    except (ImportError, ValueError) as e:
        report.warn(f"HbbTV unavailable: no WebKitGTK 6 ({e})")
        return
    report.ok("WebKitGTK {}.{}.{} for HbbTV".format(
        WebKit.get_major_version(), WebKit.get_minor_version(), WebKit.get_micro_version()))


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
        checks += [check_tls, check_stream]
    for check in checks:
        try:
            check(report)
        except Exception as e:  # a crash in one check must not hide the others
            report.fail(f"{check.__name__}: {type(e).__name__}: {e}")
    out.write("self-check " + ("FAILED" if report.failed else "passed") + "\n")
    out.flush()
    return 1 if report.failed else 0
