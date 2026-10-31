"""Main window: channel list, video area with OSD, transport controls, menus."""

import random
import time
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from ..channels import import_file, write_csv, write_dvbv5, write_m3u
from ..config import APP_NAME
from ..player import EQ_BANDS, Player
from .common import Row, hm, message

FILTERS = ["All channels", "TV", "Radio", "Favourites", "Free to air", "Encrypted",
           "HD channels", "Internet streams"]
ZOOM = [("Fit", Gtk.ContentFit.CONTAIN), ("Fill (crop)", Gtk.ContentFit.COVER),
        ("Stretch", Gtk.ContentFit.FILL)]
SLEEP_MINUTES = [15, 30, 45, 60, 90, 120, 180]

CSS = b"""
.video { background: black; }
.osd { background: rgba(0,0,0,0.72); color: white; border-radius: 10px; padding: 12px 18px; }
.osd .title { font-size: 1.4em; font-weight: bold; }
.osd-num { background: rgba(0,0,0,0.72); color: white; font-size: 2.5em; font-weight: bold;
           border-radius: 10px; padding: 6px 16px; }
.rec { color: #e01b24; font-weight: bold; }
.ttxsub { background: rgba(0,0,0,0.75); color: white; font-size: 1.5em; font-weight: bold;
          border-radius: 4px; padding: 2px 10px; }
.statusbar { padding: 2px 8px; }
"""


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=1200, default_height=720)
        self.app = app
        self.settings = app.settings
        self.current = None          # current Channel
        self.previous = None         # last channel (for "Last Channel")
        self.capture = None
        self.number_buffer = ""
        self.playlist, self.playlist_pos = [], -1
        self.playlist_random = self.playlist_repeat = False
        self.sleep_source = None
        self._osd_timeout = None

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._build_actions()
        self._build_ui()
        self.player = Player(self.picture, self.settings)
        self.player.connect("error", lambda _p, m: self.set_status(f"Playback error: {m}"))
        self.player.connect("tracks", lambda _p: self._rebuild_track_menus())
        self.player.connect("subtitle-text", self._on_subtitle_text)
        self.player.connect("eos", self._on_eos)
        self.player.connect("state", lambda _p, s: self._update_transport())
        self.app.tuners.connect("preempt-live", self._on_preempt)
        self.app.timers.connect("message", lambda _t, m: self.set_status(m))
        self.app.timers.connect("changed", lambda _t: self._update_transport())
        self.app.epg.listeners.append(self._on_epg)
        self.reload_channels()
        GLib.timeout_add(1000, self._tick)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ actions
    def _build_actions(self):
        acts = {
            "open-file": self.on_open_file, "open-url": self.on_open_url,
            "import": self.on_import, "export": self.on_export,
            "epg": lambda: self.app.show_epg(), "timers": lambda: self.app.show_timers(),
            "scan": lambda: self.app.show_scan(), "prefs": lambda: self.app.show_prefs(),
            "editor": lambda: self.app.show_editor(), "stats": lambda: self.app.show_stats(),
            "teletext": self.on_teletext, "record": self.on_record,
            "fullscreen": self.toggle_fullscreen, "toggle-list": self.toggle_list,
            "pause": self.player_pause, "live": lambda: self.player.go_live(),
            "back10": lambda: self.player.jump(-10), "fwd10": lambda: self.player.jump(10),
            "back60": lambda: self.player.jump(-60), "fwd60": lambda: self.player.jump(60),
            "ch-next": lambda: self.zap_relative(1), "ch-prev": lambda: self.zap_relative(-1),
            "fav-next": lambda: self.zap_relative(1, favs=True),
            "fav-prev": lambda: self.zap_relative(-1, favs=True),
            "last-channel": self.zap_last, "stop": self.stop_playback,
            "mute": self.toggle_mute, "vol-up": lambda: self.change_volume(0.05),
            "vol-down": lambda: self.change_volume(-0.05),
            "screenshot": self.on_screenshot, "osd": lambda: self.show_osd(),
            "picture": self.on_picture_settings, "equalizer": self.on_equalizer,
            "about": self.on_about, "keep-timeshift": self.on_keep_timeshift,
            "playlist-next": lambda: self.playlist_step(1),
            "playlist-prev": lambda: self.playlist_step(-1),
            "sleep-off": self.sleep_cancel, "sleep-programme": self.sleep_after_programme,
            "rebuild": self.rebuild_playback, "xmltv": lambda: self.app.update_xmltv(),
            "web-epg": self.on_web_search,
            "plugins": lambda: self.app.show_plugins(),
            "cam-menu": lambda: self.app.show_cam_menu(),
        }
        for name, fn in acts.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            self.add_action(a)
        for m in SLEEP_MINUTES:
            a = Gio.SimpleAction.new(f"sleep-{m}", None)
            a.connect("activate", lambda _a, _p, m=m: self.sleep_in(m * 60))
            self.add_action(a)
        a = Gio.SimpleAction.new("audio-track", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, p: self.player.select_audio(p.get_int32()))
        self.add_action(a)
        a = Gio.SimpleAction.new("subtitle", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, p: (self.player.select_subtitle(p.get_int32()),
                                             self._rebuild_track_menus()))
        self.add_action(a)
        a = Gio.SimpleAction.new("zoom", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, p: self.picture.set_content_fit(ZOOM[p.get_int32()][1]))
        self.add_action(a)
        a = Gio.SimpleAction.new("winsize", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, p: self.window_size(p.get_int32()))
        self.add_action(a)
        a = Gio.SimpleAction.new("rate", GLib.VariantType.new("d"))
        a.connect("activate", lambda _a, p: self.player.set_rate(p.get_double()))
        self.add_action(a)
        a = Gio.SimpleAction.new("plugin-action", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, p: self._run_plugin_action(p.get_int32()))
        self.add_action(a)
        for name, init, fn in (("video-off", False, lambda v: self.player.disable_video(v)),
                               ("pl-random", False, lambda v: setattr(self, "playlist_random", v)),
                               ("pl-repeat", False, lambda v: setattr(self, "playlist_repeat", v))):
            a = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(init))

            def toggle(act, _p, fn=fn):
                v = not act.get_state().get_boolean()
                act.set_state(GLib.Variant.new_boolean(v))
                fn(v)
            a.connect("activate", toggle)
            self.add_action(a)

        accels = {
            "win.open-file": ["<Ctrl>o"], "win.open-url": ["<Ctrl>u"], "win.epg": ["<Ctrl>e"],
            "win.timers": ["<Ctrl>t"], "win.scan": ["<Ctrl><Shift>s"], "win.prefs": ["<Ctrl>comma"],
            "win.record": ["<Ctrl>r"], "win.fullscreen": ["F11"], "win.toggle-list": ["F9"],
            "win.teletext": ["<Ctrl><Shift>t"], "win.screenshot": ["<Ctrl><Shift>p"],
            "win.ch-next": ["Page_Up"], "win.ch-prev": ["Page_Down"],
            "win.last-channel": ["<Ctrl>BackSpace"], "win.editor": ["<Ctrl><Shift>e"],
        }
        for k, v in accels.items():
            self.app.set_accels_for_action(k, v)

    def _run_plugin_action(self, index):
        try:
            _plugin, _label, cb = self.app.plugins.menu_actions[index]
            cb(self.app)
        except Exception as e:
            self.set_status(f"Plugin action failed: {e}")

    def _menu(self):
        m = Gio.Menu()
        file = Gio.Menu()
        file.append("Open Media File…", "win.open-file")
        file.append("Open URL…", "win.open-url")
        file.append("Import Channel List…", "win.import")
        file.append("Export Channel List…", "win.export")
        m.append_section(None, file)
        tv = Gio.Menu()
        tv.append("Programme Guide (EPG)", "win.epg")
        tv.append("Timer Recordings", "win.timers")
        tv.append("Recording and Device Statistics", "win.stats")
        tv.append("Teletext", "win.teletext")
        tv.append("CAM Menu (CI Module)…", "win.cam-menu")
        tv.append("Channel Search…", "win.scan")
        tv.append("Channel Editor / Favourites", "win.editor")
        tv.append("Update Internet EPG (XMLTV)", "win.xmltv")
        m.append_section(None, tv)

        play = Gio.Menu()
        self.audio_menu = Gio.Menu()
        self.sub_menu = Gio.Menu()
        play.append_submenu("Audio Track", self.audio_menu)
        play.append_submenu("Subtitles", self.sub_menu)
        rate = Gio.Menu()
        for r in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            rate.append(f"{r:g}×", f"win.rate({r})")
        play.append_submenu("Play Rate", rate)
        pl = Gio.Menu()
        pl.append("Next", "win.playlist-next")
        pl.append("Previous", "win.playlist-prev")
        pl.append("Random Order", "win.pl-random")
        pl.append("Endless Playback", "win.pl-repeat")
        play.append_submenu("Playlist", pl)
        play.append("Keep Timeshift as Recording", "win.keep-timeshift")
        play.append("Rebuild Playback", "win.rebuild")
        play.append("Video Off (audio only)", "win.video-off")
        sleep = Gio.Menu()
        for mins in SLEEP_MINUTES:
            sleep.append(f"Stop in {mins} min", f"win.sleep-{mins}")
        sleep.append("Stop after current programme", "win.sleep-programme")
        sleep.append("Cancel", "win.sleep-off")
        play.append_submenu("Sleep Timer", sleep)
        m.append_section(None, play)

        view = Gio.Menu()
        view.append("Full Screen", "win.fullscreen")
        view.append("Show/Hide Channel List", "win.toggle-list")
        zoom = Gio.Menu()
        for i, (name, _f) in enumerate(ZOOM):
            zoom.append(name, f"win.zoom({i})")
        view.append_submenu("Aspect / Zoom", zoom)
        ws = Gio.Menu()
        for p in (50, 75, 100, 200):
            ws.append(f"{p}%", f"win.winsize({p})")
        view.append_submenu("Window Size", ws)
        view.append("Picture Settings…", "win.picture")
        view.append("Equalizer…", "win.equalizer")
        view.append("Screenshot", "win.screenshot")
        m.append_section(None, view)
        end = Gio.Menu()
        end.append("Preferences", "win.prefs")
        end.append("Plugins…", "win.plugins")
        for i, (_plugin, label, _cb) in enumerate(self.app.plugins.menu_actions):
            end.append(label, f"win.plugin-action({i})")
        end.append("About LinDVB", "win.about")
        m.append_section(None, end)
        return m

    # ------------------------------------------------------------------ layout
    def _build_ui(self):
        hb = Gtk.HeaderBar()
        self.set_titlebar(hb)
        def btn(icon, action, tip):
            b = Gtk.Button(icon_name=icon, action_name=action, tooltip_text=tip)
            return b
        hb.pack_start(btn("go-up-symbolic", "win.ch-prev", "Channel minus"))
        hb.pack_start(btn("go-down-symbolic", "win.ch-next", "Channel plus"))
        hb.pack_start(btn("media-seek-backward-symbolic", "win.back10", "Jump −10 s"))
        self.pause_btn = btn("media-playback-pause-symbolic", "win.pause", "Pause / timeshift")
        hb.pack_start(self.pause_btn)
        hb.pack_start(btn("media-seek-forward-symbolic", "win.fwd10", "Jump +10 s"))
        hb.pack_start(btn("go-last-symbolic", "win.live", "Back to live"))
        self.rec_btn = Gtk.ToggleButton(icon_name="media-record-symbolic",
                                        tooltip_text="Record current channel")
        self.rec_btn.connect("clicked", lambda b: self.on_record() if b.get_active() !=
                             self._is_recording() else None)
        hb.pack_start(self.rec_btn)
        menu = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._menu())
        menu.set_primary(True)
        hb.pack_end(menu)
        hb.pack_end(btn("x-office-calendar-symbolic", "win.epg", "Programme guide"))
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1.5, 0.05)
        self.vol.set_size_request(110, -1)
        self.vol.set_draw_value(False)
        self.vol.set_value(self.settings["volume"])
        self.vol.connect("value-changed", lambda s: self._set_volume(s.get_value()))
        hb.pack_end(self.vol)
        self.mute_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic", tooltip_text="Mute")
        self.mute_btn.connect("toggled", lambda b: self._set_mute(b.get_active()))
        hb.pack_end(self.mute_btn)

        # sidebar
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_margin_start(6); side.set_margin_end(6); side.set_margin_top(6)
        self.search = Gtk.SearchEntry(placeholder_text="Search channels")
        self.search.connect("search-changed", lambda _e: self._refilter())
        self.search.connect("activate", lambda _e: self._activate_first())
        side.append(self.search)
        row = Gtk.Box(spacing=4)
        self.filter_dd = Gtk.DropDown.new_from_strings(FILTERS)
        self.filter_dd.connect("notify::selected", lambda *_: self._refilter())
        self.group_dd = Gtk.DropDown.new_from_strings(["All groups"])
        self.group_dd.connect("notify::selected", lambda *_: self._refilter())
        self.group_dd.set_hexpand(True)
        row.append(self.filter_dd)
        row.append(self.group_dd)
        side.append(row)

        self.store = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.selection = Gtk.SingleSelection(model=self.filtered, autoselect=False)
        fac = Gtk.SignalListItemFactory()
        fac.connect("setup", self._row_setup)
        fac.connect("bind", self._row_bind)
        self.listview = Gtk.ListView(model=self.selection, factory=fac)
        self.listview.set_single_click_activate(False)
        self.listview.connect("activate", lambda _l, pos: self.zap(self.filtered.get_item(pos).data))
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._list_context)
        self.listview.add_controller(click)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.listview)
        side.append(sw)
        self.count_label = Gtk.Label(xalign=0, css_classes=["dim-label"])
        side.append(self.count_label)
        self.sidebar = side

        # video + OSD
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, hexpand=True, vexpand=True)
        self.picture.add_css_class("video")
        overlay = Gtk.Overlay()
        overlay.set_child(self.picture)
        self.osd = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                           valign=Gtk.Align.END, halign=Gtk.Align.FILL,
                           margin_start=24, margin_end=24, margin_bottom=24)
        self.osd.add_css_class("osd")
        self.osd_title = Gtk.Label(xalign=0, css_classes=["title"], ellipsize=Pango.EllipsizeMode.END)
        self.osd_now = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.osd_prog = Gtk.ProgressBar()
        self.osd_next = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label"])
        for w in (self.osd_title, self.osd_now, self.osd_prog, self.osd_next):
            self.osd.append(w)
        self.osd.set_visible(False)
        overlay.add_overlay(self.osd)
        self.numlabel = Gtk.Label(css_classes=["osd-num"], halign=Gtk.Align.END,
                                  valign=Gtk.Align.START, margin_top=20, margin_end=20)
        self.numlabel.set_visible(False)
        overlay.add_overlay(self.numlabel)
        # teletext subtitles drawn over the video
        self.sublabel = Gtk.Label(css_classes=["ttxsub"], halign=Gtk.Align.CENTER,
                                  valign=Gtk.Align.END, margin_bottom=36, justify=Gtk.Justification.CENTER,
                                  wrap=True, can_target=False)
        self.sublabel.set_visible(False)
        overlay.add_overlay(self.sublabel)
        dbl = Gtk.GestureClick()
        dbl.connect("pressed", lambda g, n, *_: self.toggle_fullscreen() if n == 2 else self.show_osd())
        self.picture.add_controller(dbl)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", lambda _c, _dx, dy: (self.change_volume(-0.05 * dy), True)[1])
        self.picture.add_controller(scroll)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_start_child(side)
        self.paned.set_end_child(overlay)
        self.paned.set_position(300)
        self.paned.set_shrink_start_child(False)

        # status bar
        self.statusbar = Gtk.Box(spacing=12, css_classes=["statusbar"])
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.signal_label = Gtk.Label()
        self.ts_label = Gtk.Label()
        self.rec_label = Gtk.Label(css_classes=["rec"])
        for w in (self.status, self.ts_label, self.rec_label, self.signal_label):
            self.statusbar.append(w)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.paned)
        box.append(self.statusbar)
        self.set_child(box)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # ------------------------------------------------------------------ channel list
    def _row_setup(self, _f, li):
        b = Gtk.Box(spacing=6)
        num = Gtk.Label(width_chars=4, xalign=1, css_classes=["dim-label", "numeric"])
        icon = Gtk.Image()
        lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        extra = Gtk.Image()
        for w in (num, icon, lbl, extra):
            b.append(w)
        li.set_child(b)

    def _row_bind(self, _f, li):
        c = li.get_item().data
        num, icon, lbl, extra = list(_children(li.get_child()))
        num.set_text(str(c.number))
        icon.set_from_icon_name("audio-x-generic-symbolic" if c.radio else
                                "network-wireless-symbolic" if c.is_iptv else "video-display-symbolic")
        now, _ = self.app.epg.now_next(c.epg_key)
        lbl.set_text(c.name)
        lbl.set_tooltip_text(f"{c.root} / {c.category}" + (f"\nNow: {now.title}" if now else ""))
        extra.set_from_icon_name("starred-symbolic" if c.favourite else
                                 "channel-secure-symbolic" if c.encrypted else None)

    def _filter_func(self, item):
        c = item.data
        f = self.filter_dd.get_selected()
        if f == 1 and c.radio:
            return False
        if f == 2 and not c.radio:
            return False
        if f == 3 and not c.favourite:
            return False
        if f == 4 and c.encrypted:
            return False
        if f == 5 and not c.encrypted:
            return False
        if f == 6 and "HD" not in c.name and "HEVC" not in c.name and "UHD" not in c.name:
            return False
        if f == 7 and not c.is_iptv:
            return False
        g = self.group_dd.get_selected()
        if g > 0 and self._groups[g] != c.root:
            return False
        q = self.search.get_text().strip().lower()
        if q:
            if q.isdigit():
                return str(c.number).startswith(q)
            return q in c.name.lower()
        return True

    def reload_channels(self):
        chans = self.app.db.channels
        self.store.remove_all()
        self.store.splice(0, 0, [Row(c) for c in chans])
        self._groups = ["All groups"] + self.app.db.roots()
        self.group_dd.set_model(Gtk.StringList.new(self._groups))
        self._refilter()

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self.count_label.set_text(f"{self.filtered.get_n_items()} of {self.store.get_n_items()} channels")

    def _activate_first(self):
        if self.filtered.get_n_items():
            self.zap(self.filtered.get_item(0).data)

    def _list_context(self, gesture, _n, x, y):
        pos = self.selection.get_selected()
        if pos == Gtk.INVALID_LIST_POSITION:
            return
        c = self.filtered.get_item(pos).data
        pop = Gtk.PopoverMenu.new_from_model(self._channel_menu(c))
        pop.set_parent(self.listview)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.popup()

    def _channel_menu(self, c):
        for name, fn in (("ctx-fav", lambda: self.toggle_favourite(c)),
                         ("ctx-play", lambda: self.zap(c)),
                         ("ctx-rec", lambda: self.app.timers.record_now(c)),
                         ("ctx-edit", lambda: self.app.show_editor(c)),
                         ("ctx-epg", lambda: self.app.show_epg(c))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            self.add_action(a)
        m = Gio.Menu()
        m.append("Watch", "win.ctx-play")
        m.append("Remove from Favourites" if c.favourite else "Add to Favourites", "win.ctx-fav")
        m.append("Record", "win.ctx-rec")
        m.append("Programme Guide", "win.ctx-epg")
        m.append("Edit…", "win.ctx-edit")
        return m

    def toggle_favourite(self, c):
        c.favourite = not c.favourite
        self.app.db.save()
        self._refilter()
        self.listview.queue_draw()

    # ------------------------------------------------------------------ zapping
    def zap(self, c):
        if c is None:
            return
        if self.current and self.current.key != c.key:
            self.previous = self.current
        self._release_capture()
        self.current = c
        self.playlist = []
        self.set_title(f"{c.name} – {APP_NAME}")
        try:
            if c.is_iptv:
                self.player.play_uri(c.url)
            else:
                self.capture = self.app.tuners.acquire(c, "live")
                self.capture.connect("stats", self._on_stats)
                self.capture.connect("error", lambda _c, m: self.set_status(m))
                self.capture.connect("eit", self.app.on_eit)
                self.app.plugins.emit("channel-tuned", c, self.capture)
                GLib.timeout_add(700, self._start_live, self.capture)
        except RuntimeError as e:
            self.set_status(str(e))
            return
        self.settings["last_channel"] = c.key
        self.set_status(f"Tuning {c.name}…")
        self.show_osd()
        self._update_transport()

    def _start_live(self, cap):
        if cap is self.capture:
            self.player.play_capture(cap, self.current.sid, offset=0)
        return False

    def _release_capture(self):
        self.player.stop()
        if self.capture:
            self.app.tuners.release(self.capture, "live")
            self.capture = None

    def _on_preempt(self, _tm):
        self.player.stop()
        self.capture = None
        self.set_status("Live TV stopped: tuner needed for a timer recording")

    def zap_relative(self, step, favs=False):
        n = self.filtered.get_n_items()
        items = [self.filtered.get_item(i).data for i in range(n)]
        if favs:
            items = [c for c in self.app.db.channels if c.favourite]
        if not items:
            return
        try:
            i = next(i for i, c in enumerate(items) if self.current and c.key == self.current.key)
        except StopIteration:
            i = -1 if step > 0 else 0
        c = items[(i + step) % len(items)]
        self.zap(c)

    def zap_last(self):
        if self.previous:
            self.zap(self.previous)

    def zap_number(self, num):
        for c in self.app.db.channels:
            if c.number == num:
                self.zap(c)
                return

    def stop_playback(self):
        self._release_capture()
        self.current = None
        self.set_title(APP_NAME)

    def rebuild_playback(self):
        if self.current:
            self.zap(self.current)

    # ------------------------------------------------------------------ files / urls / playlist
    def play_files(self, paths):
        self.stop_playback()
        self.playlist, self.playlist_pos = list(paths), -1
        self.playlist_step(1)

    def playlist_step(self, step):
        if not self.playlist:
            return
        if self.playlist_random:
            self.playlist_pos = random.randrange(len(self.playlist))
        else:
            self.playlist_pos += step
        if not 0 <= self.playlist_pos < len(self.playlist):
            if not self.playlist_repeat:
                self.player.stop()
                return
            self.playlist_pos %= len(self.playlist)
        item = self.playlist[self.playlist_pos]
        self.player.play_uri(item)
        self.set_title(f"{Path(item).name} – {APP_NAME}")
        self.set_status(f"Playing {item}")

    def _on_eos(self, _p):
        if self.playlist:
            self.playlist_step(1)

    def on_open_file(self):
        dlg = Gtk.FileDialog(title="Open media files")
        dlg.open_multiple(self, None, self._opened_files)

    def _opened_files(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
        lists = [p for p in paths if p.lower().endswith((".m3u", ".m3u8", ".pls"))]
        self.play_files([p for p in paths if p not in lists] + _read_playlists(lists))

    def on_open_url(self):
        win = Gtk.Window(title="Open URL", transient_for=self, modal=True, default_width=500)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12,
                      margin_bottom=12, margin_start=12, margin_end=12)
        entry = Gtk.Entry(placeholder_text="http://…, rtp://…, udp://…, rtsp://…")
        def go(*_):
            url = entry.get_text().strip()
            if url:
                self.stop_playback()
                self.playlist = [url]
                self.playlist_pos = 0
                self.player.play_uri(url)
                self.set_title(f"{url} – {APP_NAME}")
            win.close()
        entry.connect("activate", go)
        b = Gtk.Button(label="Open", css_classes=["suggested-action"], halign=Gtk.Align.END)
        b.connect("clicked", go)
        box.append(entry)
        box.append(b)
        win.set_child(box)
        win.present()

    def open_any(self, arg):
        if "://" in arg:
            self.player.play_uri(arg)
        else:
            self.play_files([arg])

    # ------------------------------------------------------------------ import / export
    def on_import(self):
        dlg = Gtk.FileDialog(title="Import channel list (dvbv5, VDR, lamedb, M3U, CSV, DVBViewer .ini)")
        dlg.open_multiple(self, None, self._import_done)

    def _import_done(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        total_a = total_u = 0
        for i in range(files.get_n_items()):
            path = files.get_item(i).get_path()
            try:
                a, u = self.app.db.merge(import_file(path))
            except (OSError, ValueError) as e:
                message(self, f"Could not import {Path(path).name}", str(e))
                continue
            total_a += a
            total_u += u
        self.app.db.save()
        self.reload_channels()
        self.set_status(f"Imported {total_a} new and {total_u} updated channels")

    def on_export(self):
        dlg = Gtk.FileDialog(title="Export channels (.conf = dvbv5, .m3u = IPTV, .csv)",
                             initial_name="channels.conf")
        dlg.save(self, None, self._export_done)

    def _export_done(self, dlg, res):
        try:
            path = dlg.save_finish(res).get_path()
        except GLib.Error:
            return
        chans = self.app.db.channels
        if path.endswith((".m3u", ".m3u8")):
            text = write_m3u(chans)
        elif path.endswith(".csv"):
            text = write_csv(chans)
        else:
            text = write_dvbv5(chans)
        Path(path).write_text(text)
        self.set_status(f"Exported to {path}")

    # ------------------------------------------------------------------ recording
    def _is_recording(self):
        return bool(self.current and self.app.timers.is_recording(self.current))

    def on_record(self):
        if not self.current:
            self.set_status("Nothing to record")
            self.rec_btn.set_active(False)
            return
        title = ""
        now, _ = self.app.epg.now_next(self.current.epg_key)
        if now:
            title = now.title
        self.app.timers.record_now(self.current, self.capture, title)
        self._update_transport()

    def on_keep_timeshift(self):
        """Save the whole timeshift buffer, then continue recording live."""
        if self.current and self.capture:
            start, _ = self.capture.store.bounds()
            self.app.timers.record_now(self.current, self.capture, "Timeshift", start_offset=start)

    # ------------------------------------------------------------------ teletext
    def on_teletext(self):
        if not self.capture:
            self.set_status("Teletext needs a DVB channel")
            return
        self.app.show_teletext(self.capture, self.current)

    # ------------------------------------------------------------------ audio / video
    def player_pause(self):
        self.player.toggle_pause()

    def _set_volume(self, v):
        self.player.volume = v
        self.settings["volume"] = v

    def _set_mute(self, m):
        self.player.mute = m
        self.mute_btn.set_icon_name("audio-volume-muted-symbolic" if m else "audio-volume-high-symbolic")

    def toggle_mute(self):
        self.mute_btn.set_active(not self.mute_btn.get_active())

    def change_volume(self, d):
        self.vol.set_value(self.vol.get_value() + d)
        self.flash(f"Volume {int(self.vol.get_value() * 100)}%")

    def _on_subtitle_text(self, _player, text):
        self.sublabel.set_text(text)
        self.sublabel.set_visible(bool(text))

    def _rebuild_track_menus(self):
        self.audio_menu.remove_all()
        tracks, cur = self.player.audio_tracks()
        for i, t in enumerate(tracks):
            self.audio_menu.append(("● " if i == cur else "") + t, f"win.audio-track({i})")
        self.sub_menu.remove_all()
        self.sub_menu.append("Off", "win.subtitle(-1)")
        subs, cur = self.player.subtitle_tracks()
        for i, t in enumerate(subs):
            self.sub_menu.append(("● " if i == cur else "") + t, f"win.subtitle({i})")

    def on_screenshot(self):
        d = Path(self.settings["screenshot_dir"]).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        name = (self.current.name if self.current else "video").replace("/", "_")
        path = d / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        self.set_status(f"Screenshot saved: {path}" if self.player.screenshot(str(path))
                        else "Screenshot failed")

    def on_picture_settings(self):
        win = Gtk.Window(title="Picture Settings", transient_for=self, default_width=380)
        grid = Gtk.Grid(row_spacing=6, column_spacing=12, margin_top=12, margin_bottom=12,
                        margin_start=12, margin_end=12)
        specs = [("brightness", -1, 1, 0.0), ("contrast", 0, 2, 1.0),
                 ("saturation", 0, 2, 1.0), ("hue", -1, 1, 0.0)]
        scales = {}
        for r, (k, lo, hi, default) in enumerate(specs):
            s = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 0.02)
            s.set_value(self.player.balance[k])
            s.set_hexpand(True)
            s.connect("value-changed", lambda s, k=k: self.player.set_balance(**{k: s.get_value()}))
            grid.attach(Gtk.Label(label=k.capitalize(), xalign=0), 0, r, 1, 1)
            grid.attach(s, 1, r, 1, 1)
            scales[k] = (s, default)
        reset = Gtk.Button(label="Restore Defaults")
        reset.connect("clicked", lambda _b: [s.set_value(d) for s, d in scales.values()])
        grid.attach(reset, 1, len(specs), 1, 1)
        win.set_child(grid)
        win.present()

    def on_equalizer(self):
        win = Gtk.Window(title="Equalizer", transient_for=self, default_height=300)
        box = Gtk.Box(spacing=6, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        for i, name in enumerate(EQ_BANDS):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            s = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -24, 12, 1)
            s.set_inverted(True)
            s.set_vexpand(True)
            s.set_value(self.player.eq_gains[i])
            s.connect("value-changed", lambda s, i=i: self.player.set_eq(i, s.get_value()))
            col.append(s)
            col.append(Gtk.Label(label=name, css_classes=["caption"]))
            box.append(col)
        win.set_child(box)
        win.present()

    def window_size(self, pct):
        size = self.player.video_size() or (720, 576)
        side = self.sidebar.get_width() if self.sidebar.get_visible() else 0
        self.unfullscreen()
        self.set_default_size(int(size[0] * pct / 100) + side, int(size[1] * pct / 100) + 80)

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
            self.sidebar.set_visible(True)
            self.statusbar.set_visible(True)
        else:
            self.sidebar.set_visible(False)
            self.statusbar.set_visible(False)
            self.fullscreen()

    def toggle_list(self):
        self.sidebar.set_visible(not self.sidebar.get_visible())

    # ------------------------------------------------------------------ sleep timer
    def sleep_in(self, seconds):
        self.sleep_cancel()
        self.sleep_source = GLib.timeout_add_seconds(int(seconds), self._sleep_fire)
        self.set_status(f"Playback stops at {hm(time.time() + seconds)}")

    def sleep_after_programme(self):
        if self.current:
            now, _ = self.app.epg.now_next(self.current.epg_key)
            if now:
                self.sleep_in(max(1, now.end - time.time()))
                return
        self.set_status("No programme information for the sleep timer")

    def sleep_cancel(self):
        if self.sleep_source:
            GLib.source_remove(self.sleep_source)
            self.sleep_source = None

    def _sleep_fire(self):
        self.sleep_source = None
        self.stop_playback()
        self.set_status("Sleep timer: playback stopped")
        return False

    # ------------------------------------------------------------------ OSD / status
    def show_osd(self):
        c = self.current
        if not c:
            return
        now, nxt = self.app.epg.now_next(c.epg_key)
        self.osd_title.set_text(f"{c.number}  {c.name}")
        if now:
            self.osd_now.set_text(f"{hm(now.start)}–{hm(now.end)}  {now.title}"
                                  + (f" – {now.subtitle}" if now.subtitle else ""))
            self.osd_prog.set_fraction(min(1, max(0, (time.time() - now.start) / max(1, now.duration))))
            self.osd_prog.set_visible(True)
        else:
            self.osd_now.set_text("No programme information")
            self.osd_prog.set_visible(False)
        self.osd_next.set_text(f"Next: {hm(nxt.start)}  {nxt.title}" if nxt else "")
        self.osd.set_visible(True)
        if self._osd_timeout:
            GLib.source_remove(self._osd_timeout)
        self._osd_timeout = GLib.timeout_add_seconds(self.settings["osd_seconds"], self._hide_osd)

    def _hide_osd(self):
        self.osd.set_visible(False)
        self._osd_timeout = None
        return False

    def flash(self, text):
        self.numlabel.set_text(text)
        self.numlabel.set_visible(True)
        GLib.timeout_add(1200, lambda: self.numlabel.set_visible(False) or False)

    def set_status(self, text):
        self.status.set_text(text)

    def _on_stats(self, _cap, st):
        parts = []
        if "lock" in st:
            parts.append("LOCK" if st["lock"] else "no lock")
        if "signal" in st:
            parts.append(f"Signal {st['signal'] * 100 // 65535}%")
        if "snr" in st:
            parts.append(f"SNR {st['snr'] * 100 // 65535}%")
        if st.get("unc"):
            parts.append(f"UNC {st['unc']}")
        self.signal_label.set_text("  ".join(parts))

    def _on_epg(self):
        if self.osd.get_visible():
            self.show_osd()

    def _update_transport(self):
        self.pause_btn.set_icon_name("media-playback-start-symbolic" if self.player.paused
                                     else "media-playback-pause-symbolic")
        rec = self._is_recording()
        if self.rec_btn.get_active() != rec:
            self.rec_btn.set_active(rec)
        n = len(self.app.timers.active)
        self.rec_label.set_text(f"● REC {n}" if n else "")

    def _tick(self):
        if self.capture and self.player.capture:
            d = self.player.timeshift_delay()
            self.ts_label.set_text(f"Timeshift −{int(d) // 60}:{int(d) % 60:02d}" if d > 3 else "Live")
            if self.status.get_text().startswith("Tuning") and self.capture.store.bounds()[1] > 0:
                self.set_status(self.current.name)
        else:
            pos = self.player.position()
            self.ts_label.set_text(f"{_mmss(pos[0])} / {_mmss(pos[1])}" if pos else "")
        return True

    def on_web_search(self):
        if self.current:
            now, _ = self.app.epg.now_next(self.current.epg_key)
            if now:
                self.app.web_search(now.title, self)

    # ------------------------------------------------------------------ keys
    def _on_key(self, _ctl, keyval, _code, state):
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Text, Gtk.Entry, Gtk.SearchEntry)) or \
                state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        name = Gdk.keyval_name(keyval) or ""
        if name.isdigit() or name.startswith("KP_") and name[3:].isdigit():
            self.number_buffer += name[-1]
            self.flash(self.number_buffer)
            buf = self.number_buffer
            GLib.timeout_add(1500, lambda: self._number_commit(buf))
            return True
        acts = {"space": "pause", "f": "fullscreen", "Escape": None, "m": "mute",
                "e": "epg", "r": "record", "t": "teletext", "l": "live", "o": "osd",
                "Left": "back10", "Right": "fwd10", "Up": "ch-prev", "Down": "ch-next",
                "plus": "vol-up", "KP_Add": "vol-up", "minus": "vol-down",
                "KP_Subtract": "vol-down", "BackSpace": "last-channel", "s": "screenshot",
                "i": "osd", "n": "playlist-next", "p": "playlist-prev"}
        if name == "Escape" and self.is_fullscreen():
            self.toggle_fullscreen()
            return True
        if name in ("Up", "Down", "Left", "Right") and self.listview.get_focus_child():
            return False
        act = acts.get(name)
        if act:
            self.activate_action(f"win.{act}", None)
            return True
        return False

    def _number_commit(self, buf):
        if self.number_buffer == buf:
            self.number_buffer = ""
            self.zap_number(int(buf))
        return False

    # ------------------------------------------------------------------ misc
    def on_about(self):
        Gtk.AboutDialog(transient_for=self, program_name=APP_NAME, version="0.1",
                        comments="Watch and record DVB-S/S2, DVB-C, DVB-T/T2, ATSC and "
                                 "internet TV with the Linux DVB API and GStreamer.",
                        license_type=Gtk.License.GPL_2_0, logo_icon_name="lindvb").present()

    def _on_close(self, *_):
        self.settings.save()
        self._release_capture()
        return False


def _children(w):
    c = w.get_first_child()
    while c:
        yield c
        c = c.get_next_sibling()


def _mmss(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _read_playlists(paths):
    out = []
    for p in paths:
        for line in Path(p).read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("File") and "=" in line:
                line = line.split("=", 1)[1]
            if line and not line.startswith(("#", "[")) and "=" not in line[:8]:
                out.append(line if "://" in line or line.startswith("/") else str(Path(p).parent / line))
    return out
