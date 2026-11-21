# SPDX-License-Identifier: GPL-2.0-or-later
"""HLS renditions (EXT-X-MEDIA): audio and subtitles next to the variants.

ARD-alpha offers German subtitles (WebVTT), German sound in the variants and
"Klare Sprache" as an audio rendition of its own. LinDVB showed one audio
track and no subtitles: the timeshift reader kept only the main stream. Now
the audio renditions go into the buffer on PIDs of their own (with their
names), subtitles start switched off everywhere, and the WebVTT subtitles
play when the channel is opened directly with them."""

import http.server
import os
import threading

import pytest

from lindvb import ipstream
from lindvb.streaminfo import parse_hls

from test_ipstream import Files, _gst, _make_hls_audio, _read_all

MASTER = (
    "#EXTM3U\n#EXT-X-VERSION:4\n"
    '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Deutsch",DEFAULT=NO,AUTOSELECT=YES,'
    'FORCED=NO,LANGUAGE="de",URI="subs/index.m3u8"\n'
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Deutsch",LANGUAGE="de",DEFAULT=YES,AUTOSELECT=YES\n'
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Klare Sprache",LANGUAGE="klare sprache",'
    'DEFAULT=NO,AUTOSELECT=NO,URI="klar/index.m3u8"\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x180,CODECS="avc1.42c01e,mp4a.40.2",'
    'AUDIO="aud",SUBTITLES="subs"\nmain/index.m3u8\n')


def test_master_playlist_renditions():
    media = parse_hls(MASTER + '#EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="cc",NAME="English",'
                               'LANGUAGE="en",INSTREAM-ID="CC1"\n')["media"]
    assert [(m["type"], m["name"]) for m in media] == [
        ("SUBTITLES", "Deutsch"), ("AUDIO", "Deutsch"), ("AUDIO", "Klare Sprache"),
        ("CLOSED-CAPTIONS", "English")]
    assert media[0]["uri"] == "subs/index.m3u8" and not media[1]["uri"]
    assert media[3]["instream_id"] == "CC1" and media[1]["default"]


def test_rendition_pids_are_found_again():
    media = parse_hls(MASTER)["media"]
    taken = set()
    for m in media:
        pid = ipstream.rendition_pid(m["name"], taken)
        assert ipstream.RENDITION_PID_BASE <= pid < ipstream.RENDITION_PID_BASE + \
            ipstream.RENDITION_PIDS and pid not in taken
        taken.add(pid)
    klar = ipstream.rendition_pid("Klare Sprache")
    assert ipstream.rendition_for_pid(media, klar)["name"] == "Klare Sprache"
    assert ipstream.rendition_for_pid(media, 0x100) is None
    # two names on the same PID: the second takes the next free one
    assert ipstream.rendition_pid("x", {ipstream.rendition_pid("x")}) != ipstream.rendition_pid("x")


def _make_main(folder, seconds=6):
    """The variant: H.264 and AAC in TS segments of about 1 s."""
    import shutil
    import subprocess
    gst_launch = shutil.which("gst-launch-1.0")
    if gst_launch is None:
        pytest.skip("gst-launch-1.0 missing")
    d = folder / "main"
    d.mkdir()
    subprocess.run(
        [gst_launch, "-q", "videotestsrc", f"num-buffers={seconds * 25}", "!",
         "video/x-raw,width=320,height=180,framerate=25/1,format=I420", "!",
         "x264enc", "key-int-max=25", "speed-preset=ultrafast", "!", "h264parse", "!",
         "queue", "!", "mux.", "audiotestsrc", f"num-buffers={seconds * 47}", "!",
         "audio/x-raw,rate=48000,channels=2", "!", "avenc_aac", "!", "aacparse", "!", "queue",
         "!", "mux.", "mpegtsmux", "name=mux", "!", "hlssink", f"location={d}/seg%05d.ts",
         f"playlist-location={d}/index.m3u8", "target-duration=1", "max-files=0",
         "playlist-length=0"],
        check=True, timeout=60, capture_output=True)


def _make_subs(folder, seconds=6):
    """WebVTT segments that line up with the TS segments (mpegtsmux starts at
    3600 s, MPEGTS 324000000)."""
    d = folder / "subs"
    d.mkdir()
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(seconds):
        (d / f"s{i}.vtt").write_text(
            "WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:324000000,LOCAL:00:00:00.000\n\n"
            f"00:00:{i:02d}.000 --> 00:00:{i:02d}.800\nUntertitel {i}\n\n")
        lines += ["#EXTINF:1.0,", f"s{i}.vtt"]
    (d / "index.m3u8").write_text("\n".join(lines + ["#EXT-X-ENDLIST", ""]))


@pytest.fixture
def ard(tmp_path):
    _gst("videotestsrc", "x264enc", "avenc_aac", "mpegtsmux", "hlssink", "urisourcebin",
         "parsebin", "appsink", "h264parse", "hlsdemux2")
    _make_main(tmp_path)
    _make_hls_audio(tmp_path, "klar", seconds=6)
    _make_subs(tmp_path)
    (tmp_path / "master.m3u8").write_text(MASTER)
    old = os.getcwd()
    os.chdir(tmp_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Files)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/master.m3u8", tmp_path
    finally:
        srv.shutdown()
        os.chdir(old)


