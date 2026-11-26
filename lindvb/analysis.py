# SPDX-License-Identifier: GPL-2.0-or-later
"""Measuring session of the channel analysis (right click on a channel ▸ Channel Analysis).

An AnalysisSession measures one channel once a second and keeps the last
fifteen minutes:

* DVB: the frontend statistics (lindvb.festats, dvbsrc's figures as fallback),
  the reception margin (lindvb.linkbudget) and a TsAnalyzer on the tuner's
  transport stream (bit rate per PID, errors, tables, codecs). It uses the
  tuner that shows the channel, or a free tuner of its own ("background
  analysis"), which gives way to watching and recording (TunerManager).
* IPTV: the playlist and server (lindvb.streaminfo), the player's buffering,
  fragment downloads and, for MPEG-TS streams, a TsAnalyzer on what reaches
  the demuxer. A channel that is not playing is fetched by a small pipeline
  of its own.

For the channel on screen it also reads the decoded picture (size, scan,
colour, decoder, black bars) and the loudness (lindvb.loudness).

ReceptionMonitor runs for the live channel without the window: the optional
reception log (lindvb.receptionlog) and the warnings when the margin gets
small or the picture breaks up.
"""

import json
import math
import os
import threading
import time
from collections import deque

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

from . import linkbudget
from .festats import Frontend
from .i18n import _
from .receptionlog import ReceptionLog
from .signalq import _fraction, dvb_quality
from .tsanalysis import NULL_PID, TS_PACKET, TsAnalyzer, ca_system_name, pid_owners
from .tuner import ANALYSIS, SegmentReader, free_tuners

HISTORY_SECONDS = 900
PID_HISTORY = 60
TRANSPONDER_SECONDS = 10
BARS_EVERY = 3.0
IPTV_PROBE_EVERY = 120.0


def stream_rate(buf, tag_bps=None):
    """Bit rate of an internet stream (bit/s) for the bit rate figures, or None.

    The bit rate the stream announces comes first (internet radio). Else what
    the network buffer hands on to playback: what it takes in is the download,
    which runs far above the stream's rate while the buffer fills up.
    """
    if tag_bps:
        return tag_bps
    buf = buf or {}
    for key in ("out", "in"):
        v = buf.get(key, -1)
        if v is not None and v > 0:
            return v * 8
    return None


def no_playlist(probe):
    """The playlist line of a stream without a playlist LinDVB could read."""
    ctype = (probe.get("content_type") or "").lower()
    if "mpegurl" in ctype or "dash" in ctype:
        return _("unreadable (the server announces a playlist)")
    return _("none (a plain stream)")


def _fmt_rate(bps):
    if bps is None:
        return "–"
    if bps >= 1e6:
        return f"{bps / 1e6:.2f} Mbit/s"
    return f"{bps / 1e3:.0f} kbit/s"


def signal_sample(stats, fe=None, tuning=None, channel=None):
    """One measurement of the reception from dvbsrc's *stats* and the frontend's *fe*
    (lindvb.festats.Frontend.read()). Values that are not known are None."""
    stats = stats or {}
    fe = fe or {}
    tuning = tuning or {}
    out = {"quality": dvb_quality(stats) if stats else 0.0}
    status = fe.get("status")
    if status is None and "status" in stats:
        bits = int(stats["status"])
        status = {"signal": bool(bits & 1), "carrier": bool(bits & 2), "viterbi": bool(bits & 4),
                  "sync": bool(bits & 8), "lock": bool(bits & 16)}
    if status is None and "lock" in stats:
        status = {"lock": bool(stats["lock"])}
    out["status"] = status or {}
    out["lock"] = bool((status or {}).get("lock", stats.get("lock", False)))
    cnr = fe.get("cnr_db")
    if cnr is None:
        cnr = linkbudget.snr_db_from_dvbsrc(stats.get("snr"))
    out["cnr_db"] = cnr
    out["snr_rel"] = fe.get("cnr_rel", _fraction(stats.get("snr")) if stats.get("snr") else None)
    out["level_dbm"] = fe.get("signal_dbm")
    out["level_rel"] = fe.get("signal_rel", _fraction(stats.get("signal"))
                              if stats.get("signal") else None)
    out["ber"] = fe.get("pre_ber", fe.get("post_ber"))
    out["post_ber"] = fe.get("post_ber")
    out["ber_raw"] = stats.get("ber")
    out["unc"] = stats.get("unc", fe.get("block_errors"))
    delsys = tuning.get("delsys") or (channel.delsys if channel else "")
    mod = tuning.get("modulation") or (channel.modulation if channel else "")
    fec = tuning.get("fec") or tuning.get("code_rate_hp") or (channel.fec if channel else "")
    out["required_cn"] = linkbudget.required_cn(delsys, mod, fec)
    out["margin"] = linkbudget.margin(cnr, delsys, mod, fec)
    return out


class ReceptionMonitor:
    """Log and warnings for the live channel (works without the analysis window)."""

    WARN_EVERY = 60.0

    def __init__(self, app):
        self.app = app
        self.log = ReceptionLog()
        self._fe = None
        self._fe_cap = None
        self._tuning = {}
        self._unc = None
        self._lock = True
        self._warned = 0.0

    @property
    def active(self):
        s = self.app.settings
        return bool(s.get("reception_log") or s.get("analysis_warn"))

    def feed(self, cap, stats, window):
        s = self.app.settings
        if not self.active:
            if self._fe is not None:
                self.close()
            return
        if cap is not self._fe_cap:
            self.close()
            self._fe_cap = cap
            if not os.environ.get("LINDVB_TEST_TS"):
                self._fe = Frontend(cap.adapter, getattr(cap, "frontend", s.get("frontend", 0)))
                self._tuning = self._fe.tuning()
        fe = self._fe.read() if self._fe is not None else {}
        sm = signal_sample(stats, fe, self._tuning, cap.channel)
        if s.get("reception_log"):
            self.log.feed(cap.channel, sm["quality"], sm["cnr_db"], sm["ber"], sm["unc"],
                          sm["lock"])
        if s.get("analysis_warn") and window is not None:
            self._warn(sm, window)

    def _warn(self, sm, window):
        now = time.monotonic()
        msg = None
        limit = float(self.app.settings.get("analysis_warn_margin", 2.0))
        unc = sm["unc"]
        if unc is not None and self._unc is not None and unc > self._unc:
            msg = _("Picture errors: {n} blocks could not be corrected").format(n=unc - self._unc)
        elif sm["margin"] is not None and sm["margin"] < limit:
            msg = _("Weak reception: only {db} dB margin").format(db=f"{sm['margin']:.1f}")
        elif self._lock and not sm["lock"]:
            msg = _("Signal lost")
        self._unc = unc
        self._lock = sm["lock"]
        if msg and now - self._warned >= self.WARN_EVERY:
            self._warned = now
            window.set_status(msg)
            window.flash(msg)

    def close(self):
        if self._fe is not None:
            self._fe.close()
        self._fe = None
        self._fe_cap = None
        self._unc = None

    def shutdown(self):
        self.log.flush()
        self.close()


