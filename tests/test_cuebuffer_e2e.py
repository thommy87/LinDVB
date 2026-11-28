# SPDX-License-Identifier: GPL-2.0-or-later
"""Stage H from the stream to the picture: an HLS channel with a WebVTT
rendition goes through the timeshift buffer of an internet capture, the
reader writes the cues into the buffer (lindvb.cuebuffer), the player finds
them as it feeds the buffer and draws them at their time (lindvb.vttdraw)."""

import tempfile
import time
from pathlib import Path

import pytest

import streamkit as sk

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject, Gst, Gtk  # noqa: E402

Gst.init(None)

HLS_ELEMENTS = ("videotestsrc", "x264enc", "avenc_aac", "mpegtsmux", "hlssink", "urisourcebin",
                "parsebin", "appsink", "h264parse", "hlsdemux2", "tsdemux")


def _run(seconds, until=None):
    loop = GLib.MainLoop()
    end = time.monotonic() + seconds

    def tick():
        if time.monotonic() > end or (until is not None and until()):
            loop.quit()
            return False
        return True
    GLib.timeout_add(100, tick)
    loop.run()


class _Tuners(GObject.Object):
    def __init__(self):
        super().__init__()
        self.captures = {}


@pytest.fixture
def channel(tmp_path):
    """play(subtitles_in_buffer) -> (player, capture, shown): the live
    sample (cue n "Live n" due at the segments' second n, for 0.8 s) on a
    local server, read into a capture and played from it; *shown* fills
    with (text, picture position s) of what the player hands the label."""
    if not Gtk.init_check():
        pytest.skip("no display")
    sk.gst(*HLS_ELEMENTS)
    from lindvb.channels import Channel
    from lindvb.config import DEFAULTS
    from lindvb.player import Player
    folder = Path(tempfile.mkdtemp(prefix="lindvb-cues-"))
    srv = sk.Server(folder, live=sk.live_hls_vtt(folder, 40))
    made = []

    def play(on, path="master.m3u8"):
        settings = dict(DEFAULTS, deinterlace=False, video_decoder="software",
                        timeshift_dir=str(tmp_path), stream_engine="playbin",
                        subtitles_in_buffer=on)
        player = Player(Gtk.Picture(), settings)
        shown = []

        def on_text(_p, text):
            sink = getattr(player.video, "sink", None)
            ok, pos = sink.query_position(Gst.Format.TIME) if sink else (False, -1)
            shown.append((text, pos / Gst.SECOND if ok else None))
        player.connect("subtitle-text", on_text)
        ch = Channel(name="Test HLS", delsys="iptv", url=srv.url(path))
        assert ch.buffered
        from lindvb import ipstream
        from lindvb.tuner import Capture
        cap = Capture(ch, 901, 0, settings, tuner=ipstream.stream_profile(901, ch), live=True)
        cap.users.add("live")
        cap.start()
        made.append((player, cap))
        _run(20, lambda: cap.store.bounds()[1] > 200_000)
        player.play_capture(cap, ch.sid)
        return player, cap, shown
    yield play
    for player, cap in made:
        player.stop()
        cap.destroy()
    srv.close()


def _cues(shown):
    """[(n, picture position s)] of the cues "Live n" drawn (one drawn
    before the picture had a position is left out)."""
    return [(int(t.split()[1]), pos) for t, pos in shown
            if t.startswith("Live ") and pos is not None]


def _english(player):
    labels, _cur = player.subtitle_tracks()
    return labels.index("English") if "English" in labels else None


LATE = 0.5      # s a cue may show after its time (the drawer's, as in the tests of #239)


def _state(player, cap, shown):
    """What the reader and the player had, for a failed assertion."""
    bc = player._bcues
    draw = bc.draw if bc is not None else None
    clock = player._video_clock()
    reader = cap._net.reader
    return {"shown": shown, "position": player._position_s(),
            "found": bc.found if bc else None,
            "waiting": len(draw.pending) if draw else None,
            "drawn": draw.drawn if draw else None, "missed": draw.missed if draw else None,
            "player clock": clock.summary() if clock else None,
            "reader": reader.cues.report() if reader.cues else None}


def _check_times(player, cap, shown, epoch=0, last=None):
    """Each cue "Live n" in *shown* (the *last* ones) shows at the picture
    of its time: the segments' second n is the buffer's time the reader's
    clock gives it, which is the position the player's clock gives that.
    One already running when the picture started shows right then."""
    cues = _cues(shown)[-last:] if last else _cues(shown)
    assert len(cues) >= 3, _state(player, cap, shown)
    writer, clock = cap._net.reader.cues, player._video_clock()
    start = min(pos for _t, pos in shown if pos is not None)
    for n, at in cues:
        due = clock.stream_time(writer.clock.stream_time(3600 + n), at, epoch)
        if due < start:
            assert at < start + LATE, (n, at, due, _state(player, cap, shown))
        else:
            assert due - 0.05 <= at < due + LATE, (n, at, due, _state(player, cap, shown))
    return cues


