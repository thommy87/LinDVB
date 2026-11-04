# SPDX-License-Identifier: GPL-2.0-or-later
"""The IPTV section of Preferences › Internet: import M3U playlists and other
channel lists with internet streams, from files or from a web address."""

import threading
import urllib.request

from gi.repository import Gio, GLib, Gtk

from ..channels import download_name, import_data
from .common import message
from ..i18n import _

PATTERNS = ("*.m3u", "*.m3u8", "*.ini", "*.csv", "*.conf")


def _filters():
    store = Gio.ListStore.new(Gtk.FileFilter)
    lists = Gtk.FileFilter(name=_("IPTV channel lists (M3U, DVBViewer .ini, CSV)"))
    for p in PATTERNS:
        lists.add_pattern(p)
        lists.add_pattern(p.upper())
    store.append(lists)
    every = Gtk.FileFilter(name=_("All files"))
    every.add_pattern("*")
    store.append(every)
    return store


class IPTVPanel(Gtk.Box):
    def __init__(self, app, row):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-group"])
        self.app = app
        self.append(Gtk.Label(
            label=_("Import one or more M3U playlists or other channel lists with internet "
                  "streams. Channels that are already in your list are updated, the others "
                  "are added."),
            xalign=0, wrap=True, css_classes=["lv-hint"], margin_top=8, margin_bottom=4))
        files = Gtk.Button(label=_("Import Files…"))
        files.connect("clicked", lambda *_: self._pick_files())
        row(self, _("From files"), files,
            _("M3U / M3U8, DVBViewer .ini, CSV or dvbv5. Several files can be chosen at once."))
        url_box = Gtk.Box(spacing=6)
        self.url = Gtk.Entry(placeholder_text="https://…/playlist.m3u", width_chars=28,
                             input_purpose=Gtk.InputPurpose.URL)
        self.url.connect("activate", lambda *_: self._download())
        self.url_btn = Gtk.Button(label=_("Import"))
        self.url_btn.connect("clicked", lambda *_: self._download())
        url_box.append(self.url)
        url_box.append(self.url_btn)
        row(self, _("From a web address"), url_box,
            _("The address of an M3U playlist. Import it again to update the channels."))
        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["lv-hint"],
                                margin_top=4, margin_bottom=8)
        self.append(self.status)
        self._count()

    def _count(self, text=""):
        n = sum(1 for c in self.app.db.channels if c.is_iptv and not c.dvbi_id)
        count = _("IPTV channels in your channel list: {n}").format(n=n)
        self.status.set_text(f"{text}\n{count}" if text else count)

    # ------------------------------------------------------------------ files
    def _pick_files(self):
        dlg = Gtk.FileDialog(title=_("Import IPTV channel lists"), filters=_filters())
        dlg.open_multiple(self.get_root(), None, self._files_chosen)

    def _files_chosen(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
        win = self.app.window
        if win is not None and paths:
            self._count(win.import_files(paths, parent=self.get_root()))

    # ------------------------------------------------------------------ download
    def _download(self):
        url = self.url.get_text().strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        self.url_btn.set_sensitive(False)
        self.status.set_text(_("Downloading {url}…").format(url=url))

        def work():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "LinDVB"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    chans = import_data(r.read(), download_name(url))
                GLib.idle_add(self._downloaded, url, chans, "")
            except (OSError, ValueError) as e:
                GLib.idle_add(self._downloaded, url, None, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _downloaded(self, url, chans, error):
        self.url_btn.set_sensitive(True)
        if error or not chans:
            self._count()
            message(self.get_root(), _("Could not import {name}").format(name=url),
                    error or _("The address has no channels."))
            return False
        db = self.app.db
        a, u = db.merge(chans)
        db.save()
        text = _("Imported {total_a} new and {total_u} updated channels").format(
            total_a=a, total_u=u)
        if self.app.window is not None:
            self.app.window.reload_channels()
            self.app.window.set_status(text)
        self.url.set_text("")
        self._count(text)
        return False
