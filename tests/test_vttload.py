# SPDX-License-Identifier: GPL-2.0-or-later
"""The WebVTT loader of stage F (lindvb.vttload), in the shadow of hlsdemux2."""

import tempfile
import time
from pathlib import Path

import streamkit as sk

from lindvb import vttload

MAPPED = ("WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000\n\n"
          "1\n00:00:01.000 --> 00:00:02.500 line:90%\n<c.white>Hello</c> &amp; <i>you</i>\n"
          "  second line \n\n00:00:03.000 --> 00:00:04.000\nBye\n")


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.02)
    return cond()


def test_times():
    assert vttload.vtt_time("01:02:03.456") == 3723.456
    assert vttload.vtt_time("02:03.456") == 123.456
    assert vttload.vtt_time("2:03") is None
    assert vttload.vtt_time("") is None


def test_segment_with_a_timestamp_map():
    tmap, cues = vttload.parse_vtt("﻿" + MAPPED.replace("\n", "\r\n"))
    assert tmap == (10.0, 0.0)
    assert cues == [(1.0, 2.5, "Hello & you\nsecond line"), (3.0, 4.0, "Bye")]


def test_segment_without_one_and_header_only():
    assert vttload.parse_vtt("WEBVTT\n\n00:00:05.000 --> 00:00:06.000\nx\n") == \
        (None, [(5.0, 6.0, "x")])
    assert vttload.parse_vtt("WEBVTT\n\n") == (None, [])        # PBS: 65 bytes, nothing
    assert vttload.parse_vtt("<html>") == (None, [])


def test_timestamp_map_wraps_and_local_counts():
    line = f"X-TIMESTAMP-MAP=LOCAL:00:00:10.000,MPEGTS:{vttload.PTS_WRAP + 90000}"
    assert vttload._timestamp_map(line) == (1.0, 10.0)
    assert vttload._timestamp_map("X-TIMESTAMP-MAP=MPEGTS:abc,LOCAL:00:00:00.000") is None


def test_media_playlist():
    text = ("#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:41\n"
            "#EXTINF:6.0,\na.vtt\n#EXTINF:5.5,\nhttps://cdn/b.vtt\n")
    target, live, segs = vttload.media_segments(text, "https://host/subs/index.m3u8")
    assert (target, live) == (6.0, True)
    assert segs == [(41, 6.0, "https://host/subs/a.vtt"), (42, 5.5, "https://cdn/b.vtt")]
    assert not vttload.media_segments(text + "#EXT-X-ENDLIST\n", "")[1]


class _Site:
    """Fake HTTP: {address: text}; the master redirects."""

    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def __call__(self, url, limit=0):
        self.asked.append(url)
        if url == "https://gw/master.m3u8":
            return "#EXTM3U\n", "https://cdn/x/master.m3u8"
        if url not in self.pages:
            raise OSError(f"404 {url}")
        return self.pages[url], url


def _playlist(first, last, end=False):
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
    for i in range(first, last + 1):
        lines += ["#EXTINF:2.0,", f"s{i}.vtt"]
    return "\n".join(lines + (["#EXT-X-ENDLIST"] if end else [])) + "\n"


def test_live_starts_near_the_end_and_loads_each_segment_once():
    pages = {f"https://cdn/x/subs/s{i}.vtt": MAPPED for i in range(20)}
    pages["https://cdn/x/subs/live.m3u8"] = _playlist(0, 9)
    site, cues = _Site(pages), []
    loader = vttload.WebVttLoader("https://gw/master.m3u8", "subs/live.m3u8",
                                  lambda *c: cues.append(c), fetch=site)
    assert loader.step() == 2.0                 # the target duration
    assert loader.url == "https://cdn/x/subs/live.m3u8"   # from where the master really is
    assert [u.rsplit("/", 1)[1] for u in site.asked[2:]] == ["s6.vtt", "s7.vtt", "s8.vtt", "s9.vtt"]
    assert cues[0] == ("Hello & you\nsecond line", 11.0, 12.5, True)
    pages["https://cdn/x/subs/live.m3u8"] = _playlist(4, 11)
    loader.step()
    assert site.asked[-2:] == ["https://cdn/x/subs/s10.vtt", "https://cdn/x/subs/s11.vtt"]
    assert loader.step() == 1.0                 # nothing new: half the target duration
    st = loader.state
    assert (st.playlists, st.segments, st.mapped, st.cues, st.empty) == (3, 6, 6, 12, 0)


