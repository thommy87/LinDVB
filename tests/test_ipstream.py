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
        assert radio.buffered and not radio.via_url          # titles go into the buffer
        assert not tvh.buffered and not tvh.via_url and not tvh.plays_url
        assert not dvbi_app.buffered and dvbi_app.via_url
        channels_mod.IPTV_TIMESHIFT = False
        assert not web.buffered and web.via_url and web.plays_url
        assert not radio.buffered and radio.via_url
    finally:
        channels_mod.IPTV_TIMESHIFT = True


# -- song titles in the buffer -----------------------------------------------------

def test_meta_packet_round_trip():
    pkt = ipstream.meta_packet({"title": "Künstler - Lied", "organization": "Test FM"}, cc=17)
    assert len(pkt) == 188 and pkt[0] == 0x47 and pkt[3] == 0x11          # cc wraps
    pid = (pkt[1] & 0x1F) << 8 | pkt[2]
    assert pid == ipstream.META_PID and pkt[1] & 0x40                     # unit start
    assert ipstream.parse_meta_packet(pkt) == {"title": "Künstler - Lied",
                                              "organization": "Test FM"}
    # a title too long for one packet is cut short on a character boundary
    long = ipstream.meta_packet({"title": "ä" * 200})
    title = ipstream.parse_meta_packet(long)["title"]
    assert len(long) == 188 and 0 < len(title) < 200 and set(title) == {"ä"}
    # line breaks in a title cannot split it into fields
    assert ipstream.parse_meta_packet(ipstream.meta_packet({"title": "a\norganization=x"})) == \
        {"title": "a organization=x"}
    assert ipstream.parse_meta_packet(b"\x47\x1f\xf0\x10" + b"\xff" * 184) is None


def test_find_meta():
    null = b"\x47\x1f\xff\x10" + b"\xff" * 184
    one = ipstream.meta_packet({"title": "One"})
    two = ipstream.meta_packet({"title": "Two"}, cc=1)
    data = b"\x00" * 5 + null + one + null * 3 + two + null[:40]
    assert ipstream.find_meta(data) == [(5 + 188, {"title": "One"}),
                                        (5 + 188 * 5, {"title": "Two"})]
    assert ipstream.find_meta(one[:100]) == [] and ipstream.find_meta(null * 4) == []


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


# -- DVB-I gateways: user agent and redirects --------------------------------------


class Gateway(http.server.BaseHTTPRequestHandler):
    """A DVB-I gateway like ARD's: /dvbi/dash/36/dash.mpd redirects to the
    broadcaster's CDN, which refuses the first request (403) and serves the
    stream on the next one. /strict.ts answers 403 to a client that does not
    identify itself as a TV terminal."""
    requests = []
    agents = []
    refused = set()

    def log_message(self, *a):
        pass

    def do_GET(self):
        Gateway.requests.append(self.path)
        Gateway.agents.append(self.headers.get("User-Agent", ""))
        if self.path.startswith("/dvbi/"):
            self.send_response(302)
            self.send_header("Location", "/cdn/manifest.ts")
            self.end_headers()
            return
        if self.path == "/strict.ts" and "HbbTV/" not in self.headers.get("User-Agent", ""):
            self.send_response(403)
            self.end_headers()
            return
        if self.path == "/cdn/manifest.ts" and self.path not in Gateway.refused:
            Gateway.refused.add(self.path)          # the first request is refused
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        try:
            for _i in range(3):
                self.wfile.write(NULL * 100)
                self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True


@pytest.fixture
def gateway():
    Gateway.requests, Gateway.agents, Gateway.refused = [], [], set()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_user_agent_is_a_tv_terminal():
    ua = ipstream.user_agent()
    assert "HbbTV/" in ua and ua.startswith("Mozilla/5.0")
    assert ipstream.http_headers()["User-Agent"] == ua
    assert ipstream.http_headers({"Range": "bytes=0-"})["Range"] == "bytes=0-"


