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

import json
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

import streamkit as sk
from lindvb import ipstream, sourcestate
from lindvb.media_info import AudioTrack, is_audio_description, short_codec
from lindvb.streaminfo import parse_hls

from test_hls_renditions import _pmt_streams, _run, player  # noqa: F401  (fixture)
from test_ipstream import _read_all



def known_bug(reason):
    """A bug seen on air that is not fixed yet. Only a failed assertion
    counts as the bug: an error of the test itself still fails it."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)


LIVE = "main/live.m3u8"            # the playlist of streamkit.live_hls
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
    stub = types.SimpleNamespace(
        renditions={url: media}, uri="" if buffered else url, _pb3=pb3,
        audio_track_info=lambda: ([AudioTrack(title=t) for t in titles], 0))
    fake = types.SimpleNamespace(_tuned=tuned, capture=object() if buffered else None, player=stub)
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


def test_sound_waits_for_the_playlists_default_track():
    """The sound is chosen once: not before the playlist's default rendition
    is among the tracks (under load it can come after the others)."""
    from lindvb.player import Player
    renditions = [{"name": "TV Ton", "default": True}, {"name": "Klare Sprache"},
                  {"name": "Audio-Deskription"}]
    tracks = [AudioTrack(title="Klare Sprache", codec="AAC"),
              AudioTrack(title="Audio-Deskription", codec="AAC")]
    chosen = []
    fake = types.SimpleNamespace(
        _audio_pref_done=False, playbin=object(), settings={"audio_prefer": "off"},
        audio_track_info=lambda: (list(tracks), 0), _media=lambda kind: renditions,
        select_audio=chosen.append)
    fake._starting_track = lambda *a: Player._starting_track(fake, *a)
    Player._apply_audio_preference(fake)
    assert not fake._audio_pref_done and chosen == []
    tracks.insert(1, AudioTrack(title="TV Ton", codec="AAC"))
    Player._apply_audio_preference(fake)
    assert fake._audio_pref_done and chosen == [1]


def test_buffer_names_the_renditions(redundant, player):  # noqa: F811
    """From the buffer: each audio rendition by its name, the playlist's
    default (TV Ton) plays."""
    _srv, folder, data, _errors = redundant
    out = folder / "buffer.ts"
    out.write_bytes(data)
    player.renditions[out.as_uri()] = parse_hls(sk.REDUNDANT_MASTER)["media"]
    player.play_uri(out.as_uri())
    names = ("TV Ton", "Klare Sprache", "Audio-Deskription")
    # a track is named once its stream has been seen; under load that can be
    # after the third track appeared
    _run(10, lambda: len(player.audio_tracks()[0]) == 3 and player.audio_tracks()[1] >= 0
         and player._audio_pref_done
         and all(any(n in lab for n in names) for lab in player.audio_tracks()[0]))
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


# -- 3sat with every sound a rendition of its own (log of 24.11.2026) ------------------------

THREESAT_SOUNDS = ("TV Ton", "Klare Sprache / Originalton", "Audio-Deskription")


@pytest.fixture(scope="module")
def threesat():
    sk.gst(*HLS_ELEMENTS)
    srv, folder = _served(sk.hls_3sat)
    _r, data, errors = _read_all(ipstream.GstReader, srv.url("master.m3u8"),
                                 retry=ipstream.Retry(delays=(5,), give_up=30))
    try:
        yield srv, folder, data, errors
    finally:
        srv.close()


def test_muxer_error_before_the_first_data_starts_again(monkeypatch):
    """mpegtsmux sets up its streams with its first output and fails ("Could
    not create handler for stream") on a sound whose muxer input was added in
    that moment and has no format yet: the buffer started on the picture, the
    3sat sounds came a few milliseconds later (CI, once). Before the first
    data the reader starts again instead of giving up on the stream."""
    _GLib, Gst = sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.hls_3sat)
    real = ipstream.GstReader._on_parsed_pad
    failed = []

    def parsed_pad(self, parse, pad):
        if not failed and self._mux is None:
            # as the real failure: nothing comes out of this pipeline. Buffer
            # lists too: mpegtsmux pushes only lists, which a BUFFER probe lets
            # through; when the error was handled late (CI load) data had
            # arrived, and the reader rightly did not count a start failure.
            # On the appsink, set before the muxer is made and linked.
            self._sink.get_static_pad("sink").add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                lambda *_a: Gst.PadProbeReturn.DROP)
        real(self, parse, pad)
        mux = self._mux
        if mux is not None and not failed:
            failed.append(mux)
            err = _GLib.Error.new_literal(Gst.StreamError.quark(),
                                          "Could not create handler for stream",
                                          int(Gst.StreamError.MUX))
            mux.post_message(Gst.Message.new_error(mux, err, "gstbasetsmux.c"))
    monkeypatch.setattr(ipstream.GstReader, "_on_parsed_pad", parsed_pad)
    try:
        r, data, errors = _read_all(ipstream.GstReader, srv.url("master.m3u8"),
                                    retry=ipstream.Retry(delays=(5,), give_up=30))
    finally:
        srv.close()
    assert failed, "the muxer error was not raised"
    assert not errors, errors
    assert ipstream.looks_like_ts(data), f"{len(data)} bytes, restarts {r._mux_restarts}"
    assert r._mux_restarts == 1


def test_buffer_holds_the_3sat_sounds(threesat):
    """The three sounds of the playlist, each once on the PID its name gives
    (also the name with a slash), the audio description with its language;
    no fourth sound from the variant itself (the menu showed "Track 1" and
    two unnamed tracks, and no audio description)."""
    _srv, _folder, data, errors = threesat
    assert not errors and ipstream.looks_like_ts(data)
    streams = _pmt_streams(data)
    audio = {pid: lang for pid, kind, lang in streams if kind == "audio"}
    taken = set()
    pids = {}
    for name in THREESAT_SOUNDS:
        pids[name] = ipstream.rendition_pid(name, taken)
        taken.add(pids[name])
    assert sorted(audio) == sorted(pids.values()), [hex(p) for p in audio]
    assert audio[pids["TV Ton"]] in ("deu", "ger")
    assert audio[pids["Audio-Deskription"]] in ("deu", "ger")
    assert [k for _p, k, _l in streams].count("video") == 1


def test_buffer_names_the_3sat_sounds(threesat, player):  # noqa: F811
    """From the buffer: the three sounds by their names, TV Ton plays."""
    _srv, folder, data, _errors = threesat
    out = folder / "buffer.ts"
    out.write_bytes(data)
    player.renditions[out.as_uri()] = parse_hls(sk.THREESAT_MASTER)["media"]
    player.play_uri(out.as_uri())
    _run(10, lambda: len(player.audio_tracks()[0]) >= 3 and player.audio_tracks()[1] >= 0
         and player._audio_pref_done
         and all(any(n in lab for n in THREESAT_SOUNDS) for lab in player.audio_tracks()[0]))
    labels, cur = player.audio_tracks()
    assert len(labels) == 3, labels
    assert [any(n in lab for lab in labels) for n in THREESAT_SOUNDS] == [True] * 3, labels
    assert "TV Ton" in labels[cur]


def test_direct_offers_the_3sat_sounds(threesat, player):  # noqa: F811
    """Played directly (playbin3): the three sounds, the audio description
    among them, TV Ton plays."""
    srv, _folder, _data, _errors = threesat
    player.settings["stream_engine"] = "playbin3"
    player.play_uri(srv.url("master.m3u8"))
    _run(15, lambda: len(player.audio_tracks()[0]) >= 3 and player._played
         and player._audio_pref_done)
    labels, cur = player.audio_tracks()
    assert [any(n in lab for lab in labels) for n in THREESAT_SOUNDS] == [True] * 3, labels
    assert "TV Ton" in labels[cur]


# -- 3sat HD: the buffer's start ends before a picture (logs of 24. and 25.11.2026) ------------

def _parser_error(target):
    """What playbin posted for 3sat HD 35 ms after the start: h264parse ran
    into the end of its stream before a single valid frame."""
    from gi.repository import GLib, Gst
    parser = Gst.ElementFactory.make("h264parse")
    err = GLib.Error.new_literal(Gst.StreamError.quark(),
                                 "No valid frames found before end of stream",
                                 int(Gst.StreamError.FAILED))
    dbg = ("../libs/gst/base/gstbaseparse.c(1423): gst_base_parse_sink_event_default (): "
           "/GstPlayBin:player/GstURIDecodeBin:uridecodebin2/GstDecodeBin:decodebin2/"
           "GstH264Parse:h264parse19")
    target._on_message(None, Gst.Message.new_error(parser, err, dbg))


def test_buffer_start_without_a_picture_starts_again_at_the_live_end(player, monkeypatch):  # noqa: F811
    """Played from the buffer, the video parser ends before its first frame:
    the player starts once more at the live end instead of showing the error;
    a second time on the same channel the error shows."""
    cap = types.SimpleNamespace(adapter=3000)
    starts, errors = [], []
    monkeypatch.setattr(player, "play_capture",
                        lambda c, sid, offset=None: starts.append((c, sid, offset)))
    player.connect("error", lambda _p, text: errors.append(text))
    player.capture, player.sid, player._played = cap, 0, False
    _parser_error(player)
    _run(3, lambda: starts)
    assert starts == [(cap, 0, None)] and errors == []
    _parser_error(player)
    _run(2, lambda: errors)
    assert len(starts) == 1 and errors and "No valid frames" in errors[0]


def test_parser_error_after_the_picture_is_shown(player, monkeypatch):  # noqa: F811
    """Once the channel played, such an error is no start problem: it shows."""
    cap = types.SimpleNamespace(adapter=3000)
    starts, errors = [], []
    monkeypatch.setattr(player, "play_capture",
                        lambda c, sid, offset=None: starts.append((c, sid, offset)))
    player.connect("error", lambda _p, text: errors.append(text))
    player.capture, player.sid, player._played = cap, 0, True
    _parser_error(player)
    _run(2, lambda: errors)
    assert starts == [] and errors


# -- channel changes leave nothing behind (crash of 24.11.2026) ------------------------------

def _open_files():
    return len(os.listdir("/proc/self/fd"))


def test_channel_changes_leave_no_open_files(tmp_path):
    """Debug log of 24.11.2026: after about 40 channel changes on internet
    channels LinDVB ran out of files ("Zu viele offene Dateien", no sound
    device, then the crash). Every released capture stayed in memory with its
    pipelines, sockets and pipes: GStreamer held the methods of the capture
    and its reader, and they held their pipelines."""
    import gc
    import weakref
    GLib, _Gst = sk.gst(*HLS_ELEMENTS)
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("needs /proc")
    from lindvb.channels import Channel
    from lindvb.config import DEFAULTS
    from lindvb.tuner import TunerManager
    srv, _folder = _served(sk.make_av_segments)
    settings = dict(DEFAULTS, timeshift_dir=str(tmp_path), timeshift_max_mb=64)
    tm = TunerManager(settings, probe=lambda a, f: [])

    def change(i):
        cap = tm.acquire(Channel(name=f"Kanal {i}", delsys="iptv",
                                 url=srv.url("index.m3u8")), "live")
        loop = GLib.MainLoop()
        GLib.timeout_add(1500, loop.quit)
        loop.run()
        assert cap.store.bounds()[1] > 0                # it did read the stream
        refs = [weakref.ref(cap), weakref.ref(cap._net.reader)]
        tm.release(cap, "live")
        return refs

    try:
        change(0)                                       # GStreamer's own first-time setup
        gc.collect()
        before = _open_files()
        left = [r for i in range(1, 5) for r in change(i)]
        gc.collect()
        assert [r() for r in left] == [None] * len(left)    # captures and readers are gone
        # each leaked capture kept 8 files; allow for sockets other tests'
        # servers open or close meanwhile, not for one capture
        assert _open_files() - before < 8, (before, _open_files())
    finally:
        tm.shutdown()
        srv.close()


# -- a live stream whose server stops answering (3sat, zdf-hls-18) ---------------------------

def test_reader_notices_a_server_that_stops_answering():
    """3sat: the server stopped answering, the picture stood still and LinDVB
    neither said so nor reconnected. The buffer reader reports a stream that
    stops delivering (status or error, so it reconnects or the capture
    fails over) within 30 s."""
    GLib, _Gst = sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.live_hls, 60, stall_after=6)
    got, errors, status = [], [], []
    r = ipstream.GstReader(srv.url(LIVE), got.append, errors.append,
                           lambda k, m: status.append((k, r.event)))
    r.start()
    try:
        _run(8)
        assert got, "the stream did not start"
        _run(30, lambda: status or errors)
        assert status or errors
        # stage E: the reader names the event, and the state model reconnects
        if status:
            assert status[0] == ("reconnecting", sourcestate.STALLED)
            assert sourcestate.reaction(sourcestate.Situation(
                sourcestate.STALLED, received=True, buffered=True)) == sourcestate.RECONNECT
    finally:
        r.close()
        srv.close()


# -- a CDN that refuses us (403): never the direct path ------------------------------------

def test_refused_stream_in_the_buffer_is_named_refused():
    """ARD's CDNs answer 403 to a client they do not serve. The buffer
    reader says "refused", so the state model goes to another instance or
    says so, and does not play the same address directly (stage E)."""
    GLib, _Gst = sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.live_hls, 20, refuse=403)
    errors = []
    r = ipstream.GstReader(srv.url(LIVE), lambda b: None, errors.append,
                           lambda k, m: None)
    r.start()
    try:
        _run(25, lambda: errors)
        assert errors and not r.received
        assert (r.event, r.gave_up, r.resolved) == (sourcestate.REFUSED, True, True)
        assert "refused the stream (HTTP 403)" in errors[0]
        for left in (0, 1):
            s = sourcestate.Situation(r.event, gave_up=True, buffered=True,
                                      resolved=r.resolved, instances_left=left)
            assert sourcestate.reaction(s) in (sourcestate.RESOLVE, sourcestate.NEXT_INSTANCE,
                                               sourcestate.MESSAGE)
    finally:
        r.close()
        srv.close()


def test_buffer_reader_resolves_a_refusing_gateway():
    """ARD's DVB-I gateway redirects to a CDN that refuses the request the
    HTTP source makes: the buffer reader resolves the redirect itself and
    reads the stream from the CDN, with timeshift (stage E; until then the
    channel was played directly, without timeshift)."""
    GLib, _Gst = sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.live_hls, 30, redirect={"dvbi/live.m3u8": "/" + LIVE},
                           # the reader's own look at the playlist, then the HTTP source
                           refuse_first={LIVE: 2})
    got, errors = [], []
    r = ipstream.GstReader(srv.url("dvbi/live.m3u8"), got.append, errors.append,
                           lambda k, m: None)
    r.start()
    try:
        _run(25, lambda: got or errors)
        assert got and not errors
        assert r.resolved and r.source_uri == srv.url(LIVE)
    finally:
        r.close()
        srv.close()


def test_buffer_reader_asks_again_after_a_one_off_refusal():
    """ONE HD (Alex's log of 24.11.): the CDN refused the buffer once and
    served the very next request. The reader asks once more when its own
    look at the address was served, so the channel keeps its timeshift."""
    GLib, _Gst = sk.gst(*HLS_ELEMENTS)
    # the reader's own look at the playlist, then the HTTP source
    srv, _folder = _served(sk.live_hls, 30, refuse_first={LIVE: 2})
    got, errors = [], []
    r = ipstream.GstReader(srv.url(LIVE), got.append, errors.append, lambda k, m: None)
    r.start()
    try:
        _run(25, lambda: got or errors)
        assert got and not errors and r.resolved
    finally:
        r.close()
        srv.close()


def test_refused_capture_is_not_played_directly():
    """A buffered channel whose stream is refused (after resolving) does not
    fall back to the direct path: the next instance, else the message."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gst", "1.0")
    from lindvb.ui.window import MainWindow
    played, errors = [], []
    for event, direct in ((sourcestate.REFUSED, False), (sourcestate.UNSUPPORTED, True),
                          (sourcestate.NETWORK, True)):
        played.clear()
        errors.clear()
        cap = types.SimpleNamespace(received=0, source_state=lambda e=event: (e, True, True))
        chan = types.SimpleNamespace(name="Das Erste", buffered=True, url="https://x/a.mpd")
        win = types.SimpleNamespace(
            capture=cap, _tuned=chan, _direct_fallback=False, stopped=None,
            _release_capture=lambda: None, quality=types.SimpleNamespace(reset=lambda k: None),
            _update_quality_bar=lambda: None,
            player=types.SimpleNamespace(play_uri=lambda u, live: played.append(u)),
            hbbtv=types.SimpleNamespace(channel_changed=lambda c, cap: None),
            set_status=lambda m: None, _on_play_error=errors.append)
        MainWindow._on_capture_error(win, cap, "itv.ard.de: refused the stream (HTTP 403)")
        assert bool(played) == direct, event
        assert bool(errors) != direct, event


def _window_for_shadow(event, resolved=False, alternatives=()):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gst", "1.0")
    from lindvb.ui.window import MainWindow
    cap = types.SimpleNamespace(received=0,
                                source_state=lambda: (event, resolved, True))
    chan = types.SimpleNamespace(name="Das Erste", buffered=True)
    win = types.SimpleNamespace(capture=cap, _tried=set(), _direct_fallback=False,
                                _failover_alternatives=lambda: list(alternatives))
    return MainWindow, win, cap, chan


def test_shadow_mode_logs_where_the_model_differs(monkeypatch):
    """A buffered channel that never delivered is played directly (except
    for a refusal); the state model does so only for a format the buffer
    cannot take. The difference goes into the debug log, the behaviour
    stays as it is (shadow mode)."""
    from lindvb import debuglog
    lines = []
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append((cat, " ".join(p))))
    cls, win, cap, chan = _window_for_shadow(sourcestate.NETWORK)
    cls._shadow_source_state(win, cap, chan, True)
    assert lines[-1][0] == "State"
    assert "network" in lines[-1][1] and "would message" in lines[-1][1] \
        and "does direct" in lines[-1][1]
    # a refusal: the code follows the model now
    cls, win, cap, chan = _window_for_shadow(sourcestate.REFUSED, resolved=True)
    cls._shadow_source_state(win, cap, chan, False)
    assert lines[-1][1] == "Das Erste: refused – the state model agrees (message)"
    cls, win, cap, chan = _window_for_shadow(sourcestate.UNSUPPORTED)
    cls._shadow_source_state(win, cap, chan, True)
    assert lines[-1][1] == "Das Erste: unsupported – the state model agrees (direct)"
    # a tuner (no reader): nothing to compare
    n = len(lines)
    cls._shadow_source_state(win, types.SimpleNamespace(received=0), chan, True)
    assert len(lines) == n


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


