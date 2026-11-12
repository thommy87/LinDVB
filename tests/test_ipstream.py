"""Internet streams through the timeshift buffer (lindvb.ipstream), and
reconnecting them when the connection breaks."""

import http.server
import os
import shutil
import subprocess
import threading
import time

import pytest

from lindvb import channels as channels_mod
from lindvb import ipstream
from lindvb.channels import Channel


# -- what a channel is ---------------------------------------------------------

def test_stream_kinds():
    assert ipstream.stream_kind("http://x/live/u/p/1.ts") == "http"
    assert ipstream.stream_kind("https://x/a/index.m3u8?token=1") == "adaptive"
    assert ipstream.stream_kind("https://x/dash/manifest.mpd") == "adaptive"
    assert ipstream.stream_kind("rtp://87.141.215.251@232.0.20.35:10000") == "multicast"
    assert ipstream.can_buffer("udp://239.0.0.1:1234") and not ipstream.can_buffer("file:///x.ts")
    assert ipstream.multicast_source("rtp://1.2.3.4@232.0.20.35:10000") == \
        ("udp://232.0.20.35:10000", "1.2.3.4", True)
    assert ipstream.multicast_source("udp://239.1.1.1:5000") == ("udp://239.1.1.1:5000", None, False)


def test_looks_like_ts():
    null = b"\x47\x1f\xff\x10" + b"\xff" * 184
    assert ipstream.looks_like_ts(null * 10)
    assert ipstream.looks_like_ts(b"\x00\x01" + null * 10)          # starts mid-packet
    assert not ipstream.looks_like_ts(b"#EXTM3U\n" + b"x" * 2000)
    assert not ipstream.looks_like_ts(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000)


def test_channel_kinds():
    web = Channel(name="Web", delsys="iptv", url="http://x/1.m3u8")
    radio = Channel(name="Radio", delsys="iptv", url="http://x/radio.mp3", radio=True)
    tvh = Channel(name="Tvh", delsys="iptv", url="http://tvh:9981/stream/channel/x",
                  tvh_server="http://tvh:9981")
    dvbi_app = Channel(name="App only", delsys="iptv")
    try:
        channels_mod.IPTV_TIMESHIFT = True
        assert web.buffered and not web.via_url and web.plays_url
        assert not radio.buffered and radio.via_url          # now-playing titles: playbin
        assert not tvh.buffered and not tvh.via_url and not tvh.plays_url
        assert not dvbi_app.buffered and dvbi_app.via_url
        channels_mod.IPTV_TIMESHIFT = False
        assert not web.buffered and web.via_url and web.plays_url
    finally:
        channels_mod.IPTV_TIMESHIFT = True


def test_retry_delays():
    now = [0.0]
    r = ipstream.Retry(delays=(1, 2, 4), give_up=20, stable=30, clock=lambda: now[0])
    assert [r.broken(), r.broken(), r.broken(), r.broken()] == [1, 2, 4, 4]
    now[0] = 21
    assert r.broken() is None                           # broken for too long
    r = ipstream.Retry(delays=(1, 2, 4), give_up=20, stable=30, clock=lambda: now[0])
    assert r.broken() == 1
    r.running()
    now[0] += 5
    assert r.broken() == 2                              # only ran 5 s: next delay
    r.running()
    now[0] += 40
    assert r.broken() == 1                              # ran fine for a while: anew


# -- HTTP with reconnecting ---------------------------------------------------------

NULL = b"\x47\x1f\xff\x10" + b"\xff" * 184


class Flaky(http.server.BaseHTTPRequestHandler):
    """/live.ts sends a few chunks, then drops the connection; /fail 404;
    /page.html is no transport stream."""
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Flaky.requests.append(self.path)
        if self.path == "/fail":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        if self.path == "/page.html":
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>" + b"x" * 4000)
            return
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        chunk = NULL * 100
        if self.path == "/service.ts":            # PAT, PMT and a PCR (tsparse keeps it)
            from test_tvheadend import service_ts
            Flaky.n = getattr(Flaky, "n", 0) + 5
            chunk = None
        try:
            for i in range(5):
                self.wfile.write(chunk or service_ts(20, (Flaky.n + i) * 20))
                self.wfile.flush()
                time.sleep(0.02)
        except OSError:
            pass
        self.close_connection = True


