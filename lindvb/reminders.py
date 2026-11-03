"""Reminders for programmes: "tell me when it starts" from the programme guide.

A reminder fires a desktop notification a few minutes before the programme
(Preferences > EPG, "Remind before start"); the notification switches to the
channel. Reminders are kept in reminders.json and dropped once the programme
has ended.
"""

import json
import time
from dataclasses import asdict, dataclass

from .config import CONFIG_DIR


@dataclass
class Reminder:
    channel_key: str
    channel_name: str
    start: int
    duration: int
    title: str
    fired: bool = False

    @property
    def key(self):
        return f"{self.channel_key}@{self.start}"

    @property
    def end(self):
        return self.start + self.duration


class ReminderStore:
    path = CONFIG_DIR / "reminders.json"

    def __init__(self):
        self.items = {}
        try:
            for d in json.loads(self.path.read_text()):
                r = Reminder(**d)
                self.items[r.key] = r
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([asdict(r) for r in self.items.values()]))
        except OSError:
            pass

    def add(self, channel, event):
        r = Reminder(channel.key, channel.name, int(event.start), int(event.duration),
                     event.title)
        self.items[r.key] = r
        self.save()
        return r

    def remove(self, channel_key, start):
        if self.items.pop(f"{channel_key}@{int(start)}", None):
            self.save()

    def has(self, channel_key, start):
        return f"{channel_key}@{int(start)}" in self.items

    def upcoming(self, now=None):
        now = now or time.time()
        return sorted((r for r in self.items.values() if r.end > now), key=lambda r: r.start)

    def due(self, lead_seconds, now=None):
        """Reminders to fire now (marked as fired), oldest first; drops ended ones."""
        now = now or time.time()
        out, changed = [], False
        for k, r in list(self.items.items()):
            if r.end <= now:
                del self.items[k]
                changed = True
            elif not r.fired and r.start - lead_seconds <= now:
                r.fired = True
                out.append(r)
                changed = True
        if changed:
            self.save()
        return sorted(out, key=lambda r: r.start)