def test_reader_sends_the_user_agent(gateway):
    """A CDN that refuses an unknown client (ARD: 403) is served our agent."""
    got, errors = [], []
    done = threading.Event()
    r = ipstream.StreamReader(gateway + "/strict.ts", lambda b: (got.append(b), done.set()),
                              errors.append, reconnect=False)
    r.start()
    assert done.wait(5)                     # no 403: the server knows the client
    r.close()
    assert sum(map(len, got)) > 0 and not [e for e in errors if "403" in e]
    assert all("HbbTV/" in a for a in Gateway.agents)


def test_resolve_redirects_follows_a_gateway(gateway):
    trace = []
    target = ipstream.resolve_redirects(gateway + "/dvbi/dash/36/dash.mpd", trace=trace)
    assert target == gateway + "/cdn/manifest.ts"
    # the trace tells a debug log which hop refused the stream
    assert trace[0].startswith("302 -> /cdn/manifest.ts") or "302" in trace[0]
    assert trace[-1].startswith("403")
    assert ipstream.resolve_redirects(gateway + "/cdn/manifest.ts") == \
        gateway + "/cdn/manifest.ts"                       # no redirect: unchanged


def test_host_addresses(gateway):
    text = ipstream.host_addresses(gateway + "/cdn/manifest.ts")
    assert text.startswith("127.0.0.1: ") and "127.0.0.1" in text.split(": ", 1)[1]
    assert ipstream.host_addresses("") == ""
    assert "nowhere.invalid" in ipstream.host_addresses("https://nowhere.invalid/a.mpd")


def test_resolve_redirects_reports_an_unreachable_address():
    with pytest.raises(OSError):
        ipstream.resolve_redirects("http://127.0.0.1:1/dvbi/dash/1/dash.mpd", timeout=1)


def test_reader_retries_once_at_the_resolved_address(gateway):
    """403 from the address behind a gateway: resolve the redirect and ask
    the CDN again, instead of failing the channel."""
    got, errors = [], []
    done = threading.Event()
    r = ipstream.StreamReader(gateway + "/dvbi/dash/36/dash.mpd",
                              lambda b: (got.append(b), done.set()),
                              errors.append, reconnect=False)
    r.start()
    assert done.wait(5)                     # the CDN served the second request
    r.close()
    # the test server ends its stream after a few packets, so with reconnect=False
    # an "ended the stream" report may follow the data: only a refusal counts
    assert not [e for e in errors if "403" in e]
    assert r.url == gateway + "/cdn/manifest.ts"
    assert Gateway.requests.count("/cdn/manifest.ts") >= 2


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


def _make_hls_audio(folder, name, seconds=4, wave="sine"):
    """An audio-only HLS rendition (AAC in TS segments), as Deluxe Music has one."""
    d = folder / name
    d.mkdir()
    gst_launch = shutil.which("gst-launch-1.0")
    if gst_launch is None:
        pytest.skip("gst-launch-1.0 missing")
    subprocess.run(
        [gst_launch, "-q", "audiotestsrc", f"wave={wave}", f"num-buffers={seconds * 47}",
         "!", "audio/x-raw,rate=48000,channels=2", "!", "avenc_aac", "!", "aacparse", "!",
         "mpegtsmux", "!", "hlssink", f"location={d}/seg%05d.ts",
         f"playlist-location={d}/index.m3u8", "target-duration=1", "max-files=0",
         "playlist-length=0"],
        check=True, timeout=60, capture_output=True)
    return d


