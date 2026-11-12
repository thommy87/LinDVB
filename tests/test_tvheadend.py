"""Tvheadend as a channel source: addresses, digest/basic login, channel
list with tags, guide matching (no GTK). A small HTTP server stands in for
Tvheadend and checks the authentication like the real one does."""
import hashlib
import http.server
import threading
import urllib.parse

import pytest

from lindvb import iptvsources, tvheadend, xmltvmatch
from lindvb.channels import Channel

U1 = "0123456789abcdef0123456789abcdef"
U2 = "fedcba9876543210fedcba9876543210"
U3 = "11111111111111111111111111111111"


def m3u(host):
    return f"""#EXTM3U
#EXTINF:-1 tvg-logo="http://{host}/imagecache/7" tvg-id="{U1}" tvg-chno="1",Das Erste HD
http://{host}/stream/channelid/101?profile=pass
#EXTINF:-1 tvg-id="{U2}" tvg-chno="2",ZDF HD
http://{host}/stream/channelid/102?profile=pass
#EXTINF:-1 tvg-id="{U3}" tvg-chno="100",Bayern 3
http://{host}/stream/channelid/103?profile=pass
""".encode()


def e2(host):
    q = urllib.parse.quote(f"http://{host}", safe="")
    return f"""#NAME Tvheadend Channels
#SERVICE 1:64:0:0:0:0:0:0:0:0::Öffentlich-rechtlich
#DESCRIPTION Öffentlich-rechtlich
#SERVICE 1:0:0:0:0:0:0:0:0:0:{q}/stream/channelid/101?profile=pass:Das Erste HD
#DESCRIPTION Das Erste HD
#SERVICE 1:0:0:0:0:0:0:0:0:0:{q}/stream/channelid/102?profile=pass:ZDF HD
#DESCRIPTION ZDF HD
#SERVICE 1:64:1:0:0:0:0:0:0:0::Radio channels
#DESCRIPTION Radio channels
#SERVICE 1:0:0:0:0:0:0:0:0:0:{q}/stream/channelid/103?profile=pass:Bayern 3
#DESCRIPTION Bayern 3
""".encode()


def xmltv(ids):
    chans = "".join(f'<channel id="{i}"><display-name>{n}</display-name></channel>'
                    for i, n in ids)
    first = ids[0][0]
    return (f'<?xml version="1.0"?><tv>{chans}<programme start="20261112200000 +0000" '
            f'stop="20261112201500 +0000" channel="{first}"><title>Tagesschau</title>'
            "</programme></tv>").encode()


class FakeTvheadend(http.server.BaseHTTPRequestHandler):
    mode = "digest"            # "digest", "sha256", "basic" or "none"
    guide_ids = [(U1, "Das Erste HD"), (U2, "ZDF HD")]
    seen = []

    def log_message(self, *a):
        pass

    def _authorized(self):
        if self.mode == "none":
            return True
        got = self.headers.get("Authorization", "")
        if self.mode == "basic":
            import base64
            return got == "Basic " + base64.b64encode(b"alex:geheim").decode()
        if not got.startswith("Digest "):
            return False
        p = dict(tvheadend.parse_challenges([got])[0][1])
        algo = p.get("algorithm", "MD5")
        h = {"MD5": lambda s: hashlib.md5(s.encode()).hexdigest(),
             "SHA-256": lambda s: hashlib.sha256(s.encode()).hexdigest()}[algo]
        ha1 = h("alex:tvheadend:geheim")
        ha2 = h(f"GET:{p['uri']}")
        want = h(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:auth:{ha2}")
        return p["response"] == want and p["uri"] == self.path and p["opaque"] == "op"

    def do_GET(self):
        FakeTvheadend.seen.append(self.path)
        if not self._authorized():
            self.send_response(401)
            if self.mode == "basic":
                self.send_header("WWW-Authenticate", 'Basic realm="tvheadend"')
            else:
                if self.mode == "sha256":
                    self.send_header("WWW-Authenticate", 'Digest realm="tvheadend", qop=auth, '
                                     'algorithm=SHA-256, nonce="n1", opaque="op"')
                self.send_header("WWW-Authenticate", 'Digest realm="tvheadend", qop=auth, '
                                 'nonce="n1", opaque="op"')
            self.end_headers()
            return
        host = self.headers["Host"]
        path = self.path.split("?")[0]
        body = {"/playlist/channels.m3u": lambda: m3u(host),
                "/playlist/e2/tags": lambda: e2(host),
                "/xmltv/channels": lambda: xmltv(self.guide_ids),
                "/imagecache/7": lambda: b"\x89PNG logo"}.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server():
    FakeTvheadend.mode = "digest"
    FakeTvheadend.guide_ids = [(U1, "Das Erste HD"), (U2, "ZDF HD")]
    FakeTvheadend.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTvheadend)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    tvheadend._servers.clear()


