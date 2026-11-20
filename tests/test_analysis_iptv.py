"""Channel analysis of an internet channel on screen (issue: IPTV showed mostly dashes).

An HTTP/HLS channel is read into a capture for timeshift (lindvb.ipstream)
and played from there through the appsrc. The analysis has to measure that
stream: the transport stream for the bit rates, PCR and tables, the capture
for the buffer, the download and the reconnects. The report has to carry
every section the window shows, the internet stream with its address first.

Needs GStreamer with x264enc, an AAC encoder, mpegtsmux, hlssink and GTK 4;
skipped otherwise. The stream is served from a local HTTP server.
"""

import functools
import http.server
import threading
import time
import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
try:
    gi.require_version("Gtk", "4.0")
except ValueError:
    pytest.skip("GTK 4 missing", allow_module_level=True)
from gi.repository import GLib, GObject, Gst, Gtk  # noqa: E402

Gst.init(None)

AAC = next((n for n in ("voaacenc", "avenc_aac", "fdkaacenc") if Gst.ElementFactory.find(n)), None)
NEEDED = ("x264enc", "mpegtsmux", "hlssink", "hlsdemux", "urisourcebin", "parsebin", "playbin",
          "tsdemux", "souphttpsrc")
pytestmark = pytest.mark.skipif(
    AAC is None or any(Gst.ElementFactory.find(n) is None for n in NEEDED),
    reason="GStreamer elements for the IPTV analysis test are missing")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a):
        pass


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("iptv")
    pipe = Gst.parse_launch(
        "videotestsrc num-buffers=500 ! video/x-raw,width=320,height=180,framerate=25/1 "
        "! x264enc key-int-max=25 bitrate=600 ! h264parse ! queue ! mux. "
        f"audiotestsrc num-buffers=940 ! audio/x-raw,rate=48000 ! {AAC} ! aacparse ! queue ! mux. "
        "mpegtsmux name=mux ! tee name=t ! queue ! hlssink target-duration=2 max-files=0 "
        f"playlist-length=0 location={root}/s%05d.ts playlist-location={root}/live.m3u8 "
        f"t. ! queue ! filesink location={root}/whole.ts")
    pipe.set_state(Gst.State.PLAYING)
    msg = pipe.get_bus().timed_pop_filtered(60 * Gst.SECOND,
                                            Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    assert msg is not None and msg.type == Gst.MessageType.EOS
    handler = functools.partial(_QuietHandler, directory=str(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class _Tuners(GObject.Object):
    __gsignals__ = {"preempt-analysis": (GObject.SignalFlags.RUN_LAST, None, (object,))}

    def __init__(self):
        super().__init__()
        self.captures = {}


def _run(seconds, until=None):
    loop = GLib.MainLoop()
    end = time.monotonic() + seconds

    def tick():
        if time.monotonic() > end or (until is not None and until()):
            loop.quit()
            return False
        return True
    GLib.timeout_add(200, tick)
    loop.run()


@pytest.fixture
def setup(server, tmp_path, monkeypatch):
    if not Gtk.init_check():
        pytest.skip("no display")
    from lindvb import contentguide
    # the player switches on internet subtitles for the whole program: put it back
    monkeypatch.setattr(contentguide, "INTERNET_SUBTITLES", contentguide.INTERNET_SUBTITLES)
    from lindvb.channels import Channel
    from lindvb.config import DEFAULTS
    from lindvb.player import Player
    settings = dict(DEFAULTS, deinterlace=False, video_decoder="software",
                    timeshift_dir=str(tmp_path), stream_engine="playbin")
    player = Player(Gtk.Picture(), settings)
    tuners = _Tuners()
    window = types.SimpleNamespace(player=player, current=None,
                                   quality=types.SimpleNamespace(kind="iptv", quality=1.0))
    app = types.SimpleNamespace(window=window, tuners=tuners, settings=settings,
                                analysis_sessions=[],
                                epg=types.SimpleNamespace(now_next=lambda _k: (None, None)))
    made = []

    def play(path):
        ch = Channel(name="Test IPTV", delsys="iptv", url=f"{server}/{path}")
        assert ch.buffered            # read into a capture for timeshift, as by default
        from lindvb import ipstream
        from lindvb.tuner import Capture
        cap = Capture(ch, 900, 0, settings, tuner=ipstream.stream_profile(900, ch), live=True)
        cap.users.add("live")
        tuners.captures[900] = cap
        cap.start()
        made.append(cap)
        window.current = ch
        _run(15, lambda: cap.store.bounds()[1] > 200_000)
        player.play_capture(cap, ch.sid)
        return ch, cap
    yield app, play
    for s in list(app.analysis_sessions):
        s.stop()
    player.stop()
    for cap in made:
        cap.destroy()


@pytest.mark.parametrize("path", ["live.m3u8", "whole.ts"])
def test_iptv_on_screen_is_measured_from_the_players_stream(setup, path):
    from lindvb.analysis import AnalysisSession
    app, play = setup
    ch, cap = play(path)
    session = AnalysisSession(app, ch)
    app.analysis_sessions.append(session)
    assert session._tap is app.window.player     # fed from what the player demuxes

    def ready():
        st = session.state or {}
        snap = st.get("snap") or {}
        return bool(snap.get("pmt") and (st.get("iptv") or {}).get("download_bps") and
                    session.stats("total") and session.stream)
    _run(25, ready)
    st = session.state
    snap = st["snap"]
    pmt = snap["pmt"]
    kinds = {d["kind"] for _pid, d in pmt["streams"]}
    assert {"video", "audio"} <= kinds
    apid = next(pid for pid, d in pmt["streams"] if d["kind"] == "audio")
    vpid = next(pid for pid, d in pmt["streams"] if d["kind"] == "video")
    assert st["rates"].get(apid) and st["rates"].get(vpid)        # measured kbit/s
    es = snap["es"]
    assert es[apid].get("rate") == 48000                          # sample rate
    assert es[vpid].get("profile")                                # profile / level
    assert "pcr_avg" in snap["pids"][pmt["pcr_pid"]]              # PCR interval
    tables = {t["tid"]: t for t in snap["tables"]}
    assert 0 in tables and 2 in tables                            # PAT / PMT
    ip = st["iptv"]
    assert ip["timeshift"] is True
    assert ip["buffer"].get("percent") is not None
    assert ip["download_bps"] > 0
    assert "rebuffers" in ip
    assert session.stats("total")["avg"] > 0                      # stream bit rate
    assert session.stats("service")["avg"] > 0                    # bit rate of the channel
    assert any(s.get("buffer") is not None for s in session.history)   # buffer history

    text = session.report_text()
    for heading in ("Internet stream", "Stream", "Bit rate of the channel", "Video",
                    "Frame sizes", "Audio tracks", "Loudness", "Stream health",
                    "Encryption", "Now & next", "Tables", "PID"):
        assert heading in text, heading
    assert f"Address: {ch.url}" in text
    assert "Protocol: HTTP" in text
    assert "HTTP status: 200" in text
    assert "Content type: " in text
    assert "Response time: " in text
    assert "Rebuffering: " in text
    if path.endswith(".m3u8"):
        assert "Playlist: HLS media playlist" in text
        assert "Segments: " in text
    assert "PCR interval: " in text and "PCR interval: –" not in text
    assert "Sample rate" in text and "48 kHz" in text
    d = session.report_dict()
    assert d["sections"] and d["sections"][0]["title"]
