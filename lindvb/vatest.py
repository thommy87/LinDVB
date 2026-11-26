# SPDX-License-Identifier: GPL-2.0-or-later
"""``--va-test [FILE]``: do graphics buffers stay behind without LinDVB?

With VA-API and its deinterlacer, 17 graphics buffers (dmabuf) stay open with
every channel LinDVB starts, also after the VA display has gone (debug logs of
26.11.). This test plays FILE (an H.264, H.265 or MPEG-2 recording; the
self-check sample when none is given) a few rounds in each of a set of plain
GStreamer pipelines, without LinDVB's player, and counts the graphics buffers
the process holds after each round. A variant that leaks without LinDVB shows
where the buffers are held; the ones with LinDVB's video output (frames copied,
or gtk4paintablesink with OpenGL) show what the output adds and whether the VA
decoder can hand its pictures to it.

Everything runs in this one process: buffers that stay open stay open, so a
variant's first round may carry a one-time start (the first VA-API use); the
rounds after it show what each round leaves behind.
"""

import gc
import os
import sys
import time
from pathlib import Path

ROUNDS = 8
FRAMES = 60             # pictures each round plays
ROUND_TIMEOUT = 6.0     # seconds
TAIL = ("videoconvert", "videobalance", "videoconvert")   # LinDVB's video filter after the deinterlacer

# codec caps -> (VA-API decoder, software decoder)
DECODERS = {
    "video/x-h264": ("vah264dec", "avdec_h264"),
    "video/x-h265": ("vah265dec", "avdec_h265"),
    "video/mpeg": ("vampeg2dec", "avdec_mpeg2video"),
}

# (label, decoder "va"/"sw", filter before TAIL, sink "fake"/"copy"/"gl", rebuilt each round)
VARIANTS = (
    ("software decoder + deinterlace", "sw", ("deinterlace",), "fake", True),
    ("VA decoder", "va", (), "fake", True),
    ("VA decoder + vapostproc", "va", ("vapostproc",), "fake", True),
    ("VA decoder + vadeinterlace", "va", ("vadeinterlace",), "fake", True),
    ("VA decoder + vadeinterlace, one pipeline restarted", "va", ("vadeinterlace",), "fake", False),
    ("VA decoder + vadeinterlace into LinDVB's copying output", "va", ("vadeinterlace",), "copy", True),
    ("VA decoder into gtk4paintablesink", "va", (), "gl", True),
    ("VA decoder + vadeinterlace into gtk4paintablesink", "va", ("vadeinterlace",), "gl", True),
)


def dmabufs(fd_dir="/proc/self/fd"):
    """How many graphics buffers (dmabuf) the process has open."""
    n = 0
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return 0
    for name in names:
        try:
            if os.readlink(os.path.join(fd_dir, name)).startswith("/dmabuf:"):
                n += 1
        except OSError:
            pass
    return n


def _iterate(seconds=0.05):
    from gi.repository import GLib
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not ctx.iteration(False):
            time.sleep(0.005)


def _codec(path):
    """The video caps name of *path* ("video/x-h264"), or None."""
    from gi.repository import Gst
    pipe = Gst.parse_launch("filesrc name=src ! parsebin name=pb")
    pipe.get_by_name("src").set_property("location", str(path))
    found, done = [], []

    def added(_pb, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        if name in DECODERS and not found:
            found.append(name)
    parse = pipe.get_by_name("pb")
    parse.connect("pad-added", added)
    parse.connect("no-more-pads", lambda *_a: done.append(True))
    pipe.set_state(Gst.State.PAUSED)
    end = time.monotonic() + 5
    while time.monotonic() < end and not found and not done:
        time.sleep(0.02)
    pipe.set_state(Gst.State.NULL)
    return found[0] if found else None


class _Round:
    """One pipeline: file -> parsebin -> decoder -> filter -> sink."""

    def __init__(self, path, decoder, filters, sink):
        from gi.repository import Gst
        from . import hwaccel
        self.frames, self.error = 0, ""
        self.pipe = Gst.Pipeline.new("va-test")
        src = Gst.ElementFactory.make("filesrc")
        src.set_property("location", str(path))
        parse = Gst.ElementFactory.make("parsebin")
        self.decoder = Gst.ElementFactory.make(decoder)
        chain = []
        for name in tuple(filters) + TAIL:
            e = hwaccel.make_deinterlacer("vaapi") if name == "vadeinterlace" else \
                Gst.ElementFactory.make(name)
            chain.append(e)
        self.sink = sink
        for e in [src, parse, self.decoder] + chain + [sink]:
            self.pipe.add(e)
        src.link(parse)
        for a, b in zip([self.decoder] + chain, chain + [sink]):
            a.link(b)
        chain[-1].get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._count)
        parse.connect("pad-added", self._link)

    def _link(self, _parse, pad):
        sink = self.decoder.get_static_pad("sink")
        if not sink.is_linked():
            pad.link(sink)

    def _count(self, _pad, _info):
        from gi.repository import Gst
        self.frames += 1
        return Gst.PadProbeReturn.OK

    def play(self):
        from gi.repository import Gst
        self.frames, self.error = 0, ""
        bus = self.pipe.get_bus()
        self.pipe.set_state(Gst.State.PLAYING)
        end = time.monotonic() + ROUND_TIMEOUT
        while time.monotonic() < end and self.frames < FRAMES:
            msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    err, _dbg = msg.parse_error()
                    self.error = f"{msg.src.get_name()}: {err.message}"
                break
            _iterate(0.02)
        self.pipe.set_state(Gst.State.NULL)
        return self.frames

    def close(self):
        """Take the reused sink out (LinDVB keeps its video output across channels)."""
        if self.sink is not None and self.sink.get_parent() is self.pipe:
            self.pipe.remove(self.sink)
        self.pipe = self.decoder = self.sink = None