class IptvProbe:
    """Fetches an internet channel that is not playing, without decoding it:
    urisourcebin ! typefind ! fakesink, the TS (if it is one) to a tap."""

    def __init__(self, url, tap):
        self.tap = tap
        self.bytes = 0
        self.caps = ""
        self.error = ""
        self.pipeline = Gst.Pipeline.new("analysis-probe")
        src = Gst.ElementFactory.make("urisourcebin")
        if src is None:
            self.error = "urisourcebin missing"
            return
        src.set_property("uri", url)
        self.pipeline.add(src)
        src.connect("pad-added", self._pad_added)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)

    def _pad_added(self, _src, pad):
        tf = Gst.ElementFactory.make("typefind")
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", False)
        sink.set_property("async", False)
        for e in (tf, sink):
            self.pipeline.add(e)
            e.sync_state_with_parent()
        tf.link(sink)
        tf.connect("have-type", lambda _t, _p, caps: setattr(self, "caps", caps.to_string()))
        pad.link(tf.get_static_pad("sink"))
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._probe)

    def _probe(self, _pad, info):
        buf = info.get_buffer()
        if buf is not None:
            self.bytes += buf.get_size()
            if self.caps.startswith("video/mpegts"):
                try:
                    self.tap(buf.extract_dup(0, buf.get_size()))
                except Exception as e:      # noqa: BLE001
                    print(f"Stream analysis: {e}")
        return Gst.PadProbeReturn.OK

    def _on_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        self.error = err.message

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.error = self.error or "could not start"

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_bus().remove_signal_watch()


