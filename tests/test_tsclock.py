# SPDX-License-Identifier: GPL-2.0-or-later
"""The video clock of the WebVTT shadow: the MPEG-TS time of the picture
against its stream time (lindvb.tsclock)."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")

from lindvb import tsclock  # noqa: E402
from lindvb.tsclock import PTS_HZ, PTS_WRAP, TsClock, pes_pts  # noqa: E402


def _pts_bytes(pts):
    return bytes([0x21 | ((pts >> 29) & 0x0e), (pts >> 22) & 0xff, 0x01 | ((pts >> 14) & 0xfe),
                  (pts >> 7) & 0xff, 0x01 | ((pts << 1) & 0xfe)])


def _packet(pts=None, stream_id=0xe0, start=True, adaptation=0):
    """One TS packet; with *pts* the start of a PES of *stream_id*."""
    head = bytes([0x47, (0x40 if start else 0) | 0x01, 0x00,
                  0x30 if adaptation else 0x10])
    if adaptation:
        head += bytes([adaptation - 1]) + b"\xff" * (adaptation - 1)
    body = b""
    if pts is not None:
        body = b"\0\0\1" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + _pts_bytes(pts)
    return (head + body).ljust(tsclock.TS_PACKET, b"\xff")


def test_pes_pts_of_a_video_pes():
    assert pes_pts(_packet(324000000)) == 324000000
    assert pes_pts(_packet(PTS_WRAP - 1)) == PTS_WRAP - 1
    assert pes_pts(_packet(90000, adaptation=8)) == 90000


def test_pes_pts_only_of_video_with_a_start():
    assert pes_pts(_packet(90000, stream_id=0xc0)) is None      # sound
    assert pes_pts(_packet(90000, start=False)) is None
    assert pes_pts(_packet()) is None
    assert pes_pts(_packet(90000)[:100]) is None
    assert pes_pts(b"\x00" + _packet(90000)[1:]) is None


def _frames(clock, first, n, offset, step=3600):
    for i in range(n):
        clock.frame((first + i * step) / PTS_HZ + offset)


def test_the_first_frame_ties_the_clocks_and_the_others_match():
    clock = TsClock()
    pts = [324000000 + i * 3600 for i in range(10)]
    clock.feed_ts(b"".join(_packet(p) + _packet(start=False) for p in pts))
    _frames(clock, pts[0], 10, -3600.0)
    assert clock.offset == pytest.approx(-3600.0)
    assert (clock.frames, clock.matched, clock.missed) == (9, 9, 0)
    assert clock.stream_time(3605.0) == pytest.approx(5.0)
    assert "-3600.000 s (1 starts" in clock.summary() and "9 of 9 frames matched" in clock.summary()


def test_packets_split_over_buffers_and_junk_before_them():
    clock = TsClock()
    data = b"\x12\x34" + b"".join(_packet(90000 + i * 3600) for i in range(6))
    for i in range(0, len(data), 100):
        clock.feed_ts(data[i:i + 100])
    _frames(clock, 90000, 6, 10.0)
    assert clock.offset == pytest.approx(10.0)
    assert (clock.matched, clock.missed) == (5, 0)


def test_frames_that_do_not_fit_the_offset_are_counted():
    clock = TsClock()
    clock.feed_ts(b"".join(_packet(90000 + i * 3600) for i in range(5)))
    clock.frame(1.0)
    for i in range(1, 5):
        clock.frame(1.0 + i * 0.04 + 0.5)       # half a second off
    assert (clock.matched, clock.missed) == (0, 4)
    assert "0 of 4 frames matched it, 4 not" in clock.summary()


def test_a_flush_takes_a_new_offset():
    clock = TsClock()
    clock.feed_ts(_packet(90000) + _packet(93600))
    clock.frame(0.0)
    clock.restart()
    clock.feed_ts(_packet(900000) + _packet(903600))
    clock.frame(20.0)
    clock.frame(20.04)
    assert clock.offset == pytest.approx(10.0)
    assert clock.anchors == [pytest.approx(-1.0), pytest.approx(10.0)]
    assert clock.matched == 1 and "2 starts, offsets spread 11.000 s" in clock.summary()


def test_the_wrap_of_the_mpegts_time():
    clock = TsClock()
    last = PTS_WRAP - 1800
    clock.feed_ts(_packet(last) + _packet((last + 3600) % PTS_WRAP))
    clock.frame(100.0)
    clock.frame(100.04)
    assert clock.matched == 1, clock.summary()
    wrap = PTS_WRAP / PTS_HZ
    assert clock.stream_time(0.5, near=100.0) == pytest.approx(100.0 + 0.5 + 1800 / PTS_HZ)
    assert clock.stream_time(0.5) == pytest.approx(100.0 + 0.5 + 1800 / PTS_HZ - wrap)


def test_no_offset_without_video():
    clock = TsClock()
    clock.feed_ts(_packet(90000, stream_id=0xc0))
    clock.frame(1.0)
    assert clock.offset is None and clock.stream_time(1.0) is None
    assert clock.summary() == "no video PES seen in the segments"


def test_kept_times_are_bounded():
    clock = TsClock()
    clock.feed_ts(b"".join(_packet(i * 3600) for i in range(tsclock.KEEP + 50)))
    assert len(clock._seen) == tsclock.KEEP and len(clock._order) == tsclock.KEEP


# -- epochs: the PTS jump without a flush (the reader connected again) -----------------

def _pes_packet(pts_ticks, pid=0x41):
    """A TS packet starting a video PES with this PTS."""
    from lindvb.cuebuffer import _pts_bytes
    pes = b"\0\0\1\xe0\0\0\x80\x80\x05" + _pts_bytes(pts_ticks)
    return bytes((0x47, 0x40 | pid >> 8, pid & 0xFF, 0x10)) + pes + b"\xff" * (184 - len(pes))


def test_a_pts_jump_starts_an_epoch_with_its_own_offset():
    from lindvb import tsclock
    c = tsclock.TsClock()
    hz = tsclock.PTS_HZ
    old = [round((3700 + k * 0.04) * hz) for k in range(50)]
    new = [round((3600 + k * 0.04) * hz) for k in range(50)]
    c.feed_ts(b"".join(_pes_packet(p) for p in old + new))
    for k in range(50):
        c.frame(10 + k * 0.04)                    # stream time goes on across the jump
    assert c.epoch == 0 and c.offset == pytest.approx(10 - 3700)
    for k in range(50):
        c.frame(12 + k * 0.04)
    assert c.epoch == 1 and c.offset == pytest.approx(12 - 3600)
    assert c.missed == 0 and c.matched == 99 and c.starts == 2
    assert c.stream_time(3701, epoch=0) == pytest.approx(11)
    assert c.stream_time(3601, epoch=1) == pytest.approx(13)
    assert c.stream_time(3601, epoch=2) is None
    c.restart()
    assert c.offsets == {} and c.epoch == 0


def test_epochs_counted_in_the_fed_bytes():
    from lindvb import tsclock
    hz = tsclock.PTS_HZ
    data = b"".join(_pes_packet(round(t * hz)) for t in (3700, 3700.04, 3600, 3600.04))
    e = tsclock.Epochs()
    assert e.at(data, [0, 188 * 2, 188 * 2 + 1, 188 * 4]) == [0, 0, 1, 1]
    assert e.at(_pes_packet(round(3600.08 * hz)), [0]) == [1]       # goes on in the next piece


def test_b_frames_and_wraps_are_no_jumps():
    from lindvb.tsclock import PTS_WRAP, is_jump
    assert not is_jump(None, 5)
    assert not is_jump(90000 * 10, 90000 * 10 - 40000)              # B-frame, 0.44 s back
    assert not is_jump(PTS_WRAP - 3600, 1800)                        # the 33-bit wrap
    assert is_jump(90000 * 3700, 90000 * 3600)
    assert is_jump(90000 * 3600, 90000 * 3700)
