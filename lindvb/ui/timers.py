"""Timer recordings window (one-off, repeating and series timers)."""

import time
from datetime import datetime

from gi.repository import Gtk

from ..recorder import AFTER_ACTIONS, WEEKDAYS, Timer
from .common import day, dropdown, fill, hm, labeled, make_column_view, message, scrolled, selected


class TimersWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(title="Timer Recordings", transient_for=parent,
                         default_width=1180, default_height=620)
        self.app = app
        cols = [("Channel", lambda t: t.channel_name, False),
                ("Date", lambda t: day(t.start), False),
                ("Start", lambda t: hm(t.start), False), ("End", lambda t: hm(t.end), False),
                ("Name", lambda t: t.title, True),
                ("Repeat", lambda t: " ".join(WEEKDAYS[d] for d in t.days) or
                 ("Series" if t.series else "Once"), False),
                ("After", lambda t: t.after, False),
                ("Status", lambda t: t.status if t.enabled else "Disabled", False)]
        self.view, self.store, self.sel = make_column_view(cols)
        self.sel.connect("notify::selected-item", lambda *_: self._load(selected(self.sel)))

        rcols = [("Series title", lambda r: r.title, True),
                 ("Channel", lambda r: (app.db.by_key(r.channel_key).name
                                        if app.db.by_key(r.channel_key) else "Any"), False)]
        self.rview, self.rstore, self.rsel = make_column_view(rcols)

        # editor
        g = Gtk.Grid(row_spacing=6, column_spacing=8, margin_start=10, margin_end=10,
                     margin_top=10, margin_bottom=10)
        self.chans = list(app.db.channels)
        self.ch = labeled(g, 0, "Channel", Gtk.DropDown.new_from_strings([c.name for c in self.chans] or ["-"]))
        self.ch.set_enable_search(True)
        self.ch.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.title_e = labeled(g, 1, "Name", Gtk.Entry())
        self.date_e = labeled(g, 2, "Date (YYYY-MM-DD)", Gtk.Entry())
        self.start_e = labeled(g, 3, "Start (HH:MM)", Gtk.Entry())
        self.end_e = labeled(g, 4, "End (HH:MM)", Gtk.Entry())
        days = Gtk.Box(spacing=2)
        self.day_btns = [Gtk.ToggleButton(label=d) for d in WEEKDAYS]
        for b in self.day_btns:
            days.append(b)
        labeled(g, 5, "Repeat on", days)
        self.after = labeled(g, 6, "After recording", dropdown(AFTER_ACTIONS))
        self.margins = labeled(g, 7, "Add lead/follow time", Gtk.Switch(halign=Gtk.Align.START, active=True))
        btns = Gtk.Box(spacing=6)
        for label, fn in (("New", self.on_new), ("Add", self.on_add), ("Apply", self.on_apply),
                          ("Delete", self.on_delete), ("Enable/Disable", self.on_toggle)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, fn=fn: fn())
            btns.append(b)
        g.attach(btns, 0, 8, 2, 1)
        rdel = Gtk.Button(label="Delete series rule", halign=Gtk.Align.START)
        rdel.connect("clicked", lambda *_: self.on_delete_rule())

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.append(g)
        right.append(Gtk.Label(label="<b>Series recordings</b>", use_markup=True, xalign=0, margin_start=10))
        right.append(scrolled(self.rview))
        right.append(rdel)
        paned = Gtk.Paned(position=640)
        paned.set_start_child(scrolled(self.view))
        paned.set_end_child(right)
        self.set_child(paned)
        self.on_new()
        self.refresh()
        self._hid = app.timers.connect("changed", lambda *_: self.refresh())
        self.connect("close-request", lambda *_: (app.timers.disconnect(self._hid), False)[1])

    def refresh(self):
        fill(self.store, self.app.timers.timers)
        fill(self.rstore, self.app.timers.rules)

    def _load(self, t):
        if not t:
            return
        idx = next((i for i, c in enumerate(self.chans) if c.key == t.channel_key), 0)
        self.ch.set_selected(idx)
        self.title_e.set_text(t.title)
        self.date_e.set_text(datetime.fromtimestamp(t.start).strftime("%Y-%m-%d"))
        self.start_e.set_text(hm(t.start))
        self.end_e.set_text(hm(t.end))
        for i, b in enumerate(self.day_btns):
            b.set_active(i in t.days)
        self.after.set_selected(AFTER_ACTIONS.index(t.after) if t.after in AFTER_ACTIONS else 0)
        self.margins.set_active(False)

    def on_new(self):
        now = time.time()
        self.title_e.set_text("")
        self.date_e.set_text(datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
        self.start_e.set_text(hm(now + 300))
        self.end_e.set_text(hm(now + 3900))
        for b in self.day_btns:
            b.set_active(False)
        self.margins.set_active(True)

    def _read(self):
        try:
            d = self.date_e.get_text().strip()
            start = datetime.strptime(f"{d} {self.start_e.get_text().strip()}", "%Y-%m-%d %H:%M").timestamp()
            end = datetime.strptime(f"{d} {self.end_e.get_text().strip()}", "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            message(self, "Invalid date or time")
            return None
        if end <= start:
            end += 86400
        if not self.chans:
            return None
        c = self.chans[self.ch.get_selected()]
        return Timer(c.key, c.name, self.title_e.get_text().strip() or c.name, int(start), int(end),
                     days=[i for i, b in enumerate(self.day_btns) if b.get_active()],
                     after=AFTER_ACTIONS[self.after.get_selected()])

    def on_add(self):
        t = self._read()
        if t:
            self.app.timers.add(t, with_margins=self.margins.get_active())

    def on_apply(self):
        old = selected(self.sel)
        t = self._read()
        if old and t:
            t.id = old.id
            self.app.timers.timers = [t if x.id == old.id else x for x in self.app.timers.timers]
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

    def on_delete_rule(self):
        r = selected(self.rsel)
        if r:
            self.app.timers.rules = [x for x in self.app.timers.rules if x.id != r.id]
            self.app.timers.save()
