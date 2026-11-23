#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""How much CPU, memory, threads and open files LinDVB's player needs.

Relative numbers: a run says little alone (it depends on the machine), so
CI runs it on the base and on the head of a pull request on the same runner
and compares (``compare``). On a real machine with a tuner it shows where
the time goes (``--decoder auto`` uses the hardware decoder there).

    tools/perfcheck.py run [--seconds 20] [--switches 30] [--out perf.json]
                           [--lindvb CHECKOUT]
    tools/perfcheck.py compare base.json head.json [--summary file.md]

``--lindvb`` measures the lindvb package of another checkout (the base of a
pull request) with this tool and these stream samples, so that only the
player differs between the two runs.

Scenarios (each in a process of its own, so memory does not carry over):

* ``play``: a 720p25 H.264/AAC HLS stream (made locally) played as an
  internet channel without timeshift; CPU seconds per second of picture,
  peak memory, frames that reached the sink, late frames (QoS).
* ``play-no-cc``: the same without the closed caption chain (ccextractor,
  cc708overlay), which every picture passes through today: the difference
  to ``play`` is what captions cost a channel without captions.
* ``play-captions``: the same stream with CEA-708 captions, shown.
* ``reader``: the timeshift reader alone on a live HLS stream (what runs all
  the time for a channel with timeshift).
* ``switches``: many channel changes between two streams; threads, open
  files and memory after them, compared with after the first one (a leak
  grows with every change).