def test_playlist_with_an_end_is_loaded_once_and_bounded(monkeypatch):
    monkeypatch.setattr(vttload, "VOD_LIMIT", 3)
    pages = {f"https://h/s{i}.vtt": "WEBVTT\n\n" for i in range(5)}
    pages["https://h/index.m3u8"] = _playlist(0, 4, end=True)
    loader = vttload.WebVttLoader("https://h/master.m3u8", "https://h/index.m3u8",
                                  lambda *c: None, fetch=_Site(pages))
    assert loader.step() is None
    st = loader.state
    assert (st.segments, st.empty, st.mapped, st.capped) == (3, 3, 0, True)


def test_thread_gives_up_after_errors_in_a_row():
    loader = vttload.WebVttLoader("https://h/master.m3u8", "nowhere.m3u8", lambda *c: None,
                                  fetch=_Site({}), sleep=lambda _s: None)
    loader.start()
    assert _wait(lambda: not loader.running)
    st = loader.state
    assert st.gave_up and st.errors == vttload.GIVE_UP and "404" in st.last_error


def test_stop_ends_the_thread():
    pages = {"https://h/live.m3u8": _playlist(0, 0), "https://h/s0.vtt": MAPPED}
    loader = vttload.WebVttLoader("https://h/master.m3u8", "https://h/live.m3u8",
                                  lambda *c: None, fetch=_Site(pages))
    loader.start()
    assert _wait(lambda: loader.state.segments == 1)
    loader.stop()
    assert _wait(lambda: not loader.running)


def test_report_offset_to_the_old_path():
    shadow = vttload.VttShadow("English")
    for i in range(3):
        shadow.saw_new(f"cue {i}", 3600.0 + i, 3600.8 + i, True)
        shadow.saw_old(f"cue {i}", int((0.25 + i) * 1e9))
    shadow.saw_old("cue 1", None)
    st = vttload.LoaderState()
    st.playlists, st.segments, st.mapped, st.cues = 4, 3, 3, 3
    line = shadow.report(st)
    assert line.startswith("WebVTT loader (shadow, English): playlist read 4 times, "
                           "3 segments (0 without cues, 3 with X-TIMESTAMP-MAP), 3 cues; "
                           "the old path 3 cues"), line
    assert "3 of the old path's cues found, its times +3599.750 s off theirs " \
           "(spread 0.000 s)" in line, line


class _Clock:
    """A video clock (lindvb.tsclock) whose offset is known."""

    def __init__(self, offset):
        self.offset = offset

    def stream_time(self, mpegts, near=None):
        return None if self.offset is None else mpegts + self.offset

    def summary(self):
        return "the clock"


def test_report_on_the_picture_timeline():
    """With the video clock: when each cue is due on the picture's timeline
    against where the playback was when it came, and the old path's times
    against the picture's."""
    shadow = vttload.VttShadow("English")
    for i, pos in enumerate((None, 0.5, 2.5, 4.0)):
        shadow.saw_new(f"cue {i}", 3601.0 + i, 3601.8 + i, True, pos)
        shadow.saw_old(f"cue {i}", int((1.25 + i) * 1e9))
    assert shadow.leads(_Clock(-3600.0)) == [1.5, 0.5, 0.0]
    line = shadow.report(vttload.LoaderState(), _Clock(-3600.0))
    assert "; video: the clock; on the picture's timeline the cues came 0.667 s before they " \
           "were due (least 0.000 s, 0 late); 1 cues came before the playback had begun; " \
           "the old path's times -0.250 s off the picture's (spread 0.000 s)" in line, line
    shadow.saw_new("late", 3610.0, 3611.0, True, 11.0)
    assert "(least -1.000 s, 1 late)" in shadow.report(vttload.LoaderState(), _Clock(-3600.0))


