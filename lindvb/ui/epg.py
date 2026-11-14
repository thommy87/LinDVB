# SPDX-License-Identifier: GPL-2.0-or-later
"""Programme guide window: what's on now, per channel, timeline and search."""

import hashlib
import os
import re
import threading
import time

from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from ..config import CACHE_DIR
from ..contentguide import AUDIO_DESCRIPTION, SUBTITLES, badges, image_url
from .common import day, dropdown, fill, hm, make_column_view, scrolled, selected
from ..i18n import _

ROW_H = 34
DESC_LINES = 3      # description lines before "Show more"
IMAGE_DIR = CACHE_DIR / "dvbi" / "images"
# Now/next filter (DTVP IT-16: let viewers find programmes with these features)
ACCESS_FILTERS = [(_("All programmes"), None), (_("With audio description"), AUDIO_DESCRIPTION),
                  (_("With subtitles"), SUBTITLES)]


def _title(e):
    b = badges(e)
    return f"{e.title}  {b}" if b else e.title
PX_PER_MIN = 4
NAME_W = 150


class EPGWindow(Gtk.Window):
    def __init__(self, app, parent, channel=None):
        super().__init__(title=_("Programme Guide"), transient_for=parent,
                         default_width=1000, default_height=640)
        self.app = app
        self.parent = parent
        self.selected_event = None      # (channel, event)
        self._image_url = ""
        self.chans = [c for c in app.db.channels]
        # Channel to focus in every view: the one asked for, else the one playing.
        focus = channel or getattr(app.window, "current", None)
        self.focus_key = focus.key if focus else None
        self._initial = True

        nb = Gtk.Notebook()
        nb.append_page(self._now_page(), Gtk.Label(label=_("Now / Next")))
        nb.append_page(self._channel_page(focus), Gtk.Label(label=_("Per Channel")))
        nb.append_page(self._timeline_page(), Gtk.Label(label=_("TV Guide")))
        nb.append_page(self._search_page(), Gtk.Label(label=_("Search")))
        if channel:
            nb.set_current_page(1)

        _install_css()
        self.insert_action_group("epg", self._actions())
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=420)
        paned.set_start_child(nb)
        lower = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        margin_start=10, margin_end=10, margin_top=8, margin_bottom=8)
        lower.append(self._action_bar())
        lower.append(Gtk.Separator())
        lower.append(self._details_area())
        paned.set_end_child(lower)
        self.set_child(paned)
        self.refresh()
        self._initial = False
        self._select(selected(self.ch_sel if channel else self.now_sel))
        app.epg.listeners.append(self.refresh)
        self.connect("close-request", lambda *_: (app.epg.listeners.remove(self.refresh), False)[1])

    # -- details --------------------------------------------------------------
    def _actions(self):
        """Window actions behind the Record and More menus."""
        group = Gio.SimpleActionGroup()
        for name, fn in (("watch-start", self.on_watch_from_start),
                         ("record-options", self.on_record_options),
                         ("series", self.on_series), ("web", self.on_web)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            group.add_action(a)
        self.start_action = group.lookup_action("watch-start")
        self.start_action.set_enabled(False)
        return group

    def _action_bar(self):
        """The main actions as buttons, the rarer ones in menus."""
        bar = Gtk.Box(spacing=6)
        watch = _icon_button(_("Watch"), "media-playback-start-symbolic")
        watch.add_css_class("suggested-action")
        watch.connect("clicked", lambda _b: self.on_watch())
        bar.append(watch)
        # Record with a menu for the timer options and series recordings
        rec = Gtk.Box(css_classes=["linked"])
        rec_btn = _icon_button(_("Record"), "media-record-symbolic")
        rec_btn.connect("clicked", lambda _b: self.on_record())
        rec_menu = Gio.Menu()
        rec_menu.append(_("Record with Options…"), "epg.record-options")
        rec_menu.append(_("Record Series"), "epg.series")
        rec.append(rec_btn)
        rec.append(Gtk.MenuButton(icon_name="pan-down-symbolic", menu_model=rec_menu,
                                  tooltip_text=_("More options")))
        bar.append(rec)
        self.remind_btn = _icon_button(_("Remind Me"), "alarm-symbolic")
        self.remind_btn.connect("clicked", lambda _b: self.on_remind())
        bar.append(self.remind_btn)
        bar.append(Gtk.Box(hexpand=True))
        export = Gtk.Button(icon_name="document-save-symbolic",
                            tooltip_text=_("Export the guide as XMLTV"))
        export.connect("clicked", lambda _b: self.app.window.on_export_epg())
        bar.append(export)
        more = Gio.Menu()
        more.append(_("Watch from the Start"), "epg.watch-start")
        more.append(_("Search Web"), "epg.web")
        bar.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more,
                                  tooltip_text=_("More options")))
        return bar

    def _details_area(self):
        """Picture, title, subheading, facts as chips and the description."""
        # programme image from the DVB-I content guide
        self.image = _Thumb(can_shrink=True, content_fit=Gtk.ContentFit.COVER,
                            visible=False, valign=Gtk.Align.START,
                            overflow=Gtk.Overflow.HIDDEN, css_classes=["epg-thumb"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        head = Gtk.Box(spacing=8)
        self.title_lbl = Gtk.Label(xalign=0, wrap=True, selectable=True,
                                   wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=["epg-title"])
        self.episode_lbl = Gtk.Label(xalign=0, valign=Gtk.Align.BASELINE,
                                     css_classes=["epg-title", "dim-label"])
        self.title_lbl.set_valign(Gtk.Align.BASELINE)
        head.append(self.title_lbl)
        head.append(self.episode_lbl)
        text.append(head)
        self.sub_lbl = Gtk.Label(xalign=0, wrap=True, selectable=True,
                                 wrap_mode=Pango.WrapMode.WORD_CHAR,
                                 css_classes=["epg-sub", "dim-label"])
        text.append(self.sub_lbl)
        self.chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                                 row_spacing=6, homogeneous=False, max_children_per_line=12,
                                 margin_top=8, valign=Gtk.Align.START)
        text.append(self.chips)
        self.desc_lbl = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True,
                                  wrap_mode=Pango.WrapMode.WORD_CHAR, margin_top=10,
                                  lines=DESC_LINES, ellipsize=Pango.EllipsizeMode.END,
                                  css_classes=["epg-desc"])
        text.append(self.desc_lbl)
        self.more_btn = Gtk.Button(label=_("Show more"), halign=Gtk.Align.START,
                                   css_classes=["flat", "epg-more"], visible=False)
        self.more_btn.connect("clicked", lambda _b: self._toggle_description())
        text.append(self.more_btn)
        self._desc_open = False
        box = Gtk.Box(spacing=16)
        box.append(self.image)
        box.append(text)
        det = scrolled(box)
        det.set_size_request(-1, 150)
        return det

    def _toggle_description(self):
        self._desc_open = not self._desc_open
        self.desc_lbl.set_lines(-1 if self._desc_open else DESC_LINES)
        self.desc_lbl.set_ellipsize(Pango.EllipsizeMode.NONE if self._desc_open
                                    else Pango.EllipsizeMode.END)
        self.more_btn.set_label(_("Show less") if self._desc_open else _("Show more"))

    def _check_description(self):
        """Offer "Show more" only when the description does not fit."""
        if not self._desc_open:
            self.more_btn.set_visible(self.desc_lbl.get_layout().is_ellipsized())
        return False

    # -- pages ------------------------------------------------------------------
    def _now_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=6, margin_start=6, margin_top=6)
        self.at_dd = dropdown([_("Now"), _("Next"), "20:15", "22:00"])
        self.at_dd.connect("notify::selected", lambda *_: self.refresh())
        # all channels or one favourites list (the one shown in the channel
        # list, else the one used last)
        favs = self.app.favs
        self.only_fav = dropdown([_("All channels")] + ["★ " + n for n in favs.names()])
        self.only_fav.set_tooltip_text(_("Only the channels of a favourites list"))
        win = self.app.window
        i = win.current_fav_list() if win is not None and hasattr(win, "current_fav_list") \
            else None
        if i is not None and getattr(win, "_list_fav", None) is not None:
            self.only_fav.set_selected(i + 1)
        self.only_fav.connect("notify::selected", lambda *_: self.refresh())
        self.access_dd = dropdown([label for label, _a in ACCESS_FILTERS])
        self.access_dd.set_tooltip_text(_("Accessibility features (DVB-I programme guide)"))
        self.access_dd.connect("notify::selected", lambda *_: self.refresh())
        top.append(Gtk.Label(label=_("Show:")))
        top.append(self.at_dd)
        top.append(self.only_fav)
        top.append(self.access_dd)
        box.append(top)
        cols = [(_("Channel"), lambda r: r[0].name, False), (_("Start"), lambda r: hm(r[1].start), False),
                (_("End"), lambda r: hm(r[1].end), False), (_("Title"), lambda r: _title(r[1]), True),
                (_("Genre"), lambda r: r[1].genre, False)]
        self.now_view, self.now_store, self.now_sel = make_column_view(cols, lambda r: self.app.window.zap(r[0]))
        self.now_sel.connect("notify::selected-item", lambda s, _p: self._select(selected(s)))
        box.append(scrolled(self.now_view))
        return box

    def _channel_page(self, channel):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        names = [c.name for c in self.chans]
        self.ch_dd = Gtk.DropDown.new_from_strings(names or ["(no channels)"])
        self.ch_dd.set_enable_search(True)
        self.ch_dd.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        if channel:
            idx = next((i for i, c in enumerate(self.chans) if c.key == channel.key), 0)
            self.ch_dd.set_selected(idx)
        self.ch_dd.connect("notify::selected", lambda *_: self.refresh())
        self.ch_dd.set_margin_start(6)
        self.ch_dd.set_margin_top(6)
        self.ch_dd.set_halign(Gtk.Align.START)
        box.append(self.ch_dd)
        cols = [(_("Day"), lambda r: day(r[1].start), False), (_("Start"), lambda r: hm(r[1].start), False),
                (_("Duration"), lambda r: f"{r[1].duration // 60} min", False),
                (_("Title"), lambda r: _title(r[1]), True), (_("Subheading"), lambda r: r[1].subtitle, True)]
        self.ch_view, self.ch_store, self.ch_sel = make_column_view(cols, lambda r: self.on_record())
        self.ch_sel.connect("notify::selected-item", lambda s, _p: self._select(selected(s)))
        box.append(scrolled(self.ch_view))
        return box

    def _timeline_page(self):
        self.tl = Gtk.DrawingArea()
        self.tl.set_draw_func(self._draw_timeline)
        click = Gtk.GestureClick()
        click.connect("pressed", self._timeline_click)
        self.tl.add_controller(click)
        sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        sw.set_child(self.tl)
        self.tl_sw = sw
        return sw

    def _search_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=6, margin_start=6, margin_top=6)
        self.q = Gtk.SearchEntry(placeholder_text=_("Title or description"), hexpand=True)
        self.q.connect("activate", lambda *_: self.do_search())
        self.q_desc = Gtk.CheckButton(label=_("Include description"), active=True)
        self.q_case = Gtk.CheckButton(label=_("Case sensitive"))
        go = Gtk.Button(label=_("Search"))
        go.connect("clicked", lambda *_: self.do_search())
        for w in (self.q, self.q_desc, self.q_case, go):
            top.append(w)
        box.append(top)
        cols = [(_("Channel"), lambda r: r[0].name, False), (_("Day"), lambda r: day(r[1].start), False),
                (_("Start"), lambda r: hm(r[1].start), False), (_("Title"), lambda r: _title(r[1]), True)]
        self.s_view, self.s_store, self.s_sel = make_column_view(cols, lambda r: self.on_record())
        self.s_sel.connect("notify::selected-item", lambda s, _p: self._select(selected(s)))
        box.append(scrolled(self.s_view))
        return box

    # -- data -------------------------------------------------------------------
    def _visible_channels(self):
        i = self.only_fav.get_selected() - 1
        if self.app.favs.valid(i):
            keys = {c.key for c in self.chans}
            return [c for c in self.app.favs.channels(i) if c.key in keys]
        return self.chans

    def refresh(self):
        epg = self.app.epg
        # Keep the user's choice across EPG updates; on first fill use the focus channel.
        keep_now = None if self._initial else selected(self.now_sel)
        keep_ch = None if self._initial else selected(self.ch_sel)
        sel = self.at_dd.get_selected()
        now = time.time()
        rows = []
        for c in self._visible_channels():
            if sel == 0:
                ev = epg.now_next(c.epg_key)[0]
            elif sel == 1:
                ev = epg.now_next(c.epg_key)[1]
            else:
                hh, mm = (20, 15) if sel == 2 else (22, 0)
                lt = time.localtime(now)
                t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
                ev = epg.at(c.epg_key, t)
            want = ACCESS_FILTERS[self.access_dd.get_selected()][1]
            if ev and (want is None or want in (ev.access or [])):
                rows.append((c, ev))
        fill(self.now_store, rows)
        now_key = keep_now[0].key if keep_now else self.focus_key
        self._focus_row(self.now_view, self.now_sel, rows,
                        lambda r: r[0].key == now_key)
        if self.chans:
            c = self.chans[self.ch_dd.get_selected()]
            evs = [(c, e) for e in epg.for_key(c.epg_key)]
            fill(self.ch_store, evs)
            if keep_ch and keep_ch[0].key == c.key:
                match = lambda r: r[1].start == keep_ch[1].start
            else:
                match = lambda r: r[1].end > now
            self._focus_row(self.ch_view, self.ch_sel, evs, match)
        self._timeline_rows = [(c, epg.for_key(c.epg_key)) for c in self._visible_channels()]
        self._timeline_rows = [r for r in self._timeline_rows if r[1]]
        self._t0 = (int(now) // 1800) * 1800
        self.tl.set_content_width(NAME_W + 24 * 60 * PX_PER_MIN)
        self.tl.set_content_height(max(1, len(self._timeline_rows)) * ROW_H + 24)
        self.tl.queue_draw()
        if self._initial:
            self._focus_timeline()

    def _focus_row(self, view, sel, rows, match):
        """Select and scroll to the first row matching, else the first row."""
        if not rows:
            return
        i = next((i for i, r in enumerate(rows) if match(r)), 0)
        sel.set_selected(i)
        if hasattr(view, "scroll_to"):          # GTK >= 4.12
            GLib.idle_add(lambda: (view.scroll_to(i, None, Gtk.ListScrollFlags.NONE, None), False)[1])

    def _focus_timeline(self):
        r = next((i for i, (c, _e) in enumerate(self._timeline_rows) if c.key == self.focus_key), None)
        if r is None:
            return
        adj = self.tl_sw.get_vadjustment()
        y = 24 + r * ROW_H

        def apply(*_):
            page = adj.get_page_size()
            if page <= 0:
                return
            adj.set_value(max(0, min(y - page / 3, adj.get_upper() - page)))
            adj.disconnect(hid)
        hid = adj.connect("changed", apply)

    def do_search(self):
        text = self.q.get_text().strip()
        if not text:
            return
        by_key = {}
        for c in self.chans:
            by_key.setdefault(c.epg_key, c)
        res = [(by_key[k], e) for k, e in self.app.epg.search(text, self.q_desc.get_active(),
                                                              self.q_case.get_active()) if k in by_key]
        fill(self.s_store, sorted(res, key=lambda r: r[1].start))

    def _select(self, row):
        if not row:
            return
        self.selected_event = row
        self._update_remind_button()
        c, e = row
        title, episode = _split_episode(e.title)
        self.title_lbl.set_text(title)
        self.episode_lbl.set_text(episode)
        self.episode_lbl.set_visible(bool(episode))
        self.sub_lbl.set_text(e.subtitle or "")
        self.sub_lbl.set_visible(bool(e.subtitle))
        self.chips.remove_all()
        facts = [f"{day(e.start)}  {hm(e.start)}–{hm(e.end)}", f"{e.duration // 60} min"]
        facts += [e.genre] if e.genre else []
        facts += list(e.access or [])
        facts += [f"FSK {e.min_age}"] if e.min_age else []
        for i, fact in enumerate([c.name] + facts):
            css = ["epg-chip"] + (["epg-chip-accent"] if i == 0 else [])
            self.chips.append(Gtk.Label(label=fact, halign=Gtk.Align.START, css_classes=css))
        if self._desc_open:
            self._toggle_description()
        self.desc_lbl.set_text(e.description or "")
        self.desc_lbl.set_visible(bool(e.description))
        self.more_btn.set_visible(False)
        GLib.idle_add(self._check_description)
        self._show_image(e.image)

    def _show_image(self, url):
        """Programme image (IT-6, 640 px wide), downloaded once into the cache."""
        self._image_url = url
        if not url:
            self.image.set_visible(False)
            return
        url = image_url(url)
        path = str(IMAGE_DIR / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".img"))
        if os.path.exists(path):
            self._set_image(path, self._image_url)
            return
        self.image.set_visible(False)
        want = self._image_url

        def work():
            from ..dvbi import DVBIError, fetch
            try:
                data = fetch(url).data
                os.makedirs(IMAGE_DIR, exist_ok=True)
                with open(path + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(path + ".tmp", path)
            except (DVBIError, OSError):
                return
            GLib.idle_add(self._set_image, path, want)
        threading.Thread(target=work, daemon=True).start()

    def _set_image(self, path, want):
        if want != self._image_url:
            return False
        try:
            self.image.set_paintable(Gdk.Texture.new_from_filename(path))
            self.image.set_visible(True)
        except GLib.Error:
            self.image.set_visible(False)
        return False

    # -- timeline drawing -------------------------------------------------------
    def _draw_timeline(self, _area, cr, w, h):
        fg = self.get_color()
        cr.set_font_size(12)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
        for i in range(0, 24 * 2):
            x = NAME_W + i * 30 * PX_PER_MIN
            cr.move_to(x + 3, 15)
            cr.show_text(hm(self._t0 + i * 1800))
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            cr.rectangle(x, 20, 1, h)
            cr.fill()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
        nowx = NAME_W + (time.time() - self._t0) / 60 * PX_PER_MIN
        for r, (c, evs) in enumerate(self._timeline_rows):
            y = 24 + r * ROW_H
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
            cr.move_to(4, y + 21)
            cr.show_text(c.name[:20])
            for e in evs:
                x0 = NAME_W + (e.start - self._t0) / 60 * PX_PER_MIN
                x1 = NAME_W + (e.end - self._t0) / 60 * PX_PER_MIN
                if x1 < NAME_W:
                    continue
                x0 = max(x0, NAME_W)
                sel = self.selected_event and self.selected_event[1] is e
                cr.set_source_rgba(0.21, 0.52, 0.89, 0.85 if sel else 0.35 if e.is_now() else 0.18)
                cr.rectangle(x0 + 1, y + 2, max(1, x1 - x0 - 2), ROW_H - 4)
                cr.fill()
                cr.save()
                cr.rectangle(x0 + 1, y, max(1, x1 - x0 - 6), ROW_H)
                cr.clip()
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 1)
                cr.move_to(x0 + 5, y + 21)
                cr.show_text(e.title)
                cr.restore()
        cr.set_source_rgba(0.88, 0.1, 0.14, 0.9)
        cr.rectangle(nowx, 20, 2, h)
        cr.fill()

    def _timeline_click(self, gesture, n, x, y):
        r = int((y - 24) // ROW_H)
        if not 0 <= r < len(self._timeline_rows) or x < NAME_W:
            return
        c, evs = self._timeline_rows[r]
        t = self._t0 + (x - NAME_W) / PX_PER_MIN * 60
        ev = next((e for e in evs if e.start <= t < e.end), None)
        if ev:
            self._select((c, ev))
            self.tl.queue_draw()
            if n == 2:
                self.on_record()

    # -- actions ----------------------------------------------------------------
    def on_watch(self):
        if self.selected_event:
            c, e = self.selected_event
            from .. import catchup
            if e.end <= time.time() and catchup.available(c, e.start, e.end):
                self.app.window.play_catchup(c, e)       # over: from the archive
            else:
                self.app.window.zap(c)

    def on_watch_from_start(self):
        """Catch-up: the programme from its start, from the provider's archive."""
        if self.selected_event:
            c, e = self.selected_event
            from .. import catchup
            if catchup.available(c, e.start, e.end):
                self.app.window.play_catchup(c, e)

    def on_record(self):
        if self.selected_event:
            c, e = self.selected_event
            self.app.timers.add_from_event(c, e)

    def on_record_options(self):
        """A timer for the programme, opened in the timer window to change the
        lead and follow time, formats and tracks before it records."""
        if self.selected_event:
            c, e = self.selected_event
            t = self.app.timers.add_from_event(c, e, edit=False)
            self.app.show_timers(t)

    def on_series(self):
        if self.selected_event:
            c, e = self.selected_event
            self.app.timers.add_series(e.title, c.key)
            self.app.window.set_status(_("Series recording: {title}").format(title=e.title))

    def on_remind(self):
        if not self.selected_event:
            return
        c, e = self.selected_event
        rem = self.app.reminders
        if rem.has(c.key, e.start):
            rem.remove(c.key, e.start)
            self.app.window.set_status(_("Reminder removed: {title}").format(title=e.title))
        elif e.start <= time.time():
            self.app.window.set_status(_("{title} has already started").format(title=e.title))
        else:
            rem.add(c, e)
            self.app.window.set_status(_("Reminder set: {title}, {time}").format(
                title=e.title, time=time.strftime("%a %H:%M", time.localtime(e.start))))
        self._update_remind_button()

    def _update_remind_button(self):
        sel = self.selected_event
        from .. import catchup
        self.start_action.set_enabled(bool(sel and catchup.available(sel[0], sel[1].start,
                                                                      sel[1].end)))
        on = bool(sel and self.app.reminders.has(sel[0].key, sel[1].start))
        self.remind_btn.label.set_text(_("Remove Reminder") if on else _("Remind Me"))

    def on_web(self):
        if self.selected_event:
            self.app.web_search(self.selected_event[1].title, self)


def _split_episode(title):
    """"Panda, Gorilla & Co. (315)" -> ("Panda, Gorilla & Co.", "(315)")."""
    m = re.match(r"^(.*\S)\s+(\(\d+\))$", title or "")
    return (m.group(1), m.group(2)) if m else (title or "", "")


class _Thumb(Gtk.Picture):
    """The programme picture at a fixed 16:9 size, however large the image."""
    W, H = 200, 112

    def do_measure(self, orientation, _for_size):
        n = self.W if orientation == Gtk.Orientation.HORIZONTAL else self.H
        return n, n, -1, -1


def _icon_button(label, icon):
    b = Gtk.Button()
    box = Gtk.Box(spacing=6)
    box.append(Gtk.Image(icon_name=icon))
    b.label = Gtk.Label(label=label)
    box.append(b.label)
    b.set_child(box)
    return b


CSS = """
.epg-title { font-size: 1.4em; font-weight: bold; }
.epg-sub { font-size: 1.05em; }
.epg-chip { padding: 2px 10px; border-radius: 999px; background: alpha(currentColor, 0.10);
  font-size: 0.92em; }
.epg-chip-accent { background: alpha(@theme_selected_bg_color, 0.45); }
.epg-thumb { border-radius: 8px; }
.epg-more { margin-left: -10px; }
"""
_css_done = False


def _install_css():
    global _css_done
    if _css_done:
        return
    prov = Gtk.CssProvider()
    if hasattr(prov, "load_from_string"):
        prov.load_from_string(CSS)
    else:
        prov.load_from_data(CSS.encode())
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_done = True
