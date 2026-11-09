# SPDX-License-Identifier: GPL-2.0-or-later
"""Recordings, timer recordings and series recording rules."""

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from gi.repository import Gio, GLib, GObject

from .config import CONFIG_DIR
from .i18n import N_, _
from .recformat import (AFTER_ACTIONS, AUDIO_MODES, EXTENSIONS, EXTRAS,  # noqa: F401
                        SINGLE_AUDIO, TIMER_ACTIONS, PidFilter, audio_mode_from_legacy,
                        choose_from_pmt, effective_format, expand_scheme, keep_pids, needs_pmt,
                        usable_extras)
from .tuner import SegmentReader
# systemd-logind methods; D-Bus works natively and inside the Flatpak sandbox.
AFTER_COMMANDS = {"Standby": "Suspend", "Hibernate": "Hibernate", "Power off": "PowerOff"}
# stored in timers as indices; the names are translated where they are shown
WEEKDAYS = [N_("Mo"), N_("Tu"), N_("We"), N_("Th"), N_("Fr"), N_("Sa"), N_("Su")]


def safe(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .") or "_"


def file_name(scheme, station, event, start, ext=".ts", end=None):
    """Expand the naming scheme placeholders (see recformat.PLACEHOLDERS)."""
    return expand_scheme(scheme, station, event, start, end) + ext


def power_action(method):
    """Ask systemd-logind to suspend, hibernate or power off."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        bus.call(
            "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
            method, GLib.Variant("(b)", (True,)), None, Gio.DBusCallFlags.NONE, -1, None, None, None)
    except GLib.Error as e:
        print(f"LinDVB: {method} failed: {e.message}")


@dataclass
class Timer:
    channel_key: str
    channel_name: str
    title: str
    start: int
    end: int
    days: list = field(default_factory=list)      # weekday indices for repeating timers
    after: str = "None"
    enabled: bool = True
    status: str = "Scheduled"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    series: bool = False
    # start/end are the programme times; recording runs from start - lead to
    # end + follow (minutes). Timers saved by older versions have the margins
    # already inside start/end and lead = follow = 0.
    lead: int = 0
    follow: int = 0
    action: str = "Record"
    name_scheme: str = ""          # empty: the scheme from the preferences
    folder: str = ""               # empty: the recording folder from the preferences
    tv_format: str = "TS"
    radio_format: str = "TS"
    teletext: bool = True
    subtitles: bool = True
    epg_data: bool = True
    audio_mode: str = "all"        # see recformat.AUDIO_MODES
    # True: formats, audio tracks and extra streams come from the preferences
    # when the recording starts (new timers); False: the values above
    general: bool = False

    @property
    def rec_start(self):
        return self.start - self.lead * 60

    @property
    def rec_end(self):
        return self.end + self.follow * 60

    @property
    def extras(self):
        return [e for e in EXTRAS if getattr(self, e)]

    @property
    def records(self):
        return self.action in ("Record", "Record and tune")

    @property
    def tunes(self):
        return self.action in ("Tune only", "Record and tune")

    def next_occurrence(self, now=None):
        """Move a repeating timer to its next day once it has run."""
        now = now or time.time()
        dur = self.end - self.start
        while self.rec_end < now and self.days:
            self.start += 86400
            while datetime.fromtimestamp(self.start).weekday() not in self.days:
                self.start += 86400
            self.end = self.start + dur
            self.status = N_("Scheduled")


@dataclass
class SeriesRule:
    title: str
    channel_key: str = ""       # empty: any channel
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # recording options of the whole series (tv_format, …); empty: the preferences
    options: dict = field(default_factory=dict)

    def matches(self, timer):
        return timer.series and timer.title.lower() == self.title.lower() and \
            (not self.channel_key or self.channel_key == timer.channel_key)


class Recording:
    """An active recording.

    DVB: reads the capture's segments, keeps the wanted PIDs and either writes
    them as TS or remuxes them (MPG / MP4 / MKV / native audio). Internet TV:
    remuxes the stream from its URL.
    """

    def __init__(self, path, capture=None, url=None, start_offset=None, fmt="TS",
                 channel=None, extras=EXTRAS, audio_mode="all", language="",
                 full_capture=False):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fmt = fmt
        self.extras = set(extras)
        self.audio_mode = audio_mode if audio_mode in AUDIO_MODES else "all"
        self.path = path if fmt == "Audio" else path.with_name(path.name + EXTENSIONS[fmt])
        self.capture = capture
        self.started = time.time()
        self.running = True
        self._bytes = 0
        self.remux = None
        if capture is not None:
            if start_offset is None:
                _, start_offset = capture.store.bounds()
            self.reader = SegmentReader(capture.store, start_offset)
            pids = keep_pids(channel, self.extras, fmt, self.audio_mode) \
                if channel is not None else None
            self.want_sub_page = bool(fmt == "MKV" and "subtitles" in self.extras and
                                      channel is not None and channel.teletext_pid)
            choose = None
            if pids is not None and needs_pmt(self.audio_mode, fmt, language):
                single = fmt in SINGLE_AUDIO
                captured = None if full_capture else list(channel.audio_pids)
                choose = (lambda pmt: choose_from_pmt(pmt, pids, channel.audio_pids,
                                                      self.audio_mode, language, single,
                                                      captured))
            want_pmt = self.want_sub_page or choose is not None
            self.filter = PidFilter(pids, channel.pmt_pid if want_pmt and channel else 0, choose)
            if fmt != "TS":
                # The PID filter has already picked the audio tracks.
                self.remux = self._remuxer(program=channel.sid if channel else 0,
                                           all_audio=fmt not in SINGLE_AUDIO)
                self.remux.start()
            self.thread = threading.Thread(target=self._copy, daemon=True)
            self.thread.start()
        else:
            # Internet TV: HLS / DASH streams are chosen by language from the
            # stream list; other streams have no PMT to go by, so "language"
            # keeps the first track there.
            self.remux = self._remuxer(url=url, all_audio=self.audio_mode == "all" and
                                       fmt not in SINGLE_AUDIO,
                                       audio_mode=self.audio_mode, language=language)
            self.remux.start()

    def _remuxer(self, url=None, program=0, all_audio=True, **kw):
        from .remux import Remuxer
        return Remuxer(self.fmt, self.path, url=url, program=program, all_audio=all_audio,
                       on_done=lambda r: setattr(self, "path", Path(r.path)), **kw)

    @property
    def bytes(self):
        if self.remux is None:
            return self._bytes
        try:
            return Path(self.remux.path).stat().st_size
        except OSError:
            return 0

    def _subtitle_page(self):
        from .teletext import DEFAULT_SUBTITLE_PAGES, subtitle_pages_from_pmt
        try:
            _prog, pages = subtitle_pages_from_pmt(self.filter.pmt)
        except Exception:      # noqa: BLE001 - a broken PMT only costs the subtitles
            pages = []
        return pages[0].page if pages else DEFAULT_SUBTITLE_PAGES[0]

    def _copy(self):
        out = open(self.path, "wb") if self.remux is None else None
        held = []          # MKV subtitles: hold data until the PMT names the page
        try:
            while self.running:
                data = self.reader.read()
                if data is None:
                    break
                if not data:
                    time.sleep(0.1)
                    continue
                data = self.filter.feed(data)
                if out is not None:
                    out.write(data)
                    self._bytes += len(data)
                    continue
                if self.want_sub_page and not self.remux.subtitle_page:
                    held.append(data)
                    if self.filter.pmt is None and sum(map(len, held)) < (4 << 20):
                        continue
                    self.remux.subtitle_page = self._subtitle_page()
                    data, held = b"".join(held), []
                self.remux.push(data)
        finally:
            rest = self.filter.flush()
            if rest and out is not None:
                out.write(rest)
                self._bytes += len(rest)
            elif rest:
                held.append(rest)
            if out is not None:
                out.close()
            if self.remux is not None:
                if held:
                    self.remux.push(b"".join(held))
                GLib.idle_add(self.remux.stop)
            self.reader.close()

    def stop(self):
        self.running = False
        if self.remux is not None and self.capture is None:
            self.remux.stop()


class TimerManager(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "message": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }
    path = CONFIG_DIR / "timers.json"

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.timers: list[Timer] = []
        self.rules: list[SeriesRule] = []
        self.active = {}           # timer id or "manual:<key>" -> (Recording, capture)
        self.load()
        GLib.timeout_add_seconds(5, self._tick)

    # -- persistence ----------------------------------------------------------
    def load(self):
        try:
            d = json.loads(self.path.read_text())
            names = {f.name for f in fields(Timer)}
            for t in d.get("timers", []):
                if "audio_mode" not in t and "all_audio" in t:
                    t["audio_mode"] = audio_mode_from_legacy(t["all_audio"])
            self.timers = [Timer(**{k: v for k, v in t.items() if k in names})
                           for t in d.get("timers", [])]
            self.rules = [SeriesRule(**r) for r in d.get("rules", [])]
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"timers": [asdict(t) for t in self.timers],
                                         "rules": [asdict(r) for r in self.rules]}, indent=1))
        self.emit("changed")

    # -- timers ---------------------------------------------------------------
    def defaults(self):
        """Recording options for new timers and manual recordings (preferences)."""
        s = self.app.settings
        mode = s.get("rec_audio_mode", "all")
        return {"tv_format": s.get("rec_tv_format", "TS"),
                "radio_format": s.get("rec_radio_format", "TS"),
                "audio_mode": mode if mode in AUDIO_MODES else "all",
                **{e: bool(s.get("rec_" + e, True)) for e in EXTRAS}}

    def new_timer(self, channel, title, start, end, **kw):
        """A Timer with the default recording options from the preferences."""
        return Timer(channel.key, channel.name, title, int(start), int(end),
                     **{**self.defaults(), "general": True, **kw})

    def options_of(self, timer):
        """The recording options a timer records with (dict)."""
        if timer.general:
            return self.defaults()
        return {"tv_format": timer.tv_format, "radio_format": timer.radio_format,
                "audio_mode": timer.audio_mode, **{e: getattr(timer, e) for e in EXTRAS}}

    def rule_of(self, timer):
        return next((r for r in self.rules if r.matches(timer)), None)

    def set_series_options(self, timer, options):
        """Use *options* (None: the preferences) for the whole series of
        *timer*: its rule and every scheduled timer of it."""
        rule = self.rule_of(timer)
        if rule is not None:
            rule.options = dict(options or {})
        for t in self.timers:
            if t.id in self.active or not (rule.matches(t) if rule else t.id == timer.id):
                continue
            t.general = options is None
            for k, v in (options or {}).items():
                setattr(t, k, v)

    def add(self, timer, with_margins=True):
        s = self.app.settings
        if with_margins:
            timer.lead = s["lead_minutes"]
            timer.follow = s["follow_minutes"]
        self.timers.append(timer)
        self.timers.sort(key=lambda t: t.rec_start)
        self.save()
        return timer

    def add_from_event(self, channel, ev, series=False, edit=True):
        t = self.add(self.new_timer(channel, ev.title, ev.start, ev.end, series=series))
        self.emit("message", f"Timer set: {ev.title}")
        if edit and not series and self.app.settings.get("timer_edit_after_create") \
                and hasattr(self.app, "show_timers"):
            GLib.idle_add(lambda: (self.app.show_timers(t), False)[1])
        return t

    def remove(self, timer):
        if timer.id in self.active:
            self._stop(timer.id)
        self.timers = [t for t in self.timers if t.id != timer.id]
        self.save()

    def add_series(self, title, channel_key=""):
        self.rules.append(SeriesRule(title, channel_key))
        self.apply_series()
        self.save()

    def apply_series(self):
        """Create timers for EPG events matching a series rule."""
        known = {(t.channel_key, t.title, t.start) for t in self.timers}
        lead = self.app.settings["lead_minutes"] * 60
        for rule in self.rules:
            for ch in self.app.db.channels:
                if rule.channel_key and ch.key != rule.channel_key:
                    continue
                key = ch.epg_key
                for ev in self.app.epg.for_key(key):
                    # Older timers have the lead time inside their start.
                    if ev.title.lower() == rule.title.lower() and \
                            (ch.key, ev.title, ev.start) not in known and \
                            (ch.key, ev.title, ev.start - lead) not in known:
                        opts = dict(rule.options, general=False) if rule.options else {}
                        self.add(self.new_timer(ch, ev.title, ev.start, ev.end, series=True,
                                                **opts))
                        known.add((ch.key, ev.title, ev.start))

    # -- manual recording -----------------------------------------------------
    def record_now(self, channel, capture=None, title="", start_offset=None, options=None):
        """Start or stop a manual recording. *options* (a dict with tv_format,
        radio_format, audio_mode and the extras) replaces the defaults for this
        one recording."""
        key = "manual:" + channel.key
        if key in self.active:
            self._stop(key)
            self.emit("message", "Recording stopped")
            return False
        opts = Timer("", "", "", 0, 0, **{**self.defaults(), **(options or {})})
        self._start(key, channel, title or channel.name, capture, start_offset, options=opts)
        return True

    def is_recording(self, channel):
        return ("manual:" + channel.key) in self.active or any(
            t.channel_key == channel.key and t.id in self.active for t in self.timers)

    def stop_after(self, channel, seconds):
        """Schedule the end of a manual recording (sleep-recording)."""
        key = "manual:" + channel.key
        if key in self.active:
            GLib.timeout_add_seconds(int(seconds), lambda: (self._stop(key), False)[1])

    def _start(self, key, channel, title, capture=None, start_offset=None, timer=None,
               options=None):
        s = self.app.settings
        opts = timer or options or Timer("", "", "", 0, 0, **self.defaults())
        if opts.general:
            opts = Timer("", "", "", 0, 0, **{**self.defaults(), "name_scheme": opts.name_scheme,
                                              "folder": opts.folder})
        audio = {"audio_mode": opts.audio_mode, "language": s.get("rec_audio_language", "")}
        full = bool(s.get("full_transponder")) or not channel.pmt_pid
        extras = usable_extras(opts.extras, channel.is_iptv)
        fmt = effective_format(opts.radio_format if channel.radio else opts.tv_format,
                               extras, channel.radio, channel.is_iptv)
        if fmt != "TS":
            from .remux import available
            if not available(fmt):
                self.emit("message", f"{fmt} needs more GStreamer plugins, recording as TS")
                fmt = "TS"
        start = time.time()
        end = timer.rec_end if timer is not None else None
        name = expand_scheme(opts.name_scheme or s["name_scheme"], channel.name, title, start, end)
        path = Path(opts.folder or s["recording_dir"]).expanduser() / name
        if channel.is_iptv and not channel.url:
            self.emit("message", "Recording failed: this channel is an HbbTV application "
                                 "without a stream of its own")
            return None
        try:
            if channel.is_iptv:
                rec, cap = Recording(path, url=channel.url, fmt=fmt, channel=channel,
                                     extras=extras, **audio), None
            else:
                cap = capture or self.app.tuners.acquire(channel, key)
                cap.users.add(key)
                rec = Recording(path, capture=cap, start_offset=start_offset, fmt=fmt,
                                channel=channel, extras=extras, full_capture=full, **audio)
        except (RuntimeError, OSError) as e:
            self.emit("message", f"Recording failed: {e}")
            return None
        self.active[key] = (rec, cap)
        self._write_info(rec, channel, title, start, fmt)
        rec.channel, rec.title = channel, title
        self._plugin_event("recording-started", rec)
        self.emit("message", f"Recording: {path.name}")
        self.emit("changed")
        return rec

    def _write_info(self, rec, channel, title, start, fmt):
        """Programme details beside the recording for the Recordings tab."""
        from .library import write_info
        event = None
        epg = getattr(self.app, "epg", None)
        if epg is not None and getattr(channel, "epg_key", None):
            try:
                now, nxt = epg.now_next(channel.epg_key)
            except Exception:       # noqa: BLE001 - details are optional
                now = nxt = None
            # a timer with lead time starts before its programme: take the next one
            for ev in (now, nxt):
                if ev is not None and ev.start <= start + 15 * 60 < ev.start + ev.duration:
                    event = ev
                    break
            if event is None and now is not None and now.title.lower() in title.lower():
                event = now
        write_info(rec.path, channel.name, title, event, start, fmt=fmt)

    def _stop(self, key):
        rec, cap = self.active.pop(key, (None, None))
        if rec:
            rec.stop()
            from .library import write_info
            write_info(rec.path, end=time.time())
            self._plugin_event("recording-stopped", rec)
        if cap:
            self.app.tuners.release(cap, key)
        self.emit("changed")

    def _plugin_event(self, event, rec):
        plugins = getattr(self.app, "plugins", None)
        if plugins is not None:
            plugins.emit(event, getattr(rec, "channel", None),
                         {"title": getattr(rec, "title", ""), "file": str(rec.path)})

    def stop_all(self):
        for key in list(self.active):
            self._stop(key)

    # -- scheduler ------------------------------------------------------------
    def _tick(self):
        now = time.time()
        changed = False
        for t in list(self.timers):
            if not t.enabled:
                continue
            if t.id in self.active or t.status == "Tuned":
                if now >= t.rec_end:
                    self._stop(t.id)
                    t.status = N_("Done")
                    changed = True
                    if t.days:
                        t.next_occurrence(now)
                    self._after(t)
            elif t.rec_start <= now < t.rec_end and t.status == "Scheduled":
                ch = self.app.db.by_key(t.channel_key)
                if ch is None:
                    t.status = N_("Channel missing")
                else:
                    if t.tunes and getattr(self.app, "window", None) is not None:
                        self.app.window.zap(ch)
                    if not t.records:
                        t.status = N_("Tuned")
                    elif self._start(t.id, ch, t.title, timer=t):
                        t.status = N_("Recording")
                    else:
                        t.status = N_("Failed")
                changed = True
            elif now >= t.rec_end and t.status == "Scheduled":
                t.status = N_("Missed")
                t.next_occurrence(now)
                changed = True
        if changed:
            self.save()
        return True

    def _after(self, t):
        """The action after recording, once no other recording is running."""
        if self.active or t.after == "None":
            return
        self.emit("message", _("After recording: {action}").format(action=_(t.after)))
        if t.after in AFTER_COMMANDS:
            power_action(AFTER_COMMANDS[t.after])
        elif t.after == "Quit LinDVB":
            GLib.timeout_add_seconds(3, lambda: (self.app.quit(), False)[1])

    def next_wakeup(self):
        """Earliest future timer start (for rtcwake)."""
        future = [t.rec_start for t in self.timers if t.enabled and t.rec_start > time.time()]
        return min(future) if future else None
