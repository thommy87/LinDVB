# SPDX-License-Identifier: GPL-2.0-or-later
"""Stream samples for the regression net (tests/test_regression_net.py).

Each sample stands for one shape of stream seen on air; the channel it was
seen on is named, so a failing test says which broadcast it is about. They
are made on the fly with GStreamer (a few seconds of test picture and tone)
and served by a local HTTP server, so the tests need no network and no
files of their own in the repository:

* ``hls_redundant``: 3sat-like HLS: the same variant twice (two CDN copies),
  each with its own rendition groups, so every audio and subtitle rendition
  is listed twice in the master playlist; the variant carries German sound
  itself, "Klare Sprache" and "Audio-Deskription" are renditions of their own.
* ``hls_vtt_without_timestamps``: PBS-like HLS: a WebVTT rendition whose
  segments carry no X-TIMESTAMP-MAP and no cues.
* ``live_hls``: a live HLS stream (sliding playlist, no ENDLIST) whose server
  can be told to stop answering after a while, as zdf-hls-18 did for 3sat.
* ``make_captioned_segments``: H.264 with CEA-708 captions (service 1) in
  the picture's user data, as US stations send them (WJXT, PBS on air).
* ``dash_with_roles``: the DASH sample of tests/data/dash with Role and
  Accessibility on its audio, as ARD's DVB-I streams have them (ONE HD).

``Server`` serves a folder; its ``stall_after`` makes every request after
that many seconds hang until the server shuts down, its ``refuse`` answers
every request with that HTTP status (403: a CDN that does not serve us).
"""

import http.server
import shutil
import threading
import time
from pathlib import Path

import pytest

DATA = Path(__file__).with_name("data")


def gst(*elements):
    """(GLib, Gst), or skip the test when GStreamer or an element is missing."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    missing = [e for e in elements if Gst.ElementFactory.find(e) is None]
    if missing:
        pytest.skip("GStreamer elements missing: " + ", ".join(missing))
    return GLib, Gst


def _pipeline(desc, timeout=120, feed=None):
    """Run a gst-launch style pipeline to its end, in this process (CI has
    no gst-launch-1.0). *feed(pipeline)* pushes data into appsrcs first."""
    _GLib, Gst = gst()
    pl = Gst.parse_launch(desc)
    if feed is not None:
        feed(pl)
    pl.set_state(Gst.State.PLAYING)
    msg = pl.get_bus().timed_pop_filtered(timeout * Gst.SECOND,
                                          Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pl.set_state(Gst.State.NULL)
    if msg is None or msg.type != Gst.MessageType.EOS:
        raise RuntimeError(f"stream sample not made: {msg and msg.parse_error()}")


def _hls(folder):
    return (f"hlssink location={folder}/seg%05d.ts playlist-location={folder}/index.m3u8 "
            "target-duration=1 max-files=0 playlist-length=0")


def _video(seconds, width, height, pattern):
    return (f"videotestsrc num-buffers={seconds * 25} pattern={pattern} ! "
            f"video/x-raw,width={width},height={height},framerate=25/1,format=I420")


def _audio(seconds, freq=440):
    return (f"audiotestsrc freq={freq} num-buffers={seconds * 47} ! "
            "audio/x-raw,rate=48000,channels=2 ! avenc_aac ! aacparse")


def make_av_segments(folder, seconds=6, width=320, height=180, pattern="ball"):
    """H.264 + AAC in TS segments of 1 s and a VOD playlist (index.m3u8)."""
    gst("videotestsrc", "x264enc", "audiotestsrc", "avenc_aac", "mpegtsmux", "hlssink")
    folder.mkdir(parents=True, exist_ok=True)
    _pipeline(f"{_video(seconds, width, height, pattern)} ! "
              "x264enc key-int-max=25 speed-preset=ultrafast ! h264parse ! queue ! mux. "
              f"{_audio(seconds)} ! queue ! mux. mpegtsmux name=mux ! {_hls(folder)}")
    return folder


def make_audio_segments(folder, seconds=6, freq=440):
    """An audio-only HLS rendition: AAC in TS segments of 1 s."""
    gst("audiotestsrc", "avenc_aac", "mpegtsmux", "hlssink")
    folder.mkdir(parents=True, exist_ok=True)
    _pipeline(f"{_audio(seconds, freq)} ! mpegtsmux ! {_hls(folder)}")
    return folder


def make_vtt(folder, seconds=6, timestamps=True, text="Untertitel"):
    """WebVTT segments of 1 s. With *timestamps* they carry X-TIMESTAMP-MAP
    for mpegtsmux's start (3600 s) and one cue each, as ARD's do; without,
    they are empty, as PBS's are."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(seconds):
        body = "WEBVTT\n"
        if timestamps:
            body += ("X-TIMESTAMP-MAP=MPEGTS:324000000,LOCAL:00:00:00.000\n\n"
                     f"00:00:{i:02d}.000 --> 00:00:{i:02d}.800\n{text} {i}\n")
        (folder / f"s{i:05d}.vtt").write_text(body + "\n")
        lines += ["#EXTINF:1.0,", f"s{i:05d}.vtt"]
    (folder / "index.m3u8").write_text("\n".join(lines + ["#EXT-X-ENDLIST", ""]))
    return folder