@pytest.mark.parametrize("wave,audible", [("sine", True), ("silence", False)])
def test_hls_with_separate_audio_keeps_the_sound(tmp_path, wave, audible):
    """Picture and sound in playlists of their own (EXT-X-MEDIA): the sound
    reaches the transport stream and decodes to what was sent, and the
    self-check tells a silent one apart."""
    from lindvb import selfcheck
    _gst("videotestsrc", "x264enc", "avenc_aac", "mpegtsmux", "hlssink", "urisourcebin",
         "parsebin", "appsink", "h264parse", "audioconvert")
    gst_launch = shutil.which("gst-launch-1.0")
    if gst_launch is None:
        pytest.skip("gst-launch-1.0 missing")
    v = tmp_path / "video"
    v.mkdir()
    subprocess.run(
        [gst_launch, "-q", "videotestsrc", "num-buffers=100", "!",
         "video/x-raw,width=320,height=180,framerate=25/1,format=I420", "!",
         "x264enc", "key-int-max=25", "speed-preset=ultrafast", "!", "h264parse", "!",
         "mpegtsmux", "!", "hlssink", f"location={v}/seg%05d.ts",
         f"playlist-location={v}/index.m3u8", "target-duration=1", "max-files=0",
         "playlist-length=0"],
        check=True, timeout=60, capture_output=True)
    _make_hls_audio(tmp_path, "audio", wave=wave)
    (tmp_path / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="audio_0",DEFAULT=YES,CHANNELS="2",'
        'URI="audio/index.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x180,'
        'CODECS="avc1.4d401f,mp4a.40.2",AUDIO="a"\nvideo/index.m3u8\n')
    old = os.getcwd()
    os.chdir(tmp_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Files)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/master.m3u8"
        r, data, errors = _read_all(ipstream.GstReader, url,
                                    retry=ipstream.Retry(delays=(5,), give_up=30))
    finally:
        srv.shutdown()
        os.chdir(old)
    assert not errors and ipstream.looks_like_ts(data) and r.passthrough is False
    out = tmp_path / "out.ts"
    out.write_bytes(data)
    error, counts, _used = selfcheck._play(out.as_uri())
    assert not error and counts["video"] >= 50 and counts["audio"] > 0
    assert selfcheck.silent(counts) is not audible


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
    assert p._retry is not None                       # play_uri(live=True) sets it
    assert p._try_reconnect("gone") and seen == [(1, "gone")]
    assert p._try_reconnect("gone again") and len(seen) == 1     # the same break
    p._cancel_reconnect()
    p.uri = "file:///tmp/film.ts"
    assert not p._try_reconnect("end")
    p.uri, p._retry = "http://example.invalid/archive.ts", None     # not live: no retry
    assert not p._try_reconnect("end")
    p._retry = ipstream.Retry()
    p.uri, p.settings["stream_reconnect"] = "http://example.invalid/live.ts", False
    assert not p._try_reconnect("gone")
    p.stop()


# -- internet radio: the song titles go into the buffer -------------------------------

