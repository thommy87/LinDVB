"""Finding the recordings on disk for the "Recordings" tab (pure logic, no GTK).

Recordings live in the recording folder from the preferences and in any folder
a timer records into. A naming scheme may put them into sub folders
(``%station/%title``), so the folders are searched a few levels deep.
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path

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

    @property
    def key(self):
        return str(self.path)

    @property
    def title(self):
        return self.path.stem

    @property
    def is_audio(self):
        return self.path.suffix.lower() in AUDIO_EXTENSIONS

    def details(self):
        """``Mon 02.11. 20:15 · 1.2 GB`` for the second line of the list row."""
        when = time.strftime("%a %d.%m.%Y %H:%M", time.localtime(self.mtime))
        return f"{when} · {human_size(self.size)}"


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
                found.append(RecordingFile(p, st.st_size, st.st_mtime))
    found.sort(key=lambda r: r.mtime, reverse=True)
    return found
