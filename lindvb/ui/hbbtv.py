"""HbbTV in the main window: a transparent WebKitGTK view over the video.

The terminal side of lindvb/hbbtv.js. One HbbTV controller per main window:
it follows the live channel's AIT (lindvb.ait), runs the application the
lifecycle picks (lindvb.hbbtv.Lifecycle), forwards the keys the application
asked for, and scales the broadcast video when the application shows it in a
window of its own.
"""

import json
import os
import tempfile

import gi

from ..config import CACHE_DIR, DATA_DIR
from .. import hbbtv as hb
from ..i18n import _

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib  # noqa: E402

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402
except (ValueError, ImportError):
    WebKit = None


def available():
    """HbbTV needs WebKitGTK 6 (GTK 4) and a working WebKit sandbox."""
    return unavailable_reason() is None


def unavailable_reason():
    """Why HbbTV cannot run here (a sentence for the user), or None."""
    if WebKit is None:
        return _("Needs WebKitGTK 6 (gir1.2-webkit-6.0), which is not installed.")
    if not hb.sandbox_ok():
        return _("WebKit's sandbox may not create user namespaces here (Ubuntu's "
                 "AppArmor restriction), so applications cannot start safely. See "
                 "docs/HBBTV.md.")
    return None


def clear_data(app):
    """Delete what HbbTV applications stored (cookies, local storage, cache)."""
    import shutil
    win = getattr(app, "window", None)
    hb_ui = getattr(win, "hbbtv", None)
    if hb_ui is not None and hb_ui.view is not None:
        hb_ui.close()
        mgr = hb_ui.view.get_network_session().get_website_data_manager()
        mgr.clear(WebKit.WebsiteDataTypes.ALL, 0, None, None, None)
    for d in (DATA_DIR / "hbbtv", CACHE_DIR / "hbbtv"):
        if hb_ui is None or hb_ui.view is None:
            shutil.rmtree(d, ignore_errors=True)
    if win is not None:
        win.set_status(_("HbbTV data deleted"))


