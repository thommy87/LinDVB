# SPDX-License-Identifier: GPL-2.0-or-later
"""The window that exports the channel list: which channels, which format,
and how many of them the format can hold, before the file is chosen."""

import re
from pathlib import Path

from gi.repository import GLib, Gtk

from ..channels import EXPORT_FORMATS, export_channels, export_fits
from ..i18n import _


def format_labels():
    return {"json": _("LinDVB channel file (.json): everything, with the favourites lists"),
            "m3u": _("M3U playlist (.m3u): internet channels and DVB-I streams"),
            "dvbv5": _("dvbv5 channel file (.conf): tuner channels"),
            "csv": _("CSV table (.csv): every channel")}


def file_name(name, ext):
    """A file name made of *name* (a list name, say) and *ext*."""
    stem = re.sub(r"[^\w .-]+", "_", name).strip(" ._") or "channels"
    return stem + ext


class ExportWindow(Gtk.Window):
    """Export the channel list. *shown* are the channels the channel list
    shows right now (None when it shows the recordings)."""

    def __init__(self, app, parent, shown=None):
        super().__init__(title=_("Export Channel List"), transient_for=parent, modal=True,
                         default_width=480, resizable=False)
        self.app = app
        self.parent_win = parent
        favs = app.favs
        chans = app.db.channels
        # (label, channels, favourites lists for the LinDVB file, file name)
        self.scopes = [(_("All channels"), list(chans), favs.lists, "channels"),
                       (_("TV channels only"), [c for c in chans if not c.radio],
                        favs.lists, "tv-channels"),
                       (_("Radio stations only"), [c for c in chans if c.radio],
                        favs.lists, "radio-stations")]
        if shown is not None:
            self.scopes.append((_("The channels shown in the list ({n})").format(n=len(shown)),
                                list(shown), favs.lists, "channels"))
        for i, name in enumerate(favs.names()):
            self.scopes.append(("★ " + name, favs.channels(i), [favs.lists[i]], name))
        self.formats = [key for key, _ext in EXPORT_FORMATS]
        self.ext = dict(EXPORT_FORMATS)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=16,
                      margin_end=16, margin_top=16, margin_bottom=16)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.scope_dd = Gtk.DropDown.new_from_strings([s[0] for s in self.scopes])
        self.scope_dd.set_hexpand(True)
        labels = format_labels()
        self.format_dd = Gtk.DropDown.new_from_strings([labels[k] for k in self.formats])
        self.format_dd.set_hexpand(True)
        last = app.settings.get("export_format", "json")
        self.format_dd.set_selected(self.formats.index(last) if last in self.formats else 0)
        for row, (text, widget) in enumerate(((_("Channels"), self.scope_dd),
                                              (_("Format"), self.format_dd))):
            label = Gtk.Label(label=text, xalign=0)
            grid.attach(label, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
            widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
        box.append(grid)
        self.info = Gtk.Label(xalign=0, wrap=True, css_classes=["lv-hint"])
        box.append(self.info)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label=_("Cancel"))
        self.save_btn = Gtk.Button(label=_("Save…"), css_classes=["suggested-action"])
        buttons.append(cancel)
        buttons.append(self.save_btn)
        box.append(buttons)
        self.set_child(box)
        self.set_default_widget(self.save_btn)
        cancel.connect("clicked", lambda *_a: self.close())
        self.save_btn.connect("clicked", lambda *_a: self.on_save())
        self.scope_dd.connect("notify::selected", lambda *_a: self.update())
        self.format_dd.connect("notify::selected", lambda *_a: self.update())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, _k, _s: (self.close(), True)[1]
                     if kv == 0xff1b else False)           # Escape
        self.add_controller(keys)
        self.update()

    def choice(self):
        s = self.scopes[min(self.scope_dd.get_selected(), len(self.scopes) - 1)]
        fmt = self.formats[min(self.format_dd.get_selected(), len(self.formats) - 1)]
        return s, fmt

    def update(self):
        """Say how many of the chosen channels go into the file."""
        (_label, chans, _lists, _name), fmt = self.choice()
        total = len(chans)
        n = sum(1 for c in chans if export_fits(c, fmt))
        if not total:
            text = _("There are no channels to export.")
        elif not n:
            text = (_("None of these {total} channels can be stored in this format; "
                      "internet channels go into M3U, tuner channels into dvbv5, "
                      "every channel into CSV or the LinDVB channel file.")
                    .format(total=total))
        elif n < total:
            text = (_("{n} of {total} channels will be saved; the other {skipped} cannot be "
                      "stored in this format.").format(n=n, total=total, skipped=total - n))
        else:
            text = _("{n} channels will be saved.").format(n=n)
        self.info.set_text(text)
        self.save_btn.set_sensitive(n > 0)

    def on_save(self):
        (_label, _chans, _lists, name), fmt = self.choice()
        ext = self.ext[fmt]
        dlg = Gtk.FileDialog(title=_("Export Channel List"), initial_name=file_name(name, ext))
        dlg.save(self, None, self._save_done)

    def _save_done(self, dlg, res):
        try:
            path = dlg.save_finish(res).get_path()
        except GLib.Error:
            return
        (_label, chans, lists, _name), fmt = self.choice()
        ext = self.ext[fmt]
        if not Path(path).suffix:
            path += ext
        text, n = export_channels(chans, fmt, lists)
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as e:
            from .common import message
            message(self, _("Could not save {name}").format(name=Path(path).name), str(e))
            return
        self.app.settings["export_format"] = fmt
        self.parent_win.set_status(_("{n} channels exported to {path}").format(n=n, path=path))
        self.close()
