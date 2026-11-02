"""Recording an HLS stream whose audio is a separate rendition (as ZDF / ARD do).

Needs GStreamer with x264enc, an AAC encoder, hlssink2 and hlsdemux2; skipped
otherwise. The stream is served from a local HTTP server.
"""

import functools
import http.server
import threading
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

AAC = next((n for n in ("voaacenc", "avenc_aac", "fdkaacenc") if Gst.ElementFactory.find(n)), None)
NEEDED = ("x264enc", "hlssink2", "hlsdemux2", "urisourcebin", "parsebin", "matroskamux",
          "mp4mux", "mpegtsmux")
pytestmark = pytest.mark.skipif(
    AAC is None or any(Gst.ElementFactory.find(n) is None for n in NEEDED),
    reason="GStreamer elements for the HLS test are missing")

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="deu",NAME="Deutsch",DEFAULT=YES,AUTOSELECT=YES,URI="a/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="eng",NAME="English",DEFAULT=NO,AUTOSELECT=YES,URI="e/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=320x180,CODECS="avc1.64000d,mp4a.40.2",AUDIO="aud"
v/index.m3u8
"""


def _run(desc):
    pipe = Gst.parse_launch(desc)
    pipe.set_state(Gst.State.PLAYING)
    msg = pipe.get_bus().timed_pop_filtered(30 * Gst.SECOND,
                                            Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    assert msg is not None and msg.type == Gst.MessageType.EOS


def _segments(root, sub, src):
    (root / sub).mkdir()
    _run(f"{src} ! hlssink2 location={root / sub}/s%05d.ts "
         f"playlist-location={root / sub}/index.m3u8 target-duration=1 "
         "playlist-length=0 max-files=0")


@pytest.fixture(scope="module")
def hls(tmp_path_factory):
    root = tmp_path_factory.mktemp("hls")
    _segments(root, "v", "videotestsrc num-buffers=100 ! video/x-raw,width=320,height=180,"
                         "framerate=25/1 ! x264enc key-int-max=25 ! h264parse")
    for sub, wave in (("a", "sine"), ("e", "square")):
        _segments(root, sub, f"audiotestsrc wave={wave} num-buffers=190 ! "
                             f"audio/x-raw,rate=48000 ! {AAC} ! aacparse")
    (root / "master.m3u8").write_text(MASTER)
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/master.m3u8"
    server.shutdown()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a):
        pass


def _record(url, fmt, path, **kw):
    from lindvb.remux import Remuxer
    loop = GLib.MainLoop()
    rec = Remuxer(fmt, path, url=url, on_done=lambda _r: loop.quit(), **kw)
    rec.start()
    GLib.timeout_add_seconds(40, loop.quit)
    loop.run()
    return rec


def _streams(path):
    from gi.repository import GstPbutils
    info = GstPbutils.Discoverer.new(10 * Gst.SECOND).discover_uri(Path(path).as_uri())
    return ([s.get_caps().get_structure(0).get_name() for s in info.get_video_streams()],
            [s.get_caps().get_structure(0).get_name() for s in info.get_audio_streams()])


@pytest.mark.parametrize("fmt", ["TS", "MKV", "MP4"])
def test_hls_with_audio_rendition_keeps_video_and_audio(hls, tmp_path, fmt):
    gi.require_version("GstPbutils", "1.0")
    out = tmp_path / f"rec.{fmt.lower()}"
    rec = _record(hls, fmt, out)
    assert rec.have_video and rec.audio_count == 1, rec.dropped
    video, audio = _streams(rec.path)
    assert video == ["video/x-h264"]
    assert audio == ["audio/mpeg"]


def test_hls_audio_language_is_chosen(hls, tmp_path):
    rec = _record(hls, "MKV", tmp_path / "rec.mkv", audio_mode="language", language="en")
    assert rec.selected is not None and len(rec.selected) == 2
    assert rec.selected[1].endswith("English")          # the rendition's NAME
    assert rec.have_video and rec.audio_count == 1
