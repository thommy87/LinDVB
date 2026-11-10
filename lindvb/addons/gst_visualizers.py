# SPDX-License-Identifier: GPL-2.0-or-later
"""GStreamer visualisation for radio: the original GOOM (gst-plugins-good).

GStreamer visualisers render at 320x240 unless told otherwise, which looks
blocky in full screen. GOOM runs on the CPU and slows down a lot at high
resolutions, so it uses "goom_resolution" (default 640x360, 16:9); LinDVB's
own "Goom HD (GPU)" renders goom's effects at full resolution. A leaky queue
in front of the visualiser drops audio for the picture when it cannot keep
up, so the sound never waits for it.

projectM was offered here until version 0.14 and has been taken out: with
libprojectM 4.1 and gst-projectm it stayed black, showed fragments or froze
on many systems. It can come back once those projects have fixed it.
"""

import re

NAME = "GStreamer visualisation (GOOM)"
VERSION = "2.0"

GOOM_SIZE = (640, 360)
FRAMERATE = 30


def parse_size(text, default):
    """'1920x1080' -> (1920, 1080); anything else -> ``default``. Even sizes only."""
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*", str(text or ""))
    if not m:
        return default
    w, h = int(m.group(1)) & ~1, int(m.group(2)) & ~1
    return (w, h) if w >= 16 and h >= 16 else default


def sized(Gst, element, size):
    """Wrap a visualiser so it renders at ``size`` instead of its default 320x240."""
    w, h = size
    bin_ = Gst.Bin.new(None)
    caps = Gst.ElementFactory.make("capsfilter")
    caps.set_property("caps", Gst.Caps.from_string(
        f"video/x-raw,width={w},height={h},framerate={FRAMERATE}/1"))
    # playbin feeds the visualiser from the audio tee: when it draws slower
    # than real time it would hold the sound back until playback stalls. This
    # queue drops the oldest audio for the picture instead.
    queue = Gst.ElementFactory.make("queue")
    queue.set_property("leaky", 2)                     # downstream: drop old buffers
    queue.set_property("max-size-buffers", 8)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("max-size-time", 0)
    queue.set_property("silent", True)
    for e in (queue, element, caps):
        bin_.add(e)
    queue.link(element)
    element.link(caps)
    bin_.add_pad(Gst.GhostPad.new("sink", queue.get_static_pad("sink")))
    bin_.add_pad(Gst.GhostPad.new("src", caps.get_static_pad("src")))
    return bin_


def register(api):
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        api.log("GStreamer is not available")
        return
    if not Gst.is_initialized():
        Gst.init(None)

    def settings():
        return api.app.settings if api.app is not None else {}

    if Gst.ElementFactory.find("goom") is not None:
        def make_goom():
            size = parse_size(settings().get("goom_resolution"), GOOM_SIZE)
            return sized(Gst, Gst.ElementFactory.make("goom"), size)
        api.add_visualizer("GOOM", element=make_goom)
