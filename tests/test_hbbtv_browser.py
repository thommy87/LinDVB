"""HbbTV end to end in WebKitGTK: an application served as
application/vnd.hbbtv.xhtml+xml starts from the AIT, sets its key set, binds
the broadcast video into a window of its own and gets the red key.

Needs a display (run under xvfb-run) and WebKitGTK 6; skipped otherwise.
"""

import http.server
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import GLib, GObject, Gtk, WebKit  # noqa: F401
except (ImportError, ValueError):
    pytest.skip("WebKitGTK 6 not available", allow_module_level=True)

if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("no display (use xvfb-run)", allow_module_level=True)

from lindvb import ait  # noqa: E402
from lindvb.ait import Ait, AitApp, Transport  # noqa: E402
from lindvb.channels import Channel  # noqa: E402

APP = b"""<!DOCTYPE html PUBLIC "-//HbbTV//1.1.1//EN" "http://www.hbbtv.org/dtd/HbbTV-1.1.1.dtd">
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>loading</title>
<script type="text/javascript">
window.onload = function () {
  var app = document.getElementById('am').getOwnerApplication(document);
  app.privateData.keyset.setValue(app.privateData.keyset.RED | app.privateData.keyset.NAVIGATION);
  app.show();
  var vb = document.getElementById('vb');
  vb.bindToCurrentChannel();
  vb.setFullScreen(false);
  var caps = document.getElementById('cap');
  document.addEventListener('keydown', function (e) {
    document.title = 'key:' + e.keyCode + ':' + vb.playState + ':' + vb.currentChannel.name + ':' +
      caps.hasCapability('DVB_S') + ':' + (e.keyCode === KeyEvent.VK_RED) + ':' +
      navigator.userAgent.indexOf('HbbTV/1.7.1') + ':' +
      vb.getChannelConfig().channelList.length + ':' +
      vb.getComponents(1).map(function (c) { return c.language; }).join('/') + ':' +
      vb.getCurrentActiveComponents(2).length;
    vb.selectComponent(vb.getComponents(1)[1]);
  });
  document.title = 'ready:' + document.getElementById('cfg').configuration.countryId;
};
</script></head><body style="margin:0">
<object id="am" type="application/oipfApplicationManager"></object>
<object id="cap" type="application/oipfCapabilities"></object>
<object id="cfg" type="application/oipfConfiguration"></object>
<object id="vb" type="video/broadcast"
        style="position:absolute;left:640px;top:0px;width:640px;height:360px"></object>
</body></html>
"""


DASH_APP = b"""<html xmlns="http://www.w3.org/1999/xhtml"><head><title>loading</title>
<script type="text/javascript">
var log = [];
function show() { document.title = log.join(' '); }
window.onload = function () {
  // HbbTV 2.0: an HTML5 video with an MPEG-DASH manifest
  var v = document.getElementById('v');
  v.muted = true;
  v.src = 'dash/manifest.mpd';
  log.push('src=' + (v.src.indexOf('manifest.mpd') > 0));
  v.addEventListener('timeupdate', function () {
    if (v.currentTime > 1 && log.indexOf('html5') < 0) { log.push('html5'); show(); v.pause(); avObject(); }
  });
  v.play();
};
function avObject() {
  // HbbTV 1.x: an A/V control object with application/dash+xml
  var av = document.getElementById('av');
  av.data = 'dash/manifest.mpd';
  av.onPlayStateChange = function () {
    if (av.playState === 1 && log.indexOf('av-playing') < 0) {
      log.push('av-playing');
      setTimeout(function () {
        var audio = av.getComponents(1);
        log.push('audio=' + audio.map(function (c) { return c.language; }).join(','));
        av.selectComponent(audio[1]);
        setTimeout(function () {
          var cur = av.getCurrentActiveComponents(1);
          log.push('active=' + (cur.length ? cur[0].language : '-'));
          log.push('dashjs=' + (typeof window.dashjs));
          show();
        }, 500);
      }, 300);
    }
    show();
  };
  av.play(1);
}
</script></head><body style="margin:0">
<video id="v" style="position:absolute;left:0;top:0;width:320px;height:180px"></video>
<object id="av" type="application/dash+xml"
        style="position:absolute;left:400px;top:0;width:320px;height:180px"></object>
</body></html>
"""

