# SPDX-License-Identifier: GPL-2.0-or-later
"""Small GTK 4 helpers shared by the windows."""

import time

from gi.repository import Gio, GObject, Gtk


class Row(GObject.Object):
    """Wraps any Python object so it can live in a Gio.ListStore."""

    def __init__(self, data):
        super().__init__()
        self.data = data


def make_column_view(columns, on_activate=None):
    """columns: list of (title, fn(data) -> str, expand). Returns (view, store, selection)."""
    store = Gio.ListStore(item_type=Row)
    sel = Gtk.SingleSelection(model=store)
    view = Gtk.ColumnView(model=sel)
    view.set_show_column_separators(True)
    for title, fn, expand in columns:
        fac = Gtk.SignalListItemFactory()
        fac.connect("setup", lambda _f, li: li.set_child(Gtk.Label(xalign=0, ellipsize=3)))

        def bind(_f, li, fn=fn):
            li.get_child().set_text(fn(li.get_item().data))

        fac.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=fac)
        col.set_expand(expand)
        col.set_resizable(True)
        view.append_column(col)
    if on_activate:
        view.connect("activate", lambda _v, pos: on_activate(store.get_item(pos).data))
    return view, store, sel


def selected(sel):
    item = sel.get_selected_item()
    return item.data if item else None


def fill(store, items):
    store.remove_all()
    store.splice(0, 0, [Row(x) for x in items])


def hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def day(t):
    return time.strftime("%a %d.%m.", time.localtime(t))


def scrolled(child, h=True):
    sw = Gtk.ScrolledWindow(hexpand=h, vexpand=True)
    sw.set_child(child)
    return sw


def labeled(grid, row, text, widget):
    grid.attach(Gtk.Label(label=text, xalign=0), 0, row, 1, 1)
    grid.attach(widget, 1, row, 1, 1)
    widget.set_hexpand(True)
    return widget


def dropdown(strings, selected_index=0):
    d = Gtk.DropDown.new_from_strings(strings)
    d.set_selected(max(0, selected_index))
    return d


def message(parent, text, detail=""):
    dlg = Gtk.AlertDialog(message=text, detail=detail)
    dlg.show(parent)


def tool_window(parent, title, w=700, h=500):
    win = Gtk.Window(title=title, transient_for=parent, default_width=w, default_height=h)
    win.set_destroy_with_parent(True)
    return win