class AnalysisSession(GObject.Object):
    """Measures one channel (see module doc). Emits "updated" once a second."""

    __gsignals__ = {
        "updated": (GObject.SignalFlags.RUN_LAST, None, ()),
        "channel-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "message": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self, app, channel, follow=False):
        super().__init__()
        self.app = app
        self.channel = channel
        self.follow = follow
        self.paused = False
        self.window_seconds = 300
        self.analyzer = None
        self.capture = None
        self.own_capture = False
        self.frontend = None
        self.tuning = {}
        self.iptv_probe = None
        self.stream = {}                # streaminfo.probe() result
        self._tapped_player = False
        self._tap = None                # the player whose transport stream feeds the analyser
        self._preempt_id = app.tuners.connect("preempt-analysis", self._on_preempt)
        self._timer = GLib.timeout_add(1000, self._tick)
        self.closed = False
        self.start()

    # -- life cycle ---------------------------------------------------------
    @property
    def window(self):
        return self.app.window

    @property
    def player(self):
        return self.window.player if self.window is not None else None

    def is_on_screen(self):
        w = self.window
        return bool(w is not None and w.current is not None and
                    w.current.key == self.channel.key and w.player.playbin is not None)

    def _reset(self):
        self.history = deque(maxlen=HISTORY_SECONDS)
        self.pid_history = {}
        self.events = deque(maxlen=400)
        self.state = {}
        self.started = time.time()
        self._prev_counts = {}
        self._prev_time = None
        self._prev_unc = None
        self._prev_cc = 0
        self._prev_lock = None
        self._prev_rebuffers = None
        self._received = deque()        # (monotonic, bytes) of the stream's capture
        self._seen_events = 0
        self._loud_jumps = 0
        self._bars_at = 0.0
        self.bars = None
        self.transponder = None
        self._tp_until = None
        self._tp_counts = None
        self.status = "starting"
        self._status_detail = ""

    def start(self):
        self._reset()
        c = self.channel
        self.analyzer = TsAnalyzer(sid=c.sid)
        if c.is_iptv:
            self.kind = "iptv"
            if self.is_on_screen():
                self._tap_player()
            else:
                self.iptv_probe = IptvProbe(c.url, self.analyzer.feed)
                self.iptv_probe.start()
                self.status = "background"
            self._probe_stream()
        else:
            self.kind = "dvb"
            self._attach_capture()
        if self.player is not None and self.is_on_screen():
            self.player.measure_loudness = True
        self.emit("channel-changed")

    def _attach_capture(self):
        c = self.channel
        cap = next((x for x in self.app.tuners.captures.values() if x.channel.key == c.key), None)
        self.own_capture = False
        if cap is None:
            if not free_tuners(self.app.tuners):
                self.status = "no_tuner"
                return
            try:
                cap = self.app.tuners.acquire(c, ANALYSIS)
                self.own_capture = True
            except Exception as e:          # noqa: BLE001 - busy tuner, DVB errors
                self.status = "no_tuner"
                self._status_detail = str(e)
                return
        self.capture = cap
        cap.add_tap(self.analyzer.feed)
        self.status = "background" if cap.users == {ANALYSIS} else "live"
        if not os.environ.get("LINDVB_TEST_TS"):
            self.frontend = Frontend(cap.adapter, getattr(cap, "frontend", self.app.settings.get("frontend", 0)))
            self.tuning = self.frontend.tuning()

    def _detach(self):
        if self.capture is not None:
            self.capture.remove_tap(self.analyzer.feed)
            self.capture.set_full_ts(False)
            if self.own_capture:
                self.app.tuners.release(self.capture, ANALYSIS)
        self.capture = None
        self.own_capture = False
        if self.frontend is not None:
            self.frontend.close()
            self.frontend = None
        self._untap_player()
        if self.iptv_probe is not None:
            self.iptv_probe.stop()
            self.iptv_probe = None

    def set_channel(self, channel):
        self._detach()
        self.channel = channel
        self.start()

    def stop(self):
        if self.closed:
            return
        self.closed = True
        GLib.source_remove(self._timer)
        self.app.tuners.disconnect(self._preempt_id)
        self._detach()
        sessions = getattr(self.app, "analysis_sessions", [])
        if self in sessions:
            sessions.remove(self)
        if self.player is not None and not any(s.is_on_screen() for s in sessions):
            self.player.measure_loudness = False

    def _on_preempt(self, _tm, cap):
        if cap is self.capture:
            self.capture.remove_tap(self.analyzer.feed)
            self.capture = None
            self.own_capture = False
            if self.frontend is not None:
                self.frontend.close()
                self.frontend = None
            self.status = "preempted"
            self.emit("message", _("The tuner was needed for watching or recording."))

    def retune(self):
        """Try again after the tuner was taken (or none was free)."""
        if self.kind == "dvb" and self.capture is None:
            self._attach_capture()
            self.emit("channel-changed")

    def switch_to_channel(self):
        """Show the analysed channel in the main window."""
        if self.window is not None:
            self.window.zap(self.channel)

    # -- IPTV -----------------------------------------------------------------
    def _tap_player(self):
        """Feed the analyser with the transport stream the player demuxes: from
        its own pipeline, or from the timeshift buffer of an internet channel
        read into a capture (lindvb.ipstream), which it plays through an appsrc."""
        if self._tap is not None or self.player is None:
            return
        self.player.add_ts_tap(self.analyzer.feed)
        self._tap = self.player
        self._tapped_player = True
        self.status = "player"

    def _untap_player(self):
        tap, self._tap = self._tap, None
        self._tapped_player = False
        if tap is not None:
            tap.remove_ts_tap(self.analyzer.feed)

    def _download_rate(self, now, received):
        """Bit rate read from the network over the last ten seconds, from the
        byte counter of the stream's capture (None until two seconds passed)."""
        self._received.append((now, received))
        while len(self._received) > 2 and now - self._received[0][0] > 10:
            self._received.popleft()
        t0, b0 = self._received[0]
        if received < b0:               # a new capture: count again
            self._received.clear()
            self._received.append((now, received))
            return None
        return (received - b0) * 8 / (now - t0) if now - t0 >= 2 else None

    def _probe_stream(self):
        from .streaminfo import probe
        url = self.channel.url
        self._probe_at = time.monotonic()

        def work():
            res = probe(url)
            GLib.idle_add(self._probe_done, url, res)
        threading.Thread(target=work, daemon=True).start()

    def _probe_done(self, url, res):
        if not self.closed and url == self.channel.url:
            self.stream = res
        return False

    # -- full transponder -------------------------------------------------------
    def can_measure_transponder(self):
        return self.capture is not None and self.capture.pid_filter is not None

    def measure_transponder(self, seconds=TRANSPONDER_SECONDS):
        """Receive the whole transponder for a few seconds and share it out."""
        cap = self.capture
        if cap is None or self._tp_until is not None:
            return False
        if cap.pid_filter != "8192":
            cap.set_full_ts(True)
        self._tp_until = time.monotonic() + seconds
        self._tp_counts = None
        return True

    def _transponder_tick(self, snap, now):
        cap = self.capture
        full = cap is not None and cap.pid_filter == "8192"
        if self._tp_until is None and not full:
            return
        counts = {pid: d["packets"] for pid, d in snap["pids"].items()}
        if self._tp_counts is None:
            self._tp_counts = (now, counts)
            return
        t0, c0 = self._tp_counts
        if self._tp_until is not None and now < self._tp_until:
            self.state["transponder_busy"] = True
            return
        dt = max(0.5, now - t0)
        rates = {pid: (n - c0.get(pid, 0)) * TS_PACKET * 8 / dt for pid, n in counts.items()}
        self.transponder = transponder_shares(snap, rates, self.capacity())
        self.transponder["time"] = time.time()
        if self._tp_until is not None:
            self._tp_until = None
            if cap is not None:
                cap.set_full_ts(False)
        self._tp_counts = (now, counts) if full else None
        self.state["transponder_busy"] = False

    def capacity(self):
        c = self.channel
        t = self.tuning or {}
        sr = t.get("symbol_rate") or (c.symbol_rate * 1000 if c.symbol_rate else 0)
        return linkbudget.capacity_bps(t.get("delsys") or c.delsys,
                                       t.get("modulation") or c.modulation,
                                       t.get("fec") or t.get("code_rate_hp") or c.fec,
                                       symbol_rate=sr,
                                       bandwidth_hz=t.get("bandwidth_hz") or c.bandwidth_hz,
                                       guard=t.get("guard", ""), pilot=t.get("pilot", ""))

    # -- measuring --------------------------------------------------------------
    def _tick(self):
        if self.closed:
            return False
        if self.follow and self.window is not None and self.window.current is not None \
                and self.window.current.key != self.channel.key:
            self.set_channel(self.window.current)
        if self.kind == "dvb" and self.capture is not None and \
                self.capture not in self.app.tuners.captures.values():
            # the live view let go of the tuner we shared
            self.capture.remove_tap(self.analyzer.feed)
            self.capture = None
            self.own_capture = False
            self.status = "lost"
            if self.frontend is not None:
                self.frontend.close()
                self.frontend = None
            self._attach_capture()
        if self.kind == "iptv" and self._tap is None and self.is_on_screen():
            # the channel started playing: measure what the player gets
            if self.iptv_probe is not None:
                self.iptv_probe.stop()
                self.iptv_probe = None
            self._tap_player()
        elif self.kind == "iptv" and self._tap is not None and not self.is_on_screen():
            # the player went to another channel: fetch this one ourselves again
            self._untap_player()
            self.iptv_probe = IptvProbe(self.channel.url, self.analyzer.feed)
            self.iptv_probe.start()
            self.status = "background"
        if self.kind == "iptv" and time.monotonic() - getattr(self, "_probe_at", 0) > IPTV_PROBE_EVERY:
            self._probe_stream()
        if self.paused:
            return True
        try:
            self._measure()
        except Exception as e:              # noqa: BLE001 - keep measuring
            print(f"Channel analysis: {e}")
        self.emit("updated")
        return True

    def _event(self, kind, text):
        self.events.append((time.time(), kind, text))

    def _measure(self):
        now = time.monotonic()
        snap = self.analyzer.snapshot()
        dt = (now - self._prev_time) if self._prev_time else None
        rates = {}
        for pid, d in snap["pids"].items():
            prev = self._prev_counts.get(pid)
            if dt and prev is not None:
                rates[pid] = (d["packets"] - prev) * TS_PACKET * 8 / dt
            elif dt:
                rates[pid] = d["packets"] * TS_PACKET * 8 / max(dt, snap["elapsed"] or 1)
        self._prev_counts = {pid: d["packets"] for pid, d in snap["pids"].items()}
        self._prev_time = now
        for pid, r in rates.items():
            self.pid_history.setdefault(pid, deque(maxlen=PID_HISTORY)).append(r)
        pmt = snap["pmt"]
        layers = service_layers(pmt, rates, snap["pat"].get(pmt["sid"]) if pmt else None)
        sample = {"t": time.time(), "layers": {k: v for k, v, _kind in layers},
                  "total": sum(r for pid, r in rates.items() if pid != NULL_PID) if rates else None,
                  "service": sum(v for _k, v, _kind in layers) if layers else None}
        st = {"snap": snap, "rates": rates, "layers": layers, "kind": self.kind,
              "status": self.status, "detail": self._status_detail}
        # reception
        if self.kind == "dvb" and self.capture is not None:
            fe = self.frontend.read() if self.frontend is not None else {}
            sig = signal_sample(self.capture.stats, fe, self.tuning, self.channel)
            st["signal"] = sig
            st["frontend"] = fe
            for k in ("quality", "cnr_db", "level_dbm", "level_rel", "ber", "margin"):
                sample[k] = sig.get(k)
            unc = sig.get("unc")
            if unc is not None and self._prev_unc is not None and unc > self._prev_unc:
                sample["unc"] = unc - self._prev_unc
                self._event("unc", _("{n} uncorrectable blocks").format(n=unc - self._prev_unc))
            self._prev_unc = unc
            if self._prev_lock and not sig["lock"]:
                self._event("lock", _("Lock lost"))
            self._prev_lock = sig["lock"]
            st["full_ts"] = self.capture.pid_filter == "8192"
            st["pid_filter"] = self.capture.pid_filter
        if snap["cc_errors"] > self._prev_cc:
            n = snap["cc_errors"] - self._prev_cc
            sample["cc"] = n
            self._event("cc", _("{n} continuity errors").format(n=n))
        self._prev_cc = snap["cc_errors"]
        for t, kind, detail in snap["events"][self._seen_events:]:
            if kind == "pcr":
                self._event("pcr", _("PCR jump"))
            elif kind == "version":
                self._event("version", _("New table version (0x{tid})").format(tid=f"{detail:02X}"))
        self._seen_events = len(snap["events"])
        # the channel on screen: picture and sound
        player = self.player
        on_screen = self.is_on_screen()
        st["on_screen"] = on_screen
        if on_screen:
            player.measure_loudness = True
            st["video"] = player.video_info()
            if now - self._bars_at >= BARS_EVERY:
                self._bars_at = now
                self.bars = player.picture_bars()
            st["bars"] = self.bars
            st["tracks"] = player.audio_track_info()
            loud = player.loudness
            st["loudness"] = {"momentary": loud.momentary, "short": loud.short_term,
                              "integrated": loud.integrated, "lra": loud.loudness_range,
                              "peak": loud.peak_db, "recent_peak": loud.recent_peak,
                              "jumps": list(loud.jumps)}
            sample["loudness"] = loud.short_term
            if len(loud.jumps) > self._loud_jumps:
                lu = loud.jumps[-1][1]
                self._event("loudness", _("Loudness jump {lu} LU").format(lu=f"{lu:+.1f}"))
            self._loud_jumps = len(loud.jumps)
        if self.kind == "iptv":
            iptv = {"probe": self.stream}
            if on_screen:
                cap = player.capture
                iptv["buffer"] = player.buffer_info()
                iptv["fragments"] = list(player.fragments)
                iptv["timeshift"] = cap is not None
                rebuffers = player.rebuffers + (getattr(cap, "reconnects", 0) if cap else 0)
                iptv["rebuffers"] = rebuffers
                q = self.window.quality
                sample["quality"] = q.quality if q.kind == "iptv" else None
                if self._prev_rebuffers is not None and rebuffers > self._prev_rebuffers:
                    self._event("rebuffer", _("Rebuffering"))
                self._prev_rebuffers = rebuffers
                buf = iptv["buffer"]
                if buf:
                    sample["buffer"] = buf.get("percent")
                if not sample.get("total"):
                    total = stream_rate(buf, player.audio_bitrate()
                                        if player.is_audio_only() else None)
                    if total:
                        sample["total"] = total
                frags = [f for f in iptv["fragments"] if f["seconds"] > 0]
                dl = None
                if frags:
                    recent = frags[-5:]
                    dl = sum(f["bytes"] for f in recent) * 8 / sum(f["seconds"] for f in recent)
                elif cap is not None:
                    dl = self._download_rate(now, getattr(cap, "received", 0))
                elif buf and (buf.get("in") or 0) > 0:
                    dl = buf["in"] * 8
                if dl:
                    iptv["download_bps"] = dl
                    sample["download"] = dl
            elif self.iptv_probe is not None:
                iptv["error"] = self.iptv_probe.error
                iptv["caps"] = self.iptv_probe.caps
                if not sample.get("total"):
                    b = self.iptv_probe.bytes
                    prev = getattr(self, "_probe_bytes", None)
                    if prev is not None and dt:
                        sample["total"] = (b - prev) * 8 / dt
                    self._probe_bytes = b
                if sample.get("quality") is None:
                    sample["quality"] = 1.0 if sample.get("total") else 0.0
            st["iptv"] = iptv
            st["signal"] = {"quality": sample.get("quality")}
        self._transponder_tick(snap, now)
        st["transponder"] = self.transponder
        st["capacity"] = self.capacity() if self.kind == "dvb" else None
        st["sample"] = sample
        self.history.append(sample)
        st["history"] = self.history
        st["events"] = list(self.events)
        self.state = st

    # -- summaries and export -----------------------------------------------------
    def window_history(self):
        cut = time.time() - self.window_seconds
        return [s for s in self.history if s["t"] >= cut]

    def stats(self, key):
        vals = [s[key] for s in self.window_history() if s.get(key) is not None]
        if not vals:
            return None
        return {"now": vals[-1], "avg": sum(vals) / len(vals), "min": min(vals), "max": max(vals)}

    def summary(self):
        """A flat dict of the main figures (comparison and the text report)."""
        st = self.state or {}
        c = self.channel
        out = {"channel": c.name, "delivery": _("Internet") if c.is_iptv else c.delsys.upper()}
        es = (st.get("snap") or {}).get("es", {})
        video = next((e for e in es.values() if e.get("kind") == "video"), {})
        vinfo = st.get("video") or {}
        w = vinfo.get("width") or video.get("width")
        h = vinfo.get("height") or video.get("height")
        out["resolution"] = f"{w} × {h}" if w and h else "–"
        fps = vinfo.get("fps") or video.get("fps") or \
            (1000 / video["frame_ms"] if video.get("frame_ms") else None)
        out["fps"] = f"{fps:.2f}".rstrip("0").rstrip(".") if fps else "–"
        out["video"] = " ".join(x for x in (video.get("codec") or vinfo.get("codec", ""),
                                            video.get("profile", "")) if x) or "–"
        audio = [e for e in es.values() if e.get("kind") == "audio"]
        out["audio"] = ", ".join(
            f"{a.get('short') or a.get('codec', '?')} {a.get('layout', '')}".strip()
            for a in audio) or "–"
        rate = self.stats("service")
        out["avg_rate"] = _fmt_rate(rate["avg"]) if rate else "–"
        out["peak_rate"] = _fmt_rate(rate["max"]) if rate else "–"
        cnr = self.stats("cnr_db")
        out["cnr"] = f"{cnr['avg']:.1f} dB" if cnr else "–"
        m = self.stats("margin")
        out["margin"] = f"{m['avg']:+.1f} dB" if m else "–"
        q = self.stats("quality")
        out["quality"] = f"{q['avg'] * 100:.0f} %" if q else "–"
        snap = st.get("snap") or {}
        out["cc_errors"] = str(snap.get("cc_errors", 0))
        loud = st.get("loudness") or {}
        out["loudness"] = f"{loud['integrated']:.1f} LUFS" if loud.get("integrated") is not None \
            and math.isfinite(loud["integrated"]) else "–"
        return out

    def report_dict(self):
        st = self.state or {}
        snap = st.get("snap") or {}
        c = self.channel
        pids = []
        owners = pid_owners(snap) if snap else {}
        for pid, d in sorted((snap.get("pids") or {}).items()):
            pids.append({"pid": pid, "content": owners.get(pid, (None, ""))[1],
                         "bitrate": st.get("rates", {}).get(pid), "cc_errors": d["cc_errors"],
                         "scrambled": d["scrambled"], "packets": d["packets"]})
        es = {str(k): {kk: vv for kk, vv in v.items() if kk != "frames"}
              for k, v in (snap.get("es") or {}).items()}
        return {
            "channel": c.name, "key": c.key, "delivery": c.delsys,
            "frequency_khz": c.frequency, "polarity": c.polarity, "symbol_rate_kbd": c.symbol_rate,
            "sid": c.sid, "tsid": snap.get("tsid"), "onid": snap.get("onid"), "url": c.url,
            "measured_since": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            "summary": self.summary(), "signal": _plain(st.get("signal")),
            "tuning": self.tuning, "pids": pids, "streams": es,
            "tables": [{k: v for k, v in t.items()} for t in snap.get("tables", [])],
            "loudness": _plain(st.get("loudness")), "video": _plain(st.get("video")),
            "transponder": _plain(self.transponder), "iptv": _plain(st.get("iptv")),
            "events": [{"time": time.strftime("%H:%M:%S", time.localtime(t)), "kind": k,
                        "text": txt} for t, k, txt in self.events],
            "sections": [{"title": sec["title"], "rows": [list(r) for r in sec.get("rows", [])],
                          "table": sec.get("table"), "lines": sec.get("lines", [])}
                         for sec in self.report_sections()],
        }

    def report_sections(self):
        """The report as the window shows it: [{"title", "rows": [(label, value)],
        "table": (columns, rows) or None, "lines": [str]}] (lindvb.ui.analysis)."""
        return report_sections(self)

    def report_text(self):
        d = self.report_dict()
        lines = [_("Channel analysis: {name}").format(name=d["channel"]),
                 _("Measured since {time}").format(time=d["measured_since"]), ""]
        labels = {"delivery": _("Delivery"), "resolution": _("Resolution"), "fps": _("Frame rate"),
                  "video": _("Video"), "audio": _("Audio"), "avg_rate": _("Average bit rate"),
                  "peak_rate": _("Peak bit rate"), "quality": _("Signal quality"),
                  "cnr": "C/N", "margin": _("Margin"), "cc_errors": _("Continuity errors"),
                  "loudness": _("Loudness")}
        for k, label in labels.items():
            lines.append(f"{label}: {d['summary'].get(k, '–')}")
        for sec in d["sections"]:
            lines += ["", sec["title"], "-" * len(sec["title"])]
            lines += [f"{label}: {value}" for label, value in sec.get("rows", [])]
            table = sec.get("table")
            if table:
                lines += _text_table(table["columns"], table["rows"])
            lines += sec.get("lines", [])
        lines += ["", "PID       " + _("Content").ljust(28) + _("Bit rate").rjust(14) + "  CC"]
        for p in d["pids"]:
            lines.append(f"0x{p['pid']:04X}    {str(p['content'])[:28].ljust(28)}"
                         f"{_fmt_rate(p['bitrate']).rjust(14)}  {p['cc_errors']}")
        if d["events"]:
            lines += ["", _("Events")]
            lines += [f"{e['time']}  {e['text']}" for e in d["events"][-40:]]
        return "\n".join(lines) + "\n"

    def history_csv(self):
        keys = ["quality", "cnr_db", "margin", "level_dbm", "ber", "unc", "cc", "total",
                "service", "buffer", "download", "loudness"]
        rows = [",".join(["time"] + keys)]
        for s in self.history:
            rows.append(",".join([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["t"]))] +
                                 ["" if s.get(k) is None else f"{s[k]:.6g}" for k in keys]))
        return "\n".join(rows) + "\n"

    def save_ts(self, path, seconds=30, done=None):
        """Write the last *seconds* of a DVB channel (or the next ones of an internet
        channel) as a transport stream file. *done(ok, message)* runs in the main loop."""
        cap = self.capture
        if self.kind == "dvb" and cap is not None:
            start, end = cap.store.bounds()
            begin = max(start, end - int(cap.byte_rate() * seconds))

            def work():
                reader = SegmentReader(cap.store, begin)
                written = 0
                try:
                    with open(path, "wb") as f:
                        while written < end - begin:
                            data = reader.read()
                            if not data:
                                break
                            f.write(data[:end - begin - written])
                            written += len(data)
                    ok, msg = True, path
                except OSError as e:
                    ok, msg = False, str(e)
                finally:
                    reader.close()
                if done:
                    GLib.idle_add(lambda: (done(ok, msg), False)[1])
            threading.Thread(target=work, daemon=True).start()
            return True
        if self.kind == "iptv":
            from .tuner import IPTVRecorder
            rec = IPTVRecorder(self.channel.url, path)
            rec.start()

            def finish():
                rec.stop()
                if done:
                    GLib.timeout_add_seconds(3, lambda: (done(True, path), False)[1])
                return False
            GLib.timeout_add_seconds(int(seconds), finish)
            return True
        return False