@pytest.fixture
def flaky():
    Flaky.requests = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_reader_reconnects(flaky):
    got, status, errors = [], [], []
    done = threading.Event()

    def on_status(kind, msg):
        status.append(kind)
        if status.count("reconnected") >= 2:
            done.set()
    r = ipstream.StreamReader(flaky + "/live.ts", got.append, errors.append, on_status,
                              retry=ipstream.Retry(delays=(0.1,), give_up=10))
    r.start()
    assert done.wait(10)
    r.close()
    r._thread.join(5)
    assert not errors and Flaky.requests.count("/live.ts") >= 3
    assert status[:2] == ["reconnecting", "reconnected"] and r.reconnects >= 2
    assert sum(map(len, got)) >= 2 * 5 * 100 * 188


def test_reader_gives_up_and_first_failure(flaky):
    errors, failed = [], threading.Event()
    r = ipstream.StreamReader(flaky + "/fail", lambda b: None,
                              lambda m: (errors.append(m), failed.set()))
    r.start()
    assert failed.wait(5) and "404" in errors[0]        # no data ever: at once, no retry
    assert Flaky.requests == ["/fail"]

    errors.clear()
    failed.clear()
    r = ipstream.StreamReader(flaky + "/live.ts", lambda b: None,
                              lambda m: (errors.append(m), failed.set()),
                              retry=ipstream.Retry(delays=(0.05,), give_up=0))
    r.start()
    assert failed.wait(5) and "ended" in errors[0]


def test_reader_not_a_transport_stream(flaky):
    other, errors = threading.Event(), []
    r = ipstream.StreamReader(flaky + "/page.html", lambda b: None, errors.append,
                              on_other=other.set, expect_ts=True)
    r.start()
    assert other.wait(5) and not errors


def test_reconnect_off(flaky):
    errors, failed = [], threading.Event()
    r = ipstream.StreamReader(flaky + "/live.ts", lambda b: None,
                              lambda m: (errors.append(m), failed.set()), reconnect=False)
    r.start()
    assert failed.wait(5) and Flaky.requests.count("/live.ts") == 1


def test_tvheadend_reader_reconnects(flaky):
    from lindvb import tvheadend
    status, done = [], threading.Event()
    r = tvheadend.StreamReader(flaky + "/live.ts", lambda b: None, login=("", ""),
                               on_status=lambda k, m: (status.append(k),
                                                       k == "reconnected" and done.set()),
                               retry=ipstream.Retry(delays=(0.1,), give_up=10))
    r.start()
    assert done.wait(10)
    r.close()
    assert status[0] == "reconnecting"


# -- GStreamer -------------------------------------------------------------------------

def _gst(*elements):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    missing = [e for e in elements if Gst.ElementFactory.find(e) is None]
    if missing:
        pytest.skip("GStreamer elements missing: " + ", ".join(missing))
    return GLib, Gst


def _make_hls(folder, name, width, height, seconds=4):
    """A small HLS variant (H.264 + AAC in TS segments of about 1 s)."""
    d = folder / name
    d.mkdir()
    gst_launch = shutil.which("gst-launch-1.0")
    if gst_launch is None:
        pytest.skip("gst-launch-1.0 missing")
    frames = seconds * 25
    subprocess.run(
        [gst_launch, "-q", "videotestsrc", f"num-buffers={frames}", "pattern=ball", "!",
         f"video/x-raw,width={width},height={height},framerate=25/1,format=I420", "!",
         "x264enc", "key-int-max=25", "speed-preset=ultrafast", "!", "h264parse", "!",
         "queue", "!", "mux.", "audiotestsrc", f"num-buffers={seconds * 44}", "!",
         "avenc_aac", "!", "aacparse", "!", "queue", "!", "mux.", "mpegtsmux", "name=mux",
         "!", "hlssink", f"location={d}/seg%05d.ts", f"playlist-location={d}/index.m3u8",
         "target-duration=1", "max-files=0", "playlist-length=0"],
        check=True, timeout=60, capture_output=True)
    return d


