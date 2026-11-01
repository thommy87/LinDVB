"""LinDVB application object: owns settings, channels, EPG, tuners and timers."""

import sys
import threading
import urllib.parse
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

from .channels import ChannelDB, import_file  # noqa: E402
from .config import APP_ID, Settings  # noqa: E402
from .epg import EPGStore, apply_timezone, read_xmltv  # noqa: E402
from .plugins import PluginManager  # noqa: E402
from .recorder import TimerManager  # noqa: E402
from .tuner import TunerManager  # noqa: E402


class LinDVBApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN |
                         Gio.ApplicationFlags.NON_UNIQUE)
        self.window = None
        self.add_main_option("import", ord("i"), GLib.OptionFlags.NONE, GLib.OptionArg.FILENAME_ARRAY,
                             "Import a channel list (dvbv5, VDR, lamedb, M3U, CSV, DVBViewer .ini)", "FILE")
        self.add_main_option("channel", ord("c"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Start with this channel (name or number)", "NAME")
        self._start_channel = None
        self._epg_dirty = False

    def do_handle_local_options(self, options):
        opts = options.end().unpack()
        Gst.init(None)
        self.settings = Settings()
        self.plugins = PluginManager(self.settings)
        self.plugins.load_all()
        self.db = ChannelDB()
        for path in opts.get("import", []):
            path = path.decode() if isinstance(path, bytes) else path
            loader = self.plugins.importer_for(path)
            channels = loader(path) if loader else import_file(path)
            added, updated = self.db.merge(channels)
            print(f"{path}: {added} added, {updated} updated")
            self.db.save()
        self._start_channel = opts.get("channel")
        return -1

    def do_startup(self):
        Gtk.Application.do_startup(self)
        apply_timezone(self.settings)
        self.epg = EPGStore(persist=self.settings.get("epg_save", True))
        self.tuners = TunerManager(self.settings, ca_factory=self.plugins.make_ca_provider)
        self.timers = TimerManager(self)
        self.plugins.bind_app(self)
        self.plugins.emit("startup")
        self._cam_window = None
        if self.settings.get("ca_provider") == "ci":
            self._watch_cam()
        GLib.timeout_add_seconds(3, self._flush_epg)
        GLib.timeout_add_seconds(300, lambda: (self.timers.apply_series(), True)[1])

    def do_activate(self):
        from .ui.window import MainWindow
        if not self.window:
            self.window = MainWindow(self)
        self.window.present()
        target = None
        if self._start_channel:
            s = self._start_channel
            target = next((c for c in self.db.channels if c.name.lower() == s.lower() or
                           str(c.number) == s), None)
        elif self.settings.get("last_channel"):
            target = self.db.by_key(self.settings["last_channel"])
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
        self.tuners.shutdown()
        self.epg.save()
        Gtk.Application.do_shutdown(self)

    # -- EPG -------------------------------------------------------------------
    def on_eit(self, _cap, key, events):
        for ev in events:
            self.epg.add(key, ev)
        self._epg_dirty = True

    def _flush_epg(self):
        if self._epg_dirty:
            self._epg_dirty = False
            self.epg.notify()
        return True

    def update_xmltv(self):
        url = self.settings.get("xmltv_url")
        if not url:
            self.window.set_status("Set an XMLTV URL in the preferences first")
            return
        self.window.set_status("Downloading XMLTV guide…")
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
            self.epg.add("xmltv:" + cid, ev)
        xmltvmatch.save_channels(channels)
        matched, unmatched = xmltvmatch.auto_match(self.db.channels, channels)
        self.db.save()
        self.epg.notify()
        self.epg.save()
        msg = f"XMLTV: {len(items)} programmes loaded, {matched} IPTV channels matched"
        if unmatched:
            msg += f", {unmatched} without guide (TV ▸ Match IPTV Channels to XMLTV)"
        self.window.set_status(msg)
        return False

    def web_search(self, text, parent):
        urls = self.settings["epg_search_urls"]
        if not urls:
            return
        pop = Gtk.Window(title="Search the web", transient_for=parent, modal=True)
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

    def show_xmltv_match(self):
        from .ui.xmltvmatch import XMLTVMatchWindow
        XMLTVMatchWindow(self, self.window).present()

    def show_prefs(self):
        from .ui.dialogs import PrefsWindow
        PrefsWindow(self, self.window).present()

    def show_editor(self, channel=None):
        from .ui.dialogs import EditorWindow
        EditorWindow(self, self.window, channel).present()

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
    return LinDVBApp().run(sys.argv)