def test_subtitles_from_the_buffer_at_their_time(channel):
    player, cap, shown = channel(True)
    _run(20, lambda: _english(player) is not None)
    i = _english(player)
    assert i is not None, player.subtitle_tracks()
    assert player.buffer_subtitles() == ["English"]
    writer, clock = cap._net.reader.cues, player._video_clock()
    _run(3)
    writer.behind = 0.0         # from here on: the start (the loaders read first) is over
    player.select_subtitle(i)
    _run(20, lambda: len(_cues(shown)) >= 4)
    _check_times(player, cap, shown)
    clock = player._video_clock()
    assert "" in [t for t, _p in shown[1:]], shown                 # each goes after 0.8 s
    assert player.subtitle_tracks()[1] == i
    # woken by the picture of each new segment, the loader had its cues,
    # and they went in at once, not with the next segment (without: up to
    # 2 s after the picture came under load, and cues were lost; 28.11.2026).
    # The sample's video segments end up to 0.1 s after the second their
    # subtitle segment covers, so a cue may go in up to one segment (1 s)
    # after the first frames of its second came, not later.
    assert writer.between >= 1 and writer.kicks >= 4, writer.report()
    assert writer.behind < 1.2, writer.report()
    # the reader's clock and the player's held on every frame
    assert writer.written >= 4 and writer.clock.missed == 0, writer.report()
    assert clock is not None and clock.missed == 0, clock.summary()
    # still the capture, with timeshift: nothing opened the stream directly
    assert player.capture is cap and player.can_timeshift()
    # off: nothing drawn any more
    player.select_subtitle(-1)
    n = len(shown)
    _run(3)
    assert not _cues(shown[n:]) and player.subtitle_tracks()[1] == -1


def test_jump_back_shows_the_cues_again(channel):
    player, cap, shown = channel(True)
    _run(20, lambda: _english(player) is not None)
    player.select_subtitle(_english(player))
    _run(25, lambda: len(_cues(shown)) >= 6)
    seen = [n for n, _at in _cues(shown)]
    k = len(shown)
    player.jump(-5)
    _run(20, lambda: len(_cues(shown[k:])) >= 3)
    again = _check_times(player, cap, shown[k:])
    # earlier ones come once more (how far back depends on the byte rate
    # the jump is worked out with)
    assert again[0][0] < seen[-1] - 1, (seen, again)
    assert player.subtitle_tracks()[1] == _english(player)


def test_reconnect_keeps_the_subtitles_at_their_time(channel):
    """The reader connects again: its new muxer starts its PTS again, the
    buffer gets a new stretch (tsclock epoch) and the cues after it still
    show at their time."""
    player, cap, shown = channel(True)
    _run(20, lambda: _english(player) is not None)
    player.select_subtitle(_english(player))
    _run(20, lambda: len(_cues(shown)) >= 3)
    from lindvb import sourcestate
    reader = cap._net.reader
    GLib.idle_add(lambda: reader._broken(reader._gen, "test", sourcestate.NETWORK))
    k = len(shown)
    _run(30, lambda: len(_cues(shown[k:])) >= 6)
    after = _cues(shown[k:])
    clock = player._video_clock()
    assert clock is not None and clock.epoch >= 1, clock.summary()
    _check_times(player, cap, shown[k:], clock.epoch, last=4)
    assert len(after) >= 6, (after, clock.summary(), reader.cues.report())


def test_off_by_default_the_buffer_has_no_subtitles(channel):
    player, cap, shown = channel(False)
    _run(8)
    assert cap.cue_pids == {} and player.buffer_subtitles() == []
    assert "English" not in player.subtitle_tracks()[0]
    assert getattr(cap._net.reader, "cues", None) is None
    assert player._bcues is None and not _cues(shown)


def test_a_channel_without_subtitle_renditions_costs_nothing(channel):
    """Switched on, but the playlist has no subtitles: the reader drops its
    writer once it read it, and the next start from the buffer neither
    clocks nor scans."""
    player, cap, shown = channel(True, "main/live.m3u8")
    _run(10, lambda: cap._net.reader.cues is None)
    assert cap._net.reader.cues is None and not cap.writes_cues and cap.cue_pids == {}
    player.jump(-2)
    _run(5, lambda: player._played)
    assert player._bcues is None and player._ts_clocks == []
    assert player.feeder is not None and player.feeder._cues is None