def _plain(v):
    """JSON-safe copy (tuples to lists, inf to None, bytes dropped)."""
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items() if not isinstance(x, (bytes, bytearray))}
    if isinstance(v, (list, tuple, deque)):
        return [_plain(x) for x in v]
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def report_json(session):
    return json.dumps(_plain(session.report_dict()), indent=2, ensure_ascii=False)


def service_layers(pmt, rates, pmt_pid=None):
    """[(pid, bit rate, kind)] of the service's streams for the stacked bit rate chart:
    the video, every audio stream, and the rest (teletext, subtitles, data, PMT)."""
    if not pmt:
        return []
    out = []
    other = rates.get(pmt_pid, 0.0) if pmt_pid is not None else 0.0
    for pid, d in pmt["streams"]:
        r = rates.get(pid)
        if r is None:
            continue
        if d["kind"] in ("video", "audio"):
            out.append((pid, r, d["kind"]))
        else:
            other += r
    for _caid, ecm in pmt["ca"]:
        other += rates.get(ecm, 0.0)
    out.sort(key=lambda x: (x[2] != "video", x[0]))
    out.append(("other", other, "other"))
    return out


def transponder_shares(snap, rates, capacity=None):
    """Bit rate of every service of the transponder, SI and null packets."""
    owners = pid_owners(snap)
    names = {sid: info.get("name") or f"SID {sid}" for sid, info in snap["services"].items()}
    per = {}
    si = null = 0.0
    for pid, r in rates.items():
        if pid == NULL_PID:
            null += r
            continue
        sid, _what = owners.get(pid, (None, ""))
        if sid is None:
            si += r
        else:
            per[sid] = per.get(sid, 0.0) + r
    services = sorted(({"sid": sid, "name": names.get(sid, f"SID {sid}"), "bps": r,
                        "scrambled": bool(snap["services"].get(sid, {}).get("scrambled"))}
                       for sid, r in per.items()), key=lambda s: -s["bps"])
    used = sum(s["bps"] for s in services) + si
    return {"services": services, "si": si, "null": null, "used": used,
            "capacity": capacity, "total": used + null}


