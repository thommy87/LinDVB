"""HbbTV end to end in WebKitGTK: an application served as
application/vnd.hbbtv.xhtml+xml starts from the AIT, sets its key set, binds
the broadcast video into a window of its own and gets the red key.

Needs a display (run under xvfb-run) and WebKitGTK 6; skipped otherwise.
"""

import http.server
import os
import socket
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
from lindvb import hbbtv as _hb  # noqa: E402

if not _hb.sandbox_ok():
    pytest.skip("WebKit's sandbox cannot create user namespaces here", allow_module_level=True)
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

PC_APP = b"""<html><head><title>loading</title>
<script type="text/javascript">
var log = [];
function L(x) { log.push(x); document.title = log.join(' '); }
window.onload = function () {
  var pc = document.getElementById('pc');
  L('threshold=' + pc.parentalRatingSchemes.getParentalRatingScheme('dvb-si').threshold.name);
  var av = document.getElementById('av');
  av.onParentalRatingChange = function (cid, ratings, drm, blocked) {
    L('rated=' + (ratings.length ? ratings[0].name : '-') + ':' + blocked);
    if (blocked) {
      L('wrong=' + pc.verifyParentalControlPIN('0000'));
      L('right=' + pc.verifyParentalControlPIN('1234'));
    }
  };
  av.onPlayStateChange = function () {
    if (av.playState === 1 && log.indexOf('playing') < 0) {
      L('playing');
    }
  };
  av.data = 'dash/manifest-rated.mpd';
  av.play(1);
};
</script></head><body>
<object id="pc" type="application/oipfParentalControlManager"></object>
<object id="av" type="application/dash+xml" style="position:absolute;left:0;top:0;width:320px;height:180px"></object>
</body></html>
"""

DRM_APP = b"""<html><head><title>loading</title>
<script type="text/javascript">
var log = [];
function L(x) { log.push(x); document.title = log.join(' '); }
window.onload = function () {
  if (location.search.indexOf('nodrmagent') < 0) {
    var drm = oipfObjectFactory.createDrmAgentObject();
    L('status=' + drm.getDRMSystemStatus('urn:dvb:casystemid:19188'));
    drm.onDRMMessageResult = function (id, msg, code) { L('result=' + id + ':' + code); };
    drm.sendDRMMessage('m1', 'application/vnd.marlin.drm.actiontoken+xml', '<x/>', 'urn:dvb:casystemid:19188');
  }
  var av = document.getElementById('av');
  av.data = 'dash/manifest-drm.mpd';
  av.play(1);
};
</script></head><body>
<object id="av" type="application/dash+xml" style="position:absolute;left:0;top:0;width:320px;height:180px"></object>
</body></html>
"""

TT_LAUNCHER = b"""<html><head><title>launcher</title>
<script type="text/javascript">
window.onload = function () {
  var app = document.getElementById('am').getOwnerApplication(document);
  setTimeout(function () {
    // the "Teletext" link of a launcher bar: the AIT's teletext app by its ids
    var t = app.createApplication('dvb://current.ait/13.2?page=100', false);
    app.destroyApplication();
    document.title = 'launched:' + (t !== null);
  }, 200);
};
</script></head><body>
<object id="am" type="application/oipfApplicationManager"></object>
</body></html>
"""

TT_PAGE = b"""<html><head><title>loading</title>
<script type="text/javascript">window.onload = function () { document.title = 'teletext' + location.search; };</script>
</head><body></body></html>
"""

