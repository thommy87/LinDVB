# SPDX-License-Identifier: GPL-2.0-or-later
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
from .epgsources import ProviderEPG, provider_pids, provider_types_for  # noqa: E402
from . import tunerprofiles  # noqa: E402

try:
    gi.require_version("GstMpegts", "1.0")
    from gi.repository import GstMpegts
    GstMpegts.initialize()
except (ImportError, ValueError):
    GstMpegts = None

SEGMENT_BYTES = 64 * 1024 * 1024
# ATSC terrestrial (8VSB) and US cable (QAM annex B): the guide is in PSIP
ATSC_SYSTEMS = ("atsc", "dvb-c-b")
TS_PACKET = 188


def dvbsrc_properties(ch, settings, adapter, frontend, lnb=None):
    """Map a Channel onto dvbsrc properties (string values, set via util_set_object_arg).

    *lnb* is the tuner's (LOF low, LOF high, switch frequency), else the global one.
    """
    p = {"adapter": adapter, "frontend": frontend, "delsys": ch.delsys}
    if ch.is_sat:
        lof1, lof2, slof = lnb or tunerprofiles.tuner_lnb(None, settings)
        p.update({"frequency": ch.frequency, "polarity": ch.polarity.upper() or "H",
                  "symbol-rate": ch.symbol_rate,
                  "lnb-lof1": lof1, "lnb-lof2": lof2, "lnb-slof": slof})
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
    software_ca = ch.encrypted and settings.get("ca_provider", "none") not in ("none", "ci", "")
    # ATSC: the PSIP guide PIDs are only known from the MGT, so take the whole stream
    if settings["full_transponder"] or not ch.pmt_pid or software_ca or ch.delsys in ATSC_SYSTEMS:
        # whole transponder: also when a softcam descrambles, so that the ECM
        # PIDs (not in the channel list) reach it
        p["pids"] = "8192"
    else:
        # also receive the PIDs of a provider EPG (MediaHighway / Freesat)
        # when this transponder is in the user's list
        extra = provider_pids(provider_types_for(ch, settings.get("epg_provider_freqs", [])))
        p["pids"] = ":".join(map(str, sorted(set(ch.pids()) | set(extra))))
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
        # the private guide of a platform (MediaHighway 1/2, Freesat), keyed like "eit"
        "provider-epg": (GObject.SignalFlags.RUN_LAST, None, (str, object)),
        # the transport stream id from the PAT, when the channel list lacks it
        "tsid": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        # HbbTV: a new version of the service's AIT (lindvb.ait.Ait)
        "ait": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        # HbbTV: the AIT PID from the PMT, when it is not yet in the channel list
        "ait-pid": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(self, channel, adapter, frontend, settings, ca_provider=None, tuner=None):
        super().__init__()
        self.channel = channel
        self.adapter = adapter
        self.frontend = frontend
        # the tuner profile (lindvb.tunerprofiles), and the keys of all channels
        # this capture carries (more than one when they share the transponder)
        self.tuner = tuner or tunerprofiles.new_profile(adapter, frontend)
        self.channels = {channel.key: channel}
        # Conditional access for the user's own encrypted channels: a provider
        # supplies control words (from an external softcam or a CI module) and
        # descrambles the captured transport stream in place.
        self.ca = ca_provider if (ca_provider and channel.encrypted) else None
        self._pmt_asm = None
        self._pat_asm = None
        self._pmt_pid = channel.pmt_pid
        self.epg_language = settings.get("epg_language", "")
        self.tsid = channel.tsid
        # ATSC carries its guide in PSIP tables instead of DVB EIT
        self._atsc = None
        if channel.delsys in ATSC_SYSTEMS:
            from .atsc import AtscGuide
            self._atsc = AtscGuide(self.epg_language)
        base = Path(settings["timeshift_dir"])
        base.mkdir(parents=True, exist_ok=True)
        self.store = SegmentStore(tempfile.mkdtemp(prefix=f"a{adapter}-", dir=base))
        self.users = set()
        self.started_at = time.time()
        self.stats = {}

        self.pipeline = Gst.Pipeline.new(f"capture{adapter}")
        self._src = None                 # dvbsrc, whose PID filter may grow (AIT)
        self._pids = ""                  # the PIDs set on it ("8192" = all)
        self.ait = None                  # the latest AIT of the service (HbbTV)
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
            props = dvbsrc_properties(channel, settings, adapter, frontend,
                                      tunerprofiles.tuner_lnb(self.tuner, settings))
            for k, v in props.items():
                Gst.util_set_object_arg(src, k, str(v))
            self._src = src
            self._pids = str(props.get("pids", ""))
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

        if self.ca is not None:
            # Descramble the stream just before it is written to disk, so the
            # live view, timeshift and recordings all get the clear stream.
            from .ca import SectionAssembler
            self._pmt_asm = SectionAssembler()
            if not self._pmt_pid:
                self._pat_asm = SectionAssembler()     # find the PMT PID in the PAT
            self.ca.start(channel)
            pad = sink.get_static_pad("sink")
            pad.add_probe(Gst.PadProbeType.BUFFER, self._ca_probe)

        # EPG of other providers (MediaHighway 1/2, Freesat) on this transponder
        self.provider_epg = None
        kinds = provider_types_for(channel, settings.get("epg_provider_freqs", []))
        if kinds:
            self.provider_epg = ProviderEPG(kinds, self._on_provider_events, self.epg_language)
            parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._epg_probe)

        # HbbTV: watch the PMT for the AIT and the AIT for applications
        self.ait_monitor = None
        if settings.get("hbbtv_enabled", True) and channel.sid and channel.delsys not in ATSC_SYSTEMS:
            from .ait import AitMonitor
            self.ait_monitor = AitMonitor(channel.sid, self._pmt_pid, self._on_ait, self._on_ait_pids)
            parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._ait_probe)

        # channel analysis (lindvb.analysis): callables that get every buffer of
        # the transport stream; the list is replaced, never changed in place
        self._taps = ()
        self._tap_probe = None
        self._parse = parse
        self._saved_pids = None          # PID filter while the whole TS is measured

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    # -- channel analysis ---------------------------------------------------
    def add_tap(self, fn):
        """Hand every buffer of the transport stream (bytes) to fn, in the
        streaming thread, until remove_tap(fn)."""
        self._taps = tuple(self._taps) + (fn,)
        if self._tap_probe is None:
            pad = self._parse.get_static_pad("src")
            self._tap_probe = pad.add_probe(Gst.PadProbeType.BUFFER, self._run_taps)

    def remove_tap(self, fn):
        self._taps = tuple(t for t in self._taps if t is not fn)
        if not self._taps and self._tap_probe is not None:
            self._parse.get_static_pad("src").remove_probe(self._tap_probe)
            self._tap_probe = None

    def _run_taps(self, pad, info):
        taps = self._taps
        buf = info.get_buffer()
        if buf is not None and taps:
            data = buf.extract_dup(0, buf.get_size())
            for fn in taps:
                try:
                    fn(data)
                except Exception as e:           # noqa: BLE001 - never stop the capture
                    print(f"Stream analysis: {e}")
        return Gst.PadProbeReturn.OK

    @property
    def pid_filter(self):
        """The PIDs dvbsrc passes ("8192" = the whole transponder), or None (file)."""
        return self._pids if self._src is not None else None

    def set_full_ts(self, on):
        """Receive the whole transponder (on) or go back to the service PIDs."""
        if self._src is None:
            return False
        if on and self._saved_pids is None and "8192" not in self._pids.split(":"):
            self._saved_pids = self._pids
            self._pids = "8192"
            self._src.set_property("pids", "8192")
            return True
        if not on and self._saved_pids is not None:
            self._pids, self._saved_pids = self._saved_pids, None
            self._src.set_property("pids", self._pids)
            return True
        return False

    def _epg_probe(self, pad, info):
        buf = info.get_buffer()
        if buf is not None and self.provider_epg is not None:
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                try:
                    self.provider_epg.feed_buffer(bytes(mapinfo.data))
                finally:
                    buf.unmap(mapinfo)
        return Gst.PadProbeReturn.OK

    def _ait_probe(self, pad, info):
        buf = info.get_buffer()
        if buf is not None and self.ait_monitor is not None:
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                try:
                    self.ait_monitor.feed_buffer(bytes(mapinfo.data))
                finally:
                    buf.unmap(mapinfo)
        return Gst.PadProbeReturn.OK

    def _on_ait(self, ait):
        # streaming thread -> main loop
        def deliver():
            self.ait = ait
            self.emit("ait", ait)
            return False
        GLib.idle_add(deliver)

    def add_pid(self, pid):
        """Add *pid* to the demux filter while running (main loop): the AIT,
        and object carousels and stream events for HbbTV."""
        if not pid or self._src is None:
            return
        have = {int(x) for x in self._pids.split(":") if x.isdigit()}
        if have and 8192 not in have and pid not in have:
            self._pids = ":".join(map(str, sorted(have | {pid})))
            self._src.set_property("pids", self._pids)

    def add_channel(self, channel):
        """Carry *channel* too: it is on this transponder (main loop)."""
        self.channels[channel.key] = channel
        if self._src is None:
            return
        have = {int(x) for x in self._pids.split(":") if x.isdigit()}
        if not have or 8192 in have:
            return                       # whole transponder already
        want = have | set(channel.pids())
        if not channel.pmt_pid:
            want = {8192}                # stream layout unknown: take everything
        if want != have:
            self._pids = ":".join(map(str, sorted(want)))
            self._src.set_property("pids", self._pids)

    def _on_ait_pids(self, pids):
        def deliver():
            pid = pids[0] if pids else 0
            self.add_pid(pid)
            if pid and pid != self.channel.ait_pid:
                self.emit("ait-pid", pid)
            return False
        GLib.idle_add(deliver)

    def _on_provider_events(self, key, events):
        # called from the streaming thread; hand over to the main loop
        GLib.idle_add(lambda: (self.emit("provider-epg", key, events), False)[1])

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.emit("error", f"Could not open DVB adapter {self.adapter}.")

    def _ca_probe(self, pad, info):
        """Run the CA provider over every TS packet, descrambling in place."""
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        size = buf.get_size()
        if size < TS_PACKET:
            return Gst.PadProbeReturn.OK
        data = bytearray(buf.extract_dup(0, size))
        if self._process_ts(data):
            try:
                buf.fill(0, bytes(data))
            except Exception:
                pass
        return Gst.PadProbeReturn.OK

    def _process_ts(self, data):
        """Descramble the packets in ``data`` (a bytearray). Returns True if written."""
        from .ca import TS_SYNC, pat_pmt_pid, ts_pid
        n = len(data)
        pmt_pid = self._pmt_pid
        changed = False
        aligned = (n % TS_PACKET == 0 and data[0] == TS_SYNC)
        offsets = range(0, n - TS_PACKET + 1, TS_PACKET) if aligned else \
            self._sync_offsets(data)
        for off in offsets:
            packet = memoryview(data)[off:off + TS_PACKET]
            if packet[0] != TS_SYNC:
                continue
            if not pmt_pid and self._pat_asm is not None and ts_pid(packet) == 0:
                for section in self._pat_asm.feed(packet):
                    found = pat_pmt_pid(section, self.channel.sid) if section else 0
                    if found:
                        pmt_pid = self._pmt_pid = found
                        self.ca.pmt_pid = found
                        self._pat_asm = None
                        break
            if pmt_pid and self._pmt_asm is not None and ts_pid(packet) == pmt_pid:
                for section in self._pmt_asm.feed(packet):
                    if section and section[0] == 0x02:
                        self.ca.set_pmt(section)
                        self._pmt_asm = None
                        break
            if self.ca.process_packet(packet):
                changed = True
        return changed

    @staticmethod
    def _sync_offsets(data):
        from .ca import TS_SYNC
        offs, i, n = [], data.find(TS_SYNC), len(data)
        while 0 <= i and i + TS_PACKET <= n:
            offs.append(i)
            i += TS_PACKET if data[i] == TS_SYNC else 1
        return offs

    def stop(self):
        if self.ca is not None:
            self.ca.stop()
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
                st = {k: s.get_value(k) for k in ("signal", "snr", "ber", "unc", "lock",
                                                  "status") if s.has_field(k)}
                self.stats = st
                self.emit("stats", st)
            elif GstMpegts is not None:
                section = GstMpegts.message_parse_mpegts_section(msg)
                if section is not None:
                    if section.section_type == GstMpegts.SectionType.PAT and not self.tsid:
                        self.tsid = section.subtable_extension
                        self.emit("tsid", self.tsid)
                    if self._atsc is not None:
                        for key, events in self._atsc.feed(section, GstMpegts):
                            self.emit("eit", key, events)
                        return
                    res = parse_eit_section(section, GstMpegts, self.epg_language)
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


