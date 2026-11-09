#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Measure the dropouts of an internet stream (HLS/DASH) the way LinDVB plays it.

    python3 tools/hls-diagnose.py URL [seconds]

Plays URL for each engine and buffer setting below (default 90 s each), with
no picture and no sound, and prints for every run: how often playback stopped
to buffer, how long it stood still, how long it took to start,
the quality changes and the segments that took longer to load than they last.
A GStreamer debug log of each run is written next to the report
(hls-diagnose-*.log); please send both if asked.
"""
import os
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

RUNS = (
    # (name, engine, hlsdemux2/dashdemux2 properties)
    ("playbin3, as LinDVB 0.11 played it", "playbin3", {}),
    ("playbin3, as LinDVB plays it now (start at 5 Mbit/s, buffering below 3 s)", "playbin3",
     {"start-bitrate": 5_000_000, "low-watermark-time": 3_000_000_000}),
    ("playbin (classic hlsdemux)", "playbin", {}),
)


def run(url, name, engine, props, seconds):
    pb = Gst.ElementFactory.make(engine)
    if pb is None:
        return f"{name}: {engine} is not installed\n"
    pb.set_property("uri", url)
    for kind in ("video-sink", "audio-sink"):
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", True)
        pb.set_property(kind, sink)
    st = {"played": False, "last": 100, "stalls": 0, "stall_s": 0.0, "since": None,
          "caps": set(), "start": None, "slow": 0, "frags": 0, "errors": [], "msgs": 0}

    def element_setup(_pb, el):
        f = el.get_factory()
        if f and f.get_name() in ("hlsdemux2", "dashdemux2"):
            for k, v in props.items():
                el.set_property(k, v)

    def on_message(_bus, msg):
        t = msg.type
        if t == Gst.MessageType.BUFFERING:
            pct = msg.parse_buffering()
            st["msgs"] += 1
            if pct < 100 and st["last"] >= 100:
                if st["played"]:
                    st["stalls"] += 1
                st["since"] = time.monotonic()
            elif pct >= 100 and st["since"] is not None:
                if st["played"]:
                    st["stall_s"] += time.monotonic() - st["since"]
                st["since"] = None
            st["last"] = pct
            pb.set_state(Gst.State.PAUSED if pct < 100 else Gst.State.PLAYING)
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == pb:
            if msg.parse_state_changed()[1] == Gst.State.PLAYING and not st["played"]:
                st["played"] = True
                st["start"] = time.monotonic() - t0
        elif t == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s and s.get_name() == "adaptive-streaming-statistics":
                st["frags"] += 1
                ok, dl = s.get_uint64("fragment-download-time")
                ok2, dur = s.get_uint64("fragment-duration") if s.has_field("fragment-duration") else (False, 0)
                if ok and ok2 and dur and dl > dur:
                    st["slow"] += 1
        elif t in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
            err, _dbg = msg.parse_error() if t == Gst.MessageType.ERROR else msg.parse_warning()
            st["errors"].append(err.message)

    def caps_probe():
        for kind in ("video-sink",):
            pad = pb.get_property(kind).get_static_pad("sink")
            caps = pad.get_current_caps() if pad else None
            if caps:
                s = caps.get_structure(0)
                st["caps"].add(f"{s.get_value('width')}x{s.get_value('height')}")
        return True

    bus = pb.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)
    pb.connect("element-setup", element_setup)
    loop = GLib.MainLoop()
    t0 = time.monotonic()
    pb.set_state(Gst.State.PLAYING)
    GLib.timeout_add(500, caps_probe)
    GLib.timeout_add_seconds(seconds, loop.quit)
    loop.run()
    if st["since"] is not None and st["played"]:
        st["stall_s"] += time.monotonic() - st["since"]
    pb.set_state(Gst.State.NULL)
    bus.remove_signal_watch()
    wall = time.monotonic() - t0
    start = st.get("start")
    lines = [f"{name}",
             f"  picture after {start:.1f} s" if start is not None else
             f"  never played in {wall:.0f} s",
             f"  stopped to buffer {st['stalls']} times, {st['stall_s']:.1f} s in all"
             f" ({st['msgs']} buffering messages)",
             f"  picture sizes: {', '.join(sorted(st['caps'])) or '-'}",
             f"  segment reports: {st['frags']}, loaded slower than real time: {st['slow']}"]
    for e in dict.fromkeys(st["errors"]):
        lines.append(f"  message: {e}")
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    os.environ.setdefault("GST_DEBUG", "2,hlsdemux2:5,adaptivedemux2:5,hlsdemux:5,adaptivedemux:5,"
                                       "souphttpsrc:4,queue2:4,multiqueue:3")
    os.environ["GST_DEBUG_NO_COLOR"] = "1"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = [f"LinDVB stream diagnosis {stamp}", f"URL: {url}",
              f"GStreamer {'.'.join(map(str, Gst.version()))}", ""]
    print("\n".join(report))
    for i, (name, engine, props) in enumerate(RUNS, 1):
        log = os.path.abspath(f"hls-diagnose-{stamp}-{i}.log")
        # the debug log file is opened when GStreamer starts, so each run is
        # its own process
        if os.fork() == 0:
            os.environ["GST_DEBUG_FILE"] = log
            Gst.init(None)
            out = run(url, name, engine, props, seconds)
            with open(f"hls-diagnose-{stamp}-{i}.txt", "w") as f:
                f.write(out)
            os._exit(0)
        os.wait()
        try:
            with open(f"hls-diagnose-{stamp}-{i}.txt") as f:
                part = f.read()
        except OSError:
            part = f"{name}: run failed\n"
        print(part)
        report.append(part)
    with open(f"hls-diagnose-{stamp}.txt", "w") as f:
        f.write("\n".join(report))
    print(f"Report: hls-diagnose-{stamp}.txt, debug logs: hls-diagnose-{stamp}-*.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