SYNC_APP = b"""<html><head><title>loading</title>
<script type="text/javascript">
window.onload = function () {
  var log = [];
  // created and used in one go, before any MutationObserver could run
  var vb = document.createElement('object');
  vb.type = 'video/broadcast';
  document.body.appendChild(vb);
  vb.bindToCurrentChannel();
  log.push('created=' + vb.playState);
  var box = document.getElementById('box');
  box.innerHTML = '<object id="vb2" type="video/broadcast"></object>';
  var vb2 = document.getElementById('vb2');
  vb2.bindToCurrentChannel();
  log.push('markup=' + vb2.playState + ':' + (vb2.currentChannel ? vb2.currentChannel.name : '-'));
  var av = document.createElement('object');
  av.setAttribute('type', 'video/mp4');
  av.setAttribute('data', 'dash/manifest.mpd');
  log.push('av=' + av.data.indexOf('manifest.mpd') + ':' + av.playState);
  var v = document.createElement('video');
  log.push('dash=' + (v.canPlayType('application/dash+xml') !== ''));
  log.push('hls=' + (v.canPlayType('application/vnd.apple.mpegurl') !== ''));
  document.title = log.join(' ');
};
</script></head><body><div id="box"></div></body></html>
"""

CORS_APP = b"""<html><head><title>loading</title>
<script type="text/javascript">
window.onload = function () {
  // the manifest's CDN sends no CORS headers: a TV's own player does not need them
  var av = document.getElementById('av');
  av.data = 'http://localhost:' + location.port + '/dash/manifest.mpd';
  av.onPlayStateChange = function () {
    if (av.playState === 1) { document.title = 'av-playing'; }
    if (av.playState === 6) { document.title = 'av-error'; }
  };
  av.play(1);
};
</script></head><body>
<object id="av" type="application/dash+xml" style="position:absolute;left:0;top:0;width:320px;height:180px"></object>
</body></html>
"""

XML_AIT = b"""<?xml version="1.0" encoding="UTF-8"?>
<mhp:ServiceDiscovery xmlns:mhp="urn:dvb:mhp:2009" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <mhp:ApplicationDiscovery DomainName="example.org"><mhp:ApplicationList><mhp:Application>
  <mhp:applicationIdentifier><mhp:orgId>19</mhp:orgId><mhp:appId>1</mhp:appId></mhp:applicationIdentifier>
  <mhp:applicationDescriptor>
   <mhp:type><mhp:OtherApp>application/vnd.hbbtv.xhtml+xml</mhp:OtherApp></mhp:type>
   <mhp:controlCode>AUTOSTART</mhp:controlCode>
  </mhp:applicationDescriptor>
  <mhp:applicationTransport xsi:type="mhp:HTTPTransportType"><mhp:URLBase>BASE</mhp:URLBase></mhp:applicationTransport>
  <mhp:applicationLocation>index.html</mhp:applicationLocation>
 </mhp:Application></mhp:ApplicationList></mhp:ApplicationDiscovery>
</mhp:ServiceDiscovery>"""

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
        elif path == "/ait.xml":
            base = f"http://127.0.0.1:{self.server.server_address[1]}/"
            body = XML_AIT.replace(b"BASE", base.encode())
            ctype = "application/vnd.dvb.ait+xml"
        else:
            body = DASH_APP if path.startswith("/dashapp") else \
                SUB_APP if path.startswith("/subapp") else \
                PC_APP if path.startswith("/pcapp") else \
                DRM_APP if path.startswith("/drmapp") else \
                TT_LAUNCHER if path.startswith("/ttlauncher") else \
                TT_PAGE if path.startswith("/ttapp") else \
                SYNC_APP if path.startswith("/syncapp") else \
                CORS_APP if path.startswith("/corsapp") else APP
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


@pytest.fixture(autouse=True)
def _no_zap_delay(monkeypatch):
    """Applications start as soon as the AIT is there (no wait for zapping)."""
    import lindvb.ui.hbbtv as ui
    monkeypatch.setattr(ui, "AUTOSTART_DELAY", 0)


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


