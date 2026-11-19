# SPDX-License-Identifier: GPL-2.0-or-later
"""``lindvb --self-check``: is this installation of LinDVB able to play TV?

Every package (AppImage, Flatpak, .deb, Snap) runs this in CI before it is
released. It starts GTK and lays out text (this is where a wrong HarfBuzz or
Pango shows up), looks for the GStreamer elements live TV needs and plays a
short MPEG-TS sample (H.264 video, MPEG-1 Layer II audio, as on DVB) through
playbin into fake sinks, so the decoders are not only installed but work.

It needs a display (CI uses xvfb-run) but no sound server, tuner or network.
The exit status is 0 when everything works and 1 otherwise.
"""

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


class Report:
    def __init__(self, out):
        self.out = out
        self.failed = False

    def ok(self, what):
        self.out.write(f"OK    {what}\n")

    def fail(self, what):
        self.failed = True
        self.out.write(f"FAIL  {what}\n")


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


def check_playback(report, sample=SAMPLE):
    from gi.repository import GLib, Gst
    if not sample.is_file():
        report.fail(f"test stream {sample} is missing")
        return
    counts = {"video": 0, "audio": 0}

    def sink(kind):
        s = Gst.ElementFactory.make("fakesink", f"{kind}sink")
        s.set_property("sync", False)
        s.set_property("signal-handoffs", True)
        s.connect("handoff", lambda *a: counts.__setitem__(kind, counts[kind] + 1))
        return s

    play = Gst.ElementFactory.make("playbin", None)
    if play is None:
        report.fail("playbin cannot be created")
        return
    play.set_property("uri", sample.resolve().as_uri())
    play.set_property("video-sink", sink("video"))
    play.set_property("audio-sink", sink("audio"))
    bus = play.get_bus()
    play.set_state(Gst.State.PLAYING)
    error = None
    msg = bus.timed_pop_filtered(TIMEOUT * Gst.SECOND,
                                 Gst.MessageType.EOS | Gst.MessageType.ERROR)
    if msg is None:
        error = f"no end of stream within {TIMEOUT} s"
    elif msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        error = f"{err.message} ({dbg})" if dbg else err.message
    used = [n for n in _factories(play) if "dec" in n and "decodebin" not in n]
    play.set_state(Gst.State.NULL)
    GLib.MainContext.default().iteration(False)
    detail = f"{counts['video']} video frames, {counts['audio']} audio buffers"
    if used:
        detail += ", decoders: " + ", ".join(dict.fromkeys(used))
    if error:
        report.fail(f"playing the H.264 test stream: {error}; {detail}")
    elif counts["video"] < MIN_VIDEO_FRAMES or counts["audio"] == 0:
        report.fail(f"playing the H.264 test stream: too little decoded; {detail}")
    else:
        report.ok(f"played the H.264/MPEG audio test stream: {detail}")


def run(out=None):
    """Run all checks, print one line each, return the exit status."""
    out = out or sys.stdout
    report = Report(out)
    from . import __version__, compat
    from gi.repository import Gst
    Gst.init(None)
    where = compat.bundled() or "system packages"
    out.write(f"LinDVB {__version__} self-check ({where}, {Gst.version_string()})\n")
    for check in (check_gtk, check_elements, check_playback):
        try:
            check(report)
        except Exception as e:  # a crash in one check must not hide the others
            report.fail(f"{check.__name__}: {type(e).__name__}: {e}")
    out.write("self-check " + ("FAILED" if report.failed else "passed") + "\n")
    out.flush()
    return 1 if report.failed else 0
