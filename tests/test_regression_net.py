# SPDX-License-Identifier: GPL-2.0-or-later
"""The regression net for the work on streams, timeshift, subtitles and
extra audio (stage A of the concept "Streams, Timeshift, Untertitel und
Zusatzton").

Two kinds of tests live here:

* What works today and must keep working: plain tests. They pin down the
  behaviour of each reception path, so a fix for one path that changes
  another fails here.
* What is known to be wrong today: ``xfail(strict=True)`` with the
  observation it stands for. Such a test fails as long as the bug is there
  (and is reported as expected); the fix makes it pass, and strict turns
  that into a failure until the marker is removed. So every fix shows up
  here, and nothing gets fixed by accident without being noticed.

The streams come from tests/streamkit.py (made on the fly, served locally).
"""

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

import streamkit as sk
from lindvb import ipstream
from lindvb.media_info import AudioTrack, is_audio_description, short_codec
from lindvb.streaminfo import parse_hls

from test_hls_renditions import _pmt_streams, _run, player  # noqa: F401  (fixture)
from test_ipstream import _read_all



def known_bug(reason):
    """A bug seen on air that is not fixed yet. Only a failed assertion
    counts as the bug: an error of the test itself still fails it."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)


HLS_ELEMENTS = ("videotestsrc", "x264enc", "avenc_aac", "mpegtsmux", "hlssink", "urisourcebin",
                "parsebin", "appsink", "h264parse", "hlsdemux2")


def _served(sample, *a, **kw):
    """(server, folder): a stream sample from streamkit on a local server."""
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    result = sample(folder, *a)
    live = result if isinstance(result, dict) else None
    return sk.Server(folder, live=live, **kw), folder


# -- audio tracks: codec and audio description -------------------------------------------

def _caps_track(caps):
    _GLib, Gst = sk.gst()
    from lindvb.player import Player
    return Player._track_from(None, Gst.Caps.from_string(caps), Gst.TAG_AUDIO_CODEC)


@pytest.mark.parametrize("caps,short", [
    ("audio/x-ac3, channels=6", "AC-3"),
    ("audio/x-eac3, channels=6", "E-AC-3"),
    ("audio/x-opus, channels=2", "Opus"),
    # PBS/WEDQ and DASH: the OSD showed "MPEG" for these
    ("audio/mpeg, mpegversion=4, stream-format=raw, channels=2", "AAC"),
    ("audio/mpeg, mpegversion=2, stream-format=adts, channels=2", "AAC"),
    ("audio/mpeg, mpegversion=1, layer=2, channels=2", "MP2"),
    ("audio/mpeg, mpegversion=1, layer=3, channels=2", "MP3"),
])
def test_codec_from_caps_alone(caps, short):
    """A track whose stream has no codec tag (HLS renditions, DASH, the
    timeshift buffer before the tags arrive) is named from its caps."""
    t = _caps_track(caps)
    assert short_codec(t.codec) == short
    assert t.channels == int(caps.rsplit("=", 1)[1])


@pytest.mark.parametrize("track", [
    AudioTrack(language="qad"),
    AudioTrack(language="deu", title="Audiodeskription"),
    AudioTrack(language="deu", title="Hörfilm"),
    AudioTrack(language="eng", title="English audio description"),
    AudioTrack(language="deu", title="Audio-Deskription"),       # 3sat, with hyphen
    AudioTrack(language="fra", title="Audio-description"),
    AudioTrack(language="deu", title="TV Ton", description=True),   # HLS CHARACTERISTICS
], ids=lambda t: t.title or t.language)
def test_audio_description_is_recognised(track):
    assert is_audio_description(track)


@pytest.mark.parametrize("track", [
    AudioTrack(language="deu", title="TV Ton"), AudioTrack(language="mul", title="Klare Sprache"),
    AudioTrack(language="deu"), AudioTrack(language="eng", title="Original")],
    ids=lambda t: t.title or t.language)
def test_other_sound_is_not_audio_description(track):
    """Klare Sprache and the plain sound must not get the AD icon."""
    assert not is_audio_description(track)


# -- the subtitle and audio menus: renditions the buffer cannot carry -----------------------

def _menu_extras(media, titles, buffered=True, pb3=False):
    """window._missing_renditions on a stand-in window."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from lindvb.ui.window import MainWindow
    url = "https://example.invalid/master.m3u8"
    tuned = types.SimpleNamespace(url=url, buffered=buffered)
    player = types.SimpleNamespace(
        renditions={url: media}, uri="" if buffered else url, _pb3=pb3,
        audio_track_info=lambda: ([AudioTrack(title=t) for t in titles], 0))
    fake = types.SimpleNamespace(_tuned=tuned, capture=object() if buffered else None, player=player)
    return MainWindow._missing_renditions(fake)


