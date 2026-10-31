"""Programme guide window: what's on now, per channel, timeline and search."""

import time

from gi.repository import Gtk, Pango

from .common import day, dropdown, fill, hm, make_column_view, scrolled, selected

ROW_H = 34
PX_PER_MIN = 4
NAME_W = 150


class EPGWindow(Gtk.Window):
    def __init__(self, app, parent, channel=None):
        super().__init__(title="Programme Guide", transient_for=parent,
                         default_width=1000, default_height=640)
        self.app = app
        self.parent = parent
        self.selected_event = None      # (channel, event)
        self.chans = [c for c in app.db.channels]

        nb = Gtk.Notebook()
        nb.append_page(self._now_page(), Gtk.Label(label="Now / Next"))
        nb.append_page(self._channel_page(channel), Gtk.Label(label="Per Channel"))
        nb.append_page(self._timeline_page(), Gtk.Label(label="TV Guide"))
        nb.append_page(self._search_page(), Gtk.Label(label="Search"))
        if channel:
            nb.set_current_page(1)

        self.details = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True,
                                 wrap_mode=Pango.WrapMode.WORD_CHAR)
        det = scrolled(self.details)
        det.set_size_request(-1, 130)
        bar = Gtk.Box(spacing=6)
        for label, fn in (("Watch", self.on_watch), ("Record", self.on_record),
                          ("Record Series", self.on_series), ("Search Web", self.on_web)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, fn=fn: fn())
            bar.append(b)
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=420)
        paned.set_start_child(nb)
        lower = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
        lower.append(bar)
        lower.append(det)
        paned.set_end_child(lower)
        self.set_child(paned)
        self.refresh()
        app.epg.listeners.append(self.refresh)
        self.connect("close-request", lambda *_: (app.epg.listeners.remove(self.refresh), False)[1])

    # -- pages ------------------------------------------------------------------
    def _now_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=6, margin_start=6, margin_top=6)
        self.at_dd = dropdown(["Now", "Next", "20:15", "22:00"])
        self.at_dd.connect("notify::selected", lambda *_: self.refresh())
        self.only_fav = Gtk.CheckButton(label="Only favourites")
        self.only_fav.connect("toggled", lambda *_: self.refresh())
        top.append(Gtk.Label(label="Show:"))
        top.append(self.at_dd)
        top.append(self.only_fav)
        box.append(top)
        cols = [("Channel", lambda r: r[0].name, False), ("Start", lambda r: hm(r[1].start), False),
                ("End", lambda r: hm(r[1].end), False), ("Title", lambda r: r[1].title, True),
                ("Genre", lambda r: r[1].genre, False)]
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
        cols = [("Day", lambda r: day(r[1].start), False), ("Start", lambda r: hm(r[1].start), False),
                ("Duration", lambda r: f"{r[1].duration // 60} min", False),
                ("Title", lambda r: r[1].title, True), ("Subheading", lambda r: r[1].subtitle, True)]
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
        return sw

    def _search_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=6, margin_start=6, margin_top=6)
        self.q = Gtk.SearchEntry(placeholder_text="Title or description", hexpand=True)
        self.q.connect("activate", lambda *_: self.do_search())
        self.q_desc = Gtk.CheckButton(label="Include description", active=True)
        self.q_case = Gtk.CheckButton(label="Case sensitive")
        go = Gtk.Button(label="Search")
        go.connect("clicked", lambda *_: self.do_search())
        for w in (self.q, self.q_desc, self.q_case, go):
            top.append(w)
        box.append(top)
        cols = [("Channel", lambda r: r[0].name, False), ("Day", lambda r: day(r[1].start), False),
                ("Start", lambda r: hm(r[1].start), False), ("Title", lambda r: r[1].title, True)]
        self.s_view, self.s_store, self.s_sel = make_column_view(cols, lambda r: self.on_record())
        self.s_sel.connect("notify::selected-item", lambda s, _p: self._select(selected(s)))
        box.append(scrolled(self.s_view))
        return box

    # -- data -------------------------------------------------------------------
    def _visible_channels(self):
        if self.only_fav.get_active():
            return [c for c in self.chans if c.favourite]
        return self.chans

    def refresh(self):
        epg = self.app.epg
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
            if ev:
                rows.append((c, ev))
        fill(self.now_store, rows)
        if self.chans:
            c = self.chans[self.ch_dd.get_selected()]
            fill(self.ch_store, [(c, e) for e in epg.for_key(c.epg_key)])
        self._timeline_rows = [(c, epg.for_key(c.epg_key)) for c in self._visible_channels()]
        self._timeline_rows = [r for r in self._timeline_rows if r[1]]
        self._t0 = (int(now) // 1800) * 1800
        self.tl.set_content_width(NAME_W + 24 * 60 * PX_PER_MIN)
        self.tl.set_content_height(max(1, len(self._timeline_rows)) * ROW_H + 24)
        self.tl.queue_draw()

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
        c, e = row
        self.details.set_markup(
            f"<b>{_esc(e.title)}</b>  {_esc(e.subtitle)}\n"
            f"{_esc(c.name)} · {day(e.start)} {hm(e.start)}–{hm(e.end)} · {e.duration // 60} min"
            f"{' · ' + _esc(e.genre) if e.genre else ''}\n\n{_esc(e.description)}")

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
            self.app.window.zap(self.selected_event[0])

    def on_record(self):
        if self.selected_event:
            c, e = self.selected_event
            self.app.timers.add_from_event(c, e)

    def on_series(self):
        if self.selected_event:
            c, e = self.selected_event
            self.app.timers.add_series(e.title, c.key)
            self.app.window.set_status(f"Series recording: {e.title}")

    def on_web(self):
        if self.selected_event:
            self.app.web_search(self.selected_event[1].title, self)


def _esc(s):
    from gi.repository import GLib
    return GLib.markup_escape_text(s or "")
