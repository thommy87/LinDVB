# SPDX-License-Identifier: GPL-2.0-or-later
"""Preferences, channel editor, recording statistics and teletext windows."""

import dataclasses
import time
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, GObject, Gst, Gtk, Pango

from .. import chansort
from ..channels import Channel, number_order
from .. import hwaccel
from ..config import DATA_DIR
from ..epgsources import LANGUAGES
from ..recformat import (AUDIO_MODE_LABELS, AUDIO_MODES, EXTRA_LABELS, FORMAT_LABELS,
                         RADIO_FORMATS, TV_FORMATS, audio_note, effective_format)
from ..tuner import SegmentReader
from .common import Row, dropdown, fill, scrolled, selected
from .theme import ACCENT, ACCENT_MODES, SCHEMES, apply_accent, apply_theme, system_accent, to_hex
from ..i18n import LANGUAGES as LANGUAGES_UI, C_, _

DELSYS = ["dvb-s", "dvb-s2", "dvb-c-a", "dvb-c-b", "dvb-t", "dvb-t2", "atsc", "isdb-t", "dtmb", "iptv"]
MODS = ["auto", "qpsk", "8psk", "16apsk", "32apsk", "qam-16", "qam-32", "qam-64", "qam-128",
        "qam-256", "8vsb"]


DECODER_LABELS = {"auto": _("Automatic (graphics card when possible)"),
                  "vaapi": "VA-API (Intel, AMD)", "nvdec": "NVDEC (NVIDIA)",
                  "vulkan": "Vulkan Video", "v4l2": _("V4L2 (ARM boards)"),
                  "software": _("Software (processor)")}


def decoder_hint(player=None):
    """Which GPU decoders this machine has and which decoder plays now."""
    have = hwaccel.available()
    lines = [_("Graphics card decoders found: {list}").format(
        list=", ".join(hwaccel.NAMES[b] for b in have) if have else _("none"))]
    off = sorted(hwaccel.disabled())
    if off:
        lines.append(_("Switched off after an error: {list}").format(
            list=", ".join(hwaccel.NAMES.get(b, b) for b in off)))
    dec = getattr(player, "video_decoder", None)
    if dec:
        lines.append(_("Decoding now: {decoder}").format(decoder=hwaccel.describe(dec)))
    return "\n".join(lines)


ACCENT_LABELS = [_("LinDVB blue (default)"), _("System accent colour"), _("Custom colour")]


def _row(group, label, widget, hint=None, stacked=False):
    """One labelled setting inside a preferences group. *stacked* puts the title and
    the hint above the widget, over the full width, for widgets too wide to leave
    room for the text beside them (several entries in a row)."""
    row = Gtk.Box(spacing=8 if stacked else 16, css_classes=["lv-row"],
                  orientation=Gtk.Orientation.VERTICAL if stacked
                  else Gtk.Orientation.HORIZONTAL)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                   valign=Gtk.Align.CENTER)
    text.append(Gtk.Label(label=label, xalign=0, wrap=True))
    if hint:
        text.append(Gtk.Label(label=hint, xalign=0, wrap=True, css_classes=["lv-hint"]))
    row.append(text)
    if stacked:
        widget.set_halign(Gtk.Align.FILL if widget.get_hexpand() else Gtk.Align.START)
        row.append(widget)
        group.append(row)
        return widget
    widget.set_valign(Gtk.Align.CENTER)
    if isinstance(widget, (Gtk.Entry, Gtk.PasswordEntry)):
        widget.set_size_request(280, -1)
    elif isinstance(widget, (Gtk.SpinButton, Gtk.DropDown)):
        widget.set_size_request(200, -1)
    row.append(widget)
    group.append(row)
    return widget


def _children(box):
    out, w = [], box.get_first_child()
    while w is not None:
        out.append(w)
        w = w.get_next_sibling()
    return out


