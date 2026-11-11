# SPDX-License-Identifier: GPL-2.0-or-later
"""Main window: channel list, video area with OSD, transport controls, menus."""

import functools
import random
import time
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, GObject, Gst, Gtk, Pango

from .. import chansort, dvbi, logothumbs
from ..favourites import fav_index, filter_keys, filter_setting
from ..channels import import_file, is_lindvb_json, parse_lindvb_json
from ..config import APP_NAME, CACHE_DIR
from ..epg import details_line, programme_details
from ..media_info import audio_summary
from ..player import EQ_BANDS, Player
from ..library import Positions, delete_info
from ..recordings import find_recordings, sort_recordings
from ..signalq import Meter as QualityMeter, NONE as Q_NONE
from ..visuals import DEFAULT_STYLE, STYLES
from .common import Row, hm, message
from .theme import OSD_WIDTH, apply_accent, apply_display, apply_theme, resolve_accent
from .nowplaying import NowPlayingOverlay, overlay_enabled
from .visualizer import VisualizerArea
from ..i18n import _

NO_LOCK_SECONDS = 8          # DVB-I failover after this long without a signal lock


def _tuning_sig(c):
    return (c.delsys, c.url, c.frequency, c.polarity, c.sid)


def _instance_kind(c):
    if c.is_iptv:
        return "the internet stream"
    if c.is_sat:
        return "satellite"
    return "cable" if c.delsys.startswith("dvb-c") else "antenna"


def _short(text, n=90):
    text = (text or "").strip()
    if text.startswith(_("Playback error: ")):
        text = text[len(_("Playback error: ")):]
    text = text.split(". ")[0].rstrip(".")
    return text if len(text) <= n else text[:n - 1] + "…"

# The channel list shows either TV or radio channels; the favourites lists mix both.
FILTER_LABELS = {"all": _("All channels"), "fta": _("Free to air"), "enc": _("Encrypted"),
                 "hd": "HD channels", "iptv": _("Internet streams")}
MODES = ("tv", "radio")
# The list tabs: TV channels, radio stations, and the recordings on disk.
TABS = MODES + ("recordings",)
ZOOM = [(_("Fit"), Gtk.ContentFit.CONTAIN), (_("Fill (crop)"), Gtk.ContentFit.COVER),
        (_("Stretch"), Gtk.ContentFit.FILL)]
SLEEP_MINUTES = [15, 30, 45, 60, 90, 120, 180]
AUDIO_DELAY_STEP = 100      # ms per key press / menu click


def _menu_label(text):
    """Menu labels treat "_" as a mnemonic; folder names keep theirs."""
    return text.replace("_", "__")


def _vis_item(label, vis_id):
    item = Gio.MenuItem.new(_menu_label(label), None)
    item.set_action_and_target_value("win.vis", GLib.Variant.new_string(vis_id))
    return item

class _ChannelRow(Row):
    """A channel in the list, with its place in the chosen sort order. GTK
    sorts by that number itself, which is much faster than asking Python for
    every comparison."""
    rank = GObject.Property(type=int, default=0)


