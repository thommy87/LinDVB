"""Preferences, channel editor, recording statistics and teletext windows."""

import time
from pathlib import Path

from gi.repository import Gdk, GLib, Gst, Gtk

from ..channels import Channel
from ..config import DATA_DIR
from ..tuner import SegmentReader
from .common import dropdown, fill, make_column_view, scrolled, selected
from .theme import ACCENT, ACCENT_MODES, apply_accent, system_accent, to_hex

DELSYS = ["dvb-s", "dvb-s2", "dvb-c-a", "dvb-c-b", "dvb-t", "dvb-t2", "atsc", "isdb-t", "dtmb", "iptv"]
MODS = ["auto", "qpsk", "8psk", "16apsk", "32apsk", "qam-16", "qam-32", "qam-64", "qam-128",
        "qam-256", "8vsb"]


ACCENT_LABELS = ["LinDVB blue (default)", "System accent colour", "Custom colour"]


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
    if isinstance(widget, Gtk.Entry):
        widget.set_size_request(280, -1)
    elif isinstance(widget, (Gtk.SpinButton, Gtk.DropDown)):
        widget.set_size_request(200, -1)
    row.append(widget)
    group.append(row)
    return widget


class PrefsWindow(Gtk.Window):
    """Preferences, split into pages with a sidebar."""

    def __init__(self, app, parent):
        super().__init__(title="Preferences", transient_for=parent,
                         default_width=820, default_height=600)
        self.app = app
        self._saved = False
        s = app.settings
        self.w = {}

        hb = Gtk.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])
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
        pg = page("appearance", "Appearance")
        g = group(pg, "Accent colour")
        mode = s.get("accent_mode", "default")
        self._accent_drop = _row(g, "Accent colour", dropdown(
            ACCENT_LABELS, ACCENT_MODES.index(mode) if mode in ACCENT_MODES else 0),
            "Used for highlights, selections and buttons in every window.")
        self._accent_btn = self._color_button(s.get("accent_custom") or ACCENT)
        self._custom_row = _row(g, "Custom colour", self._accent_btn)
        self._accent_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["lv-hint"],
                                      margin_top=4, margin_bottom=6)
        g.append(self._accent_hint)
        self._accent_drop.connect("notify::selected", lambda *_: self._preview_accent())
        g = group(pg, "On-screen display")
        spin(g, "osd_seconds", "OSD display time (s)", 1, 30)

        # -- Visualisation
        pg = page("visualisation", "Visualisation",
                  "What is shown over the radio visualisation while a radio channel or an "
                  "audio file plays. Song details come from the RadioText of DVB radio, the "
                  "stream title of internet radio and the tags of audio files.")
        g = group(pg, "Overlay")
        switch(g, "vis_show_logo", "Station logo tile",
               "The channel's logo, or a tile with the station name when there is none.")
        switch(g, "vis_show_cover", "Album cover",
               "The cover of the song on air, from the file or looked up online.")
        switch(g, "vis_show_song", "Song information",
               "Artist, album and year, track and title. Without song details the station "
               "and the programme on air are shown.")
        switch(g, "vis_show_clock", "Clock in the top right corner")
        g = group(pg, "Sources")
        switch(g, "vis_cover_online", "Look up covers online",
               "Sends artist and title of the song on air to the iTunes Search API to find "
               "the album, year and cover.")
        entry(g, "logo_dir", "Station logo folder",
              "Image files (PNG, SVG, JPG) named after the channel, e.g. \"Bayern 3.png\". "
              "Logos named after the channel, as DVBViewer keeps them, work as they are. Empty: "
              f"{DATA_DIR / 'logos'}")

        # -- Playback
        pg = page("playback", "Playback")
        g = group(pg)
        switch(g, "deinterlace", "Deinterlace video")
        entry(g, "screenshot_dir", "Screenshot folder")
        g = group(pg, "Timeshift")
        entry(g, "timeshift_dir", "Timeshift folder")
        spin(g, "timeshift_max_mb", "Timeshift buffer (MB)", 128, 200000)

        # -- Tuner
        pg = page("tuner", "Tuner and LNB")
        g = group(pg, "DVB hardware")
        entry(g, "adapters", "DVB adapters", "Adapter numbers LinDVB may use, comma separated.")
        self.w["adapters"][0].set_text(", ".join(map(str, s["adapters"])))
        spin(g, "frontend", "Frontend", 0, 7)
        switch(g, "full_transponder", "Capture whole transponder",
               "Needed for all audio tracks when the channel list knows only one audio PID.")
        g = group(pg, "Satellite LNB")
        spin(g, "lnb_lof1", "LOF low band (kHz)", 0, 20000000)
        spin(g, "lnb_lof2", "LOF high band (kHz)", 0, 20000000)
        spin(g, "lnb_slof", "Switch frequency (kHz)", 0, 20000000)

        # -- Recording
        pg = page("recording", "Recording")
        g = group(pg)
        entry(g, "recording_dir", "Recording folder")
        entry(g, "name_scheme", "File naming scheme",
              "Placeholders: %year %date %time %end %station %name, / for sub-folders")
        g = group(pg, "Timer padding")
        spin(g, "lead_minutes", "Start earlier by (min)", 0, 60)
        spin(g, "follow_minutes", "Stop later by (min)", 0, 180)
        g = group(pg, "Timers")
        switch(g, "timer_edit_after_create", "Open the timer window after creating a timer",
               "After Record in the programme guide, edit the new timer straight away.")

        # -- EPG
        from .epgsettings import EPGPrefs
        pg = page("epg", "EPG", "Programme guide: broadcast EPG, internet EPG (XMLTV) and the "
                                "EPG of other providers.")
        self._epg = EPGPrefs(app, self, pg, group, _row)

        # -- Conditional access
        pg = page("ca", "Conditional access",
                  "Descrambling of the encrypted channels you subscribe to, with your own "
                  "card in a softcam or CI module.")
        g = group(pg)
        self._ca_names = ["none", "dvbapi", "ci"] + \
            [n for n in app.plugins.ca_providers if n not in ("dvbapi", "ci")]
        ca_labels = {"none": "None (leave scrambled)",
                     "dvbapi": "External softcam (DVBAPI / OSCam)",
                     "ci": "Hardware CI / CI+ module"}
        cur = s.get("ca_provider", "none")
        idx = self._ca_names.index(cur) if cur in self._ca_names else 0
        self._ca_drop = _row(g, "Descrambling via",
                             dropdown([ca_labels.get(n, n) for n in self._ca_names], idx))
        g = group(pg, "Softcam")
        entry(g, "ca_dvbapi_socket", "DVBAPI socket", "Unix socket path, or host:port.")
        g = group(pg, "CI module")
        spin(g, "ca_ci_adapter", "CI adapter number", 0, 15)
        spin(g, "ca_ci_device", "CA device number", 0, 7)

        # -- Plugins
        pg = page("plugins", "Plugins")
        g = group(pg)
        switch(g, "plugins_enabled", "Load plugins at startup")
        manage = Gtk.Button(label="Manage Plugins…", halign=Gtk.Align.START)
        manage.connect("clicked", lambda *_: app.show_plugins())
        pg.append(manage)

        self._preview_accent()
        self.connect("close-request", self._on_close)

    # accent preview ---------------------------------------------------------
    def _color_button(self, color):
        rgba = Gdk.RGBA()
        rgba.parse(color)
        if hasattr(Gtk, "ColorDialogButton"):
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False,
                                                               title="Accent colour"))
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
            self._accent_hint.set_text(f"Desktop accent: {found}" if found else
                                       "No desktop accent found, using LinDVB blue.")
        else:
            self._accent_hint.set_text(f"Current accent: {color}")

    def _on_close(self, *_):
        if not self._saved:
            apply_accent(self.app.settings)    # drop the unsaved preview
        return False

    def on_save(self):
        s = self.app.settings
        s["ca_provider"] = self._ca_names[self._ca_drop.get_selected()]
        s.update(self._accent_settings())
        for k, (w, typ) in self.w.items():
            if typ is bool:
                s[k] = w.get_active()
            elif typ is int:
                s[k] = int(w.get_value())
            elif k == "adapters":
                s[k] = [int(x) for x in w.get_text().replace(" ", "").split(",") if x.isdigit()]
            else:
                s[k] = w.get_text()
        self._epg.save(s)
        s.save()
        self._saved = True
        apply_accent(s)
        self._epg.apply(s)
        win = getattr(self.app, "window", None)
        if win is not None:
            win.nowplaying.refresh()
            win._update_nowplaying()
        self.close()