def ca_lines(snap):
    """[(caid, system name, ecm pid, level)] of the service's PMT."""
    pmt = snap.get("pmt") or {}
    out = [(caid, ca_system_name(caid), pid, "program") for caid, pid in pmt.get("ca", [])]
    for spid, d in pmt.get("streams", []):
        for caid, pid in d.get("ca", []):
            out.append((caid, ca_system_name(caid), pid, f"PID {spid}"))
    return out


# -- the report: every section of the window -------------------------------------

AIT_CODES = {1: "AUTOSTART", 2: "PRESENT", 3: "DESTROY", 4: "KILL", 5: "PREFETCH",
             6: "REMOTE", 7: "DISABLED", 8: "PLAYBACK_AUTOSTART"}


def _num(v, digits=1, unit=""):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    return f"{v:.{digits}f} {unit}".strip()


def _ms(seconds, digits=0):
    return _num(seconds * 1000, digits, "ms") if seconds is not None else "–"


def _or_dash(v):
    return v if v not in (None, "") else "–"


def _text_table(columns, rows):
    """Plain text columns, as wide as their longest cell."""
    cells = [[str(c) for c in columns]] + [[str(_or_dash(c)) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(columns))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]


def _section(title, rows=(), table=None, lines=()):
    return {"title": title, "rows": [(k, str(_or_dash(v))) for k, v in rows],
            "table": table, "lines": list(lines)}


def _stats_text(st):
    return _("now {now} · Ø {avg} · peak {max}").format(
        now=_fmt_rate(st["now"]), avg=_fmt_rate(st["avg"]), max=_fmt_rate(st["max"])) if st else "–"


def _iptv_sections(session, st):
    ip = st.get("iptv") or {}
    probe = ip.get("probe") or {}
    sig = st.get("signal") or {}
    on = st.get("on_screen")
    q = session.stats("quality")
    tot = session.stats("total")
    dl = ip.get("download_bps") or probe.get("download_bps")
    buf = ip.get("buffer") or {}
    pct = buf.get("percent")
    buffer_txt = f"{pct} %" if pct is not None else (
        "–" if on else _("only for the channel on screen"))
    if buf.get("left_ms") is not None and pct is not None:
        buffer_txt += " · " + _num(buf["left_ms"] / 1000, 1, "s")
    rows = [(_("Quality"), (f"{round((sig.get('quality') or 0) * 100)} %" +
                            (" · " + _("Ø {avg} · min {min}").format(
                                avg=f"{round(q['avg'] * 100)} %", min=f"{round(q['min'] * 100)} %")
                             if q else ""))),
            (_("Stream"), _stats_text(tot)),
            (_("Download"), _fmt_rate(dl) + (" · " + _("{x}× the stream rate").format(
                x=_num(dl / tot["avg"], 1)) if dl and tot and tot["avg"] else "") if dl else "–"),
            (_("Buffer"), buffer_txt),
            (_("Rebuffering"), str(ip.get("rebuffers", "–"))),
            (_("Timeshift"), (_("yes, played from the timeshift buffer") if ip.get("timeshift")
                              else _("no, played directly")) if on else "")]
    out = [_section(_("Stream"), [r for r in rows if r[1]])]
    hist = session.window_history()
    bufs = [h["buffer"] for h in hist if h.get("buffer") is not None]
    out[0]["rows"].append((_("Buffer history"), _("Ø {avg} % · min {min} %").format(
        avg=round(sum(bufs) / len(bufs)), min=min(bufs)) if bufs else "–"))
    pl = probe.get("playlist") or {}
    kind = pl.get("kind")
    media_pl = pl.get("media_playlist") or (pl if kind == "media" else {})
    playlist = {"master": _("HLS with {n} variants").format(n=len(pl.get("variants", []))),
                "media": _("HLS media playlist"),
                "dash": _("MPEG-DASH")}.get(kind, no_playlist(probe)) + \
        ((" · " + (_("live") if pl.get("live") or media_pl.get("live") else _("on demand")))
         if kind else "")
    segments = _("{n} segments of Ø {s} (target {t}) · {c}").format(
        n=media_pl["segments"], s=_num(media_pl["avg_segment"], 1, "s"),
        t=_num(media_pl["target"], 0, "s"), c=media_pl.get("container") or "?") \
        if media_pl.get("segments") else "–"
    rows = [(_("Address"), session.channel.url),
            (_("Final address"), probe.get("final_url")
             if probe.get("final_url") and probe.get("final_url") != session.channel.url else ""),
            (_("Protocol"), probe.get("protocol") or "–"),
            (_("HTTP status"), str(probe["status"]) if "status" in probe else "–"),
            (_("Server"), probe.get("server") or "–"),
            (_("Content type"), probe.get("content_type") or "–"),
            (_("Response time"), _ms(probe.get("latency")) if probe.get("latency") else "–"),
            (_("Download speed"), _fmt_rate(probe.get("download_bps"))
             if probe.get("download_bps") else _fmt_rate(dl) if dl else "–"),
            (_("Playlist"), playlist if probe else "–"),
            (_("Segments"), segments),
            (_("Rebuffering"), str(ip.get("rebuffers", "–"))),
            (_("Error"), probe.get("error") or ip.get("error") or "")]
    variants = pl.get("variants") or [r for r in pl.get("representations", [])
                                      if r.get("type") in ("video", "")]
    table = None
    if variants:
        table = {"columns": [_("Bit rate"), _("Resolution"), _("Frame rate"), _("Codecs")],
                 "rows": [[_fmt_rate(v.get("bandwidth")),
                           f"{v['width']} × {v['height']}" if v.get("width") else "–",
                           _num(v["fps"], 2, "fps") if v.get("fps") else "–", v.get("codecs", "")]
                          for v in variants]}
    media = pl.get("media") or [r for r in pl.get("representations", [])
                                if r.get("type") not in ("video", "")]
    lines = []
    if media:
        lines.append(_("Renditions") + ": " + ", ".join(
            " ".join(x for x in (m.get("type", "").lower(), m.get("language", ""), m.get("name", ""))
                     if x) for m in media))
    elif not variants:
        lines.append(_("No variant list: the address is a single stream."))
    out.insert(0, _section(_("Internet stream"), [r for r in rows if r[1]], table, lines))
    return out


