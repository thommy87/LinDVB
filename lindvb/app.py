# SPDX-License-Identifier: GPL-2.0-or-later
"""LinDVB application object: owns settings, channels, EPG, tuners and timers."""

import os
import sys
import threading
import time
import urllib.parse
import urllib.request

from . import compat

compat.require_gi()   # GTK 4 (>= compat.MIN_GTK) and GStreamer, or exit with a hint
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

from . import channels as channels_mod  # noqa: E402
from .channels import ChannelDB, import_file  # noqa: E402
from .config import APP_ID, Settings  # noqa: E402
from .epg import EPGStore, apply_timezone, read_xmltv  # noqa: E402
from .plugins import PluginManager  # noqa: E402
from .recorder import TimerManager  # noqa: E402
from .tuner import TunerManager  # noqa: E402
from . import i18n  # noqa: E402
from .i18n import _  # noqa: E402


GUIDE_INTERVAL = 6 * 3600         # DVB-I content guide refresh


class LinDVBApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN |
                         Gio.ApplicationFlags.NON_UNIQUE)
        self.window = None
        self.add_main_option("import", ord("i"), GLib.OptionFlags.NONE, GLib.OptionArg.FILENAME_ARRAY,
                             _("Import a channel list (dvbv5, VDR, lamedb, M3U, CSV, DVBViewer .ini)"), "FILE")
        self.add_main_option("channel", ord("c"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             _("Start with this channel (name or number)"), "NAME")
        self._start_channel = None
        self._epg_dirty = False
        self._plugins_checked = False

    def do_handle_local_options(self, options):
        opts = options.end().unpack()
        Gst.init(None)
        self.settings = Settings()
        i18n.setup(self.settings.get("language", ""))
        if i18n.is_rtl():
            Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
        channels_mod.DVBI_GUIDE_FIRST = bool(self.settings.get("dvbi_guide", True))
        from . import dvbi
        dvbi.use_country(self.settings.get("dvbi_country"))
        self.plugins = PluginManager(self.settings)
        self.plugins.load_all()
        self.db = ChannelDB()
        for path in opts.get("import", []):
            path = path.decode() if isinstance(path, bytes) else path
            if not os.path.isabs(path) and os.environ.get("LINDVB_ORIG_CWD"):
                path = os.path.join(os.environ["LINDVB_ORIG_CWD"], path)   # AppImage
            loader = self.plugins.importer_for(path)
            channels = loader(path) if loader else import_file(path)
            added, updated = self.db.merge(channels)
            print(f"{path}: {added} added, {updated} updated")
            self.db.save()
        from .favourites import FavouriteLists
        self.favs = FavouriteLists(self.db)
        self._start_channel = opts.get("channel")
        return -1

    def do_startup(self):
        Gtk.Application.do_startup(self)
        apply_timezone(self.settings)
        self.epg = EPGStore(persist=self.settings.get("epg_save", True))
        self.epg.linker = self._epg_links
        self.apply_epg_priority()
        self.tuners = TunerManager(self.settings, ca_factory=self.plugins.make_ca_provider)
        self.timers = TimerManager(self)
        # channel analysis: open measurements, reception log and weak signal warnings
        from .analysis import ReceptionMonitor
        self.analysis_sessions = []
        self.reception = ReceptionMonitor(self)
        self.plugins.bind_app(self)
        self.plugins.emit("startup")
        self._cam_window = None
        if self.settings.get("ca_provider") == "ci":
            self._watch_cam()
        GLib.timeout_add_seconds(3, self._flush_epg)
        GLib.timeout_add_seconds(300, lambda: (self.timers.apply_series(), True)[1])
        # DVB-I: check the service list for updates (IT-18: at least daily)
        self._dvbi_busy = False
        self._guide_busy = False
        GLib.timeout_add_seconds(15, self._dvbi_check)
        # programme reminders from the guide
        from .reminders import ReminderStore
        self.reminders = ReminderStore()
        watch = Gio.SimpleAction.new("watch-channel", GLib.VariantType.new("s"))
        watch.connect("activate", lambda _a, key: self._watch_key(key.get_string()))
        self.add_action(watch)
        GLib.timeout_add_seconds(15, self._check_reminders)

    def _watch_key(self, key):
        self.do_activate()
        c = self.db.by_key(key)
        if c is not None:
            self.window.zap(c)

    def _check_reminders(self):
        lead = int(self.settings.get("reminder_minutes", 2)) * 60
        for r in self.reminders.due(lead):
            when = time.strftime("%H:%M", time.localtime(r.start))
            n = Gio.Notification.new(r.title)
            n.set_body(_("{channel} at {time}").format(channel=r.channel_name, time=when))
            n.set_priority(Gio.NotificationPriority.HIGH)
            n.add_button_with_target(_("Watch"), "app.watch-channel", GLib.Variant("s", r.channel_key))
            n.set_default_action_and_target("app.watch-channel", GLib.Variant("s", r.channel_key))
            self.send_notification("reminder-" + r.key, n)
            if self.window is not None:
                self.window.set_status(_("Reminder: {title} on {channel} at {time}").format(
                    title=r.title, channel=r.channel_name, time=when))
            self.plugins.emit("reminder", r)
        return True

    def _report_missing_plugins(self):
        report = compat.plugin_report(compat.missing_plugins())
        if report:
            sys.stderr.write("LinDVB: {}\n{}\n".format(*report))
            from .ui.common import message
            message(self.window, *report)

    def do_activate(self):
        from .ui.window import MainWindow
        if not self.window:
            self.window = MainWindow(self)
        self.window.present()
        first_start = not self._plugins_checked
        if first_start:
            self._plugins_checked = True
            self._report_missing_plugins()
        target = None
        if self._start_channel:
            s = self._start_channel
            target = next((c for c in self.db.channels if c.name.lower() == s.lower() or
                           str(c.number) == s), None)
        elif first_start:
            # Preferences › Channels › At start: the last channel, the first of
            # the list or a chosen one; only at the start, not when LinDVB is
            # started again while it runs
            from .channels import startup_channel
            target = startup_channel(self.settings, self.db, self.window.first_in_list())
        if target:
            self.window.zap(target)

    def do_open(self, files, n, hint):
        self.do_activate()
        uris = [f.get_path() or f.get_uri() for f in files]
        self.window.play_files(uris)

    def do_shutdown(self):
        self.plugins.emit("shutdown")
        from . import en50221
        en50221.close_all()
        if self.window:
            self.window.settings.save()
        self.timers.stop_all()
        for session in list(self.analysis_sessions):
            session.stop()
        self.reception.shutdown()
        self.tuners.shutdown()
        self.epg.save()
        Gtk.Application.do_shutdown(self)

    # -- EPG -------------------------------------------------------------------
    def on_eit(self, _cap, key, events):
        for ev in events:
            self.epg.add(key, ev, "eit")
        self._epg_dirty = True

    def on_provider_epg(self, _cap, key, events):
        for ev in events:
            self.epg.add(key, ev, "provider")
        self._epg_dirty = True

    def _epg_links(self):
        """Store key -> all guide keys of the channel (see Channel.epg_links);
        also maps the per-channel source orders onto the store keys."""
        from .epgmerge import normalize_order
        by_channel = self.settings.get("epg_priority_channels") or {}
        links, orders = {}, {}
        for c in self.db.channels:
            keys = c.epg_links
            if len(keys) > 1:
                have = links.setdefault(keys[0], [])
                have += [k for k in keys[1:] if k not in have]
            if c.key in by_channel:
                orders[keys[0]] = normalize_order(by_channel[c.key])
        self.epg.channel_orders = orders
        return links

    def apply_epg_priority(self):
        """Hand the source priority of the settings to the EPG store."""
        s = self.settings
        self.epg.set_priority(s.get("epg_priority_mode", "auto"), s.get("epg_priority_order"))
        self.epg.relink()

    def on_tsid(self, cap, tsid):
        """Channel lists without transport stream ids (older scans) learn it from
        the PAT, so the DVB guide (keyed by tsid:sid) finds its channels."""
        mux = cap.channel.mux_key
        changed = False
        for c in self.db.channels:
            if not c.tsid and not c.is_iptv and c.mux_key == mux:
                c.tsid = tsid
                changed = True
        if changed:
            self.db.save()
            self._epg_dirty = True

    def on_ait_pid(self, cap, pid):
        """HbbTV: remember the AIT PID from the PMT, so the next tuning to the
        channel filters it from the start."""
        key = cap.channel.key
        for c in self.db.channels:
            if c.key == key and c.ait_pid != pid:
                c.ait_pid = pid
                self.db.save()
                break

    def _flush_epg(self):
        if self._epg_dirty:
            self._epg_dirty = False
            self.epg.notify()
        return True

    def update_xmltv(self):
        url = self.settings.get("xmltv_url")
        if not url:
            self.window.set_status(_("Set an XMLTV URL in the preferences first"))
            return
        self.window.set_status(_("Downloading XMLTV guide…"))
        language = self.settings.get("epg_language", "")

        def work():
            try:
                data = urllib.request.urlopen(url, timeout=60).read()
                channels, items = read_xmltv(data, language)
            except Exception as e:  # network and XML errors are reported to the user
                GLib.idle_add(self.window.set_status, f"XMLTV failed: {e}")
                return
            GLib.idle_add(self._apply_xmltv, channels, items)
        threading.Thread(target=work, daemon=True).start()

    def _apply_xmltv(self, channels, items):
        from . import xmltvmatch
        for cid, ev in items:
            self.epg.add("xmltv:" + cid, ev, "xmltv")
        xmltvmatch.save_channels(channels)
        matched, unmatched = xmltvmatch.auto_match(self.db.channels, channels)
        self.db.save()
        self.epg.notify()
        self.epg.save()
        msg = f"XMLTV: {len(items)} programmes loaded, {matched} IPTV channels matched"
        if unmatched:
            msg += _(", {unmatched} without guide (Preferences ▸ EPG ▸ Match IPTV Channels)").format(unmatched=unmatched)
        self.window.set_status(msg)
        return False

    # -- DVB-I -----------------------------------------------------------------
    def dvbi_updater(self):
        from . import dvbi
        from .config import CACHE_DIR
        return dvbi.Updater(self.settings, CACHE_DIR / "dvbi", CACHE_DIR / "dvbi" / "logos")

    def _dvbi_check(self):
        import time
        from . import dvbi
        if dvbi.update_due(self.settings):
            self.update_dvbi()
        elif time.time() - float(self.settings.get("dvbi_guide_last") or 0) >= GUIDE_INTERVAL:
            self.update_dvbi_guide()
        GLib.timeout_add_seconds(3600, self._dvbi_check)
        return False

    def update_dvbi_guide(self, done=None):
        """Load the DVB-I content guide of all DVB-I channels in the
        background (IT-6: now/next, today and 7 days ahead)."""
        import time
        from concurrent.futures import ThreadPoolExecutor
        from . import contentguide
        if self._guide_busy or not self.settings.get("dvbi_guide", True) or \
                not self.settings.get("dvbi_privacy_ack"):
            return False
        sources = contentguide.sources_for(self.db.channels)
        if not sources:
            return False
        self._guide_busy = True
        self.settings["dvbi_guide_last"] = time.time()

        def work():
            # now/next of all channels first, so the list shows them quickly
            with ThreadPoolExecutor(4) as pool:
                first = list(pool.map(lambda s: contentguide.load_guide(s, days=False), sources))
            GLib.idle_add(add, sources, first, False)
            with ThreadPoolExecutor(4) as pool:
                full = list(pool.map(lambda s: contentguide.load_guide(s, details=False), sources))
            GLib.idle_add(add, sources, full, True)

        def add(srcs, results, last):
            count, with_guide, failed = 0, 0, 0
            for src, (events, errors) in zip(srcs, results):
                for ev in events:
                    self.epg.add(src.key, ev, "dvbi")
                count += len(events)
                with_guide += bool(events)
                failed += bool(errors and not events)
            self.epg.notify()
            if last:
                self._guide_busy = False
                self.epg.save()
                msg = f"DVB-I guide: {count} programmes for {with_guide} channels"
                if failed:
                    msg += f", {failed} guide servers did not answer"
                if self.window:
                    self.window.set_status(msg)
                if done:
                    done(msg)
            return False

        threading.Thread(target=work, daemon=True).start()
        return True

    def update_dvbi(self, force=False, region=None, done=None):
        """Load the DVB-I service list in the background and update the
        channel list. *done(result, message)* runs on the main thread."""
        if self._dvbi_busy or not self.settings.get("dvbi_list_url"):
            return False
        self._dvbi_busy = True
        updater = self.dvbi_updater()
        scanned = [c for c in self.db.channels if not c.dvbi_id]
        if self.window:
            self.window.set_status(_("DVB-I: loading the service list…"))

        def work():
            try:
                res = updater.run(scanned, force=force, region=region)
            except Exception as e:  # report unexpected errors instead of dying in the thread
                from .dvbi import UpdateResult
                res = UpdateResult(error=str(e))
            GLib.idle_add(finish, res)

        def finish(res):
            self._dvbi_busy = False
            msg = updater.apply(res, self.db)
            self.settings.save()
            if res.changed:
                self.db.save()
                if self.window:
                    self.window.reload_channels()
                GLib.timeout_add_seconds(2, lambda: (self.update_dvbi_guide(), False)[1])
            if self.window:
                self.window.set_status(msg)
            if done:
                done(res, msg)
            return False

        threading.Thread(target=work, daemon=True).start()
        return True

    def show_dvbi_setup(self):
        self.show_prefs("internet")

    def web_search(self, text, parent):
        urls = self.settings["epg_search_urls"]
        if not urls:
            return
        pop = Gtk.Window(title=_("Search the web"), transient_for=parent, modal=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12,
                      margin_end=12, margin_top=12, margin_bottom=12)
        for name, tmpl in urls:
            b = Gtk.Button(label=name)
            uri = tmpl.replace("{q}", urllib.parse.quote(text))
            b.connect("clicked", lambda _b, u=uri: (Gtk.UriLauncher.new(u).launch(parent, None, None),
                                                   pop.close()))
            box.append(b)
        pop.set_child(box)
        pop.present()

    # -- windows ---------------------------------------------------------------
    def show_epg(self, channel=None):
        from .ui.epg import EPGWindow
        EPGWindow(self, self.window, channel).present()

    def show_timers(self, timer=None):
        from .ui.timers import TimersWindow
        win = TimersWindow(self, self.window)
        if timer is not None:
            win.select(timer)
        win.present()

    def show_scan(self):
        from .ui.scan import ScanWindow
        ScanWindow(self, self.window).present()

    def show_tune(self):
        from .ui.tune import TuneWindow
        TuneWindow(self, self.window).present()

    def show_xmltv_match(self):
        from .ui.xmltvmatch import XMLTVMatchWindow
        XMLTVMatchWindow(self, self.window).present()

    def show_prefs(self, page=None):
        """Open the preferences, on *page* ("tuner", "internet", "epg", ...) if given."""
        from .ui.dialogs import PrefsWindow
        PrefsWindow(self, self.window, page).present()

    def show_editor(self, channel=None):
        from .ui.dialogs import EditorWindow
        EditorWindow(self, self.window, channel).present()

    def show_favourites(self):
        """The window to create, rename, delete and reorder favourites lists."""
        from .ui.favourites import FavouritesWindow
        FavouritesWindow(self, self.window).present()

    def favourites_changed(self, remap=None):
        """Save the favourites lists and show the change in the channel list.
        *remap* maps old list indexes to new ones (None: deleted) after lists
        were moved or deleted, so the list chosen stays chosen."""
        if remap:
            from .favourites import fav_index, filter_setting
            i = fav_index(filter_setting(self.settings.get("list_filter")))
            if i in remap:
                new = remap[i]
                self.settings["list_filter"] = "all" if new is None else f"fav:{new}"
            last = self.settings.get("fav_last", 0)
            if last in remap:
                self.settings["fav_last"] = remap[last] or 0
        self.favs.sync()
        try:
            self.favs.save()
        except OSError as e:
            if self.window:
                self.window.set_status(str(e))
        if self.window:
            self.window.favourites_changed()

    def show_analysis(self, channel=None):
        """The channel analysis window (right click on a channel)."""
        from .ui.analysis import open_analysis
        channel = channel or (self.window.current if self.window else None)
        if channel is not None:
            open_analysis(self, channel)

    def show_stats(self):
        from .ui.dialogs import StatsWindow
        StatsWindow(self, self.window).present()

    # -- CI module menu ---------------------------------------------------------
    def _watch_cam(self):
        """Open the CAM menu window when the module asks something by itself
        (a PIN prompt or a message while tuning)."""
        from . import en50221
        app = self

        class Popup(en50221.CIListener):
            def ci_menu(self, stack, menu):
                GLib.idle_add(app._cam_popup, "show_menu", menu)

            def ci_enquiry(self, stack, enquiry):
                GLib.idle_add(app._cam_popup, "show_enquiry", enquiry)

        dev = en50221.shared_device(self.settings)
        dev.listeners.append(Popup())
        dev.open()

    def _cam_popup(self, method, obj):
        if self._cam_window is None and self.window is not None:
            win = self.show_cam_menu()
            getattr(win, method)(obj)
        return False

    def show_cam_menu(self):
        from .ui.cam import CamMenuWindow
        if self._cam_window is None:
            self._cam_window = CamMenuWindow(self, self.window)
            self._cam_window.connect("close-request", self._cam_closed)
        self._cam_window.present()
        return self._cam_window

    def _cam_closed(self, *_):
        self._cam_window = None
        return False

    def show_plugins(self):
        from .ui.dialogs import PluginsWindow
        PluginsWindow(self, self.window).present()

    def show_teletext(self, capture, channel):
        from .ui.dialogs import TeletextWindow
        TeletextWindow(self, self.window, capture, channel).present()


def main():
    from . import watchdog
    from .config import CACHE_DIR
    watchdog.start(CACHE_DIR / "hang.log")
    return LinDVBApp().run(sys.argv)