def _old_vtt_path(player, monkeypatch):  # noqa: F811
    """hlsdemux2 plays the WebVTT rendition (the old path), as when the
    video carries no MPEG-TS time."""
    monkeypatch.setattr(player, "_vtt_own", lambda name: False)


def test_debug_log_says_what_flows_with_subtitles_on(player, monkeypatch):  # noqa: F811
    """The old path, debug mode, live HLS with a WebVTT rendition: what leaves the demuxer
    and goes in and out of the subtitle overlay is noted per pad, for the
    line the watchdog writes when the picture stands still (PBS, ARD-alpha)."""
    from lindvb import debuglog
    sk.gst(*HLS_ELEMENTS)
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: None)
    _old_vtt_path(player, monkeypatch)
    srv, _folder = _served(sk.live_hls_vtt_without_timestamps, 40)
    try:
        player.play_rendition(srv.url("master.m3u8"), "SUBTITLES", "English")
        flows = lambda: player._flow.flows if player._flow else {}  # noqa: E731
        _run(15, lambda: player._played and flows().get("overlay out") and
             flows()["overlay out"].buffers > 25)
        names = list(flows())
        assert [n for n in names if n.startswith("demuxer video")], names
        assert [n for n in names if n.startswith("demuxer subtitle")], names
        assert {"overlay picture in", "overlay subtitles in", "overlay out"} <= set(names), names
        assert flows()["overlay out"].buffers > 25 and flows()["overlay out"].last is not None
        line = player._flow.summary()
        assert "demuxer video" in line and "overlay out: " in line, line
    finally:
        srv.close()


