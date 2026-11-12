# SPDX-License-Identifier: GPL-2.0-or-later
"""Timer recordings window (one-off, repeating and series timers).

Channel list with search on the left. On the right the timers and series
rules as tabs on top, below them the editor in the order programme, time,
recording and file, and the editor's buttons along the bottom edge.
"""

import time
from datetime import date, datetime, timedelta
from pathlib import Path

from gi.repository import Gio, GLib, Gtk

from ..recformat import (AFTER_ACTIONS, AUDIO_MODE_LABELS, AUDIO_MODES, EXTENSIONS,
                         EXTRA_LABELS, EXTRAS, FORMAT_LABELS, PLACEHOLDERS, RADIO_FORMATS,
                         IPTV_EXTRAS_NOTE, TIMER_ACTIONS, TV_FORMATS, allowed_formats,
                         audio_note, effective_format, expand_scheme, unavailable_reason,
                         usable_extras)
from ..recorder import WEEKDAYS, Timer
from .common import day, dropdown, fill, hm, make_column_view, message, scrolled, selected
from ..i18n import C_, _, N_


ALL_STREAMS_TIP = _("Records every stream the channel announces, also HbbTV data and new "
                    "audio tracks, with a programme table for this channel only. "
                    "Only MPEG-TS can hold it, so TS is used.")


def _label(text, mnemonic_widget=None):
    lbl = Gtk.Label(label=text, xalign=0, use_underline=True)
    if mnemonic_widget is not None:
        lbl.set_mnemonic_widget(mnemonic_widget)
    return lbl


def _time_entry():
    return Gtk.Entry(width_chars=6, max_length=5, placeholder_text=_("HH:MM"))


class FormatChooser:
    """A drop-down of all formats; the ones that cannot hold the ticked extras
    are greyed out and cannot be picked."""

    def __init__(self, formats, radio):
        self.formats = list(formats)
        self.radio = radio
        self.extras = []
        self.iptv = False
        self.allowed = list(formats)
        self._last = self.formats[0]
        self.model = Gtk.StringList.new([FORMAT_LABELS[f] for f in formats])
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(Gtk.Label(xalign=0)))
        factory.connect("bind", self._bind)
        factory.connect("unbind", lambda _f, item: self._bound.discard(item))
        self._bound = set()            # list items on screen (button and pop-up)
        self.widget = Gtk.DropDown(model=self.model, factory=factory, hexpand=True)
        self.widget.connect("notify::selected", self._selected)

    def _bind(self, _factory, item):
        self._bound.add(item)
        self._style(item)

    def _style(self, item):
        pos = item.get_position()
        if not 0 <= pos < len(self.formats):
            return
        fmt = self.formats[pos]
        ok = fmt in self.allowed
        label = item.get_child()
        label.set_text(FORMAT_LABELS[fmt])
        label.set_sensitive(ok)
        label.set_tooltip_text(None if ok else
                               unavailable_reason(fmt, self.extras, self.radio, self.iptv))
        item.set_selectable(ok)
        item.set_activatable(ok)
        # A row that cannot be activated loses the row padding: keep the height.
        pad = 0 if ok else 6
        label.set_margin_top(pad)
        label.set_margin_bottom(pad)
        label.set_margin_start(pad)

    def _selected(self, *_):
        fmt = self.get()
        if fmt in self.allowed:
            self._last = fmt
        else:                          # a greyed-out entry: go back to the last choice
            self.set(self._last)

    def get(self):
        i = self.widget.get_selected()
        return self.formats[i] if 0 <= i < len(self.formats) else "TS"

    def set(self, fmt):
        fmt = effective_format(fmt, self.extras, self.radio, self.iptv)
        self._last = fmt
        self.widget.set_selected(self.formats.index(fmt) if fmt in self.formats else 0)

    def restrict(self, extras, iptv=False):
        """Grey out the formats that cannot hold the extras; keep the choice if possible."""
        current = self._last
        self.extras = list(extras)
        self.iptv = iptv
        allowed = allowed_formats(extras, self.radio, iptv)
        self.allowed = allowed
        for item in list(self._bound):
            self._style(item)
        self.set(current)
        self.widget.set_sensitive(len(allowed) > 1)


class TimersWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(title=_("Recording Timers"), transient_for=parent,
                         default_width=1240, default_height=820)
        self.app = app
        self.channel = None
        self.date = date.today()
        self.folder = ""

        # -- channel list (left) ------------------------------------------------
        self.search = Gtk.SearchEntry(placeholder_text=_("Search channels"))
        self.search.connect("search-changed", lambda *_: self._fill_channels())
        self.cview, self.cstore, self.csel = make_column_view(
            [(_("Channel"), lambda c: c.name + (_("  (Radio)") if c.radio else ""), True)])
        self.csel.connect("notify::selected-item", lambda *_: self._pick_channel(selected(self.csel)))
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                       margin_end=12, margin_top=12, margin_bottom=12)
        left.append(self.search)
        left.append(Gtk.Frame(child=scrolled(self.cview)))

        # -- timers and series rules (top right) --------------------------------
        cols = [(_("Channel"), lambda t: t.channel_name, False),
                (_("Date"), lambda t: day(t.start), False),
                (_("Start"), lambda t: hm(t.start), False), (_("End"), lambda t: hm(t.end), False),
                (_("Name"), lambda t: t.title, True),
                (_("Repeat"), lambda t: " ".join(_(WEEKDAYS[d]) for d in t.days) or
                 (_("Series") if t.series else _("Once")), False),
                (_("Action"), lambda t: _(t.action), False),
                (_("Format"), self._format_text, False),
                (_("After"), lambda t: _(t.after), False),
                (_("Status"), lambda t: _(t.status) if t.enabled else _("Disabled"), False)]
        self.view, self.store, self.sel = make_column_view(cols)
        # No automatic selection: refilling the list must not load another timer.
        self.sel.set_autoselect(False)
        self.sel.set_can_unselect(True)
        self.sel.connect("notify::selected-item", lambda *_: self._load(selected(self.sel)))

        self.lists = Gtk.Stack(vexpand=True)
        self.lists.add_titled(Gtk.Frame(child=scrolled(self.view)), "timers", _("Timers"))
        self.lists.add_titled(self._series(), "series", _("Series recordings"))
        head = Gtk.Box(spacing=6)
        head.append(Gtk.StackSwitcher(stack=self.lists))
        head.append(Gtk.Box(hexpand=True))
        self.list_btns = self._list_buttons()
        head.append(self.list_btns)
        self.lists.connect("notify::visible-child-name", lambda *_: self.list_btns.set_visible(
            self.lists.get_visible_child_name() == "timers"))
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=16,
                      margin_end=16, margin_top=12, margin_bottom=12)
        top.append(head)
        top.append(self.lists)

        # -- editor (bottom right), its buttons always visible below it ---------
        editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=16,
                         margin_end=16, margin_top=12, margin_bottom=12)
        self.ed_title = Gtk.Label(xalign=0, css_classes=["lv-page-title"])
        editor.append(self.ed_title)
        editor.append(self._editor())
        split = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=230, wide_handle=True,
                          shrink_start_child=False, shrink_end_child=False)
        split.set_start_child(top)
        split.set_end_child(scrolled(editor))
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-dialog-pane"])
        right.append(split)
        right.append(Gtk.Separator())
        right.append(self._buttons())

        paned = Gtk.Paned(position=280, wide_handle=True, shrink_start_child=False,
                          shrink_end_child=False)
        paned.set_start_child(left)
        paned.set_end_child(right)
        self.set_child(paned)

        self._fill_channels()
        self.on_new()
        self.refresh()
        self._hid = app.timers.connect("changed", lambda *_: self.refresh())
        self.connect("close-request", lambda *_: (app.timers.disconnect(self._hid), False)[1])

    # -- editor ---------------------------------------------------------------
    def _editor(self):
        """The editor in titled groups, laid out like the preferences: what, when,
        how and where to record. Labels sit left of their fields."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        def section(title):
            box.append(Gtk.Label(label=title, xalign=0, css_classes=["lv-section-title"],
                                 margin_top=10))
            grid = Gtk.Grid(row_spacing=10, column_spacing=12, margin_top=10, margin_bottom=10)
            frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
            frame.append(grid)
            box.append(frame)
            return grid

        def field(grid, col, row, text, widget, width=1, label_for=None):
            lbl = _label(text, label_for or widget)
            lbl.add_css_class("lv-field-label")
            grid.attach(lbl, col, row, 1, 1)
            grid.attach(widget, col + 1, row, width, 1)
            return lbl

        s = self.app.settings

        # channel and name
        g = section(_("Programme"))
        self.ch_e = Gtk.Entry(editable=False, can_focus=False, width_chars=24,
                              placeholder_text=_("Pick a channel on the left"))
        self.title_e = Gtk.Entry(hexpand=True)
        self.title_e.connect("changed", lambda *_: self._update_preview())
        field(g, 0, 0, _("Channel"), self.ch_e)
        field(g, 2, 0, _("_Name"), self.title_e)

        # date, start, end, lead, follow, weekdays
        g = section(_("Time"))
        self.cal = Gtk.Calendar()
        self.cal.connect("day-selected", self._on_day_selected)
        pop = Gtk.Popover(child=self.cal)
        self.date_btn = Gtk.MenuButton(popover=pop, always_show_arrow=True)
        self.lead = Gtk.SpinButton.new_with_range(0, 180, 1)
        self.start_e = _time_entry()
        self.end_e = _time_entry()
        self.follow = Gtk.SpinButton.new_with_range(0, 360, 1)
        days = Gtk.Box(spacing=4)
        self.day_btns = []
        for d in WEEKDAYS:
            b = Gtk.ToggleButton(label=_(d))
            b.set_tooltip_text(_("Repeat every {day}").format(day=_(d)))
            days.append(b)
            self.day_btns.append(b)
        field(g, 0, 0, _("_Date"), self.date_btn)
        field(g, 2, 0, _("_Start"), self.start_e)
        field(g, 4, 0, _("_End"), self.end_e)
        field(g, 0, 1, _("_Lead (min)"), self.lead)
        field(g, 2, 1, _("_Follow (min)"), self.follow)
        field(g, 0, 2, _("Repeat on"), days, width=5)

        # what the timer does, then formats and tracks
        g = section(C_("preferences page", "Recording"))
        self.action = dropdown([_(a) for a in TIMER_ACTIONS])
        self.after = dropdown([_(a) for a in AFTER_ACTIONS])
        self.action.connect("notify::selected", lambda *_: self._sync_action())
        for w in (self.action, self.after):
            w.set_hexpand(True)
        field(g, 0, 0, _("_Timer action"), self.action)
        field(g, 2, 0, _("_After recording"), self.after)
        g.attach(Gtk.Separator(margin_top=2, margin_bottom=2), 0, 1, 4, 1)
        # formats and tracks: from the preferences, or chosen for this timer;
        # for a series timer also: the whole series or this recording only
        self.opt_mode = dropdown([_("General settings"), _("For this recording")])
        self.opt_mode.set_tooltip_text(
            _("General settings: formats, audio tracks and extra streams come from the "
              "preferences when the recording starts."))
        self.opt_mode.connect("notify::selected", lambda *_: self._on_opt_mode())
        field(g, 0, 2, _("Formats and _tracks"), self.opt_mode)
        self.scope = dropdown([_("Whole series"), _("Next recording only")])
        self.scope_lbl = field(g, 2, 2, _("_Apply to"), self.scope)
        self._show_scope(False)
        self.tv_fmt = FormatChooser(TV_FORMATS, radio=False)
        self.radio_fmt = FormatChooser(RADIO_FORMATS, radio=True)
        for w in (self.tv_fmt.widget, self.radio_fmt.widget):
            w.connect("notify::selected", lambda *_: self._format_changed())
        field(g, 0, 3, _("T_V format"), self.tv_fmt.widget)
        field(g, 2, 3, _("_Radio format"), self.radio_fmt.widget)
        self.audio = dropdown([AUDIO_MODE_LABELS[m] for m in AUDIO_MODES])
        self.audio.set_tooltip_text(
            _("Preferred language only: the tracks in the audio language from the preferences, "
            "without audio description. MP4 and the radio audio file keep one track."))
        self.audio.connect("notify::selected", lambda *_: self._sync_formats())
        field(g, 0, 4, _("Audi_o tracks"), self.audio)
        extras = Gtk.Grid(row_spacing=4, column_spacing=24)
        self.extra_btns = {}
        for i, e in enumerate(EXTRAS):
            b = Gtk.CheckButton(label=EXTRA_LABELS[e])
            b.connect("toggled", lambda *_: self._sync_formats())
            extras.attach(b, i % 2, i // 2, 1, 1)
            self.extra_btns[e] = b
        self.extra_btns["teletext"].set_tooltip_text(_("Teletext only exists in MPEG-TS, so TS is used."))
        self.extra_btns["epg_data"].set_tooltip_text(_("EPG data (EIT) only exists in MPEG-TS, so TS is used."))
        self.extra_btns["all_streams"].set_tooltip_text(ALL_STREAMS_TIP)
        self.extra_btns["subtitles"].set_tooltip_text(
            _("TS keeps all subtitle streams. MKV gets the teletext subtitle page as a text track; "
            "DVB bitmap subtitles are kept in TS only."))
        lbl = field(g, 0, 5, _("Extra streams"), extras, width=3)
        lbl.set_valign(Gtk.Align.START)
        self.fmt_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["lv-hint"])
        g.attach(self.fmt_hint, 0, 6, 4, 1)

        # file naming scheme, placeholders, folder
        g = section(_("File"))
        self.scheme_e = Gtk.Entry(hexpand=True)
        self.scheme_e.connect("changed", lambda *_: self._update_preview())
        folder_btn = Gtk.Button(label=_("Folder…"))
        folder_btn.connect("clicked", lambda *_: self._choose_folder())
        sbox = Gtk.Box(spacing=6)
        sbox.append(self.scheme_e)
        sbox.append(self._placeholder_button())
        sbox.append(folder_btn)
        field(g, 0, 0, _("File _naming scheme"), sbox, label_for=self.scheme_e)
        self.folder_lbl = Gtk.Label(xalign=0, ellipsize=1, hexpand=True, css_classes=["lv-hint"])
        reset = Gtk.Button(icon_name="edit-undo-symbolic", css_classes=["flat"],
                           tooltip_text=_("Use the recording folder from the preferences"))
        reset.connect("clicked", lambda *_: self._set_folder(""))
        fbox = Gtk.Box(spacing=4)
        fbox.append(self.folder_lbl)
        fbox.append(reset)
        g.attach(fbox, 0, 1, 2, 1)
        self.preview = Gtk.Label(xalign=0, ellipsize=1, selectable=True, css_classes=["lv-hint"])
        g.attach(self.preview, 0, 2, 2, 1)
        self.scheme_e.set_tooltip_text(", ".join(p for p, _unused in PLACEHOLDERS))
        self._default_scheme = s["name_scheme"]
        return box

    def _placeholder_button(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        pop = Gtk.Popover(child=box)
        for ph, text in PLACEHOLDERS:
            b = Gtk.Button(css_classes=["flat"])
            b.set_child(Gtk.Label(label=f"{ph}   {text}", xalign=0))
            b.connect("clicked", lambda _b, ph=ph: (self._insert_placeholder(ph), pop.popdown()))
            box.append(b)
        return Gtk.MenuButton(label=_("Placeholders"), popover=pop)

    def _insert_placeholder(self, ph):
        pos = self.scheme_e.get_position()
        text = self.scheme_e.get_text()
        if pos < 0 or pos > len(text):
            pos = len(text)
        self.scheme_e.set_text(text[:pos] + ph + text[pos:])
        self.scheme_e.set_position(pos + len(ph))

    def _choose_folder(self):
        dlg = Gtk.FileDialog(title=_("Recording folder"))
        start = Path(self.folder or self.app.settings["recording_dir"]).expanduser()
        if start.is_dir():
            dlg.set_initial_folder(Gio.File.new_for_path(str(start)))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self._set_folder(f.get_path())

        dlg.select_folder(self, None, done)

    def _set_folder(self, folder):
        self.folder = folder
        shown = folder or _("{folder} (from the preferences)").format(
            folder=self.app.settings["recording_dir"])
        self.folder_lbl.set_text(_("Folder: ") + shown)
        self._update_preview()

    def _list_buttons(self):
        """Buttons that act on the timer selected in the list."""
        box = Gtk.Box(spacing=6)
        for label, fn, tip in (
                (_("New"), self.on_new, _("Clear the editor")),
                (_("Enable/Disable"), self.on_toggle, _("Switch the selected timer on or off")),
                (_("Delete"), self.on_delete, _("Delete the selected timer"))):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", lambda _b, fn=fn: fn())
            box.append(b)
        return box

    def _buttons(self):
        """The editor's buttons, at the bottom of the window."""
        box = Gtk.Box(spacing=6, margin_start=16, margin_end=16, margin_top=10, margin_bottom=10)
        defaults = Gtk.Button(label=_("Save as default"), tooltip_text=_(
            "Use these formats and extra streams for new timers and manual recordings"))
        defaults.connect("clicked", lambda *_: self.on_save_defaults())
        box.append(defaults)
        box.append(Gtk.Box(hexpand=True))
        for label, fn, tip, cls in (
                (_("Close"), self.close, None, None),
                (_("Apply"), self.on_apply, _("Save the editor's settings to the selected timer"),
                 None),
                (_("Add"), self.on_add, _("Add the editor's settings as a new timer"),
                 "suggested-action")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            if cls:
                b.add_css_class(cls)
            b.connect("clicked", lambda _b, fn=fn: fn())
            box.append(b)
        return box

    def _series(self):
        rcols = [(_("Series title"), lambda r: r.title, True),
                 (_("Channel"), lambda r: (self.app.db.by_key(r.channel_key).name
                                        if self.app.db.by_key(r.channel_key) else "Any"), False)]
        self.rview, self.rstore, self.rsel = make_column_view(rcols)
        rdel = Gtk.Button(label=_("Delete series rule"), halign=Gtk.Align.END)
        rdel.connect("clicked", lambda *_: self.on_delete_rule())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(Gtk.Frame(child=scrolled(self.rview)))
        box.append(rdel)
        return box

    # -- state ----------------------------------------------------------------
    def select(self, timer):
        """Select a timer in the list and load it into the editor."""
        for i, t in enumerate(self.app.timers.timers):
            if t.id == timer.id:
                self.sel.set_selected(i)
                self.view.scroll_to(i, None, Gtk.ListScrollFlags.SELECT, None)
                return

    def refresh(self):
        fill(self.store, self.app.timers.timers)
        fill(self.rstore, self.app.timers.rules)

    def _fill_channels(self):
        q = self.search.get_text().strip().lower()
        fill(self.cstore, [c for c in self.app.db.channels if q in c.name.lower()])

    def _pick_channel(self, c):
        if c is None:
            return
        self.channel = c
        self.ch_e.set_text(c.name)
        self._sync_formats()

    def _format_text(self, t):
        ch = self.app.db.by_key(t.channel_key)
        radio = bool(ch and ch.radio)
        fmt = effective_format(t.radio_format if radio else t.tv_format, t.extras, radio,
                               bool(ch and ch.via_url))
        return FORMAT_LABELS[fmt].split(" (")[0] if fmt == "Audio" else fmt

    def _on_day_selected(self, cal):
        dt = cal.get_date()
        self.date = date(dt.get_year(), dt.get_month(), dt.get_day_of_month())
        self.date_btn.set_label(self.date.strftime("%a %d.%m.%Y"))
        self._update_preview()

    def _set_date(self, d):
        self.date = d
        self.cal.select_day(GLib.DateTime.new_local(d.year, d.month, d.day, 0, 0, 0))
        self.date_btn.set_label(d.strftime("%a %d.%m.%Y"))

    def _extras(self):
        return [e for e, b in self.extra_btns.items() if b.get_active()]

    def _audio_mode(self):
        return AUDIO_MODES[self.audio.get_selected()]

    def _set_audio_mode(self, mode):
        self.audio.set_selected(AUDIO_MODES.index(mode) if mode in AUDIO_MODES else 0)

    def _iptv(self):
        return bool(self.channel and self.channel.via_url)

    def _sync_formats(self):
        iptv = self._iptv()
        extras = usable_extras(self._extras(), iptv)
        self.tv_fmt.restrict(extras, iptv)
        self.radio_fmt.restrict(extras, iptv)
        records = TIMER_ACTIONS[self.action.get_selected()] != "Tune only"
        for b in self.extra_btns.values():
            b.set_sensitive(records and not iptv)
        why = [EXTRA_LABELS[e].split(" (")[0] for e in ("teletext", "epg_data", "all_streams")
               if e in extras]
        radio = bool(self.channel and self.channel.radio)
        fmt = effective_format((self.radio_fmt if radio else self.tv_fmt).get(), extras, radio,
                               iptv)
        notes = []
        if iptv:
            notes.append(IPTV_EXTRAS_NOTE)
        if why:
            notes.append(_("{items} can only be kept in MPEG-TS, so TS is used.").format(
                items=", ".join(why)))
        elif "subtitles" in extras:
            notes.append(_("Subtitles fit into TS or MKV."))
        note = audio_note(fmt, self._audio_mode(), iptv)
        if note:
            notes.append(note)
        if self._audio_mode() == "language" and not self.app.settings.get("rec_audio_language"):
            notes.append(_("No preferred audio language in the preferences: the first track is "
                         "recorded."))
        self.fmt_hint.set_text(" ".join(notes))
        self._update_preview()
        self._grey_general()

    def _general(self):
        return self.opt_mode.get_selected() == 0

    def _show_scope(self, on):
        self.scope_lbl.set_visible(on)
        self.scope.set_visible(on)
        self.scope.set_selected(0)

    def _on_opt_mode(self):
        """General settings: show the preferences' values, greyed out."""
        if not hasattr(self, "extra_btns"):
            return
        if self._general():
            d = self.app.timers.defaults()
            for e, b in self.extra_btns.items():
                b.set_active(d[e])
            self._set_audio_mode(d["audio_mode"])
            extras = [e for e in d if e in self.extra_btns and d[e]]
            self.tv_fmt.restrict(extras, self._iptv())
            self.radio_fmt.restrict(extras, self._iptv())
            self.tv_fmt.set(d["tv_format"])
            self.radio_fmt.set(d["radio_format"])
        self._sync_action()

    def _grey_general(self):
        if hasattr(self, "opt_mode") and self._general():
            for w in (self.tv_fmt.widget, self.radio_fmt.widget, self.audio,
                      *self.extra_btns.values()):
                w.set_sensitive(False)

    def _format_changed(self):
        if hasattr(self, "audio"):
            self._sync_formats()

    def _sync_action(self):
        """A tune-only timer records nothing, so its recording options are greyed out."""
        records = TIMER_ACTIONS[self.action.get_selected()] != "Tune only"
        for b in self.extra_btns.values():
            b.set_sensitive(records and not self._iptv())
        self.audio.set_sensitive(records)
        self.scheme_e.set_sensitive(records)
        if records:
            self._sync_formats()
        else:
            self.tv_fmt.widget.set_sensitive(False)
            self.radio_fmt.widget.set_sensitive(False)
        self.opt_mode.set_sensitive(records)
        self._grey_general()

    def _update_preview(self):
        if not hasattr(self, "preview"):
            return
        c = self.channel
        radio = bool(c and c.radio)
        fmt = (self.radio_fmt if radio else self.tv_fmt).get()
        fmt = effective_format(fmt, self._extras(), radio, self._iptv())
        start, end = self._times() or (time.time(), time.time())
        ext = "" if fmt == "Audio" else EXTENSIONS[fmt]
        name = expand_scheme(self.scheme_e.get_text() or self._default_scheme,
                             c.name if c else _("Channel"), self.title_e.get_text().strip() or
                             (c.name if c else _("Name")), start, end) + (ext or ".mp2/.mp3/.aac/.ac3")
        folder = Path(self.folder or self.app.settings["recording_dir"]).expanduser()
        self.preview.set_text(_("File: {v0}").format(v0=folder / name))

    # -- reading and writing a timer ----------------------------------------
    def _load(self, t):
        if not t:
            return
        self.ed_title.set_text(_("Edit timer"))
        c = self.app.db.by_key(t.channel_key)
        self.channel = c
        self.ch_e.set_text(c.name if c else t.channel_name)
        self.title_e.set_text(t.title)
        self._set_date(datetime.fromtimestamp(t.start).date())
        self.start_e.set_text(hm(t.start))
        self.end_e.set_text(hm(t.end))
        self.lead.set_value(t.lead)
        self.follow.set_value(t.follow)
        for i, b in enumerate(self.day_btns):
            b.set_active(i in t.days)
        self.action.set_selected(TIMER_ACTIONS.index(t.action) if t.action in TIMER_ACTIONS else 0)
        self.after.set_selected(AFTER_ACTIONS.index(t.after) if t.after in AFTER_ACTIONS else 0)
        for e, b in self.extra_btns.items():
            b.set_active(bool(getattr(t, e)))
        self._set_audio_mode(t.audio_mode)
        self.tv_fmt.restrict(t.extras, self._iptv())
        self.radio_fmt.restrict(t.extras, self._iptv())
        self.tv_fmt.set(t.tv_format)
        self.radio_fmt.set(t.radio_format)
        self.scheme_e.set_text(t.name_scheme or self._default_scheme)
        self._set_folder(t.folder)
        self.opt_mode.set_selected(0 if t.general else 1)
        self._show_scope(t.series)
        self._on_opt_mode()

    def on_new(self):
        s = self.app.settings
        now = time.time()
        self.sel.set_selected(Gtk.INVALID_LIST_POSITION)
        self.ed_title.set_text(_("New timer"))
        self.title_e.set_text("")
        self._set_date(datetime.fromtimestamp(now + 300).date())
        self.start_e.set_text(hm(now + 300))
        self.end_e.set_text(hm(now + 3900))
        self.lead.set_value(s["lead_minutes"])
        self.follow.set_value(s["follow_minutes"])
        for b in self.day_btns:
            b.set_active(False)
        self.action.set_selected(0)
        self.after.set_selected(0)
        d = self.app.timers.defaults()
        for e, b in self.extra_btns.items():
            b.set_active(d[e])
        self._set_audio_mode(d["audio_mode"])
        extras = [e for e in EXTRAS if d[e]]
        self.tv_fmt.restrict(extras, self._iptv())
        self.radio_fmt.restrict(extras, self._iptv())
        self.tv_fmt.set(d["tv_format"])
        self.radio_fmt.set(d["radio_format"])
        self.scheme_e.set_text(self._default_scheme)
        self._set_folder("")
        self.opt_mode.set_selected(0)
        self._show_scope(False)
        if self.channel is None and self.app.window is not None:
            self.channel = getattr(self.app.window, "current", None)
            if self.channel is not None:
                self.ch_e.set_text(self.channel.name)
        self._sync_action()

    def _times(self):
        try:
            st = datetime.strptime(self.start_e.get_text().strip(), "%H:%M").time()
            en = datetime.strptime(self.end_e.get_text().strip(), "%H:%M").time()
        except ValueError:
            return None
        start = datetime.combine(self.date, st)
        end = datetime.combine(self.date, en)
        if end <= start:
            end += timedelta(days=1)
        return start.timestamp(), end.timestamp()

    def _read(self):
        times = self._times()
        if times is None:
            message(self, _("Invalid start or end time"), _("Use HH:MM, for example 20:15."))
            return None
        c = self.channel
        if c is None:
            message(self, _("No channel"), _("Pick the channel to record in the list on the left."))
            return None
        scheme = self.scheme_e.get_text().strip()
        return Timer(c.key, c.name, self.title_e.get_text().strip() or c.name,
                     int(times[0]), int(times[1]),
                     days=[i for i, b in enumerate(self.day_btns) if b.get_active()],
                     after=AFTER_ACTIONS[self.after.get_selected()],
                     lead=int(self.lead.get_value()), follow=int(self.follow.get_value()),
                     action=TIMER_ACTIONS[self.action.get_selected()],
                     name_scheme="" if scheme == self._default_scheme else scheme,
                     folder=self.folder,
                     tv_format=self.tv_fmt.get(), radio_format=self.radio_fmt.get(),
                     audio_mode=self._audio_mode(), general=self._general(),
                     **{e: b.get_active() for e, b in self.extra_btns.items()})

    def on_add(self):
        t = self._read()
        if t:
            self.app.timers.add(t, with_margins=False)

    def on_apply(self):
        old = selected(self.sel)
        if not old:
            self.on_add()
            return
        t = self._read()
        if not t:
            return
        t.id, t.enabled, t.series = old.id, old.enabled, old.series
        t.status = old.status if old.id in self.app.timers.active else N_("Scheduled")
        self.app.timers.timers = [t if x.id == old.id else x for x in self.app.timers.timers]
        if t.series and self.scope.get_selected() == 0:
            # the whole series records with these options from now on
            tm = self.app.timers
            tm.set_series_options(t, None if t.general else tm.options_of(t))
        self.app.timers.timers.sort(key=lambda x: x.rec_start)
        self.app.timers.save()

    def on_delete(self):
        t = selected(self.sel)
        if t:
            self.app.timers.remove(t)

    def on_toggle(self):
        t = selected(self.sel)
        if t:
            t.enabled = not t.enabled
            self.app.timers.save()

    def on_save_defaults(self):
        s = self.app.settings
        s["rec_tv_format"] = self.tv_fmt.get()
        s["rec_radio_format"] = self.radio_fmt.get()
        s["rec_audio_mode"] = self._audio_mode()
        for e, b in self.extra_btns.items():
            s["rec_" + e] = b.get_active()
        s.save()
        self.app.window and self.app.window.set_status(_("Recording defaults saved"))

    def on_delete_rule(self):
        r = selected(self.rsel)
        if r:
            self.app.timers.rules = [x for x in self.app.timers.rules if x.id != r.id]
            self.app.timers.save()


class RecordOptionsWindow(Gtk.Window):
    """Format and tracks for one manual recording of the current channel.

    Starts with the defaults from the preferences; the choice is used for this
    recording only.
    """

    def __init__(self, app, parent, channel, on_record):
        super().__init__(title=_("Record with Options"), transient_for=parent, modal=True,
                         default_width=440, resizable=False)
        self.app = app
        self.channel = channel
        self.on_record = on_record
        d = app.timers.defaults()

        hb = Gtk.HeaderBar(show_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        rec = Gtk.Button(label=_("Record"), css_classes=["suggested-action"])
        rec.connect("clicked", lambda *_: self._record())
        hb.pack_end(rec)
        self.set_titlebar(hb)
        self.set_default_widget(rec)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16,
                      margin_end=16, margin_top=12, margin_bottom=16)
        box.append(Gtk.Label(label=channel.name, xalign=0, css_classes=["heading"]))
        radio = channel.radio
        self.fmt = FormatChooser(RADIO_FORMATS if radio else TV_FORMATS, radio=radio)
        self.fmt.widget.connect("notify::selected", lambda *_: self._sync())
        box.append(_label(_("_Format"), self.fmt.widget))
        box.append(self.fmt.widget)
        self.audio = dropdown([AUDIO_MODE_LABELS[m] for m in AUDIO_MODES],
                              AUDIO_MODES.index(d["audio_mode"]))
        self.audio.connect("notify::selected", lambda *_: self._sync())
        box.append(_label(_("Audi_o tracks"), self.audio))
        box.append(self.audio)
        self.extra_btns = {}
        iptv = channel.via_url
        for e in EXTRAS:
            b = Gtk.CheckButton(label=EXTRA_LABELS[e], active=d[e] and not iptv,
                                sensitive=not iptv)
            if iptv:
                b.set_tooltip_text(IPTV_EXTRAS_NOTE)
            b.connect("toggled", lambda *_: self._sync())
            box.append(b)
            self.extra_btns[e] = b
        self.hint = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"], max_width_chars=50)
        box.append(self.hint)
        self.set_child(box)
        self._ready = True
        self.fmt.restrict(self._extras(), iptv)
        self.fmt.set(d["radio_format" if radio else "tv_format"])
        self._sync()

    def _extras(self):
        return [e for e, b in self.extra_btns.items() if b.get_active()]

    def _sync(self):
        if not getattr(self, "_ready", False):
            return
        iptv = self.channel.via_url
        extras = usable_extras(self._extras(), iptv)
        self.fmt.restrict(extras, iptv)
        mode = AUDIO_MODES[self.audio.get_selected()]
        notes = []
        if iptv:
            notes.append(IPTV_EXTRAS_NOTE)
        if any(e in extras for e in ("teletext", "epg_data")):
            notes.append(_("Teletext and EPG data can only be kept in MPEG-TS, so TS is used."))
        elif "all_streams" in extras:
            notes.append(ALL_STREAMS_TIP)
        note = audio_note(self.fmt.get(), mode, iptv)
        if note:
            notes.append(note)
        if mode == "language":
            lang = self.app.settings.get("rec_audio_language")
            notes.append(_("Preferred audio language: {lang}.").format(lang=lang) if lang else
                         _("No preferred audio language in the preferences: the first track "
                         "is recorded."))
        self.hint.set_text(" ".join(notes))
        self.hint.set_visible(bool(notes))

    def _record(self):
        fmt = self.fmt.get()
        key = "radio_format" if self.channel.radio else "tv_format"
        self.on_record({key: fmt, "audio_mode": AUDIO_MODES[self.audio.get_selected()],
                        **{e: b.get_active() for e, b in self.extra_btns.items()}})
        self.close()
