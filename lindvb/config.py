# SPDX-License-Identifier: GPL-2.0-or-later
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
    # Tuner profiles (see lindvb/tunerprofiles.py): adapter, frontend, delivery
    # systems, on/off, priority, shared antenna cable and an LNB of their own.
    # Empty: the older settings below are used.
    "tuners": [],
    # Older settings: adapter numbers LinDVB may use and one frontend for all.
    "adapters": [0],
    "frontend": 0,
    # Satellite LNB (kHz). Universal LNB by default.
    "lnb_lof1": 9750000,
    "lnb_lof2": 10600000,
    "lnb_slof": 11700000,
    # Capture whole transponder instead of the service PIDs (needed for all audio
    # tracks when the channel list only knows one audio PID).
    "full_transponder": False,
    # HbbTV (red button): run the channels' interactive applications
    "hbbtv_enabled": True,
    # start a channel's AUTOSTART application by itself (else only with the red button)
    "hbbtv_autostart": False,
    "hbbtv_third_party_cookies": False,
    "hbbtv_country": "DEU",             # configuration.countryId (ISO 3166 alpha-3)
    # what applications are told about the viewer (configuration object, and
    # the first audio / subtitle choice in their DASH videos)
    "hbbtv_subtitles": False,
    "hbbtv_audio_description": False,
    "hbbtv_language": "",               # ISO 639-2; "" = the interface language
    "hbbtv_teletext": True,             # TXT starts the channel's digital teletext app
    "hbbtv_age_limit": 0,               # youth protection: age of the youngest viewer, 0 = off
    "hbbtv_pin": "",                    # PBKDF2 of the youth protection PIN
    "hbbtv_pin_salt": "",
    "hbbtv_companion": False,           # DIAL, app to app and sync on the network (off: local only)
    "hbbtv_device_uuid": "",            # DIAL/UPnP device id, made on first use
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
    "rec_epg_data": True,
    # Audio tracks: "all", "language" (the preferred language only) or "first".
    # MP4 and the radio audio file keep one track either way.
    "rec_audio_mode": "all",
    # Preferred audio language (ISO 639-2, e.g. "deu"); "" = none.
    "rec_audio_language": "",
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
    # Channel list filter (index into the filter list, 1 = favourites) and
    # group ("" = all groups) last chosen, shown again at the next start.
    "list_filter": 0,
    "list_group": "",
    # Radio visualisation: a built-in style (glow, nebula, ribbons, pulsar, supernova, sunset, aurora,
    # goomhd, bars, vu, scope, off) or "plugin:<name>" for one registered by a plugin.
    "visualizer": "glow",
    # Draw the built-in visualisations on the graphics card (OpenGL); cairo
    # on the CPU is used when this is off or OpenGL is not available.
    "visualizer_gpu": True,
    # Size the GStreamer visualisations render at: "auto" (the screen, at most
    # 2560x1440, for projectM) or "WIDTHxHEIGHT". The original GOOM runs on
    # the CPU and uses "goom_resolution" (default 640x360).
    "visualizer_resolution": "auto",
    "goom_resolution": "640x360",
    "xmltv_url": "",
    "epg_search_urls": [
        ["Wikipedia (en)", "https://en.wikipedia.org/wiki/Special:Search/{q}"],
        ["IMDb", "https://www.imdb.com/find?q={q}"],
        ["TMDB", "https://www.themoviedb.org/search?query={q}"],
    ],
    "osd_seconds": 4,
    # Shown over the radio visualisation (Preferences › Visualisation): the
    # station logo tile, the album cover, the song (artist, album, title, from
    # DVB RadioText, internet radio ICY titles or file tags) and a clock.
    "vis_show_logo": True,
    "vis_show_cover": True,
    "vis_show_song": True,
    "vis_show_clock": True,
    # Look up album covers online (artist and title go to the iTunes Search API).
    "vis_cover_online": True,
    # Station logos: image files named after the channel (DVBViewer's
    # channel logos work as they are). "" = DATA_DIR/logos.
    "logo_dir": "",
    # channel analysis ----------------------------------------------------------
    # one line a minute about the reception of the channel on screen (31 days)
    "reception_log": False,
    # warn on screen when the signal margin falls below this many dB
    "analysis_warn": False,
    "analysis_warn_margin": 2.0,
    # EPG ---------------------------------------------------------------------
    # Keep the guide on disk so it survives the end of the program.
    "epg_save": True,
    # programme reminders: notify this many minutes before the start
    "reminder_minutes": 2,
    # media keys and the desktop media controls (MPRIS on the session bus)
    "mpris": True,
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
    # which EPG source wins (lindvb.epgmerge): "auto" or "custom" with the order
    # below; per channel (Channel.key -> order) always a custom order
    "epg_priority_mode": "auto",
    "epg_priority_order": ["eit", "provider", "dvbi", "xmltv"],
    "epg_priority_channels": {},
    "deinterlace": True,
    # Video decoder (lindvb.hwaccel): "auto" (VA-API, NVDEC, V4L2, then
    # software), one interface ("vaapi", "nvdec", "vulkan", "v4l2") or
    # "software". A GPU decoder that fails falls back to the next one.
    "video_decoder": "auto",
    # CEA-608/708 closed captions (ATSC, US cable)
    "closed_captions": False,
    # Internet streams: "auto" plays HLS with separate audio renditions on
    # playbin3 (keeps sound and picture in sync), everything else on playbin;
    # "playbin" or "playbin3" forces one of them.
    "stream_engine": "auto",
    # Per-channel audio delay in ms (+ = sound later), set with J/K.
    "audio_delays": {},
    # Accent colour: "default" (LinDVB blue), "system" (desktop accent) or
    # "custom" (accent_custom, "#RRGGBB").
    # user interface language ("" follows the desktop); see lindvb/i18n.py
    "language": "",
    "accent_mode": "default",
    "accent_custom": "#119CFF",
    # Channel editor sections that are unfolded.
    "editor_sections_open": ["general"],
    # DVB-I (Preferences ▸ Internet, see lindvb.dvbi) --------------------------
    # Country whose DVB-I profile is used (lindvb.dvbi_countries: "DEU",
    # "ITA", "NZL"). The dvbi_* keys below up to dvbi_prefer_broadcast belong
    # to that country; the settings of the other countries are kept in
    # dvbi_country_state and come back when switching to them again.
    "dvbi_country": "DEU",
    "dvbi_country_state": {},
    # Service list registries, asked in this order; the chosen service list,
    # the postcode for the region, the region chosen when several match, and
    # the state of the daily update check.
    "dvbi_registries": ["https://deu.csr.dvbservices.org/query", "https://csr.dvb-i-slr.de/query"],
    "dvbi_list_url": "",
    "dvbi_list_name": "",
    "dvbi_postcode": "",
    "dvbi_region": "",
    "dvbi_last_check": 0,
    "dvbi_last_modified": "",
    "dvbi_etag": "",
    "dvbi_privacy_ack": False,
    # Region from the country's fixed list (Italian regione, Freeview region;
    # lindvb.dvbi_countries.Area.id), the confirmations given with their time
    # ({key: {"given", "time"}}) and whether antenna, cable and satellite
    # come before internet streams (on in New Zealand).
    "dvbi_area": "",
    "dvbi_consents": {},
    "dvbi_prefer_broadcast": False,
    # subscription package whose LCN table is used ("" = the free one)
    "dvbi_package": "",
    # check the chosen list daily by itself (off: only with Update Now)
    "dvbi_auto_update": True,
    # Load the programme guide of DVB-I channels from the list's content
    # guide servers (now/next and 7 days ahead, refreshed every 6 hours).
    "dvbi_guide": True,
    "dvbi_guide_last": 0,
    # Tune satellite instances straight from the service list (frequency,
    # polarisation, symbol rate) when they are not in the channel list.
    # Used only when the channel list has satellite channels (a dish).
    "dvbi_sat_direct": True,
    # Switch to the next instance of a DVB-I channel (internet, antenna,
    # cable, satellite) when the one in use fails.
    "dvbi_failover": True,
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
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            saved = {}
        if "rec_all_audio" in saved and "rec_audio_mode" not in saved:
            saved["rec_audio_mode"] = "all" if saved["rec_all_audio"] else "first"
        saved.pop("rec_all_audio", None)
        self.update(saved)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, indent=2))
        tmp.replace(self.path)
