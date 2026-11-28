# SPDX-License-Identifier: GPL-2.0-or-later
"""Subtitle cues in the timeshift buffer (lindvb.cuebuffer)."""


from lindvb import cuebuffer as cb
from lindvb.tsclock import pes_pts


def test_a_cue_is_one_packet_with_its_time():
    data, cc = cb.cue_packets(0x1FC3, "Guten Abend", 3610.5, 3612.25, cc=7)
    assert len(data) == 188 and cc == 8
    assert data[0] == 0x47 and data[1] & 0x40 and (data[3] & 0x0F) == 7
    pid, ticks, dur, part, parts, text = cb.parse_cue_packet(data)
    assert pid == 0x1FC3 and ticks == round(3610.5 * 90000) and dur == round(1.75 * 90000)
    assert (part, parts, text) == (0, 1, "Guten Abend".encode())


def test_a_long_cue_goes_in_parts_and_comes_back_whole():
    text = "Ä" * 300 + " Ende"                     # 605 bytes in UTF-8
    data, cc = cb.cue_packets(0x1FC0, text, 10.0, 12.0)
    n = len(data) // 188
    assert len(data) % 188 == 0 and n == 4 and cc == 4
    found = cb.CueReader().feed(data)
    assert [(pos, pid, start, end, t) for pos, pid, start, end, t in found] == \
        [(188 * 3, 0x1FC0, 10.0, 12.0, text)]


def test_parts_split_over_two_pieces_of_data():
    data, _cc = cb.cue_packets(0x1FC1, "x" * 400, 5.0, 6.0)
    r = cb.CueReader()
    assert r.feed(data[:300]) == []
    found = r.feed(data[188:])                    # the second piece starts at a packet
    assert len(found) == 1 and found[0][4] == "x" * 400


def test_a_cue_packet_cut_between_two_reads():
    other = bytes((0x47, 0x01, 0x00, 0x10)) + b"\xab" * 184
    cue, _cc = cb.cue_packets(0x1FC2, "geteilt", 7.0, 8.0)
    data = other + cue + other
    for cut in (188 + 1, 188 + 2, 188 + 100, 188 + 187):
        r = cb.CueReader()
        first, second = r.feed(data[:cut]), r.feed(data[cut:])
        assert first == [] and [(f[0], f[4]) for f in second] == [(0, "geteilt")], cut


def test_cue_packets_taken_out_of_whole_packets():
    other = bytes((0x47, 0x01, 0x00, 0x10)) + b"\xab" * 184
    a, cc = cb.cue_packets(0x1FC2, "x" * 400, 1.0, 2.0)          # three packets
    b, _cc = cb.cue_packets(0x1FC5, "zwei", 3.0, 4.0, cc)
    assert cb.strip_cue_packets(other + a + other + b + other) == other * 3
    assert cb.strip_cue_packets(other * 2) == other * 2
    # the same bytes inside a packet are no cue packet
    inside = bytes((0x47, 0x01, 0x00, 0x10)) + b"\x47\x5f\xc0" + b"\xab" * 181
    assert cb.strip_cue_packets(inside) == inside


def test_cues_among_other_packets_not_aligned():
    other = bytes((0x47, 0x01, 0x00, 0x10)) + b"\xab" * 184
    a, cc = cb.cue_packets(0x1FC2, "eins", 1.0, 2.0)
    b, _cc = cb.cue_packets(0x1FC2, "zwei", 3.0, 4.0, cc)
    data = b"\x00\x11" + other + a + other * 2 + b
    found = cb.CueReader().feed(data)
    assert [(f[0], f[4]) for f in found] == [(2 + 188, "eins"), (2 + 188 * 4, "zwei")]


def test_the_pes_carries_a_pts_other_tools_read():
    data, _cc = cb.cue_packets(0x1FC0, "a", 3600.04, 3601.0)
    # pes_pts reads only video PES: the same bytes with a video stream id
    video = bytearray(data)
    i = video.index(b"\0\0\1\xbd")
    video[i + 3] = 0xE0
    assert pes_pts(bytes(video)) == round(3600.04 * 90000)