def _sink_maker(kind):
    """A function that gives the sink for each round, and the output's name.
    LinDVB's own output (VideoBin) is made once and used in every round."""
    from gi.repository import Gst
    if kind == "fake":
        def fake():
            s = Gst.ElementFactory.make("fakesink")
            s.set_property("sync", False)
            return s
        return fake, "fakesink"
    from gi.repository import Gtk
    if not Gtk.init_check():
        return None, "no display"
    from .player import VideoBin
    if kind == "gl" and Gst.ElementFactory.find("gtk4paintablesink") is None:
        return None, "gtk4paintablesink missing"
    vb = VideoBin(Gtk.Picture(), kind == "gl")
    if kind == "gl" and vb.kind == "copy":
        return None, "gtk4paintablesink has no OpenGL here"
    return (lambda: vb.sink), vb.kind


def run_variant(path, codec, variant, rounds=ROUNDS):
    """{"label", "skipped" or "counts" (buffers after each round), "frames",
    "error"} for one variant."""
    from gi.repository import Gst
    label, dec_kind, filters, sink_kind, rebuild = variant
    out = {"label": label}
    decoder = DECODERS[codec][0 if dec_kind == "va" else 1]
    missing = [n for n in (decoder,) + tuple(filters) if Gst.ElementFactory.find(n) is None]
    if missing:
        out["skipped"] = ", ".join(missing) + " missing"
        return out
    make_sink, output = _sink_maker(sink_kind)
    out["output"] = output
    if make_sink is None:
        out["skipped"] = output
        return out
    start = dmabufs()
    counts, frames, errors = [], [], []
    rnd = None
    for _i in range(rounds):
        if rnd is None:
            rnd = _Round(path, decoder, filters, make_sink())
        frames.append(rnd.play())
        if rnd.error:
            errors.append(rnd.error)
        if rebuild:
            rnd.close()
            rnd = None
        gc.collect()
        _iterate(0.2)
        counts.append(dmabufs())
    if rnd is not None:
        rnd.close()
        gc.collect()
        _iterate(0.2)
        counts[-1] = dmabufs()
    out.update(start=start, counts=counts, frames=frames, error=errors[0] if errors else "")
    return out


def describe(result):
    """One line of the report for a variant's result."""
    label = result["label"]
    if "skipped" in result:
        return f"{label}: skipped ({result['skipped']})"
    counts, start = result["counts"], result["start"]
    first = counts[0] - start
    later = (counts[-1] - counts[0]) / (len(counts) - 1) if len(counts) > 1 else 0.0
    shown = sum(1 for f in result["frames"] if f)
    pictures = "pictures in every round" if shown == len(result["frames"]) else \
        f"pictures in {shown} of {len(result['frames'])} rounds"
    line = (f"{label} [{result.get('output', '')}]: {pictures}; graphics buffers "
            f"{start} -> {counts[-1]}: first round {first:+d}, then {later:+.1f} per round")
    if result["error"]:
        line += f"; error: {result['error']}"
    return line


def report_path():
    from .config import CACHE_DIR
    return Path(CACHE_DIR) / "va-test.txt"


def run(argv=None, out=None):
    """Run every variant and write the report to the terminal and to
    ~/.cache/lindvb/va-test.txt (to send along with a bug report)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gst
    Gst.init(None)
    from . import __version__, fdlimit
    fdlimit.raise_limit()
    args = [a for a in argv if a != "--va-test"]
    rounds = ROUNDS
    if "--rounds" in args:
        i = args.index("--rounds")
        rounds = max(2, int(args[i + 1]))
        del args[i:i + 2]
    if args:
        path = Path(os.environ.get("LINDVB_ORIG_CWD", "."), os.path.expanduser(args[0]))
    else:
        from .selfcheck import SAMPLE
        path = SAMPLE
    lines = [f"LinDVB {__version__} VA-API isolation test; {Gst.version_string()}; "
             f"{rounds} rounds of {FRAMES} pictures per variant",
             f"file: {path.name}"]

    def say(line):
        lines.append(line)
        print(line, file=out, flush=True)
    for line in lines:
        print(line, file=out, flush=True)
    if not path.is_file():
        say(f"file not found: {path}")
        return 2
    codec = _codec(path)
    if codec is None:
        say("no H.264, H.265 or MPEG-2 video found in the file")
        return 2
    say(f"video: {codec}; graphics buffers open at the start: {dmabufs()}")
    for variant in VARIANTS:
        say(describe(run_variant(path, codec, variant, rounds)))
    target = report_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nThe report is in {target}", file=out, flush=True)
    except OSError as e:
        print(f"\nThe report could not be saved: {e}", file=out, flush=True)
    return 0