class Files(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _decode(path):
    """(picture sizes in order, error) of a transport stream file."""
    GLib, Gst = _gst("decodebin")
    pl = Gst.parse_launch(f'filesrc location="{path}" ! decodebin name=d '
                          'identity name=v ! fakesink sync=false')
    sizes, res = [], []

    def added(_d, pad):
        caps = pad.query_caps(None)
        if caps.get_structure(0).get_name().startswith("video"):
            pad.link(pl.get_by_name("v").get_static_pad("sink"))
        else:
            f = Gst.ElementFactory.make("fakesink")
            f.set_property("sync", False)
            pl.add(f)
            f.sync_state_with_parent()
            pad.link(f.get_static_pad("sink"))
    pl.get_by_name("d").connect("pad-added", added)

    def on_caps(_pad, info):
        ev = info.get_event()
        if ev is not None and ev.type == Gst.EventType.CAPS:
            s = ev.parse_caps().get_structure(0)
            size = (s.get_value("width"), s.get_value("height"))
            if not sizes or sizes[-1] != size:
                sizes.append(size)
        return Gst.PadProbeReturn.OK
    pl.get_by_name("v").get_static_pad("sink").add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM, on_caps)
    loop = GLib.MainLoop()

    def msg(_bus, m):
        if m.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            res.append(m.parse_error()[0].message if m.type == Gst.MessageType.ERROR else "")
            loop.quit()
    bus = pl.get_bus()
    bus.add_signal_watch()
    bus.connect("message", msg)
    GLib.timeout_add_seconds(30, lambda: (res.append("timeout"), loop.quit()))
    pl.set_state(Gst.State.PLAYING)
    loop.run()
    pl.set_state(Gst.State.NULL)
    return sizes, res[0] if res else ""