CAROUSEL_APP = b"""<html><head><title>loading</title>
<script type="text/javascript">
var got = {};
function show() {
  document.title = 'xhr=' + got.xhr + ' ste=' + got.ste + ' xml=' + got.xml + ' origin=' + location.protocol;
}
window.onload = function () {
  var vb = document.getElementById('vb');
  vb.bindToCurrentChannel();
  var x = new XMLHttpRequest();
  x.open('GET', 'data/text.txt');
  x.onload = function () { got.xhr = x.responseText; show(); };
  x.send();
  vb.addStreamEventListener('events/quiz', 'question', function (e) {
    got.ste = e.status + ':' + e.text + ':' + e.data; show();
  });
  vb.addStreamEventListener('events.xml', 'question', function (e) {
    got.xml = e.status + ':' + e.text; show();
  });
  vb.addStreamEventListener('events/quiz', 'nosuchevent', function (e) {
    got.missing = e.status;
  });
};
</script></head><body>
<object id="vb" type="video/broadcast" style="position:absolute;left:0;top:0;width:1280px;height:720px"></object>
</body></html>
"""

EVENTS_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<dsmcc xmlns="urn:dvb:mis:dsmcc:2009">
  <dsmcc_object component_tag="13">
    <stream_event stream_event_id="1" stream_event_name="question"/>
  </dsmcc_object>
