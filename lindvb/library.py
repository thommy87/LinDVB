# SPDX-License-Identifier: GPL-2.0-or-later
"""Recording library: programme details beside each recording and resume positions.

When LinDVB records, it writes ``<name>.lindvb.json`` next to the file with the
channel and the programme on air (title, subheading, description, genre) and
the start and end time. The Recordings tab shows these details, and remembers
where you stopped watching each file (``positions.json`` in the data folder),
so a recording continues where you left it.
"""

import json
import time
from pathlib import Path

from .config import DATA_DIR

SIDECAR_SUFFIX = ".lindvb.json"
# Resume only when more than this was watched and the end is not near.
RESUME_MIN_SECONDS = 30
WATCHED_FRACTION = 0.95


def sidecar_path(path):
    """``Film.ts`` -> ``Film.lindvb.json`` (the same for every container)."""
    path = Path(path)
    return path.with_name(path.stem + SIDECAR_SUFFIX)


def write_info(path, channel_name="", title="", event=None, start=None, end=None, fmt=""):
    """Write (or update) the details of a recording. ``event`` is an epg.Event."""
    info = read_info(path)
    info.update({k: v for k, v in {
        "channel": channel_name, "title": title, "format": fmt,
        "start": int(start) if start else None, "end": int(end) if end else None,
    }.items() if v})
    if event is not None:
        info.update({k: v for k, v in {
            "title": event.title, "subtitle": event.subtitle,
            "description": event.description, "genre": event.genre,
            "year": event.year or None, "country": event.country,
            "event_start": event.start, "event_duration": event.duration,
        }.items() if v})
    try:
        sidecar_path(path).write_text(json.dumps(info, ensure_ascii=False, indent=1))
    except OSError:
        pass
    return info


def read_info(path):
    try:
        data = json.loads(sidecar_path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def delete_info(path):
    try:
        sidecar_path(path).unlink()
    except OSError:
        pass


class Positions:
    """Where each file was left: {path: {"pos": s, "dur": s, "at": unix time}}."""

    path = DATA_DIR / "positions.json"
    KEEP = 500          # the most recently watched files

    def __init__(self):
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        if len(self.data) > self.KEEP:
            newest = sorted(self.data.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)
            self.data = dict(newest[:self.KEEP])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
        except OSError:
            pass

    def update(self, file, pos, dur):
        if not dur or dur <= 0:
            return
        self.data[str(file)] = {"pos": round(max(0.0, pos), 1), "dur": round(dur, 1),
                                "at": int(time.time())}

    def get(self, file):
        return self.data.get(str(file))

    def forget(self, file):
        self.data.pop(str(file), None)

    def progress(self, file):
        """Watched fraction 0..1 (0 when never played)."""
        d = self.get(file)
        if not d or not d.get("dur"):
            return 0.0
        return min(1.0, d["pos"] / d["dur"])

    def watched(self, file):
        return self.progress(file) >= WATCHED_FRACTION

    def resume_at(self, file):
        """Position to continue from, or None to start at the beginning."""
        d = self.get(file)
        if not d:
            return None
        pos, dur = d.get("pos", 0), d.get("dur", 0)
        if pos < RESUME_MIN_SECONDS or (dur and pos >= dur * WATCHED_FRACTION) or \
                (dur and dur - pos < 60):
            return None
        return pos