SUB_APP = b"""<html xmlns="http://www.w3.org/1999/xhtml"><head><title>loading</title>
<script type="text/javascript">
window.onload = function () {
  var av = document.getElementById('av');
  av.data = 'dash/manifest-subtitles.mpd';
  av.onPlayStateChange = function () {
    if (av.playState !== 1 || av.__done) { return; }
    av.__done = true;
    setTimeout(function () {
      var subs = av.getComponents(2);
      var first = document.querySelector('[data-lindvb=subtitles]');
      var before = first ? first.textContent : 'no layer';
      av.selectComponent(subs[0]);
      var tries = 0;
      var poll = setInterval(function () {
        var layer = document.querySelector('[data-lindvb=subtitles]');
        var text = layer ? layer.textContent : '';
        if (text || ++tries > 40) {
          clearInterval(poll);
          document.title = 'subs=' + subs.map(function (c) { return c.language; }).join(',') +
            ' before=' + (before || '-') + ' shown=' + (layer ? text : 'no layer');
        }
      }, 200);
    }, 300);
  };
  av.play(1);
};
</script></head><body style="margin:0">
<object id="av" type="video/mp4" style="position:absolute;left:0;top:0;width:640px;height:360px"></object>
</body></html>
"""

DATA = os.path.join(os.path.dirname(__file__), "data")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/dash/"):
            name = os.path.basename(path)
            try:
                with open(os.path.join(DATA, "dash", name), "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            ctype = "application/dash+xml" if name.endswith(".mpd") else \
                "application/ttml+xml" if name.endswith(".ttml") else \
                ("video/mp4" if "stream0" in name else "audio/mp4")
        else:
            body = DASH_APP if path.startswith("/dashapp") else \
                SUB_APP if path.startswith("/subapp") else APP
            ctype = "application/vnd.hbbtv.xhtml+xml"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        if os.environ.get("LINDVB_TEST_HTTP_LOG"):
            sys.stderr.write((a[0] % a[1:]) + "\n")


class FakeCapture(GObject.Object):
    __gsignals__ = {"ait": (GObject.SignalFlags.RUN_LAST, None, (object,))}

    def __init__(self):
        super().__init__()
        self.ait = None


class FakeEPG:
    def now_next(self, key):
        return None, None


class FakeApp:
    def __init__(self, channels):
        self.db = type("DB", (), {"channels": channels})()
        self.epg = FakeEPG()


class FakePlayer(GObject.Object):
    __gsignals__ = {"tracks": (GObject.SignalFlags.RUN_LAST, None, ())}
    playbin = object()

    def __init__(self):
        super().__init__()
        self.stops = 0
        self.selected = []

    def stop(self):
        self.stops += 1

    def audio_track_info(self):
        from lindvb.media_info import AudioTrack
        return [AudioTrack("deu", codec="MPEG-1 Layer 2"), AudioTrack("eng")], 0

    def subtitle_tracks(self):
        return ["Teletext 150 (deu)"], -1

    def teletext_pages(self):
        from lindvb.teletext import TeletextPage
        return [TeletextPage(150, "deu")]

    def select_audio(self, i):
        self.selected.append(("audio", i))

    def select_subtitle(self, i):
        self.selected.append(("subtitle", i))


class FakeWindow:
    def __init__(self, channel):
        self.settings = {"hbbtv_enabled": True, "hbbtv_autostart": True}
        self.window = Gtk.Window(default_width=1280, default_height=720)
        self.picture = Gtk.Picture(hexpand=True, vexpand=True)
        self.overlay = Gtk.Overlay(child=self.picture)
        self.window.set_child(self.overlay)
        self.app = FakeApp([channel, Channel("ZDF", delsys="dvb-s2", sid=28006, onid=1, tsid=1079)])
        self.current = channel
        self.capture = None
        self.player = FakePlayer()
        self.vol = Gtk.Adjustment(lower=0, upper=1, value=0.5)
        self.status = []
        self.zapped = []

    def set_status(self, text):
        self.status.append(text)

    def zap(self, c):
        self.zapped.append(c)

    def zap_relative(self, step):
        pass


def _wait(cond, timeout=20):
    ctx = GLib.MainContext.default()
    end = time.time() + timeout
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_red_button_application():
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        win.overlay.connect("get-child-position",
                            lambda o, w, r: hbb.place(o, r) if w is hbb.view else False)
        cap = FakeCapture()
        hbb.channel_changed(ch, cap)
        a = Ait(0x10, 1, transports=[Transport(ait.PROTOCOL_HTTP, 1, [url])])
        a.apps = [AitApp(0x13, 1, ait.AUTOSTART, labels=[1], location="index.html")]
        cap.ait = a
        cap.emit("ait", a)
        assert hbb.life.active and hbb.view is not None
        assert _wait(lambda: (hbb.view.get_title() or "").startswith("ready")), hbb.view.get_title()
        assert hbb.view.get_title() == "ready:DEU"
        assert _wait(lambda: hbb.keyset == 0x11)
        # the app's video window: the right half of the top half
        assert _wait(lambda: hbb.video_rect == (640, 0, 640, 360))
        assert _wait(lambda: win.picture.get_margin_start() > 0)
        w = win.overlay.get_width()
        assert abs(win.picture.get_margin_start() - w / 2) <= 2
        # green is not in the key set: the terminal keeps it
        assert hbb.handle_key("F6") is False
        assert hbb.handle_key("F5") is True
        assert _wait(lambda: (hbb.view.get_title() or "").startswith("key:"))
        assert hbb.view.get_title() == "key:403:2:Das Erste:true:true:%d:2:deu/eng:0" % \
            hbb.view.get_settings().get_user_agent().index("HbbTV/1.7.1")
        # the app picked the English sound of the live picture
        assert _wait(lambda: win.player.selected == [("audio", 1)])
        # EXIT ends the application and gives the whole picture back to the video
        assert hbb.handle_key("Escape") is True
        assert not hbb.life.active and not hbb.view.get_visible()
        assert win.picture.get_margin_start() == 0
        win.window.destroy()
    finally:
        server.shutdown()


def _start(win, hbb, url, location="dashapp"):
    win.overlay.connect("get-child-position",
                        lambda o, w, r: hbb.place(o, r) if w is hbb.view else False)
    cap = FakeCapture()
    hbb.channel_changed(win.current, cap)
    a = Ait(0x10, 1, transports=[Transport(ait.PROTOCOL_HTTP, 1, [url])])
    a.apps = [AitApp(0x13, 1, ait.AUTOSTART, labels=[1], location=location)]
    cap.ait = a
    cap.emit("ait", a)


def test_dash_video_and_av_object():
    """MPEG-DASH through dash.js: an HTML5 video and an A/V control object play,
    the A/V object lists both audio languages and switches to the second, and
    the page has no global dashjs of ours."""
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        _start(win, hbb, f"http://127.0.0.1:{server.server_address[1]}/")
        title = lambda: hbb.view.get_title() or ""  # noqa: E731
        assert _wait(lambda: "dashjs=" in title(), timeout=40), title()
        assert title() == "src=true html5 av-playing audio=deu,eng active=eng dashjs=undefined"
        assert win.player.stops >= 1                  # the broadcast made way for the app's video
        win.window.destroy()
    finally:
        server.shutdown()


def test_dash_ttml_subtitles():
    """EBU-TT-D subtitles of a DASH stream (an A/V object typed video/mp4 with a
    manifest URL): hidden at first, shown once the app selects the subtitle component."""
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        _start(win, hbb, f"http://127.0.0.1:{server.server_address[1]}/", "subapp")
        title = lambda: hbb.view.get_title() or ""  # noqa: E731
        assert _wait(lambda: title().startswith("subs="), timeout=40), title()
        # off until selected (Preferences: subtitles off), then on
        assert title() == "subs=deu before=- shown=Untertitel aus der Mediathek"
        win.window.destroy()
    finally:
        server.shutdown()