def test_hls_characteristics_are_kept():
    media = parse_hls(sk.REDUNDANT_MASTER)["media"]
    ad = [m for m in media if "describes-video" in m["characteristics"]]
    assert [m["name"] for m in ad] == ["Audio-Deskription"] * 2


def test_menu_extras_ard():
    """ARD-alpha (one group): WebVTT is offered next to the buffer; Klare
    Sprache only while the buffer does not hold it yet."""
    from test_hls_renditions import MASTER
    media = parse_hls(MASTER)["media"]
    assert _menu_extras(media, ["Deutsch", "Klare Sprache"]) == (["Deutsch"], [])
    assert _menu_extras(media, []) == (["Deutsch"], ["Klare Sprache"])


@pytest.mark.parametrize("titles", [["TV Ton", "Klare Sprache", "Audio-Deskription"], []],
                         ids=["buffer-has-renditions", "buffer-not-yet"])
def test_menu_extras_redundant_groups(titles):
    """3sat lists each rendition once per CDN copy of the variant: the menus
    offer it once (they offered every choice twice)."""
    media = parse_hls(sk.REDUNDANT_MASTER)["media"]
    subs, audio = _menu_extras(media, titles)
    assert subs == ["Untertitel deutsch"]
    assert audio == ([] if titles else ["Klare Sprache", "Audio-Deskription"])


# -- the 3sat-like stream through the timeshift buffer and directly ----------------------

@pytest.fixture(scope="module")
def redundant():
    sk.gst(*HLS_ELEMENTS)
    srv, folder = _served(sk.hls_redundant)
    _r, data, errors = _read_all(ipstream.GstReader, srv.url("master.m3u8"),
                                 retry=ipstream.Retry(delays=(5,), give_up=30))
    try:
        yield srv, folder, data, errors
    finally:
        srv.close()


def test_buffer_holds_each_rendition_once(redundant):
    """The reader keeps the variant's sound and each audio rendition once,
    on the PID its name gives, although the playlist lists them twice."""
    _srv, _folder, data, errors = redundant
    assert not errors and ipstream.looks_like_ts(data)
    streams = _pmt_streams(data)
    audio = sorted(pid for pid, kind, _l in streams if kind == "audio")
    assert audio == sorted(ipstream.rendition_pid(n) for n in
                           ("TV Ton", "Klare Sprache", "Audio-Deskription"))
    assert [k for _p, k, _l in streams].count("video") == 1


def test_buffer_names_the_renditions(redundant, player):  # noqa: F811
    """From the buffer: each audio rendition by its name, the playlist's
    default (TV Ton) plays."""
    _srv, folder, data, _errors = redundant
    out = folder / "buffer.ts"
    out.write_bytes(data)
    player.renditions[out.as_uri()] = parse_hls(sk.REDUNDANT_MASTER)["media"]
    player.play_uri(out.as_uri())
    _run(5, lambda: len(player.audio_tracks()[0]) == 3 and player.audio_tracks()[1] >= 0
         and player._audio_pref_done)
    labels, cur = player.audio_tracks()
    assert len(labels) == 3, labels
    assert [any(n in lab for lab in labels) for n in ("TV Ton", "Klare Sprache", "Audio-Deskription")] \
        == [True] * 3, labels
    assert "TV Ton" in labels[cur]
    assert player.subtitle_tracks() == ([], -1)
    # CHARACTERISTICS="public.accessibility.describes-video" in the playlist
    ad = [t.title for t in player.audio_track_info()[0] if is_audio_description(t)]
    assert ad == ["Audio-Deskription"]