def _vtt_shadow_run(player, monkeypatch, sample, until):  # noqa: F811
    """Debug mode, the old path: play *sample*'s "English" rendition until *until(shadow)*
    holds, then stop; (shadow, debug log lines)."""
    from lindvb import debuglog
    sk.gst(*HLS_ELEMENTS)
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append(" ".join(p)))
    _old_vtt_path(player, monkeypatch)
    srv, _folder = _served(sample, 30)
    shadows = []
    try:
        player.play_rendition(srv.url("master.m3u8"), "SUBTITLES", "English")
        _run(25, lambda: player._vtt is not None and (shadows.append(player._vtt[1]) or
                                                      until(player._vtt[1])))
        player._teardown()
    finally:
        srv.close()
    assert shadows, "\n".join(lines)
    assert player._vtt is None
    assert any("WebVTT loader (shadow, English): loading subs/" in ln for ln in lines), lines
    return shadows[-1], lines


def _vtt_report(lines):
    return [ln for ln in lines if ln.startswith("WebVTT loader (shadow, English): playlist")]


def test_webvtt_loader_finds_the_cues_of_the_old_path(player, monkeypatch):  # noqa: F811
    """Timed WebVTT that the old path shows (a playlist with an end): the
    loader loads the same cues and says how far apart the two timelines
    are, which drawing them at the right time needs."""
    clocks = []
    shadow, lines = _vtt_shadow_run(player, monkeypatch, sk.hls_vtt,
                                    lambda sh: len(sh.offsets()) >= 3 and
                                    clocks.append(player._video_clock()) is None)
    offsets = shadow.offsets()
    assert len(offsets) >= 3, (shadow.old, shadow.new)
    assert max(offsets) - min(offsets) < 0.1, offsets
    report = _vtt_report(lines)
    assert report and "of the old path's cues found" in report[-1], lines
    print(report[-1])
    # the picture's clock: cue i is due at stream time i, as the old path has it
    clock = clocks[-1]
    assert clock is not None and clock.offset == pytest.approx(-3600.0, abs=0.001), clock.summary()
    assert clock.matched > 25 and clock.missed == 0, clock.summary()
    assert "video: MPEG-TS time to stream time -3600.000 s" in report[-1]
    against = shadow.offsets(clock)
    assert against and max(abs(x) for x in against) < 0.5, against
    assert "off the picture's" in report[-1], report[-1]
    # a playlist with an end: the loader has its cues before the playback begins
    assert "cues came before the playback had begun" in report[-1], report[-1]


