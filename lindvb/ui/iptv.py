# SPDX-License-Identifier: GPL-2.0-or-later
"""The IPTV section of Preferences › Internet: import M3U playlists and other
channel lists with internet streams, from files, from a web address or with
an Xtream Codes login; the imported lists can be updated and removed."""

import os
import threading
import time
import urllib.request

from gi.repository import Gio, GLib, Gtk

from .. import iptvsources
from ..channels import download_name, import_data, import_file
from ..config import CACHE_DIR
from .common import dropdown, fill, make_column_view, message, selected
from .favourites import ask_name
from ..i18n import _

PATTERNS = ("*.m3u", "*.m3u8", "*.m3uplus", "*.m3u8plus", "*.m3u_plus", "*.m3u8_plus",
            "*.ini", "*.csv", "*.conf")
KINDS = {"file": _("File"), "url": _("Web address"), "xtream": "Xtream Codes"}


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


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "LinDVB"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return import_data(r.read(), download_name(url), source=url)


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
            _("The address of an M3U playlist, or of a single HLS stream (.m3u8)."))

        # Xtream Codes: server, user name and password give the M3U address
        xt = Gtk.Box(spacing=6)
        self.xt_server = Gtk.Entry(placeholder_text=_("Server"), width_chars=16,
                                   input_purpose=Gtk.InputPurpose.URL)
        self.xt_user = Gtk.Entry(placeholder_text=_("User name"), width_chars=10)
        self.xt_pass = Gtk.PasswordEntry(placeholder_text=_("Password"), show_peek_icon=True)
        self.xt_pass.set_size_request(110, -1)
        self.xt_output = dropdown(["MPEG-TS", "HLS (m3u8)"])
        self.xt_output.set_tooltip_text(
            _("The stream format the server delivers. Choose HLS when the channels "
              "do not play as MPEG-TS."))
        self.xt_btn = Gtk.Button(label=_("Import"))
        self.xt_btn.connect("clicked", lambda *_: self._xtream())
        for w in (self.xt_server, self.xt_user, self.xt_pass, self.xt_output, self.xt_btn):
            xt.append(w)
        row(self, "Xtream Codes", xt,
            _("The live channels of an Xtream Codes account. The password is kept in the "
              "list's address in the LinDVB settings."))

        # the imported lists
        self.append(Gtk.Label(label=_("Your lists"), xalign=0, css_classes=["heading"],
                              margin_start=12, margin_top=10))
        cols = [(_("Name"), lambda s: s.get("root", ""), False),
                (_("Type"), lambda s: KINDS.get(s.get("kind"), ""), False),
                (_("Source"), iptvsources.display_location, True),
                (_("Channels"), lambda s: str(len(iptvsources.own_channels(
                    self.app.db.channels, s.get("root", "")))), False),
                (_("Updated"), lambda s: time.strftime("%x %H:%M", time.localtime(
                    s.get("date", 0))), False)]
        self.view, self.store, self.sel = make_column_view(cols)
        sw = Gtk.ScrolledWindow(child=self.view, min_content_height=110, margin_start=12,
                                margin_end=12, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(sw)
        btns = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6)
        self.update_btn = Gtk.Button(label=_("Update"))
        self.update_btn.set_tooltip_text(
            _("Load the list again: favourites, channel numbers and guide matches stay, "
              "channels no longer in the list are removed."))
        self.update_btn.connect("clicked", lambda *_: self._update())
        rename = Gtk.Button(label=_("Rename"))
        rename.set_tooltip_text(_("Give the list its own name (also the name of its "
                                  "channel group)"))
        rename.connect("clicked", lambda *_: self._rename())
        remove = Gtk.Button(label=_("Remove"))
        remove.set_tooltip_text(_("Remove the list and its channels"))
        remove.connect("clicked", lambda *_: self._remove())
        btns.append(self.update_btn)
        btns.append(rename)
        btns.append(remove)
        self.append(btns)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["lv-hint"],
                                margin_top=4, margin_bottom=8, margin_start=12)
        self.append(self.status)
        self._refresh()
        self._count()

    # ------------------------------------------------------------------ helpers
    def _sources(self):
        return list(self.app.settings.get("iptv_sources", []) or [])

    def _set_sources(self, sources):
        self.app.settings["iptv_sources"] = sources
        try:
            self.app.settings.save()
        except OSError:
            pass
        self._refresh()

    def _refresh(self):
        fill(self.store, self._sources())

    def _count(self, text=""):
        n = sum(1 for c in self.app.db.channels if c.is_iptv and not c.dvbi_id)
        count = _("IPTV channels in your channel list: {n}").format(n=n)
        self.status.set_text(f"{text}\n{count}" if text else count)

    def _busy(self, on, text=""):
        for b in (self.url_btn, self.xt_btn, self.update_btn):
            b.set_sensitive(not on)
        if text:
            self.status.set_text(text)

    def _apply(self, kind, location, chans, replace=False):
        """Merge a loaded list, remember it and show the result."""
        db = self.app.db
        sources = self._sources()
        old = iptvsources.find(sources, location)
        root = old.get("root", "") if old else ""
        root = root or iptvsources.unique_name(
            sources, iptvsources.default_name(kind, location, chans[0].root), location)
        for c in chans:
            c.root = root
        if replace:
            a, u, gone = iptvsources.replace(db, root, chans)
        else:
            (a, u), gone = db.merge(chans), 0
        db.save()
        self._set_sources(iptvsources.remember(sources, kind, location, root,
                                               len(chans)))
        text = _("Imported {total_a} new and {total_u} updated channels").format(
            total_a=a, total_u=u)
        if gone:
            text += " " + _("{n} removed").format(n=gone)
        if self.app.window is not None:
            self.app.window.reload_channels()
            self.app.window.set_status(text)
        self._count(text)
        self._fetch_logos()

    def _fetch_logos(self):
        """tvg-logo: download the channel logos in the background."""
        todo = [c for c in self.app.db.channels
                if c.is_iptv and not c.dvbi_id and c.logo_url and not c.logo]
        if not todo:
            return
        from .. import dvbi

        def work():
            dvbi.download_logos(todo, os.path.join(str(CACHE_DIR), "iptv-logos"))
            GLib.idle_add(done)

        def done():
            self.app.db.save()
            if self.app.window is not None:
                self.app.window.reload_channels()
            return False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ files
    def _pick_files(self):
        dlg = Gtk.FileDialog(title=_("Import IPTV channel lists"), filters=_filters())
        dlg.open_multiple(self.get_root(), None, self._files_chosen)

    def _files_chosen(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        for i in range(files.get_n_items()):
            self._import_path(files.get_item(i).get_path())

    def _import_path(self, path, replace=False):
        try:
            chans = import_file(path)
        except (OSError, ValueError) as e:
            message(self.get_root(), _("Could not import {name}").format(
                name=os.path.basename(path)), str(e))
            return
        if not chans:
            message(self.get_root(), _("Could not import {name}").format(
                name=os.path.basename(path)), _("The file has no channels."))
            return
        self._apply("file", path, chans, replace)

    # ------------------------------------------------------------------ download
    def _download(self):
        url = self.url.get_text().strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        self._load_url("url", url)

    def _xtream(self):
        server, user = self.xt_server.get_text().strip(), self.xt_user.get_text().strip()
        if not server or not user:
            return
        output = iptvsources.XTREAM_OUTPUTS[self.xt_output.get_selected()]
        self._load_url("xtream", iptvsources.xtream_url(server, user, self.xt_pass.get_text(),
                                                        output))

    def _load_url(self, kind, url, replace=False):
        shown = iptvsources.display_location({"kind": kind, "location": url})
        self._busy(True, _("Downloading {url}…").format(url=shown))

        def work():
            try:
                GLib.idle_add(self._downloaded, kind, url, _download(url), "", replace)
            except (OSError, ValueError) as e:
                GLib.idle_add(self._downloaded, kind, url, None, str(e), replace)
        threading.Thread(target=work, daemon=True).start()

    def _downloaded(self, kind, url, chans, error, replace):
        self._busy(False)
        if chans and kind == "xtream":
            chans = iptvsources.live_only(chans)       # no films and series
        if error or not chans:
            self._count()
            shown = iptvsources.display_location({"kind": kind, "location": url})
            message(self.get_root(), _("Could not import {name}").format(name=shown),
                    error or _("The address has no channels."))
            return False
        self._apply(kind, url, chans, replace)
        if kind == "url":
            self.url.set_text("")
        elif kind == "xtream":
            self.xt_pass.set_text("")
        return False

    # ------------------------------------------------------------------ the lists
    def _update(self):
        src = selected(self.sel)
        if not src:
            return
        loc = src.get("location", "")
        if src.get("kind") == "file":
            if os.path.exists(loc):
                self._import_path(loc, replace=True)
            else:
                self._pick_files()          # the file moved: choose it again
        else:
            self._load_url(src.get("kind", "url"), loc, replace=True)

    def _rename(self):
        src = selected(self.sel)
        if not src:
            return

        def done(name):
            sources, used = iptvsources.rename(self.app.db, self._sources(),
                                               src.get("location"), name)
            self.app.db.save()
            self._set_sources(sources)
            if self.app.window is not None:
                self.app.window.reload_channels()
            self._count(_("List renamed to {name}").format(name=used))

        ask_name(self.get_root(), _("Rename"), src.get("root", ""), done, label=_("Name"))

    def _remove(self):
        src = selected(self.sel)
        if not src:
            return
        n = iptvsources.remove(self.app.db, src.get("root", ""))
        self.app.db.save()
        self._set_sources(iptvsources.forget(self._sources(), src.get("location")))
        if self.app.window is not None:
            self.app.window.reload_channels()
        self._count(_("{n} channels removed").format(n=n))