ANALYSIS = tunerprofiles.ANALYSIS   # TunerManager user of a channel analysis


def free_tuners(manager):
    """Adapters a channel analysis could tune without taking one from anybody."""
    test = bool(os.environ.get("LINDVB_TEST_TS"))
    out = []
    for p in manager.profiles():
        a = p["adapter"]
        if a not in manager.captures and a not in out and \
                (test or os.path.exists(f"/dev/dvb/adapter{a}")):
            out.append(a)
    return out


class TunerManager(GObject.Object):
    """Hands out Captures on the user's tuners (see lindvb.tunerprofiles).

    One tuner serves live viewing and recordings of the same channel, and of
    other channels on the same transponder.
    """

    __gsignals__ = {
        # emitted when the live view must give up its tuner for a recording
        "preempt-live": (GObject.SignalFlags.RUN_LAST, None, ()),
        # a channel analysis on a tuner of its own gave it up (the Capture)
        "preempt-analysis": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, settings, ca_factory=None, probe=None):
        super().__init__()
        self.settings = settings
        # Called with no arguments to build a fresh CA provider for an
        # encrypted channel, or None for no descrambling.
        self.ca_factory = ca_factory
        self.captures = {}           # adapter -> Capture
        self._probe = probe or tunerprofiles.probe_delivery_systems
        self._systems = {}           # (adapter, frontend) -> delivery systems

    def _systems_for(self, adapter, frontend):
        key = (adapter, frontend)
        if key not in self._systems:
            self._systems[key] = self._probe(adapter, frontend)
        return self._systems[key]

    def profiles(self):
        """The enabled tuners, in the order they are used."""
        return tunerprofiles.active_profiles(self.settings, self._systems_for, self._detect)

    def _detect(self):
        return [tunerprofiles.new_profile(a, f, name) for a, f, name in self._find()]

    @staticmethod
    def _find():
        from .scan import find_adapters
        return find_adapters()

    def _needs_own_descrambler(self, channel):
        return (channel.encrypted and self.ca_factory is not None and
                self.settings.get("ca_provider", "none") not in ("none", ""))

    def acquire(self, channel, user):
        try:
            how = tunerprofiles.choose(channel, user, self.profiles(), self.captures,
                                       self.settings,
                                       can_share=not self._needs_own_descrambler(channel))
        except tunerprofiles.NoTuner as e:
            raise RuntimeError(str(e)) from None
        if how[0] in ("join", "share"):
            cap = how[1]
            if how[0] == "share":
                cap.add_channel(channel)
            cap.users.add(user)
            return cap
        _tune, profile, preempt = how
        if preempt is not None:
            if preempt.users == {ANALYSIS}:
                self.emit("preempt-analysis", preempt)
                gone = ANALYSIS
            else:
                if user != "live":
                    self.emit("preempt-live")
                gone = "live"
            if preempt.adapter in self.captures:
                self.release(preempt, gone)
        provider = None
        if channel.encrypted and self.ca_factory is not None:
            try:
                provider = self.ca_factory()
            except Exception:
                provider = None
        a = profile["adapter"]
        cap = Capture(channel, a, int(profile.get("frontend", 0) or 0), self.settings, provider,
                      tuner=profile)
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