def test_webvtt_loader_beside_a_stalled_live_stream(player, monkeypatch):  # noqa: F811
    """Live, timed WebVTT: hlsdemux2 (1.24) gets its subtitle buffers
    without time and holds picture and sound back, as with PBS and
    ARD-alpha; the old path shows nothing, the loader has the cues. The
    watchdog opens the stream again without subtitles and the loader
    reports."""
    shadow, lines = _vtt_shadow_run(player, monkeypatch, sk.live_hls_vtt,
                                    lambda sh: len(sh.new) >= 3)
    assert len(shadow.new) >= 3 and all(m for _t, _s, m, _pos in shadow.new), shadow.new
    text, start, _mapped, _pos = shadow.new[0]
    n = int(text.split()[1])
    assert text == f"Live {n}" and start == 3600.0 + n
    report = _vtt_report(lines)
    assert report and "with X-TIMESTAMP-MAP" in report[-1], lines
    # the picture stands still here, but its first frames tie the clocks together
    assert "video: MPEG-TS time to stream time -3600.000 s" in report[-1], report[-1]
    print(report[-1])


def test_no_webvtt_shadow_without_debug_mode(player, monkeypatch):  # noqa: F811
    from lindvb import debuglog
    if debuglog.enabled():
        pytest.skip("debug mode is on")
    sk.gst(*HLS_ELEMENTS)
    _old_vtt_path(player, monkeypatch)
    srv, _folder = _served(sk.live_hls_vtt, 30)
    try:
        player.play_rendition(srv.url("master.m3u8"), "SUBTITLES", "English")
        _run(4, lambda: player._sub_choice)
        assert player._sub_choice and player._vtt is None and player._vtt_draw is None
    finally:
        srv.close()


def test_no_video_clock_without_subtitles_or_debug_mode(player):  # noqa: F811
    from lindvb import debuglog
    if debuglog.enabled():
        pytest.skip("debug mode is on")
    sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.live_hls_vtt, 30)
    try:
        player.play_uri(srv.url("master.m3u8"), live=True)
        _run(15, lambda: player._played)
        assert player._ts_clocks == [] and player._vtt_draw is None
    finally:
        srv.close()


# -- HLS WebVTT loaded and drawn by LinDVB (stage F) ----------------------------------------

def _drawn_run(player, sample, until, seconds=30):  # noqa: F811
    """Play *sample*'s "English" rendition until *until(shown)* holds;
    (shown, server): shown is [(text, picture position s)] of what the
    player handed the subtitle label, in order; the caller closes the server."""
    from gi.repository import Gst
    sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sample, seconds)
    shown = []

    def on_text(_p, text):
        sink = getattr(player.video, "sink", None)
        ok, pos = sink.query_position(Gst.Format.TIME) if sink else (False, -1)
        shown.append((text, pos / Gst.SECOND if ok else None))

    player.connect("subtitle-text", on_text)
    try:
        player.play_rendition(srv.url("master.m3u8"), "SUBTITLES", "English")
        _run(25, lambda: until(shown))
    except BaseException:
        srv.close()
        raise
    return shown, srv


def _cue_texts(shown, word):
    return [(int(t.split()[1]), pos) for t, pos in shown if t.startswith(word + " ")]


def test_live_webvtt_drawn_by_lindvb_keeps_the_picture(player):  # noqa: F811
    """Live, timed WebVTT (ARD-alpha, PBS): hlsdemux2 (1.24) would hold
    picture and sound back for it. LinDVB loads and draws the cues itself,
    playbin3 never gets the subtitle track: the picture goes on, each cue
    shows at its time and goes, and the menu shows the rendition on."""
    from gi.repository import Gst
    notices = []
    player.connect("notice", lambda _p, text: notices.append(text))
    shown, srv = _drawn_run(player, sk.live_hls_vtt,
                            lambda sh: len(_cue_texts(sh, "Live")) >= 4)
    try:
        ok, pos = player.playbin.query_position(Gst.Format.TIME)
        _run(3)
        ok2, pos2 = player.playbin.query_position(Gst.Format.TIME)
        assert ok and ok2 and pos2 - pos >= 2 * Gst.SECOND
        _check_live_drawn(player, shown, notices)
    finally:
        srv.close()


def _check_live_drawn(player, shown, notices):  # noqa: F811
    from gi.repository import Gst
    cues = _cue_texts(shown, "Live")
    assert len(cues) >= 4, shown
    # cue n is due at stream time n (X-TIMESTAMP-MAP 3600 s, the video's start)
    for n, at in cues:
        assert at is not None and n - 0.05 <= at < n + 0.5, (n, at, shown)
    assert "" in [t for t, _p in shown[1:]], shown           # each goes after 0.8 s
    assert player.subtitle_tracks() == (["English"], 0)
    assert player._vtt_draw is not None and not notices
    assert not [st for st in player._streams(Gst.StreamType.TEXT)
                if st.get_stream_id() in player._selected]
    # off: the stream opens again without them, nothing is drawn any more
    player.select_subtitle(-1)
    _run(10, lambda: player._played and player._vtt_draw is None)
    assert player._vtt_draw is None and player._sub_choice is None
    assert player.subtitle_tracks()[1] == -1


def test_vod_webvtt_drawn_by_lindvb_at_their_time(player):  # noqa: F811
    """A playlist with an end: the loader has the cues before the playback
    begins; cue i shows at stream time i."""
    shown, srv = _drawn_run(player, sk.hls_vtt, lambda sh: len(_cue_texts(sh, "Cue")) >= 3, 8)
    srv.close()
    cues = _cue_texts(shown, "Cue")
    assert len(cues) >= 3, shown
    for n, at in cues:
        assert at is not None and n - 0.05 <= at < n + 0.5, (n, at, shown)
    assert [n for n, _at in cues] == sorted(n for n, _at in cues)


