# SPDX-License-Identifier: GPL-2.0-or-later
"""Settings and file locations (XDG base directories)."""

import json
import os
from pathlib import Path

from .dvbi_countries import system_country

APP_ID = "io.github.thommy87.LinDVB"
APP_NAME = "LinDVB"


def _xdg(var, fallback):
    return Path(os.environ.get(var) or (Path.home() / fallback))


def _snap_real_home():
    """The user's own home in the snap, where HOME is ~/snap/lindvb/<revision>."""
    real = os.environ.get("SNAP_REAL_HOME")
    return Path(real) if os.environ.get("SNAP") and real else None


def _in_snap_data(path):
    data = os.environ.get("SNAP_USER_DATA")
    return bool(data) and (path == Path(data) or Path(data) in path.parents)


def user_dir(key, fallback):
    """An XDG user directory (key XDG_VIDEOS_DIR, XDG_PICTURES_DIR, ...) from
    user-dirs.dirs, so a German desktop gets its ~/Videos and a French one its
    ~/Vidéos; *fallback* below the home when there is none. In the snap the
    real home is used: its HOME is a folder of the snap revision, which nobody
    looks into and which snapd removes with old revisions."""
    home = _snap_real_home() or Path.home()
    files = [_xdg("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs"]
    if home != Path.home():
        files.insert(0, home / ".config" / "user-dirs.dirs")
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            name, sep, value = line.strip().partition("=")
            if name != key or not sep:
                continue
            value = value.strip().strip('"')
            if value.startswith("$HOME"):
                path = home / value[len("$HOME"):].lstrip("/")
            elif value.startswith("/"):
                path = Path(value)
            else:
                continue
            # "$HOME/" switches the folder off; a folder inside the snap is the
            # revision folder again
            if path != home and not _in_snap_data(path):
                return path
    return home / fallback


def old_snap_folder(sub):
    """Where the snap put recordings (sub "Videos") or screenshots ("Pictures")
    before user_dir: the LinDVB folder in the snap revision's home. None
    outside the snap."""
    if _snap_real_home() is None:
        return None
    return Path.home() / sub / APP_NAME


def _old_snap_default(value, sub):
    """True for an old snap default such as ~/snap/lindvb/42/Videos/LinDVB,
    of any revision: settings.json keeps the revision it was written in."""
    real = _snap_real_home()
    if real is None or not value:
        return False
    path = Path(value)
    base = real / "snap" / (os.environ.get("SNAP_INSTANCE_NAME") or "lindvb")
    return path.parent.parent.parent == base and path.parts[-2:] == (sub, APP_NAME)


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
    # SAT>IP servers on the network (see lindvb/satip.py); each of their tuners
    # becomes a tuner profile
    "satip_servers": [],
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
    # configuration.countryId (ISO 3166 alpha-3) for satellite, cable,
    # antenna and IPTV channels, the desktop's country by default; DVB-I
    # channels use the country of their list (lindvb.hbbtv.country_for).
    # hbbtv_country_set: chosen in the preferences (before 0.23 the DVB-I
    # country set it, see Settings.__init__)
    "hbbtv_country": system_country(),
    "hbbtv_country_set": False,
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
    # Preferences ▸ Debug: write ~/.cache/lindvb/debug.log (lindvb.debuglog, for bug reports)
    "debug_mode": False,
    # timeshift buffer (lindvb.timeshift): "disk" (in timeshift_dir), "ram"
    # (tmpfs, at most half of the memory) or "off" (no pause / jumping back)
    "timeshift_mode": "disk",
    "timeshift_dir": str(CACHE_DIR / "timeshift"),
    "timeshift_max_mb": 4096,
    "timeshift_ram_mb": 1024,
    # internet TV channels through the timeshift buffer (lindvb.ipstream);
    # HLS/DASH quality there: "auto" (follows the connection) or "best"
    "iptv_timeshift": True,
    "iptv_timeshift_quality": "auto",
    # the WebVTT subtitles of HLS channels into that buffer too (stage H, on test)
    "subtitles_in_buffer": False,
    # open internet streams (and Tvheadend) again when the connection breaks
    "stream_reconnect": True,
    "recording_dir": str(user_dir("XDG_VIDEOS_DIR", "Videos") / APP_NAME),
    "name_scheme": "%station_%date_%time_%name",
    # Default recording options for new timers and manual recordings.
    # TV: "TS" "MPG" "MP4" "MKV"; radio: "Audio" (native MP2/MP3/AAC/AC3) or "TS".
    # Teletext and EPG data need TS; subtitles TS or MKV (see recformat.py).
    "rec_tv_format": "TS",
    "rec_radio_format": "TS",
    "rec_teletext": True,
    "rec_subtitles": True,
    "rec_epg_data": True,
    "rec_all_streams": False,      # every stream of the PMT, cleaned PAT (TS)
    # Audio tracks: "all", "language" (the preferred language only) or "first".
    # MP4 and the radio audio file keep one track either way.
    "rec_audio_mode": "all",
    # Preferred audio language (ISO 639-2, e.g. "deu"); "" = none.
    "rec_audio_language": "",
    # After "Record" in the EPG, open the timer window with the new timer.
    "timer_edit_after_create": False,
    "lead_minutes": 2,
    "follow_minutes": 5,
    "screenshot_dir": str(user_dir("XDG_PICTURES_DIR", "Pictures") / APP_NAME),
    "volume": 0.8,
    # Last tuned channel (TV or radio, tuned again at start) and the last
    # channel of each kind, tuned when switching between TV and radio.
    "last_channel": None,
    # Channel tuned when LinDVB starts (Preferences › Channels): "last" (the
    # last channel watched), "first" (the first channel of the channel list as
    # it was left: TV or radio, filter, group and order), "fixed" (the
    # channel in startup_channel_key) or "none" (no channel, a black screen).
    "startup_channel": "last",
    "startup_channel_key": None,
    "last_tv_channel": None,
    "last_radio_channel": None,
    "mode": "tv",                    # the kind of channel watched: "tv" or "radio"
    "list_tab": "",                  # "favourites": the favourites tab was shown last
    # The channel list's filter steps and sort order last chosen, for "tv"
    # and "radio" (lindvb.listfilter), shown again at the next start.
    "list_filters": {},
    "fav_last": 0,                   # favourites list shown last (and favourite zapping)
    "iptv_sources": [],             # imported IPTV lists (lindvb.iptvsources)
    "channel_order_manual": False,  # the channel editor's own order, not the DVB-I LCNs
    # Radio visualisation: a built-in style (glow, nebula, ribbons, pulsar, supernova, sunset, aurora,
    # goomhd, bars, vu, scope, off) or "plugin:<name>" for one registered by a plugin.
    "visualizer": "glow",
    # Draw the built-in visualisations on the graphics card (OpenGL); cairo
    # on the CPU is used when this is off or OpenGL is not available.
    "visualizer_gpu": True,
    "xmltv_sources": [],             # added internet guides (lindvb.xmltvsources)
    "epg_search_urls": [
        ["Wikipedia (en)", "https://en.wikipedia.org/wiki/Special:Search/{q}"],
        ["IMDb", "https://www.imdb.com/find?q={q}"],
        ["TMDB", "https://www.themoviedb.org/search?query={q}"],
    ],
    "osd_seconds": 4,
    # Size of the on-screen display in percent (Preferences › OSD and Visualisations).
    "osd_scale": 100,
    # Subtitles: font family ("" = the default) and size in percent of the
    # normal size (teletext subtitles and text subtitles of files and streams).
    "subtitle_font": "",
    "subtitle_scale": 100,
    # Shown over the radio visualisation (Preferences › OSD and Visualisations): the
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
    # The AppImage (and the Snap) bring gtk4paintablesink, which hands the
    # picture to the graphics card without a copy of each frame. While it is
    # tested it is used only when this is on (Preferences › Debug); the system
    # packages use it whenever the system has it (lindvb.player.gtk4_sink_wanted).
    "video_output_gl": False,
    # Keep the screen saver and screen blanking away while TV, radio or a
    # file plays (Preferences › Playback).
    "inhibit_screensaver": True,
    # Preferred audio format when a channel or file has several tracks in one
    # language: "off", "ac3" (Dolby Digital first), "eac3" (Dolby Digital Plus
    # first) or "stereo" (the MPEG / AAC sound).
    "audio_prefer": "off",
    # Preferred audio language of a channel or file (Preferences › Playback):
    # "auto" is the language of the interface, "" the track the broadcaster
    # starts with, else an ISO 639-2 code ("deu").
    "audio_language": "auto",
    # Start with the audio description track (for blind and visually
    # impaired viewers) when there is one; off, it plays only when chosen.
    "audio_description_prefer": False,
    # Audio passthrough (lindvb.passthrough): formats sent undecoded to an AV
    # receiver ("ac3", "eac3", "dts"; empty = off) and the output: "" is the
    # sound server (PulseAudio / PipeWire), else an ALSA device such as
    # "hdmi:CARD=PCH,DEV=0" or "iec958:CARD=PCH". Radio is always decoded.
    "audio_passthrough": [],
    "audio_passthrough_device": "",
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
    "theme": "system",              # dark, light or system (lindvb.ui.theme.SCHEMES)
    "accent_mode": "default",
    "accent_custom": "#119CFF",
    # Channel editor sections that are unfolded.
    "editor_sections_open": ["general"],
    # DVB-I (Preferences ▸ Internet, see lindvb.dvbi) --------------------------
    # Country whose DVB-I profile is used (lindvb.dvbi_countries: "DEU",
    # "ITA", "NZL", "ZZZ"; "" = none chosen yet). The dvbi_* keys below up to dvbi_prefer_broadcast belong
    # to that country; the settings of the other countries are kept in
    # dvbi_country_state and come back when switching to them again.
    "dvbi_country": "",
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
    # DVB-I lists the viewer added beside the country's list (list blocks,
    # lindvb.dvbi.own_lists): [{"id", "slot", "url", "name", "country",
    # "postcode", "region", "label", "auto_label", "last_check",
    # "last_modified", "etag"}], in the order they were added
    "dvbi_own_lists": [],
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
        # the HbbTV-only diagnostic log became the debug mode of all of LinDVB
        if saved.pop("hbbtv_debug_log", False):
            saved.setdefault("debug_mode", True)
        # the one XMLTV address became a list of guides
        from .xmltvsources import migrate
        migrate(saved)
        # the snap's old defaults were inside a revision folder: the new ones
        # (real home) take over; recordings already made stay in the list
        # (window._recording_folders)
        for key, sub in (("recording_dir", "Videos"), ("screenshot_dir", "Pictures")):
            if _old_snap_default(saved.get(key), sub):
                saved.pop(key)
        # the HbbTV country was set with the DVB-I country and could not be
        # chosen: until it is, the desktop's country applies
        if not saved.get("hbbtv_country_set"):
            saved.pop("hbbtv_country", None)
        # a new installation follows the desktop's light or dark look; one
        # from before the colour scheme could be chosen keeps the dark look
        if saved and "theme" not in saved:
            saved["theme"] = "dark"
        self.update(saved)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, indent=2))
        tmp.replace(self.path)