def test_base_url():
    assert tvheadend.base_url("tvh") == "http://tvh:9981"
    assert tvheadend.base_url(" tvh.local:9982/ ") == "http://tvh.local:9982"
    assert tvheadend.base_url("https://tv.example.org/tvheadend/") == \
        "https://tv.example.org/tvheadend"
    assert tvheadend.base_url("[fe80::1]") == "http://[fe80::1]:9981"
    with pytest.raises(tvheadend.TvheadendError):
        tvheadend.base_url("  ")


def test_challenge_choice():
    ch = tvheadend.parse_challenges([
        'Digest realm="tvheadend", qop=auth, algorithm=SHA-256, nonce="abc", opaque="x"',
        'Digest realm="tvheadend", qop=auth, nonce="abc", opaque="x"',
        'Basic realm="tvheadend"'])
    assert [s for s, _p in ch] == ["digest", "digest", "basic"]
    assert ch[0][1]["nonce"] == "abc" and ch[0][1]["algorithm"] == "SHA-256"
    scheme, p = tvheadend.pick_challenge(ch)
    assert scheme == "digest" and p["algorithm"] == "SHA-256"
    assert tvheadend.pick_challenge([("basic", {})])[0] == "basic"
    assert tvheadend.pick_challenge([("digest", {"algorithm": "SHA-1"})]) is None