class HbbTV:
    """HbbTV terminal for one main window (window.overlay, window.picture)."""

    def __init__(self, window):
        self.win = window
        self.settings = window.settings
        self.view = None
        self.capture = None
        self._cap_handler = None
        self.keyset = 0
        self.other_keys = ()
        self._zoom = 0.0
        self.video_rect = None          # (x, y, w, h) in app coordinates, None = full
        self.life = hb.Lifecycle(self._launch, self._kill,
                                 autostart=self.settings.get("hbbtv_autostart", False))
        self.on_state = None            # callback(has_app, running) for the header button
        self._pending_red = False
        self._broadcast_stopped = False
        self._ccids = {}
        self._channel_key = None
        self._picture_hidden = False
        self._tracks_handler = None

    # -- channel -------------------------------------------------------------

    @property
    def enabled(self):
        return available() and self.settings.get("hbbtv_enabled", True)

    def channel_changed(self, channel, capture):
        """The live view tuned *channel*; *capture* is None for internet channels."""
        if self.capture is not None and self._cap_handler:
            try:
                self.capture.disconnect(self._cap_handler)
            except TypeError:
                pass
        same = channel is not None and channel.key == self._channel_key
        self._channel_key = channel.key if channel is not None else None
        self.capture, self._cap_handler = capture, None
        self.life.autostart = self.settings.get("hbbtv_autostart", False)
        if capture is None or not self.enabled:
            self.life.no_signalling()
            self._notify()
            return
        if not same:
            self.life.channel_changed()
        if self.view is not None and self.life.active:
            self._js("channelChanged", self._channel_json(channel))
        self._cap_handler = capture.connect("ait", lambda _c, a: self._on_ait(a))
        if capture.ait is not None:
            self._on_ait(capture.ait)
        self._notify()

    def stopped(self):
        """Live TV stopped (no channel)."""
        self.life.no_signalling()
        self._notify()

    def _on_ait(self, ait):
        self.life.on_ait(ait)
        self._notify()

    def _notify(self):
        if self.on_state:
            ait = self.life.ait
            self.on_state(bool(ait and ait.autostart()), self.life.active)

    @property
    def has_app(self):
        return bool(self.life.ait and self.life.ait.autostart())

    # -- keys ----------------------------------------------------------------

    def handle_key(self, gdk_name):
        """Called by the window first. True when HbbTV took the key."""
        if not self.enabled:
            return False
        code = hb.key_code(gdk_name)
        if self.life.active:
            if gdk_name == "Escape":                # EXIT: end the application
                self.life.stop(by_user=True)
                self._notify()
                return True
            if code and hb.wanted(code, self.keyset, self.other_keys):
                self._js("key", code)
                return True
            return False
        if code == hb.VK["RED"]:
            started = self.life.red_button()
            # the app was not running: hand it the red key once it has loaded
            self._pending_red = started
            return started
        return False

    def red_button(self):
        """Header button / menu: like the red key."""
        if self.life.active:
            if self.keyset & hb.RED:
                self._js("key", hb.VK["RED"])
            return True
        return self.handle_key("F5")

    def close(self):
        if self.life.active:
            self.life.stop(by_user=True)
            self._notify()

    # -- browser -------------------------------------------------------------

    def _ensure_view(self):
        if self.view is not None:
            return self.view
        data = DATA_DIR / "hbbtv"
        cache = CACHE_DIR / "hbbtv"
        data.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        session = WebKit.NetworkSession.new(str(data), str(cache))
        cookies = session.get_cookie_manager()
        cookies.set_persistent_storage(str(data / "cookies.sqlite"),
                                       WebKit.CookiePersistentStorage.SQLITE)
        cookies.set_accept_policy(WebKit.CookieAcceptPolicy.ALWAYS
                                  if self.settings.get("hbbtv_third_party_cookies", False)
                                  else WebKit.CookieAcceptPolicy.NO_THIRD_PARTY)
        session.connect("download-started", self._on_download)
        ucm = WebKit.UserContentManager()
        ucm.register_script_message_handler("lindvb", None)
        ucm.connect("script-message-received::lindvb", self._on_message)
        view = WebKit.WebView(network_session=session, user_content_manager=ucm,
                              hexpand=True, vexpand=True, can_focus=False, focusable=False)
        s = view.get_settings()
        s.set_user_agent(hb.user_agent(hb.terminal_version()))
        s.set_enable_javascript(True)
        s.set_javascript_can_open_windows_automatically(False)
        s.set_media_playback_requires_user_gesture(False)
        s.set_allow_file_access_from_file_urls(False)
        s.set_allow_universal_access_from_file_urls(False)
        s.set_enable_developer_extras(bool(os.environ.get("LINDVB_HBBTV_DEBUG")))
        s.set_enable_write_console_messages_to_stdout(bool(os.environ.get("LINDVB_HBBTV_DEBUG")))
        s.set_default_font_family("sans-serif")
        s.set_enable_back_forward_navigation_gestures(False)
        clear = Gdk.RGBA()
        clear.parse("rgba(0,0,0,0)")
        view.set_background_color(clear)        # the video shows through the page
        view.connect("decide-policy", self._on_policy)
        view.connect("load-failed", self._on_load_failed)
        view.connect("load-changed", self._on_load_changed)
        view.connect("context-menu", lambda *_: True)        # no browser menu on the TV
        view.set_visible(False)
        self.view = view
        self.win.overlay.add_overlay(view)
        view.add_tick_callback(self._tick)
        return view

    def _init_script(self, url, app):
        win = self.win
        db = win.app.db
        chans = [c for c in db.channels if not c.is_iptv]
        ccids = {c.key: f"ccid:{i + 1}" for i, c in enumerate(chans)}
        self._ccids = {ccids[c.key]: c for c in chans}
        cur = win.current
        from .. import i18n
        lang = hb.viewer_language(self.settings, getattr(i18n, "current", "") or
                                  os.environ.get("LANG", ""))
        init = {
            "version": hb.terminal_version(),
            "channels": [hb.channel_dict(c, ccids[c.key]) for c in chans],
            "current": self._channel_json(cur),
            "programmes": self._programmes(cur),
            "capabilities": hb.capabilities_xml(),
            "components": self._components(),
            "config": {"audioLanguage": lang, "subtitleLanguage": lang, "uiLanguage": lang,
                       "country": self.settings.get("hbbtv_country", "DEU"),
                       "subtitles": bool(self.settings.get("hbbtv_subtitles", False)),
                       "audioDescription": bool(self.settings.get("hbbtv_audio_description", False))},
            "app": {"org": app.org_id, "id": app.app_id} if app else None,
        }
        return hb.page_script(json.dumps(init))

    def _channel_json(self, c):
        if c is None:
            return None
        ccid = next((k for k, v in self._ccids.items() if v.key == c.key), "ccid:0")
        return hb.channel_dict(c, ccid)

    def _programmes(self, c):
        if c is None:
            return []
        now, nxt = self.win.app.epg.now_next(c.epg_key)
        return [hb.event_dict(e) for e in (now, nxt) if e is not None]

    def _components(self):
        """The live picture's audio and subtitle tracks, for video/broadcast."""
        pl = self.win.player
        if not hasattr(pl, "audio_track_info"):
            return []
        try:
            audio, acur = pl.audio_track_info()
            labels, scur = pl.subtitle_tracks()
            pages = pl.teletext_pages()
        except Exception:
            return []
        n = len(labels) - len(pages)
        subs = [(label, pages[i - n].language if 0 <= i - n < len(pages) else "")
                for i, label in enumerate(labels)]
        return hb.broadcast_components(audio, acur, subs, scur)

    def _on_tracks(self, *_):
        if self.view is not None and self.life.active:
            self._js("components", self._components())

    def _launch(self, url, app):
        if not self._tracks_handler and hasattr(self.win.player, "connect"):
            self._tracks_handler = self.win.player.connect("tracks", self._on_tracks)
        view = self._ensure_view()
        ucm = view.get_user_content_manager()
        ucm.remove_all_scripts()
        ucm.add_script(WebKit.UserScript.new(
            self._init_script(url, app), WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START, None, None))
        self.keyset, self.other_keys = 0, ()
        self.set_video_rect(None)
        view.set_visible(True)
        view.load_uri(url)
        name = app.name(("deu", "ger", "eng")) if app else ""
        self.win.set_status(_("HbbTV: {name}").format(name=name or url))

    def _kill(self):
        self._pending_red = False
        self.keyset, self.other_keys = 0, ()
        self.set_video_rect(None)
        if self.view is not None:
            self.view.set_visible(False)
            self.view.load_uri("about:blank")
        GLib.idle_add(lambda: (self._resume_broadcast(), False)[1])

    def _resume_broadcast(self):
        """Back to the live picture after the app stopped it (or played its own video)."""
        if self._broadcast_stopped:
            self._broadcast_stopped = False
            if self.win.current is not None:
                self.win.zap(self.win.current)

    def _js(self, fn, *args):
        if self.view is None:
            return
        code = f"window.__lindvb && window.__lindvb.{fn}(" + ",".join(json.dumps(a) for a in args) + ");"
        self.view.evaluate_javascript(code, -1, None, None, None, None, None)

    # HbbTV documents (application/vnd.hbbtv.xhtml+xml) are not a type WebKit
    # shows: take the response as a download and load it as HTML at its URL.
    def _on_policy(self, view, decision, kind):
        if kind == WebKit.PolicyDecisionType.RESPONSE:
            resp = decision.get_response()
            mime = (resp.get_mime_type() or "").lower()
            if decision.is_main_frame_main_resource() and mime in hb.DOCUMENT_TYPES:
                decision.download()
                return True
            if not decision.is_mime_type_supported():
                decision.ignore()                       # never save files from an app
                return True
        elif kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False

    def _on_download(self, _session, download):
        path = tempfile.mktemp(prefix="hbbtv-", dir=str(CACHE_DIR / "hbbtv"))

        def dest(d, _name):
            d.set_destination(path)
            return True

        def finished(d):
            uri = d.get_request().get_uri()
            try:
                with open(path, "rb") as f:
                    html = f.read().decode("utf-8", "replace")
                os.unlink(path)
            except OSError:
                return
            if self.view is not None and self.life.active:
                self.view.load_alternate_html(html, uri, uri)

        download.connect("decide-destination", dest)
        download.connect("finished", finished)

    def _on_load_failed(self, _view, _event, uri, error):
        if uri == "about:blank" or error.matches(WebKit.NetworkError.quark(),
                                                 WebKit.NetworkError.CANCELLED):
            return False
        if error.matches(WebKit.PolicyError.quark(),
                         WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE):
            return False                                  # turned into the HTML load
        self.win.set_status(_("HbbTV application could not be loaded: {msg}").format(msg=error.message))
        self.life.stop()
        self._notify()
        return False

    def _on_load_changed(self, _view, event):
        if event == WebKit.LoadEvent.FINISHED and self._pending_red:
            self._pending_red = False
            GLib.timeout_add(1500, lambda: (self._js("key", hb.VK["RED"]), False)[1])

    def _on_message(self, _ucm, value):
        try:
            msg = json.loads(value.to_string())
        except (ValueError, AttributeError):
            return
        cmd = msg.get("cmd")
        if cmd == "keyset":
            self.keyset = int(msg.get("value") or 0)
            self.other_keys = tuple(msg.get("otherKeys") or ())
        elif cmd == "show":
            self.view.set_visible(True)
        elif cmd == "hide":
            self.view.set_visible(False)
        elif cmd == "destroy":
            self.life.stop()
            self._notify()
        elif cmd == "create":
            url = msg.get("url") or ""
            if url.startswith(("http://", "https://")):
                self.life.create_application(url)
        elif cmd == "video-rect":
            if msg.get("full"):
                self.set_video_rect(None)
            elif msg.get("hidden"):
                self.set_video_rect((0, 0, 0, 0))
            else:
                self.set_video_rect((msg["x"], msg["y"], msg["w"], msg["h"]))
        elif cmd == "set-channel":
            self._set_channel(msg)
        elif cmd == "channel-step":
            self.win.zap_relative(int(msg.get("step") or 1))
        elif cmd == "stop-broadcast":
            self._broadcast_stopped = True
            self.win.player.stop()
        elif cmd == "bind":
            self._resume_broadcast()
        elif cmd == "volume":
            v = max(0, min(100, int(msg.get("value") or 0)))
            self.win.vol.set_value(v / 100)
        elif cmd == "av-play":
            self._broadcast_stopped = True
            self.win.player.stop()                          # the app plays its own video
        elif cmd == "av-stop":
            pass
        elif cmd == "select-component":
            i = int(msg.get("index", -1))
            if msg.get("type") == 1:
                self.win.player.select_audio(i)
            elif msg.get("type") == 2:
                self.win.player.select_subtitle(i)
            GLib.timeout_add(300, lambda: (self._on_tracks(), False)[1])

    def _set_channel(self, msg):
        c = self._ccids.get(msg.get("ccid"))
        if c is None:
            c = next((x for x in self.win.app.db.channels if not x.is_iptv and
                      x.onid == msg.get("onid") and x.sid == msg.get("sid") and
                      (not msg.get("tsid") or x.tsid == msg.get("tsid"))), None)
        if c is None:
            self._js("channelChanged", None, 5)           # unknown channel
            return
        self.win.zap(c)

    # -- geometry ------------------------------------------------------------

    def place(self, overlay, rect):
        """Overlay position of the view: the 16:9 area the video fills."""
        w, h = overlay.get_width(), overlay.get_height()
        vw = min(w, h * 16 // 9)
        vh = vw * 9 // 16
        rect.x, rect.y = (w - vw) // 2, (h - vh) // 2
        rect.width, rect.height = vw, vh
        return True

    def _tick(self, view, _clock):
        width = view.get_width()
        if width > 0:
            zoom = width / hb.APP_WIDTH
            if abs(zoom - self._zoom) > 0.001:
                self._zoom = zoom
                view.set_zoom_level(zoom)
                self._apply_video_rect()
        return GLib.SOURCE_CONTINUE

    def _hide_picture(self, hide):
        """The app hid the broadcast video object (it is not on screen)."""
        if hide != self._picture_hidden:
            self._picture_hidden = hide
            self.win.picture.set_opacity(0 if hide else 1)

    def set_video_rect(self, rect):
        self.video_rect = rect
        self._apply_video_rect()

    def _apply_video_rect(self):
        pic = self.win.picture
        rect = self.video_rect
        if rect is None or self.view is None or not self.view.get_visible():
            for side in ("start", "end", "top", "bottom"):
                getattr(pic, f"set_margin_{side}")(0)
            self._hide_picture(False)
            return
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            self._hide_picture(True)
            return
        self._hide_picture(False)
        ow, oh = self.win.overlay.get_width(), self.win.overlay.get_height()
        vw = min(ow, oh * 16 // 9)
        vh = vw * 9 // 16
        ox, oy = (ow - vw) / 2, (oh - vh) / 2
        sx, sy = vw / hb.APP_WIDTH, vh / hb.APP_HEIGHT
        left, top = int(ox + x * sx), int(oy + y * sy)
        right, bottom = int(ow - (ox + (x + w) * sx)), int(oh - (oy + (y + h) * sy))
        pic.set_margin_start(max(0, left))
        pic.set_margin_top(max(0, top))
        pic.set_margin_end(max(0, right))
        pic.set_margin_bottom(max(0, bottom))
