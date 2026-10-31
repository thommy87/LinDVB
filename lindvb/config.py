"""Settings and file locations (XDG base directories)."""

import json
import os
from pathlib import Path

APP_ID = "io.github.thommy87.LinDVB"
APP_NAME = "LinDVB"


def _xdg(var, fallback):
    return Path(os.environ.get(var) or (Path.home() / fallback))


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "lindvb"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "lindvb"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "lindvb"

DEFAULTS = {
    # Tuners: list of adapter numbers that LinDVB may use, in order of preference.
    "adapters": [0],
    "frontend": 0,
    # Satellite LNB (kHz). Universal LNB by default.
    "lnb_lof1": 9750000,
    "lnb_lof2": 10600000,
    "lnb_slof": 11700000,
    # Capture whole transponder instead of the service PIDs (needed for all audio
    # tracks when the channel list only knows one audio PID).
    "full_transponder": False,
    "timeshift_dir": str(CACHE_DIR / "timeshift"),
    "timeshift_max_mb": 4096,
    "recording_dir": str(Path.home() / "Videos" / "LinDVB"),
    "name_scheme": "%station_%date_%time_%event",
    "lead_minutes": 2,
    "follow_minutes": 5,
    "screenshot_dir": str(Path.home() / "Pictures" / "LinDVB"),
    "volume": 0.8,
    "last_channel": None,
    "xmltv_url": "",
    "epg_search_urls": [
        ["Wikipedia (en)", "https://en.wikipedia.org/wiki/Special:Search/{q}"],
        ["IMDb", "https://www.imdb.com/find?q={q}"],
        ["TMDB", "https://www.themoviedb.org/search?query={q}"],
    ],
    "osd_seconds": 4,
    "deinterlace": True,
}


class Settings(dict):
    path = CONFIG_DIR / "settings.json"

    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            self.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, indent=2))
        tmp.replace(self.path)