def test_debug_log_says_what_lindvb_drew_of_the_webvtt(player, monkeypatch):  # noqa: F811
    from lindvb import debuglog
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append(" ".join(p)))
    shown, srv = _drawn_run(player, sk.live_hls_vtt, lambda sh: len(_cue_texts(sh, "Live")) >= 3)
    try:
        assert len(_cue_texts(shown, "Live")) >= 3, shown
        assert player._vtt is None                              # no shadow beside it
        player._teardown()
    finally:
        srv.close()
    assert any(ln.startswith("WebVTT subtitles (LinDVB draws, English): loading subs/")
               for ln in lines), lines
    report = [ln for ln in lines if ln.startswith("WebVTT subtitles (LinDVB draws, English): "
                                                  "playlist")]
    assert report, lines
    line = report[-1]
    print(line)
    assert "video: MPEG-TS time to stream time -3600.000 s" in line, line
    assert "old path" not in line, line
    m = re.search(r"; (\d+) cues drawn, (-?[0-9.]+) s after their time on average "
                  r"\(most (-?[0-9.]+) s\)", line)
    assert m and int(m.group(1)) >= 3 and float(m.group(3)) < 0.5, line


def test_webvtt_without_a_video_clock_goes_to_the_old_path(player, monkeypatch):  # noqa: F811
    """The video carries no MPEG-TS time (no tsdemux, as fragmented MP4
    would): after a while with the picture going on, the stream opens again
    with hlsdemux2 playing the subtitles, and stays so."""
    from gi.repository import Gst
    from lindvb import debuglog
    from lindvb import player as player_mod
    lines = []
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append(" ".join(p)))
    monkeypatch.setattr(player_mod, "VTT_CLOCK_LOOKS", 4)
    monkeypatch.setattr(player, "_video_clock", lambda: None)
    sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.hls_vtt, 8)
    try:
        player.play_rendition(srv.url("master.m3u8"), "SUBTITLES", "English")
        _run(20, lambda: player._vtt_old_path and player._played and [
            st for st in player._streams(Gst.StreamType.TEXT)
            if st.get_stream_id() in player._selected])
        assert player._vtt_old_path and player._vtt_draw is None, lines
        assert any("the video carries no MPEG-TS time to draw them by; hlsdemux2 plays them"
                   in ln for ln in lines), lines
        assert [st for st in player._streams(Gst.StreamType.TEXT)
                if st.get_stream_id() in player._selected]
        assert player.subtitle_tracks() == (["English"], 0)
    finally:
        srv.close()


def test_no_flow_watch_without_debug_mode(player):  # noqa: F811
    from lindvb import debuglog
    if debuglog.enabled():
        pytest.skip("debug mode is on")
    sk.gst(*HLS_ELEMENTS)
    srv, _folder = _served(sk.live_hls_vtt_without_timestamps, 40)
    try:
        player.play_uri(srv.url("master.m3u8"), live=True)
        _run(15, lambda: player._played)
        assert player._flow is None
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


def test_subtitle_watchdog_says_what_flowed(player, monkeypatch):  # noqa: F811
    """Debug mode: at the start of a stall (2 s) and when the watchdog gives
    up (6 s) the debug log says what flowed where."""
    from gi.repository import GLib
    from lindvb import debuglog, flowwatch
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append(" ".join(p)))
    player._flow = flowwatch.FlowWatch(clock=lambda: 50.0)
    flow = player._flow.flows["demuxer video_00"] = flowwatch.Flow()
    flow.buffers, flow.last, flow.at = 120, 70 * 10**9, 44.0
    player._flow.flows["demuxer subtitle_01"] = flowwatch.Flow()
    _stall(player, monkeypatch)
    assert lines[0] == ("the picture stood at 0:00:10.0 for 6 s (running time -): demuxer "
                        "video_00: 120 buffers, last 0:01:10.0 (6.0 s ago); demuxer subtitle_01: "
                        "nothing"), lines
    assert lines[1].startswith("the picture stands still with the subtitles on")
    lines.clear()
    player._sub_choice = ("text", "x/text-English")
    player.playbin = _StillPlaybin()
    monkeypatch.setattr(GLib, "timeout_add", lambda *a: 0)          # no next look
    try:
        player._watch_subtitle_stall(player._token, 10 * 10**9, 0.0, 1)    # still for 2 s
    finally:
        player.playbin = None
    assert len(lines) == 1 and lines[0].startswith(
        "the picture stands at 0:00:10.0 with the subtitles on (running time -): "), lines


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


def _caption_texts(pl):
    """What the window is handed to show as closed captions; nothing of
    them goes to the teletext/subtitle label."""
    texts = []
    pl.connect("caption-text", lambda _p, text: texts.append(text))
    pl.connect("subtitle-text", lambda _p, text: text and texts.append(("subtitle-text", text)))
    return texts


def _drawer_lag(line):
    """The most seconds a caption change was shown after its frame, from the
    debug log line."""
    m = re.search(r"after their frame on average \(most (-?[0-9.]+) s\)", line)
    assert m, line
    return float(m.group(1))


@pytest.mark.parametrize("per_frame", [20, 3])
def test_cea708_captions_start_off_are_drawn_and_go(player, monkeypatch, per_frame):  # noqa: F811
    """Captions on service 1: offered as CC1, not drawn at the start, drawn
    by LinDVB's renderer once picked (over the picture, as teletext
    subtitles), gone again with Off. With 3 triplets a frame a DTVCC packet
    is spread over frames, as PBS WJXT sends them: cc708overlay drew
    nothing of those."""
    from lindvb import captions
    if not captions.available():
        pytest.skip("ccextractor missing")
    monkeypatch.delenv("LINDVB_CC708OVERLAY", raising=False)
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    sk.make_captioned_segments(folder / "main", 20, pattern="blue", per_frame=per_frame)
    srv = sk.Server(folder)
    texts = _caption_texts(player)
    try:
        player.play_uri(srv.url("main/index.m3u8"), live=True)
        _run(10, lambda: player.has_closed_captions() and player._played)
        assert player.subtitle_tracks() == (["Closed captions (CC1)"], -1)
        assert player.cc.overlay is None
        _run(1)
        assert not [t for t in texts if t]
        off = _grab(player.video.picture)
        player.select_subtitle(0)
        assert player.subtitle_tracks()[1] == 0
        _run(4, lambda: len({t for t in texts if t}) >= 2)
        shown = [t for t in texts if t]
        assert len(set(shown)) >= 2 and all(t.startswith("LinDVB 708 ") for t in shown), texts
        n = [int(t.split()[-1]) for t in shown]
        assert n == sorted(n)                           # in order, one line a second
        on = _grab(player.video.picture)
        assert _changed(off, on) < 50                   # nothing blended into the picture
        player.select_subtitle(-1)
        assert texts[-1] == "" and player._cc_draw is None
        _run(1)
        assert texts[-1] == ""
    finally:
        srv.close()