</dsmcc>
"""


def _test_ts_tool():
    from importlib import util
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "hbbtv-test-ts.py")
    spec = util.spec_from_file_location("hbbtv_test_ts", path)
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CarouselCapture(GObject.Object):
    """A capture that replays a transport stream into a real AitMonitor."""
    __gsignals__ = {"ait": (GObject.SignalFlags.RUN_LAST, None, (object,))}

    def __init__(self, ts):
        super().__init__()
        self.ait = None
        self.pids = []
        self.ts, self.pos = ts, 0
        self.ait_monitor = ait.AitMonitor(28106, on_ait=self._on_ait)
        GLib.timeout_add(20, self._feed)

    def _on_ait(self, a):
        def deliver():
            self.ait = a
            self.emit("ait", a)
        GLib.idle_add(lambda: (deliver(), False)[1])

    def add_pid(self, pid):
        self.pids.append(pid)

    def _feed(self):
        chunk = 188 * 200
        if self.pos >= len(self.ts):
            self.pos = 0
        self.ait_monitor.feed_buffer(self.ts[self.pos:self.pos + chunk])
        self.pos += chunk
        return True


def test_object_carousel_application_and_stream_events():
    """An application from the DSM-CC object carousel: its page, an XHR for a
    file next to it, and stream events through a stream event object and
    through an XML event description."""
    from lindvb.ui.hbbtv import HbbTV

    tool = _test_ts_tool()
    files = {"index.html": CAROUSEL_APP, "data/text.txt": b"aus dem Karussell",
             "events.xml": EVENTS_XML}
    ts = tool.build("index.html", 28106, files=files,
                    events={"events/quiz": ({"question": 1}, 0x0D)},
                    stream_events=[(1, b"Frage 1", 1)], repeat=20)
    ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
    win = FakeWindow(ch)
    win.window.present()
    hbb = HbbTV(win)
    win.overlay.connect("get-child-position",
                        lambda o, w, r: hbb.place(o, r) if w is hbb.view else False)
    cap = CarouselCapture(ts)
    hbb.channel_changed(ch, cap)
    title = lambda: (hbb.view.get_title() if hbb.view else "") or ""  # noqa: E731
    assert _wait(lambda: "xml=trigger" in title() and "ste=trigger" in title(), timeout=40), title()
    assert title() == ("xhr=aus dem Karussell ste=trigger:Frage 1:%s xml=trigger:Frage 1 "
                       "origin=dvb:" % b"Frage 1".hex())
    assert hbb.view.get_uri() == "dvb://current.c/index.html"
    assert set(cap.pids) == {0x102, 0x103}
    # a new channel: the listeners hear of it
    hbb.channel_changed(Channel("ZDF", delsys="dvb-s2", sid=28006, onid=1, tsid=1079), None)
    win.window.destroy()


def test_parental_control_holds_a_rated_video():
    """A DASH video rated 16+ with the age limit at 12: held back until the
    app checks the PIN with verifyParentalControlPIN()."""
    from lindvb import hbbtv as hbc
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
        win = FakeWindow(ch)
        salt = hbc.new_salt()
        win.settings.update({"hbbtv_age_limit": 12, "hbbtv_pin_salt": salt,
                             "hbbtv_pin": hbc.hash_pin("1234", salt)})
        win.window.present()
        hbb = HbbTV(win)
        _start(win, hbb, f"http://127.0.0.1:{server.server_address[1]}/", "pcapp")
        title = lambda: hbb.view.get_title() or ""  # noqa: E731
        assert _wait(lambda: "playing" in title(), timeout=40), title()
        assert title() == "threshold=13 rated=16:true wrong=1 rated=-:false right=0 playing"
        assert hbb._pin_dialog is None and not hbb._media_blocked
        win.window.destroy()
    finally:
        server.shutdown()


def test_dvbi_internet_channel_application():
    """A DVB-I internet channel: the service list links an XML AIT, the app
    starts without any broadcast signalling."""
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        ch = Channel("Das Erste", delsys="iptv", url="https://example.org/ard.mpd",
                     dvbi_id="tag:ard",
                     dvbi_apps=[{"url": url + "ait.xml", "type": "application/vnd.dvb.ait+xml",
                                 "role": "parallel", "level": "service"}])
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        win.overlay.connect("get-child-position",
                            lambda o, w, r: hbb.place(o, r) if w is hbb.view else False)
        hbb.channel_changed(ch, None)
        assert _wait(lambda: hbb.life.active, timeout=20)
        assert hbb.life.running.key == (19, 1)
        assert _wait(lambda: (hbb.view.get_title() or "").startswith("ready")), hbb.view.get_title()
        # an internet channel without applications ends it
        hbb.channel_changed(Channel("Web", delsys="iptv", url="https://example.org/x.m3u8"), None)
        assert not hbb.life.active
        win.window.destroy()
    finally:
        server.shutdown()


def test_protected_video_tells_the_user():
    """A DRM agent request and a Widevine DASH manifest: the app hears that
    the DRM system is unknown and the user sees why the video stays dark."""
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ch = Channel("RTL", delsys="dvb-s2", sid=12003, onid=1, tsid=1089)
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        _start(win, hbb, f"http://127.0.0.1:{server.server_address[1]}/", "drmapp")
        title = lambda: hbb.view.get_title() or ""  # noqa: E731
        drm = lambda: [s for s in win.status if "DRM" in s]  # noqa: E731
        assert _wait(lambda: "result=" in title() and drm(), timeout=40), (title(), win.status)
        assert title() == "status=1 result=m1:5"
        # the DRM agent came first and named Marlin: the notice is shown once
        _wait(lambda: False, timeout=3)               # the manifest loads meanwhile
        assert len(drm()) == 1 and "Marlin" in drm()[0], win.status
        assert hbb._drm_label.get_visible()
        hbb.close()
        assert not hbb._drm_label.get_visible()
        win.window.destroy()
    finally:
        server.shutdown()


def test_manifest_drm_is_detected():
    """Without a DRM agent call the Widevine manifest itself triggers the notice."""
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ch = Channel("RTL", delsys="dvb-s2", sid=12003, onid=1, tsid=1089)
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        _start(win, hbb, f"http://127.0.0.1:{server.server_address[1]}/", "drmapp?nodrmagent")
        drm = lambda: [s for s in win.status if "DRM" in s]  # noqa: E731
        assert _wait(drm, timeout=40), (hbb.view.get_title(), win.status)
        assert "Widevine" in drm()[0], win.status
        win.window.destroy()
    finally:
        server.shutdown()


CS_APP = b"""<html><head><title>loading</title>
<script type="text/javascript">
var log = [];
function L(x) { log.push(x); document.title = log.join(' '); }
window.onload = function () {
  var csm = document.getElementById('cs');
  var local = csm.getApp2AppLocalBaseURL();
  L('local=' + (local.indexOf('ws://127.0.0.1:') === 0) + ' remote=' + (csm.getApp2AppRemoteBaseURL() !== ''));
  var vb = document.getElementById('vb');
  vb.bindToCurrentChannel();
  var ms = oipfObjectFactory.createMediaSynchroniser();
  ms.initMediaSynchroniser(vb, 'urn:dvb:css:timeline:pts');
  var waited = 0;
  var t = setInterval(function () {
    if (!isNaN(ms.currentTime) || ++waited > 100) {
      clearInterval(t);
      L('timeline=' + (!isNaN(ms.currentTime) && ms.currentTime > 9 && ms.currentTime < 12));
      var ws = new WebSocket(local + 'org.example.quiz');
      ws.onmessage = function (e) {
        if (e.data === 'pairingcompleted') { L('paired'); ws.send('hallo handy'); }
        else { L('got=' + e.data); }
      };
    }
  }, 100);
};
</script></head><body>
<object id="cs" type="application/hbbtvCSManager"></object>
<object id="vb" type="video/broadcast" style="position:absolute;left:0;top:0;width:1280px;height:720px"></object>
</body></html>
"""


def test_companion_app_to_app_and_broadcast_timeline():
    """hbbtvCSManager's app-to-app URL pairs the app with a 'phone' (a test
    client on the remote side); a MediaSynchroniser on video/broadcast sees
    the PTS of the live video."""
    import asyncio
    from lindvb.ui import hbbtv as ui

    tool = _test_ts_tool()
    ts = tool.build("index.html", 28106, files={"index.html": CS_APP}, repeat=20)
    ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
    win = FakeWindow(ch)
    win.window.present()
    hbb = ui.HbbTV(win)
    win.overlay.connect("get-child-position",
                        lambda o, w, r: hbb.place(o, r) if w is hbb.view else False)
    cap = CarouselCapture(ts)
    hbb.channel_changed(ch, cap)
    title = lambda: (hbb.view.get_title() if hbb.view else "") or ""  # noqa: E731
    # the broadcast timeline: feed PTS = 10 s as the demux would
    assert _wait(lambda: hbb._sync_broadcast, timeout=40), title()
    cap.ait_monitor.on_pts(900000, time.monotonic_ns())
    assert _wait(lambda: "timeline=" in title(), timeout=20), title()
    assert "timeline=true" in title() and "local=true" in title()
    svc = hbb.cs

    # bring a remote connection into the service's loop
    fut = asyncio.run_coroutine_threadsafe(_remote_client(svc, "org.example.quiz"), svc.loop)
    assert _wait(lambda: "paired" in title(), timeout=20), title()
    assert _wait(lambda: fut.done(), timeout=20)
    assert fut.result() == "hallo handy"
    assert _wait(lambda: "got=hallo tv" in title(), timeout=20), title()
    win.window.destroy()


async def _remote_client(svc, endpoint):
    """A companion's connection to the remote base, made inside the service
    (the network listener is off in this test)."""
    import asyncio
    from lindvb import hbbtvcs
    a, b = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=a)
    creader, cwriter = await asyncio.open_connection(sock=b)
    ws_server = hbbtvcs.WebSocket(reader, writer, "/hbbtv/" + endpoint)
    client = hbbtvcs.ClientWebSocket(creader, cwriter, "/hbbtv/" + endpoint)
    asyncio.ensure_future(svc.app2app.serve(ws_server, "remote", endpoint))
    assert await client.recv() == "pairingcompleted"
    msg = await client.recv()
    await client.send("hallo tv")
    return msg


def test_teletext_link_starts_the_ait_application():
    """A launcher's teletext link, createApplication("dvb://current.ait/13.2"),
    starts that application of the AIT (with the query) and the launcher's
    destroyApplication() right after does not end it."""
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        cap = FakeCapture()
        hbb.channel_changed(ch, cap)
        a = Ait(0x10, 1, transports=[Transport(ait.PROTOCOL_HTTP, 1, [url])])
        a.apps = [AitApp(0x13, 1, ait.AUTOSTART, labels=[1], location="ttlauncher"),
                  AitApp(0x13, 2, ait.PRESENT, labels=[1], location="ttapp", usage=ait.USAGE_TELETEXT)]
        cap.ait = a
        cap.emit("ait", a)
        title = lambda: hbb.view.get_title() or ""  # noqa: E731
        assert _wait(lambda: title().startswith("teletext")), (title(), win.status)
        assert title() == "teletext?page=100"
        assert hbb.life.active and hbb.life.running.app_id == 2
        win.window.destroy()
    finally:
        server.shutdown()


def test_objects_work_before_the_observer_ran():
    """Objects made and used in one script (createElement + type, innerHTML)
    work at once; DASH and HLS are playable types."""
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        _start(win, hbb, f"http://127.0.0.1:{server.server_address[1]}/", "syncapp")
        title = lambda: hbb.view.get_title() or ""  # noqa: E731
        assert _wait(lambda: title().startswith("created"), timeout=30), title()
        parts = title().replace("Das Erste", "DasErste").split()
        assert parts[:2] == ["created=1", "markup=1:DasErste"], title()
        assert parts[2].startswith("av=") and not parts[2].startswith("av=-1"), title()
        assert parts[3:] == ["dash=true", "hls=true"], title()
        win.window.destroy()
    finally:
        server.shutdown()


def test_dash_without_cors_plays_natively():
    """dash.js cannot read a manifest without CORS headers: GStreamer plays it."""
    from lindvb.ui.hbbtv import HbbTV

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
        win = FakeWindow(ch)
        win.window.present()
        hbb = HbbTV(win)
        _start(win, hbb, f"http://127.0.0.1:{server.server_address[1]}/", "corsapp")
        title = lambda: hbb.view.get_title() or ""  # noqa: E731
        assert _wait(lambda: title().startswith("av-"), timeout=40), (title(), win.status)
        assert title() == "av-playing", win.status
        win.window.destroy()
    finally:
        server.shutdown()


def test_autostart_waits_while_zapping(monkeypatch):
    """The AUTOSTART application loads only once the viewer stays on the channel."""
    import lindvb.ui.hbbtv as ui
    monkeypatch.setattr(ui, "AUTOSTART_DELAY", 0.6)
    ch = Channel("Das Erste", delsys="dvb-s2", sid=28106, onid=1, tsid=1019)
    other = Channel("ZDF", delsys="dvb-s2", sid=28006, onid=1, tsid=1079)
    win = FakeWindow(ch)
    hbb = ui.HbbTV(win)
    launched = []
    hbb._launch = lambda url, app: launched.append(url)
    hbb.life.launch_cb = hbb._launch
    a = Ait(0x10, 1, transports=[Transport(ait.PROTOCOL_HTTP, 1, ["http://tv.example/"])])
    a.apps = [AitApp(0x13, 1, ait.AUTOSTART, labels=[1], location="index.html")]
    cap = FakeCapture()
    cap.ait = a
    hbb.channel_changed(ch, cap)
    assert launched == [] and hbb.has_app          # the red button is offered at once
    hbb.channel_changed(other, FakeCapture())      # zapped on before the delay ran out
    assert not _wait(lambda: launched, timeout=1.0)
    cap2 = FakeCapture()
    cap2.ait = a
    hbb.channel_changed(ch, cap2)
    assert _wait(lambda: launched, timeout=3)
    assert launched == ["http://tv.example/index.html"]
