# SPDX-License-Identifier: GPL-2.0-or-later
"""Favourites lists: name prompt and the window to manage the lists."""

from gi.repository import GLib, Gtk

from ..i18n import _


def ask_name(parent, title, initial, done, label=None):
    """A small window asking for a list name; calls ``done(name)`` on OK."""
    win = Gtk.Window(title=title, transient_for=parent, modal=True, resizable=False,
                     default_width=360)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=16,
                  margin_end=16, margin_top=16, margin_bottom=16)
    entry = Gtk.Entry(text=initial, activates_default=True)
    entry.update_property([Gtk.AccessibleProperty.LABEL], [_("Name")])
    box.append(Gtk.Label(label=label or _("Name of the favourites list"), xalign=0))
    box.append(entry)
    buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
    cancel = Gtk.Button(label=_("Cancel"))
    ok = Gtk.Button(label=_("OK"), css_classes=["suggested-action"])
    buttons.append(cancel)
    buttons.append(ok)
    box.append(buttons)
    win.set_child(box)
    win.set_default_widget(ok)

    def accept(*_a):
        name = entry.get_text().strip()
        if name:
            win.close()
            done(name)

    ok.connect("clicked", accept)
    cancel.connect("clicked", lambda *_a: win.close())
    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", lambda _c, kv, _k, _s: (win.close(), True)[1]
                 if kv == 0xff1b else False)           # Escape
    win.add_controller(keys)
    win.present()
    entry.grab_focus()
    entry.select_region(0, -1)
    return win


class FavouritesWindow(Gtk.Window):
    """Create, rename, delete and reorder the favourites lists. Changes are
    saved right away."""

    def __init__(self, app, parent):
        super().__init__(title=_("Favourites Lists"), transient_for=parent,
                         default_width=440, default_height=420)
        self.app = app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=14,
                      margin_end=14, margin_top=14, margin_bottom=14)
        box.append(Gtk.Label(
            label=_("Each list keeps its own order. Sort the channels of a list in the "
                    "channel list: pick the list, then drag the channels or use the "
                    "right-click menu."),
            xalign=0, wrap=True, css_classes=["lv-hint"]))
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", lambda *_a: self.on_rename())
        frame = Gtk.Frame(child=Gtk.ScrolledWindow(child=self.listbox, vexpand=True))
        box.append(frame)
        bar = Gtk.Box(spacing=4)
        for icon, tip, fn in (("list-add-symbolic", _("New list"), self.on_new),
                              ("document-edit-symbolic", _("Rename"), self.on_rename),
                              ("user-trash-symbolic", _("Delete list"), self.on_delete),
                              ("go-up-symbolic", _("Move up"), lambda: self.on_move(-1)),
                              ("go-down-symbolic", _("Move down"), lambda: self.on_move(1))):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.update_property([Gtk.AccessibleProperty.LABEL], [tip])
            b.connect("clicked", lambda _b, fn=fn: fn())
            bar.append(b)
        box.append(bar)
        self.set_child(box)
        self.refresh()

    @property
    def favs(self):
        return self.app.favs

    def refresh(self, select=0):
        self.listbox.remove_all()
        for i, name in enumerate(self.favs.names()):
            n = len(self.favs.channels(i))
            row = Gtk.Box(spacing=10, margin_start=8, margin_end=8, margin_top=6,
                          margin_bottom=6)
            row.append(Gtk.Image(icon_name="starred-symbolic", css_classes=["lv-fav"]))
            row.append(Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=3))
            row.append(Gtk.Label(label=_("{n} channels").format(n=n), css_classes=["lv-hint"]))
            self.listbox.append(row)
        r = self.listbox.get_row_at_index(select)
        if r is not None:
            self.listbox.select_row(r)

    def _selected(self):
        r = self.listbox.get_selected_row()
        return r.get_index() if r is not None else -1

    def _changed(self, select, remap=None):
        self.app.favourites_changed(remap)
        self.refresh(select)

    def on_new(self):
        def done(name):
            self._changed(self.favs.create(name))
        ask_name(self, _("New Favourites List"), _("New list"), done)

    def on_rename(self):
        i = self._selected()
        if not self.favs.valid(i):
            return

        def done(name):
            self.favs.rename(i, name)
            self._changed(i)
        ask_name(self, _("Rename Favourites List"), self.favs.names()[i], done)

    def on_delete(self):
        i = self._selected()
        if not self.favs.valid(i):
            return
        name = self.favs.names()[i]
        dlg = Gtk.AlertDialog(message=_("Delete the list “{name}”?").format(name=name),
                              detail=_("The channels stay in the channel list."),
                              buttons=[_("Cancel"), _("Delete")], cancel_button=0,
                              default_button=0)

        def done(d, res):
            try:
                if d.choose_finish(res) != 1:
                    return
            except GLib.Error:
                return
            n = len(self.favs)
            self.favs.delete(i)
            remap = {k: (None if k == i else k - 1 if k > i else k) for k in range(n)}
            self._changed(max(0, i - 1), remap)

        dlg.choose(self, None, done)

    def on_move(self, step):
        i = self._selected()
        if self.favs.valid(i):
            j = self.favs.move_list(i, step)
            self._changed(j, {i: j, j: i} if j != i else None)
