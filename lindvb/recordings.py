# SPDX-License-Identifier: GPL-2.0-or-later
"""Finding the recordings on disk for the "Recordings" tab (pure logic, no GTK).

Recordings live in the recording folder from the preferences and in any folder
a timer records into. A naming scheme may put them into sub folders
(``%station/%title``), so the folders are searched a few levels deep.
"""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .library import read_info

VIDEO_EXTENSIONS = {".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".mp4", ".m4v", ".mkv",
                    ".avi", ".webm"}
AUDIO_EXTENSIONS = {".mp2", ".mp3", ".aac", ".ac3", ".eac3", ".m4a", ".ogg", ".opus",
                    ".flac", ".wav"}
MAX_DEPTH = 4


@dataclass
class RecordingFile:
    path: Path
    size: int
    mtime: float
    info: dict = field(default_factory=dict)    # from <name>.lindvb.json (lindvb.library)

    @property
    def key(self):
        return str(self.path)

    @property
    def title(self):
        return self.info.get("title") or self.path.stem

    @property
    def subtitle(self):
        return self.info.get("subtitle", "")

    @property
    def channel(self):
        return self.info.get("channel", "")

    @property
    def description(self):
        return self.info.get("description", "")

    @property
    def start(self):
        return self.info.get("start") or self.mtime

    @property
    def duration(self):
        """Recorded length in seconds from the start and end time, 0 when unknown."""
        s, e = self.info.get("start"), self.info.get("end")
        return max(0, int(e - s)) if s and e else 0

    def search_text(self):
        return " ".join((self.title, self.subtitle, self.channel, self.path.name)).lower()

    @property
    def is_audio(self):
        return self.path.suffix.lower() in AUDIO_EXTENSIONS

    def details(self):
        """``ZDF · Mon 02.11.2026 20:15 · 90 min · 1.2 GB`` for the list row."""
        when = time.strftime("%a %d.%m.%Y %H:%M", time.localtime(self.start))
        parts = [self.channel, when]
        if self.duration:
            parts.append(f"{self.duration // 60} min")
        parts.append(human_size(self.size))
        return " · ".join(p for p in parts if p)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def is_recording_file(path):
    suffix = Path(path).suffix.lower()
    return suffix in VIDEO_EXTENSIONS or suffix in AUDIO_EXTENSIONS


def find_recordings(folders, max_depth=MAX_DEPTH):
    """All media files below ``folders``, newest first. Folders that do not
    exist (yet) are skipped; a file found through two folders is listed once."""
    seen, found = set(), []
    for folder in folders:
        if not folder:
            continue
        root = Path(folder).expanduser()
        if not root.is_dir():
            continue
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if len(Path(dirpath).parts) - base >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith(".") or not is_recording_file(name):
                    continue
                p = Path(dirpath) / name
                try:
                    real = p.resolve()
                    st = p.stat()
                except OSError:
                    continue
                if real in seen:
                    continue
                seen.add(real)
                found.append(RecordingFile(p, st.st_size, st.st_mtime, read_info(p)))
    found.sort(key=lambda r: r.start, reverse=True)
    return found


SORT_KEYS = {
    "newest": (lambda r: -r.start),
    "oldest": (lambda r: r.start),
    "title": (lambda r: (r.title.lower(), -r.start)),
    "channel": (lambda r: (r.channel.lower(), -r.start)),
    "size": (lambda r: -r.size),
}


def sort_recordings(recs, how="newest"):
    return sorted(recs, key=SORT_KEYS.get(how, SORT_KEYS["newest"]))
