# SPDX-License-Identifier: GPL-2.0-or-later
"""Preferences, channel editor, recording statistics and teletext windows."""

import dataclasses
import time
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gst, Gtk, Pango

from ..channels import Channel
from .. import hwaccel
from ..config import DATA_DIR
from ..epgsources import LANGUAGES
from ..recformat import (AUDIO_MODE_LABELS, AUDIO_MODES, EXTRA_LABELS, FORMAT_LABELS,
                         RADIO_FORMATS, TV_FORMATS, audio_note, effective_format)
from ..tuner import SegmentReader
from .common import dropdown, fill, make_column_view, scrolled, selected
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


def _row(group, label, widget, hint=None):
    """One labelled setting inside a preferences group."""
    row = Gtk.Box(spacing=16, css_classes=["lv-row"])
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                   valign=Gtk.Align.CENTER)
    text.append(Gtk.Label(label=label, xalign=0, wrap=True))
    if hint:
        text.append(Gtk.Label(label=hint, xalign=0, wrap=True, css_classes=["lv-hint"]))
    row.append(text)
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
        body.append(scrolled(self.stack))
        self.set_child(body)

        def page(name, title, intro=None):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=24,
                          margin_end=24, margin_top=20, margin_bottom=24)
            box.append(Gtk.Label(label=title, xalign=0, css_classes=["lv-page-title"]))
            if intro:
                box.append(Gtk.Label(label=intro, xalign=0, wrap=True, css_classes=["lv-hint"]))
            self.stack.add_titled(box, name, title)
            return box

        def group(pg, heading=None):
            if heading:
                pg.append(Gtk.Label(label=heading, xalign=0, css_classes=["lv-section-title"],
                                    margin_top=4))
            g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
            pg.append(g)
            return g

        def entry(g, key, label, hint=None):
            self.w[key] = (_row(g, label, Gtk.Entry(text=str(s[key])), hint), str)

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
                ).format(v0=DATA_DIR / 'logos'))
        _row(g, _("Picon sets"), Gtk.LinkButton(uri="https://github.com/picons/picons",
                                                label=_("Find picons online")),
             _("Download a set for your satellite, cable or antenna there and unpack it "
               "into the logo folder."))
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
        edit = Gtk.Button(label=_("Channel Editor / Favourites"))
        edit.connect("clicked", lambda *_: app.show_editor())
        _row(g, _("Channel editor"), edit,
             _("Rename, sort, delete and add channels and mark favourites (Shift+Ctrl+E)."))

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
              _("Placeholders: %year %date %time %end %station %name, / for sub-folders"))
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

    def _page_shown(self):
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
        entry(g, "visualizer_resolution", _("Resolution of projectM"),
              _("\"auto\" (the screen) or width x height, e.g. 1280x720. Smaller sizes start "
                "faster and need less processing power."))
        spin(g, "projectm_preset_seconds", _("projectM: seconds per preset"), 5, 600)
        entry(g, "projectm_preset_dir", _("projectM preset folder"),
              _("Empty: the usual folders of projectM."))

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
        """One block per tuner: name, on/off, priority, reception type, and for
        satellite its cable, LNB and single cable settings."""
        from .. import tunerprofiles as tp
        legacy = None if s.get("tuners") else list(s.get("adapters") or [0])
        found = tp.merge_profiles(s.get("tuners") or [], tp.detect_tuners(), legacy)
        if not found:
            found = [dict(p, present=False) for p in tp.legacy_profiles(s)]
        self._tuner_rows = []
        glob = tp.tuner_lnb(None, s)
        missing = [p for p in found if not p.get("present", True)]
        if len(missing) == len(found):
            pg.append(Gtk.Label(label=_("No tuner is connected."), xalign=0,
                                css_classes=["lv-hint"]))
        away = None
        if missing:
            away = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            exp = Gtk.Expander(label=_("Not connected ({n})").format(n=len(missing)),
                               child=away)
        for prof in [p for p in found if p.get("present", True)] + missing:
            g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
            (away if away is not None and not prof.get("present", True) else pg).append(g)
            self._tuner_rows.append(self._tuner_block(g, prof, glob))
        if away is not None:
            pg.append(exp)

    def _tuner_block(self, g, prof, glob):
        from .. import tunerprofiles as tp
        info = [tp.systems_label(prof.get("systems") or []) or _("Reception types unknown")]
        if prof.get("label") and prof.get("name"):
            info.append(prof["name"])
        info.append(f"adapter{prof['adapter']}/frontend{prof['frontend']}")
        if not prof.get("present", True):
            info.append(_("not connected"))
        on = _row(g, tp.display_name(prof), Gtk.Switch(active=bool(prof.get("enabled", True))),
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
            row["group"] = _row(g, _("Shared antenna cable"),
                                Gtk.Entry(text=prof.get("group", "")),
                                _("Give tuners on one cable the same name (loop-through or a "
                                  "splitter on one LNB output). They then only receive one "
                                  "band and polarisation at a time. Empty: own cable."))
            own = list(prof.get("lnb") or glob)
            row["lnb"] = []
            for lbl, value in zip((_("LOF low band (kHz)"), _("LOF high band (kHz)"),
                                   _("Switch frequency (kHz)")), own):
                sp = _row(g, lbl, Gtk.SpinButton.new_with_range(0, 20000000, 1))
                sp.set_value(value)
                row["lnb"].append(sp)
            row["uc"] = self._single_cable_rows(g, prof)
            sat_rows = _children(g)[first:]
        if row["use"] is not None and sat_rows:
            uc_rows = row["uc"]["rows"]

            def show(dd, *_a):
                use = row["uses"][dd.get_selected()]
                for r in sat_rows:
                    if r not in uc_rows:
                        r.set_visible(use in ("", "sat"))
                row["uc"]["show"](use in ("", "sat"))
            row["use"].connect("notify::selected", show)
            show(row["use"])
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
                    _("Each tuner on the cable needs a different user band. Give all "
                      "tuners on the cable the same cable name above."))
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
                prof["group"] = row["group"].get_text().strip()
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
        on = [p for p in tuners if p["enabled"]]
        # the older settings, still read by the channel search
        s["adapters"] = sorted({p["adapter"] for p in on}) or [0]
        s["frontend"] = on[0]["frontend"] if on else 0

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
        self._save_tuners(s)
        self._epg.save(s)
        s.save()
        self._saved = True
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

    Edits are kept as drafts per channel while moving through the list (the
    favourite switches in the list included) and written all at once with
    Apply; closing the window with drafts left asks what to do with them."""

    SECTIONS = ("general", "tuning", "pids")

    def __init__(self, app, parent, channel=None):
        super().__init__(title=_("Channel Editor"), transient_for=parent,
                         default_width=1040, default_height=640)
        self.app = app
        self.current = None
        self.drafts = {}             # id(channel) -> (channel, edited copy)
        self._filling = False        # set while the list is rebuilt
        self._close_ok = False
        sh = self._shown

        def name(c):
            return sh(c).name + ("  ✎" if id(c) in self.drafts else "")
        cols = [("#", lambda c: str(c.number), False), (_("Name"), name, True),
                (_("Group"), lambda c: sh(c).root, False),
                (_("Category"), lambda c: sh(c).category, False),
                (_("Type"), lambda c: sh(c).delsys, False)]
        self.view, self.store, self.sel = make_column_view(cols)
        self.view.insert_column(4, self._fav_column())
        self.sel.connect("notify::selected-item", lambda *_: self._on_select())
        self.filter = Gtk.SearchEntry(placeholder_text=_("Filter channels"))
        self.filter.connect("search-changed", lambda *_: self.refresh())

        # -- details, grouped in collapsible sections
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

        # -- slim vertical strip of actions beside the details
        strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        css_classes=["lv-toolstrip"])
        for item in (("object-select-symbolic",
                      _("Apply the changes of all edited channels (Ctrl+S)"), self.on_apply,
                      "lv-primary"),
                     None,
                     ("list-add-symbolic", _("New channel"), self.on_new, None),
                     ("user-trash-symbolic", _("Delete channel"), self.on_delete, "lv-danger"),
                     None,
                     ("go-up-symbolic", _("Move up"), lambda: self.on_move(-1), None),
                     ("go-down-symbolic", _("Move down"), lambda: self.on_move(1), None),
                     ("view-sort-ascending-symbolic", _("Sort by group and name"), self.on_sort,
                      None)):
            if item is None:
                strip.append(Gtk.Separator())
                continue
            icon, tip, fn, cls = item
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.update_property([Gtk.AccessibleProperty.LABEL], [tip])
            if cls:
                b.add_css_class(cls)
            b.connect("clicked", lambda _b, fn=fn: fn())
            strip.append(b)

        right = Gtk.Box(css_classes=["lv-dialog-pane"])
        right.append(scrolled(details))
        right.append(strip)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=10,
                       margin_end=12, margin_top=10, margin_bottom=10)
        left.append(self.filter)
        frame = Gtk.Frame(child=scrolled(self.view))
        left.append(frame)
        paned = Gtk.Paned(position=540, wide_handle=True, shrink_start_child=False,
                          shrink_end_child=False)
        paned.set_start_child(left)
        paned.set_end_child(right)
        self.set_child(paned)

        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>s"),
                                       action=Gtk.CallbackAction.new(
                                           lambda *_: (self.on_apply(), True)[1])))
        self.add_controller(keys)
        self.connect("close-request", self._on_close_request)

        self.refresh()
        self._load(channel)
        self._select_row(channel)

    # -- drafts: edits not applied yet
    def _shown(self, c):
        """The channel as the editor shows it: its draft if it has one."""
        return self.drafts.get(id(c), (c, c))[1]

    def _in_db(self, c):
        return any(x is c for x in self.app.db.channels)

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
        """Bind the row of ``c`` again (draft marker, favourite switch)."""
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i).data is c:
                self._filling = True
                self.store.items_changed(i, 1, 1)
                self._filling = False
                return

    def _update_title(self):
        n = len(self.drafts)
        self.set_title(_("Channel Editor") if not n else
                       _("Channel Editor – {n} channels not applied yet").format(n=n))

    def _fav_column(self):
        """Favourite switch in every row: a draft like any other edit."""
        fac = Gtk.SignalListItemFactory()

        def setup(_f, li):
            sw = Gtk.Switch(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            sw.update_property([Gtk.AccessibleProperty.LABEL], [_("Favourite")])
            sw.chan = None
            sw.connect("notify::active", self._on_fav_switch)
            li.set_child(sw)

        def bind(_f, li):
            sw = li.get_child()
            sw.chan = None                     # no draft while the switch is set up
            sw.set_active(self._shown(li.get_item().data).favourite)
            sw.chan = li.get_item().data

        def unbind(_f, li):
            li.get_child().chan = None

        fac.connect("setup", setup)
        fac.connect("bind", bind)
        fac.connect("unbind", unbind)
        return Gtk.ColumnViewColumn(title=_("Favourite"), factory=fac)

    def _on_fav_switch(self, sw, _pspec):
        c = sw.chan
        if c is None or self._shown(c).favourite == sw.get_active():
            return
        if c is self.current:
            self._stash()                      # keep what was typed in the form
        d = dataclasses.replace(self._shown(c))
        d.favourite = sw.get_active()
        if self._set_draft(c, d):
            GLib.idle_add(lambda: (self._redraw(c), False)[1])

    def _on_select(self):
        if self._filling:
            return
        self._stash()
        self._load(selected(self.sel))

    def _on_close_request(self, *_a):
        if self._close_ok:
            return False
        self._stash()
        if not self.drafts:
            return False
        n = len(self.drafts)
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
            self._close_ok = True
            self.close()

        dlg.choose(self, None, done)
        return True

    def _remember_sections(self):
        self.app.settings["editor_sections_open"] = [
            k for k in self.SECTIONS if self._exp[k].get_expanded()]
        self.app.settings.save()

    def refresh(self):
        """Rebuild the list; the channel shown stays selected and its form kept."""
        self._stash()
        q = self.filter.get_text().lower()
        items = [c for c in self.app.db.channels if q in self._shown(c).name.lower()]
        self._filling = True
        fill(self.store, items)
        self._filling = False
        self._select_row(self.current)

    def _select_row(self, c):
        """Select the row of ``c`` without loading it again."""
        pos = next((i for i in range(self.store.get_n_items())
                    if c is not None and self.store.get_item(i).data is c),
                   Gtk.INVALID_LIST_POSITION)
        self._filling = True
        self.sel.set_selected(pos)
        self._filling = False
        if pos != Gtk.INVALID_LIST_POSITION:
            self.view.scroll_to(pos, None, Gtk.ListScrollFlags.NONE, None) \
                if hasattr(self.view, "scroll_to") else None

    def _load(self, c):
        self.current = c
        if not c:
            self.title.set_text(_("No channel selected"))
            self.subtitle.set_text(_("Pick a channel in the list to edit it."))
            return
        c = self._shown(c)
        self.title.set_text(c.name or _("Unnamed channel"))
        kind = _("Radio") if c.radio else "TV"
        self.subtitle.set_text(f"#{c.number} · {kind} · {c.delsys}")
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
        self.app.db.renumber()
        self.app.db.save()
        self.refresh()
        self.app.window.reload_channels()

    def on_apply(self):
        """Write the drafts of all edited channels and save the list once."""
        self._stash()
        for c, d in self.drafts.values():
            if self._in_db(c):
                for f in dataclasses.fields(Channel):
                    setattr(c, f.name, getattr(d, f.name))
        self.drafts.clear()
        self._update_title()
        self._commit()
        self._load(self.current)

    def on_new(self):
        self._stash()
        c = Channel(name=_("New channel"), delsys="iptv")
        self.app.db.channels.append(c)
        self.current = c
        self._load(c)
        self._commit()

    def on_delete(self):
        c = selected(self.sel)
        if c:
            self.drafts.pop(id(c), None)
            self._update_title()
            self.app.db.channels.remove(c)
            self.current = None
            self._commit()
            self._load(selected(self.sel))

    def on_move(self, step):
        c = selected(self.sel)
        if not c:
            return
        ch = self.app.db.channels
        i = ch.index(c)
        j = i + step
        if 0 <= j < len(ch):
            ch[i], ch[j] = ch[j], ch[i]
            self._commit()             # the moved channel stays selected

    def on_sort(self):
        self.app.db.channels.sort(key=lambda c: (c.root, c.name.lower()))
        self._commit()


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