def test_cea608_captions_alone_are_offered_and_drawn(player, monkeypatch):  # noqa: F811
    """A stream with CEA-608 captions and no CEA-708 (older encoders, some
    IPTV): CC1 is offered, LinDVB's CEA-608 renderer draws it over the
    picture once picked, and it goes again with Off. The debug log says what
    was drawn."""
    from lindvb import captions, debuglog
    if not captions.available():
        pytest.skip("ccextractor missing")
    monkeypatch.delenv("LINDVB_CC708OVERLAY", raising=False)
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append(" ".join(p)))
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    sk.make_captioned_segments(folder / "main", 20, pattern="blue", text=None, cc608="LinDVB 608")
    srv = sk.Server(folder)
    texts = _caption_texts(player)
    try:
        player.play_uri(srv.url("main/index.m3u8"), live=True)
        _run(10, lambda: player.has_closed_captions() and player._played)
        assert player.cc.kinds == {"608"}
        assert player.subtitle_tracks() == (["Closed captions (CC1)"], -1)
        _run(1)
        assert not [t for t in texts if t]
        player.select_subtitle(0)
        assert player.subtitle_tracks()[1] == 0 and player._cc_shadow is None
        _run(6, lambda: len({t.split("\n")[-1] for t in texts if t.endswith(("2", "3", "4"))}) >= 2)
        # roll-up: the bottom row grows a pair at a time, one line a second
        shown = [t.split("\n")[-1] for t in texts if t]
        assert all("LinDVB 608".startswith(t) or t.startswith("LinDVB 608 ") for t in shown), texts
        assert len({t for t in shown if t.startswith("LinDVB 608 ")}) >= 2, texts
        player.select_subtitle(-1)
        assert texts[-1] == "" and player._cc_draw is None
    finally:
        srv.close()
    assert any("CEA-608 caption channels offered: CC1" in ln for ln in lines), lines
    drawn = [ln for ln in lines if ln.startswith("CEA-608 renderer (CC1): ")]
    assert len(drawn) == 1 and "captions drawn" in drawn[0] and "dropped 0" in drawn[0], lines
    assert "thrown away: 0 with more than 300 waiting, 0 as a time went back" in drawn[0], drawn
    assert _drawer_lag(drawn[0]) < 0.5, drawn


def test_cea708_captions_by_the_old_path_when_asked(player, monkeypatch):  # noqa: F811
    """LINDVB_CC708OVERLAY=1: cc708overlay blends them into the picture, as
    before stage F, and LinDVB's renderer draws nothing."""
    from lindvb import captions
    monkeypatch.setenv("LINDVB_CC708OVERLAY", "1")
    if not captions.old_path():
        pytest.skip("cc708overlay missing")
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    sk.make_captioned_segments(folder / "main", 20, pattern="blue")
    srv = sk.Server(folder)
    texts = _caption_texts(player)
    try:
        player.play_uri(srv.url("main/index.m3u8"), live=True)
        _run(10, lambda: player.has_closed_captions() and player._played)
        assert player.cc.overlay is not None
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
        assert player._cc_draw is None and not [t for t in texts if t]
    finally:
        srv.close()


def test_channel_analysis_counts_the_captions(player):  # noqa: F811
    """PBS WJXT: "the channel sends nothing" and "LinDVB draws nothing"
    looked the same. While the channel analysis measures, the captions in
    the H.264 stream are counted per service, with the text in them; not
    measuring, nothing is counted."""
    from lindvb import captions
    if not captions.available():
        pytest.skip("ccextractor missing")
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    sk.make_captioned_segments(folder / "main", 8, pattern="blue")
    srv = sk.Server(folder)
    try:
        player.play_uri(srv.url("main/index.m3u8"), live=True)
        _run(10, lambda: player.has_closed_captions() and player._played)
        assert player.caption_info() is None
        player.measure_captions = True
        _run(10, lambda: (player.caption_info() or {}).get("708", {}).get(1, (0, 0))[1] >= 24)
        info = player.caption_info()
        assert info["seen"] and info["708"][1][1] >= 24             # "LinDVB 708 n", twice
        assert info["608"] == {}
        player.measure_captions = False
        assert player.caption_info() is None
    finally:
        player.measure_captions = False
        srv.close()


def test_cea608_renderer_in_the_shadow(player, monkeypatch):  # noqa: F811
    """Stage F, debug mode: closed captions picked (cc708overlay draws
    service 1), the CEA-608 renderer decodes CC1 of the H.264 stream beside
    it, off the video thread, and the debug log says what it decoded."""
    from lindvb import captions, debuglog
    if not captions.available():
        pytest.skip("ccextractor missing")
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append((cat, " ".join(p))))
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    sk.make_captioned_segments(folder / "main", 8, pattern="blue", cc608="LinDVB 608")
    srv = sk.Server(folder)
    try:
        player.play_uri(srv.url("main/index.m3u8"), live=True)
        _run(10, lambda: player.has_closed_captions() and player._played)
        assert player._cc_shadow is None                    # not picked: nothing decoded
        player.select_subtitle(0)
        r = _cc_shadow_renderer(player, "CEA-608")
        # roll-up: the line grows a pair at a time, one line a second
        _run(10, lambda: r.state().last.endswith(("608 2", "608 3", "608 4")))
        assert r.state().last.split("\n")[-1].startswith("LinDVB 608 "), r.state()
        player.select_subtitle(-1)
        assert player._cc_shadow is None
    finally:
        srv.close()
    reports = [t for c, t in lines if t.startswith("CEA-608 renderer (shadow, CC1)")]
    assert len(reports) == 1, lines
    assert "captions decoded" in reports[0] and "'LinDVB 608 " in reports[0], reports
    assert "dropped 0" in reports[0], reports


def test_debug_log_says_what_the_caption_renderer_drew(player, monkeypatch):  # noqa: F811
    """Debug mode: LinDVB's CEA-708 renderer draws service 1; when the
    captions go off the log says what it drew. In the shadow only the
    CEA-608 renderer runs then (no CEA-608 here: it reports nothing)."""
    from lindvb import captions, debuglog
    if not captions.available():
        pytest.skip("ccextractor missing")
    monkeypatch.delenv("LINDVB_CC708OVERLAY", raising=False)
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append((cat, " ".join(p))))
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    sk.make_captioned_segments(folder / "main", 8, pattern="blue", per_frame=3)
    srv = sk.Server(folder)
    try:
        player.play_uri(srv.url("main/index.m3u8"), live=True)
        _run(10, lambda: player.has_closed_captions() and player._played)
        player.select_subtitle(0)
        r = player._cc_draw[0]
        _cc, shadows = player._cc_shadow
        assert [sr.kind for sr, _cmp in shadows] == ["CEA-608"]
        _run(10, lambda: r.state().decoded >= 2)
        player.select_subtitle(-1)
    finally:
        srv.close()
    drawn = [t for c, t in lines if t.startswith("CEA-708 renderer (service 1)")]
    assert len(drawn) == 1 and "captions drawn" in drawn[0], lines
    assert "'LinDVB 708 " in drawn[0] and "dropped 0" in drawn[0], drawn
    # the drawer: each change with its frame (within a look, 0.1 s), nothing thrown away
    assert "; drawer: " in drawn[0] and _drawer_lag(drawn[0]) < 0.5, drawn
    assert "thrown away: 0 with more than 300 waiting, 0 as a time went back; " \
           "0 shown without a time" in drawn[0], drawn
    print(drawn[0])
    assert not [t for c, t in lines if t.startswith(("CEA-708 renderer (shadow", "CEA-608 renderer"))]


def _cc_shadow_renderer(pl, kind):
    _cc, shadows = pl._cc_shadow
    return next(r for r, _cmp in shadows if r.kind == kind)


