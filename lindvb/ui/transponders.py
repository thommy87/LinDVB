# SPDX-License-Identifier: GPL-2.0-or-later
"""The transponder editor of the channel search: the transponders of one
satellite position, to search only some of them, or to change the list.

The list itself is kept by lindvb.satedit.
"""

from gi.repository import Gio, GObject, Gtk

from .. import satedit
from ..scan import Transponder
from .common import dropdown, labeled, message
from ..i18n import _

POL_LABELS = {"h": _("Horizontal"), "v": _("Vertical"), "l": _("Left (circular)"),
              "r": _("Right (circular)")}
SYSTEM_LABELS = {"dvb-s": "DVB-S", "dvb-s2": "DVB-S2"}
MOD_LABELS = {"auto": _("Auto"), "qpsk": "QPSK", "8psk": "8PSK", "16apsk": "16APSK",
              "32apsk": "32APSK"}


def fec_label(fec):
    return _("Auto") if fec in ("", "auto") else fec


class _Item(GObject.Object):
    """One transponder in the list, with its tick."""

    def __init__(self, tp, on=True):
        super().__init__()
        self.tp, self.on = tp, on


class TransponderWindow(Gtk.Window):
    """*tplist*: the position's whole list (lindvb.scan.TransponderList).
    *only*: the keys (lindvb.satedit.key) ticked before, None for all.
    *on_done(edited, only)*: edited is the new list or None when unchanged
    (reset: the string "reset"); only is a set of keys or None for all."""

    def __init__(self, parent, position, name, tplist, only, edited_before, on_done):
        super().__init__(title=_("Transponders of {position}").format(position=name),
                         transient_for=parent, modal=True, default_width=640,
                         default_height=520)
        self.position, self.on_done = position, on_done
        self.changed = False
        self._handlers = {}
        self.store = Gio.ListStore(item_type=_Item)
        for t in tplist.transponders:
            self.store.append(_Item(t, only is None or satedit.key(t) in only))

        self.sel = Gtk.SingleSelection(model=self.store)
        view = Gtk.ColumnView(model=self.sel, show_column_separators=True, vexpand=True)
        view.connect("activate", lambda _v, pos: self._edit(pos))
        tick = Gtk.SignalListItemFactory()
        tick.connect("setup", lambda _f, li: li.set_child(Gtk.CheckButton()))
        tick.connect("bind", self._bind_tick)
        tick.connect("unbind", self._unbind_tick)
        col = Gtk.ColumnViewColumn(title=_("Search"), factory=tick)
        view.append_column(col)
        for title, fn, expand in (
                (_("Frequency"), lambda t: f"{t.frequency / 1000:.0f} MHz", True),
                (_("Polarisation"), lambda t: POL_LABELS.get(t.polarity, t.polarity.upper()), True),
                (_("Symbol rate"), lambda t: f"{t.symbol_rate} kBd", True),
                (_("System"), lambda t: SYSTEM_LABELS.get(t.delsys, t.delsys), False),
                (_("Modulation"), lambda t: MOD_LABELS.get(t.modulation, t.modulation), False),
                ("FEC", lambda t: fec_label(t.fec), False)):
            fac = Gtk.SignalListItemFactory()
            fac.connect("setup", lambda _f, li: li.set_child(Gtk.Label(xalign=0)))
            fac.connect("bind", lambda _f, li, fn=fn: li.get_child().set_text(fn(li.get_item().tp)))
            c = Gtk.ColumnViewColumn(title=title, factory=fac)
            c.set_expand(expand)
            view.append_column(c)
        sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True, child=view)

        tools = Gtk.Box(spacing=6)
        for label, cb in ((_("Add…"), lambda *_a: self._edit(None)),
                          (_("Edit…"), lambda *_a: self._edit(self.sel.get_selected())),
                          (_("Remove"), lambda *_a: self._remove())):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            tools.append(b)
        tools.append(Gtk.Box(hexpand=True))
        for label, on in ((_("Tick all"), True), (_("Tick none"), False)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda *_a, on=on: self._tick_all(on))
            tools.append(b)

        self.note = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        bar = Gtk.Box(spacing=6)
        self.reset_btn = Gtk.Button(label=_("Back to the lists"), visible=edited_before,
                                    tooltip_text=_("Forget the changes to this position and "
                                                   "use the transponder lists again"))
        self.reset_btn.connect("clicked", lambda *_a: self._reset())
        bar.append(self.reset_btn)
        bar.append(Gtk.Box(hexpand=True))
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_a: self.close())
        ok = Gtk.Button(label=_("Apply"), css_classes=["suggested-action"])
        ok.connect("clicked", lambda *_a: self._apply())
        bar.append(cancel)
        bar.append(ok)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                      margin_end=12, margin_top=12, margin_bottom=12)
        for w in (sw, tools, self.note, bar):
            box.append(w)
        self.set_child(box)
        self._update_note()

    # -- ticks ---------------------------------------------------------------------

    def _bind_tick(self, _f, li):
        item, chk = li.get_item(), li.get_child()
        chk.set_active(item.on)
        self._handlers[chk] = chk.connect("toggled",
                                          lambda b: self._toggled(item, b.get_active()))

    def _unbind_tick(self, _f, li):
        chk = li.get_child()
        h = self._handlers.pop(chk, None)
        if h:
            chk.disconnect(h)

    def _toggled(self, item, on):
        item.on = on
        self._update_note()

    def _tick_all(self, on):
        for i in range(self.store.get_n_items()):
            self.store.get_item(i).on = on
        self.store.items_changed(0, self.store.get_n_items(), self.store.get_n_items())
        self._update_note()

    def _items(self):
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    def _update_note(self):
        items = self._items()
        n_on = sum(1 for it in items if it.on)
        if n_on == len(items):
            text = _("All {n} transponders are searched.").format(n=len(items))
        else:
            text = _("{on} of {n} transponders are searched. A search of single transponders "
                     "does not add the ones the network announces (NIT).").format(
                on=n_on, n=len(items))
        if self.changed:
            text += " " + _("The changed list is kept for this position.")
        self.note.set_text(text)

    # -- changing the list -----------------------------------------------------------

    def _edit(self, pos):
        if pos is not None and pos >= self.store.get_n_items():
            return
        item = self.store.get_item(pos) if pos is not None else None
        TransponderEditDialog(self, item.tp if item else None,
                              lambda t: self._edited(item, t)).present()

    def _edited(self, item, t):
        dup = next((it for it in self._items() if it is not item and
                    satedit.key(it.tp) == satedit.key(t)), None)
        if dup is not None:
            message(self, _("This transponder is already in the list."))
            return False
        if item is None:
            items = self._items() + [_Item(t)]
        else:
            item.tp = t
            items = self._items()
        items.sort(key=lambda it: (it.tp.frequency, it.tp.polarity))
        self.store.splice(0, self.store.get_n_items(), items)
        for i, it in enumerate(items):
            if it.tp is t:
                self.sel.set_selected(i)
        self.changed = True
        self._update_note()
        return True

    def _remove(self):
        pos = self.sel.get_selected()
        if pos >= self.store.get_n_items():
            return
        self.store.remove(pos)
        self.changed = True
        self._update_note()

    def _reset(self):
        self.on_done("reset", None)
        self.close()

    def _apply(self):
        items = self._items()
        if not any(it.on for it in items):
            message(self, _("Tick the transponders to search."))
            return
        if self.changed:
            try:
                satedit.save(self.position, [it.tp for it in items])
            except OSError as e:
                message(self, _("The list could not be saved."), str(e))
                return
        only = None if all(it.on for it in items) else {satedit.key(it.tp) for it in items if it.on}
        self.on_done([it.tp for it in items] if self.changed else None, only)
        self.close()


