# SPDX-License-Identifier: GPL-2.0-or-later
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
import threading
import urllib.request

import gi

import time
import weakref

from ..config import CACHE_DIR, DATA_DIR
from .. import dsmcc
from .. import hbbtv as hb
from ..i18n import _

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

# WebKitGTK hides GStreamer's HLS and DASH demuxers unless asked: without
# them an application's HLS video (and DASH that dash.js cannot load) stays
# black. Set before the first web process starts, which inherits them.
os.environ.setdefault("WEBKIT_GST_ENABLE_HLS_SUPPORT", "1")
os.environ.setdefault("WEBKIT_GST_ENABLE_DASH_SUPPORT", "1")

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


AUTOSTART_DELAY = 1.5          # seconds on a channel before its AUTOSTART app loads
CAROUSEL_TIMEOUT = 30          # seconds to wait for a file of the object carousel
_companion = None               # lindvb.hbbtvcs.CompanionService of this process
_views = weakref.WeakKeyDictionary()     # WebView -> HbbTV
_schemes = set()


def _idle(fn):
    GLib.idle_add(lambda: (fn(), False)[1])


def _register_schemes(view):
    """dvb: (object carousel) for every view, once per web context."""
    ctx = view.get_context()
    if ctx in _schemes:
        return
    _schemes.add(ctx)
    ctx.register_uri_scheme("dvb", _on_dvb_request)
    ctx.register_uri_scheme("lindvb", _on_terminal_request)
    sec = ctx.get_security_manager()
    sec.register_uri_scheme_as_cors_enabled("dvb")
    sec.register_uri_scheme_as_cors_enabled("lindvb")


def _on_terminal_request(request):
    """lindvb:pin?value=1234: verifyParentalControlPIN(), synchronous for the page."""
    hb_ui = _views.get(request.get_web_view())
    if hb_ui is None:
        _request_error(request, "no HbbTV view")
        return
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(request.get_uri())
    answer = ""
    if parts.path.strip("/") == "pin":
        answer = str(hb_ui.pin.verify(parse_qs(parts.query).get("value", [""])[0]))
    _respond(request, answer.encode(), "text/plain")


def _respond(request, data, mime):
    """Finish a custom-scheme request; pages of any origin may read it (CORS)."""
    resp = WebKit.URISchemeResponse.new(Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data)),
                                        len(data))
    resp.set_content_type(mime)
    try:
        gi.require_version("Soup", "3.0")
        from gi.repository import Soup
        headers = Soup.MessageHeaders.new(Soup.MessageHeadersType.RESPONSE)
        headers.append("Access-Control-Allow-Origin", "*")
        headers.append("Cache-Control", "no-store")
        resp.set_http_headers(headers)
    except (ImportError, ValueError):
        pass
    request.finish_with_response(resp)


def _on_dvb_request(request):
    hb_ui = _views.get(request.get_web_view())
    if hb_ui is None:
        _request_error(request, "no HbbTV view")
        return
    hb_ui.serve_dvb(request)


def _request_error(request, message):
    request.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), message,
                                                int(Gio.IOErrorEnum.NOT_FOUND)))