def test_cea708_renderer_in_the_shadow(player, monkeypatch):  # noqa: F811
    """Stage F, debug mode, the old path asked for: closed captions picked,
    the CEA-708 renderer decodes service 1 beside cc708overlay, off the
    video thread, and the debug log says what it decoded. No CEA-608 in the
    stream: its renderer reports nothing."""
    from lindvb import captions, debuglog
    monkeypatch.setenv("LINDVB_CC708OVERLAY", "1")
    if not captions.old_path():
        pytest.skip("cc708overlay missing")
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append((cat, " ".join(p))))
    folder = Path(tempfile.mkdtemp(prefix="lindvb-net-"))
    sk.make_captioned_segments(folder / "main", 8, pattern="blue")
    srv = sk.Server(folder)
    try:
        player.play_uri(srv.url("main/index.m3u8"), live=True)
        _run(10, lambda: player.has_closed_captions() and player._played)
        player.select_subtitle(0)
        r = _cc_shadow_renderer(player, "CEA-708")
        _run(10, lambda: r.state().decoded >= 2)
        assert r.state().last.startswith("LinDVB 708 "), r.state()
        player.select_subtitle(-1)
        assert player._cc_shadow is None
    finally:
        srv.close()
    reports = [t for c, t in lines if t.startswith("CEA-708 renderer (shadow, service 1)")]
    assert len(reports) == 1, lines
    assert "captions decoded" in reports[0] and "'LinDVB 708 " in reports[0], reports
    assert "dropped 0" in reports[0], reports
    assert not [t for c, t in lines if t.startswith("CEA-608 renderer")], lines


# -- DASH with roles (ARD's DVB-I streams, ONE HD) ------------------------------------------

@pytest.fixture(scope="module")
def dash():
    sk.gst("urisourcebin", "parsebin", "appsink", "mpegtsmux", "dashdemux2", "h264parse")
    srv, folder = _served(sk.dash_with_roles)
    try:
        yield srv, folder
    finally:
        srv.close()


def test_dash_buffer_keeps_every_audio(dash):
    """ONE HD: the timeshift buffer kept only the first DASH audio, the
    audio description was lost."""
    srv, _folder = dash
    _r, data, errors = _read_all(ipstream.GstReader, srv.url("manifest-roles.mpd"),
                                 retry=ipstream.Retry(delays=(5,), give_up=30))
    assert not errors and ipstream.looks_like_ts(data)
    langs = sorted(lang for _p, kind, lang in _pmt_streams(data) if kind == "audio")
    assert langs == ["deu", "eng"]


def test_dash_language_with_a_stray_character(dash, tmp_path):
    """NDR's DASH audio names its language "de`" in the MP4 header (log of
    25.11.2026): mpegtsmux knows no such code and warned about it dozens of
    times at every start. The reader hands it the German it means."""
    from gi.repository import Gst
    srv, folder = dash
    ndr = tmp_path / "ndr"
    ndr.mkdir()
    for f in folder.iterdir():
        if f.is_file():
            (ndr / f.name).write_bytes(f.read_bytes())
    init = bytearray((ndr / "init-stream1.m4s").read_bytes())     # the German set
    at = init.find(b"mdhd") + 8 + 16                                # version 0: the language
    init[at:at + 2] = ((4 << 10) | (5 << 5) | 0).to_bytes(2, "big")  # "de`"
    (ndr / "init-stream1.m4s").write_bytes(bytes(init))
    warned = []

    def log(category, _level, _file, _function, _line, _obj, message, *_user):
        if category.get_name() == "basetsmux":
            warned.append(message.get())
    Gst.debug_add_log_function(log, None)
    old = Gst.debug_get_default_threshold()
    Gst.debug_set_threshold_for_name("basetsmux", Gst.DebugLevel.WARNING)
    Gst.debug_set_active(True)
    served = sk.Server(ndr)
    try:
        _r, data, errors = _read_all(ipstream.GstReader, served.url("manifest-roles.mpd"),
                                     retry=ipstream.Retry(delays=(5,), give_up=30))
    finally:
        served.close()
        Gst.debug_remove_log_function(log)
        Gst.debug_set_threshold_for_name("basetsmux", old)
    assert not errors and ipstream.looks_like_ts(data)
    assert not [w for w in warned if "language" in w], warned[:3]
    langs = sorted(lang for _p, kind, lang in _pmt_streams(data) if kind == "audio")
    assert langs in (["deu", "eng"], ["eng", "ger"]), langs


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

def _hbbtv_channel_changed(capture, iptv=True):
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
        _dvbi_signalling=lambda *a: called.append("dvb-i"))
    channel = types.SimpleNamespace(key="one", name="ONE HD", sid=0, is_iptv=iptv,
                                    dvbi_apps=[("https://hbbtv.example/one", "html", "")])
    hbbtv.HbbTV.channel_changed(fake, channel, capture)
    return called


def test_hbbtv_dvbi_apps_without_timeshift():
    assert _hbbtv_channel_changed(None) == ["dvb-i"]


def _capture():
    """A stream capture: it watches the stream for an AIT of its own."""
    return types.SimpleNamespace(connect=lambda *a: 1, ait=None, ait_monitor=object())


def test_hbbtv_dvbi_apps_with_timeshift():
    """ONE HD with timeshift: the DVB-I HbbTV app was not offered (red
    button greyed) because the buffer counted as a tuner."""
    assert _hbbtv_channel_changed(_capture()) == ["dvb-i"]


def test_hbbtv_of_a_tuner_channel_comes_from_the_broadcast():
    """A DVB-T/S/C channel of a DVB-I list keeps the AIT of its broadcast."""
    assert _hbbtv_channel_changed(_capture(), iptv=False) == []


# -- the channel list ------------------------------------------------------------------------

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


def _defaults(env, *keys):
    code = "from lindvb.config import DEFAULTS; print(*(DEFAULTS[k] for k in %r), sep='\\n')" % (keys,)
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return [Path(line) for line in out.stdout.splitlines()]