def test_direct_offers_subtitles_once(redundant, player):  # noqa: F811
    """Played directly (playbin3): the subtitle rendition once, off at the
    start; the three sounds."""
    srv, _folder, _data, _errors = redundant
    player.settings["stream_engine"] = "playbin3"
    player.play_uri(srv.url("master.m3u8"))
    _run(15, lambda: player.subtitle_tracks()[0] and player._played)
    assert player.subtitle_tracks() == (["Untertitel deutsch"], -1)
    assert len(player.audio_tracks()[0]) == 3
    ad = [t.title for t in player.audio_track_info()[0] if is_audio_description(t)]
    assert ad == ["Audio-Deskription"]


# -- a live stream whose server stops answering (3sat, zdf-hls-18) ---------------------------

@known_bug("3sat: the server stops answering, the picture stands still "
           "and LinDVB neither says so nor reconnects")
def test_reader_notices_a_server_that_stops_answering():
    """The buffer reader reports a stream that stops delivering (status or
    error, so the capture reconnects or fails over) within 30 s."""
    GLib, _Gst = sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.live_hls, 60, stall_after=6)
    got, errors, status = [], [], []
    r = ipstream.GstReader(srv.url("main/live.m3u8"), got.append, errors.append,
                           lambda k, m: status.append(k))
    r.start()
    try:
        _run(8)
        assert got, "the stream did not start"
        _run(30, lambda: status or errors)
        assert status or errors
    finally:
        r.close()
        srv.close()


# -- PBS-like WebVTT without time stamps ----------------------------------------------------

def test_live_subtitles_without_timestamps_keep_the_picture(player):  # noqa: F811
    """Live HLS with an empty WebVTT rendition, played with it: the picture
    goes on and nothing is switched off."""
    from gi.repository import Gst
    sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.live_hls_vtt_without_timestamps, 40)
    notices = []
    player.connect("notice", lambda _p, text: notices.append(text))
    try:
        player.play_rendition(srv.url("master.m3u8"), "SUBTITLES", "English")
        _run(15, lambda: player._played)
        ok, pos = player.playbin.query_position(Gst.Format.TIME)
        _run(4)
        ok2, pos2 = player.playbin.query_position(Gst.Format.TIME)
        assert ok and ok2 and pos2 - pos >= 2 * Gst.SECOND
        assert player.subtitle_tracks() == (["English"], 0)
        assert not notices
    finally:
        srv.close()


class _StillPlaybin:
    """Stands in for playbin: the picture stands at 10 s."""

    def query_position(self, _fmt):
        from gi.repository import Gst
        return True, 10 * Gst.SECOND


def _stall(player, monkeypatch):  # noqa: F811
    """The subtitle watchdog seeing a still picture for 6 s, with the PBS
    rendition picked from the start; [pb3 flags of the reopenings]."""
    reopened, notices = [], []
    monkeypatch.setattr(player, "_start_uri", reopened.append)
    player.connect("notice", lambda _p, text: notices.append(text))
    player.playbin = _StillPlaybin()
    player._pb3 = True
    player._want_rendition = ("SUBTITLES", "English")
    player._sub_choice = ("text", "x/text-English")
    try:
        player._watch_subtitle_stall(player._token, 10 * 10**9, 4.0, 3)
    finally:
        player.playbin = None
    return reopened, notices


def test_subtitle_watchdog_switches_them_off_once(player, monkeypatch):  # noqa: F811
    reopened, notices = _stall(player, monkeypatch)
    assert reopened == [True] and len(notices) == 1
    assert player._sub_choice is None


def test_subtitle_watchdog_does_not_pick_them_again(player, monkeypatch):  # noqa: F811
    """PBS/WEDQ: the stream was reopened with the subtitle rendition still
    wanted, so it stalled again and again."""
    _stall(player, monkeypatch)
    assert not (player._want_rendition and player._want_rendition[0] == "SUBTITLES")


# -- CEA-708 closed captions in the picture (US stations) -----------------------------------