Needs a display (run under xvfb-run) and GStreamer with x264enc.
"""

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
if "--lindvb" in sys.argv[:-1]:
    # before anything imports lindvb: the checkout to measure comes first
    sys.path.insert(0, str(Path(sys.argv[sys.argv.index("--lindvb") + 1]).resolve()))

SCENARIOS = ("play", "play-no-cc", "play-captions", "reader", "switches")
# what compare calls worse: (key, relative tolerance, absolute slack)
CHECKS = (
    ("cpu_per_s", 0.15, 0.02),
    ("rss_mb", 0.10, 8.0),
    ("late_frames", 0.50, 5),
    ("threads_growth", 0.0, 2),
    ("fds_growth", 0.0, 2),
    ("rss_growth_mb", 0.50, 15.0),
)
# a growth beyond the slack after many channel changes is a leak, not noise
LEAKS = ("threads_growth", "fds_growth")


def _proc():
    """(threads, open files, resident MB) of this process now."""
    threads = rss = 0
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("Threads:"):
                threads = int(line.split()[1])
            elif line.startswith("VmRSS:"):
                rss = int(line.split()[1]) / 1024
    return threads, len(os.listdir("/proc/self/fd")), rss


def _cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _peak_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _run_loop(seconds, until=None):
    from gi.repository import GLib
    loop = GLib.MainLoop()
    end = time.monotonic() + seconds

    def tick():
        if time.monotonic() > end or (until and until()):
            loop.quit()
            return False
        return True
    GLib.timeout_add(50, tick)
    loop.run()


def _player(decoder):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, Gtk
    Gst.init(None)
    if not Gtk.init_check():
        sys.exit("perfcheck: no display (run under xvfb-run)")
    from lindvb.config import DEFAULTS
    from lindvb.player import Player
    return Player(Gtk.Picture(), dict(DEFAULTS, deinterlace=False, video_decoder=decoder))


def _count_frames(player):
    """{"frames": buffers into the video sink, "late": QoS messages}, kept
    up to date while the pipeline plays."""
    from gi.repository import Gst
    counts = {"frames": 0, "late": 0, "probed": None}

    def on_buffer(_pad, _info):
        counts["frames"] += 1
        return Gst.PadProbeReturn.OK

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.QOS:
            counts["late"] += 1

    def attach():
        pb = player.playbin
        if pb is None or counts["probed"] is pb:
            return True
        pad = player.video.sink.get_static_pad("sink")
        if pad is not None:
            pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
            bus = pb.get_bus()
            bus.add_signal_watch()
            bus.connect("message", on_message)
            counts["probed"] = pb
        return True
    from gi.repository import GLib
    GLib.timeout_add(20, attach)
    return counts


def _serve(kind, folder, seconds):
    import streamkit as sk
    if kind == "captions":
        sk.make_captioned_segments(folder / "main", seconds, 1280, 720)
        return sk.Server(folder), "main/index.m3u8"
    if kind == "live":
        seg = sk.make_av_segments(folder / "main", seconds, 1280, 720)
        live = {"main/live.m3u8": sk.LivePlaylist(sorted(p.name for p in seg.glob("seg*.ts")))}
        return sk.Server(folder, live=live), "main/live.m3u8"
    sk.make_av_segments(folder / "main", seconds, 1280, 720)
    return sk.Server(folder), "main/index.m3u8"


def scenario_play(seconds, decoder, cc=True, shown=False):
    player = _player(decoder)
    if not cc:
        from lindvb import captions
        captions.available = lambda: False
    with tempfile.TemporaryDirectory() as tmp:
        srv, path = _serve("captions" if shown else "vod", Path(tmp), seconds + 10)
        counts = _count_frames(player)
        player.play_uri(srv.url(path), live=True)
        _run_loop(20, lambda: player._played and (not shown or player.subtitle_tracks()[0]))
        if shown:
            labels, _cur = player.subtitle_tracks()
            if not labels:
                raise SystemExit("perfcheck: the captions were not found")
            player.select_subtitle(0)
        _run_loop(2)                       # past the start
        c0, t0, f0, l0 = _cpu(), time.monotonic(), counts["frames"], counts["late"]
        _run_loop(seconds)
        cpu, wall = _cpu() - c0, time.monotonic() - t0
        frames, late = counts["frames"] - f0, counts["late"] - l0
        player.stop()
        srv.close()
    return {"cpu_per_s": round(cpu / wall, 3), "rss_mb": round(_peak_mb(), 1),
            "fps": round(frames / wall, 1), "late_frames": late}


def scenario_reader(seconds, _decoder):
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    from lindvb import ipstream
    with tempfile.TemporaryDirectory() as tmp:
        srv, path = _serve("live", Path(tmp), seconds + 20)
        got = [0]
        r = ipstream.GstReader(srv.url(path), lambda d: got.__setitem__(0, got[0] + len(d)),
                               lambda e: None, lambda k, m: None)
        r.start()
        _run_loop(20, lambda: got[0] > 0)
        c0, t0 = _cpu(), time.monotonic()
        _run_loop(seconds)
        cpu, wall = _cpu() - c0, time.monotonic() - t0
        r.close()
        srv.close()
    return {"cpu_per_s": round(cpu / wall, 3), "rss_mb": round(_peak_mb(), 1),
            "kbit_s": round(got[0] * 8 / 1000 / wall)}


def scenario_switches(switches, decoder):
    player = _player(decoder)
    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "a"
        b = Path(tmp) / "b"
        srv_a, path = _serve("vod", a, 8)
        srv_b, _p = _serve("vod", b, 8)
        urls = [srv_a.url(path), srv_b.url(path)]

        def play(i):
            player.play_uri(urls[i % 2], live=True)
            _run_loop(10, lambda: player._played)
            _run_loop(0.5)

        # measured after the first change and after the last, both stopped:
        # what the first one sets up once is not counted as growth
        play(0)
        player.stop()
        _run_loop(1)
        before = _proc()
        for i in range(1, switches):
            play(i)
        player.stop()
        _run_loop(1)
        after = _proc()
        srv_a.close()
        srv_b.close()
    return {"switches": switches, "threads_growth": after[0] - before[0],
            "fds_growth": after[1] - before[1], "rss_growth_mb": round(after[2] - before[2], 1),
            "rss_mb": round(_peak_mb(), 1)}


def run_scenario(name, args):
    if name == "play":
        return scenario_play(args.seconds, args.decoder)
    if name == "play-no-cc":
        return scenario_play(args.seconds, args.decoder, cc=False)
    if name == "play-captions":
        return scenario_play(args.seconds, args.decoder, shown=True)
    if name == "reader":
        return scenario_reader(args.seconds, args.decoder)
    if name == "switches":
        return scenario_switches(args.switches, args.decoder)
    raise SystemExit(f"unknown scenario {name}")


def cmd_run(args):
    results = {}
    for name in args.only or SCENARIOS:
        runs = []
        for _i in range(args.repeat):
            out = subprocess.run(
                [sys.executable, __file__, "scenario", name, "--seconds", str(args.seconds),
                 "--switches", str(args.switches), "--decoder", args.decoder,
                 *(["--lindvb", args.lindvb] if args.lindvb else [])],
                capture_output=True, text=True, timeout=600)
            line = next((ln for ln in reversed(out.stdout.splitlines()) if ln.startswith("{")), "")
            if out.returncode or not line:
                sys.stderr.write(out.stderr[-2000:])
                raise SystemExit(f"perfcheck: scenario {name} failed")
            runs.append(json.loads(line))
        # the median of each number: one slow run on a busy runner is not a trend
        results[name] = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
        print(name, json.dumps(results[name]), flush=True)
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    import lindvb
    report = {"gstreamer": Gst.version_string(), "decoder": args.decoder,
              "lindvb": str(Path(lindvb.__file__).parent),
              "seconds": args.seconds, "repeat": args.repeat, "scenarios": results}
    if "play" in results and "play-no-cc" in results:
        report["cc_chain_cpu_per_s"] = round(results["play"]["cpu_per_s"] -
                                             results["play-no-cc"]["cpu_per_s"], 3)
    text = json.dumps(report, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n")
    else:
        print(text)


def compare(base, head):
    """[(scenario, key, base, head, verdict)], verdict "ok", "worse" or "leak"."""
    rows = []
    for name, hres in head["scenarios"].items():
        bres = base["scenarios"].get(name, {})
        for key, rel, slack in CHECKS:
            if key not in hres or key not in bres:
                continue
            b, h = bres[key], hres[key]
            worse = h > b * (1 + rel) + slack
            verdict = ("leak" if key in LEAKS else "worse") if worse else "ok"
            rows.append((name, key, b, h, verdict))
    return rows


def cmd_compare(args):
    base = json.loads(Path(args.base).read_text())
    head = json.loads(Path(args.head).read_text())
    rows = compare(base, head)
    lines = ["| scenario | measure | base | head | |", "|---|---|---|---|---|"]
    mark = {"ok": "", "worse": "⚠ worse", "leak": "✗ leak"}
    lines += [f"| {n} | {k} | {b} | {h} | {mark[v]} |" for n, k, b, h, v in rows]
    for key in ("cc_chain_cpu_per_s",):
        if key in base or key in head:
            lines.append(f"| play | {key} | {base.get(key, '–')} | {head.get(key, '–')} | |")
    text = "\n".join(lines) + "\n"
    print(text)
    if args.summary:
        with open(args.summary, "a") as f:
            f.write("### Player resources (base vs head, same runner)\n\n" + text)
    # CPU and memory on a shared runner are noisy: only leaks fail the check
    return 1 if any(v == "leak" for *_r, v in rows) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "scenario"):
        p = sub.add_parser(name)
        if name == "scenario":
            p.add_argument("name", choices=SCENARIOS)
        p.add_argument("--seconds", type=int, default=20)
        p.add_argument("--switches", type=int, default=30)
        p.add_argument("--decoder", default="software",
                       help="video_decoder setting: software (CI), auto (hardware if there)")
        p.add_argument("--lindvb", help="checkout whose lindvb package is measured")
        if name == "run":
            p.add_argument("--repeat", type=int, default=3)
            p.add_argument("--only", action="append", choices=SCENARIOS)
            p.add_argument("--out")
    p = sub.add_parser("compare")
    p.add_argument("base")
    p.add_argument("head")
    p.add_argument("--summary")
    args = ap.parse_args(argv)
    if args.cmd == "scenario":
        print(json.dumps(run_scenario(args.name, args)), flush=True)
        return 0
    if args.cmd == "run":
        cmd_run(args)
        return 0
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
