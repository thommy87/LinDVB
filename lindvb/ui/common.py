# SPDX-License-Identifier: GPL-2.0-or-later
"""Small GTK 4 helpers shared by the windows."""

import time

from gi.repository import Gio, GObject, Gtk


class Row(GObject.Object):
    """Wraps any Python object so it can live in a Gio.ListStore."""

    def __init__(self, data):
        super().__init__()
        self.data = data


def make_column_view(columns, on_activate=None, widths=None):
    """columns: list of (title, fn(data) -> str, expand[, icons]). Returns (view, store, selection).

    icons: optional fn(data) -> [(icon name, tooltip)], shown as symbols after the text.

    widths: optional fixed start width per column (None = size to content).
    A fixed width keeps the columns from jumping while scrolling, because
    otherwise GTK sizes them to the rows that happen to be visible."""
    store = Gio.ListStore(item_type=Row)
    sel = Gtk.SingleSelection(model=store)
    view = Gtk.ColumnView(model=sel)
    view.set_show_column_separators(True)
    for i, (title, fn, expand, *rest) in enumerate(columns):
        icons = rest[0] if rest else None
        fac = Gtk.SignalListItemFactory()
        if icons:
            def setup(_f, li):
                box = Gtk.Box(spacing=6)
                box.append(Gtk.Label(xalign=0, ellipsize=3))
                box.append(Gtk.Box(spacing=4, valign=Gtk.Align.CENTER))
                li.set_child(box)
            fac.connect("setup", setup)
        else:
            fac.connect("setup", lambda _f, li: li.set_child(Gtk.Label(xalign=0, ellipsize=3)))

        def bind(_f, li, fn=fn, icons=icons):
            data = li.get_item().data
            if not icons:
                li.get_child().set_text(fn(data))
                return
            lbl, marks = li.get_child().get_first_child(), li.get_child().get_last_child()
            lbl.set_text(fn(data))
            while marks.get_first_child():
                marks.remove(marks.get_first_child())
            for name, tip in icons(data):
                marks.append(Gtk.Image(icon_name=name, pixel_size=16, tooltip_text=tip))

        fac.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=fac)
        col.set_expand(expand)
        col.set_resizable(True)
        if widths and i < len(widths) and widths[i]:
            col.set_fixed_width(int(widths[i]))
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