def _grab(picture):
    """The pixels the player shows now (RGBA bytes, every 4th the red)."""
    from gi.repository import Gdk
    paintable = picture.get_paintable()
    texture = paintable.get_current_image() if hasattr(paintable, "get_current_image") else paintable
    data, _stride = Gdk.TextureDownloader.new(texture).download_bytes()
    return bytes(data.get_data())[::4]


def _changed(a, b):
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 60)


def test_cea708_captions_start_off_are_drawn_and_go(player):  # noqa: F811
    """A still picture with captions on service 1: offered as CC1, not
    drawn at the start, drawn once picked, gone again with Off."""
    from lindvb import captions
    if not captions.available():
        pytest.skip("cc708overlay missing")
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    sk.make_captioned_segments(folder / "main", 20, pattern="blue")
    srv = sk.Server(folder)
    try:
        player.play_uri(srv.url("main/index.m3u8"), live=True)
        _run(10, lambda: player.has_closed_captions() and player._played)
        assert player.subtitle_tracks() == (["Closed captions (CC1)"], -1)
        _run(1)
        off = _grab(player.video.picture)
        player.select_subtitle(0)
        _run(3)
        on = _grab(player.video.picture)
        player.select_subtitle(-1)
        _run(2)
        off_again = _grab(player.video.picture)
        assert _changed(off, on) > 500                  # the caption window
        assert _changed(off, off_again) < 50
    finally:
        srv.close()


# -- DASH with roles (ARD's DVB-I streams, ONE HD) ------------------------------------------

@pytest.fixture(scope="module")
def dash():
    sk.gst("urisourcebin", "parsebin", "appsink", "mpegtsmux", "dashdemux2", "h264parse")
    srv, folder = _served(sk.dash_with_roles)
    try:
        yield srv, folder
    finally:
        srv.close()


@known_bug("ONE HD: the timeshift buffer keeps only the first DASH audio, "
           "the audio description is lost")
def test_dash_buffer_keeps_every_audio(dash):
    srv, _folder = dash
    _r, data, errors = _read_all(ipstream.GstReader, srv.url("manifest-roles.mpd"),
                                 retry=ipstream.Retry(delays=(5,), give_up=30))
    assert not errors and ipstream.looks_like_ts(data)
    langs = sorted(lang for _p, kind, lang in _pmt_streams(data) if kind == "audio")
    assert langs == ["deu", "eng"]


def _dash_direct(srv, player):  # noqa: F811
    player.settings["stream_engine"] = "playbin3"
    player.play_uri(srv.url("manifest-roles.mpd"))
    _run(10, lambda: player._played and len(player.audio_tracks()[0]) == 2)
    return player.audio_track_info()[0]


def test_dash_direct_lists_both_sounds(dash, player):  # noqa: F811
    srv, _folder = dash
    tracks = _dash_direct(srv, player)
    assert [t.language for t in tracks] == ["deu", "eng"]


@known_bug("ONE HD: Role 'description' in the manifest gives no AD icon")
def test_dash_role_description_is_audio_description(dash, player):  # noqa: F811
    srv, _folder = dash
    tracks = _dash_direct(srv, player)
    assert [is_audio_description(t) for t in tracks] == [False, True]


# -- HbbTV of DVB-I channels ----------------------------------------------------------------

def _hbbtv_channel_changed(capture):
    pytest.importorskip("gi")
    try:
        from lindvb.ui import hbbtv
    except (ImportError, ValueError) as e:
        pytest.skip(f"HbbTV UI unavailable: {e}")
    called = []
    nothing = lambda *a, **kw: None  # noqa: E731
    life = types.SimpleNamespace(autostart=False, active=False, channel_changed=nothing,
                                 no_signalling=lambda: called.append("no signalling"))
    fake = types.SimpleNamespace(
        capture=None, _cap_handler=None, _update_debug=nothing, settings={}, _channel_key=None,
        dsmcc=None, _new_dsmcc=nothing, _sync_broadcast=False, _sync_selector=False, cs=None,
        life=life, enabled=True, _unlocked=set(), _tuned_at=0, _cancel_autostart=nothing,
        view=None, _notify=nothing, _on_ait=nothing, _js=nothing,
        _dvbi_signalling=lambda ch, same: called.append("dvb-i"))
    channel = types.SimpleNamespace(key="one", name="ONE HD", sid=0,
                                    dvbi_apps=[("https://hbbtv.example/one", "html", "")])
    hbbtv.HbbTV.channel_changed(fake, channel, capture)
    return called