@pytest.fixture
def hls(tmp_path):
    _gst("videotestsrc", "x264enc", "avenc_aac", "mpegtsmux", "hlssink", "urisourcebin",
         "parsebin", "appsink", "h264parse")
    _make_hls(tmp_path, "low", 320, 180)
    _make_hls(tmp_path, "high", 640, 360)
    (tmp_path / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x180\nlow/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=640x360\nhigh/index.m3u8\n")
    old = os.getcwd()
    os.chdir(tmp_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Files)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    finally:
        srv.shutdown()
        os.chdir(old)


def _read_all(reader_cls, url, seconds=15, **kw):
    GLib, _Gst = _gst()
    got, errors, status = [], [], []
    r = reader_cls(url, got.append, errors.append, lambda k, m: status.append(k), **kw)
    r.start()
    loop = GLib.MainLoop()
    t0 = time.monotonic()

    def tick():
        # a VOD playlist ends: the reader reconnects (status) once it is through
        if status or errors or time.monotonic() - t0 > seconds:
            loop.quit()
            return False
        return True
    GLib.timeout_add(100, tick)
    loop.run()
    r.close()
    return r, b"".join(got), errors


@pytest.mark.parametrize("quality,size", [("best", (640, 360))])
def test_hls_into_transport_stream(hls, tmp_path, quality, size):
    base, folder = hls
    r, data, errors = _read_all(ipstream.GstReader, base + "/master.m3u8", quality=quality,
                                retry=ipstream.Retry(delays=(5,), give_up=30))
    assert not errors and ipstream.looks_like_ts(data) and r.passthrough is False
    out = tmp_path / "out.ts"
    out.write_bytes(data)
    sizes, err = _decode(out)
    assert sizes and sizes[-1] == size and not err


def test_quality_change_inside_the_buffer(hls, tmp_path):
    """Segments of two variants one after the other (the demuxer switched):
    the packed transport stream plays through with both picture sizes."""
    base, folder = hls
    mixed = folder / "mixed"
    mixed.mkdir()
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    segs = sorted(p.name for p in (folder / "low").glob("seg*.ts"))
    for i, name in enumerate(segs):
        variant = "high" if i < len(segs) // 2 else "low"
        lines += ["#EXT-X-DISCONTINUITY"] if i == len(segs) // 2 else []
        lines += ["#EXTINF:1.0,", f"../{variant}/{name}"]
    (mixed / "index.m3u8").write_text("\n".join(lines + ["#EXT-X-ENDLIST", ""]))
    r, data, errors = _read_all(ipstream.GstReader, base + "/mixed/index.m3u8",
                                retry=ipstream.Retry(delays=(5,), give_up=30))
    assert not errors and data
    out = tmp_path / "out.ts"
    out.write_bytes(data)
    sizes, err = _decode(out)
    assert sizes == [(640, 360), (320, 180)] and not err


def test_source_switches_to_gstreamer(hls):
    """An HTTP address that is no transport stream (here a playlist without
    .m3u8 in its name) goes to GStreamer."""
    base, folder = hls
    shutil.copy(folder / "high" / "index.m3u8", folder / "high" / "playlist")
    GLib, _Gst = _gst()
    got, errors = [], []
    src = ipstream.StreamSource(base + "/high/playlist", got.append, errors.append)
    src.start()
    loop = GLib.MainLoop()
    GLib.timeout_add(100, lambda: (loop.quit(), False)[1] if sum(map(len, got)) > 20_000
                     else True)
    GLib.timeout_add_seconds(15, lambda: (loop.quit(), False)[1])
    loop.run()
    src.close()
    assert isinstance(src.reader, ipstream.GstReader) and sum(map(len, got)) > 20_000


def test_capture_of_an_internet_channel(flaky, tmp_path):
    GLib, Gst = _gst("appsrc", "tsparse", "multifilesink")
    from lindvb.config import DEFAULTS
    from lindvb.tuner import TunerManager
    settings = dict(DEFAULTS, timeshift_dir=str(tmp_path), timeshift_max_mb=64)
    tm = TunerManager(settings, probe=lambda a, f: [])
    ch = Channel(name="Web TV", delsys="iptv", url=flaky + "/service.ts")
    cap = tm.acquire(ch, "live")
    assert cap.adapter == ipstream.ADAPTER_BASE and "127.0.0.1" in cap.source_label
    assert tm.acquire(ch, "rec") is cap
    kinds = []
    cap.connect("connection", lambda _c, k, _m: kinds.append(k))
    loop = GLib.MainLoop()
    GLib.timeout_add(100, lambda: (loop.quit(), False)[1]
                     if "reconnected" in kinds and cap.store.bounds()[1] > 300_000 else True)
    GLib.timeout_add_seconds(15, lambda: (loop.quit(), False)[1])
    loop.run()
    assert cap.store.bounds()[1] > 300_000 and cap.received > 300_000     # in the buffer
    assert kinds[:2] == ["reconnecting", "reconnected"]
    tm.release(cap, "live")
    tm.release(cap, "rec")
    assert not tm.captures and cap._net.closed


# -- playing directly: the player opens a broken stream again --------------------------

def test_player_reconnects():
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    try:
        gi.require_version("Gtk", "4.0")
    except ValueError:
        pytest.skip("GTK 4 missing")
    from gi.repository import Gst, Gtk
    Gst.init(None)
    if not Gtk.init_check():
        pytest.skip("no display")
    from lindvb.config import DEFAULTS
    from lindvb.player import Player
    p = Player(Gtk.Picture(), dict(DEFAULTS, deinterlace=False, video_decoder="software"))
    seen = []
    p.connect("reconnecting", lambda _p, s, r: seen.append((s, r)))
    p.uri, p._retry, p._played = "http://example.invalid/live.ts", ipstream.Retry(), True
    assert p._try_reconnect("gone") and seen == [(1, "gone")]
    assert p._try_reconnect("gone again") and len(seen) == 1     # the same break
    p._cancel_reconnect()
    p.uri = "file:///tmp/film.ts"
    assert not p._try_reconnect("end")
    p.uri, p.settings["stream_reconnect"] = "http://example.invalid/live.ts", False
    assert not p._try_reconnect("gone")
    p.stop()