def test_empty_and_wrapped_times():
    data, _cc = cb.cue_packets(0x1FC0, "", 95443.7, 95444.7)    # past 2^33 ticks
    pid, ticks, dur, _p, _n, text = cb.parse_cue_packet(data)
    assert ticks == round(95443.7 * 90000) % (1 << 33) and dur == 90000 and text == b""


def test_each_rendition_its_own_pid():
    pids = cb.cue_pids(["Deutsch", "English", "Untertitel deutsch"])
    assert len(set(pids.values())) == 3 and all(cb.is_cue_pid(p) for p in pids.values())
    assert cb.cue_pids(["Deutsch"]) == {"Deutsch": pids["Deutsch"]}
    assert not cb.is_cue_pid(0x1FF0) and not cb.is_cue_pid(0x1C0)


def test_other_packets_are_no_cues():
    assert cb.parse_cue_packet(bytes((0x47, 0x5F, 0xC0, 0x10)) + b"\0" * 184) is None
    assert cb.parse_cue_packet(b"\x47" * 100) is None


# -- the reader writes them, the player finds them ----------------------------------

HZ = 90000


def _video(t, pid=0x41):
    """A TS packet starting a video PES at *t* s."""
    pes = b"\0\0\1\xe0\0\0\x80\x80\x05" + cb._pts_bytes(round(t * HZ))
    return bytes((0x47, 0x40 | pid >> 8, pid & 0xFF, 0x10)) + pes + b"\xff" * (184 - len(pes))


def _writer(lead=2.0):
    w = cb.CueWriter({"Deutsch": 0x1FC4}, lead=lead)
    # the segments: MPEG-TS time 100 s on; the reader's own timeline: 3600 s on
    w.clock.feed_ts(b"".join(_video(100 + k * 0.04) for k in range(500)))
    return w


def _hand_on(w, start, frames):
    """The reader hands on *frames* frames from *start* (its own timeline)."""
    out = []
    for k in range(frames):
        data = _video(start + k * 0.04)
        out.append(data + w.out(data))
    return b"".join(out)


def test_cues_are_held_until_shortly_before_their_picture():
    w = _writer()
    w.cue("Deutsch", "eins", 103.0, 104.0)
    w.cue("Deutsch", "zwei", 101.0, 102.0)
    w.cue("Englisch", "not in the buffer", 101.0, 102.0)
    assert w.came == 2 and [c[3] for c in w._held] == ["zwei", "eins"]
    r = cb.CueReader()
    data = _hand_on(w, 3600, 1)                          # 3600 s: the segments' 100 s
    assert [text for *_x, text in r.feed(data)] == ["zwei"]       # due in 1 s
    assert [c[3] for c in w._held] == ["eins"]
    data = _hand_on(w, 3600.04, 24)                       # up to 3600.96 s: 3 s is more than 2 ahead
    assert r.feed(data) == [] and len(w._held) == 1
    found = r.feed(_hand_on(w, 3601.0, 1))
    assert [(t0, t1, text) for _pos, _pid, t0, t1, text in found] == [(3603.0, 3604.0, "eins")]
    assert w.written == 2 and w.late == 0 and not w._held
    assert "2 came, 2 written (0 after their picture, at most 0.00 s after it came)" in w.report()


def test_late_cues_still_go_in_long_over_ones_not():
    w = _writer()
    _hand_on(w, 3600, 250)                                # up to 3609.96 s
    w.cue("Deutsch", "late", 108.0, 112.0)                # its picture went 2 s ago
    w.cue("Deutsch", "long over", 50.0, 52.0)
    found = cb.CueReader().feed(_hand_on(w, 3610, 1))
    assert [text for *_x, text in found] == ["late"]
    # its picture came moments ago: that is how long it went in after it
    assert w.late == 1 and w.stale == 1 and 0 <= w.behind < 1.0