def test_report_with_a_clock_that_saw_no_picture():
    shadow = vttload.VttShadow("English")
    shadow.saw_new("x", 3601.0, 3602.0, True, 0.5)
    line = shadow.report(vttload.LoaderState(), _Clock(None))
    assert line.endswith("; video: the clock; first 'x'"), line
    assert "video" not in shadow.report(vttload.LoaderState())


def test_report_says_what_went_wrong():
    shadow = vttload.VttShadow("English")
    shadow.saw_new("x", 5.0, 6.0, False)
    shadow.saw_old("y", 0)
    st = vttload.LoaderState()
    st.errors, st.last_error, st.gave_up = 5, "HTTP Error 403: Forbidden", True
    line = shadow.report(st)
    assert "5 errors, last 'HTTP Error 403: Forbidden', gave up" in line
    assert "none of the old path's cues found" in line and "first 'x'" in line


def test_loads_a_live_rendition_over_http():
    folder = Path(tempfile.mkdtemp(prefix="lindvb-vtt-"))
    subs = sk.make_vtt(folder / "subs", 8, text="Live")
    srv = sk.Server(folder, live={"subs/live.m3u8": sk.LivePlaylist(
        sorted(p.name for p in subs.glob("s*.vtt")))})
    (folder / "master.m3u8").write_text("#EXTM3U\n")
    cues = []
    loader = vttload.WebVttLoader(srv.url("master.m3u8"), "subs/live.m3u8",
                                  lambda *c: cues.append(c))
    try:
        loader.start()
        assert _wait(lambda: len(cues) >= 6, timeout=10), (cues, vars(loader.state))
    finally:
        loader.stop()
        srv.close()
    text, start, end, mapped = cues[0]
    n = int(text.split()[1])
    assert mapped and text == f"Live {n}" and start == 3600.0 + n and end == 3600.8 + n
    assert [int(c[0].split()[1]) for c in cues] == list(range(n, n + len(cues)))


def test_live_stream_that_starts_anew():
    pages = {f"https://h/s{i}.vtt": MAPPED for i in range(30)}
    pages["https://h/live.m3u8"] = _playlist(20, 25)
    site = _Site(pages)
    loader = vttload.WebVttLoader("https://h/master.m3u8", "https://h/live.m3u8",
                                  lambda *c: None, fetch=site)
    loader.step()
    pages["https://h/live.m3u8"] = _playlist(0, 5)    # the encoder restarted
    loader.step()
    assert site.asked[-4:] == [f"https://h/s{i}.vtt" for i in range(2, 6)]


def test_a_kick_looks_again_soon_until_the_segment_is_listed():
    """The buffer's reader got the picture of a new segment: the loader
    looks at once, and a few times shortly after while the subtitle
    segment is not listed yet; then at its usual pace again."""
    import threading
    pages = {f"https://cdn/x/subs/s{i}.vtt": MAPPED for i in range(20)}
    pages["https://cdn/x/subs/live.m3u8"] = _playlist(0, 9)
    loader = vttload.WebVttLoader("https://cdn/x/master.m3u8", "https://cdn/x/subs/live.m3u8",
                                  lambda *c: None, fetch=_Site(pages))
    assert loader.step() == 2.0
    loader.kick()
    assert loader._kick.is_set()
    assert [loader.step() for _ in range(vttload.KICK_TRIES)] == \
        [vttload.KICK_RETRY] * vttload.KICK_TRIES
    assert loader.step() == 1.0                 # nothing came: the usual pace
    loader.kick()
    pages["https://cdn/x/subs/live.m3u8"] = _playlist(2, 10)
    assert loader.step() == 2.0 and loader._eager == 0    # it came
    # the wait ends with a kick, not after its time
    done = threading.Event()
    t = threading.Thread(target=lambda: (loader._wait(30), done.set()))
    t.start()
    loader.kick()
    assert done.wait(5)
    loader.stop()
