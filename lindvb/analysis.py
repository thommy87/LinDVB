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
                self._fe = Frontend(cap.adapter, s.get("frontend", 0))
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
                self.player.add_ts_tap(self.analyzer.feed)
                self._tapped_player = True
                self.status = "player"
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
            self.frontend = Frontend(cap.adapter, self.app.settings.get("frontend", 0))
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
        if self._tapped_player and self.player is not None:
            self.player.remove_ts_tap(self.analyzer.feed)
        self._tapped_player = False
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
        if self.kind == "iptv" and not self._tapped_player and self.is_on_screen():
            # the channel started playing: measure what the player gets
            if self.iptv_probe is not None:
                self.iptv_probe.stop()
                self.iptv_probe = None
            self.player.add_ts_tap(self.analyzer.feed)
            self._tapped_player = True
            self.status = "player"
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
                iptv["buffer"] = player.buffer_info()
                iptv["fragments"] = list(player.fragments)
                iptv["rebuffers"] = player.rebuffers
                q = self.window.quality
                sample["quality"] = q.quality if q.kind == "iptv" else None
                if self._prev_rebuffers is not None and player.rebuffers > self._prev_rebuffers:
                    self._event("rebuffer", _("Rebuffering"))
                self._prev_rebuffers = player.rebuffers
                buf = iptv["buffer"]
                if buf:
                    sample["buffer"] = buf.get("percent")
                    if not sample.get("total") and buf.get("in", -1) > 0:
                        sample["total"] = buf["in"] * 8
                frags = [f for f in iptv["fragments"] if f["seconds"] > 0]
                if frags:
                    recent = frags[-5:]
                    iptv["download_bps"] = sum(f["bytes"] for f in recent) * 8 / \
                        sum(f["seconds"] for f in recent)
                    sample["download"] = iptv["download_bps"]
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
        }

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
