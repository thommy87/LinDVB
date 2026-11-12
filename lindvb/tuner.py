# SPDX-License-Identifier: GPL-2.0-or-later
"""DVB capture with GStreamer's dvbsrc (Linux DVB API v5).

A Capture tunes one service and writes the transport stream into a rolling
set of segment files. The live player, the timeshift function and the
recorder all read from those segments, so one tuner feeds all of them.
"""

import os
import random
import shutil
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

from .epg import parse_eit_section  # noqa: E402
from .epgsources import ProviderEPG, provider_pids, provider_types_for  # noqa: E402
from . import directtune, ipstream, satip, tunerprofiles, tvheadend, unicable  # noqa: E402

try:
    gi.require_version("GstMpegts", "1.0")
    from gi.repository import GstMpegts
    GstMpegts.initialize()
except (ImportError, ValueError):
    GstMpegts = None

SEGMENT_BYTES = 64 * 1024 * 1024
# Unicable / JESS: seconds to wait for a lock before the command is sent again,
# and how often it is sent at most
UNICABLE_LOCK_WAIT = 4
UNICABLE_TRIES = 3
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


class ServiceView(GObject.Object):
    """HbbTV's view of one more service on a shared capture: the AIT of a
    channel watched while the tuner is tuned for another (a recording)."""

    __gsignals__ = {
        "ait": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, capture, channel):
        super().__init__()
        from .ait import AitMonitor
        self.capture = capture
        self.channel = channel
        self.ait = None
        self.ait_monitor = AitMonitor(channel.sid, channel.pmt_pid, self._on_ait,
                                      self._on_pids)

    def add_pid(self, pid):
        self.capture.add_pid(pid)

    def _on_ait(self, ait):
        def deliver():
            self.ait = ait
            self.emit("ait", ait)
            return False
        GLib.idle_add(deliver)

    def _on_pids(self, pids):
        GLib.idle_add(lambda: (self.add_pid(pids[0] if pids else 0), False)[1])


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
        # internet streams and Tvheadend: (kind, message) while the connection
        # is opened again; kind "reconnecting" or "reconnected"
        "connection": (GObject.SignalFlags.RUN_LAST, None, (str, str)),
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
        # Unicable / JESS: LinDVB tunes the frontend itself (lindvb.directtune)
        # and reads the stream from the DVR device instead of dvbsrc.
        self._uc = unicable.config(self.tuner) if channel.is_sat else None
        # SAT>IP: a server on the network tunes; the stream comes over RTP
        self._satip = None
        # Tvheadend: the server tunes; LinDVB reads the service's stream over HTTP
        self._tvh = None
        # internet streams (lindvb.ipstream) and Tvheadend: the reader of the stream
        self._net = None
        if satip.is_satip(self.tuner) or tvheadend.is_tvheadend(self.tuner) or \
                ipstream.is_stream(self.tuner):
            self._uc = None
        self._lnb = tunerprofiles.tuner_lnb(self.tuner, settings)
        self._direct = None
        self._uc_tries = 0
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
            props = dvbsrc_properties(channel, settings, adapter, frontend, self._lnb)
            reconnect = bool(settings.get("stream_reconnect", True))
            if tvheadend.is_tvheadend(self.tuner):
                src = self._appsrc()
                self._tvh = self._net = tvheadend.StreamReader(
                    channel.url, on_data=self._push_direct, on_error=self._on_tvh_error,
                    on_status=self._on_net_status, reconnect=reconnect)
            elif ipstream.is_stream(self.tuner):
                src = self._appsrc()
                self._net = ipstream.StreamSource(
                    channel.url, on_data=self._push_direct, on_error=self._on_net_error,
                    on_status=self._on_net_status, reconnect=reconnect,
                    quality=settings.get("iptv_timeshift_quality", "auto"))
            elif satip.is_satip(self.tuner):
                src = self._appsrc()
                self._satip = satip.Session(self.tuner["host"], self.tuner.get("port"),
                                            self.tuner.get("transport", "udp"),
                                            on_data=self._push_direct,
                                            on_status=self._on_satip_status)
            elif self._uc is not None:
                # the DVR device is read by a thread (see _start_direct)
                src = self._appsrc()
                self._direct = directtune.DirectTuner(adapter, frontend)
            else:
                src = Gst.ElementFactory.make("dvbsrc", "src")
                if src is None:
                    raise RuntimeError("GStreamer element 'dvbsrc' is missing (gst-plugins-bad).")
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
        self._views = {}                 # channel key -> ServiceView (shared services)
        # (a Tvheadend or internet stream has one service: its id comes with the PAT)
        if settings.get("hbbtv_enabled", True) and (channel.sid or self._net is not None) and \
                channel.delsys not in ATSC_SYSTEMS:
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

    @staticmethod
    def _appsrc():
        """The source for a stream LinDVB reads itself (single cable, SAT>IP)."""
        src = Gst.ElementFactory.make("appsrc", "src")
        src.set_property("caps", Gst.Caps.from_string(
            "video/mpegts,systemstream=true,packetsize=188"))
        Gst.util_set_object_arg(src, "format", "bytes")
        src.set_property("is-live", True)
        src.set_property("do-timestamp", True)
        return src

    @property
    def source_label(self):
        """Where the stream comes from, for the user."""
        if satip.is_satip(self.tuner):
            return f"SAT>IP {self.tuner.get('host', '')}"
        if tvheadend.is_tvheadend(self.tuner):
            return self.tuner.get("name", "Tvheadend")
        if ipstream.is_stream(self.tuner):
            return self.tuner.get("name", "")
        return f"adapter{self.adapter}/frontend{self.frontend}"

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
                    data = bytes(mapinfo.data)
                    self.ait_monitor.feed_buffer(data)
                    for view in tuple(self._views.values()):
                        view.ait_monitor.feed_buffer(data)
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
            self._set_pids(":".join(map(str, sorted(have | {pid}))))

    def _set_pids(self, pids):
        self._pids = pids
        if self._satip is not None:
            self._satip.set_pids(pids)
        elif self._direct is not None:
            if self._direct.fe is not None:
                try:
                    self._direct.set_pids([int(x) for x in pids.split(":") if x.isdigit()])
                except OSError as e:
                    self.emit("error", f"Could not set the PID filter: {e.strerror}")
        else:
            self._src.set_property("pids", pids)

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
            self._set_pids(":".join(map(str, sorted(want))))

    def can_switch_to(self, channel):
        """True if live TV may move this capture to *channel* without
        retuning: same transponder, clear, no descrambler, only live TV."""
        return (self.users == {"live"} and self.ca is None and not channel.encrypted
                and channel.key != self.channel.key
                and tunerprofiles.same_transponder(self.channel, channel))

    def service_view(self, channel):
        """HbbTV signalling of *channel*, another service this capture
        carries: a ServiceView, or None (HbbTV off, ATSC, no service id)."""
        if self.ait_monitor is None or not channel.sid or channel.key not in self.channels:
            return None
        view = self._views.get(channel.key)
        if view is None:
            view = ServiceView(self, channel)
            self._views = dict(self._views, **{channel.key: view})
        return view

    def drop_service_view(self, key):
        if key in self._views:
            self._views = {k: v for k, v in self._views.items() if k != key}

    def switch_service(self, channel):
        """Make *channel* (on this transponder) the service of the capture
        (main loop): its PIDs join the filter, HbbTV follows its AIT."""
        self.add_channel(channel)
        self.channel = channel
        self._pmt_pid = channel.pmt_pid
        self.ait = None
        self._views = {}
        if self.ait_monitor is not None and channel.sid:
            from .ait import AitMonitor
            self.ait_monitor = AitMonitor(channel.sid, self._pmt_pid, self._on_ait,
                                          self._on_ait_pids)

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
        if self._net is not None:
            self._net.start()
        elif self._satip is not None:
            self._start_satip()
        elif self._direct is not None and not self._start_direct():
            return
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.emit("error", f"Could not open DVB adapter {self.adapter}.")

    def _start_direct(self):
        """Single cable: open the frontend, send the command, tune the user band."""
        try:
            self._direct.open()
            self._direct.tune(self._uc, self.channel, self._lnb)
            self._direct.set_pids([int(x) for x in self._pids.split(":") if x.isdigit()])
        except (OSError, ValueError) as e:
            self._direct.close()
            msg = e.strerror if isinstance(e, OSError) else str(e)
            self.emit("error", f"Could not tune adapter {self.adapter} (single cable): {msg}")
            return False
        self._uc_tries = 1
        import threading
        threading.Thread(target=self._direct.read_loop, args=(self._push_direct,),
                         daemon=True, name=f"dvr{self.adapter}").start()
        from .festats import Frontend
        self._fe_stats = Frontend(self.adapter, self.frontend)
        GLib.timeout_add_seconds(1, self._poll_direct)
        return True

    def _start_satip(self):
        """SAT>IP: SETUP and PLAY on the server, in a thread (network)."""
        import threading
        pids = self._pids
        try:
            query = satip.tuning_query(self.channel, pids or "all")
        except satip.SatipError as e:
            msg = str(e)
            GLib.idle_add(lambda: (self.emit("error", msg), False)[1])
            return
        sess = self._satip

        def run():
            try:
                sess.open(query)
            except satip.SatipError as e:
                msg = str(e)
                if not self.store.finished:
                    GLib.idle_add(lambda: (self.emit("error", msg), False)[1])
                return
            if self.store.finished:
                sess.close()
                return
            if self._pids != pids:           # the filter changed meanwhile
                sess.set_pids(self._pids)
            GLib.timeout_add_seconds(2, self._watch_satip)
        threading.Thread(target=run, daemon=True, name=f"satip{self.adapter}").start()

    def _watch_satip(self):
        """Report a lost connection to the SAT>IP server."""
        if self.store.finished or self._satip is None:
            return False
        if self._satip.dead:
            self.emit("error", f"SAT>IP {self._satip.host}: "
                      f"{self._satip.error or 'the connection was lost'}")
            return False
        return True

    def _on_satip_status(self, st):
        """Receiving thread: the signal figures from RTCP."""
        def deliver():
            if not self.store.finished:
                self.stats = st
                self.emit("stats", st)
            return False
        GLib.idle_add(deliver)

    def _on_tvh_error(self, msg):
        """Reader thread: the server refused the stream or the connection broke."""
        def deliver():
            if not self.store.finished:
                self.emit("error", f"Tvheadend: {msg}")
            return False
        GLib.idle_add(deliver)

    def _on_net_error(self, msg):
        """Reader: the stream could not be opened, or reconnecting gave up."""
        def deliver():
            if not self.store.finished:
                self.emit("error", msg)
            return False
        GLib.idle_add(deliver)

    def _on_net_status(self, kind, msg):
        """Reader: the connection broke and is opened again, or is back."""
        def deliver():
            if not self.store.finished:
                self.emit("connection", kind, msg)
            return False
        GLib.idle_add(deliver)

    @property
    def received(self):
        """Bytes read from the network (internet streams and Tvheadend)."""
        return self._net.received if self._net is not None else 0

    def _learn_service(self, section):
        """Tvheadend: the PAT names the one service of the stream. Keep its ids
        on the channel, so the broadcast EIT finds it (Channel.epg_links)."""
        try:
            programs = [p.program_number for p in section.get_pat() if p.program_number]
        except (TypeError, AttributeError):
            return
        tsid = section.subtable_extension
        if programs and (self.channel.sid, self.channel.tsid) != (programs[0], tsid):
            self.channel.sid, self.channel.tsid = programs[0], tsid
            self.tsid = tsid
            self.emit("tsid", tsid)

    def _push_direct(self, data):
        """Reader thread: hand a chunk of the DVR stream to the pipeline."""
        if not self.store.finished:
            self._src.emit("push-buffer", Gst.Buffer.new_wrapped(data))

    def _poll_direct(self):
        """Signal figures for the single-cable tuner (dvbsrc would post them),
        and the command again without a lock: another receiver on the cable may
        have sent at the same moment and the command got lost."""
        if self.store.finished:
            self._fe_stats.close()
            return False
        st = directtune.dvb_stats(self._fe_stats.read())
        self.stats = st
        self.emit("stats", st)
        waited = time.time() - self.started_at
        if not st.get("lock") and self._uc_tries < UNICABLE_TRIES and \
                waited >= UNICABLE_LOCK_WAIT * self._uc_tries:
            time.sleep(random.uniform(0.05, 0.3))
            try:
                self._direct.tune(self._uc, self.channel, self._lnb)
            except (OSError, ValueError):
                pass
            self._uc_tries += 1
        return True

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
        if self._direct is not None:
            self._direct.close()
        if self._satip is not None:
            self._satip.close()
        if self._net is not None:
            self._net.close()

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
                    if section.section_type == GstMpegts.SectionType.PAT and \
                            self._tvh is not None:
                        self._learn_service(section)
                    elif section.section_type == GstMpegts.SectionType.PAT and not self.tsid:
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
        from .tvheadend import set_login
        self.src.connect("source-setup", lambda _b, el: set_login(el, url))
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
    profiles = manager.profiles() if hasattr(manager, "profiles") else \
        tunerprofiles.active_profiles(manager.settings)
    for p in profiles:
        a = p["adapter"]
        if a not in manager.captures and a not in out and \
                (test or satip.is_satip(p) or os.path.exists(f"/dev/dvb/adapter{a}")):
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
        if channel.tvh_server:
            return self._acquire_tvheadend(channel, user)
        if channel.is_iptv:
            return self._acquire_stream(channel, user)
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

    def _acquire_tvheadend(self, channel, user):
        """A Tvheadend channel: one stream (capture) per channel, shared by the
        live view and recordings of that channel. The server decides whether
        it has a tuner free; it refuses the stream otherwise."""
        for cap in self.captures.values():
            if tvheadend.is_tvheadend(cap.tuner) and cap.channel.key == channel.key:
                cap.users.add(user)
                return cap
        a = tvheadend.free_adapter(self.captures)
        cap = Capture(channel, a, 0, self.settings, tuner=tvheadend.stream_profile(a, channel))
        cap.users.add(user)
        self.captures[a] = cap
        cap.start()
        return cap

    def _acquire_stream(self, channel, user):
        """An internet channel read into a capture (lindvb.ipstream): one
        capture per channel, shared by the live view and its recordings."""
        for cap in self.captures.values():
            if ipstream.is_stream(cap.tuner) and cap.channel.key == channel.key:
                cap.users.add(user)
                return cap
        a = ipstream.free_adapter(self.captures)
        cap = Capture(channel, a, 0, self.settings, tuner=ipstream.stream_profile(a, channel))
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