def _dvb_sections(session, st):
    sig = st.get("signal") or {}
    c = session.channel
    t = session.tuning or {}
    q = session.stats("quality")
    cn = session.stats("cnr_db")
    m = sig.get("margin")
    ber = sig.get("ber")
    rows = [(_("Quality"), (f"{round((sig.get('quality') or 0) * 100)} %" +
                            (" · " + _("Ø {avg} · min {min}").format(
                                avg=f"{round(q['avg'] * 100)} %", min=f"{round(q['min'] * 100)} %")
                             if q else ""))),
            ("C/N", _num(sig.get("cnr_db"), 1, "dB") + (
                " · " + _("min {min} · max {max}").format(min=_num(cn["min"], 1), max=_num(cn["max"], 1))
                if cn else "")),
            (_("Margin"), (("+" if m >= 0 else "") + _num(m, 1, "dB")) if m is not None else "–"),
            (_("C/N needed"), _num(sig.get("required_cn"), 1, "dB") if sig.get("required_cn") else ""),
            (_("Level"), _num(sig.get("level_dbm"), 1, "dBm") if sig.get("level_dbm") is not None
             else (f"{round(sig['level_rel'] * 100)} %" if sig.get("level_rel") is not None else "–")),
            ("BER", (f"{ber:.1e}" if ber else "0") if ber is not None else
             (str(sig.get("ber_raw")) if sig.get("ber_raw") is not None else "–")),
            (_("Uncorrectable blocks"), str(sum(s.get("unc", 0) for s in session.history))),
            (_("Lock losses"), str(sum(1 for e in session.events if e[1] == "lock"))),
            ("Lock", ", ".join(k for k, v in (sig.get("status") or {}).items() if v) or "–")]
    out = [_section(_("Signal"), [r for r in rows if r[1]])]
    snap = st.get("snap") or {}
    sr = (t.get("symbol_rate") or (c.symbol_rate or 0) * 1000) // 1000
    cap = st.get("capacity")
    rows = [(_("Frequency"), f"{c.frequency // 1000} MHz {c.polarity.upper()}".strip()
             if c.is_sat else (_num(c.frequency / 1000, 1, "MHz") if c.frequency else "")),
            (_("Symbol rate"), f"{sr} kBd" if sr else ""),
            (_("Modulation"), " · ".join(x for x in ((t.get("delsys") or c.delsys).upper(),
                                                      (t.get("modulation") or c.modulation).upper(),
                                                      "FEC " + (t.get("fec") or t.get("code_rate_hp")
                                                                or c.fec))
                                         if x and x.upper() not in ("AUTO", "FEC AUTO"))),
            (_("Roll-off / pilots"), " · ".join(x for x in (t.get("rolloff", ""), t.get("pilot", ""),
                                                            t.get("guard", ""),
                                                            t.get("transmission_mode", "")) if x)),
            ("DiSEqC", _("Port {n}").format(n=c.diseqc + 1) if c.diseqc >= 0 else ""),
            (_("Transponder capacity"), _fmt_rate(cap) + " " + _("(theoretical)") if cap else ""),
            ("ONID · TSID · SID", f"{snap.get('onid') or c.onid or '–'} · "
                                  f"{snap.get('tsid') or c.tsid or '–'} · {c.sid or '–'}")]
    out.append(_section(_("Reception"), [r for r in rows if r[1]]))
    if t:
        out.append(_section(_("Tuned parameters (from the frontend)"),
                            [(k, str(v)) for k, v in t.items() if v not in (None, "")]))
    return out