def companion(settings):
    """The companion screen services, started on first use; restarted when
    the network setting changed."""
    global _companion
    network = bool(settings.get("hbbtv_companion", False))
    if _companion is not None and _companion.network != network:
        _companion.stop()
        _companion = None
    if _companion is None:
        from .. import hbbtvcs
        uid = settings.get("hbbtv_device_uuid") or ""
        if not uid:
            import uuid
            uid = str(uuid.uuid4())
            try:
                settings["hbbtv_device_uuid"] = uid
                settings.save()
            except (AttributeError, OSError):
                pass
        import socket
        _companion = hbbtvcs.CompanionService(
            user_agent=hb.user_agent(hb.terminal_version()),
            friendly_name=f"LinDVB ({socket.gethostname()})", call_soon=_idle, device_uuid=uid)
        _companion.start(network=network)
    return _companion


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
        self.pin = hb.PinGuard(self.settings)
        self._unlocked = set()          # apps the PIN opened on this service
        self._media_blocked = {}        # JS id -> minimum age of a video held back
        self._pin_dialog = None
        self.life.allowed = self._allowed
        self.life.on_blocked = self._ask_pin_for_app
        self.cs = None                  # lindvb.hbbtvcs.CompanionService (process-wide)
        self._sync_broadcast = False    # an app's MediaSynchroniser follows the broadcast
        self._sync_selector = None      # inter-device sync on this timeline
        self.dsmcc = None               # lindvb.dsmcc.DsmccService of the capture
        self._carousel_tag = None       # component tag of the running app's carousel
        self._ste = {}                  # JS listener id -> DsmccService handle
        self._drm_label = None          # the "content is encrypted" notice over the video
        self._drm_timeout = None
        self._tuned_at = 0.0            # monotonic time of the last zap
        self._autostart_timer = None

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
        if capture is not self.capture or (self.dsmcc is not None and
                                           getattr(self.dsmcc, "monitor", None) is not
                                           getattr(capture, "ait_monitor", None)):
            self._new_dsmcc(capture)       # another tuner, or another service on it
        self.capture, self._cap_handler = capture, None
        if self._sync_broadcast:
            self._follow_broadcast_timeline(True)
        if self._sync_selector and self.cs is not None:
            self.cs.set_content(self._content_id() or None)
        self.life.autostart = self.settings.get("hbbtv_autostart", False)
        if capture is None and self.enabled and channel is not None and channel.dvbi_apps:
            self._dvbi_signalling(channel, same)
            return
        if capture is None or not self.enabled:
            self.life.no_signalling()
            self._notify()
            return
        if not same:
            self._unlocked = set()
            self._tuned_at = time.monotonic()
            self._cancel_autostart()
            self.life.channel_changed()
        if self.view is not None and self.life.active:
            self._js("channelChanged", self._channel_json(channel))
        self._cap_handler = capture.connect("ait", lambda _c, a: self._on_ait(a))
        if capture.ait is not None:
            self._on_ait(capture.ait)
        self._notify()

    def stopped(self):
        """Live TV stopped (no channel)."""
        self._cancel_autostart()
        self._new_dsmcc(None)
        self.capture = None
        self.life.no_signalling()
        self._notify()

    def _new_dsmcc(self, capture):
        """The broadcast changed: carousels and stream events start over."""
        if self.dsmcc is not None:
            self.dsmcc.close()
            self.dsmcc = None
        if self._ste:
            self._ste = {}
            self._js("streamEventsLost")
        monitor = getattr(capture, "ait_monitor", None)
        if monitor is not None:
            self.dsmcc = dsmcc.DsmccService(monitor, capture.add_pid, _idle)

    # -- parental control ---------------------------------------------------------

    @property
    def age_limit(self):
        try:
            return int(self.settings.get("hbbtv_age_limit", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _allowed(self, app):
        return app.key in self._unlocked or not hb.needs_pin(
            app.parental, self.age_limit, self.settings.get("hbbtv_country", "DEU"))

    def _ask_pin_for_app(self, app, start):
        age = hb.min_age(app.parental, self.settings.get("hbbtv_country", "DEU"))

        def ok():
            self._unlocked.add(app.key)
            start()
            self._notify()
        self.ask_pin(_("This interactive service is rated {age}+.").format(age=age), ok)

    def ask_pin(self, text, on_ok):
        """The youth protection PIN dialog; on_ok() after the right PIN."""
        if not self.pin.has_pin:
            self.win.set_status(_("Blocked by youth protection. Set a PIN in the preferences."))
            return
        if self.pin.locked:
            self.win.set_status(_("Too many wrong PINs. Try again in a few minutes."))
            return
        if self._pin_dialog is not None:
            return
        from gi.repository import Gtk
        parent = getattr(self.win, "window", None) or self.win
        dlg = Gtk.Window(title=_("Youth protection"), modal=True, resizable=False,
                         transient_for=parent if isinstance(parent, Gtk.Window) else None)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                      margin_bottom=18, margin_start=18, margin_end=18)
        box.append(Gtk.Label(label=text, xalign=0, wrap=True))
        entry = Gtk.PasswordEntry(placeholder_text=_("PIN"), activates_default=True)
        box.append(entry)
        msg = Gtk.Label(xalign=0, css_classes=["lv-hint"])
        box.append(msg)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label=_("Cancel"))
        okb = Gtk.Button(label=_("Unlock"), css_classes=["suggested-action"])
        buttons.append(cancel)
        buttons.append(okb)
        box.append(buttons)
        dlg.set_child(box)
        dlg.set_default_widget(okb)
        self._pin_dialog = dlg

        def close(*_):
            self._pin_dialog = None
            dlg.destroy()

        def check(*_):
            r = self.pin.verify(entry.get_text())
            if r == 0:
                close()
                on_ok()
            elif r == 2:
                close()
                self.win.set_status(_("Too many wrong PINs. Try again in a few minutes."))
            else:
                entry.set_text("")
                msg.set_text(_("Wrong PIN."))
        okb.connect("clicked", check)
        entry.connect("activate", check)
        cancel.connect("clicked", close)
        dlg.connect("close-request", lambda *_: (close(), True)[1])
        dlg.present()
        entry.grab_focus()

    def unlock_media(self):
        """The right PIN (dialog or the app's verifyParentalControlPIN): play held videos."""
        if self._pin_dialog is not None:
            self._pin_dialog.destroy()
            self._pin_dialog = None
        for vid in list(self._media_blocked):
            self._js("parentalUnlock", vid)
        self._media_blocked = {}

    def _media_rated(self, vid, age):
        self._media_blocked[vid] = age
        self.ask_pin(_("This video is rated {age}+.").format(age=age), self.unlock_media)

    # -- DRM -------------------------------------------------------------------

    def drm_unsupported(self, system):
        """The app wants to play a protected video: say why it stays dark."""
        name = hb.drm_name(system)
        if name:
            text = _("This video is encrypted with {drm}. LinDVB does not support DRM, "
                     "so it cannot be played.").format(drm=name)
        else:
            text = _("This video is encrypted. LinDVB does not support DRM, "
                     "so it cannot be played.")
        self.win.set_status(text)
        if self._drm_label is None:
            self._drm_label = Gtk.Label(css_classes=["osd"], wrap=True, max_width_chars=60,
                                        justify=Gtk.Justification.CENTER,
                                        halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
                                        margin_bottom=48, can_target=False)
            self.win.overlay.add_overlay(self._drm_label)
        self._drm_label.set_text(text)
        self._drm_label.set_visible(True)
        if self._drm_timeout:
            GLib.source_remove(self._drm_timeout)
        self._drm_timeout = GLib.timeout_add_seconds(8, self._hide_drm_notice)

    def _hide_drm_notice(self):
        self._drm_timeout = None
        if self._drm_label is not None:
            self._drm_label.set_visible(False)
        return False

    # -- object carousel (dvb: URLs) ---------------------------------------------

    def serve_dvb(self, request):
        """Answer a dvb: request from the carousel, waiting for it to arrive."""
        uri = request.get_uri()
        deadline = time.monotonic() + CAROUSEL_TIMEOUT
        svc = self.dsmcc
        done = [False]

        def attempt(final=False):
            if done[0]:
                return True
            if svc is None or svc.closed:
                done[0] = True
                _request_error(request, "no object carousel on this channel")
                return True
            _oc, obj = svc.resolve(uri, self._carousel_tag)
            if obj is None and not final and time.monotonic() < deadline:
                return False                                   # not broadcast yet
            done[0] = True
            if not obj or obj.kind != "fil":
                _request_error(request, "not in the object carousel: " + uri)
                return True
            _respond(request, obj.content, dsmcc.mime_type(dsmcc.parse_dvb_url(uri)[2]))
            return True

        if svc is None:
            attempt(final=True)
            return
        svc.when_ready(attempt)
        if not done[0]:
            GLib.timeout_add_seconds(CAROUSEL_TIMEOUT, lambda: (attempt(final=True), False)[1])

    # -- stream events ---------------------------------------------------------------

    def _stream_event_add(self, msg):
        lid, name = msg.get("lid"), msg.get("name") or ""
        svc = self.dsmcc

        def error():
            self._js("streamEvent", lid, {"name": name, "data": "", "text": "", "status": "error"})

        def deliver(ev):
            if lid in self._ste:
                self._js("streamEvent", lid, {"name": name, "data": ev.data.hex(),
                                              "text": ev.data.decode("utf-8", "replace"),
                                              "status": "trigger"})

        def listen(tag, event_id):
            if self.dsmcc is svc and svc is not None:
                self._ste[lid] = svc.add_listener(tag, event_id, deliver)
            else:
                error()

        if svc is None:
            error()
            return
        if msg.get("url"):
            deadline = time.monotonic() + 10
            done = [False]

            def attempt(final=False):
                if done[0]:
                    return True
                _oc, obj = svc.resolve(msg["url"], self._carousel_tag)
                if obj is None and not final and time.monotonic() < deadline:
                    return False
                done[0] = True
                if obj and obj.kind == "fil":                   # an XML event description
                    found = dsmcc.event_description(obj.content, name)
                    if found:
                        listen(*found)
                        return True
                if obj and obj.kind == "ste" and name in obj.event_names \
                        and obj.event_tag() is not None:
                    i = obj.event_names.index(name)
                    if i < len(obj.event_ids):
                        listen(obj.event_tag(), obj.event_ids[i])
                        return True
                error()
                return True
            svc.when_ready(attempt)
            if not done[0]:
                GLib.timeout_add_seconds(10, lambda: (attempt(final=True), False)[1])
        else:
            listen(int(msg.get("tag", -1)), int(msg.get("eventId", -1)))

    def _stream_event_remove(self, lid):
        h = self._ste.pop(lid, None)
        if h is not None and self.dsmcc is not None:
            self.dsmcc.remove_listener(h)

    def _dvbi_signalling(self, channel, same):
        """A DVB-I internet channel: its service list names the applications
        (an XML AIT or a page) instead of an AIT in the broadcast."""
        if not same:
            self._unlocked = set()
            self.life.channel_changed()
        if self.view is not None and self.life.active:
            self._js("channelChanged", self._channel_json(channel))
        key, apps, app_only = channel.key, list(channel.dvbi_apps), not channel.url
        ua = hb.user_agent(hb.terminal_version())

        def fetch(url):
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read(1 << 20)

        def done(ait):
            if self._channel_key != key or self.capture is not None:
                return False                     # zapped away meanwhile
            if ait is None:
                self.life.no_signalling()
                self._notify()
            else:
                self._on_ait(ait)
                if app_only:
                    self.life.start_service_app()
                    self._notify()
            return False

        threading.Thread(target=lambda: GLib.idle_add(done, hb.dvbi_ait(apps, fetch)),
                         daemon=True).start()
        self._notify()

    def _on_ait(self, ait):
        wait = AUTOSTART_DELAY - (time.monotonic() - self._tuned_at)
        if self.life.autostart and not self.life.active and wait > 0:
            # zapping through: start the app only once the viewer stays on
            # the channel, so passing channels load no pages
            self.life.ait = ait                 # red and TXT work meanwhile
            self._cancel_autostart()
            key = self._channel_key
            self._autostart_timer = GLib.timeout_add(int(wait * 1000) + 1,
                                                     self._deferred_autostart, key)
            self._notify()
            return
        self.life.on_ait(ait)
        self._notify()

    def _deferred_autostart(self, key):
        self._autostart_timer = None
        if key == self._channel_key and self.life.ait is not None and self.enabled:
            self.life.on_ait(self.life.ait)
            self._notify()
        return False

    def _cancel_autostart(self):
        if self._autostart_timer:
            GLib.source_remove(self._autostart_timer)
            self._autostart_timer = None

    def _notify(self):
        if self.on_state:
            ait = self.life.ait
            self.on_state(bool(ait and ait.autostart()), self.life.active)

    @property
    def has_teletext_app(self):
        return bool(self.enabled and self.settings.get("hbbtv_teletext", True)
                    and self.life.ait and self.life.ait.teletext())

    def teletext_button(self):
        """The TXT key: start the digital teletext application, hand the key
        to an application that asked for it, or close the teletext app.
        False: the classic teletext should open."""
        if not self.enabled:
            return False
        if self.life.active:
            if hb.wanted(hb.VK["TELETEXT"], self.keyset, self.other_keys):
                self._js("key", hb.VK["TELETEXT"])
                return True
            tt = self.life.ait.teletext() if self.life.ait else None
            if tt is not None and self.life.running is not None and self.life.running.key == tt.key:
                self.life.stop(by_user=True)              # TXT again: back to TV
                self._notify()
                return True
        if not self.has_teletext_app:
            return False
        started = self.life.teletext_button()
        self._notify()
        return started

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
        s.set_enable_mediasource(True)              # dash.js plays DASH through MSE
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
        _views[view] = self
        _register_schemes(view)
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
                       "audioDescription": bool(self.settings.get("hbbtv_audio_description", False)),
                       "ageLimit": self.age_limit, "hasPin": self.pin.has_pin},
            "app": {"org": app.org_id, "id": app.app_id} if app else None,
            "cs": self._cs_init(app),
        }
        return hb.page_script(json.dumps(init))

    def _cs_init(self, app):
        try:
            svc = self._companion()
        except OSError:
            return {}
        return {"localBase": svc.app2app_local_base, "remoteBase": svc.app2app_remote_base,
                "syncUrl": svc.inter_dev_sync_url, "launchUrl": svc.app_launch_url,
                "contentId": self._content_id()}

    def _companion(self):
        if self.cs is None or self.cs is not _companion or self.cs.loop is None:
            self.cs = companion(self.settings)
            self.cs.on_launch = self._on_cs_launch
            self.cs.on_slaves = lambda n: self._js("syncSlaves", n)
        return self.cs

    def _content_id(self):
        c = self.win.current
        if c is None or not getattr(c, "sid", 0):
            return ""
        return f"dvb://{c.onid:x}.{c.tsid:x}.{c.sid:x}"

    def _on_cs_launch(self, info, reply):
        """A companion asks to start an application (DIAL, XML AIT): the user decides."""
        from gi.repository import Gtk
        parent = getattr(self.win, "window", None) or self.win
        dlg = Gtk.AlertDialog(
            message=_("Start an application from another device?"),
            detail=_("A device on your network wants to start “{name}” ({url}).").format(
                name=info.get("name") or _("an HbbTV application"), url=info["url"]),
            buttons=[_("Refuse"), _("Start")], cancel_button=0, default_button=1)

        def done(d, res):
            try:
                ok = d.choose_finish(res) == 1
            except GLib.Error:
                ok = False
            reply(ok)
            if ok and self.enabled:
                self.life.create_application(info["url"])
                self._notify()
        dlg.choose(parent if isinstance(parent, Gtk.Window) else None, None, done)

    # -- media synchronisation --------------------------------------------------------

    def _display_delay_ns(self):
        """How long a frame takes from the demux to the screen, roughly."""
        pb = getattr(self.win.player, "playbin", None)
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            q = Gst.Query.new_latency()
            if pb is not None and hasattr(pb, "query") and pb.query(q):
                _live, lo, _hi = q.parse_latency()
                return int(lo)
        except (ImportError, ValueError, TypeError):
            pass
        return 0

    def _follow_broadcast_timeline(self, on):
        self._sync_broadcast = on
        mon = getattr(self.capture, "ait_monitor", None)
        if mon is None:
            return
        if not on:
            mon.on_pts = None
            return
        delay = self._display_delay_ns()

        def got(pts, at):
            def deliver():
                if self._sync_broadcast and self.view is not None:
                    self._js("timeline", pts, at + delay, time.monotonic_ns())
            _idle(deliver)
        mon.on_pts = got

    def _sync_message(self, cmd, msg):
        svc = self._companion()
        if cmd == "sync-broadcast":
            self._follow_broadcast_timeline(bool(msg.get("on")))
        elif cmd == "sync-enable":
            self._sync_selector = msg.get("selector") or ""
            ups = 90000 if self._sync_selector.endswith(":pts") else 1000
            svc.set_content(self._content_id() or None, timelines=[{
                "timelineSelector": self._sync_selector,
                "timelineProperties": {"unitsPerTick": 1, "unitsPerSecond": ups}}])
        elif cmd == "sync-ct" and self._sync_selector:
            wall = msg.get("wallNs")
            svc.set_timeline(msg.get("selector") or self._sync_selector,
                             msg.get("contentTime") if wall is not None else None,
                             wall if wall is not None else time.monotonic_ns(),
                             float(msg.get("speed") or 0) if msg.get("speed") is not None else 1.0)
        elif cmd == "sync-disable":
            if self._sync_selector:
                svc.set_timeline(self._sync_selector, None, time.monotonic_ns())
            svc.set_content(None, presentation="fault", timelines=[])
            self._sync_selector = None

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
        self._carousel_tag = None
        ait = self.life.ait
        t = ait.transport_for(app) if ait is not None and app is not None else None
        if t is not None and t.protocol == hb.aitmod.PROTOCOL_OBJECT_CAROUSEL:
            self._carousel_tag = t.component_tag
        self._ste_clear()
        view.set_visible(True)
        view.load_uri(url)
        name = app.name(("deu", "ger", "eng")) if app else ""
        self.win.set_status(_("HbbTV: {name}").format(name=name or url))

    def _ste_clear(self):
        if self.dsmcc is not None:
            for h in self._ste.values():
                self.dsmcc.remove_listener(h)
        self._ste = {}

    def _kill(self):
        self._ste_clear()
        if self._sync_broadcast:
            self._follow_broadcast_timeline(False)
        if self._sync_selector and self.cs is not None:
            self._sync_message("sync-disable", {})
        self._media_blocked = {}
        if self._drm_timeout:
            GLib.source_remove(self._drm_timeout)
        self._hide_drm_notice()
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
        elif kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if hb.parse_ait_url(uri) is not None:
                # location.href = "dvb://current.ait/..": an AIT application
                decision.ignore()
                GLib.idle_add(lambda: (self._create(uri), False)[1])
                return True
        return False

    def _create(self, url):
        """createApplication(): start it, or tell the app it is not there."""
        if not self.life.create_application(url):
            self.win.set_status(_("HbbTV: the application is not signalled on this channel"))
            self._js("createFailed", url)
        self._notify()

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
        elif cmd == "cs-discover-terminals":
            cid = msg.get("id")
            self._companion().discover_terminals(lambda found: self._js("csResult", cid, found))
        elif cmd in ("sync-broadcast", "sync-enable", "sync-ct", "sync-disable"):
            self._sync_message(cmd, msg)
        elif cmd == "drm-unsupported":
            self.drm_unsupported(msg.get("system") or "")
        elif cmd == "pc-blocked":
            self._media_rated(msg.get("id"), int(msg.get("age") or 0))
        elif cmd == "pc-unlocked":                 # the app checked the PIN itself
            self.unlock_media()
        elif cmd == "ste-add":
            self._stream_event_add(msg)
        elif cmd == "ste-remove":
            self._stream_event_remove(msg.get("lid"))
        elif cmd == "create":
            url = msg.get("url") or ""
            if url.startswith(("http://", "https://", "dvb://")):
                self._create(url)
        elif cmd == "media-error":
            self._media_error(msg)
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

    def _media_error(self, msg):
        """The app's own video failed: say so (the picture would only stay black)."""
        detail = str(msg.get("message") or "")[:200]
        print(f"LinDVB HbbTV: video failed: {msg.get('url') or ''} {detail}", flush=True)
        self.win.set_status(_("HbbTV video could not be played: {msg}").format(
            msg=detail or _("unknown error")))

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