@pytest.fixture
def icy_radio(tmp_path):
    """An Icecast-like MP3 station that changes the song title every second."""
    _gst("audiotestsrc", "lamemp3enc", "icydemux", "souphttpsrc", "mpegtsmux", "appsink")
    gst_launch = shutil.which("gst-launch-1.0")
    if gst_launch is None:
        pytest.skip("gst-launch-1.0 missing")
    mp3 = tmp_path / "radio.mp3"
    subprocess.run([gst_launch, "-q", "audiotestsrc", "num-buffers=430", "!", "audioconvert",
                    "!", "lamemp3enc", "target=bitrate", "bitrate=64", "cbr=true", "!",
                    "filesink", f"location={mp3}"], check=True, timeout=60, capture_output=True)
    data = mp3.read_bytes()
    metaint = 8000                       # 64 kbit/s: one second of sound

    class Station(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            icy = self.headers.get("Icy-MetaData") == "1"
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("icy-name", "Test FM")
            if icy:
                self.send_header("icy-metaint", str(metaint))
            self.end_headers()
            n = 0
            try:
                for pos in range(0, len(data), metaint):
                    self.wfile.write(data[pos:pos + metaint])
                    if icy:
                        m = f"StreamTitle='Artist {n} - Song {n}';".encode()
                        m += b"\0" * (-len(m) % 16)
                        self.wfile.write(bytes([len(m) // 16]) + m)
                    n += 1
                    time.sleep(0.25)
            except OSError:
                pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Station)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/live"
    finally:
        srv.shutdown()


def test_radio_titles_into_the_stream(icy_radio, tmp_path):
    r, data, errors = _read_all(ipstream.StreamSource, icy_radio, seconds=6, meta=True)
    assert not errors and isinstance(r.reader, ipstream.GstReader) and \
        r.reader.passthrough is False
    found = ipstream.find_meta(data)
    titles = []
    for _pos, fields in found:
        if not titles or titles[-1] != fields.get("title"):
            titles.append(fields.get("title"))
    assert len(titles) >= 3 and titles[1] == "Artist 0 - Song 0"
    assert titles[2] == "Artist 1 - Song 1"
    # the titles come in order, each once a change and then as repeats
    assert all(t is None or t.startswith("Artist ") for t in titles)
    assert [p for p, _f in found] == sorted(p for p, _f in found)
    assert all(p % 188 == 0 for p, _f in found)            # whole packets
    # the packed stream with the title packets still plays
    out = tmp_path / "radio.ts"
    out.write_bytes(data)
    GLib, Gst = _gst("tsdemux", "decodebin")
    pl = Gst.parse_launch(f'filesrc location="{out}" ! decodebin ! audioconvert ! '
                          'fakesink sync=false')
    pl.set_state(Gst.State.PLAYING)
    msg = pl.get_bus().timed_pop_filtered(20 * Gst.SECOND,
                                          Gst.MessageType.ERROR | Gst.MessageType.EOS)
    pl.set_state(Gst.State.NULL)
    assert msg is not None and msg.type == Gst.MessageType.EOS


def test_without_meta_no_title_packets(icy_radio):
    _r, data, errors = _read_all(ipstream.StreamSource, icy_radio, seconds=3)
    assert not errors and data and ipstream.find_meta(data) == []


def test_capture_of_an_internet_radio(icy_radio, tmp_path):
    GLib, Gst = _gst("appsrc", "tsparse", "multifilesink")
    from lindvb.config import DEFAULTS
    from lindvb.tuner import SegmentReader, TunerManager
    settings = dict(DEFAULTS, timeshift_dir=str(tmp_path), timeshift_max_mb=64)
    tm = TunerManager(settings, probe=lambda a, f: [])
    ch = Channel(name="Test FM", delsys="iptv", url=icy_radio, radio=True)
    assert ch.buffered
    cap = tm.acquire(ch, "live")
    assert cap.meta_titles
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(5, lambda: (loop.quit(), False)[1])
    loop.run()
    start, end = cap.store.bounds()
    reader = SegmentReader(cap.store, start)
    data = b""
    while True:
        piece = reader.read()
        if not piece:
            break
        data += piece
    reader.close()
    tm.release(cap, "live")
    titles = {f.get("title") for _p, f in ipstream.find_meta(data)}
    assert end > 10_000 and "Artist 1 - Song 1" in titles      # tsparse kept the PID


def test_player_shows_the_title_of_the_place_it_plays(tmp_path):
    """After a jump back the title that ran there shows at once, and the
    later ones as playback reaches them, not when the feeder reads them."""
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
    from lindvb.tuner import SegmentStore

    null = b"\x47\x1f\xff\x10" + b"\xff" * 184
    store = SegmentStore(tmp_path / "buf")
    seg = bytearray()
    for i in range(10):                  # a title every 100 packets
        seg += ipstream.meta_packet({"title": f"Song {i}", "organization": "Test FM"})
        seg += null * 99
    store.path(0).write_bytes(bytes(seg))

    class Cap:
        meta_titles = True
        timeshift = True

        def __init__(self):
            self.store = store

        def byte_rate(self):
            return 188 * 50.0            # two seconds between titles

    p = Player(Gtk.Picture(), dict(DEFAULTS, deinterlace=False, video_decoder="software"))
    cap = Cap()
    shown = []
    p.connect("metadata", lambda _p, md: shown.append(md.get("title")))
    p.capture, p._token = cap, 5
    p._start_meta(cap, 188 * 450)        # in the middle of song 4
    assert p.metadata == {"title": "Song 4", "organization": "Test FM"}
    assert p._meta_timer
    # the feeder reads ahead; what it finds shows when playback gets there
    p._queue_meta(188 * 500, {"title": "Song 5"}, 5)
    p._queue_meta(188 * 500 + 188 * 100, {"title": "Song 6"}, 5)
    p._queue_meta(188 * 700, {"title": "Old feeder"}, 4)            # an earlier start
    now = [188 * 480]
    p.played_offset = lambda: now[0]
    assert p._apply_meta(5) and p.metadata["title"] == "Song 4"
    now[0] = 188 * 650
    assert p._apply_meta(5) and p.metadata["title"] == "Song 6"
    assert shown == ["Song 4", "Song 6"] and p._meta_queue == []
    assert not p._apply_meta(4)                                     # a stale timer stops
    p._stop_meta()
    assert not p._meta_timer
