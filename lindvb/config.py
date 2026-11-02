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

# Where user plugins live. Additional directories can be given in the
# LINDVB_PLUGIN_PATH environment variable (os.pathsep separated).
PLUGIN_DIR = DATA_DIR / "plugins"

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
    "name_scheme": "%station_%date_%time_%name",
    # Default recording options for new timers and manual recordings.
    # TV: "TS" "MPG" "MP4" "MKV"; radio: "Audio" (native MP2/MP3/AAC/AC3) or "TS".
    # Teletext and EPG data need TS; subtitles TS or MKV (see recformat.py).
    "rec_tv_format": "TS",
    "rec_radio_format": "TS",
    "rec_teletext": True,
    "rec_subtitles": True,
    "rec_all_audio": True,
    "rec_epg_data": True,
    # After "Record" in the EPG, open the timer window with the new timer.
    "timer_edit_after_create": False,
    "lead_minutes": 2,
    "follow_minutes": 5,
    "screenshot_dir": str(Path.home() / "Pictures" / "LinDVB"),
    "volume": 0.8,
    # Last tuned channel (TV or radio, tuned again at start) and the last
    # channel of each kind, tuned when switching between TV and radio.
    "last_channel": None,
    "last_tv_channel": None,
    "last_radio_channel": None,
    "mode": "tv",                    # channel list shows "tv" or "radio"
    # Radio visualisation: a built-in style (glow, nebula, bars, vu, scope, off)
    # or "plugin:<name>" for one registered by a plugin.
    "visualizer": "glow",
    "xmltv_url": "",
    "epg_search_urls": [
        ["Wikipedia (en)", "https://en.wikipedia.org/wiki/Special:Search/{q}"],
        ["IMDb", "https://www.imdb.com/find?q={q}"],
        ["TMDB", "https://www.themoviedb.org/search?query={q}"],
    ],
    "osd_seconds": 4,
    # EPG ---------------------------------------------------------------------
    # Keep the guide on disk so it survives the end of the program.
    "epg_save": True,
    # Preferred EPG language (ISO 639-2, "" = as broadcast).
    "epg_language": "",
    # How long the programme info is shown on a channel change (0 = never).
    "epg_info_seconds": 4,
    # Time zone for EPG times: automatic (system) or a manual offset to GMT.
    "epg_tz_auto": True,
    "epg_tz_offset": 60,             # minutes east of GMT
    # Transponders that carry a provider EPG: [frequency MHz, "h"/"v", type]
    # with type "mhw1", "mhw2" or "freesat" (see lindvb.epgsources).
    "epg_provider_freqs": [[10847, "v", "mhw2"], [11428, "h", "freesat"]],
    "deinterlace": True,
    # Internet streams: "auto" plays HLS with separate audio renditions on
    # playbin3 (keeps sound and picture in sync), everything else on playbin;
    # "playbin" or "playbin3" forces one of them.
    "stream_engine": "auto",
    # Per-channel audio delay in ms (+ = sound later), set with J/K.
    "audio_delays": {},
    # Accent colour: "default" (LinDVB blue), "system" (desktop accent) or
    # "custom" (accent_custom, "#RRGGBB").
    "accent_mode": "default",
    "accent_custom": "#119CFF",
    # Channel editor sections that are unfolded.
    "editor_sections_open": ["general"],
    # Plugins -----------------------------------------------------------------
    # Load plugins from the plugins directory (DATA_DIR/plugins and the
    # LINDVB_PLUGIN_PATH entries) at startup.
    "plugins_enabled": True,
    "plugins_disabled": [],          # plugin names the user has switched off
    # Conditional access (descrambling of the user's own encrypted channels).
    # Provider for descrambling:
    #   "none"   - do not descramble (encrypted channels stay scrambled)
    #   "dvbapi" - client of an external softcam (OSCam & co.) over the DVBAPI
    #              socket; the softcam reads the user's own subscription card
    #              and returns the control words, LinDVB only applies them
    #   "ci"     - a hardware CI/CI+ module in the adapter's CA device
    #   or the name of a CA plugin that provides control words
    "ca_provider": "none",
    # DVBAPI socket of the external softcam. Unix socket path, or "host:port".
    "ca_dvbapi_socket": "/tmp/camd.socket",
    # Hardware CI: which /dev/dvb/adapterN CA device to use.
    "ca_ci_adapter": 0,
    "ca_ci_device": 0,
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