def test_digest_header_rfc2617_example():
    # the example of RFC 2617, section 3.5
    p = {"realm": "testrealm@host.com", "qop": "auth,auth-int",
         "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093", "opaque": "5ccc069c403ebaf9f0171e9517f40e41"}
    h = tvheadend.digest_header(p, "Mufasa", "Circle Of Life", "GET", "/dir/index.html",
                                cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in h
    assert "nc=00000001" in h and 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in h


@pytest.mark.parametrize("mode", ["digest", "sha256", "basic", "none"])
def test_load_channels_with_login(server, mode):
    FakeTvheadend.mode = mode
    chans = tvheadend.load_channels(server, "alex", "geheim")
    assert [c.name for c in chans] == ["Das Erste HD", "ZDF HD", "Bayern 3"]
    erste, zdf, b3 = chans
    assert erste.url == f"{server}/stream/channel/{U1}?profile=pass"
    assert erste.xmltv_id == U1 and erste.tvh_server == server and erste.is_iptv
    assert erste.category == "Öffentlich-rechtlich" and not erste.radio
    assert b3.radio and b3.category == ""
    assert erste.root == "Tvheadend 127.0.0.1"
    assert erste.logo_url == f"{server}/imagecache/7"


def test_wrong_password(server):
    with pytest.raises(tvheadend.TvheadendError, match="wrong user name or password"):
        tvheadend.load_channels(server, "alex", "falsch")
    with pytest.raises(tvheadend.TvheadendError, match="asks for a user name"):
        tvheadend.load_channels(server)


def test_guide_and_match(server):
    chans = tvheadend.load_channels(server, "alex", "geheim")
    xch, items = tvheadend.load_guide(server, "alex", "geheim")
    assert items[0][0] == U1 and items[0][1].title == "Tagesschau"
    for c in chans:
        c.xmltv_id = ""
    assert tvheadend.match_guide(chans, server, xch) == 2
    assert chans[0].epg_key == "xmltv:" + U1
    # a server that names its guide channels by name ("basic, no hash")
    FakeTvheadend.guide_ids = [("Das Erste HD", "Das Erste HD"), ("ZDF HD", "ZDF HD")]
    xch, _items = tvheadend.load_guide(server, "alex", "geheim")
    assert tvheadend.match_guide(chans, server, xch) == 2
    assert chans[1].xmltv_id == "ZDF HD"


def test_other_xmltv_leaves_tvheadend_channels_alone(server):
    chans = tvheadend.load_channels(server, "alex", "geheim")
    other = Channel(name="Das Erste", delsys="iptv", url="http://x/1")
    xmltvmatch.auto_match(chans + [other], {"DasErste.de": ["Das Erste"]}, overwrite=True)
    assert chans[0].xmltv_id == U1 and other.xmltv_id == "DasErste.de"


def test_credentials_and_logos(server, tmp_path):
    settings = {"iptv_sources": iptvsources.remember(
        [], "tvheadend", server, "Tvheadend", 3, user="alex", password="geheim")}
    assert settings["iptv_sources"][0]["password"] == "geheim"
    assert iptvsources.display_location(settings["iptv_sources"][0]) == \
        f"{server.split('//')[1]} (alex)"
    tvheadend.configure(settings)
    assert tvheadend.credentials(f"{server}/stream/channel/{U1}?profile=pass") == \
        ("alex", "geheim")
    assert tvheadend.credentials("http://elsewhere:9981/stream/x") is None
    assert tvheadend.credentials(server + "0/stream") is None      # another port
    chans = tvheadend.load_channels(server, "alex", "geheim")
    assert tvheadend.download_logos(chans, str(tmp_path)) == 1
    assert open(chans[0].logo, "rb").read() == b"\x89PNG logo"


def test_set_login():
    class Src:
        props = {}

        def find_property(self, name):
            return name in ("user-id", "user-pw") or None

        def set_property(self, k, v):
            self.props[k] = v
    tvheadend._servers.clear()
    tvheadend._servers["http://tvh:9981"] = ("alex", "pw")
    src = Src()
    tvheadend.set_login(src, "http://tvh:9981/stream/channel/x")
    assert src.props == {"user-id": "alex", "user-pw": "pw"}
    other = Src()
    other.props = {}
    tvheadend.set_login(other, "http://other/stream")
    assert other.props == {}
    tvheadend._servers.clear()


# --------------------------------------------------------------------------
# stage 2: the stream through a tuner capture
# --------------------------------------------------------------------------

def psi(table_id, ext, body):
    from lindvb.epgsources import mpeg_crc
    head = bytes([table_id, 0xB0 | ((len(body) + 9) >> 8), (len(body) + 9) & 0xFF,
                  ext >> 8, ext & 0xFF, 0xC1, 0, 0])
    sec = head + body
    return sec + mpeg_crc(sec).to_bytes(4, "big")


SID, TSID, PMT_PID = 28106, 1101, 0x100


def pcr_packet(pid, pcr90k, cc=0):
    """A video packet carrying a PCR (tsparse timestamps by the PCR)."""
    b = pcr90k & ((1 << 33) - 1)
    pcr = bytes([b >> 25, (b >> 17) & 0xFF, (b >> 9) & 0xFF, (b >> 1) & 0xFF,
                 ((b & 1) << 7) | 0x7E, 0])
    af = bytes([7, 0x10]) + pcr
    return bytes([0x47, pid >> 8, pid & 0xFF, 0x30 | (cc & 0x0F)]) + af + \
        b"\xff" * (184 - len(af))


def service_ts(repeat=40, start=0, step_ms=40):
    """A small transport stream of one service (as Tvheadend's "pass" profile
    sends it): PAT, PMT, a PCR and null packets, *step_ms* apart."""
    from lindvb.dsmcc import ts_packets
    pat = psi(0x00, TSID, bytes([SID >> 8, SID & 0xFF, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]))
    pmt = psi(0x02, SID, bytes([0xE1, 0x01, 0xF0, 0x00,
                                0x1B, 0xE1, 0x01, 0xF0, 0x00]))   # H.264 video on 0x101
    null = b"\x47\x1f\xff\x10" + b"\xff" * 184
    out = b""
    for i in range(start, start + repeat):
        out += ts_packets(0, [pat], i)[0] + ts_packets(PMT_PID, [pmt], i)[0] + \
            pcr_packet(0x101, i * step_ms * 90, i) + null * 20
    return out


class StreamingTvheadend(FakeTvheadend):
    def do_GET(self):
        if not self.path.startswith("/stream/"):
            return FakeTvheadend.do_GET(self)
        FakeTvheadend.seen.append(self.path)
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Digest realm="tvheadend", qop=auth, '
                             'nonce="n1", opaque="op"')
            self.end_headers()
            return
        if U3 in self.path:                       # no tuner free on the server
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        import time
        try:
            for n in range(400):
                self.wfile.write(service_ts(1, n))
                self.wfile.flush()
                time.sleep(0.01)
        except OSError:
            pass