def _dtvcc_packet(seq, data, service=1):
    """One DTVCC packet (CEA-708 5) with one service block."""
    body = bytes([service << 5 | len(data)]) + bytes(data)
    size = len(body) + 1
    size += size % 2
    pkt = bytes([(seq & 3) << 6 | size // 2]) + body
    return pkt + b"\0" * (size - len(pkt))


def caption_frames(seconds, fps=25, text="LinDVB 708", per_frame=20):
    """cc_data of each frame: one line of text a second on service 1, each
    time with its window (CEA-708 DefineWindow, text, CR, DisplayWindows),
    so that every segment shows captions on its own. A DTVCC packet has to
    come within one frame for cc708overlay."""
    window = [0x98, 0x38, 0x0A, 0x00, 0x01, 0x1F, 0x09]    # DF0: visible, 2 rows, 32 columns
    frames = []
    for sec in range(seconds):
        pkt = _dtvcc_packet(sec, window +
                            list(f"{text} {sec}".encode()) + [0x0D, 0x89, 0x01])
        trip = []
        for i in range(0, len(pkt), 2):
            trip += [0xFF if i == 0 else 0xFE, pkt[i], pkt[i + 1]]   # packet start, data
        chunks = [trip[i:i + per_frame * 3] for i in range(0, len(trip), per_frame * 3)]
        for f in range(fps):
            t = chunks[f] if f < len(chunks) else []
            frames.append(bytes(t + [0xFA, 0, 0] * (per_frame - len(t) // 3)))   # padding
    return frames


def make_captioned_segments(folder, seconds=6, width=320, height=180, pattern="ball"):
    """As make_av_segments, with CEA-708 captions in the H.264 stream."""
    _GLib, Gst = gst("cccombiner", "x264enc", "avenc_aac", "mpegtsmux", "hlssink")
    folder.mkdir(parents=True, exist_ok=True)

    def feed(pl):
        src = pl.get_by_name("cc")
        for i, data in enumerate(caption_frames(seconds)):
            buf = Gst.Buffer.new_wrapped(data)
            buf.pts, buf.duration = i * Gst.SECOND // 25, Gst.SECOND // 25
            src.emit("push-buffer", buf)
        src.emit("end-of-stream")
    _pipeline(f"{_video(seconds, width, height, pattern)} ! cccombiner name=c ! "
              "x264enc key-int-max=25 speed-preset=ultrafast ! h264parse ! queue ! mux. "
              "appsrc name=cc format=time caps=closedcaption/x-cea-708,format=cc_data,framerate=25/1 "
              f"! c.caption {_audio(seconds)} ! queue ! mux. mpegtsmux name=mux ! {_hls(folder)}",
              feed=feed)
    return folder


# 3sat (zdf-hls-18 .../dach/veryhigh/master.m3u8), shortened: two copies of the
# variant, each with its own groups; the German sound of the variant itself
# is the rendition without URI
REDUNDANT_MASTER = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="TV Ton",LANGUAGE="deu",DEFAULT=YES,AUTOSELECT=YES
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="Klare Sprache",LANGUAGE="mul",DEFAULT=NO,AUTOSELECT=NO,URI="klar/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="Audio-Deskription",LANGUAGE="deu",CHARACTERISTICS="public.accessibility.describes-video",DEFAULT=NO,AUTOSELECT=NO,URI="ad/index.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s1",NAME="Untertitel deutsch",LANGUAGE="deu",DEFAULT=NO,AUTOSELECT=YES,URI="subs/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="TV Ton",LANGUAGE="deu",DEFAULT=YES,AUTOSELECT=YES
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="Klare Sprache",LANGUAGE="mul",DEFAULT=NO,AUTOSELECT=NO,URI="klar/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="Audio-Deskription",LANGUAGE="deu",CHARACTERISTICS="public.accessibility.describes-video",DEFAULT=NO,AUTOSELECT=NO,URI="ad/index.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s2",NAME="Untertitel deutsch",LANGUAGE="deu",DEFAULT=NO,AUTOSELECT=YES,URI="subs/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x180,FRAME-RATE=25.000,CODECS="avc1.42c01e,mp4a.40.2",AUDIO="a1",SUBTITLES="s1"
main/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x180,FRAME-RATE=25.000,CODECS="avc1.42c01e,mp4a.40.2",AUDIO="a2",SUBTITLES="s2"
main/index.m3u8
"""

# PBS (wedqdt4.lls.pbs.org), shortened: one variant, an English WebVTT rendition
PBS_MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="eng",DEFAULT=NO,AUTOSELECT=YES,URI="subs/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x180,CODECS="avc1.42c01e,mp4a.40.2",SUBTITLES="subs"
main/index.m3u8
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "streamkit"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.requests.append(self.path)
        if srv.refuse:
            self.send_error(srv.refuse)
            return
        if srv.stall_after is not None and time.monotonic() - srv.started > srv.stall_after:
            srv.stalled.wait()              # hangs until the server shuts down
            return
        live = srv.live.get(self.path.lstrip("/").split("?")[0])
        if live is not None:
            body = live.playlist().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class Server:
    """A folder over HTTP on 127.0.0.1; ``url(path)`` gives an address.
    *live* maps paths to LivePlaylists, served in place of files."""

    def __init__(self, folder, stall_after=None, live=None, refuse=None):
        self.folder = Path(folder)

        def handler(*a, **kw):
            return _Handler(*a, directory=str(self.folder), **kw)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.httpd.requests = []
        self.httpd.stall_after = stall_after
        self.httpd.refuse = refuse
        self.httpd.stalled = threading.Event()
        self.httpd.live = dict(live or {})
        self.httpd.started = time.monotonic()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def requests(self):
        return self.httpd.requests

    def url(self, path):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{path}"

    def close(self):
        self.httpd.stalled.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class LivePlaylist:
    """A sliding window over VOD segments, as a live HLS playlist: one more
    segment every second, *window* segments long, no ENDLIST. *prefix* is
    the segments' folder as seen from the playlist."""

    def __init__(self, segments, prefix="", window=4):
        self.segments = segments
        self.prefix = prefix
        self.window = window
        self.t0 = time.monotonic()

    def playlist(self):
        now = int(time.monotonic() - self.t0)
        last = min(now + self.window, len(self.segments))
        first = max(0, last - self.window)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1",
                 f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for s in self.segments[first:last]:
            lines += ["#EXTINF:1.0,", f"{self.prefix}{s}"]
        return "\n".join(lines) + "\n"


# -- the samples ----------------------------------------------------------------

def hls_redundant(folder, seconds=6):
    make_av_segments(folder / "main", seconds)
    make_audio_segments(folder / "klar", seconds, freq=660)
    make_audio_segments(folder / "ad", seconds, freq=880)
    make_vtt(folder / "subs", seconds)
    (folder / "master.m3u8").write_text(REDUNDANT_MASTER)
    return folder / "master.m3u8"


def hls_vtt_without_timestamps(folder, seconds=6):
    make_av_segments(folder / "main", seconds)
    make_vtt(folder / "subs", seconds, timestamps=False)
    (folder / "master.m3u8").write_text(PBS_MASTER)
    return folder / "master.m3u8"


def _live(folder, pattern):
    return LivePlaylist(sorted(p.name for p in folder.glob(pattern)))


def live_hls(folder, seconds=30):
    """A live stream: serve with Server(folder, live=...) and open
    server.url("main/live.m3u8")."""
    seg = make_av_segments(folder / "main", seconds)
    return {"main/live.m3u8": _live(seg, "seg*.ts")}


def live_hls_vtt_without_timestamps(folder, seconds=30):
    """PBS as it is on air: live, with the empty WebVTT rendition. Serve
    with Server(folder, live=...) and open server.url("master.m3u8")."""
    seg = make_av_segments(folder / "main", seconds)
    subs = make_vtt(folder / "subs", seconds, timestamps=False)
    (folder / "master.m3u8").write_text(
        PBS_MASTER.replace("subs/index.m3u8", "subs/live.m3u8").replace(
            "main/index.m3u8", "main/live.m3u8"))
    return {"main/live.m3u8": _live(seg, "seg*.ts"), "subs/live.m3u8": _live(subs, "s*.vtt")}


DASH_ROLES = {
    # adaptation set id -> what ARD's DVB-I manifests put on it
    "1": ('<Role schemeIdUri="urn:mpeg:dash:role:2011" value="main" />',),
    "2": ('<Role schemeIdUri="urn:mpeg:dash:role:2011" value="description" />',
          '<Accessibility schemeIdUri="urn:tva:metadata:cs:AudioPurposeCS:2007" value="1" />'),
}


def dash_with_roles(folder):
    """The DASH sample (video, German and English AAC, TTML subtitles) with
    the English set turned into an audio description by Role/Accessibility."""
    src = DATA / "dash"
    for p in src.iterdir():
        shutil.copy(p, folder / p.name)
    text = (src / "manifest-subtitles.mpd").read_text()
    for set_id, extra in DASH_ROLES.items():
        marker = f'<AdaptationSet id="{set_id}" '
        start = text.index(marker)
        end = text.index(">", start) + 1
        text = text[:end] + "\n\t\t\t" + "\n\t\t\t".join(extra) + text[end:]
    (folder / "manifest-roles.mpd").write_text(text)
    return folder / "manifest-roles.mpd"