def test_recordings_follow_the_desktops_own_videos_folder(tmp_path):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "user-dirs.dirs").write_text(
        '# written by xdg-user-dirs-update\nXDG_VIDEOS_DIR="$HOME/Vidéos"\n'
        'XDG_PICTURES_DIR="/data/Bilder"\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SNAP", "XDG_"))}
    env["HOME"] = str(tmp_path)
    assert _defaults(env, "recording_dir", "screenshot_dir") == [
        tmp_path / "Vidéos" / "LinDVB", Path("/data/Bilder/LinDVB")]


def test_a_switched_off_videos_folder_falls_back_to_videos(tmp_path):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "user-dirs.dirs").write_text('XDG_VIDEOS_DIR="$HOME/"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SNAP", "XDG_"))}
    env["HOME"] = str(tmp_path)
    assert _defaults(env, "recording_dir") == [tmp_path / "Videos" / "LinDVB"]


def _snap_env(tmp_path, rev="43"):
    real = tmp_path / "home"
    snap_home = real / "snap" / "lindvb" / rev
    (snap_home / ".config" / "lindvb").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SNAP", "XDG_"))}
    env.update(HOME=str(snap_home), SNAP="/snap/lindvb/" + rev, SNAP_REAL_HOME=str(real),
               SNAP_USER_DATA=str(snap_home), SNAP_INSTANCE_NAME="lindvb",
               XDG_CONFIG_HOME=str(snap_home / ".config"))
    return env, real, snap_home


def _settings(env, *keys):
    code = ("from lindvb.config import Settings; s = Settings(); "
            "print(*(s[k] for k in %r), sep='\\n')" % (keys,))
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return [Path(line) for line in out.stdout.splitlines()]


def test_snap_moves_the_old_revision_default_to_the_real_home(tmp_path):
    env, real, snap_home = _snap_env(tmp_path)
    old = real / "snap" / "lindvb" / "42"
    (snap_home / ".config" / "lindvb" / "settings.json").write_text(json.dumps({
        "recording_dir": str(old / "Videos" / "LinDVB"),
        "screenshot_dir": str(old / "Pictures" / "LinDVB")}))
    assert _settings(env, "recording_dir", "screenshot_dir") == [
        real / "Videos" / "LinDVB", real / "Pictures" / "LinDVB"]


def test_snap_keeps_a_folder_the_user_chose(tmp_path):
    env, real, snap_home = _snap_env(tmp_path)
    (snap_home / ".config" / "lindvb" / "settings.json").write_text(json.dumps({
        "recording_dir": "/media/disk/TV", "screenshot_dir": str(real / "snap" / "x")}))
    assert _settings(env, "recording_dir", "screenshot_dir") == [
        Path("/media/disk/TV"), real / "snap" / "x"]


def test_settings_outside_the_snap_are_left_alone(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SNAP", "XDG_"))}
    env["HOME"] = str(tmp_path)
    saved = str(tmp_path / "snap" / "lindvb" / "42" / "Videos" / "LinDVB")
    (tmp_path / ".config" / "lindvb").mkdir(parents=True)
    (tmp_path / ".config" / "lindvb" / "settings.json").write_text(
        json.dumps({"recording_dir": saved}))
    assert _settings(env, "recording_dir") == [Path(saved)]


def test_snap_recordings_list_still_shows_the_old_folder(tmp_path, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    env, real, snap_home = _snap_env(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (snap_home / "Videos" / "LinDVB").mkdir(parents=True)
    from lindvb.ui.window import MainWindow
    fake = types.SimpleNamespace(settings={"recording_dir": str(real / "Videos" / "LinDVB")},
                                 app=types.SimpleNamespace(timers=types.SimpleNamespace(timers=[])))
    assert MainWindow._recording_folders(fake) == [
        str(real / "Videos" / "LinDVB"), str(snap_home / "Videos" / "LinDVB")]



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


def test_snap_brings_text_subtitles():
    """Snap: text subtitles (WebVTT/TTML) were never drawn, it lacked textoverlay."""
    assert "gstreamer1.0-x" in _snap_stage_packages()


def test_deb_brings_text_subtitles():
    """.deb: text subtitles need gstreamer1.0-x, which it did not ask for."""
    assert "gstreamer1.0-x" in _deb_depends()


def test_appimage_build_has_text_subtitles_to_bundle():
    """The AppImage bundles the pango plugin only if the build machine has it
    (the AppImage of 0.21 had no textoverlay)."""
    text = (ROOT / ".github" / "workflows" / "appimage.yml").read_text()
    assert "gstreamer1.0-x" in text.split("Install build dependencies", 1)[1].split("- name:", 1)[0]


def test_direct_starts_with_the_main_sound(redundant, player):  # noqa: F811
    """3sat played directly: the sound is chosen, and it is the German TV
    sound, not the audio description or the easy-to-understand one."""
    srv, _folder, _data, _errors = redundant
    player.settings["stream_engine"] = "playbin3"
    player.play_uri(srv.url("master.m3u8"))
    _run(15, lambda: len(player.audio_tracks()[0]) == 3 and player.audio_tracks()[1] >= 0
         and player._audio_pref_done)
    tracks, cur = player.audio_track_info()
    assert player._audio_pref_done
    assert 0 <= cur < len(tracks) and not is_audio_description(tracks[cur]), tracks
    assert tracks[cur].title == "TV Ton", tracks


def test_direct_skips_audio_description_marked_default(redundant, player):  # noqa: F811
    """A playlist that starts with the audio description (DEFAULT=YES):
    the German TV sound plays instead (3sat over IPTV)."""
    srv, folder, _data, _errors = redundant
    master = sk.REDUNDANT_MASTER.replace('NAME="TV Ton",LANGUAGE="deu",DEFAULT=YES',
                                         'NAME="TV Ton",LANGUAGE="deu",DEFAULT=NO')
    master = master.replace('describes-video",DEFAULT=NO', 'describes-video",DEFAULT=YES')
    (folder / "ad_first.m3u8").write_text(master)
    player.settings["stream_engine"] = "playbin3"
    player.play_uri(srv.url("ad_first.m3u8"))
    _run(15, lambda: player._audio_pref_done and player.audio_track_info()[1] >= 0 and
         not is_audio_description(player.audio_track_info()[0][player.audio_track_info()[1]]))
    tracks, cur = player.audio_track_info()
    assert player._audio_pref_done
    assert tracks[cur].title == "TV Ton", (tracks, cur)


def test_sound_in_the_interface_language(monkeypatch):
    """ONE over DVB-I: the series started with its original English sound.
    With German as the interface language the German sound plays; set to
    "as broadcast" the first track stays."""
    from lindvb import i18n
    from lindvb.player import Player
    monkeypatch.setattr(i18n, "current", "de")
    tracks = [AudioTrack(language="eng", codec="AAC"), AudioTrack(language="deu", codec="AAC")]
    for setting, want in (("auto", [1]), ("", []), ("eng", [])):
        chosen = []
        fake = types.SimpleNamespace(
            _audio_pref_done=False, playbin=object(),
            settings={"audio_prefer": "off", "audio_language": setting},
            audio_track_info=lambda: (list(tracks), 0), _media=lambda kind: [],
            select_audio=chosen.append)
        fake._starting_track = lambda *a, f=fake: Player._starting_track(f, *a)
        Player._apply_audio_preference(fake)
        assert chosen == want, setting


def test_catalog_agrees_with_the_menus(redundant, player, monkeypatch):  # noqa: F811
    """Stage D shadow mode on the 3sat-like stream, from the buffer and
    played directly: the track catalog and the menus agree."""
    from lindvb import debuglog
    from lindvb.player import Player
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append((cat, " ".join(p))))
    srv, folder, data, _errors = redundant
    out = folder / "buffer.ts"
    out.write_bytes(data)
    player.renditions[out.as_uri()] = parse_hls(sk.REDUNDANT_MASTER)["media"]
    player.play_uri(out.as_uri())
    _run(10, lambda: len(player.audio_tracks()[0]) == 3 and player._audio_pref_done
         and player._pmt_raw)
    Player._shadow_catalog(player, player._token)
    player.settings["stream_engine"] = "playbin3"
    player.play_uri(srv.url("master.m3u8"))
    _run(15, lambda: player.subtitle_tracks()[0] and len(player.audio_tracks()[0]) == 3)
    Player._shadow_catalog(player, player._token)
    tracks = [text for cat, text in lines if cat == "Tracks"]
    assert sum(text.startswith("catalog:") for text in tracks) == 2, tracks
    assert not [t for t in tracks if "differs" in t], tracks