class PrefsWindow(Gtk.Window):
    """Preferences, split into pages with a sidebar."""

    def __init__(self, app, parent, page_name=None):
        super().__init__(title=_("Preferences"), transient_for=parent,
                         default_width=820, default_height=600)
        self.app = app
        self._saved = False
        s = app.settings
        self.w = {}

        hb = Gtk.HeaderBar()
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        save = Gtk.Button(label=_("Save"), css_classes=["suggested-action"])
        save.connect("clicked", lambda *_: self.on_save())
        hb.pack_end(save)
        self.set_titlebar(hb)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        nav = Gtk.StackSidebar(stack=self.stack, css_classes=["lv-nav"], width_request=200)
        body = Gtk.Box()
        body.append(nav)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self.stack)
        self.set_child(body)

        def page(name, title, intro=None):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=24,
                          margin_end=24, margin_top=20, margin_bottom=24)
            box.append(Gtk.Label(label=title, xalign=0, css_classes=["lv-page-title"]))
            if intro:
                box.append(Gtk.Label(label=intro, xalign=0, wrap=True, css_classes=["lv-hint"]))
            # every page scrolls on its own and only as far as its content goes;
            # no sideways scrolling: the window opens as wide as the widest page
            # needs in the chosen language, so no control is cut off
            sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.set_child(box)
            self.stack.add_titled(sw, name, title)
            return box

        def group(pg, heading=None):
            if heading:
                pg.append(Gtk.Label(label=heading, xalign=0, css_classes=["lv-section-title"],
                                    margin_top=4))
            g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
            pg.append(g)
            return g

        def entry(g, key, label, hint=None, stacked=False):
            self.w[key] = (_row(g, label, Gtk.Entry(text=str(s[key]), hexpand=stacked), hint,
                                stacked=stacked), str)

        def spin(g, key, label, lo, hi, hint=None):
            sp = _row(g, label, Gtk.SpinButton.new_with_range(lo, hi, 1), hint)
            sp.set_value(s[key])
            self.w[key] = (sp, int)

        def switch(g, key, label, hint=None):
            self.w[key] = (_row(g, label, Gtk.Switch(active=bool(s[key])), hint), bool)

        # -- Appearance
        pg = page("appearance", _("Appearance"))
        g = group(pg, _("Language"))
        lang = s.get("language", "")
        self._lang_codes = list(LANGUAGES_UI)
        self._lang_drop = _row(g, _("Language"), dropdown(
            [_(n) if c == "" else n for c, n in LANGUAGES_UI.items()],
            self._lang_codes.index(lang) if lang in self._lang_codes else 0),
            _("Takes effect when LinDVB is started again."))
        g = group(pg, _("Colour scheme"))
        theme = s.get("theme", "dark")
        self._theme_drop = _row(g, _("Colour scheme"), dropdown(
            [_("Dark"), _("Light"), _("Same as the system")],
            SCHEMES.index(theme) if theme in SCHEMES else 0),
            _("The picture always keeps its dark frame."))
        self._theme_drop.connect("notify::selected", lambda *_: apply_theme(
            SCHEMES[self._theme_drop.get_selected()] if
            SCHEMES[self._theme_drop.get_selected()] != "system" else {"theme": "system"}))
        g = group(pg, _("Accent colour"))
        mode = s.get("accent_mode", "default")
        self._accent_drop = _row(g, _("Accent colour"), dropdown(
            ACCENT_LABELS, ACCENT_MODES.index(mode) if mode in ACCENT_MODES else 0),
            _("Used for highlights, selections and buttons in every window."))
        self._accent_btn = self._color_button(s.get("accent_custom") or ACCENT)
        self._custom_row = _row(g, _("Custom colour"), self._accent_btn)
        self._accent_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["lv-hint"],
                                      margin_top=4, margin_bottom=6)
        g.append(self._accent_hint)
        self._accent_drop.connect("notify::selected", lambda *_: self._preview_accent())

        # -- OSD and visualisations
        pg = page("visualisation", _("OSD and Visualisations"))
        g = group(pg, _("On-screen display"))
        spin(g, "osd_seconds", _("OSD display time (s)"), 1, 30)
        spin(g, "osd_scale", _("OSD size (%)"), 50, 250,
             _("The programme info over the picture and the channel number."))
        g = group(pg, _("Overlay"))
        g.append(Gtk.Label(label=_("What is shown over the radio visualisation while a radio "
                                   "channel or an audio file plays. Song details come from the "
                                   "RadioText of DVB radio, the stream title of internet radio "
                                   "and the tags of audio files."),
                           xalign=0, wrap=True, css_classes=["lv-hint"], margin_top=10,
                           margin_bottom=4))
        switch(g, "vis_show_logo", _("Station logo tile"),
               _("The channel's logo, or a tile with the station name when there is none."))
        switch(g, "vis_show_cover", _("Album cover"),
               _("The cover of the song on air, from the file or looked up online."))
        switch(g, "vis_show_song", _("Song information"),
               _("Artist, album and year, track and title. Without song details the station "
               "and the programme on air are shown."))
        switch(g, "vis_show_clock", _("Clock in the top right corner"))
        g = group(pg, _("Sources"))
        switch(g, "vis_cover_online", _("Look up covers online"),
               _("Sends artist and title of the song on air to the iTunes Search API to find "
               "the album, year and cover."))
        entry(g, "logo_dir", _("Station logo folder"),
              _("Image files (PNG, SVG, JPG) named after the channel, e.g. \"Bayern 3.png\", or "
                "a picon set as Enigma2, Kodi and VDR use it (named by service reference or "
                "by SNP name). They are shown in the channel list too. LinDVB ships no "
                "logos, because they belong to the broadcasters. Empty: {v0}"
                ).format(v0=DATA_DIR / 'logos'), stacked=True)
        self._picon_btn = _row(g, _("Picons for my channels"),
                               Gtk.Button(label=_("Download Logos")),
                               _("Fetches only the logos of the channels in your list from "
                                 "the picons project (github.com/picons/picons) into the "
                                 "logo folder. Channels that already have a logo there are "
                                 "left alone."))
        self._picon_btn.connect("clicked", lambda *_: self._download_picons())
        self._picon_status = Gtk.Label(xalign=0, wrap=True, visible=False,
                                       css_classes=["lv-hint"], margin_start=12,
                                       margin_end=12, margin_bottom=8)
        g.append(self._picon_status)
        self._build_visualisations(group(pg, _("Visualisations")), s, spin, entry, switch)

        # -- Playback
        pg = page("playback", _("Playback"))
        g = group(pg)
        win = getattr(app, "window", None)
        player = getattr(win, "player", None)
        self._dec_codes = list(hwaccel.BACKENDS)
        mode = s.get("video_decoder", "auto")
        self._dec_drop = _row(g, _("Video decoding"), dropdown(
            [DECODER_LABELS[c] for c in self._dec_codes],
            self._dec_codes.index(mode) if mode in self._dec_codes else 0),
            decoder_hint(player))
        switch(g, "deinterlace", _("Deinterlace video"),
               _("Interlaced TV pictures (most SD and 1080i channels). Done on the graphics "
                 "card when VA-API decodes, else by the processor."))
        from ..media_info import AUDIO_PREFERENCES
        self._aprefs = list(AUDIO_PREFERENCES)
        cur = s.get("audio_prefer", "off")
        self._apref_drop = _row(g, _("Preferred audio format"), dropdown(
            [_("As broadcast"), _("Dolby Digital (AC-3)"), _("Dolby Digital Plus (E-AC-3)"),
             _("Stereo (MPEG / AAC)")],
            self._aprefs.index(cur) if cur in self._aprefs else 0),
            _("When a channel or file has this sound in the same language, it plays first."))
        switch(g, "inhibit_screensaver", _("No screen saver during playback"),
               _("Keeps the screen on while TV, radio or a file plays."))
        entry(g, "screenshot_dir", _("Screenshot folder"))
        g = group(pg, _("Subtitles"))
        self._sub_font = _row(g, _("Font"), self._font_button(s.get("subtitle_font", "")),
                              _("Teletext subtitles and the text subtitles of files and streams."))
        spin(g, "subtitle_scale", _("Size (%)"), 50, 300,
             _("Subtitles drawn as pictures (DVB subtitles) keep the size the channel sends."))
        g = group(pg, _("Timeshift"))
        entry(g, "timeshift_dir", _("Timeshift folder"))
        spin(g, "timeshift_max_mb", _("Timeshift buffer (MB)"), 128, 200000)
        switch(g, "iptv_timeshift", _("Timeshift for internet channels"),
               _("Internet TV channels go through the timeshift buffer like a tuner's: "
                 "pause, jump back and keep the buffer as a recording. Radio streams "
                 "play directly."))
        from ..ipstream import QUALITIES
        self._ts_qualities = list(QUALITIES)
        cur = s.get("iptv_timeshift_quality", "auto")
        self._ts_quality_drop = _row(g, _("Quality of HLS / DASH streams"), dropdown(
            [_("Automatic (follows the connection)"), _("Always the best")],
            self._ts_qualities.index(cur) if cur in self._ts_qualities else 0),
            _("Automatic switches to a lower quality when the connection gets slow; the "
              "buffer keeps what arrived. Always the best looks the same all the time, "
              "but stops to wait on a slow connection."))
        switch(g, "stream_reconnect", _("Reconnect internet streams"),
               _("When the connection of an internet or Tvheadend channel breaks, it is "
                 "opened again after a few seconds, for up to three minutes."))

        # -- Tuner
        pg = page("tuner", _("Tuners"),
                  _("LinDVB records and plays on several tuners at once and uses the tuners "
                    "with the highest priority first. Channels on one transponder share a "
                    "tuner.") + " " +
                  _("Only the tuners connected now are listed; the settings of others are "
                    "kept under \u201cNot connected\u201d."))
        self._build_tuners(pg, s)
        g = group(pg, _("Reception"))
        switch(g, "full_transponder", _("Capture whole transponder"),
               _("Receives everything the transponder sends instead of only the channel's own "
                 "streams. It costs more bandwidth on USB tuners, but plays and records all "
                 "audio tracks even when the channel list knows only one. Applies to live TV "
                 "and recordings."))

        # -- Channels
        pg = page("channels", _("Channels"))
        g = group(pg)
        scan = Gtk.Button(label=_("Channel Search…"))
        scan.connect("clicked", lambda *_: app.show_scan())
        _row(g, _("Channel search"), scan,
             _("Search the antenna, cable or satellite for channels (Shift+Ctrl+S)."))
        tune = Gtk.Button(label=_("Add Channels…"))
        tune.connect("clicked", lambda *_: app.show_tune())
        _row(g, _("Add channels"), tune,
             _("Tune a single frequency by hand and compare its channels with the "
               "channel list: add new ones, update changed ones, remove old ones."))
        edit = Gtk.Button(label=_("Channel Editor / Favourites"))
        edit.connect("clicked", lambda *_: app.show_editor())
        _row(g, _("Channel editor"), edit,
             _("Rename, sort, delete and add channels and mark favourites (Shift+Ctrl+E)."))
        favs = Gtk.Button(label=_("Favourites Lists…"))
        favs.connect("clicked", lambda *_: app.show_favourites())
        _row(g, _("Favourites lists"), favs,
             _("Create, rename, delete and reorder your favourites lists."))
        self._build_startup(group(pg, _("At start")), s)

        # -- HbbTV
        pg = page("hbbtv", _("HbbTV"))
        g = group(pg)
        from . import hbbtv as hbbtv_ui
        switch(g, "hbbtv_enabled", _("Interactive services (HbbTV)"),
               _("Media libraries, teletext successors and more behind the red button (F5).")
               if hbbtv_ui.available() else hbbtv_ui.unavailable_reason())
        switch(g, "hbbtv_autostart", _("Start automatically"),
               _("Off: an application starts only with the red button. Channels' "
                 "applications send usage data to the broadcaster."))
        switch(g, "hbbtv_subtitles", _("Subtitles in media libraries"),
               _("Applications show subtitles in their videos from the start."))
        switch(g, "hbbtv_audio_description", _("Audio description in media libraries"),
               _("Applications prefer the soundtrack that describes the picture."))
        switch(g, "hbbtv_teletext", _("Teletext from the interactive service"),
               _("The TXT button opens the channel's HbbTV teletext where there is one."))
        from .. import dvbi_countries
        # the age steps of the country chosen for DVB-I (Germany 6/12/16,
        # Italy 6/14, New Zealand 13/16); a limit set before stays a choice
        ages = list(dvbi_countries.get(s.get("dvbi_country")).age_limits)
        cur = int(s.get("hbbtv_age_limit", 0) or 0)
        if cur not in ages:
            ages = sorted(ages + [cur])
        self._age_drop = _row(g, _("Youth protection"), dropdown(
            [_("Off")] + [_("Viewers from {age} years").format(age=a) for a in ages[1:]],
            ages.index(cur) if cur in ages else 0),
            _("Applications and their videos rated above this age need the PIN."))
        self._ages = ages
        self._pin_old = None
        if s.get("hbbtv_pin"):
            self._pin_old = _row(g, _("Current PIN"), Gtk.PasswordEntry(show_peek_icon=True),
                                 _("Needed to change the youth protection or the PIN."))
        self._pin_entry = _row(g, _("New PIN"), Gtk.PasswordEntry(show_peek_icon=True),
                               _("Leave empty to keep the current PIN."))
        switch(g, "hbbtv_companion", _("Companion screens on the network"),
               _("Phones and tablets in your network can find LinDVB, talk to applications and "
                 "follow the picture in sync, and ask to start applications. Off: nothing listens "
                 "on the network."))
        switch(g, "hbbtv_third_party_cookies", _("Allow third-party cookies"),
               _("Advertising networks use them to follow you across channels."))
        clear = Gtk.Button(label=_("Delete HbbTV Data"))
        clear.connect("clicked", lambda *_: hbbtv_ui.clear_data(app))
        _row(g, _("Cookies and storage"), clear,
             _("Removes what HbbTV applications stored, such as cookies and settings."))

        # -- Internet: DVB-I and IPTV lists
        from .dvbi import DVBIPanel
        from .iptv import IPTVPanel
        pg = page("internet", _("Internet"),
                  _("Channel lists from the internet: the DVB-I list of your region and IPTV "
                  "playlists. These settings apply at once."))
        pg.append(Gtk.Label(label="DVB-I", xalign=0, css_classes=["lv-section-title"],
                            margin_top=4))
        self._dvbi = DVBIPanel(app)
        pg.append(self._dvbi)
        pg.append(Gtk.Label(label="IPTV", xalign=0, css_classes=["lv-section-title"],
                            margin_top=4))
        pg.append(IPTVPanel(app, _row))
        self.stack.connect("notify::visible-child-name", lambda *_: self._page_shown())

        # -- Recording
        pg = page("recording", C_("preferences page", "Recording"))
        g = group(pg)
        entry(g, "recording_dir", _("Recording folder"))
        entry(g, "name_scheme", _("File naming scheme"),
              _("Placeholders: %year %date %time %end %station %name, / for sub-folders"),
              stacked=True)
        self._recording_options(pg, group, switch)
        g = group(pg, _("Timer padding"))
        spin(g, "lead_minutes", _("Start earlier by (min)"), 0, 60)
        spin(g, "follow_minutes", _("Stop later by (min)"), 0, 180)
        g = group(pg, _("Timers"))
        switch(g, "timer_edit_after_create", _("Open the timer window after creating a timer"),
               _("After Record in the programme guide, edit the new timer straight away."))

        # -- EPG
        from .epgsettings import EPGPrefs
        pg = page("epg", _("EPG"), _("Programme guide: broadcast EPG, internet EPG (XMLTV) and the "
                                "EPG of other providers."))
        self._epg = EPGPrefs(app, self, pg, group, _row)

        # -- Conditional access
        pg = page("ca", _("Conditional access"),
                  _("Descrambling of the encrypted channels you subscribe to, with your own "
                  "card in a softcam or CI module."))
        g = group(pg)
        self._ca_names = ["none", "dvbapi", "ci"] + \
            [n for n in app.plugins.ca_providers if n not in ("dvbapi", "ci")]
        ca_labels = {"none": _("None (leave scrambled)"),
                     "dvbapi": _("External softcam (DVBAPI / OSCam)"),
                     "ci": _("Hardware CI / CI+ module")}
        cur = s.get("ca_provider", "none")
        idx = self._ca_names.index(cur) if cur in self._ca_names else 0
        self._ca_drop = _row(g, _("Descrambling via"),
                             dropdown([ca_labels.get(n, n) for n in self._ca_names], idx))
        g = group(pg, _("Softcam"))
        entry(g, "ca_dvbapi_socket", _("DVBAPI socket"), _("Unix socket path, or host:port."))
        g = group(pg, _("CI module"))
        spin(g, "ca_ci_adapter", _("CI adapter number"), 0, 15)
        spin(g, "ca_ci_device", _("CA device number"), 0, 7)

        # -- Plugins
        pg = page("plugins", _("Plugins"))
        g = group(pg)
        switch(g, "plugins_enabled", _("Load plugins at startup"))
        manage = Gtk.Button(label=_("Manage Plugins…"), halign=Gtk.Align.START)
        manage.connect("clicked", lambda *_: app.show_plugins())
        pg.append(manage)

        self._preview_accent()
        self.connect("close-request", self._on_close)
        if page_name and self.stack.get_child_by_name(page_name) is not None:
            self.stack.set_visible_child_name(page_name)

    # channel at start -------------------------------------------------------
    def _build_startup(self, g, s):
        """Which channel LinDVB tunes when it starts: the last one, the first of
        the list or one chosen from any channel list."""
        from ..channels import STARTUP_CHOICES
        mode = s.get("startup_channel", "last")
        self._start_drop = _row(g, _("Channel at start"), dropdown(
            [_("Last channel watched"), _("First channel of the current list"),
             _("A chosen channel"), _("No channel (black screen)")],
            STARTUP_CHOICES.index(mode) if mode in STARTUP_CHOICES else 0),
            _("The current list is the channel list as you left it: TV or radio, "
              "filter, group and sort order."))
        favs = self.app.favs
        self._start_lists = [None] + list(range(len(favs)))
        self._start_list_drop = _row(g, _("Channel list"), dropdown(
            [_("All channels")] + ["★ " + n for n in favs.names()]),
            _("All channels or one of your favourites lists."))
        self._start_chan_drop = _row(g, _("Channel"), dropdown([]),
                                     _("Tuned at every start, whichever channel you "
                                       "watched last."))
        self._start_chan_drop.set_enable_search(True)
        self._start_chan_drop.set_expression(
            Gtk.PropertyExpression.new(Gtk.StringObject.__gtype__, None, "string"))
        self._start_chans = []
        self._start_key = s.get("startup_channel_key")
        self._start_list_drop.connect("notify::selected", lambda *_: self._fill_startup())
        self._start_chan_drop.connect("notify::selected", lambda *_: self._startup_picked())
        self._start_drop.connect("notify::selected", lambda *_: self._startup_sensitive())
        self._fill_startup()
        self._startup_sensitive()

    def _fill_startup(self):
        i = self._start_lists[self._start_list_drop.get_selected()]
        favs = self.app.favs
        chans = favs.channels(i) if i is not None and favs.valid(i) else self.app.db.channels

        def label(c):
            text = f"{c.number}  {c.name}" if c.number else c.name
            return f"{text} ({c.root})" if c.root else text
        self._start_chans = list(chans)
        keys = [c.key for c in self._start_chans]
        key = self._start_key
        self._start_filling = True
        self._start_chan_drop.set_model(Gtk.StringList.new([label(c) for c in chans]))
        self._start_chan_drop.set_selected(keys.index(key) if key in keys
                                           else Gtk.INVALID_LIST_POSITION)
        self._start_filling = False

    def _startup_picked(self):
        if getattr(self, "_start_filling", False):
            return
        n = self._start_chan_drop.get_selected()
        if 0 <= n < len(self._start_chans):
            self._start_key = self._start_chans[n].key

    def _startup_sensitive(self):
        fixed = self._start_drop.get_selected() == 2
        for w in (self._start_list_drop, self._start_chan_drop):
            w.get_parent().set_sensitive(fixed)

    def _startup_values(self):
        from ..channels import STARTUP_CHOICES
        mode = STARTUP_CHOICES[self._start_drop.get_selected()]
        if mode == "fixed" and not self._start_key:
            mode = "last"          # nothing chosen: keep the usual behaviour
        return {"startup_channel": mode, "startup_channel_key": self._start_key}

    def _page_shown(self):
        # every page opens at its top, not where it was scrolled to before
        sw = self.stack.get_visible_child()
        if isinstance(sw, Gtk.ScrolledWindow):
            sw.get_vadjustment().set_value(0)
        if self.stack.get_visible_child_name() == "internet":
            self._dvbi.start()

    # recording defaults -----------------------------------------------------
    def _recording_options(self, pg, group, switch):
        """Default container and streams for new timers and manual recordings."""
        s = self.app.settings
        g = group(pg, _("Format and tracks"))
        g.append(Gtk.Label(
            label=_("Defaults for new timers and the Record button. Each timer, and a "
                  "recording started with \"Record with options…\", can change them.\n"
                  "TS: video, all audio tracks, subtitles, teletext and EPG data. "
                  "MKV: video, several audio tracks and subtitles. "
                  "MPG: video and several audio tracks. MP4: video and one audio track. "
                  "Internet streams have no teletext, subtitles or EPG data; HLS and DASH "
                  "streams are recorded with one audio track."),
            xalign=0, wrap=True, css_classes=["lv-hint"], margin_top=6, margin_bottom=6))

        def pick(key, values, labels, label, hint=None):
            cur = s.get(key)
            dd = _row(g, label, dropdown([labels[v] for v in values],
                                         values.index(cur) if cur in values else 0), hint)
            dd.connect("notify::selected", lambda *_: self._rec_hint_update())
            self._rec[key] = (dd, values)

        self._rec = {}
        pick("rec_tv_format", TV_FORMATS, FORMAT_LABELS, _("TV format"))
        pick("rec_radio_format", RADIO_FORMATS, FORMAT_LABELS, _("Radio format"))
        pick("rec_audio_mode", AUDIO_MODES, AUDIO_MODE_LABELS, _("Audio tracks"))
        codes = [c for c, _unused in LANGUAGES]
        names = {c: (_("None") if not c else name) for c, name in LANGUAGES}
        pick("rec_audio_language", codes, names, _("Preferred audio language"),
             _("Used by \"Preferred language only\" and by MP4, which keeps one track. "
             "Audio description tracks are skipped. Without a match the first track is "
             "recorded."))
        for e in ("subtitles", "teletext", "epg_data"):
            switch(g, "rec_" + e, EXTRA_LABELS[e])
            self.w["rec_" + e][0].connect("notify::active", lambda *_: self._rec_hint_update())
        self._rec_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["lv-hint"],
                                   margin_top=4, margin_bottom=6)
        g.append(self._rec_hint)
        self._rec_hint_update()

    def _rec_values(self):
        return {k: values[dd.get_selected()] for k, (dd, values) in self._rec.items()}

    def _rec_hint_update(self):
        v = self._rec_values()
        extras = [e for e in ("teletext", "subtitles", "epg_data")
                  if self.w["rec_" + e][0].get_active()]
        notes = []
        for fmt, radio, what in ((v["rec_tv_format"], False, "TV"),
                                 (v["rec_radio_format"], True, "Radio")):
            used = effective_format(fmt, extras, radio)
            if used != fmt:
                why = [EXTRA_LABELS[e].split(" (")[0] for e in extras if e != "subtitles"]
                if why:
                    text = _("{what}: {items} can only be kept in MPEG-TS, so recordings are "
                             "saved as TS.")
                else:
                    why = [EXTRA_LABELS["subtitles"].split(" (")[0]]
                    text = _("{what}: {items} can only be kept in TS or MKV, so recordings "
                             "are saved as TS.")
                notes.append(text.format(what=what, items=_(" and ").join(why)))
            else:
                note = audio_note(used, v["rec_audio_mode"])
                if note:
                    notes.append(f"{what}: {note}")
        if v["rec_audio_mode"] == "language" and not v["rec_audio_language"]:
            notes.append(_("No preferred audio language is set, so only the first audio track "
                         "is recorded."))
        self._rec_hint.set_text("\n".join(notes))
        self._rec_hint.set_visible(bool(notes))

    # accent preview ---------------------------------------------------------
    def _color_button(self, color):
        rgba = Gdk.RGBA()
        rgba.parse(color)
        if hasattr(Gtk, "ColorDialogButton"):
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False,
                                                               title=_("Accent colour")))
            btn.set_rgba(rgba)
        else:  # GTK < 4.10
            btn = Gtk.ColorButton(rgba=rgba, use_alpha=False)
        btn.connect("notify::rgba", lambda *_: self._preview_accent())
        return btn

    def _accent_settings(self):
        rgba = self._accent_btn.get_rgba()
        return {"accent_mode": ACCENT_MODES[self._accent_drop.get_selected()],
                "accent_custom": to_hex((rgba.red * 255, rgba.green * 255, rgba.blue * 255))}

    def _preview_accent(self):
        a = self._accent_settings()
        self._custom_row.get_parent().set_visible(a["accent_mode"] == "custom")
        color = apply_accent(a)
        if a["accent_mode"] == "system":
            found = system_accent()
            self._accent_hint.set_text(_("Desktop accent: {found}").format(found=found) if found else
                                       _("No desktop accent found, using LinDVB blue."))
        else:
            self._accent_hint.set_text(_("Current accent: {color}").format(color=color))

    def _on_close(self, *_):
        if not self._saved:
            apply_accent(self.app.settings)    # drop the unsaved preview
            apply_theme(self.app.settings)
        return False

    def _font_button(self, family):
        """Font family picker; a plain entry where GTK has no FontDialogButton."""
        if hasattr(Gtk, "FontDialogButton"):
            btn = Gtk.FontDialogButton(dialog=Gtk.FontDialog(title=_("Subtitle font")),
                                       level=Gtk.FontLevel.FAMILY)
            if family:
                btn.set_font_desc(Pango.FontDescription.from_string(family))
            box = Gtk.Box(spacing=6)
            box.append(btn)
            reset = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text=_("Default font"))
            reset.connect("clicked", lambda *_: btn.set_font_desc(Pango.FontDescription()))
            box.append(reset)
            box.font_btn = btn
            return box
        return Gtk.Entry(text=family, placeholder_text=_("Default font"))

    def _font_value(self):
        w = self._sub_font
        btn = getattr(w, "font_btn", None)
        if btn is None:
            return w.get_text().strip()
        desc = btn.get_font_desc()
        return (desc.get_family() or "") if desc is not None else ""

    def _build_visualisations(self, g, s, spin, entry, switch):
        """Radio visualisation: style, drawing and the GStreamer visualisations."""
        win = getattr(self.app, "window", None)
        vis = getattr(win, "visualizers", None) or {}
        self._vis_ids = [v for v in vis if "|" not in v]
        current = s.get("visualizer", "")
        base = current.split("|", 1)[0]
        self._vis_drop = None
        if self._vis_ids:
            self._vis_drop = _row(g, _("Visualisation"), dropdown(
                [vis[v][0] for v in self._vis_ids],
                self._vis_ids.index(base) if base in self._vis_ids else 0),
                _("Shown while radio or audio plays; also in the visualisation button."))
        switch(g, "visualizer_gpu", _("Draw on the graphics card"),
               _("OpenGL for the built-in visualisations. Takes effect when LinDVB is started again."))

    def _save_youth_protection(self, s):
        """Age limit and PIN; with a PIN set, only after the current PIN."""
        from .. import hbbtv as hb_core
        age = self._ages[self._age_drop.get_selected()]
        pin = self._pin_entry.get_text().strip()
        if age == int(s.get("hbbtv_age_limit", 0) or 0) and not pin:
            return
        if self._pin_old is not None:
            old = self._pin_old.get_text().strip()
            if hb_core.hash_pin(old, s.get("hbbtv_pin_salt", "")) != s.get("hbbtv_pin"):
                from .common import message
                message(self.get_transient_for() or self, _("Youth protection not changed"),
                        _("The current PIN is wrong."))
                return
        if age and not pin and not s.get("hbbtv_pin"):
            from .common import message
            message(self.get_transient_for() or self, _("Youth protection needs a PIN"),
                    _("Set a PIN to switch on the youth protection."))
            return
        s["hbbtv_age_limit"] = age
        if pin:
            s["hbbtv_pin_salt"] = hb_core.new_salt()
            s["hbbtv_pin"] = hb_core.hash_pin(pin, s["hbbtv_pin_salt"])

    def _build_tuners(self, pg, s):
        """Two sections: the hardware tuners found in this computer (numbered,
        one block each with name, on/off, priority, reception type, and for
        satellite its cable, LNB and single cable settings), then SAT>IP."""
        from .. import satip, tunerprofiles as tp
        present, missing = tp.hardware_tuners(s, tp.detect_tuners())
        net = satip.merge_profiles([dict(p) for p in s.get("tuners") or []
                                    if satip.is_satip(p)], s)
        self._tuner_rows = []
        glob = tp.tuner_lnb(None, s)
        local = present + missing
        sats = [p for p in local if not p.get("systems") or "sat" in tp.families(p)]
        cables = {(p.get("group") or "").strip() for p in sats} - {""}
        # with up to two satellite tuners a switch says enough; the cable name
        # only shows when there can be more than one shared cable
        self._cable_names = len(sats) > 2 or len(cables) > 1
        self._default_cable = cables.pop() if len(cables) == 1 else tp.DEFAULT_GROUP

        hw = self._tuner_section(pg, _("Hardware Tuners"))
        if not present:
            hw.append(Gtk.Label(label=_("No hardware tuner found."), xalign=0,
                                css_classes=["lv-section-title"]))
            hint = {"snap": _("The Snap package may only use the tuners once its DVB "
                              "interface is connected: run \u201csudo snap connect "
                              "lindvb:dvb\u201d and start LinDVB again."),
                    "driver": _("Linux has not set up a DVB device (/dev/dvb is missing). "
                                "The driver or the firmware of the tuner is probably "
                                "missing; \u201cdmesg | grep -i dvb\u201d shows what the "
                                "kernel reports.")}.get(tp.no_tuner_reason(),
                                                       _("Connect a tuner and open the "
                                                         "preferences again."))
            hw.append(Gtk.Label(label=hint, xalign=0, wrap=True, css_classes=["lv-hint"]))
        else:
            hw.append(Gtk.Label(label=_("{n} tuners found in this computer.").format(
                n=len(present)) if len(present) > 1 else
                _("1 tuner found in this computer."), xalign=0, css_classes=["lv-hint"]))
        for n, prof in enumerate(present, 1):
            g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
            hw.append(g)
            self._tuner_rows.append(self._tuner_block(g, prof, glob, number=n))
        if missing:
            away = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            hw.append(Gtk.Expander(label=_("Not connected ({n})").format(n=len(missing)),
                                   child=away))
            for prof in missing:
                g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
                away.append(g)
                self._tuner_rows.append(self._tuner_block(g, prof, glob))

        self._build_satip(self._tuner_section(pg, "SAT>IP"), s, net, glob)

    @staticmethod
    def _tuner_section(pg, title):
        """A darker box with a heading that holds one kind of tuners."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      css_classes=["lv-tuner-section"])
        box.append(Gtk.Label(label=title, xalign=0, css_classes=["lv-section-title"]))
        pg.append(box)
        return box

    def _build_satip(self, pg, s, tuners=(), glob=None):
        """The SAT>IP servers (found on the network or added by hand) and their tuners."""
        from .. import satip
        self._satip_servers = [dict(x) for x in s.get("satip_servers") or []]
        self._satip_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    css_classes=["lv-group"])
        pg.append(self._satip_group)
        self._satip_transport = {}
        self._satip_status = Gtk.Label(xalign=0, wrap=True, css_classes=["lv-hint"],
                                       visible=False)
        self._fill_satip()

        g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
        pg.append(g)
        search = Gtk.Button(label=_("Search Network"))
        search.connect("clicked", lambda *_a: self._search_satip(search))
        _row(g, _("Find SAT>IP servers"), search,
             _("SAT>IP boxes and LNBs, minisatip, FRITZ!Box Cable and other DVB over IP "
               "servers in the local network. Their tuners are listed below once the "
               "preferences are saved and opened again."))
        g.append(self._satip_status)
        host = _row(g, _("Add by address"), Gtk.Entry(placeholder_text="192.168.178.1"),
                    _("For a server the search does not find (another network, or no "
                      "multicast)."))
        kinds = list(satip.MANUAL_CAPS)
        kind = _row(g, _("Reception"), dropdown([_("Satellite"), _("Cable"),
                                                 _("Antenna (terrestrial)")]))
        count = _row(g, _("Tuners"), Gtk.SpinButton.new_with_range(1, satip.TUNERS_PER_SERVER, 1))
        count.set_value(2)
        add = Gtk.Button(label=_("Add Server"), halign=Gtk.Align.END)
        g.append(add)

        def do_add(*_a):
            text = host.get_text().strip()
            if not text:
                return
            name, _sep, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
            self._add_satip({"host": name or text, "port": int(port) if port.isdigit()
                             else satip.DEFAULT_PORT, "name": "", "uuid": "",
                             "caps": f"{kinds[kind.get_selected()]}-{int(count.get_value())}",
                             "transport": "udp"})
            host.set_text("")
        add.connect("clicked", do_add)
        host.connect("activate", do_add)
        for prof in tuners:
            g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
            pg.append(g)
            self._tuner_rows.append(self._tuner_block(g, prof, glob))

    def _fill_satip(self):
        from .. import satip, tunerprofiles as tp
        g = self._satip_group
        for w in _children(g):
            g.remove(w)
        self._satip_transport = {}
        if not self._satip_servers:
            g.append(Gtk.Label(label=_("No SAT>IP server added."), xalign=0,
                               css_classes=["lv-hint"]))
            return
        for srv in self._satip_servers:
            box = Gtk.Box(spacing=8)
            trans = dropdown([_("UDP"), _("TCP (in the RTSP connection)")],
                             1 if srv.get("transport") == "tcp" else 0)
            trans.set_tooltip_text(_("TCP works through firewalls that block the UDP stream."))
            rm = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text=_("Remove"))
            rm.connect("clicked", lambda *_a, x=srv: self._remove_satip(x))
            box.append(trans)
            box.append(rm)
            n = satip.tuner_count(srv.get("caps"))
            info = [tp.systems_label(satip.server_systems(srv.get("caps")))
                    or _("Reception types unknown"),
                    _("{n} tuners").format(n=n) if n > 1 else _("1 tuner")]
            _row(g, satip.server_label(srv), box, " · ".join(info))
            self._satip_transport[id(srv)] = trans

    def _add_satip(self, srv):
        from .. import satip
        for old in self._satip_servers:
            if (srv.get("uuid") and old.get("uuid") == srv["uuid"]) or \
                    (old.get("host") == srv["host"] and
                     int(old.get("port") or 554) == int(srv.get("port") or 554)):
                old.update({k: v for k, v in srv.items() if v and k not in ("id", "transport")})
                self._fill_satip()
                return False
        srv = dict(srv, id=satip.next_server_id(self._satip_servers))
        self._satip_servers.append(srv)
        self._fill_satip()
        return True

    def _remove_satip(self, srv):
        self._satip_servers = [x for x in self._satip_servers if x is not srv]
        self._fill_satip()

    def _search_satip(self, btn):
        import threading
        from .. import satip
        btn.set_sensitive(False)
        self._satip_status.set_text(_("Searching…"))
        self._satip_status.set_visible(True)

        def done(found):
            btn.set_sensitive(True)
            new = sum(1 for srv in found if self._add_satip(srv))
            if not found:
                text = _("No SAT>IP server answered. Add it by its address below.")
            else:
                text = _("{n} servers found, {new} new.").format(n=len(found), new=new)
            self._satip_status.set_text(text)
            return False

        def work():
            try:
                found = satip.discover()
            except Exception:                # noqa: BLE001 - no network
                found = []
            GLib.idle_add(done, found)
        threading.Thread(target=work, daemon=True, name="satip-discover").start()

    def _save_satip(self, s):
        servers = []
        for srv in self._satip_servers:
            trans = self._satip_transport.get(id(srv))
            if trans is not None:
                srv = dict(srv, transport="tcp" if trans.get_selected() == 1 else "udp")
            servers.append(srv)
        s["satip_servers"] = servers

    def _tuner_block(self, g, prof, glob, number=None):
        from .. import satip, tunerprofiles as tp
        info = [tp.systems_label(prof.get("systems") or []) or _("Reception types unknown")]
        if prof.get("label") and prof.get("name"):
            info.append(prof["name"])
        if satip.is_satip(prof):
            info.append(f"SAT>IP {prof.get('host', '')}:{prof.get('port', satip.DEFAULT_PORT)}")
        else:
            info.append(f"adapter{prof['adapter']}/frontend{prof['frontend']}")
        if not prof.get("present", True):
            info.append(_("not connected"))
        title = tp.display_name(prof)
        if number is not None:
            title = _("Tuner {n}: {name}").format(n=number, name=title)
        on = _row(g, title, Gtk.Switch(active=bool(prof.get("enabled", True))),
                  " · ".join(info))
        label = _row(g, _("Name"), Gtk.Entry(text=prof.get("label", ""),
                                             placeholder_text=prof.get("name", "")),
                     _("Your own name for this tuner. Empty: the name of the device."))
        prio = _row(g, _("Priority"), Gtk.SpinButton.new_with_range(0, 9, 1),
                    _("Higher numbers are used first."))
        prio.set_value(int(prof.get("priority", 0) or 0))
        row = {"profile": prof, "on": on, "label": label, "prio": prio, "group": None,
               "lnb": None, "uc": None, "use": None, "uses": []}
        fams = tp.families(prof)
        names = {"sat": _("Satellite"), "ter": _("Antenna (terrestrial)"), "cab": _("Cable")}
        sat_rows = []
        if satip.is_satip(prof):
            # the server sets up its own reception (LNB, DiSEqC, single cable)
            return row
        if len(fams) > 1:
            row["uses"] = ["", *fams]
            cur = prof.get("use") or ""
            row["use"] = _row(g, _("Connected to"), dropdown(
                [_("All its reception types")] + [names[f] for f in fams],
                row["uses"].index(cur) if cur in row["uses"] else 0),
                _("A combined tuner receives one type at a time, on the socket it is "
                  "connected to. LinDVB then only uses it for channels of that type."))
        sat = not prof.get("systems") or "sat" in fams
        if sat:
            first = len(_children(g))
            group = (prof.get("group") or "").strip()
            row["group"] = _row(g, _("Shared antenna cable"), Gtk.Switch(active=bool(group)),
                                _("On for tuners that hang on one cable with other tuners "
                                  "(loop-through or a splitter on one LNB output). Without "
                                  "Unicable they then only receive one band and polarisation "
                                  "at a time."))
            row["cable"] = None
            if self._cable_names:
                row["cable"] = _row(g, _("Cable name"), Gtk.Entry(
                    text=group, placeholder_text=self._default_cable),
                    _("Tuners with the same name share one cable."))
            own = list(prof.get("lnb") or glob)
            row["lnb"] = []
            for lbl, value in zip((_("LOF low band (kHz)"), _("LOF high band (kHz)"),
                                   _("Switch frequency (kHz)")), own):
                sp = _row(g, lbl, Gtk.SpinButton.new_with_range(0, 20000000, 1))
                sp.set_value(value)
                row["lnb"].append(sp)
            row["uc"] = self._single_cable_rows(g, prof)
            sat_rows = _children(g)[first:]
        if not sat_rows:
            return row
        cable_row = row["cable"].get_parent() if row["cable"] is not None else None
        uc_rows = row["uc"]["rows"]

        def show(*_a):
            on = row["use"] is None or row["uses"][row["use"].get_selected()] in ("", "sat")
            for r in sat_rows:
                if r not in uc_rows and r is not cable_row:
                    r.set_visible(on)
            if cable_row is not None:
                cable_row.set_visible(on and row["group"].get_active())
            row["uc"]["show"](on)
        if row["use"] is not None:
            row["use"].connect("notify::selected", show)
        row["group"].connect("notify::active", show)
        show()
        return row

    def _single_cable_rows(self, g, prof):
        """Unicable / JESS: standard, user band, its frequency and PIN. Only the
        choice of the standard shows while no single cable is set."""
        from .. import unicable
        uc = prof.get("unicable") or {}
        kinds = ["", *unicable.STANDARDS]
        std = _row(g, _("Unicable"), dropdown(
            [_("No Unicable"), _("Unicable I (EN 50494)"), _("Unicable II / JESS (EN 50607)")],
            kinds.index(uc.get("standard")) if uc.get("standard") in kinds else 0))
        slot = _row(g, _("User band"), Gtk.SpinButton.new_with_range(1, 32, 1),
                    _("Each tuner on the cable needs a different user band. Switch on "
                      "the shared antenna cable above for all of them."))
        slot.set_value(int(uc.get("slot", 0)) + 1)
        freq = _row(g, _("User band frequency (MHz)"),
                    Gtk.SpinButton.new_with_range(950, 2150, 1),
                    _("From the label or manual of the LNB or multiswitch."))
        freq.set_value(int(uc.get("frequency", 1210000)) // 1000)
        pin = _row(g, _("PIN"), Gtk.Entry(text="" if uc.get("pin") is None else str(uc["pin"])),
                   _("Only for a protected user band (0-255). Usually empty."))
        names = list(unicable.TEMPLATES)
        tmpl = _row(g, _("Fill in from"), dropdown([_("(choose…)"), *names], 0),
                    _("Common devices: sets the type and the frequency of the user band "
                      "above."))

        def fill(dd, _pspec):
            i = dd.get_selected()
            if i <= 0:
                return
            kind, freqs = unicable.TEMPLATES[names[i - 1]]
            std.set_selected(kinds.index(kind))
            n = int(slot.get_value()) - 1
            if n >= len(freqs):
                slot.set_value(1)
                n = 0
            freq.set_value(freqs[n])
        tmpl.connect("notify::selected", fill)

        details = [w.get_parent() for w in (slot, freq, pin, tmpl)]
        state = {"shown": True}            # False while the tuner is not used for satellite

        def update(*_a):
            on = state["shown"] and std.get_selected() > 0
            for r in details:
                r.set_visible(on)
        std.connect("notify::selected", update)
        update()

        def show(visible):
            state["shown"] = visible
            std.get_parent().set_visible(visible)
            update()
        return {"std": std, "slot": slot, "freq": freq, "pin": pin, "kinds": kinds,
                "rows": [std.get_parent(), *details], "show": show}

    def _save_tuners(self, s):
        from .. import tunerprofiles as tp
        glob = tp.tuner_lnb(None, s)
        tuners = []
        for row in self._tuner_rows:
            prof = {k: v for k, v in row["profile"].items() if k not in ("present", "lnb")}
            prof["enabled"] = row["on"].get_active()
            prof["label"] = row["label"].get_text().strip()
            if not prof["label"]:
                del prof["label"]
            prof.pop("use", None)
            if row["use"] is not None and row["use"].get_selected() > 0:
                prof["use"] = row["uses"][row["use"].get_selected()]
            prof["priority"] = int(row["prio"].get_value())
            if row["group"] is not None:
                prof["group"] = ""
                if row["group"].get_active():
                    name = row["cable"].get_text().strip() if row["cable"] is not None else ""
                    prof["group"] = name or self._default_cable
            if row["lnb"]:
                lnb = tuple(int(sp.get_value()) for sp in row["lnb"])
                if lnb != glob and all(lnb):
                    prof["lnb"] = list(lnb)
            prof.pop("unicable", None)
            uc = row.get("uc")
            if uc and uc["std"].get_selected() > 0:
                kind = uc["kinds"][uc["std"].get_selected()]
                from .. import unicable
                pin = uc["pin"].get_text().strip()
                prof["unicable"] = {
                    "standard": kind,
                    "slot": min(int(uc["slot"].get_value()), unicable.SLOTS[kind]) - 1,
                    "frequency": int(uc["freq"].get_value()) * 1000,
                    "pin": int(pin) & 0xFF if pin.isdigit() else None}
            tuners.append(prof)
        s["tuners"] = tuners
        on = [p for p in tuners if p["enabled"] and p.get("type") != "satip"]
        # the older settings, still read by the channel search (local tuners only)
        s["adapters"] = sorted({p["adapter"] for p in on}) or [0]
        s["frontend"] = on[0]["frontend"] if on else 0

    def _download_picons(self):
        """Fetch picons for the channel list on a background thread."""
        import threading
        from .. import picondl
        from .nowplaying import DEFAULT_LOGO_DIR
        btn = self._picon_btn
        folder = self.w["logo_dir"][0].get_text().strip() or str(DEFAULT_LOGO_DIR)
        channels = list(self.app.db.channels)
        btn.set_sensitive(False)
        btn.set_label(_("Looking up…"))

        def progress(done, total):
            GLib.idle_add(btn.set_label, _("{done} of {total}").format(done=done, total=total))

        def finished(text):
            btn.set_label(_("Download Logos"))
            btn.set_sensitive(True)
            self._picon_status.set_text(text)
            self._picon_status.set_visible(True)
            win = getattr(self.app, "window", None)
            if win is not None:
                win.reload_channels()
            return False

        def work():
            try:
                got, have, missing = picondl.download(channels, folder, progress=progress)
                text = _("{got} logos downloaded, {have} already there, {missing} not in "
                         "the picon set.").format(got=got, have=have, missing=missing)
            except (picondl.Error, OSError) as e:
                text = _("Download failed: {error}").format(error=e)
            GLib.idle_add(finished, text)

        threading.Thread(target=work, daemon=True).start()

    def on_save(self):
        s = self.app.settings
        s["ca_provider"] = self._ca_names[self._ca_drop.get_selected()]
        s["language"] = self._lang_codes[self._lang_drop.get_selected()]
        video_before = (s.get("video_decoder", "auto"), bool(s.get("deinterlace")))
        s["video_decoder"] = self._dec_codes[self._dec_drop.get_selected()]
        self._save_youth_protection(s)
        s.update(self._accent_settings())
        s["theme"] = SCHEMES[self._theme_drop.get_selected()]
        s.update(self._rec_values())
        s["subtitle_font"] = self._font_value()
        s["audio_prefer"] = self._aprefs[self._apref_drop.get_selected()]
        s["iptv_timeshift_quality"] = self._ts_qualities[self._ts_quality_drop.get_selected()]
        s.update(self._startup_values())
        vis_before = s.get("visualizer", "")
        if self._vis_drop is not None:
            chosen = self._vis_ids[self._vis_drop.get_selected()]
            if chosen != vis_before.split("|", 1)[0]:
                s["visualizer"] = chosen
        for k, (w, typ) in self.w.items():
            if typ is bool:
                s[k] = w.get_active()
            elif typ is int:
                s[k] = int(w.get_value())
            else:
                s[k] = w.get_text()
        self._save_satip(s)
        self._save_tuners(s)
        self._epg.save(s)
        s.save()
        self._saved = True
        from .. import channels as channels_mod
        channels_mod.IPTV_TIMESHIFT = bool(s.get("iptv_timeshift", True))
        apply_accent(s)
        apply_theme(s)
        self._epg.apply(s)
        win = getattr(self.app, "window", None)
        if win is not None:
            win.nowplaying.refresh()
            win._update_nowplaying()
            win.update_inhibit()
            win.apply_display_settings()
            if s.get("visualizer") != vis_before:
                win.set_visualizer(s["visualizer"])
            if (s["video_decoder"], bool(s["deinterlace"])) != video_before:
                win.player.apply_decoder_setting()
                if win.player.playbin is not None:
                    win.player.restart()
        self.close()


class EditorWindow(Gtk.Window):
    """Channel editor: rename, reorder, favourites, tuning data, delete.

    Left the full channel list with the filters of the main window, beside it
    the actions, then one favourites list (picked above it) with its own
    arrows and drag and drop, right the details of the channel.

    Edits are kept as drafts while moving through the list: the fields of each
    channel, the order of the channel list and the favourites lists (members
    and order). Apply writes them all at once; closing the window with drafts
    left asks what to do with them. New and Delete act at once."""

    SECTIONS = ("general", "tuning", "pids")
    # the filters of the main window without the favourites lists, plus TV / radio
    FILTERS = (("all", _("All channels")), ("tv", _("TV")), ("radio", _("Radio")),
               ("fta", _("Free to air")), ("enc", _("Encrypted")), ("hd", "HD channels"),
               ("iptv", _("Internet streams")))

    def __init__(self, app, parent, channel=None):
        super().__init__(title=_("Channel Editor"), transient_for=parent,
                         default_width=1320, default_height=680)
        self.app = app
        self.current = None
        self.drafts = {}             # id(channel) -> (channel, edited copy)
        db = app.db
        self.order = list(db.channels)          # the channel list order (draft)
        self.manual = bool(db.manual_order)     # own order instead of the DVB-I numbers
        self.fav_keys = [list(f["keys"]) for f in app.favs.lists]   # favourites (draft)
        self.touched = set()         # id(channel): moved or in other favourites lists
        self.fav_index = 0 if self.fav_keys else -1
        self._num = {}               # id(channel) -> channel number in the draft order
        self._filling = False        # set while a list is rebuilt
        self._close_ok = False
        self._last_pane = "all"      # the list Alt+Up/Down acts on

        # -- 1: the full channel list with the filters of the main window
        self.filter = Gtk.SearchEntry(placeholder_text=_("Search name or number"))
        self.filter.connect("search-changed", lambda *_: self.refresh())
        self.type_dd = dropdown([lbl for _k, lbl in self.FILTERS])
        self.type_dd.set_tooltip_text(_("Filter"))
        self.group_dd = dropdown([_("All groups")])
        self.group_dd.set_tooltip_text(_("Source or group"))
        self._groups = [""]
        self._sorts = [k for k, _l in chansort.SORTS]
        self.sort_dd = dropdown([lbl for _k, lbl in chansort.SORTS])
        self.sort_dd.set_tooltip_text(_("Sort by"))
        for d in (self.type_dd, self.group_dd, self.sort_dd):
            d.set_hexpand(True)
            d.connect("notify::selected", lambda *_: self.refresh())

        self.store = Gio.ListStore(item_type=Row)
        self.sel = Gtk.SingleSelection(model=self.store, autoselect=False)
        self.view = Gtk.ColumnView(model=self.sel, show_column_separators=True)
        sh = self._shown

        def name(c):
            return sh(c).name + ("  ✎" if self._pending(c) else "")
        for title, fn, expand in ((("#", lambda c: str(self._num.get(id(c), "")), False),
                                   (_("Name"), name, True),
                                   (_("Group"), lambda c: sh(c).root, False),
                                   (_("Category"), lambda c: sh(c).category, False),
                                   (_("Type"), lambda c: sh(c).delsys, False))):
            self.view.append_column(self._text_column(title, fn, expand))
        self.view.insert_column(4, self._fav_column())
        self.sel.connect("notify::selected-item", lambda *_: self._on_select())
        self.count = Gtk.Label(xalign=0, css_classes=["lv-hint"])
        self.sort_hint = Gtk.Label(xalign=1, hexpand=True, css_classes=["lv-hint"])

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=10,
                       margin_end=6, margin_top=10, margin_bottom=10, hexpand=True)
        left.append(self.filter)
        frow = Gtk.Box(spacing=6)
        for d in (self.type_dd, self.group_dd, self.sort_dd):
            frow.append(d)
        left.append(frow)
        left.append(Gtk.Frame(child=scrolled(self.view)))
        crow = Gtk.Box(spacing=6)
        crow.append(self.count)
        crow.append(self.sort_hint)
        left.append(crow)

        # -- 2: the actions, between the full list and the favourites
        strip, self.btn = self._strip((
            ("apply", "object-select-symbolic",
             _("Apply the changes of all edited channels (Ctrl+S)"), self.on_apply, "lv-primary"),
            None,
            ("new", "list-add-symbolic", _("New channel"), self.on_new, None),
            ("delete", "user-trash-symbolic", _("Delete channel"), self.on_delete, "lv-danger"),
            None,
            ("up", "go-up-symbolic", _("Move up in the channel list (Alt+Up)"),
             lambda: self.on_move(-1), None),
            ("down", "go-down-symbolic", _("Move down in the channel list (Alt+Down)"),
             lambda: self.on_move(1), None),
            ("sort", None, None, None, None),
            None,
            ("fav_add", "go-next-symbolic", _("Add to the favourites list"), self.on_fav_add,
             None),
            ("fav_remove", "go-previous-symbolic", _("Remove from the favourites list"),
             self.on_fav_remove, None)))

        # -- 3: one favourites list, chosen above it
        self.fav_dd = dropdown([])
        self.fav_dd.set_tooltip_text(_("Favourites list"))
        self.fav_dd.update_property([Gtk.AccessibleProperty.LABEL], [_("Favourites list")])
        self.fav_dd.connect("notify::selected", lambda *_: self._on_fav_list())
        self.fav_store = Gio.ListStore(item_type=Row)
        self.fav_sel = Gtk.SingleSelection(model=self.fav_store, autoselect=False,
                                           can_unselect=True)
        self.fav_sel.connect("notify::selected-item", lambda *_: self._on_fav_select())
        fac = Gtk.SignalListItemFactory()
        fac.connect("setup", self._fav_row_setup)
        fac.connect("bind", self._fav_row_bind)
        self.fav_view = Gtk.ListView(model=self.fav_sel, factory=fac,
                                     css_classes=["lv-fav-list"])
        self.fav_count = Gtk.Label(xalign=0, css_classes=["lv-hint"], wrap=True)
        # dropped below the last row: at the end of the list
        end_drop = Gtk.DropTarget.new(GObject.TYPE_STRING,
                                      Gdk.DragAction.MOVE | Gdk.DragAction.COPY)
        end_drop.connect("drop", lambda _t, v, _x, _y: self._drop(v, None, True))
        fav_frame = Gtk.Frame(child=scrolled(self.fav_view, h=False), vexpand=True)
        fav_frame.add_controller(end_drop)
        favs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=6,
                       margin_end=2, margin_top=10, margin_bottom=10, width_request=230)
        favs.append(self.fav_dd)
        favs.append(fav_frame)
        favs.append(self.fav_count)

        # -- 4: arrows for the favourites list
        fstrip, fbtn = self._strip((
            ("fav_up", "go-up-symbolic", _("Move up in the favourites list (Alt+Up)"),
             lambda: self.on_fav_move(-1), None),
            ("fav_down", "go-down-symbolic", _("Move down in the favourites list (Alt+Down)"),
             lambda: self.on_fav_move(1), None)))
        fstrip.set_margin_top(48)
        self.btn.update(fbtn)

        # -- 5: details, grouped in collapsible sections
        self.f = {}
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=20,
                          margin_end=16, margin_top=16, margin_bottom=16)
        self.title = Gtk.Label(xalign=0, ellipsize=3, css_classes=["lv-page-title"])
        self.subtitle = Gtk.Label(xalign=0, css_classes=["lv-hint"], margin_bottom=10)
        details.append(self.title)
        details.append(self.subtitle)
        open_ = set(app.settings.get("editor_sections_open", ["general"]))

        def section(key, title):
            grid = Gtk.Grid(row_spacing=8, column_spacing=14, css_classes=["lv-section-body"])
            exp = Gtk.Expander(child=grid, expanded=key in open_, css_classes=["lv-section"])
            exp.set_label_widget(Gtk.Label(label=title, css_classes=["lv-section-title"]))
            exp.connect("notify::expanded", lambda *_: self._remember_sections())
            self._exp[key] = exp
            details.append(exp)
            return grid

        def field(grid, row, label, widget):
            grid.attach(Gtk.Label(label=label, xalign=0, css_classes=["lv-field-label"]),
                        0, row, 1, 1)
            widget.set_hexpand(True)
            if isinstance(widget, Gtk.Switch):
                widget.set_halign(Gtk.Align.START)
            grid.attach(widget, 1, row, 1, 1)
            return widget

        self._exp = {}
        g = section("general", _("General"))
        for r, (k, lbl) in enumerate([("name", _("Name")), ("root", _("Group")),
                                      ("category", _("Category")), ("url", _("Stream URL"))]):
            self.f[k] = field(g, r, lbl, Gtk.Entry())
        self.radio = field(g, 4, _("Radio"), Gtk.Switch())
        self.radio.set_tooltip_text(_("For channels whose type was guessed wrong, e.g. "
                                      "internet streams or imported lists"))
        self.enc = field(g, 5, _("Encrypted"), Gtk.Switch())

        g = section("tuning", _("Tuning"))
        self.delsys = field(g, 0, _("System"), dropdown(DELSYS))
        self.pol = field(g, 1, _("Polarity"), dropdown(["", "h", "v", "l", "r"]))
        self.mod = field(g, 2, _("Modulation"), dropdown(MODS))
        for r, (k, lbl) in enumerate([("frequency", _("Frequency (kHz)")),
                                      ("symbol_rate", _("Symbol rate (kBd)")),
                                      ("bandwidth_hz", _("Bandwidth (Hz)")),
                                      ("stream_id", _("Stream ID")),
                                      ("diseqc", _("DiSEqC port (-1 none)"))], start=3):
            self.f[k] = field(g, r, lbl, Gtk.Entry())

        g = section("pids", _("Service and PIDs"))
        for r, (k, lbl) in enumerate([("sid", _("Service ID")), ("tsid", _("Transport ID")),
                                      ("pmt_pid", "PMT PID"), ("video_pid", _("Video PID")),
                                      ("audio_pids", _("Audio PIDs")), ("pcr_pid", "PCR PID"),
                                      ("teletext_pid", _("Teletext PID"))]):
            self.f[k] = field(g, r, lbl, Gtk.Entry())

        lists = Gtk.Box()
        lists.append(left)
        lists.append(strip)
        lists.append(favs)
        lists.append(fstrip)
        right = Gtk.Box(css_classes=["lv-dialog-pane"])
        right.append(scrolled(details))
        paned = Gtk.Paned(position=860, wide_handle=True, shrink_start_child=False,
                          shrink_end_child=False)
        paned.set_start_child(lists)
        paned.set_end_child(right)
        self.set_child(paned)

        keys = Gtk.ShortcutController()
        for trigger, fn in (("<Control>s", self.on_apply),
                            ("<Alt>Up", lambda: self._alt_move(-1)),
                            ("<Alt>Down", lambda: self._alt_move(1))):
            keys.add_shortcut(Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                action=Gtk.CallbackAction.new(lambda *_a, fn=fn: (fn(), True)[1])))
        self.add_controller(keys)
        self.connect("close-request", self._on_close_request)

        self._fill_groups()
        self._fill_fav_lists()
        self.refresh()
        self._load(channel)
        self._select_row(channel)
        self._fill_favs()

    # -- building blocks
    def _strip(self, items):
        """A slim vertical strip of icon buttons; returns (strip, {name: button})."""
        strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        css_classes=["lv-toolstrip"])
        buttons = {}
        for item in items:
            if item is None:
                strip.append(Gtk.Separator())
                continue
            key, icon, tip, fn, cls = item
            if key == "sort":
                b = Gtk.MenuButton(icon_name="view-sort-ascending-symbolic",
                                   tooltip_text=_("Sort the channel list"))
                b.update_property([Gtk.AccessibleProperty.LABEL], [_("Sort the channel list")])
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                pop = Gtk.Popover(child=box)
                for label, act in ((_("By group and name"), self.on_sort),
                                   (_("By the numbers of the DVB-I list"), self.on_sort_lcn)):
                    mb = Gtk.Button(label=label, css_classes=["flat"])
                    mb.get_child().set_xalign(0)
                    mb.connect("clicked", lambda _b, act=act: (pop.popdown(), act()))
                    box.append(mb)
                b.set_popover(pop)
            else:
                b = Gtk.Button(icon_name=icon, tooltip_text=tip)
                b.update_property([Gtk.AccessibleProperty.LABEL], [tip])
                b.connect("clicked", lambda _b, fn=fn: fn())
            if cls:
                b.add_css_class(cls)
            buttons[key] = b
            strip.append(b)
        return strip, buttons

    def _text_column(self, title, fn, expand):
        fac = Gtk.SignalListItemFactory()

        def setup(_f, li):
            lbl = Gtk.Label(xalign=0, ellipsize=3)
            # drag a channel from the list into the favourites list
            src = Gtk.DragSource(actions=Gdk.DragAction.COPY)
            src.connect("prepare", lambda _s, _x, _y, li=li: self._drag_value(
                "all", li.get_item().data if li.get_item() else None))
            lbl.add_controller(src)
            li.set_child(lbl)

        def bind(_f, li):
            li.get_child().set_text(fn(li.get_item().data))

        fac.connect("setup", setup)
        fac.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=fac)
        col.set_expand(expand)
        col.set_resizable(True)
        return col

    @staticmethod
    def _drag_value(src, c):
        if c is None:
            return None
        return Gdk.ContentProvider.new_for_value(f"{src}\n{c.key}")

    # -- drafts: edits not applied yet
    def _shown(self, c):
        """The channel as the editor shows it: its draft if it has one."""
        return self.drafts.get(id(c), (c, c))[1]

    def _in_db(self, c):
        return any(x is c for x in self.app.db.channels)

    def _order_dirty(self):
        db = self.app.db
        return self.manual != bool(db.manual_order) or \
            [id(c) for c in self.order] != [id(c) for c in db.channels]

    def _favs_dirty(self):
        return self.fav_keys != [list(f["keys"]) for f in self.app.favs.lists]

    def _pending(self, c):
        return id(c) in self.drafts or id(c) in self.touched

    def _touch(self, *channels):
        if not self._order_dirty() and not self._favs_dirty():
            self.touched.clear()          # moved back: nothing to apply
        else:
            self.touched.update(id(c) for c in channels)
        self._update_title()

    def _set_draft(self, c, d):
        if dataclasses.asdict(d) == dataclasses.asdict(c):
            changed = self.drafts.pop(id(c), None) is not None
        else:
            changed = True
            self.drafts[id(c)] = (c, d)
        self._update_title()
        return changed

    def _stash(self):
        """Keep the form of the channel shown as its draft."""
        c = self.current
        if c is None or not self._in_db(c):
            return
        d = dataclasses.replace(self._shown(c))
        self._save_into(d)
        if self._set_draft(c, d):
            self._redraw(c)

    def _redraw(self, c):
        """Bind the row of ``c`` again (draft marker, favourite star)."""
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i).data is c:
                self._filling = True
                self.store.items_changed(i, 1, 1)
                self._filling = False
                return

    def _update_title(self):
        n = len(set(self.drafts) | self.touched)
        if not n and (self._order_dirty() or self._favs_dirty()):
            n = 1
        self.set_title(_("Channel Editor") if not n else
                       _("Channel Editor – {n} channels not applied yet").format(n=n))

    # -- favourites lists (drafts)
    def _favs_of(self, c):
        """The favourites lists of *c* as the editor shows them."""
        return frozenset(i for i, keys in enumerate(self.fav_keys) if c.key in keys)

    def _set_favs(self, c, lists):
        for i, keys in enumerate(self.fav_keys):
            if i in lists and c.key not in keys:
                keys.append(c.key)
            elif i not in lists and c.key in keys:
                keys.remove(c.key)
        self._touch(c)
        GLib.idle_add(lambda: (self._redraw(c), self._fill_favs(), False)[2])

    def _sync_fav_lists(self):
        """Lists made or deleted meanwhile (the favourites window)."""
        favs = self.app.favs
        while len(self.fav_keys) < len(favs):
            self.fav_keys.append(list(favs.lists[len(self.fav_keys)]["keys"]))
        del self.fav_keys[len(favs):]

    def _fav_column(self):
        """A star in every row: a menu to put the channel into favourites lists."""
        fac = Gtk.SignalListItemFactory()

        def setup(_f, li):
            btn = Gtk.MenuButton(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                 css_classes=["flat"])
            btn.update_property([Gtk.AccessibleProperty.LABEL], [_("Favourites lists")])
            btn.chan = None
            btn.set_create_popup_func(self._fav_popup)
            li.set_child(btn)

        def bind(_f, li):
            btn = li.get_child()
            c = li.get_item().data
            btn.chan = c
            lists = self._favs_of(c)
            names = self.app.favs.names()
            btn.set_icon_name("starred-symbolic" if lists else "non-starred-symbolic")
            if lists:
                btn.add_css_class("lv-fav")
            else:
                btn.remove_css_class("lv-fav")
            btn.set_tooltip_text(", ".join(names[i] for i in sorted(lists) if i < len(names))
                                 or _("In no favourites list"))

        def unbind(_f, li):
            li.get_child().chan = None

        fac.connect("setup", setup)
        fac.connect("bind", bind)
        fac.connect("unbind", unbind)
        return Gtk.ColumnViewColumn(title="★", factory=fac)

    def _fav_popup(self, btn):
        """The lists to tick for the channel of *btn*, made when it opens."""
        c = btn.chan
        if c is None:
            return
        favs = self.app.favs
        if not len(favs):
            favs.ensure_one()
            self.app.favourites_changed()
            self._sync_fav_lists()
            self._fill_fav_lists()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=4,
                      margin_end=4, margin_top=4, margin_bottom=4)
        lists = self._favs_of(c)
        for i, name in enumerate(favs.names()):
            chk = Gtk.CheckButton(label=name, active=i in lists)

            def toggled(b, i=i):
                cur = set(self._favs_of(c))
                if b.get_active():
                    cur.add(i)
                else:
                    cur.discard(i)
                self._set_favs(c, cur)
            chk.connect("toggled", toggled)
            box.append(chk)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        new = Gtk.Button(label=_("New list…"), css_classes=["flat"])
        pop = Gtk.Popover(child=box)

        def make_list(*_a):
            from .favourites import ask_name
            pop.popdown()

            def done(name):
                i = favs.create(name)
                self.app.favourites_changed()
                self._sync_fav_lists()
                self._fill_fav_lists()
                self._set_favs(c, set(self._favs_of(c)) | {i})
            ask_name(self, _("New Favourites List"), _("New list"), done)
        new.connect("clicked", make_list)
        box.append(new)
        btn.set_popover(pop)

    def _fill_fav_lists(self):
        names = self.app.favs.names()
        if not (0 <= self.fav_index < len(names)):
            self.fav_index = 0 if names else -1
        self._filling = True
        self.fav_dd.set_model(Gtk.StringList.new(["★ " + n for n in names]))
        if self.fav_index >= 0:
            self.fav_dd.set_selected(self.fav_index)
        self._filling = False

    def _on_fav_list(self):
        if self._filling:
            return
        self.fav_index = self.fav_dd.get_selected()
        self._fill_favs()

    def _fav_channels(self):
        """The channels of the favourites list shown, in its order (draft)."""
        if not (0 <= self.fav_index < len(self.fav_keys)):
            return []
        index = {}
        for c in self.order:
            index.setdefault(c.key, c)
        return [index[k] for k in self.fav_keys[self.fav_index] if k in index]

    def _fill_favs(self):
        """Rebuild the favourites list; the channel shown stays selected."""
        chans = self._fav_channels()
        self._filling = True
        fill(self.fav_store, chans)
        pos = next((i for i, c in enumerate(chans) if c is self.current),
                   Gtk.INVALID_LIST_POSITION)
        self.fav_sel.set_selected(pos)
        self._filling = False
        if pos != Gtk.INVALID_LIST_POSITION and hasattr(self.fav_view, "scroll_to"):
            self.fav_view.scroll_to(pos, Gtk.ListScrollFlags.NONE, None)
        self.fav_count.set_text(
            _("{n} channels · drag to reorder").format(n=len(chans)) if chans else
            _("Empty. Drag channels here or use the arrow button.")
            if self.fav_index >= 0 else _("No favourites list yet. Use the star of a channel "
                                          "to make one."))
        self._update_buttons()

    def _fav_row_setup(self, _f, li):
        row = Gtk.Box(spacing=8, margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
        row.pos = Gtk.Label(xalign=1, width_chars=3, css_classes=["lv-hint", "numeric"])
        row.name = Gtk.Label(xalign=0, ellipsize=3, hexpand=True)
        row.append(row.pos)
        row.append(row.name)
        row.append(Gtk.Image(icon_name="list-drag-handle-symbolic", css_classes=["lv-hint"]))
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", lambda _s, _x, _y: self._drag_value(
            "fav", li.get_item().data if li.get_item() else None))
        row.add_controller(src)
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE | Gdk.DragAction.COPY)

        def where(_t, _x, y):
            after = y > row.get_height() / 2
            row.remove_css_class("lv-drop-after" if not after else "lv-drop-before")
            row.add_css_class("lv-drop-after" if after else "lv-drop-before")
            return Gdk.DragAction.MOVE

        def leave(_t):
            row.remove_css_class("lv-drop-before")
            row.remove_css_class("lv-drop-after")

        def dropped(_t, value, _x, y):
            leave(_t)
            item = li.get_item()
            return self._drop(value, item.data if item else None, y > row.get_height() / 2)

        drop.connect("enter", where)
        drop.connect("motion", where)
        drop.connect("leave", leave)
        drop.connect("drop", dropped)
        row.add_controller(drop)
        li.set_child(row)

    def _fav_row_bind(self, _f, li):
        row = li.get_child()
        row.pos.set_text(str(li.get_position() + 1))
        row.name.set_text(self._shown(li.get_item().data).name)

    def _drop(self, value, target, after):
        """A channel dropped on *target* in the favourites list (None: at the end)."""
        if not isinstance(value, str) or "\n" not in value or self.fav_index < 0:
            return False
        _src, key = value.split("\n", 1)
        c = next((x for x in self.order if x.key == key), None)
        if c is None or c is target:
            return False
        keys = self.fav_keys[self.fav_index]
        if key in keys:
            keys.remove(key)
        if target is None or target.key not in keys:
            keys.append(key)
        else:
            keys.insert(keys.index(target.key) + (1 if after else 0), key)
        self._touch(c)
        self._show(c)
        return True

    def _show(self, c):
        """Select *c* in both lists and load it."""
        if c is not self.current:
            self._stash()
            self._load(c)
        self._redraw(c)
        self._select_row(c)
        self._fill_favs()

    # -- selection
    def _on_select(self):
        if self._filling:
            return
        self._last_pane = "all"
        self._stash()
        self._load(selected(self.sel))
        self._fill_favs()

    def _on_fav_select(self):
        if self._filling:
            return
        c = selected(self.fav_sel)
        if c is None:
            return
        self._last_pane = "fav"
        self._stash()
        self._load(c)
        self._select_row(c)
        self._update_buttons()

    def _alt_move(self, step):
        if self._last_pane == "fav":
            self.on_fav_move(step)
        else:
            self.on_move(step)

    def _update_buttons(self):
        c = self.current
        movable = self._sorts[self.sort_dd.get_selected()] == "number"
        vis = self._visible_ids()
        p = vis.index(id(c)) if c is not None and id(c) in vis else -1
        self.btn["up"].set_sensitive(movable and p > 0)
        self.btn["down"].set_sensitive(movable and 0 <= p < len(vis) - 1)
        self.sort_hint.set_text("" if movable else _("Sort by number to move channels"))
        self.btn["delete"].set_sensitive(c is not None)
        inlist = c is not None and self.fav_index >= 0 and c.key in self.fav_keys[self.fav_index]
        self.btn["fav_add"].set_sensitive(c is not None and self.fav_index >= 0 and not inlist)
        self.btn["fav_remove"].set_sensitive(inlist)
        favs = [x.key for x in self._fav_channels()]
        q = favs.index(c.key) if inlist and c.key in favs else -1
        self.btn["fav_up"].set_sensitive(q > 0)
        self.btn["fav_down"].set_sensitive(0 <= q < len(favs) - 1)

    def _on_close_request(self, *_a):
        if self._close_ok:
            return False
        self._stash()
        if not self.drafts and not self._order_dirty() and not self._favs_dirty():
            return False
        n = max(1, len(set(self.drafts) | self.touched))
        dlg = Gtk.AlertDialog(
            message=_("Apply the changes to {n} channels?").format(n=n),
            detail=_("The edits were not applied yet. Without applying they are lost."),
            buttons=[_("Cancel"), _("Discard"), _("Apply")], cancel_button=0, default_button=2)

        def done(d, res):
            try:
                choice = d.choose_finish(res)
            except GLib.Error:
                return
            if choice == 0:
                return
            if choice == 2:
                self.on_apply()
            self.drafts.clear()
            self.touched.clear()
            self._close_ok = True
            self.close()

        dlg.choose(self, None, done)
        return True

    def _remember_sections(self):
        self.app.settings["editor_sections_open"] = [
            k for k in self.SECTIONS if self._exp[k].get_expanded()]
        self.app.settings.save()

    # -- the full list
    def _fill_groups(self):
        """The group entries as in the main window: all, the reception paths
        when the list mixes several, then the groups by name."""
        cur = self._groups[self.group_dd.get_selected()] if self._groups else ""
        srcs = chansort.sources(self.order)
        roots = sorted({self._shown(c).root for c in self.order if self._shown(c).root})
        self._groups = [""] + [k for k, _l in srcs] + roots
        self._filling = True
        self.group_dd.set_model(Gtk.StringList.new(
            [_("All groups")] + [lbl for _k, lbl in srcs] + roots))
        self.group_dd.set_selected(self._groups.index(cur) if cur in self._groups else 0)
        self._filling = False

    def _visible(self):
        """The channels the filters let through, in the order chosen."""
        numbered = number_order(self.order, self.manual)
        self._num = {id(c): n for c, n in numbered}
        q = self.filter.get_text().strip().lower()
        f = self.FILTERS[self.type_dd.get_selected()][0]
        g = self.group_dd.get_selected()
        group = self._groups[g] if 0 < g < len(self._groups) else None
        srt = self._sorts[self.sort_dd.get_selected()]
        out = []
        for c, n in numbered:
            s = self._shown(c)
            if f == "tv" and s.radio or f == "radio" and not s.radio:
                continue
            if f == "fta" and s.encrypted or f == "enc" and not s.encrypted:
                continue
            if f == "hd" and not any(t in s.name for t in ("HD", "HEVC", "UHD")):
                continue
            if f == "iptv" and not s.is_iptv:
                continue
            if group is not None:
                if group.startswith(chansort.SOURCE_PREFIX):
                    if not chansort.in_source(s, group):
                        continue
                elif group != s.root:
                    continue
            if q and not (str(n).startswith(q) if q.isdigit() else q in s.name.lower()):
                continue
            out.append(c)
        if srt != "number":
            out.sort(key=lambda c: chansort.sort_key(self._shown(c), srt), reverse=srt == "za")
        return out

    def _visible_ids(self):
        return [id(self.store.get_item(i).data) for i in range(self.store.get_n_items())]

    def refresh(self):
        """Rebuild the list; the channel shown stays selected and its form kept."""
        if self._filling:
            return
        self._stash()
        items = self._visible()
        self._filling = True
        fill(self.store, items)
        self._filling = False
        self.count.set_text(_("{n} of {total}").format(n=len(items), total=len(self.order)))
        self._select_row(self.current)
        self._update_buttons()

    def _select_row(self, c):
        """Select the row of ``c`` without loading it again."""
        pos = next((i for i in range(self.store.get_n_items())
                    if c is not None and self.store.get_item(i).data is c),
                   Gtk.INVALID_LIST_POSITION)
        self._filling = True
        self.sel.set_selected(pos)
        self._filling = False
        if pos != Gtk.INVALID_LIST_POSITION and hasattr(self.view, "scroll_to"):
            self.view.scroll_to(pos, None, Gtk.ListScrollFlags.NONE, None)

    def _load(self, c):
        self.current = c
        if not c:
            self.title.set_text(_("No channel selected"))
            self.subtitle.set_text(_("Pick a channel in the list to edit it."))
            self._update_buttons()
            return
        c = self._shown(c)
        self.title.set_text(c.name or _("Unnamed channel"))
        kind = _("Radio") if c.radio else "TV"
        self.subtitle.set_text(f"#{self._num.get(id(self.current), c.number)} · {kind} · "
                               f"{c.delsys}")
        for k, w in self.f.items():
            v = getattr(c, k)
            w.set_text(" ".join(map(str, v)) if isinstance(v, list) else str(v))
        self.delsys.set_selected(DELSYS.index(c.delsys) if c.delsys in DELSYS else 0)
        pols = ["", "h", "v", "l", "r"]
        self.pol.set_selected(pols.index(c.polarity) if c.polarity in pols else 0)
        self.mod.set_selected(MODS.index(c.modulation) if c.modulation in MODS else 0)
        # what the form showed, so that only a real edit counts as a change
        self._loaded = {k: w.get_text() for k, w in self.f.items()}
        self._loaded_sel = (self.delsys.get_selected(), self.pol.get_selected(),
                            self.mod.get_selected())
        self.enc.set_active(c.encrypted)
        self.radio.set_active(c.radio)
        self._update_buttons()

    def _save_into(self, c):
        loaded = getattr(self, "_loaded", {})
        for k, w in self.f.items():
            if w.get_text() == loaded.get(k):
                continue                       # untouched: keep the value as it is
            t = w.get_text().strip()
            cur = getattr(c, k)
            if isinstance(cur, list):
                setattr(c, k, [int(x) for x in t.split() if x.lstrip("-").isdigit()])
            elif isinstance(cur, int):
                try:
                    setattr(c, k, int(t))
                except ValueError:
                    pass
            else:
                setattr(c, k, t)
        sel = (self.delsys.get_selected(), self.pol.get_selected(), self.mod.get_selected())
        before = getattr(self, "_loaded_sel", (None, None, None))
        if sel[0] != before[0]:
            c.delsys = DELSYS[sel[0]]
        if sel[1] != before[1]:
            c.polarity = ["", "h", "v", "l", "r"][sel[1]]
        if sel[2] != before[2]:
            c.modulation = MODS[sel[2]]
        c.encrypted, c.radio = self.enc.get_active(), self.radio.get_active()

    def _commit(self):
        """Save the channel list now (New, Delete) and show it everywhere."""
        self.app.db.renumber()
        self.app.db.save()
        self._fill_groups()
        self.refresh()
        self._fill_favs()
        self.app.window.reload_channels()

    # -- actions
    def on_apply(self):
        """Write the drafts: channels, their order and the favourites lists."""
        self._stash()
        app, db, favs = self.app, self.app.db, self.app.favs
        self._sync_fav_lists()
        # favourites first: their keys are those before the tuning edits below
        for i, keys in enumerate(self.fav_keys):
            favs.lists[i]["keys"] = list(keys)
        for c, d in self.drafts.values():
            if self._in_db(c):
                old = c.key
                for f in dataclasses.fields(Channel):
                    setattr(c, f.name, getattr(d, f.name))
                favs.rekey(old, c.key)         # new tuning data: keep its favourites
        ids = {id(c) for c in db.channels}
        mine = {id(c) for c in self.order}
        db.channels[:] = [c for c in self.order if id(c) in ids] + \
            [c for c in db.channels if id(c) not in mine]
        db.manual_order = self.manual
        app.settings["channel_order_manual"] = self.manual
        app.settings.save()
        self.drafts.clear()
        app.favourites_changed()
        db.renumber()
        self.order = list(db.channels)
        self.fav_keys = [list(f["keys"]) for f in favs.lists]
        self.touched.clear()
        self._update_title()
        self._commit()
        self._load(self.current)

    def on_new(self):
        self._stash()
        c = Channel(name=_("New channel"), delsys="iptv")
        self.app.db.channels.append(c)
        self.order.append(c)
        self.current = c
        self._load(c)
        self._commit()

    def on_delete(self):
        c = self.current
        if c is None or not self._in_db(c):
            return
        self.drafts.pop(id(c), None)
        self.touched.discard(id(c))
        self.app.db.channels.remove(c)
        self.order = [x for x in self.order if x is not c]
        self.current = None
        self._update_title()
        self._commit()
        self._load(selected(self.sel))

    def _own_order(self):
        """From now on the list order counts, not the numbers of a DVB-I list:
        the order is the one shown."""
        if not self.manual:
            self.order = [c for c, _n in number_order(self.order, False)]
            self.manual = True

    def on_move(self, step):
        """Move the channel shown one place up or down among the rows shown
        (so it also works while the list is filtered)."""
        c = self.current
        if c is None or self._sorts[self.sort_dd.get_selected()] != "number":
            return
        rows = [self.store.get_item(i).data for i in range(self.store.get_n_items())]
        if not any(r is c for r in rows):
            return
        p = next(i for i, r in enumerate(rows) if r is c)
        q = p + step
        if not 0 <= q < len(rows):
            return
        self._own_order()
        other = rows[q]
        i = next(n for n, x in enumerate(self.order) if x is c)
        j = next(n for n, x in enumerate(self.order) if x is other)
        self.order[i], self.order[j] = self.order[j], self.order[i]
        self._touch(c, other)
        self.refresh()                 # the moved channel stays selected

    def on_sort(self):
        self.order.sort(key=lambda c: (self._shown(c).root, self._shown(c).name.lower()))
        self.manual = True
        self._touch(*self.order)
        self.refresh()

    def on_sort_lcn(self):
        """Back to the numbers the DVB-I list gives its channels."""
        self.manual = False
        self.order = [c for c, _n in number_order(self.order, False)]
        self._touch(*self.order)
        self.refresh()

    def on_fav_add(self):
        c = self.current
        if c is None or self.fav_index < 0:
            return
        self._set_favs(c, set(self._favs_of(c)) | {self.fav_index})

    def on_fav_remove(self):
        c = self.current
        if c is None or self.fav_index < 0:
            return
        self._set_favs(c, set(self._favs_of(c)) - {self.fav_index})

    def on_fav_move(self, step):
        """Move the channel shown one place in the favourites list."""
        c = self.current
        chans = self._fav_channels()
        if c is None or not any(x is c for x in chans):
            return
        p = next(i for i, x in enumerate(chans) if x is c)
        q = p + step
        if not 0 <= q < len(chans):
            return
        keys = self.fav_keys[self.fav_index]
        a, b = keys.index(c.key), keys.index(chans[q].key)
        keys[a], keys[b] = keys[b], keys[a]
        self._last_pane = "fav"
        self._touch(c)
        self._fill_favs()


class StatsWindow(Gtk.Window):
    """Recording and device statistics."""

    def __init__(self, app, parent):
        super().__init__(title=_("Recording and Device Statistics"), transient_for=parent,
                         default_width=620, default_height=360)
        self.app = app
        self.label = Gtk.Label(xalign=0, yalign=0, selectable=True, margin_start=12, margin_top=12)
        self.label.add_css_class("monospace")
        self.set_child(scrolled(self.label))
        self._src = GLib.timeout_add_seconds(1, self.update)
        self.connect("close-request", lambda *_: (GLib.source_remove(self._src), False)[1])
        self.update()

    def update(self):
        lines = ["Tuners:"]
        for a, cap in sorted(self.app.tuners.captures.items()):
            st = cap.stats
            start, end = cap.store.bounds()
            names = ", ".join(c.name for c in getattr(cap, "channels", {0: cap.channel}).values())
            lines.append(f"  adapter{a}/frontend{getattr(cap, 'frontend', 0)}: {names}  "
                         f"users={','.join(sorted(cap.users))}")
            lines.append(f"    lock={st.get('lock', '?')} signal={st.get('signal', '?')} "
                         f"snr={st.get('snr', '?')} ber={st.get('ber', '?')} unc={st.get('unc', '?')}")
            lines.append(f"    rate={cap.byte_rate() * 8 / 1e6:.2f} Mbit/s  "
                         f"buffer={(end - start) / 1e6:.0f} MB")
        if len(lines) == 1:
            lines.append("  (idle)")
        lines.append("")
        lines.append(_("Active recordings:"))
        for key, (rec, _cap) in self.app.timers.active.items():
            size = rec.bytes if rec.capture else (rec.path.stat().st_size if rec.path.exists() else 0)
            lines.append(f"  {rec.path.name}: {size / 1e6:.1f} MB, "
                         f"{int(time.time() - rec.started) // 60} min")
        if not self.app.timers.active:
            lines.append("  (none)")
        nxt = self.app.timers.next_wakeup()
        if nxt:
            lines.append(_("\nNext timer: {v0}").format(v0=time.strftime('%a %d.%m. %H:%M', time.localtime(nxt))))
        self.label.set_text("\n".join(lines))
        return True


class TeletextWindow(Gtk.Window):
    """Teletext pages decoded with GStreamer's teletextdec (libzvbi)."""

    def __init__(self, app, parent, capture, channel):
        super().__init__(title=_("Teletext – {name}").format(name=channel.name), transient_for=parent,
                         default_width=560, default_height=620)
        self.capture = capture
        self.channel = channel
        self.page = 100
        self.history = []

        top = Gtk.Box(spacing=4, margin_start=6, margin_end=6, margin_top=6)
        self.page_e = Gtk.SpinButton.new_with_range(100, 899, 1)
        self.page_e.set_value(100)
        self.page_e.connect("activate", lambda *_: self.goto(int(self.page_e.get_value())))
        prev = Gtk.Button(icon_name="go-previous-symbolic")
        prev.connect("clicked", lambda *_: self.goto(self.page - 1))
        nxt = Gtk.Button(icon_name="go-next-symbolic")
        nxt.connect("clicked", lambda *_: self.goto(self.page + 1))
        back = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text=_("Back"))
        back.connect("clicked", lambda *_: self.history and self.goto(self.history.pop(), False))
        self.find_e = Gtk.SearchEntry(placeholder_text=_("Find on page"), hexpand=True)
        self.find_e.connect("activate", lambda *_: self.find())
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text=_("Copy page"))
        copy.connect("clicked", lambda *_: self.get_clipboard().set(self.text.get_buffer().props.text))
        save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Save page"))
        save.connect("clicked", lambda *_: self.save())
        for w in (prev, self.page_e, nxt, back, self.find_e, copy, save):
            top.append(w)
        self.text = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        self.text.get_buffer().set_text(_("Waiting for teletext…"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(top)
        box.append(scrolled(self.text))
        self.set_child(box)
        self.pipeline = None
        if Gst.ElementFactory.find("teletextdec") is None:
            self.text.get_buffer().set_text(_("teletextdec is missing (gst-plugins-bad with libzvbi)."))
        else:
            self._build()
        self.connect("close-request", lambda *_: (self._teardown(), False)[1])

    def _build(self):
        self._teardown()
        p = Gst.Pipeline.new("teletext")
        src = Gst.ElementFactory.make("appsrc")
        src.set_property("caps", Gst.Caps.from_string("video/mpegts,systemstream=(boolean)true,packetsize=(int)188"))
        src.set_property("format", Gst.Format.BYTES)
        src.set_property("block", True)
        demux = Gst.ElementFactory.make("tsdemux")
        demux.set_property("program-number", self.channel.sid)
        self.dec = Gst.ElementFactory.make("teletextdec")
        self.dec.set_property("page", self.page)
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("caps", Gst.Caps.from_string("text/x-raw,format=utf-8"))
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.connect("new-sample", self._on_sample)
        for e in (src, demux, self.dec, sink):
            p.add(e)
        src.link(demux)
        self.dec.link(sink)
        demux.connect("pad-added", self._pad_added)
        self.pipeline = p
        _, end = self.capture.store.bounds()
        start = max(0, end - int(self.capture.byte_rate() * 20))
        from ..player import Feeder
        self.feeder = Feeder(SegmentReader(self.capture.store, start), src)
        p.set_state(Gst.State.PLAYING)
        self.feeder.start()

    def _pad_added(self, _d, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps and caps.get_structure(0).get_name() == "application/x-teletext":
            pad.link(self.dec.get_static_pad("sink"))

    def _on_sample(self, sink):
        s = sink.emit("pull-sample")
        buf = s.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if ok:
            text = bytes(m.data).decode("utf-8", "replace")
            buf.unmap(m)
            GLib.idle_add(self.text.get_buffer().set_text, text)
        return Gst.FlowReturn.OK

    def goto(self, page, remember=True):
        page = min(899, max(100, page))
        if remember:
            self.history.append(self.page)
        self.page = page
        self.page_e.set_value(page)
        self.text.get_buffer().set_text(_("Searching page {page}…").format(page=page))
        if self.pipeline:
            self.dec.set_property("page", page)

    def find(self):
        buf = self.text.get_buffer()
        q = self.find_e.get_text()
        start = buf.get_insert()
        it = buf.get_iter_at_mark(start)
        res = it.forward_search(q, Gtk.TextSearchFlags.CASE_INSENSITIVE, None) or \
            buf.get_start_iter().forward_search(q, Gtk.TextSearchFlags.CASE_INSENSITIVE, None)
        if res:
            buf.select_range(res[1], res[0])
            self.text.scroll_to_iter(res[0], 0, False, 0, 0)

    def save(self):
        d = Path.home() / "Documents"
        d.mkdir(exist_ok=True)
        p = d / f"teletext_{self.channel.name}_{self.page}_{time.strftime('%Y%m%d_%H%M')}.txt"
        p.write_text(self.text.get_buffer().props.text)

    def _teardown(self):
        if self.pipeline:
            self.feeder.stop()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

class PluginsWindow(Gtk.Window):
    """Lists the loaded plugins and the conditional-access status."""

    def __init__(self, app, parent):
        super().__init__(title=_("Plugins"), transient_for=parent, default_width=560,
                         default_height=440)
        self.app = app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                      margin_end=12, margin_top=12, margin_bottom=12)

        pm = app.plugins
        box.append(Gtk.Label(label=_("Conditional access"), css_classes=["heading"], xalign=0))
        provider = app.tuners.ca_factory() if app.tuners.ca_factory else None
        if app.settings.get("ca_provider", "none") == "none":
            ca_text = (_("No descrambling configured. Encrypted channels you are entitled to "
                       "can be descrambled by pointing LinDVB at your own softcam or CI module "
                       "in Preferences."))
        elif provider is None:
            ca_text = _("Provider '{v0}' is not available.").format(v0=app.settings['ca_provider'])
        elif provider.name == "ci":
            ca_text = (_("Provider: CI module – {status}\nThe module descrambles with your card inside the tuner.").format(status=provider.status))
        else:
            ca_text = (_("Provider: {name}\nLinDVB forwards ECM sections to your own card client and applies the control words it returns; it holds no keys itself.").format(name=provider.name))
        lab = Gtk.Label(label=ca_text, xalign=0, wrap=True, css_classes=["dim-label"])
        box.append(lab)
        cam = Gtk.Button(label=_("Open CAM Menu…"), halign=Gtk.Align.START)
        cam.connect("clicked", lambda *_: app.show_cam_menu())
        box.append(cam)

        box.append(Gtk.Label(label=_("Plugins"), css_classes=["heading"], xalign=0))
        loaded = {getattr(p, "module_name", p.name): p for p in pm.plugins}
        found = pm.discover()
        if found:
            lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
            for name, path in found:
                row = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=10,
                              margin_end=10)
                text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
                p = loaded.get(name)
                title = f"{p.name} {p.version}".rstrip() if p else name
                text.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"]))
                if name in pm.failed:
                    detail = _("Failed to load: {v0}").format(v0=pm.failed[name])
                elif p and p.description:
                    detail = p.description.splitlines()[0]
                elif not pm.is_enabled(name):
                    detail = _("Switched off")
                else:
                    detail = str(path)
                text.append(Gtk.Label(label=detail, xalign=0, wrap=True, css_classes=["dim-label"]))
                sw = Gtk.Switch(active=pm.is_enabled(name), valign=Gtk.Align.CENTER)
                sw.connect("notify::active", self._toggled, name)
                row.append(text)
                row.append(sw)
                lst.append(row)
            box.append(lst)
            self.restart_note = Gtk.Label(label=_("Changes take effect when LinDVB is started again."),
                                          xalign=0, css_classes=["dim-label"], visible=False)
            box.append(self.restart_note)
        else:
            box.append(Gtk.Label(label=_("No plugins installed."), xalign=0,
                                  css_classes=["dim-label"]))
        if pm.menu_actions:
            box.append(Gtk.Label(label=_("Plugin actions"), css_classes=["heading"], xalign=0))
            for _plugin, label, cb in pm.menu_actions:
                b = Gtk.Button(label=label, halign=Gtk.Align.START)
                b.connect("clicked", lambda _b, cb=cb: cb(app))
                box.append(b)

        # only the folders the user copies plugins into; the built-in add-ons live
        # inside LinDVB (in the AppImage a temporary mount) and are listed above
        folder = pm.ensure_plugin_dir()
        box.append(Gtk.Label(label=_("Plugin folder: {path}").format(path=folder), xalign=0,
                             wrap=True, selectable=True, css_classes=["dim-label"]))
        for extra in pm.user_dirs()[1:]:
            box.append(Gtk.Label(label=_("Plugin folder: {path}").format(path=extra), xalign=0,
                                 wrap=True, selectable=True, css_classes=["dim-label"]))
        box.append(Gtk.Label(label=_("Copy plugins into this folder: a Python file (.py), or a "
                                     "folder containing __init__.py."),
                             xalign=0, wrap=True, css_classes=["dim-label"]))
        opener = Gtk.Button(label=_("Open Plugin Folder"), halign=Gtk.Align.START)
        opener.connect("clicked", lambda *_: self._open_folder(folder))
        box.append(opener)
        self.set_child(scrolled(box))

    def _open_folder(self, folder):
        try:
            Gio.AppInfo.launch_default_for_uri(Path(folder).as_uri(), None)
        except GLib.Error as e:
            win = getattr(self.app, "window", None)
            if win is not None:
                win.set_status(_("Cannot open {folder}: {message}").format(folder=folder,
                                                                           message=e.message))

    def _toggled(self, sw, _pspec, name):
        self.app.plugins.set_enabled(name, sw.get_active())
        self.app.settings.save()
        self.restart_note.set_visible(True)
