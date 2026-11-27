# SPDX-License-Identifier: GPL-2.0-or-later
"""LinDVB's drawer of HLS WebVTT cues (lindvb.vttdraw), without a stream."""

from lindvb import vttdraw, vttload


class _Clock:
    """A video clock (lindvb.tsclock): stream time minus MPEG-TS time."""

    def __init__(self, offset=None):
        self.offset = offset

    def stream_time(self, mpegts, near=None):
        return None if self.offset is None else mpegts + self.offset

    def summary(self):
        return "the clock" if self.offset is not None else "no video PES seen in the segments"


def _draw(clock, pos=None):
    texts = []
    position = [pos]
    d = vttdraw.VttDraw("English", lambda: clock, lambda: position[0], texts.append)
    return d, texts, position


def test_cues_wait_for_the_clock():
    clock = _Clock()
    d, _texts, _pos = _draw(clock)
    d.cue("Live 1", 3601.0, 3601.8, True)
    d.cue("Live 2", 3602.0, 3602.8, True)
    assert not d.timed() and d.flush() is True
    assert len(d.pending) == 2 and not d.drawer.timeline.at(1 * 10**9)
    clock.offset = -3600.0
    assert d.timed() and d.flush() is False and d.pending == []
    # on the picture's timeline: Live n from n s to n + 0.8 s
    assert [c[2] for c in d.drawer.timeline.at(int(1.5e9))] == ["Live 1"]
    assert [c[2] for c in d.drawer.timeline.at(int(2.5e9))] == ["Live 2"]
    assert not d.drawer.timeline.at(int(1.9e9))


def test_cues_after_the_clock_go_straight_to_the_drawer():
    d, _texts, _pos = _draw(_Clock(-10.0), pos=0)
    d.cue("Cue 12", 12.0, 13.0, False)            # no X-TIMESTAMP-MAP: MPEG-TS time 0
    assert d.pending == []
    assert [c[2] for c in d.drawer.timeline.at(int(2.5e9))] == ["Cue 12"]


def test_waiting_is_bounded():
    d, _texts, _pos = _draw(_Clock())
    for i in range(vttdraw.PENDING + 7):
        d.cue(f"Cue {i}", float(i), i + 0.5, True)
    assert len(d.pending) == vttdraw.PENDING and d.dropped == 7
    assert d.pending[0][0] == "Cue 7"


def test_drawn_and_missed_are_counted():
    d, _texts, _pos = _draw(_Clock(0.0))
    d._shown("Cue 1", 1 * 10**9, 50 * 10**6)
    d._shown("Cue 2", 2 * 10**9, 150 * 10**6)
    d._shown("Cue 3", 3 * 10**9, None)
    assert d.drawn == 2 and d.missed == 1
    assert abs(d.late.mean - 0.1) < 1e-9 and abs(d.late.high - 0.15) < 1e-9


def test_report_names_the_drawer_not_the_shadow():
    d, _texts, _pos = _draw(_Clock(-3600.0), pos=0)
    d.cue("Live 1", 3601.0, 3601.8, True)
    d._shown("Live 1", 1 * 10**9, 80 * 10**6)
    line = d.report(vttload.LoaderState())
    assert line.startswith("WebVTT subtitles (LinDVB draws, English): "), line
    assert "shadow" not in line and "old path" not in line, line
    assert line.endswith("; 1 cues drawn, 0.080 s after their time on average "
                         "(most 0.080 s), 0 over before they could be drawn"), line


def test_report_says_cues_never_timed():
    d, _texts, _pos = _draw(_Clock())
    d.cue("Live 1", 3601.0, 3601.8, True)
    line = d.report(vttload.LoaderState())
    assert line.endswith("; 0 cues drawn, 0 over before they could be drawn, "
                         "1 never timed (no offset)"), line