@pytest.fixture
def streaming():
    FakeTvheadend.mode = "digest"
    FakeTvheadend.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamingTvheadend)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    tvheadend._servers.clear()
    tvheadend._servers[base] = ("alex", "geheim")
    yield base
    srv.shutdown()
    tvheadend._servers.clear()


def test_stream_reader(streaming):
    got, errors, done = [], [], threading.Event()

    def data(b):
        got.append(b)
        if sum(map(len, got)) > 100_000:
            done.set()
    r = tvheadend.StreamReader(tvheadend.stream_url(streaming, U1), data, errors.append)
    assert r.login == ("alex", "geheim")
    r.start()
    assert done.wait(10)
    r.close()
    r._thread.join(5)
    assert not r._thread.is_alive() and not errors
    assert b"".join(got)[0] == 0x47

    failed = threading.Event()
    r = tvheadend.StreamReader(tvheadend.stream_url(streaming, U3), got.append,
                               lambda m: (errors.append(m), failed.set()))
    r.start()
    assert failed.wait(10) and "503" in errors[-1]


def test_channel_kinds():
    tvh = Channel(name="Das Erste HD", delsys="iptv", url="http://tvh:9981/stream/channel/x",
                  tvh_server="http://tvh:9981", xmltv_id=U1)
    web = Channel(name="Web", delsys="iptv", url="http://x/1.m3u8")
    assert tvh.is_iptv and not tvh.via_url and web.plays_url and not tvh.plays_url
    assert tvh.epg_links == ["xmltv:" + U1]
    tvh.sid, tvh.tsid = SID, TSID
    assert tvh.epg_links == ["xmltv:" + U1, f"{TSID}:{SID}"]


def test_ait_monitor_takes_the_only_service():
    from lindvb.ait import AitMonitor
    mon = AitMonitor(0)
    mon.feed_buffer(service_ts(1))
    assert mon.sid == SID and mon.pmt_pid == PMT_PID


def test_tuner_manager_capture(streaming, tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    if not all(Gst.ElementFactory.find(e) for e in ("appsrc", "tsparse", "multifilesink")):
        pytest.skip("GStreamer elements missing")
    from lindvb.tuner import TunerManager
    from lindvb.config import DEFAULTS
    settings = dict(DEFAULTS, timeshift_dir=str(tmp_path), timeshift_max_mb=64)
    tm = TunerManager(settings, probe=lambda a, f: [])
    ch = tvheadend.channels_from_playlist(m3u("127.0.0.1"), streaming)[0]
    assert ch.tvh_server == streaming and ch.sid == 0
    cap = tm.acquire(ch, "live")
    assert cap.adapter == tvheadend.ADAPTER_BASE and cap.source_label == "Tvheadend " + \
        streaming.split("//")[1]
    assert tm.acquire(ch, "rec") is cap and cap.users == {"live", "rec"}
    tsids = []
    cap.connect("tsid", lambda _c, t: tsids.append(t))
    loop = GLib.MainLoop()
    GLib.timeout_add(100, lambda: (loop.quit(), False)[1]
                     if tsids and cap.store.bounds()[1] > 50_000 else True)
    GLib.timeout_add_seconds(10, lambda: (loop.quit(), False)[1])
    loop.run()
    assert tsids == [TSID] and (ch.sid, ch.tsid) == (SID, TSID)
    assert cap.store.bounds()[1] > 50_000             # in the timeshift buffer
    assert any(p.startswith(f"/stream/channel/{U1}") for p in FakeTvheadend.seen)
    other = tvheadend.channels_from_playlist(m3u("127.0.0.1"), streaming)[1]
    cap2 = tm.acquire(other, "live2")
    assert cap2 is not cap and cap2.adapter == tvheadend.ADAPTER_BASE + 1
    tm.release(cap, "live")
    tm.release(cap, "rec")
    tm.release(cap2, "live2")
    assert not tm.captures and cap._tvh.closed