def _video_sections(session, st):
    snap = st.get("snap") or {}
    pmt = snap.get("pmt") or {}
    es = snap.get("es") or {}
    rates = st.get("rates") or {}
    vi = st.get("video") or {}
    on = st.get("on_screen")
    vpid = next((pid for pid, d in pmt.get("streams", []) if d["kind"] == "video"), None)
    vid = es.get(vpid, {}) if vpid is not None else {}
    vd = dict(pmt.get("streams", [])).get(vpid, {})
    w, h = vi.get("width") or vid.get("width"), vi.get("height") or vid.get("height")
    im = vi.get("interlace-mode", "")
    scan = {"progressive": _("progressive"), "interleaved": _("interlaced"),
            "mixed": _("mixed"), "fields": _("fields")}.get(im, "")
    if not scan and "interlaced" in vid:
        scan = _("interlaced capable") if vid["interlaced"] else _("progressive")
    fps = vi.get("fps") or vid.get("fps") or (1000 / vid["frame_ms"] if vid.get("frame_ms") else None)
    par = vi.get("par") or vid.get("sar")
    afd = vid.get("afd")
    from .esparse import AFD_NAMES
    bars = st.get("bars")
    bars_txt = "–" if on else _("only for the channel on screen")
    if bars:
        t, b, lft, r = bars
        bars_txt = (_("letterbox {pct} % top and bottom").format(pct=round((t + b) * 50))
                    if t + b > 0.05 else
                    _("pillarbox {pct} % at the sides").format(pct=round((lft + r) * 50))
                    if lft + r > 0.05 else _("none"))
    tr, cm = vid.get("transfer", ""), vi.get("colorimetry", "")
    hdr = ("HDR10 (PQ)" if "PQ" in tr or "smpte2084" in cm or "bt2100-pq" in cm else
           "HLG" if "HLG" in tr or "hlg" in cm or "arib-std-b67" in cm else "SDR") if tr or cm else ""
    nominal = vid.get("nominal_bps") or vd.get("max_bps")
    hist = session.window_history()
    vr = [hh["layers"].get(vpid) for hh in hist if vpid in hh.get("layers", {})]
    tc = vid.get("telecine")
    decoder = vi.get("decoder", "") if on else _("only for the channel on screen")
    rows = [("PID", str(vpid) if vpid is not None else "–"),
            (_("Codec"), vid.get("codec") or vd.get("codec") or vi.get("codec", "")),
            (_("Profile / level"), " · ".join(x for x in (
                vid.get("profile"), vid.get("level") and _("level {l}").format(l=vid["level"]),
                vid.get("tier") and _("tier {t}").format(t=vid["tier"])) if x)),
            (_("Resolution"), f"{w} × {h}" if w and h else ""),
            (_("Scan"), scan + (f" · {vi['field-order']}" if vi.get("field-order") else "")),
            (_("Frame rate"), _num(fps, 3 if fps and fps % 1 else 0, "fps") if fps else ""),
            (_("Aspect ratio"), " · ".join(x for x in (vid.get("aspect", ""),
                                                       f"{_('pixel')} {par[0]}:{par[1]}" if par else "")
                                           if x)),
            ("AFD", f"{afd} – {AFD_NAMES.get(afd, '')}" if afd is not None else _("not sent")),
            (_("Black bars"), bars_txt),
            (_("Film (3:2 pulldown)"), (_("yes, repeated fields") if tc else _("no"))
             if tc is not None else ""),
            (_("Colour"), " · ".join(x for x in (vid.get("chroma"),
                                                 f"{vid['bit_depth']} bit" if vid.get("bit_depth") else "",
                                                 vid.get("primaries"), cm) if x)),
            ("HDR", hdr),
            (_("Decoder"), decoder),
            (_("Video output"), vi.get("output", "") if on else ""),
            (_("Frames shown / dropped"), f"{vi.get('rendered', 0)} / {vi.get('dropped', 0)}"
             if "dropped" in vi else ""),
            (_("Nominal bit rate"), (_fmt_rate(nominal) + ("" if vid.get("nominal_bps") else
                                                           " " + _("(maximum)")))
             if nominal else _("not signalled (variable)")),
            (_("Measured bit rate"), _("now {now} · Ø {avg} · peak {max}").format(
                now=_fmt_rate(rates.get(vpid)), avg=_fmt_rate(sum(vr) / len(vr)),
                max=_fmt_rate(max(vr))) if vr else ""),
            (_("VBV buffer"), f"{vid['vbv_bytes'] // 1024} KiB" if vid.get("vbv_bytes") else "")]
    out = [_section(_("Video"), rows)]
    frames = vid.get("frames") or []
    pb = [sz for sz, t in frames if t in ("P", "B")]
    counts = {k: sum(1 for _sz, t in frames if t == k) for k in ("I", "P", "B")}
    out.append(_section(_("Frame sizes"), [
        (_("GOP length"), _("{n} frames").format(n=vid["gop"]) +
         (f" ({_num(vid['gop'] / fps, 2, 's')})" if fps else "") if vid.get("gop") else ""),
        (_("I frame average"), f"{vid['i_frame_avg'] / 1024:.0f} KiB" if vid.get("i_frame_avg") else ""),
        (_("P/B frame average"), f"{sum(pb) / len(pb) / 1024:.0f} KiB" if pb else ""),
        (_("Frames measured"), " · ".join(f"{n} {k}" for k, n in counts.items() if n) if frames else "")]))
    return out


def _audio_sections(session, st):
    snap = st.get("snap") or {}
    pmt = snap.get("pmt") or {}
    es = snap.get("es") or {}
    rates = st.get("rates") or {}
    on = st.get("on_screen")
    tracks, cur = st.get("tracks") or ([], -1)
    rows = []
    ai = 0
    for pid, d in pmt.get("streams", []):
        if d["kind"] != "audio":
            continue
        e = es.get(pid, {})
        notes = []
        if d.get("audio_description"):
            notes.append(_("audio description"))
        if d.get("hard_of_hearing"):
            notes.append(_("clean audio"))
        if e.get("mode") and e["mode"] not in ("stereo",):
            notes.append(e["mode"])
        if on and ai == cur:
            notes.append(_("playing"))
        lang = d.get("language") or ""
        if not lang and ai < len(tracks):
            lang = tracks[ai].language or ""
        rows.append([str(pid), lang or "–", e.get("codec") or d.get("codec", ""), e.get("layout", ""),
                     f"{e['rate'] / 1000:g} kHz" if e.get("rate") else "",
                     _fmt_rate(e.get("nominal_bps")) if e.get("nominal_bps") else "–",
                     _fmt_rate(rates.get(pid)), f"{e['dialnorm']} dB" if e.get("dialnorm") else "–",
                     ", ".join(notes)])
        ai += 1
    if not rows:
        for i, t in enumerate(tracks):
            rows.append(["–", t.language_name or "–", t.codec or "", t.layout or "", "", "", "", "",
                         _("playing") if on and i == cur else ""])
    cols = ["PID", _("Language"), _("Format"), _("Channels"), _("Sample rate"), _("Nominal"),
            _("Measured"), _("Dialnorm"), _("Notes")]
    out = [_section(_("Audio tracks"), [(_("Tracks"), str(len(rows)))],
                    {"columns": cols, "rows": rows} if rows else None)]
    loud = st.get("loudness") or {}

    def lu(key, unit="LUFS"):
        v = loud.get(key)
        return _num(v, 1, unit) if v is not None and math.isfinite(v) else "–"
    if on:
        rows = [(_("Momentary"), lu("momentary")), (_("Short term"), lu("short")),
                (_("Integrated"), lu("integrated")), (_("Loudness range"), lu("lra", "LU")),
                (_("Sample peak"), lu("recent_peak", "dBFS")),
                (_("Loudness jumps"), str(len(loud.get("jumps", []))))]
    else:
        rows = [(_("Loudness"), _("Loudness is measured for the channel on screen only."))]
    out.append(_section(_("Loudness (EBU R 128)"), rows))
    lines = []
    for pid, d in pmt.get("streams", []):
        if d["kind"] == "teletext":
            types = {1: _("index"), 2: _("subtitles"), 3: _("info"), 4: _("schedule"),
                     5: _("subtitles (hard of hearing)")}
            lines.append(_("Teletext PID {pid}:").format(pid=pid))
            for lang, ttype, page in d.get("pages", []):
                lines.append(f"   {page}  {lang}  {types.get(ttype, ttype)}")
        elif d["kind"] == "subtitle":
            lines.append(_("DVB subtitles PID {pid}:").format(pid=pid) + " " + ", ".join(
                f"{lang} (0x{t:02X})" for lang, t in d.get("subtitles", [])))
    out.append(_section(_("Teletext and subtitles"),
                        lines=lines or [_("No teletext or subtitles in this channel.")]))
    return out


def _health_section(session, st):
    snap = st.get("snap") or {}
    pmt = snap.get("pmt") or {}
    pids = snap.get("pids") or {}
    rates = st.get("rates") or {}
    pd = pids.get(pmt.get("pcr_pid"), {})
    tables = {t["tid"]: t for t in snap.get("tables", [])}
    pat, pmt_t = tables.get(0), tables.get(2)
    rows = [(_("Packets"), str(snap.get("packets", 0))),
            (_("Sync losses"), str(snap.get("sync_losses", 0))),
            (_("Transport errors"), str(snap.get("tei", 0))),
            (_("Continuity errors"), str(snap.get("cc_errors", 0))),
            (_("PCR interval"), (_ms(pd["pcr_avg"], 1) + " · max " + _ms(pd["pcr_max"], 1))
             if "pcr_avg" in pd else "–"),
            (_("PCR arrival jitter"), ("± " + _ms(pd["pcr_jitter"], 2))
             if pd.get("pcr_jitter") is not None else "–"),
            (_("PCR jumps"), str(pd.get("pcr_jumps", 0))),
            (_("PAT / PMT interval"), " / ".join(_ms(t["interval"]) if t and t.get("interval") else "–"
                                                 for t in (pat, pmt_t))),
            (_("Null packets"), _fmt_rate(rates.get(NULL_PID)))]
    f = st.get("pid_filter")
    if f:
        rows.append((_("PID filter"), _("whole transponder") if f == "8192" else
                     _("{n} PIDs").format(n=len(f.split(":")))))
    cap = session.capture
    if cap is None and session.kind == "iptv" and session.player is not None and st.get("on_screen"):
        cap = session.player.capture
    if cap is not None:
        try:
            start, end = cap.store.bounds()
            secs = int((end - start) / max(1, cap.byte_rate()))
            rows.append((_("Timeshift buffer"), f"{(end - start) / 1e6:.1f} MB · "
                                                f"{secs // 60:02d}:{secs % 60:02d}"))
        except (AttributeError, OSError, TypeError):
            pass
    return _section(_("Stream health"), rows)