def test_a_new_pipeline_waits_for_its_first_picture():
    w = _writer()
    _hand_on(w, 3600, 10)
    w.restart()
    w.cue("Deutsch", "after the reconnect", 300.0, 301.0)
    assert w.out(b"\x47" + b"\0" * 187) == b""            # no video yet: no clock
    w.clock.feed_ts(b"".join(_video(299 + k * 0.04) for k in range(100)))
    data = _hand_on(w, 3600, 30)                          # its muxer starts at 3600 s again
    assert [(t0, text) for _p, _pid, t0, _t1, text in cb.CueReader().feed(data)] == \
        [(3601.0, "after the reconnect")]
    # it went after the frame of its time minus the lead, not before the first one
    assert data.find(cb.CUE_MAGIC) > 188


def test_cues_held_are_bounded():
    w = _writer()
    for k in range(cb.CUE_HOLD + 5):
        w.cue("Deutsch", str(k), 200.0 + k, 201.0 + k)
    assert len(w._held) == cb.CUE_HOLD and w.dropped == 5


def test_the_player_finds_cues_with_their_epoch():
    old = b"".join(_video(3700 + k * 0.04) for k in range(10))
    cue1, _ = cb.cue_packets(0x1FC4, "before the start", 3700.1, 3702.0)
    new = b"".join(_video(3600 + k * 0.04) for k in range(10))
    cue2, _ = cb.cue_packets(0x1FC4, "after the jump", 3600.2, 3601.0)
    f = cb.CueFinder()
    f.before(old + cue1)
    assert f.feed(b"\x47" + b"\xff" * 187) == []          # no video fed yet: held
    got = f.feed(old[:188 * 2])
    assert got == [(0x1FC4, 3700.1, 3702.0, "before the start", 0)]
    assert f.feed(old[188 * 2:] + new + cue2) == [(0x1FC4, 3600.2, 3601.0, "after the jump", 1)]


def test_cues_from_before_a_jump_at_the_start_are_left_out():
    old = b"".join(_video(3700 + k * 0.04) for k in range(10))
    cue1, _ = cb.cue_packets(0x1FC4, "the old stretch", 3700.1, 3702.0)
    f = cb.CueFinder()
    f.before(old + cue1)
    new = b"".join(_video(3600 + k * 0.04) for k in range(10))   # playback starts on the jump
    assert f.feed(new) == []


def test_new_segment_time_wakes_the_loaders():
    """The loaders read their playlists every target duration: new segment
    time at the demuxer wakes them, at most every KICK_GAP s, and again
    after a jump back (a new start)."""
    w = cb.CueWriter({"Deutsch": 0x1FC4})
    c = w.clock
    assert not w._new_segment_time(c, now=10.0)                       # nothing seen yet
    c.feed_ts(_video(100.0))
    assert w._new_segment_time(c, now=10.0) and w.kicks == 1
    c.feed_ts(_video(100.2))
    assert not w._new_segment_time(c, now=11.0)                       # not half a second on
    c.feed_ts(_video(101.0))
    w._kick_time = 11.0
    assert not w._new_segment_time(c, now=11.2)                       # too soon after the last
    assert w._new_segment_time(c, now=11.6) and w.kicks == 2
    c.feed_ts(_video(50.0))                                           # the stream started anew
    assert w._new_segment_time(c, now=12.2) and w.kicks == 3
    woken = []
    w.on_new = lambda: woken.append(1)
    c.feed_ts(_video(60.0))
    w._kick_time = 0.0
    w.out(_video(3600.0))
    assert woken == [1]


def test_a_cue_that_comes_between_two_segments_goes_in_at_once():
    """The demuxer hands on a segment at a time: a cue the loader brings
    after its segment's picture goes into the stream at once, not with the
    next segment; not while the stream stands between two packets."""
    w = _writer()
    _hand_on(w, 3600, 25)                                 # the segment up to 3600.96 s
    w.cue("Deutsch", "late", 100.5, 101.5)
    found = cb.CueReader().feed(w.due_now())
    assert [text for *_x, text in found] == ["late"] and w.between == 1
    w.cue("Deutsch", "far ahead", 110.0, 111.0)
    assert w.due_now() == b"" and len(w._held) == 1        # not due yet: waits
    w.out(b"\x47" * 100, aligned=False)
    w.cue("Deutsch", "now", 101.0, 101.5)
    assert w.due_now() == b""                             # mid packet: with the next data
    assert "1 times written between two pieces of video" in w.report()