class EditorWindow(Gtk.Window):
    """Channel editor: rename, reorder, favourites, tuning data, delete."""

    SECTIONS = ("general", "tuning", "pids")

    def __init__(self, app, parent, channel=None):
        super().__init__(title="Channel Editor", transient_for=parent,
                         default_width=1040, default_height=640)
        self.app = app
        self.current = None
        cols = [("#", lambda c: str(c.number), False), ("Name", lambda c: c.name, True),
                ("Group", lambda c: c.root, False), ("Category", lambda c: c.category, False),
                ("Fav", lambda c: "★" if c.favourite else "", False),
                ("Type", lambda c: c.delsys, False)]
        self.view, self.store, self.sel = make_column_view(cols)
        self.sel.connect("notify::selected-item", lambda *_: self._load(selected(self.sel)))
        self.filter = Gtk.SearchEntry(placeholder_text="Filter channels")
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
        g = section("general", "General")
        for r, (k, lbl) in enumerate([("name", "Name"), ("root", "Group"),
                                      ("category", "Category"), ("url", "Stream URL")]):
            self.f[k] = field(g, r, lbl, Gtk.Entry())
        self.fav = field(g, 4, "Favourite", Gtk.Switch())
        self.radio = field(g, 5, "Radio", Gtk.Switch())
        self.enc = field(g, 6, "Encrypted", Gtk.Switch())

        g = section("tuning", "Tuning")
        self.delsys = field(g, 0, "System", dropdown(DELSYS))
        self.pol = field(g, 1, "Polarity", dropdown(["", "h", "v", "l", "r"]))
        self.mod = field(g, 2, "Modulation", dropdown(MODS))
        for r, (k, lbl) in enumerate([("frequency", "Frequency (kHz)"),
                                      ("symbol_rate", "Symbol rate (kBd)"),
                                      ("bandwidth_hz", "Bandwidth (Hz)"),
                                      ("stream_id", "Stream ID"),
                                      ("diseqc", "DiSEqC port (-1 none)")], start=3):
            self.f[k] = field(g, r, lbl, Gtk.Entry())

        g = section("pids", "Service and PIDs")
        for r, (k, lbl) in enumerate([("sid", "Service ID"), ("tsid", "Transport ID"),
                                      ("pmt_pid", "PMT PID"), ("video_pid", "Video PID"),
                                      ("audio_pids", "Audio PIDs"), ("pcr_pid", "PCR PID"),
                                      ("teletext_pid", "Teletext PID")]):
            self.f[k] = field(g, r, lbl, Gtk.Entry())

        # -- slim vertical strip of actions beside the details
        strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        css_classes=["lv-toolstrip"])
        for item in (("object-select-symbolic", "Apply changes (Ctrl+S)", self.on_apply,
                      "lv-primary"),
                     None,
                     ("list-add-symbolic", "New channel", self.on_new, None),
                     ("user-trash-symbolic", "Delete channel", self.on_delete, "lv-danger"),
                     None,
                     ("go-up-symbolic", "Move up", lambda: self.on_move(-1), None),
                     ("go-down-symbolic", "Move down", lambda: self.on_move(1), None),
                     ("view-sort-ascending-symbolic", "Sort by group and name", self.on_sort,
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

        self.refresh()
        self._load(channel)

    def _remember_sections(self):
        self.app.settings["editor_sections_open"] = [
            k for k in self.SECTIONS if self._exp[k].get_expanded()]
        self.app.settings.save()

    def refresh(self):
        q = self.filter.get_text().lower()
        fill(self.store, [c for c in self.app.db.channels if q in c.name.lower()])

    def _load(self, c):
        self.current = c
        if not c:
            self.title.set_text("No channel selected")
            self.subtitle.set_text("Pick a channel in the list to edit it.")
            return
        self.title.set_text(c.name or "Unnamed channel")
        kind = "Radio" if c.radio else "TV"
        self.subtitle.set_text(f"#{c.number} · {kind} · {c.delsys}")
        for k, w in self.f.items():
            v = getattr(c, k)
            w.set_text(" ".join(map(str, v)) if isinstance(v, list) else str(v))
        self.delsys.set_selected(DELSYS.index(c.delsys) if c.delsys in DELSYS else 0)
        pols = ["", "h", "v", "l", "r"]
        self.pol.set_selected(pols.index(c.polarity) if c.polarity in pols else 0)
        self.mod.set_selected(MODS.index(c.modulation) if c.modulation in MODS else 0)
        self.fav.set_active(c.favourite)
        self.enc.set_active(c.encrypted)
        self.radio.set_active(c.radio)

    def _save_into(self, c):
        for k, w in self.f.items():
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
        c.delsys = DELSYS[self.delsys.get_selected()]
        c.polarity = ["", "h", "v", "l", "r"][self.pol.get_selected()]
        c.modulation = MODS[self.mod.get_selected()]
        c.favourite, c.encrypted, c.radio = self.fav.get_active(), self.enc.get_active(), self.radio.get_active()

    def _commit(self):
        self.app.db.renumber()
        self.app.db.save()
        self.refresh()
        self.app.window.reload_channels()

    def on_apply(self):
        if self.current:
            self._save_into(self.current)
            self._commit()
            self._load(self.current)

    def on_new(self):
        c = Channel(name="New channel", delsys="iptv")
        self.app.db.channels.append(c)
        self._load(c)
        self._commit()

    def on_delete(self):
        c = selected(self.sel)
        if c:
            self.app.db.channels.remove(c)
            self._commit()

    def on_move(self, step):
        c = selected(self.sel)
        if not c:
            return
        ch = self.app.db.channels
        i = ch.index(c)
        j = i + step
        if 0 <= j < len(ch):
            ch[i], ch[j] = ch[j], ch[i]
            self._commit()
            self.sel.set_selected(j)

    def on_sort(self):
        self.app.db.channels.sort(key=lambda c: (c.root, c.name.lower()))
        self._commit()


class StatsWindow(Gtk.Window):
    """Recording and device statistics."""

    def __init__(self, app, parent):
        super().__init__(title="Recording and Device Statistics", transient_for=parent,
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
            lines.append(f"  adapter{a}: {cap.channel.name}  users={','.join(sorted(cap.users))}")
            lines.append(f"    lock={st.get('lock', '?')} signal={st.get('signal', '?')} "
                         f"snr={st.get('snr', '?')} ber={st.get('ber', '?')} unc={st.get('unc', '?')}")
            lines.append(f"    rate={cap.byte_rate() * 8 / 1e6:.2f} Mbit/s  "
                         f"buffer={(end - start) / 1e6:.0f} MB")
        if len(lines) == 1:
            lines.append("  (idle)")
        lines.append("")
        lines.append("Active recordings:")
        for key, (rec, _cap) in self.app.timers.active.items():
            size = rec.bytes if rec.capture else (rec.path.stat().st_size if rec.path.exists() else 0)
            lines.append(f"  {rec.path.name}: {size / 1e6:.1f} MB, "
                         f"{int(time.time() - rec.started) // 60} min")
        if not self.app.timers.active:
            lines.append("  (none)")
        nxt = self.app.timers.next_wakeup()
        if nxt:
            lines.append(f"\nNext timer: {time.strftime('%a %d.%m. %H:%M', time.localtime(nxt))}")
        self.label.set_text("\n".join(lines))
        return True


class TeletextWindow(Gtk.Window):
    """Teletext pages decoded with GStreamer's teletextdec (libzvbi)."""

    def __init__(self, app, parent, capture, channel):
        super().__init__(title=f"Teletext – {channel.name}", transient_for=parent,
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
        back = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Back")
        back.connect("clicked", lambda *_: self.history and self.goto(self.history.pop(), False))
        self.find_e = Gtk.SearchEntry(placeholder_text="Find on page", hexpand=True)
        self.find_e.connect("activate", lambda *_: self.find())
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy page")
        copy.connect("clicked", lambda *_: self.get_clipboard().set(self.text.get_buffer().props.text))
        save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Save page")
        save.connect("clicked", lambda *_: self.save())
        for w in (prev, self.page_e, nxt, back, self.find_e, copy, save):
            top.append(w)
        self.text = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        self.text.get_buffer().set_text("Waiting for teletext…")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(top)
        box.append(scrolled(self.text))
        self.set_child(box)
        self.pipeline = None
        if Gst.ElementFactory.find("teletextdec") is None:
            self.text.get_buffer().set_text("teletextdec is missing (gst-plugins-bad with libzvbi).")
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
        self.text.get_buffer().set_text(f"Searching page {page}…")
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
        super().__init__(title="Plugins", transient_for=parent, default_width=560,
                         default_height=440)
        self.app = app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                      margin_end=12, margin_top=12, margin_bottom=12)

        pm = app.plugins
        box.append(Gtk.Label(label="Conditional access", css_classes=["heading"], xalign=0))
        provider = app.tuners.ca_factory() if app.tuners.ca_factory else None
        if app.settings.get("ca_provider", "none") == "none":
            ca_text = ("No descrambling configured. Encrypted channels you are entitled to "
                       "can be descrambled by pointing LinDVB at your own softcam or CI module "
                       "in Preferences.")
        elif provider is None:
            ca_text = f"Provider '{app.settings['ca_provider']}' is not available."
        elif provider.name == "ci":
            ca_text = (f"Provider: CI module – {provider.status}\n"
                       "The module descrambles with your card inside the tuner.")
        else:
            ca_text = (f"Provider: {provider.name}\n"
                       "LinDVB forwards ECM sections to your own card client and applies the "
                       "control words it returns; it holds no keys itself.")
        lab = Gtk.Label(label=ca_text, xalign=0, wrap=True, css_classes=["dim-label"])
        box.append(lab)
        cam = Gtk.Button(label="Open CAM Menu…", halign=Gtk.Align.START)
        cam.connect("clicked", lambda *_: app.show_cam_menu())
        box.append(cam)

        box.append(Gtk.Label(label="Plugins", css_classes=["heading"], xalign=0))
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
                    detail = f"Failed to load: {pm.failed[name]}"
                elif p and p.description:
                    detail = p.description.splitlines()[0]
                elif not pm.is_enabled(name):
                    detail = "Switched off"
                else:
                    detail = str(path)
                text.append(Gtk.Label(label=detail, xalign=0, wrap=True, css_classes=["dim-label"]))
                sw = Gtk.Switch(active=pm.is_enabled(name), valign=Gtk.Align.CENTER)
                sw.connect("notify::active", self._toggled, name)
                row.append(text)
                row.append(sw)
                lst.append(row)
            box.append(lst)
            self.restart_note = Gtk.Label(label="Changes take effect when LinDVB is started again.",
                                          xalign=0, css_classes=["dim-label"], visible=False)
            box.append(self.restart_note)
        else:
            box.append(Gtk.Label(label="No plugins installed.", xalign=0,
                                  css_classes=["dim-label"]))
        if pm.menu_actions:
            box.append(Gtk.Label(label="Plugin actions", css_classes=["heading"], xalign=0))
            for _plugin, label, cb in pm.menu_actions:
                b = Gtk.Button(label=label, halign=Gtk.Align.START)
                b.connect("clicked", lambda _b, cb=cb: cb(app))
                box.append(b)

        dirs = ", ".join(str(d) for d in pm.plugin_dirs())
        box.append(Gtk.Label(label=f"Plugin folders: {dirs}", xalign=0, wrap=True,
                             css_classes=["dim-label"]))
        self.set_child(scrolled(box))

    def _toggled(self, sw, _pspec, name):
        self.app.plugins.set_enabled(name, sw.get_active())
        self.app.settings.save()
        self.restart_note.set_visible(True)
