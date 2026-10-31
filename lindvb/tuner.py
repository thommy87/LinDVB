"""DVB capture with GStreamer's dvbsrc (Linux DVB API v5).

A Capture tunes one service and writes the transport stream into a rolling
set of segment files. The live player, the timeshift function and the
recorder all read from those segments, so one tuner feeds all of them.
"""

import os
import shutil
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

from .epg import parse_eit_section  # noqa: E402

try:
    gi.require_version("GstMpegts", "1.0")
    from gi.repository import GstMpegts
    GstMpegts.initialize()
except (ImportError, ValueError):
    GstMpegts = None

SEGMENT_BYTES = 64 * 1024 * 1024
TS_PACKET = 188


def dvbsrc_properties(ch, settings, adapter, frontend):
    """Map a Channel onto dvbsrc properties (string values, set via util_set_object_arg)."""
    p = {"adapter": adapter, "frontend": frontend, "delsys": ch.delsys}
    if ch.is_sat:
        p.update({"frequency": ch.frequency, "polarity": ch.polarity.upper() or "H",
                  "symbol-rate": ch.symbol_rate,
                  "lnb-lof1": settings["lnb_lof1"], "lnb-lof2": settings["lnb_lof2"],
                  "lnb-slof": settings["lnb_slof"]})
        if ch.diseqc >= 0:
            p["diseqc-source"] = ch.diseqc
    else:
        p["frequency"] = ch.frequency * 1000
        if ch.symbol_rate:
            p["symbol-rate"] = ch.symbol_rate
        if ch.bandwidth_hz:
            p["bandwidth-hz"] = ch.bandwidth_hz
    if ch.modulation and ch.modulation != "auto":
        p["modulation"] = ch.modulation
    elif ch.delsys.startswith("dvb-c") or ch.delsys.startswith("dvb-t"):
        p["modulation"] = "auto"
    if ch.fec and ch.fec != "auto":
        p["code-rate-hp"] = ch.fec
    if ch.stream_id >= 0:
        p["stream-id"] = ch.stream_id
    p["pids"] = "8192" if settings["full_transponder"] or not ch.pmt_pid else \
        ":".join(map(str, ch.pids()))
    return p