def _rate_section(session, st):
    layers = st.get("layers") or []
    stats = session.stats("service")
    if st.get("kind") == "iptv" and not layers:
        stats = session.stats("total")
    tp = st.get("transponder") or {}
    base = tp.get("used") or st.get("capacity")
    snap = st.get("snap") or {}
    streams = dict((snap.get("pmt") or {}).get("streams", []))
    es = snap.get("es") or {}
    rows = [(_("Now"), _fmt_rate(stats["now"]) if stats else "–"),
            (_("Average"), _fmt_rate(stats["avg"]) if stats else "–"),
            (_("Peak"), _fmt_rate(stats["max"]) if stats else "–"),
            (_("Share of transponder"), f"{round(stats['avg'] / base * 100)} %"
             if stats and base else "–")]
    for key, r, kind in layers:
        d = streams.get(key, {})
        e = es.get(key, {})
        name = _("Video") if kind == "video" else (
            _("Teletext / subtitles / data") if kind == "other" else
            " ".join(x for x in (e.get("short") or d.get("codec", ""), d.get("language", "")) if x))
        rows.append((f"  {name}", _fmt_rate(r)))
    return _section(_("Bit rate of the channel"), rows)


def _encryption_section(session, st):
    snap = st.get("snap") or {}
    pmt = snap.get("pmt") or {}
    pids = snap.get("pids") or {}
    rates = st.get("rates") or {}
    cas = ca_lines(snap)
    streams = pmt.get("streams", [])
    scr = sum(pids.get(p, {}).get("scrambled", 0) for p, _d in streams)
    tot = sum(pids.get(p, {}).get("packets", 0) for p, _d in streams)
    sv = (snap.get("services") or {}).get(session.channel.sid)
    rows = [(_("Status"), _("Encrypted") if cas or scr else _("Free-to-air")),
            (_("Scrambled packets"), f"{scr * 100 // max(1, tot)} %" if tot else "–"),
            ("SDT", (_("scrambled") if sv.get("scrambled") else _("free")) if sv else "")]
    table = {"columns": ["CAID", _("System"), "ECM PID", _("Level"), _("Bit rate")],
             "rows": [[f"0x{caid:04X}", name or _("unknown"), f"0x{pid:04X}",
                       lvl if lvl != "program" else _("all streams"),
                       _fmt_rate(rates.get(pid)) if pid in rates else _("not received")]
                      for caid, name, pid, lvl in cas]} if cas else None
    emm = snap.get("cat") or []
    lines = [_("EMM (CAT): {list}").format(list=", ".join(
        f"{ca_system_name(c) or hex(c)} PID 0x{p:04X}" for c, p in emm))] if emm else []
    return _section(_("Encryption"), [r for r in rows if r[1]], table, lines)


def _interactive_section(session, st):
    snap = st.get("snap") or {}
    pmt = snap.get("pmt") or {}
    ait_pid = next((pid for pid, d in pmt.get("streams", []) if d.get("ait")), None)
    ait = getattr(session.capture, "ait", None) if session.capture is not None else None
    auto = ait.autostart() if ait is not None else None
    ttx = [(pid, d) for pid, d in pmt.get("streams", []) if d["kind"] == "teletext"]
    subs = [d for _p, d in pmt.get("streams", []) if d["kind"] == "subtitle"]
    rows = [("HbbTV", f"AIT · PID {ait_pid}" if ait_pid else _("none")),
            (_("Autostart"), auto.name() if auto else "–"),
            (_("Teletext"), _("PID {pid} · {n} pages").format(pid=ttx[0][0], n=len(ttx[0][1].get("pages", [])))
             if ttx else _("none")),
            (_("DVB subtitles"), ", ".join(d.get("language", "?") for d in subs) if subs else _("none"))]
    table = None
    if ait is not None:
        table = {"columns": [_("Name"), _("Organisation / app"), _("Control"), _("Address")],
                 "rows": [[a.name() or "–", f"0x{a.org_id:X} / 0x{a.app_id:X}",
                           AIT_CODES.get(a.control_code, str(a.control_code)), ait.url(a) or "–"]
                          for a in ait.apps]}
    return _section(_("Interactive"), rows, table)


def _epg_section(session):
    epg = getattr(session.app, "epg", None)
    rows = []
    try:
        ev_now, ev_next = epg.now_next(session.channel.epg_key) if epg is not None else (None, None)
    except Exception:                   # noqa: BLE001 - a guide that is still loading
        ev_now = ev_next = None
    hm = lambda t: time.strftime("%H:%M", time.localtime(t))   # noqa: E731
    if ev_now:
        rows.append((_("Now"), f"{hm(ev_now.start)} – {hm(ev_now.end)}  {ev_now.title}"))
    if ev_next:
        rows.append((_("Next"), f"{hm(ev_next.start)}  {ev_next.title}"))
    if not rows:
        rows.append((_("Now"), _("No programme information")))
    return _section(_("Now & next"), rows)


def _tables_section(st):
    snap = st.get("snap") or {}
    rows = []
    for t in snap.get("tables", []):
        rows.append([f"0x{t.get('tid', 0):02X}", f"0x{t['pid']:04X}" if t.get("pid") is not None else "–",
                     str(t.get("name") or ""), str(t.get("version", "–")),
                     _ms(t["interval"]) if t.get("interval") else "–", str(t.get("count", "–"))])
    return _section(_("Tables"), table={"columns": ["TID", "PID", _("Table"), _("Version"),
                                                    _("Interval"), _("Count")], "rows": rows}
                    if rows else None, lines=[] if rows else [_("No tables received.")])


def _transponder_section(st):
    tp = st.get("transponder")
    if not tp:
        return None
    rows = [(sv["name"], _fmt_rate(sv["bps"]) + (" (" + _("scrambled") + ")" if sv["scrambled"] else ""))
            for sv in tp["services"]]
    rows += [(_("SI / EPG"), _fmt_rate(tp["si"])), (_("free / null"), _fmt_rate(tp["null"])),
             (_("Capacity"), _fmt_rate(tp.get("capacity")) if tp.get("capacity") else "–")]
    return _section(_("Transponder"), rows)


def report_sections(session):
    """Every section the analysis window shows, as label/value rows (see
    AnalysisSession.report_sections)."""
    st = session.state or {}
    status = {"player": _("the player's stream"), "live": _("the player's stream"),
              "background": _("a background reception"), "no_tuner": _("no tuner free"),
              "preempted": _("the tuner was needed for watching or recording"),
              "lost": _("reception lost")}.get(st.get("status", session.status), session.status)
    out = [_section(_("Source"), [(_("Channel"), session.channel.name),
                                   (_("Delivery"), _("Internet") if session.kind == "iptv"
                                    else session.channel.delsys.upper()),
                                   (_("Source"), status),
                                   (_("Window"), _num(session.window_seconds / 60, 0, "min"))])]
    out += _iptv_sections(session, st) if session.kind == "iptv" else _dvb_sections(session, st)
    out.append(_rate_section(session, st))
    out += _video_sections(session, st)
    out += _audio_sections(session, st)
    out.append(_health_section(session, st))
    out.append(_encryption_section(session, st))
    out.append(_interactive_section(session, st))
    out.append(_epg_section(session))
    out.append(_tables_section(st))
    tp = _transponder_section(st)
    if tp:
        out.append(tp)
    return out