class MainWindow(Gtk.ApplicationWindow):
    mpris = None

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
        self._hide_source = None     # auto-hide of the controls in full screen
        self._seek_source = None     # debounced seek while dragging the seek bar
        self._seek_target = None
        self._fs_layout = False      # full screen layout applied (controls float)
        self.stopped = None          # channel switched off with Stop (Play resumes it)
        self._tuned = None           # the channel or DVB-I instance actually playing
        self._tried = set()          # DVB-I instances tried since the last channel change
        self._nolock_since = None
        self.quality = QualityMeter()  # the OSD's signal quality bar
        self.mode = self.settings.get("mode") if self.settings.get("mode") in MODES else "tv"
        self._mode_sync = False      # set while the TV/Radio buttons follow the code
        self.tab = self.mode         # list tab shown: "tv", "radio" or "recordings"
        # recording library: where each file was left, preview pictures
        self.mpris = None
        if self.settings.get("mpris", True):
            try:
                from ..mpris import MprisServer
                self.mpris = MprisServer(app, self)
            except Exception:       # noqa: BLE001 - no session bus: no media keys
                self.mpris = None
        self.positions = Positions()
        self._positions_dirty = False
        self._resume_at = None
        self.thumbnailer = None
        GLib.timeout_add_seconds(2, self._track_position)
        GLib.timeout_add_seconds(30, self._save_positions)
        self._vis_id = None          # visualisation style currently drawn

        # dark (the default), light, or the desktop's scheme (Preferences › Appearance)
        self.add_css_class("lindvb")
        apply_theme(self.settings)
        self._watch_color_scheme()
        # LinDVB's own symbolic icons (e.g. the visualisation sine wave)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
            str(Path(__file__).resolve().parent.parent / "icons"))
        # the LinDVB logo as window icon (it also ships in icons/…/apps)
        Gtk.Window.set_default_icon_name("lindvb")
        self.set_icon_name("lindvb")
        apply_accent(self.settings)
        apply_display(self.settings)

        # channel list: logos made in the background and kept on disk, the
        # rows showing them, the logo path per channel, DVB-I channels off air
        self._logos = logothumbs.Loader(CACHE_DIR / "list-logos",
                                        Gdk.Texture.new_from_filename, self._on_logo_ready)
        self._row_icons = {}
        self._row_labels = {}
        self._logo_paths = {}
        self._off_air = set()
        self._fstate = None
        self._build_actions()
        self._build_ui()
        self.player = Player(self.picture, self.settings)
        self.player.connect("error", lambda _p, m: self._on_play_error(_("Playback error: {m}").format(m=m)))
        self.player.connect("vis-error", self._on_vis_error)
        self.player.connect("hw-fallback", lambda _p, name: self.set_status(
            _("{name} video decoding failed, playing with the next decoder").format(name=name)))
        self.player.connect("tracks", lambda _p: (self._rebuild_track_menus(),
                                                  self._update_visualizer(),
                                                  self._update_teletext()))
        self.player.connect("state", lambda _p, _s: self._update_visualizer())
        self.vis.analysis = self.player.analysis
        self.player.connect("metadata", self._on_metadata)
        self.player.connect("subtitle-text", self._on_subtitle_text)
        # DVB-I pop-up channels come and go with their availability windows
        GLib.timeout_add_seconds(60, self._availability_tick)
        self._update_teletext()
        self.player.connect("eos", self._on_eos)
        self.player.connect("state", lambda _p, s: self._update_transport())
        # quality bar in the OSD for internet channels: the stream's health
        self.player.connect("buffering", self._on_stream_buffering)
        self.player.connect("state", self._on_stream_state)
        # no screen saver while something plays (Preferences › Playback)
        self._inhibit_cookie = 0
        self.player.connect("state", lambda _p, st: self.update_inhibit(st == "playing"))
        self.player.connect("error", lambda _p, _m: self.update_inhibit(False))
        self.player.connect("error", lambda _p, _m: self._on_stream_failed())
        self.app.tuners.connect("preempt-live", self._on_preempt)
        self.app.timers.connect("message", lambda _t, m: self.set_status(m))
        self.app.timers.connect("changed", lambda _t: self._update_transport())
        self.app.epg.listeners.append(self._on_epg)
        self.reload_channels()
        GLib.timeout_add(1000, self._tick)
        self.connect("close-request", self._on_close)
        self.connect("notify::fullscreened", self._on_fullscreened)

    # ------------------------------------------------------------------ actions
    def _build_actions(self):
        acts = {
            "open-file": self.on_open_file, "open-url": self.on_open_url,
            "import": self.on_import, "export": self.on_export,
            "export-epg": self.on_export_epg,
            "epg": lambda: self.app.show_epg(), "timers": lambda: self.app.show_timers(),
            "scan": lambda: self.app.show_scan(), "prefs": lambda: self.app.show_prefs(),
            "editor": lambda: self.app.show_editor(), "stats": lambda: self.app.show_stats(),
            "favourites": lambda: self.app.show_favourites(),
            "analysis": lambda: self.app.show_analysis(),
            "teletext": self.on_teletext, "record": self.on_record,
            "hbbtv": lambda: self.hbbtv.red_button(),
            "record-options": self.on_record_options,
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
            "xmltv-match": lambda: self.app.show_xmltv_match(),
            "dvbi-setup": lambda: self.app.show_dvbi_setup(),
            "web-epg": self.on_web_search,
            "plugins": lambda: self.app.show_plugins(),
            "cam-menu": lambda: self.app.show_cam_menu(),
            "prev-item": lambda: self.step_item(-1), "next-item": lambda: self.step_item(1),
            "audio-later": lambda: self.shift_audio(AUDIO_DELAY_STEP),
            "audio-earlier": lambda: self.shift_audio(-AUDIO_DELAY_STEP),
            "audio-delay-reset": lambda: self.shift_audio(None),
            "toggle-mode": lambda: self.set_mode(self.mode if self.tab == "recordings" else
                                                 "tv" if self.tab == "radio" else "radio"),
            "mode-tv": lambda: self.set_mode("tv"), "mode-radio": lambda: self.set_mode("radio"),
            "mode-recordings": lambda: self.set_mode("recordings"),
            "rec-refresh": self.reload_recordings, "rec-folder": self.open_recording_folder,
            "vis-next": self.cycle_visualizer,
            "pip": self.pip_selected, "pip-swap": self.pip_swap, "pip-close": self.pip_close,
            "mosaic": self.show_mosaic, "shortcuts": self.show_shortcuts,
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
        # radio visualisations: built-in styles and those plugins registered
        self.visualizers = {s.id: (_(s.label), "draw", s) for s in STYLES}
        # a plugin's choices (e.g. preset folders) get ids "plugin:name|choice"
        self._vis_choices = {}
        choice_fns = getattr(self.app.plugins, "visualizer_choices", {})
        for _plugin, name, kind, factory in self.app.plugins.visualizers:
            vid = f"plugin:{name}"
            self.visualizers[vid] = (name, kind, factory)
            if name not in choice_fns:
                continue
            try:
                choices = [str(c) for c in choice_fns[name]()]
            except Exception as e:
                print(f"{name}: no choices ({e})")
                choices = []
            self._vis_choices[vid] = choices
            for c in choices:
                self.visualizers[f"{vid}|{c}"] = (f"{name}: {c}", kind, functools.partial(factory, c))
        if self.settings.get("visualizer") == "plugin:GOOM" and "plugin:GOOM" not in self.visualizers:
            self.settings["visualizer"] = "goomhd"      # the plain GOOM add-on was taken out
        if self.settings.get("visualizer") not in self.visualizers:
            self.settings["visualizer"] = DEFAULT_STYLE
        a = Gio.SimpleAction.new_stateful("vis", GLib.VariantType.new("s"),
                                          GLib.Variant.new_string(self.settings["visualizer"]))
        a.connect("activate", lambda _a, p: self.set_visualizer(p.get_string()))
        self.add_action(a)
        a = Gio.SimpleAction.new("plugin-action", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, p: self._run_plugin_action(p.get_int32()))
        self.add_action(a)
        for name, init, fn in (("video-off", False, lambda v: self.player.disable_video(v)),
                               ("closed-captions", bool(self.settings.get("closed_captions")),
                                lambda v: self.player.set_closed_captions(v)),
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
            "win.toggle-mode": ["<Ctrl>m"], "win.shortcuts": ["<Ctrl>question"],
        }
        for k, v in accels.items():
            self.app.set_accels_for_action(k, v)

    def _run_plugin_action(self, index):
        try:
            _plugin, _label, cb = self.app.plugins.menu_actions[index]
            cb(self.app)
        except Exception as e:
            self.set_status(_("Plugin action failed: {e}").format(e=e))

    def _menu(self):
        m = Gio.Menu()
        file = Gio.Menu()
        file.append(_("Open Media File…"), "win.open-file")
        file.append(_("Open URL…"), "win.open-url")
        file.append(_("Import Channel List…"), "win.import")
        file.append(_("Export Channel List…"), "win.export")
        m.append_section(None, file)
        # programme guide and the channel's interactive service, second group
        guide = Gio.Menu()
        guide.append(_("Programme Guide (EPG)"), "win.epg")
        guide.append(_("Update Internet EPG (XMLTV)"), "win.xmltv")
        guide.append(_("Export Programme Guide (XMLTV)…"), "win.export-epg")
        guide.append(_("HbbTV (red button)"), "win.hbbtv")
        m.append_section(None, guide)
        tv = Gio.Menu()
        tv.append(_("Switch TV / Radio"), "win.toggle-mode")
        tv.append(_("Recordings"), "win.mode-recordings")
        tv.append(_("Timer Recordings"), "win.timers")
        tv.append(_("Favourites Lists…"), "win.favourites")
        tv.append(_("Record with Options…"), "win.record-options")
        tv.append(_("Recording and Device Statistics"), "win.stats")
        tv.append(_("Channel Analysis…"), "win.analysis")
        tv.append(_("Teletext"), "win.teletext")
        tv.append(_("CAM Menu (CI Module)…"), "win.cam-menu")
        # channel search, channel editor and DVB-I live in the preferences
        # (Tuner and Channels, Internet); their shortcuts still work
        m.append_section(None, tv)

        play = Gio.Menu()
        self.audio_menu = Gio.Menu()
        self.sub_menu = Gio.Menu()
        play.append_submenu(_("Audio Track"), self.audio_menu)
        play.append_submenu(_("Subtitles"), self.sub_menu)
        rate = Gio.Menu()
        for r in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            rate.append(f"{r:g}×", f"win.rate({r})")
        play.append_submenu(_("Play Rate"), rate)
        jump = Gio.Menu()
        jump.append(_("Back 10 s"), "win.back10")
        jump.append(_("Forward 10 s"), "win.fwd10")
        jump.append(_("Back 1 min"), "win.back60")
        jump.append(_("Forward 1 min"), "win.fwd60")
        jump.append(_("Back to Live"), "win.live")
        play.append_submenu(_("Jump"), jump)
        pl = Gio.Menu()
        pl.append(_("Next"), "win.playlist-next")
        pl.append(_("Previous"), "win.playlist-prev")
        pl.append(_("Random Order"), "win.pl-random")
        pl.append(_("Endless Playback"), "win.pl-repeat")
        play.append_submenu(_("Playlist"), pl)
        play.append(_("Keep Timeshift as Recording"), "win.keep-timeshift")
        play.append(_("Rebuild Playback"), "win.rebuild")
        play.append(_("Video Off (audio only)"), "win.video-off")
        pip = Gio.Menu()
        pip.append(_("Show Selected Channel"), "win.pip")
        pip.append(_("Swap Pictures"), "win.pip-swap")
        pip.append(_("Close"), "win.pip-close")
        play.append_submenu(_("Picture in Picture"), pip)
        play.append(_("Channel Mosaic…"), "win.mosaic")
        sleep = Gio.Menu()
        for mins in SLEEP_MINUTES:
            sleep.append(_("Stop in {mins} min").format(mins=mins), f"win.sleep-{mins}")
        sleep.append(_("Stop after current programme"), "win.sleep-programme")
        sleep.append(_("Cancel"), "win.sleep-off")
        play.append_submenu(_("Sleep Timer"), sleep)
        delay = Gio.Menu()
        delay.append(_("Sound Later (+{AUDIO_DELAY_STEP} ms, K)").format(AUDIO_DELAY_STEP=AUDIO_DELAY_STEP), "win.audio-later")
        delay.append(_("Sound Earlier (−{AUDIO_DELAY_STEP} ms, J)").format(AUDIO_DELAY_STEP=AUDIO_DELAY_STEP), "win.audio-earlier")
        delay.append(_("Reset"), "win.audio-delay-reset")
        play.append_submenu(_("Audio Delay (this channel)"), delay)
        m.append_section(None, play)

        view = Gio.Menu()
        view.append(_("Full Screen"), "win.fullscreen")
        view.append(_("Show/Hide Channel List"), "win.toggle-list")
        zoom = Gio.Menu()
        for i, (name, _f) in enumerate(ZOOM):
            zoom.append(name, f"win.zoom({i})")
        view.append_submenu(_("Aspect / Zoom"), zoom)
        ws = Gio.Menu()
        for p in (50, 75, 100, 200):
            ws.append(f"{p}%", f"win.winsize({p})")
        view.append_submenu(_("Window Size"), ws)
        view.append(_("Picture Settings…"), "win.picture")
        view.append(_("Equalizer…"), "win.equalizer")
        # shared with the visualisation button in the control bar
        self.vis_menu = vis = Gio.Menu()
        for vid, (label, _kind, _f) in self.visualizers.items():
            if "|" in vid:
                continue
            if self._vis_choices.get(vid):
                vis.append_submenu(_menu_label(label), self._vis_choice_menu(vid))
            else:
                vis.append_item(_vis_item(label, vid))
        view.append_submenu(_("Radio Visualisation"), vis)
        view.append(_("Screenshot"), "win.screenshot")
        m.append_section(None, view)
        # the gear in the control bar shows just the playback and view sections
        self.settings_menu = Gio.Menu()
        self.settings_menu.append_section(None, play)
        self.settings_menu.append_section(None, view)
        end = Gio.Menu()
        end.append(_("Preferences"), "win.prefs")
        end.append(_("Keyboard Shortcuts"), "win.shortcuts")
        end.append(_("Plugins…"), "win.plugins")
        for i, (_plugin, label, _cb) in enumerate(self.app.plugins.menu_actions):
            end.append(label, f"win.plugin-action({i})")
        end.append(_("About LinDVB"), "win.about")
        m.append_section(None, end)
        return m

    # ------------------------------------------------------------------ layout
    def _build_ui(self):
        menu_model = self._menu()

        # slim header: channel list toggle, title/now playing, guide and main menu
        hb = Gtk.HeaderBar()
        self.set_titlebar(hb)
        self.list_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", active=True,
                                         tooltip_text=_("Show/hide channel list (F9)"))
        self.list_btn.connect("toggled", lambda b: self.sidebar.set_visible(b.get_active()))
        hb.pack_start(self.list_btn)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title_label = Gtk.Label(label=APP_NAME, css_classes=["title"],
                                     ellipsize=Pango.EllipsizeMode.END)
        self.subtitle_label = Gtk.Label(css_classes=["subtitle"], ellipsize=Pango.EllipsizeMode.END,
                                        visible=False)
        title_box.append(self.title_label)
        title_box.append(self.subtitle_label)
        hb.set_title_widget(title_box)
        menu = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu_model,
                              tooltip_text=_("Main menu (F10)"))
        menu.set_primary(True)
        hb.pack_end(menu)
        hb.pack_end(Gtk.Button(icon_name="x-office-calendar-symbolic", action_name="win.epg",
                               tooltip_text=_("Programme guide (Ctrl+E)")))
        self.ttx_btn = Gtk.Button(icon_name="lindvb-teletext-symbolic", action_name="win.teletext",
                                  tooltip_text=_("Teletext (Ctrl+Shift+T)"))
        hb.pack_end(self.ttx_btn)
        # HbbTV: like the red button of a remote control
        # like the TXT button: always there, greyed out when the channel has none
        self.hbbtv_btn = Gtk.Button(label=_("HbbTV"), action_name="win.hbbtv",
                                    css_classes=["lv-hbbtv"],
                                    tooltip_text=_("HbbTV: not available on this channel"))
        self.lookup_action("hbbtv").set_enabled(False)
        hb.pack_end(self.hbbtv_btn)

        # sidebar
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["lv-sidebar"])
        side.set_size_request(260, -1)
        head = Gtk.Box(spacing=8, margin_start=10, margin_end=10, margin_top=12)
        # TV / Radio / Recordings tabs: the list shows one kind at a time
        seg = Gtk.Box(css_classes=["linked", "lv-mode"], hexpand=True)
        self.mode_btns = {}
        group = None
        for mode, label, icon, tip in (
                ("tv", "TV", "video-display-symbolic", _("Show TV channels (Ctrl+M switches)")),
                ("radio", _("Radio"), "audio-x-generic-symbolic",
                 _("Show radio stations (Ctrl+M switches)")),
                ("recordings", _("Recordings"), "media-record-symbolic",
                 _("Show your recordings (double-click plays one)"))):
            content = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            content.append(Gtk.Image(icon_name=icon))
            content.append(Gtk.Label(label=label))
            b = Gtk.ToggleButton(child=content, active=mode == self.tab, hexpand=True,
                                 tooltip_text=tip)
            if group is None:
                group = b
            else:
                b.set_group(group)
            b.connect("toggled", self._on_mode_toggled, mode)
            self.mode_btns[mode] = b
            seg.append(b)
        head.append(seg)
        side.append(head)
        srow = Gtk.Box(spacing=8, margin_start=10, margin_end=10)
        self.search = Gtk.SearchEntry(placeholder_text=_("Search name or number"), hexpand=True)
        self.search.connect("search-changed", lambda _e: self._refilter())
        self.search.connect("activate", lambda _e: self._activate_first())
        srow.append(self.search)
        self.count_label = Gtk.Label(xalign=1, css_classes=["lv-count"])
        srow.append(self.count_label)
        side.append(srow)
        # the channel page (filters + list) and the recordings page share the search
        self.side_stack = Gtk.Stack(vexpand=True,
                                    transition_type=Gtk.StackTransitionType.CROSSFADE)
        chan_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(spacing=8, margin_start=10, margin_end=10)
        # the list filter and group last used come back at the next start
        # (the favourites lists are entries of the filter, see _rebuild_filters)
        self.filter_dd = Gtk.DropDown.new_from_strings([FILTER_LABELS["all"]])
        self._filters = ["all"]
        self._filter_sync = False
        self._list_fav = None        # favourites list the store holds (None: all channels)
        self._fav_pos = {}           # id(channel) -> place in that list (1, 2, ...)
        self._rebuild_filters()
        self.filter_dd.connect("notify::selected", lambda *_: self._on_list_filter())
        self.filter_dd.set_hexpand(True)
        self.filter_dd.set_tooltip_text(_("Filter; the favourites lists show TV and radio"))
        self._group_sync = False     # set while reload_channels rebuilds the group list
        self.group_dd = Gtk.DropDown.new_from_strings([_("All groups")])
        self.group_dd.connect("notify::selected", lambda *_: self._on_list_group())
        self.group_dd.set_hexpand(True)
        row.append(self.filter_dd)
        favs_btn = Gtk.Button(icon_name="starred-symbolic", css_classes=["flat"],
                              tooltip_text=_("Manage favourites lists"))
        favs_btn.update_property([Gtk.AccessibleProperty.LABEL], [_("Manage favourites lists")])
        favs_btn.connect("clicked", lambda *_: self.app.show_favourites())
        row.append(favs_btn)
        row.append(self.group_dd)
        chan_page.append(row)
        # sort order, and for grouping orders (provider …) one value to show
        row2 = Gtk.Box(spacing=8, margin_start=10, margin_end=10)
        self._sorts = [k for k, _l in chansort.SORTS]
        self.sort_dd = Gtk.DropDown.new_from_strings([lbl for _k, lbl in chansort.SORTS])
        self.sort_dd.set_tooltip_text(_("Sort by"))
        srt = self.settings.get("list_sort", "number")
        self.sort_dd.set_selected(self._sorts.index(srt) if srt in self._sorts else 0)
        self.sort_dd.set_hexpand(True)
        self._values = [""]
        self._value_sync = False
        self.value_dd = Gtk.DropDown.new_from_strings([_("All")])
        self.value_dd.set_hexpand(True)
        row2.append(self.sort_dd)
        row2.append(self.value_dd)
        chan_page.append(row2)

        self.store = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self._filter_model = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.filtered = Gtk.SortListModel(model=self._filter_model)
        self.sort_dd.connect("notify::selected", lambda *_: self._on_list_sort())
        self.value_dd.connect("notify::selected", lambda *_: self._on_list_value())
        self.selection = Gtk.SingleSelection(model=self.filtered, autoselect=False)
        fac = Gtk.SignalListItemFactory()
        fac.connect("setup", self._row_setup)
        fac.connect("bind", self._row_bind)
        fac.connect("unbind", self._row_unbind)
        self.listview = Gtk.ListView(model=self.selection, factory=fac)
        self.listview.set_single_click_activate(False)
        self.listview.connect("activate", lambda _l, pos: self.zap(self.filtered.get_item(pos).data))
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._list_context)
        self.listview.add_controller(click)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.listview)
        chan_page.append(sw)
        self.side_stack.add_named(chan_page, "channels")
        self.side_stack.add_named(self._build_recordings_page(), "recordings")
        self.side_stack.set_visible_child_name(
            "recordings" if self.tab == "recordings" else "channels")
        side.append(self.side_stack)
        self.sidebar = side

        # video + overlays (OSD, number entry, teletext subtitles)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, hexpand=True, vexpand=True)
        self.picture.add_css_class("video")
        self.overlay = overlay = Gtk.Overlay()
        overlay.set_child(self.picture)
        # radio / audio-only: drawn visualisation over the (empty) picture
        self.vis = VisualizerArea(None, gpu=self.settings.get("visualizer_gpu", True))
        self.vis.set_visible(False)
        overlay.add_overlay(self.vis)
        # station logo, cover, song and clock over the visualisation
        self.nowplaying = NowPlayingOverlay(self.settings)
        self.nowplaying.set_visible(False)
        overlay.add_overlay(self.nowplaying)
        self.osd = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                           valign=Gtk.Align.END, halign=Gtk.Align.START,
                           margin_start=24, margin_end=24, margin_bottom=24,
                           width_request=self._osd_width())
        self.osd.add_css_class("osd")
        top = Gtk.Box(spacing=10)
        self.osd_num = Gtk.Label(css_classes=["lv-osd-num"], valign=Gtk.Align.CENTER)
        self.osd_title = Gtk.Label(xalign=0, css_classes=["title"], hexpand=True,
                                   ellipsize=Pango.EllipsizeMode.END)
        self.osd_clock = Gtk.Label(css_classes=["lv-osd-time"])
        for w in (self.osd_num, self.osd_title, self.osd_clock):
            top.append(w)
        self.osd_now = Gtk.Label(xalign=0, css_classes=["lv-osd-now"], ellipsize=Pango.EllipsizeMode.END)
        # genre, country, year of the programme on air, and what audio is on offer
        self.osd_meta_row = meta = Gtk.Box(spacing=6)
        self.osd_meta = Gtk.Label(xalign=0, hexpand=True, css_classes=["lv-osd-meta"],
                                  ellipsize=Pango.EllipsizeMode.END)
        self.osd_tags = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        meta.append(self.osd_meta)
        meta.append(self.osd_tags)
        self.osd_prog = Gtk.ProgressBar()
        self.osd_next = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["lv-osd-next"])
        # quality of service: empty without signal, red / orange / yellow / green
        self.osd_quality = Gtk.ProgressBar(css_classes=["lv-osd-quality"])
        for w in (self.osd_quality, top, self.osd_now, meta, self.osd_prog, self.osd_next):
            self.osd.append(w)
        self.osd.set_visible(False)
        overlay.add_overlay(self.osd)
        self.numlabel = Gtk.Label(css_classes=["osd-num"], halign=Gtk.Align.END,
                                  valign=Gtk.Align.START, margin_top=20, margin_end=20)
        self.numlabel.set_visible(False)
        overlay.add_overlay(self.numlabel)
        self.sublabel = Gtk.Label(css_classes=["ttxsub"], halign=Gtk.Align.CENTER,
                                  valign=Gtk.Align.END, margin_bottom=36, justify=Gtk.Justification.CENTER,
                                  wrap=True, can_target=False)
        self.sublabel.set_visible(False)
        overlay.add_overlay(self.sublabel)
        # DVB-I: banner of a channel that is not on air (IT-4, pop-up channels)
        self.oos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, can_target=False)
        self.oos_image = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN,
                                     width_request=480, height_request=270)
        self.oos_label = Gtk.Label(css_classes=["title-2"], wrap=True,
                                   justify=Gtk.Justification.CENTER)
        self.oos.append(self.oos_image)
        self.oos.append(self.oos_label)
        self.oos.set_visible(False)
        overlay.add_overlay(self.oos)
        # LinDVB logo, faint in the middle of the empty picture while nothing plays
        self.idle_logo = Gtk.Image(icon_name="lindvb", pixel_size=160, opacity=0.18,
                                   halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                   can_target=False, css_classes=["lv-idle-logo"])
        overlay.add_overlay(self.idle_logo)
        from .pip import PipOverlay
        self.pip = PipOverlay(self)
        overlay.add_overlay(self.pip)
        from .hbbtv import HbbTV
        self.hbbtv = HbbTV(self)
        self.hbbtv.on_state = self._on_hbbtv_state
        overlay.connect("get-child-position", self._place_overlay_child)
        dbl = Gtk.GestureClick()
        dbl.connect("pressed", lambda g, n, *_: self.toggle_fullscreen() if n == 2 else self.show_osd())
        self.picture.add_controller(dbl)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", lambda _c, _dx, dy: (self.change_volume(-0.05 * dy), True)[1])
        self.picture.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_pointer_motion)
        overlay.add_controller(motion)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["lv-paned"])
        self.paned.set_start_child(side)
        self.paned.set_end_child(overlay)
        self.paned.set_position(300)
        self.paned.set_shrink_start_child(False)
        self.paned.set_resize_start_child(False)

        # bottom control bar (floats over the picture in full screen)
        self.controls = self._build_controls()
        self.controls_revealer = Gtk.Revealer(child=self.controls, reveal_child=True,
                                              transition_type=Gtk.RevealerTransitionType.CROSSFADE,
                                              transition_duration=200)

        # status strip
        self.statusbar = Gtk.Box(spacing=16, css_classes=["statusbar"])
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.signal_label = Gtk.Label(css_classes=["lv-signal"])
        self.ts_label = Gtk.Label()
        self.rec_label = Gtk.Label(css_classes=["rec"])
        for w in (self.status, self.ts_label, self.rec_label, self.signal_label):
            self.statusbar.append(w)

        self.main_box = box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.paned)
        box.append(self.controls_revealer)
        box.append(self.statusbar)
        self.set_child(box)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def _build_controls(self):
        bar = Gtk.Box(spacing=8, css_classes=["lv-controls"], valign=Gtk.Align.CENTER)

        def btn(icon, action, tip, *classes):
            return Gtk.Button(icon_name=icon, action_name=action, tooltip_text=tip,
                              css_classes=list(classes), valign=Gtk.Align.CENTER)

        def menubtn(icon, model, tip):
            mb = Gtk.MenuButton(icon_name=icon, menu_model=model, tooltip_text=tip,
                                valign=Gtk.Align.CENTER, direction=Gtk.ArrowType.UP)
            return mb

        def sep():
            return Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-sep"])

        # transport
        self.prev_btn = btn("media-skip-backward-symbolic", "win.prev-item", _("Previous channel (Page Down)"))
        self.pause_btn = btn("media-playback-pause-symbolic", "win.pause",
                             _("Pause / timeshift (Space)"), "lv-play")
        self.stop_btn = btn("media-playback-stop-symbolic", "win.stop", _("Stop (switch the channel off)"))
        self.next_btn = btn("media-skip-forward-symbolic", "win.next-item", _("Next channel (Page Up)"))
        self.rec_btn = Gtk.ToggleButton(icon_name="media-record-symbolic", valign=Gtk.Align.CENTER,
                                        tooltip_text=_("Record current channel (Ctrl+R)"),
                                        css_classes=["lv-rec"])
        self.rec_btn.connect("clicked", lambda b: self.on_record() if b.get_active() !=
                             self._is_recording() else None)
        rec_menu = Gio.Menu()
        rec_menu.append(_("Record with Options…"), "win.record-options")
        self.rec_more = Gtk.MenuButton(menu_model=rec_menu, valign=Gtk.Align.CENTER,
                                       direction=Gtk.ArrowType.UP, css_classes=["lv-rec-more"],
                                       tooltip_text=_("Choose format and tracks for this recording"))
        rec_box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        rec_box.append(self.rec_btn)
        rec_box.append(self.rec_more)
        for w in (self.prev_btn, self.pause_btn, self.stop_btn, self.next_btn, rec_box):
            bar.append(w)

        # seek bar: file position, or the timeshift buffer for live TV
        self.pos_label = Gtk.Label(label="--:--", css_classes=["lv-time"], xalign=1)
        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.set_draw_value(False)
        self.seek.set_hexpand(True)
        self.seek.set_sensitive(False)
        self.seek.add_css_class("lv-seek")
        self.seek.set_tooltip_text(_("Seek"))
        self.seek.connect("change-value", self._on_seek_changed)
        self.dur_label = Gtk.Label(label="--:--", css_classes=["lv-time"], xalign=0)
        self.live_btn = Gtk.Button(label=_("LIVE"), action_name="win.live", valign=Gtk.Align.CENTER,
                                   tooltip_text=_("Back to live (L)"), css_classes=["lv-live"], visible=False)
        for w in (self.pos_label, self.seek, self.dur_label, self.live_btn):
            bar.append(w)
        bar.append(sep())

        # volume
        self.mute_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic", tooltip_text=_("Mute (M)"),
                                         valign=Gtk.Align.CENTER)
        self.mute_btn.connect("toggled", lambda b: self._set_mute(b.get_active()))
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1.5, 0.05)
        self.vol.set_size_request(96, -1)
        self.vol.set_draw_value(False)
        self.vol.add_css_class("lv-volume")
        self.vol.set_tooltip_text(_("Volume"))
        self.vol.set_value(self.settings["volume"])
        self.vol.connect("value-changed", lambda s: self._set_volume(s.get_value()))
        bar.append(self.mute_btn)
        bar.append(self.vol)
        bar.append(sep())

        # tracks, settings, full screen
        bar.append(menubtn("audio-x-generic-symbolic", self.audio_menu, _("Audio track")))
        bar.append(menubtn("media-view-subtitles-symbolic", self.sub_menu, _("Subtitles")))
        bar.append(menubtn("emblem-system-symbolic", self.settings_menu, _("Playback and picture settings")))
        # radio visualisation picker: only usable while radio / audio is playing
        self.vis_btn = menubtn("lindvb-sine-wave-symbolic", self.vis_menu, "")
        self._set_vis_button(False)
        bar.append(self.vis_btn)
        self.fs_btn = btn("view-fullscreen-symbolic", "win.fullscreen", _("Full screen (F)"))
        bar.append(self.fs_btn)
        self._update_volume_icon()
        return bar

    # ------------------------------------------------------------------ channel list
    def _row_setup(self, _f, li):
        b = Gtk.Box(spacing=10)
        num = Gtk.Label(width_chars=4, xalign=1, css_classes=["lv-num"])
        icon = Gtk.Image(css_classes=["lv-chan-icon"])
        lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                        has_tooltip=True)
        # the tooltip (with the programme on air) is made only when it is shown
        lbl.connect("query-tooltip", self._row_tooltip)
        extra = Gtk.Image(css_classes=["lv-badge"])
        for w in (num, icon, lbl, extra):
            b.append(w)
        # in a favourites list the channels can be dragged into another order
        b.chan = None
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", self._row_drag_prepare, b)
        b.add_controller(drag)
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("drop", self._row_drop, b)
        b.add_controller(drop)
        li.set_child(b)

    def _row_bind(self, _f, li):
        c = li.get_item().data
        li.get_child().chan = c
        num, icon, lbl, extra = list(_children(li.get_child()))
        pos = self._fav_pos.get(id(c)) if self._list_fav is not None else None
        num.set_text(str(pos) if pos else c.vchannel or str(c.number))
        path = self._list_logo(c)
        self._row_icons[icon] = (c, path)
        logo, pending = None, False
        if path:
            known, logo = self._logos.get(path)     # not ready yet: comes in later
            pending = not known
        self._set_row_icon(icon, c, logo, pending)
        self._row_labels[lbl] = c
        lbl.set_text(c.name)
        extra.set_from_icon_name("starred-symbolic" if c.favourite else
                                 "channel-secure-symbolic" if c.encrypted else None)
        if c.favourite:
            extra.add_css_class("lv-fav")
        else:
            extra.remove_css_class("lv-fav")

    def _row_unbind(self, _f, li):
        li.get_child().chan = None
        _num, icon, lbl, _extra = list(_children(li.get_child()))
        self._row_icons.pop(icon, None)
        self._row_labels.pop(lbl, None)

    def _set_row_icon(self, icon, c, logo, pending=False):
        # while a logo is on its way the row already takes the logo's height,
        # so it does not jump when the logo comes in
        icon.set_size_request(*((32, 32) if pending else (-1, -1)))
        if logo is not None:
            icon.set_from_paintable(logo)
            icon.set_pixel_size(32)
        else:
            icon.set_pixel_size(-1)
            icon.set_from_icon_name("audio-x-generic-symbolic" if c.radio else
                                    "network-wireless-symbolic" if c.is_iptv else
                                    "video-display-symbolic")

    def _on_logo_ready(self, path, tex):
        """A logo came in from the background: show it in the rows that want it."""
        for icon, (c, p) in self._row_icons.items():
            if p == path:
                self._set_row_icon(icon, c, tex)

    def _row_tooltip(self, lbl, _x, _y, _kb, tooltip):
        c = self._row_labels.get(lbl)
        if c is None:
            return False
        now, _unused = self.app.epg.now_next(c.epg_key)
        tooltip.set_text(f"{c.root} / {c.category}" +
                         (_("\nNow: {title}").format(title=now.title) if now else ""))
        return True

    def _list_logo(self, c):
        """_channel_logo, remembered per channel for the list (the picon folder
        is looked at again after half a minute, or when the list is reloaded)."""
        stamp = (self.settings.get("logo_dir") or "", c.logo, int(time.monotonic()) // 30)
        hit = self._logo_paths.get(id(c))
        if hit is not None and hit[0] == stamp:
            return hit[1]
        path = self._channel_logo(c)
        self._logo_paths[id(c)] = (stamp, path)
        return path

    def _channel_logo(self, c):
        """The channel's logo file: a picon or logo from the logo folder, else
        the DVB-I logo, else ""."""
        from .. import picons
        from .nowplaying import DEFAULT_LOGO_DIR
        folders = [self.settings.get("logo_dir") or str(DEFAULT_LOGO_DIR), str(DEFAULT_LOGO_DIR)]
        return picons.find(c, dict.fromkeys(folders)) or c.logo or ""

    def _filter_state(self):
        """What the list is filtered by, read from the widgets."""
        g, v = self.group_dd.get_selected(), self.value_dd.get_selected()
        raw = self.search.get_text().strip()
        return (raw, raw.lower(), self._filter_key(), self.mode == "radio",
                self._groups[g] if g > 0 else None,
                self._values[v] if 0 < v < len(self._values) else None, self._sort())

    def _filter_func(self, item):
        # _refilter reads the widgets once for the whole list, GTK asking for
        # single new rows reads them here
        raw, q, f, radio, group, value, srt = self._fstate or self._filter_state()
        c = item.data
        if not c.visible or c.dvbi_periods and id(c) in self._off_air:
            # DVB-I: hidden services and pop-up channels off air (IT-4), still
            # tunable by number
            return bool(raw) and raw.isdigit() and str(c.number) == raw
        fav = f.startswith("fav:")
        if not fav and c.radio != radio:
            return False             # (a favourites list: the store holds just its channels)
        if f == "fta" and c.encrypted:
            return False
        if f == "enc" and not c.encrypted:
            return False
        if f == "hd" and "HD" not in c.name and "HEVC" not in c.name and "UHD" not in c.name:
            return False
        if f == "iptv" and not c.is_iptv:
            return False
        if group is not None and group != c.root:
            return False
        if value is not None and chansort.group_value(c, srt) != value:
            return False
        if q:
            if q.isdigit():
                if fav:              # a favourites list is numbered 1, 2, 3 ...
                    return str(self._fav_pos.get(id(c), "")).startswith(q)
                return str(c.number).startswith(q)
            if c.vchannel and q.replace("-", ".").replace(".", "").isdigit():
                return c.vchannel.startswith(q.replace("-", "."))      # ATSC "7.1"
            return q in c.name.lower()
        return True

    def reload_channels(self):
        self.app.favs.sync()
        self.app.epg.relink()
        self._logo_paths.clear()
        self._off_air = self._channels_off_air()
        self._fill_store()
        self._groups = [_("All groups")] + self.app.db.roots()
        self._group_sync = True
        self.group_dd.set_model(Gtk.StringList.new(self._groups))
        group = self.settings.get("list_group") or ""
        self.group_dd.set_selected(self._groups.index(group) if group in self._groups[1:] else 0)
        self._group_sync = False
        self._update_sort()
        self._refilter()
        if self.current is not None:
            self._select_in_list(self.current)

    def _sort(self):
        i = self.sort_dd.get_selected()
        return self._sorts[i] if 0 <= i < len(self._sorts) else "number"

    def _rank_rows(self, srt):
        """Number the rows in the order of *srt* for the sorter."""
        rows = [self.store.get_item(i) for i in range(self.store.get_n_items())]
        rows.sort(key=lambda r: chansort.sort_key(r.data, srt), reverse=srt == "za")
        for i, r in enumerate(rows):
            r.rank = i

    def _update_sort(self):
        """Apply the sort order and offer the values of a grouping order."""
        srt = self._sort()
        sorter = None
        if srt != "number":
            self._rank_rows(srt)
            # a new sorter each time: GTK keeps the numbers of a sorter it knows
            sorter = Gtk.NumericSorter.new(
                Gtk.PropertyExpression.new(_ChannelRow.__gtype__, None, "rank"))
        self.filtered.set_sorter(sorter)
        grouped = srt in chansort.GROUPED
        keep = self.settings.get("list_sort_value", "")
        self._values = [""] + (chansort.values(self.app.db.channels, srt) if grouped else [])
        self._value_sync = True
        self.value_dd.set_model(Gtk.StringList.new([_("All")] + self._values[1:]))
        self.value_dd.set_selected(self._values.index(keep) if keep in self._values[1:] else 0)
        self._value_sync = False
        self.value_dd.set_visible(grouped)

    def _on_list_sort(self):
        self.settings["list_sort"] = self._sort()
        self.settings["list_sort_value"] = ""
        self._update_sort()
        self._refilter()

    def _on_list_value(self):
        if not self._value_sync:
            v = self.value_dd.get_selected()
            self.settings["list_sort_value"] = (self._values[v] if 0 < v < len(self._values)
                                                else "")
            self._refilter()

    # -- the filter and the favourites lists
    def _filter_key(self):
        i = self.filter_dd.get_selected()
        return self._filters[i] if 0 <= i < len(self._filters) else "all"

    def _rebuild_filters(self):
        """The filter entries with one per favourites list; the filter in the
        settings stays chosen."""
        favs = self.app.favs
        self._filters = filter_keys(len(favs))
        labels = [FILTER_LABELS.get(k) or "★ " + favs.names()[fav_index(k)]
                  for k in self._filters]
        key = filter_setting(self.settings.get("list_filter"))
        if key not in self._filters:
            key = "all"
        self._filter_sync = True
        self.filter_dd.set_model(Gtk.StringList.new(labels))
        self.filter_dd.set_selected(self._filters.index(key))
        self._filter_sync = False

    def current_fav_list(self):
        """The favourites list shown, else the one used last (or None)."""
        favs = self.app.favs
        i = fav_index(self._filter_key())
        if i is None:
            i = self.settings.get("fav_last", 0)
        return i if favs.valid(i) else (0 if len(favs) else None)

    def _fill_store(self):
        """All channels, or the channels of the favourites list chosen in the
        list's own order."""
        i = fav_index(self._filter_key())
        favs = self.app.favs
        if i is not None and favs.valid(i):
            chans = favs.channels(i)
            self._list_fav = i
            self._fav_pos = {id(c): n for n, c in enumerate(chans, 1)}
        else:
            chans = self.app.db.channels
            self._list_fav = None
            self._fav_pos = {}
        self.store.remove_all()
        self.store.splice(0, 0, [_ChannelRow(c) for c in chans])

    def _on_list_filter(self):
        if self._filter_sync:
            return
        key = self._filter_key()
        self.settings["list_filter"] = key
        i = fav_index(key)
        if i is not None:
            self.settings["fav_last"] = i
        if i != self._list_fav:
            self._fill_store()
            self._update_sort()
        self._refilter()
        if self.current is not None:
            self._select_in_list(self.current)

    def favourites_changed(self):
        """Favourites lists or their channels changed: filter entries, list and stars."""
        self._rebuild_filters()
        self._fill_store()
        self._update_sort()
        self._refilter()
        if self.current is not None:
            self._select_in_list(self.current)

    def _on_list_group(self):
        if not self._group_sync:
            g = self.group_dd.get_selected()
            self.settings["list_group"] = (self._groups[g] if 0 < g < len(self._groups)
                                           else "")
        self._refilter()

    def _refilter(self):
        self._fstate = self._filter_state()
        try:
            self.filter.changed(Gtk.FilterChange.DIFFERENT)
        finally:
            self._fstate = None
        if self.tab == "recordings":
            self.rec_filter.changed(Gtk.FilterChange.DIFFERENT)
            n, total = self.rec_filtered.get_n_items(), self.rec_store.get_n_items()
            self.count_label.set_text(_("{n} of {total}").format(n=n, total=total))
            self.count_label.set_tooltip_text(_("{n} of {total} recordings shown").format(n=n, total=total))
            return
        radio = self.mode == "radio"
        fav = self._list_fav is not None
        if fav:
            total = self.store.get_n_items()
        else:
            total = sum(1 for c in self.app.db.channels if c.radio == radio)
        n = self.filtered.get_n_items()
        self.count_label.set_text(_("{n} of {total}").format(n=n, total=total))
        if fav:
            tip = _("{n} of {total} favourites shown")
        elif radio:
            tip = _("{n} of {total} radio stations shown")
        else:
            tip = _("{n} of {total} TV channels shown")
        self.count_label.set_tooltip_text(tip.format(n=n, total=total))

    def _activate_first(self):
        if self.tab == "recordings":
            if self.rec_filtered.get_n_items():
                self.play_recording(self.rec_filtered.get_item(0).data)
            return
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
        favs = self.app.favs
        shown = self._list_fav
        for name, fn in (("ctx-fav-new", lambda: self.new_fav_list(c)),
                         ("ctx-fav-manage", lambda: self.app.show_favourites()),
                         ("ctx-fav-up", lambda: self.move_favourite(c, -1)),
                         ("ctx-fav-down", lambda: self.move_favourite(c, 1)),
                         ("ctx-fav-top", lambda: self.move_favourite(c, "top")),
                         ("ctx-fav-bottom", lambda: self.move_favourite(c, "bottom")),
                         ("ctx-play", lambda: self.zap(c)),
                         ("ctx-rec", lambda: self.app.timers.record_now(c)),
                         ("ctx-edit", lambda: self.app.show_editor(c)),
                         ("ctx-epg", lambda: self.app.show_epg(c)),
                         ("ctx-pip", lambda: self.pip.show_channel(c)),
                         ("ctx-analysis", lambda: self.app.show_analysis(c))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            self.add_action(a)
        a = Gio.SimpleAction.new("ctx-fav", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, p: self.toggle_favourite(c, p.get_int32()))
        self.add_action(a)

        def fav_item(label, i):
            item = Gio.MenuItem.new(_menu_label(label), None)
            item.set_action_and_target_value("win.ctx-fav", GLib.Variant.new_int32(i))
            return item

        m = Gio.Menu()
        m.append(_("Watch"), "win.ctx-play")
        names = favs.names()
        if shown is not None:
            # the favourites list shown: remove and sort
            m.append_item(fav_item(_("Remove from “{name}”").format(name=names[shown]), shown))
            order = Gio.Menu()
            order.append(_("Move up"), "win.ctx-fav-up")
            order.append(_("Move down"), "win.ctx-fav-down")
            order.append(_("Move to the top"), "win.ctx-fav-top")
            order.append(_("Move to the bottom"), "win.ctx-fav-bottom")
            m.append_submenu(_("Order in this list"), order)
        elif len(names) == 1:
            m.append_item(fav_item(_("Remove from Favourites") if favs.contains(0, c)
                                   else _("Add to Favourites"), 0))
        lists = Gio.Menu()
        for i, name in enumerate(names):
            lists.append_item(fav_item(("✓ " if favs.contains(i, c) else "    ") + name, i))
        more = Gio.Menu()
        more.append(_("New list…"), "win.ctx-fav-new")
        more.append(_("Manage favourites lists…"), "win.ctx-fav-manage")
        lists.append_section(None, more)
        m.append_submenu(_("Favourites lists"), lists)
        m.append(_("Record"), "win.ctx-rec")
        m.append(_("Programme Guide"), "win.ctx-epg")
        m.append(_("Edit…"), "win.ctx-edit")
        if c.is_iptv:
            m.append(_("Show as Picture in Picture"), "win.ctx-pip")
        m.append(_("Channel Analysis…"), "win.ctx-analysis")
        return m

    # ------------------------------------------------------------------ picture in picture
    def pip_selected(self):
        pos = self.selection.get_selected()
        c = self.filtered.get_item(pos).data if pos != Gtk.INVALID_LIST_POSITION else None
        if c is None or (self.current is not None and c.key == self.current.key):
            c = self.previous
        if c is None:
            self.set_status(_("Select a channel in the list first"))
            return
        self.pip.show_channel(c)

    def pip_swap(self):
        """The small picture becomes the main one and the other way round."""
        small = self.pip.channel
        if small is None:
            return
        main = self.current
        self.zap(small)
        if main is not None and self.pip.show_channel(main):
            return
        self.pip.close()

    def _on_hbbtv_state(self, has_app, running):
        self.lookup_action("hbbtv").set_enabled(bool(has_app or running))
        self.hbbtv_btn.set_tooltip_text(
            _("Close the interactive service (Esc)") if running else
            _("Interactive service of the channel (red button, F5)") if has_app else
            _("HbbTV: not available on this channel"))
        self._update_teletext()

    def _place_overlay_child(self, overlay, widget, rect):
        """Picture in picture: a quarter of the video width in the bottom right corner.
        HbbTV: the 16:9 area of the video."""
        if widget is self.hbbtv.view and widget is not None:
            return self.hbbtv.place(overlay, rect)
        if widget is not self.pip:
            return False
        w, h = overlay.get_width(), overlay.get_height()
        pw = max(160, int(w * 0.28))
        ph = pw * 9 // 16
        rect.x, rect.y = max(0, w - pw - 18), max(0, h - ph - 18)
        rect.width, rect.height = pw, ph
        return True

    def pip_close(self):
        self.pip.close()

    def show_mosaic(self):
        from .pip import MosaicWindow
        chans = [self.filtered.get_item(i).data for i in range(self.filtered.get_n_items())]
        if not any(c.is_iptv for c in chans):
            chans = [c for c in self.app.db.channels if c.is_iptv]
        MosaicWindow(self.app, self, chans).present()

    def toggle_favourite(self, c, i=None):
        """Put *c* into favourites list *i* (default: the list shown or used
        last) or take it out."""
        favs = self.app.favs
        if i is None:
            i = self.current_fav_list()
        if i is None:
            i = favs.ensure_one()
        favs.toggle(i, c)
        self.app.favourites_changed()

    def new_fav_list(self, c=None):
        """Ask for a name, make the list and put *c* into it."""
        from .favourites import ask_name

        def done(name):
            i = self.app.favs.create(name)
            if c is not None:
                self.app.favs.add(i, c)
            self.app.favourites_changed()
        ask_name(self, _("New Favourites List"), _("New list"), done)

    def move_favourite(self, c, step, target=None, after=None):
        """Sort the favourites list shown: *step* -1/+1, "top", "bottom", or
        to the place of *target*."""
        i = self._list_fav
        favs = self.app.favs
        if i is None:
            return
        if target is not None:
            moved = favs.move_to(i, c, target, after)
        elif step in ("top", "bottom"):
            moved = favs.move_to_end(i, c, top=step == "top")
        else:
            moved = favs.move(i, c, step)
        if moved:
            self.app.favourites_changed()

    def _row_drag_prepare(self, _src, _x, _y, box):
        """Drag a channel of a favourites list (sorted by number = list order)."""
        c = box.chan
        if c is None or self._list_fav is None or self._sort() != "number":
            return None
        return Gdk.ContentProvider.new_for_value(c.key)

    def _row_drop(self, _target, value, _x, y, box):
        target = box.chan
        if target is None or self._list_fav is None or not isinstance(value, str):
            return False
        src = next((c for c in self.app.favs.channels(self._list_fav) if c.key == value), None)
        if src is None or src is target:
            return False
        self.move_favourite(src, 0, target=target, after=y > box.get_height() / 2)
        return True

    # ------------------------------------------------------------------ recordings tab
    def _build_recordings_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bar = Gtk.Box(spacing=6, margin_start=10, margin_end=10)
        self.rec_folder_label = Gtk.Label(xalign=0, css_classes=["lv-count"], margin_start=10,
                                          margin_end=10, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.rec_sort_keys = ["newest", "oldest", "title", "channel", "size"]
        self.rec_sort = Gtk.DropDown.new_from_strings(
            [_("Newest first"), _("Oldest first"), _("By title"), _("By channel"),
             _("Largest first")])
        self.rec_sort.set_tooltip_text(_("Sort the recordings"))
        how = self.settings.get("rec_sort", "newest")
        self.rec_sort.set_selected(self.rec_sort_keys.index(how)
                                   if how in self.rec_sort_keys else 0)
        self.rec_sort.connect("notify::selected", lambda *_a: self._on_rec_sort())
        self.rec_sort.set_hexpand(True)
        bar.append(self.rec_sort)
        bar.append(Gtk.Button(icon_name="folder-open-symbolic", action_name="win.rec-folder",
                              tooltip_text=_("Open the recording folder"), css_classes=["flat"]))
        bar.append(Gtk.Button(icon_name="view-refresh-symbolic", action_name="win.rec-refresh",
                              tooltip_text=_("Look for new recordings"), css_classes=["flat"]))
        page.append(bar)
        page.append(self.rec_folder_label)

        self.rec_store = Gio.ListStore(item_type=Row)
        self.rec_filter = Gtk.CustomFilter.new(self._rec_filter_func)
        self.rec_filtered = Gtk.FilterListModel(model=self.rec_store, filter=self.rec_filter)
        self.rec_selection = Gtk.SingleSelection(model=self.rec_filtered, autoselect=False)
        fac = Gtk.SignalListItemFactory()
        fac.connect("setup", self._rec_row_setup)
        fac.connect("bind", self._rec_row_bind)
        self.rec_view = Gtk.ListView(model=self.rec_selection, factory=fac)
        self.rec_view.connect("activate", lambda _l, pos: self.play_recording(
            self.rec_filtered.get_item(pos).data))
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._rec_context)
        self.rec_view.add_controller(click)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.rec_view)
        self.rec_empty = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                   css_classes=["lv-count"], margin_start=20, margin_end=20,
                                   valign=Gtk.Align.START, margin_top=24)
        stack = self.rec_stack = Gtk.Stack(vexpand=True)
        stack.add_named(sw, "list")
        stack.add_named(self.rec_empty, "empty")
        page.append(stack)
        self.app.timers.connect("changed", lambda _t: self.tab == "recordings"
                                and self.reload_recordings())
        return page

    def _recording_folders(self):
        """The recording folder from the preferences plus any folder a timer uses."""
        folders = [self.settings.get("recording_dir", "")]
        folders += [t.folder for t in self.app.timers.timers if t.folder]
        return list(dict.fromkeys(str(Path(f).expanduser()) for f in folders if f))

    def _active_recording_paths(self):
        paths = set()
        for rec, _cap in list(self.app.timers.active.values()):
            try:
                paths.add(str(Path(rec.path).resolve()))
            except (OSError, AttributeError):
                pass
        return paths

    def reload_recordings(self):
        folders = self._recording_folders()
        recs = sort_recordings(find_recordings(folders), self.settings.get("rec_sort", "newest"))
        self._recording_now = self._active_recording_paths()
        self.rec_store.remove_all()
        self.rec_store.splice(0, 0, [Row(r) for r in recs])
        main = folders[0] if folders else ""
        self.rec_folder_label.set_text(main)
        self.rec_folder_label.set_tooltip_text("\n".join(folders))
        self.rec_empty.set_text(_("No recordings yet.\n\nRecordings are saved in\n{main}").format(main=main)
                                if main else _("No recording folder set in the preferences."))
        self.rec_stack.set_visible_child_name("list" if recs else "empty")
        self._refilter()

    def _on_rec_sort(self):
        self.settings["rec_sort"] = self.rec_sort_keys[self.rec_sort.get_selected()]
        self.reload_recordings()

    def _rec_filter_func(self, item):
        q = self.search.get_text().strip().lower()
        return not q or q in item.data.search_text()

    def _rec_row_setup(self, _f, li):
        b = Gtk.Box(spacing=10)
        # a fixed 80x45 slot: the preview picture, or a file-type icon
        thumb = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.START, width_request=80,
                        height_request=45)
        thumb.append(Gtk.Image(css_classes=["lv-chan-icon"], pixel_size=24, width_request=80))
        area = Gtk.DrawingArea(content_width=80, content_height=45, css_classes=["lv-rec-thumb"])
        area.set_draw_func(_draw_thumb)
        thumb.append(area)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=1)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["lv-count"])
        info = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["lv-count"])
        watched = Gtk.ProgressBar(css_classes=["lv-rec-progress"], margin_top=2, width_request=40)
        for w in (title, sub, info, watched):
            text.append(w)
        badge = Gtk.Image(icon_name="media-record-symbolic", css_classes=["rec"],
                          tooltip_text=_("Recording in progress"))
        for w in (thumb, text, badge):
            b.append(w)
        li.set_child(b)

    def _rec_row_bind(self, _f, li):
        r = li.get_item().data
        thumb, text, badge = list(_children(li.get_child()))
        title, sub, info, watched = list(_children(text))
        icon, picture = list(_children(thumb))
        icon.set_from_icon_name("audio-x-generic-symbolic" if r.is_audio
                                else "video-x-generic-symbolic")
        png = None if r.is_audio else self._thumbnail(r)
        pix = _thumb_pixbuf(png) if png is not None else None
        picture.pixbuf = pix
        picture.queue_draw()
        picture.set_visible(pix is not None)
        icon.set_visible(pix is None)
        title.set_text(r.title)
        sub.set_text(r.subtitle)
        sub.set_visible(bool(r.subtitle))
        info.set_text(r.details())
        progress = self.positions.progress(r.path)
        watched.set_fraction(progress)
        watched.set_visible(progress > 0)
        watched.set_tooltip_text(_("Watched") if self.positions.watched(r.path) else
                                 _("Watched up to {pos}").format(
                                     pos=_mmss(self.positions.get(r.path)["pos"]))
                                 if progress > 0 else None)
        tip = [str(r.path)]
        if r.description:
            tip.insert(0, r.description[:600])
        li.get_child().set_tooltip_text("\n\n".join(tip))
        try:
            running = str(r.path.resolve()) in getattr(self, "_recording_now", ())
        except OSError:
            running = False
        badge.set_visible(running)

    def _thumbnail(self, r):
        if self.thumbnailer is None:
            try:
                from ..thumbnails import Thumbnailer
                self.thumbnailer = Thumbnailer()
            except (ImportError, ValueError):
                self.thumbnailer = False
        if not self.thumbnailer:
            return None
        return self.thumbnailer.request(r.path, r.mtime, self._thumbnail_ready)

    def _thumbnail_ready(self, path, _png):
        # rebind the row of that recording
        for i in range(self.rec_store.get_n_items()):
            row = self.rec_store.get_item(i)
            if row.data.path == path:
                self.rec_store.splice(i, 1, [Row(row.data)])     # rebinds the row
                break
        return False

    def play_recording(self, r, from_start=False):
        if not r.path.exists():
            self.set_status(_("Recording not found: {path}").format(path=r.path))
            self.reload_recordings()
            return
        resume = None if from_start else self.positions.resume_at(r.path)
        self.play_files([str(r.path)])
        if resume:
            self._resume_at = (str(r.path), resume)
            self.set_status(_("Continuing at {pos}").format(pos=_mmss(resume)))

    def _track_position(self):
        """Remember how far the playing file got (every few seconds)."""
        if not (0 <= self.playlist_pos < len(self.playlist)) or self.current is not None:
            return True
        item = self.playlist[self.playlist_pos]
        pos = self.player.position()
        if pos is None:
            return True
        if self._resume_at and self._resume_at[0] == item:
            if pos[0] < self._resume_at[1] - 5:
                self.player.seek_to(self._resume_at[1])
            self._resume_at = None
            return True
        self.positions.update(item, pos[0], pos[1])
        self._positions_dirty = True
        return True

    def _save_positions(self):
        if self._positions_dirty:
            self.positions.save()
            self._positions_dirty = False
        return True

    def _set_watched(self, r, watched):
        p = self.positions.get(r.path)
        dur = (p or {}).get("dur") or r.duration or 1
        if watched:
            self.positions.update(r.path, dur, dur)
        else:
            self.positions.forget(r.path)
        self.positions.save()
        self.reload_recordings()

    def _show_recording_details(self, r):
        lines = [r.subtitle, r.details(), r.info.get("genre", ""), "", r.description,
                 "", str(r.path)]
        message(self, r.title, "\n".join(x for x in lines if x is not None).strip())

    def open_recording_folder(self, path=None):
        folder = Path(path) if path else Path(self.settings.get("recording_dir", "")).expanduser()
        if not folder.is_dir():
            self.set_status(_("Folder not found: {folder}").format(folder=folder))
            return
        try:
            Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)
        except GLib.Error as e:
            self.set_status(_("Cannot open {folder}: {message}").format(folder=folder, message=e.message))

    def _rec_context(self, _gesture, _n, x, y):
        pos = self.rec_selection.get_selected()
        if pos == Gtk.INVALID_LIST_POSITION:
            return
        r = self.rec_filtered.get_item(pos).data
        for name, fn in (("rec-ctx-play", lambda: self.play_recording(r)),
                         ("rec-ctx-restart", lambda: self.play_recording(r, from_start=True)),
                         ("rec-ctx-details", lambda: self._show_recording_details(r)),
                         ("rec-ctx-watched", lambda: self._set_watched(r, True)),
                         ("rec-ctx-unwatched", lambda: self._set_watched(r, False)),
                         ("rec-ctx-folder", lambda: self.open_recording_folder(r.path.parent)),
                         ("rec-ctx-delete", lambda: self._confirm_delete_recording(r))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            self.add_action(a)
        m = Gio.Menu()
        m.append(_("Play"), "win.rec-ctx-play")
        if self.positions.resume_at(r.path):
            m.append(_("Play from the Beginning"), "win.rec-ctx-restart")
        m.append(_("Details…"), "win.rec-ctx-details")
        if self.positions.watched(r.path):
            m.append(_("Mark as Unwatched"), "win.rec-ctx-unwatched")
        else:
            m.append(_("Mark as Watched"), "win.rec-ctx-watched")
        m.append(_("Show in Folder"), "win.rec-ctx-folder")
        m.append(_("Delete…"), "win.rec-ctx-delete")
        pop = Gtk.PopoverMenu.new_from_model(m)
        pop.set_parent(self.rec_view)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.popup()

    def _confirm_delete_recording(self, r):
        try:
            running = str(r.path.resolve()) in self._active_recording_paths()
        except OSError:
            running = False
        if running:
            self.set_status(_("Stop the recording before deleting it"))
            return
        dlg = Gtk.AlertDialog(message=_("Delete “{title}”?").format(title=r.title),
                              detail=_("{path}\n\nThe file is removed from the disk.").format(path=r.path),
                              buttons=[_("Cancel"), _("Delete")], cancel_button=0, default_button=0)

        def done(d, res):
            try:
                if d.choose_finish(res) != 1:
                    return
            except GLib.Error:
                return
            if 0 <= self.playlist_pos < len(self.playlist) and \
                    Path(self.playlist[self.playlist_pos]) == r.path:
                self.stop_playback()
            try:
                r.path.unlink()
                delete_info(r.path)
                self.positions.forget(r.path)
                self.positions.save()
                self.set_status(_("Deleted {name}").format(name=r.path.name))
            except OSError as e:
                self.set_status(_("Cannot delete {name}: {strerror}").format(name=r.path.name, strerror=e.strerror))
            self.reload_recordings()

        dlg.choose(self, None, done)

    # ------------------------------------------------------------------ zapping
    def zap(self, c):
        if c is None:
            return
        if self.current and self.current.key != c.key:
            self.previous = self.current
        if self._switch_on_transponder(c):
            return
        self._release_capture()
        self.current = c
        self.stopped = None
        self.playlist = []
        self.set_title(f"{c.name} – {APP_NAME}")
        self._show_picture(True)
        self.player.audio_delay_ms = self.settings.get("audio_delays", {}).get(c.key, 0)
        self._tuned = c
        self._tried = {_tuning_sig(c)}
        self.oos.set_visible(False)
        off_air = bool(c.dvbi_periods) and not dvbi.available(c.dvbi_periods)
        if off_air:
            self._show_off_air(c)
        else:
            try:
                self._start_stream(c)
            except RuntimeError as e:
                if not self._try_failover(str(e)):
                    self.set_status(str(e))
                    self._update_teletext()
                    return
        self._update_teletext()
        self._remember(c)
        if self.tab != "recordings" and (self.mode == "radio") != c.radio:
            self.set_mode("radio" if c.radio else "tv", tune=False)
        elif self.tab == "recordings":
            self.mode = "radio" if c.radio else "tv"
        self._select_in_list(c)
        if not off_air:
            self.set_status(_("Tuning {name}…").format(name=c.name))
        secs = int(self.settings.get("epg_info_seconds", self.settings["osd_seconds"]))
        if secs > 0:
            self.show_osd(secs)
        self._update_transport()
        self._update_visualizer()

    def _switch_on_transponder(self, c):
        """Zap within the transponder of the live tuner without retuning:
        the capture keeps running, only the service changes."""
        cap = self.capture
        if cap is None or c.is_iptv or c.dvbi_periods or self.stopped is not None or \
                not hasattr(cap, "can_switch_to") or not cap.can_switch_to(c):
            return False
        cap.switch_service(c)
        self.current = c
        self.playlist = []
        self.set_title(f"{c.name} – {APP_NAME}")
        self._show_picture(True)
        self.player.audio_delay_ms = self.settings.get("audio_delays", {}).get(c.key, 0)
        self._tuned = c
        self._tried = {_tuning_sig(c)}
        self.oos.set_visible(False)
        self.quality.reset("dvb")
        self._update_quality_bar()
        self.hbbtv.channel_changed(c, cap)
        self.app.plugins.emit("channel-tuned", c, cap)
        self.player.play_capture(cap, c.sid, offset=None)
        self._update_teletext()
        self._remember(c)
        if self.tab != "recordings" and (self.mode == "radio") != c.radio:
            self.set_mode("radio" if c.radio else "tv", tune=False)
        elif self.tab == "recordings":
            self.mode = "radio" if c.radio else "tv"
        self._select_in_list(c)
        secs = int(self.settings.get("epg_info_seconds", self.settings["osd_seconds"]))
        if secs > 0:
            self.show_osd(secs)
        self._update_transport()
        self._update_visualizer()
        return True

    def _watch_color_scheme(self):
        """Follow the desktop's light / dark switch while "system" is chosen."""
        def changed(*_a):
            if self.settings.get("theme", "dark") == "system":
                apply_theme(self.settings)
        try:
            src = Gio.SettingsSchemaSource.get_default()
            if src is not None and src.lookup("org.gnome.desktop.interface", True):
                self._gnome_iface = Gio.Settings.new("org.gnome.desktop.interface")
                self._gnome_iface.connect("changed::color-scheme", changed)
                self._gnome_iface.connect("changed::gtk-theme", changed)
        except Exception:          # noqa: BLE001 - no GNOME settings
            pass

    def first_in_list(self):
        """The first channel of the channel list as shown (TV or radio, filter,
        group and order), or None when the list is empty."""
        if self.filtered.get_n_items():
            return self.filtered.get_item(0).data
        return None

    def _remember(self, c):
        """Last channel overall (tuned at start) and per kind (for the TV/Radio switch)."""
        self.settings["last_channel"] = c.key
        self.settings["last_radio_channel" if c.radio else "last_tv_channel"] = c.key
        try:
            self.settings.save()
        except OSError:
            pass

    # ------------------------------------------------------------------ TV / radio
    def _on_mode_toggled(self, btn, mode):
        if btn.get_active() and not self._mode_sync:
            self.set_mode(mode)

    def set_mode(self, mode, tune=True):
        """Show TV or radio channels; with ``tune`` also switch to the last
        channel watched (or listened to) in that mode. ``"recordings"`` shows
        the recordings instead and leaves playback alone."""
        if mode not in TABS:
            return
        self.tab = mode
        self._mode_sync = True
        self.mode_btns[mode].set_active(True)
        self._mode_sync = False
        self.search.set_placeholder_text(_("Search recordings") if mode == "recordings"
                                         else _("Search name or number"))
        if mode == "recordings":
            self.side_stack.set_visible_child_name("recordings")
            self.reload_recordings()
            return
        self.side_stack.set_visible_child_name("channels")
        self.mode = mode
        self.settings["mode"] = mode
        self._refilter()
        if not tune or (self.current is not None and self.current.radio == (mode == "radio")):
            return
        radio = mode == "radio"
        target = self.app.db.by_key(self.settings.get(f"last_{mode}_channel") or "")
        if target is None or target.radio != radio:
            target = next((c for c in self.app.db.channels if c.radio == radio), None)
        if target is None:
            self.set_status(_("No radio stations in the channel list") if radio
                            else _("No TV channels in the channel list"))
            return
        self.zap(target)

    def _select_in_list(self, c):
        for i in range(self.filtered.get_n_items()):
            if self.filtered.get_item(i).data.key == c.key:
                self.selection.set_selected(i)
                if hasattr(self.listview, "scroll_to"):      # GTK >= 4.12
                    self.listview.scroll_to(i, Gtk.ListScrollFlags.NONE, None)
                return

    def step_item(self, step):
        """Previous/next button: playlist entry when a playlist runs, else channel."""
        if len(self.playlist) > 1:
            self.playlist_step(step)
        else:
            self.zap_relative(step)

    def set_title(self, title):
        super().set_title(title)
        name = title.removesuffix(f" – {APP_NAME}") if title else APP_NAME
        self.title_label.set_text(name or APP_NAME)
        if name == APP_NAME:
            self.subtitle_label.set_visible(False)

    def _update_heading(self):
        """Header subtitle: the programme on air on the current channel."""
        now = None
        if self.current:
            now, _ = self.app.epg.now_next(self.current.epg_key)
        self.subtitle_label.set_text(f"{hm(now.start)}–{hm(now.end)}  {now.title}" if now else "")
        self.subtitle_label.set_visible(bool(now))

    def _start_stream(self, c):
        """Play channel *c* (a list channel, or another instance of it)."""
        self._nolock_since = None
        # DVB-I services without a stream (an HbbTV application) have nothing to measure
        self.quality.reset("dvb" if not c.is_iptv else "iptv" if c.url else None)
        self._update_quality_bar()
        if c.is_iptv:
            if c.url:
                self.player.play_uri(c.url)
            else:
                self.player.stop()          # DVB-I: an HbbTV application is the service
            self.hbbtv.channel_changed(c, None)
            from .. import hooks
            hooks.run("channel-tuned", (c,))          # the user's scripts (DVB: via emit)
        else:
            self.capture = self.app.tuners.acquire(c, "live")
            self.capture.connect("stats", self._on_stats)
            self.capture.connect("error", lambda _c, m: self._on_play_error(m))
            self.capture.connect("eit", self.app.on_eit)
            self.capture.connect("provider-epg", self.app.on_provider_epg)
            self.capture.connect("tsid", self.app.on_tsid)
            self.capture.connect("ait-pid", self.app.on_ait_pid)
            # HbbTV follows the AIT of the channel the tuner was tuned for; a
            # channel that only shares its transponder starts without signalling.
            # A channel that only shares the transponder (a recording tuned
            # it) gets a view of its own signalling on the same capture.
            cap = self.capture
            own = cap if cap.channel.key == c.key else \
                getattr(cap, "service_view", lambda _c: None)(c)
            self.hbbtv.channel_changed(c, own)
            self.app.plugins.emit("channel-tuned", c, self.capture)
            GLib.timeout_add(700, self._start_live, self.capture)

    def _on_play_error(self, msg):
        if not self._try_failover(msg):
            self.set_status(msg)

    def _try_failover(self, reason):
        """IT-10: when the instance of a DVB-I channel in use fails, switch
        to the next one in priority order. Each instance is tried once per
        channel change, so a failing pair does not alternate."""
        c = self.current
        if c is None or not c.dvbi_id or not self.settings.get("dvbi_failover", True) or \
                self.stopped is not None:
            return False
        alts = dvbi.alternatives(c, self.app.db.channels,
                                 sat_direct=self.settings.get("dvbi_sat_direct", True),
                                 prefer_broadcast=self.settings.get("dvbi_prefer_broadcast", False))
        for alt in alts:
            sig = _tuning_sig(alt)
            if sig in self._tried:
                continue
            self._tried.add(sig)
            self._tuned_before_iptv = bool(self._tuned and self._tuned.is_iptv)
            self._release_capture()
            self._tuned = alt
            try:
                self._start_stream(alt)
            except RuntimeError as e:
                reason = str(e)
                continue
            kind = _instance_kind(alt)
            if alt.is_iptv and self._tuned_before_iptv:
                kind = "another internet stream"
            self.set_status(_("{name}: {v1} – switched to {kind}").format(name=c.name, v1=_short(reason), kind=kind))
            self._update_teletext()
            return True
        return False

    def _channels_off_air(self):
        """ids of the DVB-I channels outside their availability right now (IT-4)."""
        return {id(c) for c in self.app.db.channels
                if c.dvbi_periods and not dvbi.available(c.dvbi_periods)}

    def _availability_tick(self):
        if any(c.dvbi_periods for c in self.app.db.channels):
            off = self._channels_off_air()
            if off != self._off_air:         # filter again only when something changed
                self._off_air = off
                self._refilter()
            c = self.current
            if c is not None and c.dvbi_periods and self.oos.get_visible() and \
                    dvbi.available(c.dvbi_periods):
                self.zap(c)                  # the channel went on air
        return True

    def _show_off_air(self, c):
        """IT-4: a pop-up channel outside its availability: banner, no tuning."""
        self.oos_label.set_text(_("{name} is not on air at the moment").format(name=c.name))
        self.oos_image.set_paintable(None)
        self.oos.set_visible(True)
        self.set_status(_("{name} is not on air at the moment").format(name=c.name))
        url = c.dvbi_oos_logo
        if not url:
            return
        folder = CACHE_DIR / "dvbi" / "banners"
        path = dvbi.logo_file(str(folder), url)

        def show():
            if self.current is c and Path(path).exists():
                self.oos_image.set_filename(path)
            return False

        if Path(path).exists():
            show()
            return

        def work():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                data = dvbi.fetch(url).data
                with open(path, "wb") as f:
                    f.write(data)
            except (OSError, dvbi.DVBIError):
                return
            GLib.idle_add(show)

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _start_live(self, cap):
        if cap is self.capture:
            self.player.play_capture(cap, self._tuned.sid, offset=0)
        return False

    def _release_capture(self):
        self.player.stop()
        if self.capture:
            if self._tuned is not None and hasattr(self.capture, "drop_service_view"):
                self.capture.drop_service_view(self._tuned.key)
            self.app.tuners.release(self.capture, "live")
            self.capture = None
        self._update_teletext()

    def _on_preempt(self, _tm):
        self.player.stop()
        self.capture = None
        self.hbbtv.stopped()
        self._update_teletext()
        self.set_status(_("Live TV stopped: tuner needed for a timer recording"))

    def zap_relative(self, step, favs=False):
        n = self.filtered.get_n_items()
        items = [self.filtered.get_item(i).data for i in range(n)]
        if favs:
            i = self.current_fav_list()
            items = self.app.favs.channels(i) if i is not None else []
        items = [c for c in items if c.selectable]      # DVB-I LCN flag
        if not items:
            return
        here = self.current or self.stopped
        try:
            i = next(i for i, c in enumerate(items) if here and c.key == here.key)
        except StopIteration:
            i = -1 if step > 0 else 0
        c = items[(i + step) % len(items)]
        self.zap(c)

    def zap_last(self):
        if self.previous:
            self.zap(self.previous)

    def zap_number(self, num):
        if self._list_fav is not None and self.tab != "recordings":
            # a favourites list is shown: its own numbers 1, 2, 3 ...
            chans = self.app.favs.channels(self._list_fav)
            if 0 < num <= len(chans) and chans[num - 1].selectable:
                self.zap(chans[num - 1])
            return
        for c in self.app.db.channels:
            if c.number == num and c.selectable:
                self.zap(c)
                return

    def _osd_width(self):
        try:
            scale = max(50, min(300, int(self.settings.get("osd_scale", 100))))
        except (TypeError, ValueError):
            scale = 100
        return round(OSD_WIDTH * scale / 100)

    def apply_display_settings(self):
        """OSD size and subtitle font after the preferences changed."""
        apply_display(self.settings)
        self.osd.set_size_request(self._osd_width(), -1)
        self.player.apply_subtitle_font()

    def update_inhibit(self, playing=None):
        """Keep the screen saver off while playing, if the preferences say so."""
        if playing is None:
            playing = bool(self.player.playbin) and not self.player.paused
        want = playing and bool(self.settings.get("inhibit_screensaver", True))
        if want and not self._inhibit_cookie:
            self._inhibit_cookie = self.app.inhibit(
                self, Gtk.ApplicationInhibitFlags.IDLE, _("Playing TV or radio")) or 0
        elif not want and self._inhibit_cookie:
            self.app.uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0

    def stop_playback(self):
        """Switch the channel (or file) off completely; Play brings it back."""
        self._track_position()
        self._save_positions()
        if self.current is not None or self.playlist:
            self.app.plugins.emit("playback-stopped", self.current)
        if self.current:
            self.stopped = self.current
        self._release_capture()
        self.hbbtv.stopped()
        self.current = None
        self.set_title(APP_NAME)
        self._show_picture(False)            # no frozen last frame
        self.oos.set_visible(False)
        self._hide_osd()
        self.sublabel.set_visible(False)
        self.signal_label.set_text("")
        self.quality.reset(None)
        self._update_quality_bar()
        self.set_status(_("Stopped"))
        self._update_transport()
        self._update_visualizer()

    def _show_picture(self, on):
        """Show the video picture, or hide it and show the faint logo instead."""
        self.picture.set_opacity(1 if on else 0)
        self.idle_logo.set_visible(not on)

    def shift_audio(self, step):
        """Move the sound against the picture; remembered per channel."""
        ms = 0 if step is None else self.player.audio_delay_ms + step
        ms = max(-10000, min(10000, ms))
        self.player.set_audio_delay(ms)
        if self.current:
            delays = self.settings.setdefault("audio_delays", {})
            if ms:
                delays[self.current.key] = ms
            else:
                delays.pop(self.current.key, None)
        if ms > 0:
            self.flash(_("Sound {ms} ms later").format(ms=ms))
        elif ms < 0:
            self.flash(_("Sound {v0} ms earlier").format(v0=-ms))
        else:
            self.flash(_("Audio delay off"))

    def rebuild_playback(self):
        if self.current:
            self.zap(self.current)

    # ------------------------------------------------------------------ files / urls / playlist
    def play_files(self, paths):
        self.stop_playback()
        self.stopped = None
        self._show_picture(True)
        self.player.audio_delay_ms = 0
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
        self._update_visualizer()
        self.set_status(_("Playing {item}").format(item=item))

    def _on_eos(self, _p):
        if 0 <= self.playlist_pos < len(self.playlist) and self.current is None:
            item = self.playlist[self.playlist_pos]
            p = self.positions.get(item)
            if p:
                self.positions.update(item, p["dur"], p["dur"])     # watched to the end
                self._positions_dirty = True
        if self.playlist:
            self.playlist_step(1)

    def on_open_file(self):
        dlg = Gtk.FileDialog(title=_("Open media files"))
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
        win = Gtk.Window(title=_("Open URL"), transient_for=self, modal=True, default_width=500)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12,
                      margin_bottom=12, margin_start=12, margin_end=12)
        entry = Gtk.Entry(placeholder_text=_("http://…, rtp://…, udp://…, rtsp://…"))
        def go(*_):
            url = entry.get_text().strip()
            if url:
                self.stop_playback()
                self.stopped = None
                self._show_picture(True)
                self.player.audio_delay_ms = 0
                self.playlist = [url]
                self.playlist_pos = 0
                self.player.play_uri(url)
                self.set_title(f"{url} – {APP_NAME}")
            win.close()
        entry.connect("activate", go)
        b = Gtk.Button(label=_("Open"), css_classes=["suggested-action"], halign=Gtk.Align.END)
        b.connect("clicked", go)
        box.append(entry)
        box.append(b)
        win.set_child(box)
        win.present()

    def open_any(self, arg):
        if "://" in arg:
            self._show_picture(True)
            self.player.play_uri(arg)
        else:
            self.play_files([arg])

    # ------------------------------------------------------------------ import / export
    def on_import(self):
        dlg = Gtk.FileDialog(title=_("Import channel list (LinDVB .json, dvbv5, VDR, lamedb, M3U, "
                                     "CSV, DVBViewer .ini)"))
        dlg.open_multiple(self, None, self._import_done)

    def _import_done(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        self.import_files([files.get_item(i).get_path() for i in range(files.get_n_items())])

    def import_files(self, paths, parent=None):
        """Merge the channel lists in *paths* into the channel list; returns
        the status text."""
        total_a = total_u = 0
        fav_lists = []
        for path in paths:
            try:
                a, u = self.app.db.merge(import_file(path))
                # LinDVB's own channel file also brings its favourites lists
                if str(path).lower().endswith(".json"):
                    text = Path(path).read_text(encoding="utf-8", errors="replace")
                    if is_lindvb_json(text):
                        fav_lists += parse_lindvb_json(text)[1]
            except (OSError, ValueError) as e:
                message(parent or self, _("Could not import {name}").format(name=Path(path).name),
                        str(e))
                continue
            total_a += a
            total_u += u
        self.app.db.save()
        if fav_lists and self.app.favs.merge_lists(fav_lists):
            self.app.favourites_changed()
        self.reload_channels()
        text = _("Imported {total_a} new and {total_u} updated channels").format(
            total_a=total_a, total_u=total_u)
        self.set_status(text)
        return text

    def on_export(self):
        """The export window: which channels, which format, how many fit."""
        from .export import ExportWindow
        shown = None
        if self.tab != "recordings":
            shown = [self.filtered.get_item(i).data for i in range(self.filtered.get_n_items())]
        ExportWindow(self.app, self, shown).present()

    def on_export_epg(self):
        dlg = Gtk.FileDialog(title=_("Export the programme guide (XMLTV)"),
                             initial_name="lindvb-guide.xml")
        dlg.save(self, None, self._export_epg_done)

    def _export_epg_done(self, dlg, res):
        try:
            path = dlg.save_finish(res).get_path()
        except GLib.Error:
            return
        from ..epg import write_xmltv
        data = write_xmltv(self.app.epg, self.app.db.channels)
        try:
            Path(path).write_bytes(data)
            self.set_status(_("Exported to {path}").format(path=path))
        except OSError as e:
            self.set_status(_("Cannot write {path}: {error}").format(path=path, error=e.strerror))

    # ------------------------------------------------------------------ recording
    def _is_recording(self):
        return bool(self.current and self.app.timers.is_recording(self.current))

    def on_record(self):
        if not self.current:
            self.set_status(_("Nothing to record"))
            self.rec_btn.set_active(False)
            return
        title = ""
        now, _unused = self.app.epg.now_next(self.current.epg_key)
        if now:
            title = now.title
        self.app.timers.record_now(self.current, self.capture, title)
        self._update_transport()

    def on_record_options(self):
        """Choose format and tracks for one manual recording, then start it."""
        if not self.current:
            self.set_status(_("Nothing to record"))
            return
        if self._is_recording():
            self.set_status(_("This channel is already being recorded"))
            return
        channel = self.current

        def start(options):
            if self.current is not channel or self._is_recording():
                return
            now, _unused = self.app.epg.now_next(channel.epg_key)
            self.app.timers.record_now(channel, self.capture, now.title if now else "",
                                       options=options)
            self._update_transport()

        from .timers import RecordOptionsWindow
        RecordOptionsWindow(self.app, self, channel, start).present()

    def on_keep_timeshift(self):
        """Save the whole timeshift buffer, then continue recording live."""
        if self.current and self.capture:
            start, _ = self.capture.store.bounds()
            self.app.timers.record_now(self.current, self.capture, _("Timeshift"), start_offset=start)

    # ------------------------------------------------------------------ teletext
    def _update_teletext(self):
        """Enable the teletext button/action only when the live channel has teletext."""
        if not hasattr(self, "player"):
            return
        c = self.current
        has = bool(self.capture and c and (c.teletext_pid or self.player.has_teletext()))
        decoder = Gst.ElementFactory.find("teletextdec") is not None
        hbb = getattr(self, "hbbtv", None)
        if hbb is not None and hbb.has_teletext_app:
            # HbbTV's digital teletext takes the TXT key (TS 102 796 7.2.3.2)
            self.lookup_action("teletext").set_enabled(True)
            self.ttx_btn.set_tooltip_text(_("Teletext of the channel's interactive service (Ctrl+Shift+T)"))
            return
        self.lookup_action("teletext").set_enabled(has and decoder)
        self.ttx_btn.set_tooltip_text(
            _("Teletext (Ctrl+Shift+T)") if has and decoder else
            _("Teletext: decoder missing (gst-plugins-bad with libzvbi)") if has else
            _("Teletext: not available on this channel"))

    def on_teletext(self):
        if self.hbbtv.teletext_button():
            return
        if not self.capture:
            self.set_status(_("Teletext needs a DVB channel"))
            return
        self.app.show_teletext(self.capture, self.current)

    # ------------------------------------------------------------------ audio / video
    def player_pause(self):
        if not self.player.playbin and self.stopped:
            self.zap(self.stopped)          # Play after Stop: switch the channel back on
            return
        self.player.toggle_pause()

    def _set_volume(self, v):
        self.player.volume = v
        self.settings["volume"] = v
        self._update_volume_icon()

    def _set_mute(self, m):
        self.player.mute = m
        self._update_volume_icon()

    def _update_volume_icon(self):
        v = self.vol.get_value()
        level = "muted" if self.mute_btn.get_active() or v <= 0 else \
            "low" if v < 0.4 else "medium" if v < 0.8 else "high"
        self.mute_btn.set_icon_name(f"audio-volume-{level}-symbolic")

    def toggle_mute(self):
        self.mute_btn.set_active(not self.mute_btn.get_active())

    def change_volume(self, d):
        self.vol.set_value(self.vol.get_value() + d)
        self.flash(_("Volume {v0}%").format(v0=int(self.vol.get_value() * 100)))

    def _on_subtitle_text(self, _player, text):
        self.sublabel.set_text(text)
        self.sublabel.set_visible(bool(text))

    def _rebuild_track_menus(self):
        self.audio_menu.remove_all()
        tracks, cur = self.player.audio_tracks()
        for i, t in enumerate(tracks):
            self.audio_menu.append(("● " if i == cur else "") + t, f"win.audio-track({i})")
        self.sub_menu.remove_all()
        self.sub_menu.append(_("Off"), "win.subtitle(-1)")
        subs, cur = self.player.subtitle_tracks()
        for i, t in enumerate(subs):
            self.sub_menu.append(("● " if i == cur else "") + t, f"win.subtitle({i})")
        if self.player.has_closed_captions():
            cc = Gio.Menu()
            cc.append(_("Closed Captions (CEA-608/708)"), "win.closed-captions")
            self.sub_menu.append_section(None, cc)
        self._update_osd_tags()

    def _update_osd_tags(self):
        """Info bar tags: sound formats (current first), languages, subtitles."""
        for w in list(_children(self.osd_tags)):
            self.osd_tags.remove(w)
        if not self.current or not self.player.playbin:
            self.osd_meta_row.set_visible(bool(self.osd_meta.get_text()))
            return
        tracks, cur = self.player.audio_track_info()
        layouts, languages = audio_summary(tracks, cur)
        tags = [(lay, i == 0, None) for i, lay in enumerate(layouts)]
        if len(languages) > 1:
            tags.append((_("{n} languages").format(n=len(languages)), False, " / ".join(languages)))
        elif len(tracks) > 1:
            tags.append((_("{n} audio tracks").format(n=len(tracks)), False, None))
        if self.player.subtitle_tracks()[0]:
            tags.append((_("Subtitles"), False, None))
        for text, current, tip in tags:
            lbl = Gtk.Label(label=text, css_classes=["lv-osd-tag"], tooltip_text=tip)
            if current:
                lbl.add_css_class("lv-current")
            self.osd_tags.append(lbl)
        self.osd_meta_row.set_visible(bool(tags) or bool(self.osd_meta.get_text()))

    def on_screenshot(self):
        d = Path(self.settings["screenshot_dir"]).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        name = (self.current.name if self.current else "video").replace("/", "_")
        path = d / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        self.set_status(_("Screenshot saved: {path}").format(path=path) if self.player.screenshot(str(path))
                        else _("Screenshot failed"))

    def on_picture_settings(self):
        win = Gtk.Window(title=_("Picture Settings"), transient_for=self, default_width=380)
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
        reset = Gtk.Button(label=_("Restore Defaults"))
        reset.connect("clicked", lambda _b: [s.set_value(d) for s, d in scales.values()])
        grid.attach(reset, 1, len(specs), 1, 1)
        win.set_child(grid)
        win.present()

    def on_equalizer(self):
        win = Gtk.Window(title=_("Equalizer"), transient_for=self, default_height=300)
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
        self.set_default_size(int(size[0] * pct / 100) + side, int(size[1] * pct / 100) + 120)

    def toggle_fullscreen(self):
        if self._fs_layout:
            self.unfullscreen()
        else:
            self.fullscreen()
        self._apply_fullscreen_layout(not self._fs_layout)

    def _on_fullscreened(self, *_):
        # follow full screen changes made by the window manager as well
        if self.is_fullscreen() != self._fs_layout:
            self._apply_fullscreen_layout(self.is_fullscreen())

    def _apply_fullscreen_layout(self, on):
        self._fs_layout = on
        self.sidebar.set_visible(not on and self.list_btn.get_active())
        self.statusbar.set_visible(not on)
        self._dock_controls(floating=on)

    def _dock_controls(self, floating):
        """Controls sit under the picture in a window and float over it in full screen."""
        rev = self.controls_revealer
        if floating == (rev.get_parent() is self.overlay):
            return
        rev.get_parent().remove(rev) if floating else self.overlay.remove_overlay(rev)
        if floating:
            rev.set_valign(Gtk.Align.END)
            rev.add_css_class("lv-floating")
            self.overlay.add_overlay(rev)
            self.osd.set_margin_bottom(110)
            self.sublabel.set_margin_bottom(120)
            self._schedule_hide()
        else:
            rev.remove_css_class("lv-floating")
            self.main_box.insert_child_after(rev, self.paned)
            self.osd.set_margin_bottom(24)
            self.sublabel.set_margin_bottom(36)
            self._show_controls()
        self.fs_btn.set_icon_name("view-restore-symbolic" if floating else "view-fullscreen-symbolic")
        self.fs_btn.set_tooltip_text(_("Leave full screen (F)") if floating else _("Full screen (F)"))

    def _on_pointer_motion(self, _ctl, x, y):
        if getattr(self, "_last_xy", None) == (int(x), int(y)):
            return
        self._last_xy = (int(x), int(y))
        if self.controls_revealer.get_parent() is self.overlay:
            self._show_controls()
            self._schedule_hide()

    def _show_controls(self):
        self.controls_revealer.set_reveal_child(True)
        self.set_cursor(None)

    def _schedule_hide(self):
        if self._hide_source:
            GLib.source_remove(self._hide_source)
        self._hide_source = GLib.timeout_add(2500, self._auto_hide)

    def _auto_hide(self):
        self._hide_source = None
        floating = self.controls_revealer.get_parent() is self.overlay
        hovered = self.controls.get_state_flags() & Gtk.StateFlags.PRELIGHT
        if floating and not self.player.paused and not hovered:
            self.controls_revealer.set_reveal_child(False)
            self.set_cursor_from_name("none")
        elif floating:
            self._schedule_hide()
        return False

    def toggle_list(self):
        self.list_btn.set_active(not self.list_btn.get_active())

    # ------------------------------------------------------------------ radio visualisation
    def set_visualizer(self, vis_id):
        if vis_id not in self.visualizers:
            vis_id = DEFAULT_STYLE
        self.settings["visualizer"] = vis_id
        self.lookup_action("vis").set_state(GLib.Variant.new_string(vis_id))
        self._update_visualizer()
        self.flash(self.visualizers[vis_id][0])

    def _on_vis_error(self, _player, message):
        """A GStreamer visualiser failed: the sound plays on, Goom HD takes over."""
        failed = self.visualizers.get(self.settings.get("visualizer"), ("?",))[0]
        fallback = "goomhd" if "goomhd" in self.visualizers else DEFAULT_STYLE
        self.set_visualizer(fallback)
        self.set_status(_("{name} could not start ({m}), showing {other} instead").format(
            name=failed, m=message, other=self.visualizers[fallback][0]))

    def _vis_choice_menu(self, vid):
        """The submenu of a visualiser with choices: all of them, then each top
        folder (with its subfolders in a further submenu)."""
        choices = self._vis_choices[vid]
        menu, top, folders = Gio.Menu(), Gio.Menu(), Gio.Menu()
        top.append_item(_vis_item(_("All presets (shuffled)"), vid))
        menu.append_section(None, top)
        for c in (c for c in choices if "/" not in c):
            subs = [s for s in choices if s.startswith(c + "/")]
            if not subs:
                folders.append_item(_vis_item(_(c), f"{vid}|{c}"))
                continue
            sub, inner = Gio.Menu(), Gio.Menu()
            sub.append_item(_vis_item(_("All of {name}").format(name=_(c)), f"{vid}|{c}"))
            for s in subs:
                inner.append_item(_vis_item(s.split("/", 1)[1], f"{vid}|{s}"))
            sub.append_section(None, inner)
            folders.append_submenu(_menu_label(_(c)), sub)
        menu.append_section(None, folders)
        return menu

    def cycle_visualizer(self):
        ids = [i for i in self.visualizers if "|" not in i]
        cur = self.settings.get("visualizer", "").split("|")[0]
        i = ids.index(cur) if cur in ids else -1
        self.set_visualizer(ids[(i + 1) % len(ids)])

    def _update_visualizer(self):
        """Show the visualisation for radio channels and audio-only files."""
        if self.current is not None:
            audio_only = self.current.radio and self.player.playbin is not None
            name = self.current.name
        else:
            audio_only = self.player.is_audio_only()
            name = self.get_title().removesuffix(f" – {APP_NAME}") if audio_only else ""
        self._update_nowplaying(audio_only, name)
        if self.settings.get("vis_show_song") or self.settings.get("vis_show_logo"):
            name = ""                   # the overlay names the station already
        vis_id = self.settings.get("visualizer", DEFAULT_STYLE)
        _label, kind, factory = self.visualizers.get(vis_id) or self.visualizers[DEFAULT_STYLE]
        # a GStreamer visualiser renders into the picture; the drawn ones overlay it
        self.player.set_visualizer(factory if kind == "element" else None)
        drawn = audio_only and kind == "draw"
        if drawn:
            self.vis.set_accent(resolve_accent(self.settings))
        if drawn and vis_id != self._vis_id:
            try:
                self.vis.set_style(factory())
            except Exception as e:
                self.set_status(_("Visualisation failed: {e}").format(e=e))
                self.vis.set_style(None)
            self._vis_id = vis_id
        self.vis.set_caption(name)
        self.vis.set_visible(drawn)
        self.player.analyze = drawn
        self._set_vis_button(audio_only)

    def _set_vis_button(self, enabled):
        """The visualisation button is greyed out unless radio or audio is playing."""
        btn = getattr(self, "vis_btn", None)
        if btn is None:
            return
        if not enabled:
            btn.popdown()
        btn.set_sensitive(enabled)
        btn.set_tooltip_text(_("Radio visualisation") if enabled
                             else _("Radio visualisation (only for radio and audio)"))

    def _update_nowplaying(self, audio_only=None, name=None):
        """Logo, cover, song and clock over the visualisation (Preferences ›
        OSD and Visualisations)."""
        if audio_only is None:
            audio_only = self.vis.get_visible() or (
                self.player.playbin is not None and
                (self.current.radio if self.current else self.player.is_audio_only()))
        np = self.nowplaying
        show = bool(audio_only) and overlay_enabled(self.settings)
        if show:
            programme = ""
            if self.current is not None:
                now, _ = self.app.epg.now_next(self.current.epg_key)
                programme = now.title if now else ""
            if name is None:
                name = self.current.name if self.current else np.station
            np.channel = self.current is not None
            np.set_accent(resolve_accent(self.settings))
            np.set_station(self.player.metadata.get("organization") or name
                           if self.current is None else name, programme,
                           logo=self._channel_logo(self.current) if self.current is not None else "")
            np.tick()
        np.set_visible(show)

    def _on_metadata(self, _player, meta):
        self.nowplaying.set_metadata(meta)
        self._mpris_update()
        if self.nowplaying.get_visible() and self.current is None:
            self._update_nowplaying()

    # ------------------------------------------------------------------ sleep timer
    def sleep_in(self, seconds):
        self.sleep_cancel()
        self.sleep_source = GLib.timeout_add_seconds(int(seconds), self._sleep_fire)
        self.set_status(_("Playback stops at {v0}").format(v0=hm(time.time() + seconds)))

    def sleep_after_programme(self):
        if self.current:
            now, _unused = self.app.epg.now_next(self.current.epg_key)
            if now:
                self.sleep_in(max(1, now.end - time.time()))
                return
        self.set_status(_("No programme information for the sleep timer"))

    def sleep_cancel(self):
        if self.sleep_source:
            GLib.source_remove(self.sleep_source)
            self.sleep_source = None

    def _sleep_fire(self):
        self.sleep_source = None
        self.stop_playback()
        self.set_status(_("Sleep timer: playback stopped"))
        return False

    # ------------------------------------------------------------------ OSD / status
    def show_osd(self, seconds=None):
        c = self.current
        if not c:
            return
        self._osd_secs = seconds
        now, nxt = self.app.epg.now_next(c.epg_key)
        self.osd_num.set_text(c.vchannel or str(c.number))
        self.osd_title.set_text(c.name)
        self.osd_clock.set_text(hm(now.start) + "–" + hm(now.end) if now else hm(time.time()))
        self._update_heading()
        meta = details_line(now) if now else ""
        self.osd_meta.set_text(meta)
        self.osd_meta.set_visible(bool(meta))
        self._update_osd_tags()
        self.quality.tick()
        self._update_quality_bar()
        if now:
            sub = now.subtitle if now.subtitle and not programme_details(now)[3] else ""
            self.osd_now.set_text(now.title + (f" – {sub}" if sub else ""))
            self.osd_prog.set_fraction(min(1, max(0, (time.time() - now.start) / max(1, now.duration))))
            self.osd_prog.set_visible(True)
        else:
            self.osd_now.set_text(_("No programme information"))
            self.osd_prog.set_visible(False)
        self.osd_next.set_text(_("Next: {v0}  {title}").format(v0=hm(nxt.start), title=nxt.title) if nxt else "")
        self.osd.set_visible(True)
        if self._osd_timeout:
            GLib.source_remove(self._osd_timeout)
        self._osd_timeout = GLib.timeout_add_seconds(seconds or self.settings["osd_seconds"],
                                                     self._hide_osd)

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

    def _update_quality_bar(self):
        """Fill and colour the OSD's quality strip from the meter."""
        bar = self.osd_quality
        kind = self.quality.kind
        bar.set_visible(kind is not None)
        if kind is None:
            return
        q, level = self.quality.quality, self.quality.level
        bar.set_fraction(q)
        for lv in ("bad", "poor", "fair", "good"):
            bar.remove_css_class("lv-q-" + lv)
        if level != Q_NONE:
            bar.add_css_class("lv-q-" + level)
        pct = int(round(q * 100))
        if level == Q_NONE:
            bar.set_tooltip_text(_("No signal"))
        elif kind == "dvb":
            bar.set_tooltip_text(_("Signal quality {v0}%").format(v0=pct))
        else:
            bar.set_tooltip_text(_("Stream quality {v0}%").format(v0=pct))

    def _on_stream_buffering(self, _p, pct):
        if self.quality.kind == "iptv":
            self.quality.feed_buffering(pct)
            self._update_quality_bar()

    def _on_stream_state(self, _p, state):
        if self.quality.kind == "iptv" and state == "playing":
            self.quality.stream_playing()
            self._update_quality_bar()

    def _on_stream_failed(self):
        if self.quality.kind == "iptv":
            self.quality.stream_failed()
            self._update_quality_bar()

    def _on_stats(self, cap, st):
        if cap is self.capture and self.quality.kind == "dvb":
            self.quality.feed_stats(st)
            self._update_quality_bar()
            monitor = getattr(self.app, "reception", None)
            if monitor is not None:
                monitor.feed(cap, st, self)
        parts = []
        if "lock" in st and cap is self.capture:
            # no lock for a while: the antenna/cable/satellite instance fails
            if st["lock"]:
                self._nolock_since = None
            elif self._nolock_since is None:
                self._nolock_since = time.monotonic()
            elif time.monotonic() - self._nolock_since > NO_LOCK_SECONDS:
                self._nolock_since = None
                GLib.idle_add(lambda: self._try_failover("no signal") and False)
        if "lock" in st:
            parts.append("LOCK" if st["lock"] else "no lock")
        if "signal" in st:
            parts.append(_("Signal {v0}%").format(v0=st['signal'] * 100 // 65535))
        if "snr" in st:
            parts.append(f"SNR {st['snr'] * 100 // 65535}%")
        if st.get("unc"):
            parts.append(f"UNC {st['unc']}")
        self.signal_label.set_text("  ".join(parts))

    def _on_epg(self):
        self._update_heading()
        if self.nowplaying.get_visible():
            self._update_nowplaying()
        if self.osd.get_visible():
            self.show_osd(getattr(self, "_osd_secs", None))

    def mpris_status(self):
        if not self.player.playbin:
            return "Stopped"
        return "Paused" if self.player.paused else "Playing"

    def _mpris_update(self):
        """Channel / file, programme or song, logo for the desktop's media controls."""
        if self.mpris is None:
            return
        meta = self.player.metadata or {}
        state = {"title": "", "artist": "", "album": "", "art": "", "length": 0}
        if self.current is not None:
            now, _unused = self.app.epg.now_next(self.current.epg_key)
            state["title"] = self.current.name
            state["artist"] = meta.get("title") and " – ".join(
                x for x in (meta.get("artist"), meta.get("title")) if x) or (now.title if now else "")
            logo = self._channel_logo(self.current)
            state["art"] = Path(logo).as_uri() if logo and Path(logo).is_absolute() else ""
        elif 0 <= self.playlist_pos < len(self.playlist):
            item = self.playlist[self.playlist_pos]
            state["title"] = meta.get("title") or Path(item).stem
            state["artist"] = meta.get("artist", "")
            state["album"] = meta.get("album", "")
            pos = self.player.position()
            state["length"] = pos[1] if pos else 0
        self.mpris.update(**state)

    def _update_transport(self):
        self._mpris_update()
        idle = self.player.paused or not self.player.playbin
        self.pause_btn.set_icon_name("media-playback-start-symbolic" if idle
                                     else "media-playback-pause-symbolic")
        rec = self._is_recording()
        if self.rec_btn.get_active() != rec:
            self.rec_btn.set_active(rec)
        n = len(self.app.timers.active)
        self.rec_label.set_text(_("● REC {n}").format(n=n) if n else "")
        if self.controls_revealer.get_parent() is self.overlay and self.player.paused:
            self._show_controls()

    def _tick(self):
        if self.nowplaying.get_visible():
            self.nowplaying.tick()
        if self.capture and self.player.capture:
            d = self.player.timeshift_delay()
            self.ts_label.set_text(_("Timeshift −{v0}:{v1:02d}").format(v0=int(d) // 60, v1=int(d) % 60) if d > 3 else _("Live"))
            if self.status.get_text().startswith(_("Tuning {name}…").split("{")[0]) and self.capture.store.bounds()[1] > 0:
                self.set_status(self.current.name)
            start, end = self.capture.store.bounds()
            buffered = max(1.0, (end - start) / self.capture.byte_rate())
            d = min(d, buffered)
            self._show_seek(buffered - d, buffered, live=True, behind=d)
        else:
            pos = self.player.position()
            self.ts_label.set_text("")
            if pos:
                self._show_seek(pos[0], pos[1])
            else:
                self._show_seek(None, None)
        return True

    # ------------------------------------------------------------------ seek bar
    def _show_seek(self, value, upper, live=False, behind=0.0):
        self.live_btn.set_visible(live)
        self.dur_label.set_visible(not live)
        if value is None:
            self.seek.set_sensitive(False)
            self.seek.set_range(0, 1)
            self.seek.set_value(0)
            self.pos_label.set_text("--:--")
            self.dur_label.set_text("--:--")
            return
        self.seek.set_sensitive(True)
        if live:
            # what is on screen, as time of day; LIVE lights up at the live edge
            self.pos_label.set_text(time.strftime("%H:%M:%S", time.localtime(time.time() - behind)))
            if behind > 3:
                self.live_btn.remove_css_class("lv-at-live")
                self.live_btn.set_label(f"−{_mmss(behind)}")
            else:
                self.live_btn.add_css_class("lv-at-live")
                self.live_btn.set_label(_("LIVE"))
        else:
            self.pos_label.set_text(_mmss(value))
            self.dur_label.set_text(_mmss(upper))
        if self._seek_source is None:            # don't fight the user while dragging
            self.seek.set_range(0, max(1.0, upper))
            self.seek.set_value(value)

    def _on_seek_changed(self, _scale, _scroll, value):
        self._seek_target = value
        if self._seek_source:
            GLib.source_remove(self._seek_source)
        self._seek_source = GLib.timeout_add(250, self._commit_seek)
        return False

    def _commit_seek(self):
        self._seek_source = None
        v = self._seek_target
        if v is None:
            return False
        if self.capture and self.player.capture:
            start, end = self.capture.store.bounds()
            buffered = max(1.0, (end - start) / self.capture.byte_rate())
            shown = buffered - min(self.player.timeshift_delay(), buffered)
            self.player.jump(v - shown)
        else:
            self.player.seek_to(v)
        return False

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
        if self.hbbtv.handle_key(name):              # keys the HbbTV app asked for
            return True
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
                "i": "osd", "n": "playlist-next", "p": "playlist-prev",
                "j": "audio-earlier", "k": "audio-later",
                "v": "vis-next"}
        if name == "Escape" and self._fs_layout:
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
    def show_shortcuts(self):
        from .shortcuts import ShortcutsWindow
        ShortcutsWindow(self).present()

    def on_about(self):
        from .. import COPYRIGHT, WEBSITE, __version__
        from ..credits import CREDITS
        dlg = Gtk.AboutDialog(transient_for=self, program_name=APP_NAME, version=__version__,
                              comments=_("Watch and record DVB-S/S2, DVB-C, DVB-T/T2, ATSC and "
                                         "internet TV with the Linux DVB API and GStreamer."),
                              copyright=COPYRIGHT, website=WEBSITE, authors=["thommy87"],
                              license_type=Gtk.License.GPL_2_0, logo_icon_name="lindvb")
        for title, people in CREDITS:
            dlg.add_credit_section(_(title), list(people))
        dlg.present()

    def _on_close(self, *_):
        self.pip.close()
        self._track_position()
        self._save_positions()
        self.settings.save()
        self._release_capture()
        return False


def _thumb_pixbuf(png, width=80, height=45):
    """A recording preview at list size."""
    try:
        from gi.repository import GdkPixbuf
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(str(png), width * 2, height * 2, False)
    except GLib.Error:
        return None


def _draw_thumb(area, cr, w, h):
    pix = getattr(area, "pixbuf", None)
    if pix is None:
        return
    cr.scale(w / pix.get_width(), h / pix.get_height())
    Gdk.cairo_set_source_pixbuf(cr, pix, 0, 0)
    cr.paint()


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
