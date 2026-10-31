"""Recordings, timer recordings and series recording rules."""

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from gi.repository import Gio, GLib, GObject

from .config import CONFIG_DIR
from .tuner import IPTVRecorder, SegmentReader

AFTER_ACTIONS = ["None", "Standby", "Hibernate", "Power off"]
# systemd-logind methods; D-Bus works natively and inside the Flatpak sandbox.
AFTER_COMMANDS = {"Standby": "Suspend", "Hibernate": "Hibernate", "Power off": "PowerOff"}
WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def safe(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .") or "_"


def file_name(scheme, station, event, start):
    """Expand the naming scheme placeholders: %year %date %time %station %event."""
    dt = datetime.fromtimestamp(start)
    rep = {"%year": dt.strftime("%Y"), "%date": dt.strftime("%m-%d"),
           "%time": dt.strftime("%H-%M"), "%station": safe(station),
           "%event": safe(event or "Recording")}
    out = scheme
    for k, v in rep.items():
        out = out.replace(k, v)
    return safe(out) + ".ts"


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

    def next_occurrence(self, now=None):
        """Move a repeating timer to its next day once it has run."""
        now = now or time.time()
        dur = self.end - self.start
        while self.end < now and self.days:
            self.start += 86400
            while datetime.fromtimestamp(self.start).weekday() not in self.days:
                self.start += 86400
            self.end = self.start + dur
            self.status = "Scheduled"


@dataclass
class SeriesRule:
    title: str
    channel_key: str = ""       # empty: any channel
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


class Recording:
    """An active recording. DVB: copies from the capture segments; IPTV: remux."""

    def __init__(self, path, capture=None, url=None, start_offset=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.capture = capture
        self.started = time.time()
        self.running = True
        self.bytes = 0
        if capture is not None:
            if start_offset is None:
                _, start_offset = capture.store.bounds()
            self.reader = SegmentReader(capture.store, start_offset)
            self.thread = threading.Thread(target=self._copy, daemon=True)
            self.thread.start()
            self.iptv = None
        else:
            self.iptv = IPTVRecorder(url, self.path)
            self.iptv.start()

    def _copy(self):
        with open(self.path, "wb") as out:
            while self.running:
                data = self.reader.read()
                if data is None:
                    break
                if not data:
                    time.sleep(0.1)
                    continue
                out.write(data)
                self.bytes += len(data)
        self.reader.close()

    def stop(self):
        self.running = False
        if self.iptv:
            self.iptv.stop()


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
            self.timers = [Timer(**t) for t in d.get("timers", [])]
            self.rules = [SeriesRule(**r) for r in d.get("rules", [])]
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"timers": [asdict(t) for t in self.timers],
                                         "rules": [asdict(r) for r in self.rules]}, indent=1))
        self.emit("changed")

    # -- timers ---------------------------------------------------------------
    def add(self, timer, with_margins=True):
        s = self.app.settings
        if with_margins:
            timer.start -= s["lead_minutes"] * 60
            timer.end += s["follow_minutes"] * 60
        self.timers.append(timer)
        self.timers.sort(key=lambda t: t.start)
        self.save()
        return timer

    def add_from_event(self, channel, ev, series=False):
        t = self.add(Timer(channel.key, channel.name, ev.title, ev.start, ev.end, series=series))
        self.emit("message", f"Timer set: {ev.title}")
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
                    if ev.title.lower() == rule.title.lower() and \
                            (ch.key, ev.title, ev.start - lead) not in known:
                        self.add(Timer(ch.key, ch.name, ev.title, ev.start, ev.end, series=True))
                        known.add((ch.key, ev.title, ev.start - lead))

    # -- manual recording -----------------------------------------------------
    def record_now(self, channel, capture=None, title="", start_offset=None):
        key = "manual:" + channel.key
        if key in self.active:
            self._stop(key)
            self.emit("message", "Recording stopped")
            return False
        self._start(key, channel, title or channel.name, capture, start_offset)
        return True

    def is_recording(self, channel):
        return ("manual:" + channel.key) in self.active or any(
            t.channel_key == channel.key and t.id in self.active for t in self.timers)

    def stop_after(self, channel, seconds):
        """Schedule the end of a manual recording (sleep-recording)."""
        key = "manual:" + channel.key
        if key in self.active:
            GLib.timeout_add_seconds(int(seconds), lambda: (self._stop(key), False)[1])

    def _start(self, key, channel, title, capture=None, start_offset=None):
        s = self.app.settings
        name = file_name(s["name_scheme"], channel.name, title, time.time())
        path = Path(s["recording_dir"]).expanduser() / name
        try:
            if channel.is_iptv:
                rec, cap = Recording(path, url=channel.url), None
            else:
                cap = capture or self.app.tuners.acquire(channel, key)
                cap.users.add(key)
                rec = Recording(path, capture=cap, start_offset=start_offset)
        except RuntimeError as e:
            self.emit("message", f"Recording failed: {e}")
            return None
        self.active[key] = (rec, cap)
        self.emit("message", f"Recording: {path.name}")
        self.emit("changed")
        return rec

    def _stop(self, key):
        rec, cap = self.active.pop(key, (None, None))
        if rec:
            rec.stop()
        if cap:
            self.app.tuners.release(cap, key)
        self.emit("changed")

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
            if t.id in self.active:
                if now >= t.end:
                    self._stop(t.id)
                    t.status = "Done"
                    changed = True
                    if t.days:
                        t.next_occurrence(now)
                    if t.after in AFTER_COMMANDS and not self.active:
                        self.emit("message", f"After recording: {t.after}")
                        power_action(AFTER_COMMANDS[t.after])
            elif t.start <= now < t.end and t.status == "Scheduled":
                ch = self.app.db.by_key(t.channel_key)
                if ch is None:
                    t.status = "Channel missing"
                elif self._start(t.id, ch, t.title):
                    t.status = "Recording"
                else:
                    t.status = "Failed"
                changed = True
            elif now >= t.end and t.status == "Scheduled":
                t.status = "Missed"
                t.next_occurrence(now)
                changed = True
        if changed:
            self.save()
        return True

    def next_wakeup(self):
        """Earliest future timer start (for rtcwake)."""
        future = [t.start for t in self.timers if t.enabled and t.start > time.time()]
        return min(future) if future else None