class TransponderEditDialog(Gtk.Window):
    """Add or change one transponder; *on_ok(Transponder)* returns False to stay open."""

    def __init__(self, parent, tp, on_ok):
        super().__init__(title=_("Edit transponder") if tp else _("Add transponder"),
                         transient_for=parent, modal=True, resizable=False)
        self.on_ok = on_ok
        tp = tp or Transponder("dvb-s2", 11494000, "h", 22000, "8psk", "auto")
        g = Gtk.Grid(row_spacing=6, column_spacing=8, margin_start=12, margin_end=12,
                     margin_top=12)
        lo, hi = satedit.FREQ_RANGE
        self.freq = labeled(g, 0, _("Frequency (MHz)"), Gtk.SpinButton.new_with_range(
            lo // 1000, hi // 1000, 1))
        self.freq.set_value(tp.frequency // 1000)
        self.pol = labeled(g, 1, _("Polarisation"), dropdown(
            [POL_LABELS[p] for p in satedit.POLARITIES],
            satedit.POLARITIES.index(tp.polarity) if tp.polarity in satedit.POLARITIES else 0))
        lo, hi = satedit.SR_RANGE
        self.sr = labeled(g, 2, _("Symbol rate (kBd)"), Gtk.SpinButton.new_with_range(lo, hi, 1))
        self.sr.set_value(tp.symbol_rate or 27500)
        self.sys = labeled(g, 3, _("System"), dropdown(
            [SYSTEM_LABELS[s] for s in satedit.SAT_SYSTEMS],
            satedit.SAT_SYSTEMS.index(tp.delsys) if tp.delsys in satedit.SAT_SYSTEMS else 0))
        self.mod = labeled(g, 4, _("Modulation"), dropdown(
            [MOD_LABELS[m] for m in satedit.MODULATIONS],
            satedit.MODULATIONS.index(tp.modulation) if tp.modulation in satedit.MODULATIONS else 0))
        self.fec = labeled(g, 5, "FEC", dropdown(
            [fec_label(f) for f in satedit.FECS],
            satedit.FECS.index(tp.fec) if tp.fec in satedit.FECS else 0))
        self.err = Gtk.Label(xalign=0, wrap=True, css_classes=["error"], visible=False)
        g.attach(self.err, 0, 6, 2, 1)
        bar = Gtk.Box(spacing=6, halign=Gtk.Align.END, margin_start=12, margin_end=12,
                      margin_top=6, margin_bottom=12)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_a: self.close())
        ok = Gtk.Button(label=_("OK"), css_classes=["suggested-action"])
        ok.connect("clicked", lambda *_a: self._ok())
        bar.append(cancel)
        bar.append(ok)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(g)
        box.append(bar)
        self.set_child(box)

    def value(self):
        return Transponder(satedit.SAT_SYSTEMS[self.sys.get_selected()],
                           int(self.freq.get_value()) * 1000,
                           satedit.POLARITIES[self.pol.get_selected()],
                           int(self.sr.get_value()),
                           satedit.MODULATIONS[self.mod.get_selected()],
                           satedit.FECS[self.fec.get_selected()])

    def _ok(self):
        t = self.value()
        if satedit.check(t) == "system":
            self.err.set_text(_("DVB-S only uses QPSK; choose DVB-S2 for 8PSK, 16APSK "
                                "and 32APSK."))
            self.err.set_visible(True)
            return
        if satedit.check(t):
            self.err.set_text(_("This transponder is not valid."))
            self.err.set_visible(True)
            return
        if self.on_ok(t) is not False:
            self.close()
