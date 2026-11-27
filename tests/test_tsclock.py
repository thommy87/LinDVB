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