class SegmentStore:
    """Rolling TS segments written by multifilesink (seg00000.ts, seg00001.ts...)."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.starts = {}            # segment index -> absolute byte offset
        self.finished = False

    def path(self, idx):
        return self.dir / f"seg{idx:05d}.ts"

    def indices(self):
        return sorted(int(p.stem[3:]) for p in self.dir.glob("seg*.ts"))

    def _update(self):
        idx = self.indices()
        if not idx:
            return idx
        if idx[0] not in self.starts:
            self.starts[idx[0]] = 0 if not self.starts else max(self.starts.values())
        for a, b in zip(idx, idx[1:]):
            if b not in self.starts and a in self.starts:
                try:
                    self.starts[b] = self.starts[a] + self.path(a).stat().st_size
                except OSError:
                    pass
        return idx

    def bounds(self):
        """(first absolute offset still on disk, absolute end offset)."""
        idx = self._update()
        if not idx:
            return 0, 0
        last = idx[-1]
        try:
            size = self.path(last).stat().st_size
        except OSError:
            size = 0
        return self.starts.get(idx[0], 0), self.starts.get(last, 0) + size

    def locate(self, offset):
        idx = self._update()
        for i in reversed(idx):
            if i in self.starts and self.starts[i] <= offset:
                return i, offset - self.starts[i]
        return (idx[0], 0) if idx else (0, 0)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class SegmentReader:
    """Reads a SegmentStore sequentially, following it while it grows."""

    def __init__(self, store, offset):
        self.store = store
        self.offset = offset - offset % TS_PACKET
        self.idx, pos = store.locate(self.offset)
        self.file = None
        self._open(pos)

    def _open(self, pos=0):
        if self.file:
            self.file.close()
            self.file = None
        try:
            self.file = open(self.store.path(self.idx), "rb")
            self.file.seek(pos)
        except OSError:
            self.file = None

    def read(self, n=TS_PACKET * 512):
        """Return data, b"" if none available yet, or None at the end of stream."""
        if self.file is None:
            if self.idx not in self.store.indices():
                nxt = [i for i in self.store.indices() if i > self.idx]
                if nxt:
                    self.idx = nxt[0]
                    self.offset = self.store.starts.get(self.idx, self.offset)
            self._open()
            if self.file is None:
                return None if self.store.finished else b""
        data = self.file.read(n)
        if data:
            self.offset += len(data)
            return data
        if self.idx + 1 in self.store.indices():
            self.idx += 1
            self._open()
            return b""
        return None if self.store.finished else b""

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


class Capture(GObject.Object):
    """One tuned service written to a SegmentStore."""

    __gsignals__ = {
        "stats": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "eit": (GObject.SignalFlags.RUN_LAST, None, (str, object)),
    }

    def __init__(self, channel, adapter, frontend, settings):
        super().__init__()
        self.channel = channel
        self.adapter = adapter
        base = Path(settings["timeshift_dir"])
        base.mkdir(parents=True, exist_ok=True)
        self.store = SegmentStore(tempfile.mkdtemp(prefix=f"a{adapter}-", dir=base))
        self.users = set()
        self.started_at = time.time()
        self.stats = {}

        self.pipeline = Gst.Pipeline.new(f"capture{adapter}")
        test_ts = os.environ.get("LINDVB_TEST_TS")
        if test_ts:
            # Development aid: replay a recorded transport stream instead of a tuner.
            src = Gst.parse_bin_from_description(
                f'filesrc location="{test_ts}" ! tsparse set-timestamps=true '
                '! clocksync sync-to-first=true', True)
        else:
            src = Gst.ElementFactory.make("dvbsrc", "src")
            if src is None:
                raise RuntimeError("GStreamer element 'dvbsrc' is missing (gst-plugins-bad).")
            for k, v in dvbsrc_properties(channel, settings, adapter, frontend).items():
                Gst.util_set_object_arg(src, k, str(v))
        parse = Gst.ElementFactory.make("tsparse", "parse")
        Gst.util_set_object_arg(parse, "set-timestamps", "true")
        sink = Gst.ElementFactory.make("multifilesink", "sink")
        sink.set_property("location", str(self.store.dir / "seg%05d.ts"))
        Gst.util_set_object_arg(sink, "next-file", "max-size")
        sink.set_property("max-file-size", SEGMENT_BYTES)
        max_files = max(2, int(settings["timeshift_max_mb"]) * 1024 * 1024 // SEGMENT_BYTES)
        sink.set_property("max-files", max_files)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        for e in (src, parse, sink):
            self.pipeline.add(e)
        src.link(parse)
        parse.link(sink)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.emit("error", f"Could not open DVB adapter {self.adapter}.")

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_bus().remove_signal_watch()
        self.store.finished = True

    def destroy(self):
        self.stop()
        self.store.cleanup()

    def byte_rate(self):
        _, end = self.store.bounds()
        dt = max(1.0, time.time() - self.started_at)
        return max(1.0, end / dt) if end else 1_000_000.0

    def _on_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s and s.get_name() == "dvb-frontend-stats":
                st = {k: s.get_value(k) for k in ("signal", "snr", "ber", "unc", "lock")
                      if s.has_field(k)}
                self.stats = st
                self.emit("stats", st)
            elif GstMpegts is not None:
                section = GstMpegts.message_parse_mpegts_section(msg)
                if section is not None:
                    res = parse_eit_section(section, GstMpegts)
                    if res and res[1]:
                        self.emit("eit", res[0], res[1])
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self.emit("error", f"{err.message}")


class IPTVRecorder:
    """Record an internet stream by remuxing it to MPEG-TS."""

    def __init__(self, url, path):
        self.pipeline = Gst.Pipeline.new("iptvrec")
        self.src = Gst.ElementFactory.make("urisourcebin")
        self.src.set_property("uri", url)
        self.parse = Gst.ElementFactory.make("parsebin")
        self.mux = Gst.ElementFactory.make("mpegtsmux")
        sink = Gst.ElementFactory.make("filesink")
        sink.set_property("location", str(path))
        for e in (self.src, self.parse, self.mux, sink):
            self.pipeline.add(e)
        self.mux.link(sink)
        self.src.connect("pad-added", lambda _e, pad: pad.link(self.parse.get_static_pad("sink")))
        self.parse.connect("pad-added", self._parsed_pad)

    def _parsed_pad(self, _e, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        if name == "video/mpegts":
            return
        q = Gst.ElementFactory.make("queue")
        self.pipeline.add(q)
        q.sync_state_with_parent()
        pad.link(q.get_static_pad("sink"))
        mpad = self.mux.request_pad_simple("sink_%d")
        if mpad is not None:
            q.get_static_pad("src").link(mpad)

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self.pipeline.send_event(Gst.Event.new_eos())
        GLib.timeout_add_seconds(2, lambda: self.pipeline.set_state(Gst.State.NULL) and False)


class TunerManager(GObject.Object):
    """Hands out Captures; shares one tuner between live viewing and recording."""

    __gsignals__ = {
        # emitted when the live view must give up its tuner for a recording
        "preempt-live": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.captures = {}           # adapter -> Capture

    def acquire(self, channel, user):
        for cap in self.captures.values():
            if cap.channel.key == channel.key:
                cap.users.add(user)
                return cap
        adapters = self.settings["adapters"] or [0]
        free = [a for a in adapters if a not in self.captures]
        if not free:
            if user == "live":
                live = [a for a, c in self.captures.items() if c.users == {"live"}]
                if not live:
                    raise RuntimeError("All tuners are busy recording.")
                self.release(self.captures[live[0]], "live")
                free = live
            else:
                live = [a for a, c in self.captures.items() if c.users == {"live"}]
                if not live:
                    raise RuntimeError("No free tuner for the recording.")
                self.emit("preempt-live")
                cap = self.captures.get(live[0])
                if cap:
                    self.release(cap, "live")
                free = live
        a = free[0]
        cap = Capture(channel, a, self.settings["frontend"], self.settings)
        cap.users.add(user)
        self.captures[a] = cap
        cap.start()
        return cap

    def release(self, cap, user):
        cap.users.discard(user)
        if not cap.users:
            self.captures.pop(cap.adapter, None)
            cap.destroy()

    def shutdown(self):
        for cap in list(self.captures.values()):
            cap.destroy()
        self.captures.clear()


def check_dvb_tools():
    """Report which optional Linux DVB components are present."""
    return {
        "dvbsrc": Gst.ElementFactory.find("dvbsrc") is not None,
        "teletextdec": Gst.ElementFactory.find("teletextdec") is not None,
        "gtk4paintablesink": Gst.ElementFactory.find("gtk4paintablesink") is not None,
        "dvbv5-scan": shutil.which("dvbv5-scan") is not None,
        "devices": os.path.isdir("/dev/dvb"),
    }