def _pmt_streams(data):
    """[(pid, stream type, language)] of the last PMT version in a TS."""
    from lindvb.tsanalysis import parse_pat, parse_pmt_full
    sections = {}
    for i in range(0, len(data) - 187, 188):
        p = data[i:i + 188]
        if not p[1] & 0x40:
            continue
        pid = (p[1] & 0x1f) << 8 | p[2]
        off = 5 + p[4] if p[3] & 0x20 else 4
        s = p[off + 1 + p[off]:]
        sections[pid] = s[:((s[1] & 0x0f) << 8 | s[2]) + 3]
    pmt_pid = next(pid for prog, pid in parse_pat(sections[0]).items() if prog)
    pmt = parse_pmt_full(sections[pmt_pid])
    return [(pid, d["kind"], d.get("language", "")) for pid, d in pmt["streams"]]


def test_reader_keeps_the_audio_renditions(ard, tmp_path):
    """Both German tracks reach the buffer, each on the PID its name gives,
    and the one with a real language says so in the PMT."""
    url, _folder = ard
    r, data, errors = _read_all(ipstream.GstReader, url,
                                retry=ipstream.Retry(delays=(5,), give_up=30))
    assert not errors and ipstream.looks_like_ts(data)
    streams = _pmt_streams(data)
    audio = {pid: lang for pid, kind, lang in streams if kind == "audio"}
    taken = set()
    pids = {}
    for name in ("Deutsch", "Klare Sprache"):
        pids[name] = ipstream.rendition_pid(name, taken)
        taken.add(pids[name])
    assert set(audio) == set(pids.values())
    assert audio[pids["Deutsch"]] in ("deu", "ger")
    assert [k for _p, k, _l in streams].count("video") == 1


@pytest.fixture
def player():
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
    except ValueError:
        pytest.skip("GTK 4 missing")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, Gtk
    Gst.init(None)
    if not Gtk.init_check():
        pytest.skip("no display")
    from lindvb.config import DEFAULTS
    from lindvb.player import Player
    p = Player(Gtk.Picture(), dict(DEFAULTS, deinterlace=False, video_decoder="software"))
    yield p
    p.stop()


def _run(seconds, until=None):
    from gi.repository import GLib
    loop = GLib.MainLoop()
    import time
    end = time.monotonic() + seconds

    def tick():
        if time.monotonic() > end or (until and until()):
            loop.quit()
            return False
        return True
    GLib.timeout_add(100, tick)
    loop.run()


def test_buffer_lists_the_renditions_by_name(ard, tmp_path, player):
    """Played from the buffer: 'Klare Sprache' by name, the playlist's default
    (Deutsch) plays, no subtitles shown."""
    url, _folder = ard
    _r, data, errors = _read_all(ipstream.GstReader, url,
                                 retry=ipstream.Retry(delays=(5,), give_up=30))
    out = tmp_path / "buffer.ts"
    out.write_bytes(data)
    uri = out.as_uri()
    player.renditions[uri] = parse_hls(MASTER)["media"]
    player.play_uri(uri)
    _run(10, lambda: len(player.audio_tracks()[0]) == 2 and player.audio_tracks()[1] >= 0
         and player._audio_pref_done)
    _run(1)
    labels, cur = player.audio_tracks()
    assert any(lab.startswith("Klare Sprache") for lab in labels)
    assert "Klare Sprache" not in labels[cur]
    assert player.subtitle_tracks() == ([], -1)


def test_subtitles_start_off_and_off_works(ard, player):
    """Played directly (playbin3): the WebVTT rendition is offered, does not
    switch itself on, goes on when picked and off with Off."""
    from gi.repository import Gst
    url, _folder = ard
    player.settings["stream_engine"] = "playbin3"
    player.play_uri(url)
    _run(15, lambda: player.subtitle_tracks()[0] and player._played)
    labels, cur = player.subtitle_tracks()
    assert labels == ["Deutsch"] and cur == -1
    assert not player.playbin.get_property("flags") & 4
    player.select_subtitle(0)
    _run(15, lambda: player.subtitle_tracks()[1] == 0 and player._played)
    assert player.subtitle_tracks()[1] == 0
    assert player.playbin.get_property("flags") & 4
    ok, pos = player.playbin.query_position(Gst.Format.TIME)
    _run(2)
    ok2, pos2 = player.playbin.query_position(Gst.Format.TIME)
    assert ok and ok2 and pos2 > pos                    # the picture goes on
    player.select_subtitle(-1)
    _run(15, lambda: player.subtitle_tracks()[1] == -1 and player._played)
    assert player.subtitle_tracks() == (["Deutsch"], -1)
    assert not player.playbin.get_property("flags") & 4


def test_new_stream_starts_without_subtitles(player):
    """A teletext page or caption picked on one channel is not switched on by
    itself on the next one."""
    player.ttx_page = 150
    player._sub_choice = ("ttx", 150)
    player._reset_metadata()
    assert player.ttx_page == 0 and player._sub_choice is None


def test_captions_in_the_list_and_off(player):
    """Closed captions are a line of Subtitles like the others: picked there,
    and Off switches them off (before, only a second click on their line did)."""
    pb = player._build()
    if player.cc is None:
        pytest.skip("cc708overlay missing")
    player.playbin = pb
    player.cc.present = True
    player.cc._scanner.services = {1, 2}
    labels, cur = player.subtitle_tracks()
    assert labels == ["Closed captions (CC1)", "Closed captions (service 2)"] and cur == -1
    player.select_subtitle(1)
    assert player.cc.service == 2 and player.subtitle_tracks()[1] == 1
    player.select_subtitle(-1)
    assert player.cc.service == 0 and player.subtitle_tracks()[1] == -1
    assert player.cc.overlay.get_property("silent")