def test_hbbtv_dvbi_apps_without_timeshift():
    assert _hbbtv_channel_changed(None) == ["dvb-i"]


@known_bug("ONE HD with timeshift: the DVB-I HbbTV app is not offered "
           "(red button greyed) because the buffer counts as a tuner")
def test_hbbtv_dvbi_apps_with_timeshift():
    capture = types.SimpleNamespace(connect=lambda *a: 1, ait=None, ait_monitor=None)
    assert _hbbtv_channel_changed(capture) == ["dvb-i"]


# -- the channel list ------------------------------------------------------------------------

@known_bug("the search text of one tab stays as a filter on the others")
def test_switching_tabs_clears_the_search():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display")
    from lindvb.ui.window import MainWindow
    nothing = lambda *a, **kw: None  # noqa: E731
    search = Gtk.SearchEntry()
    search.set_text("ard")
    fake = types.SimpleNamespace(
        tab="tv", _mode_sync=False, search=search, settings={}, _list_fav=None, current=None,
        mode_btns={m: Gtk.ToggleButton() for m in ("tv", "radio", "favourites", "recordings")},
        side_stack=types.SimpleNamespace(set_visible_child_name=nothing),
        reload_recordings=nothing, _set_list_mode=nothing, _show_filters=nothing,
        _show_list=nothing, _apply_sort=nothing, _refilter=nothing)
    MainWindow.set_mode(fake, "radio", tune=False)
    assert search.get_text() == ""


# -- where recordings go ---------------------------------------------------------------------

@known_bug("Snap: recordings default to ~/snap/lindvb/<rev>/Videos, "
           "a folder the user does not find")
def test_snap_records_into_the_real_videos_folder(tmp_path):
    real = tmp_path / "home"
    snap_home = real / "snap" / "lindvb" / "42"
    snap_home.mkdir(parents=True)
    env = dict(os.environ, HOME=str(snap_home), SNAP="/snap/lindvb/42", SNAP_REAL_HOME=str(real),
               SNAP_USER_DATA=str(snap_home), XDG_CONFIG_HOME=str(snap_home / ".config"))
    out = subprocess.run(
        [sys.executable, "-c", "from lindvb.config import DEFAULTS; print(DEFAULTS['recording_dir'])"],
        env=env, cwd=Path(__file__).parent.parent, capture_output=True, text=True, check=True)
    folder = Path(out.stdout.strip())
    assert snap_home not in folder.parents and real in folder.parents


def test_recordings_default_to_videos_outside_snap(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SNAP")}
    env["HOME"] = str(tmp_path)
    out = subprocess.run(
        [sys.executable, "-c", "from lindvb.config import DEFAULTS; print(DEFAULTS['recording_dir'])"],
        env=env, cwd=Path(__file__).parent.parent, capture_output=True, text=True, check=True)
    assert Path(out.stdout.strip()) == tmp_path / "Videos" / "LinDVB"



# -- what each package brings ------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def _deb_depends():
    text = (ROOT / "packaging" / "build-deb.sh").read_text()
    return text.split("\nDepends:", 1)[1].split("\nRecommends:", 1)[0]


def _snap_stage_packages():
    text = (ROOT / "snap" / "snapcraft.yaml").read_text()
    return text.split("stage-packages:", 1)[1]


def test_appimage_brings_text_subtitles_and_the_gtk_sink():
    text = (ROOT / "packaging" / "build-appdir.sh").read_text()
    plugins = text.split('PLUGINS="', 1)[1].split('"', 1)[0].split()
    assert "pango" in plugins and "gtk4" in plugins


@known_bug("Snap: text subtitles (WebVTT/TTML) are never drawn, the snap lacks textoverlay")
def test_snap_brings_text_subtitles():
    assert "gstreamer1.0-x" in _snap_stage_packages()


@known_bug(".deb: text subtitles depend on gstreamer1.0-x, which it does not ask for")
def test_deb_brings_text_subtitles():
    assert "gstreamer1.0-x" in _deb_depends()
