"""The subtitle renderers (stage F of the concept "Streams, Timeshift,
Untertitel und Zusatzton", section 5).

First the safety net: what the player shows today, played end to end with
a stream sample of tests/streamkit.py. The renderers that take over one by
one have to pass the same tests.
"""

import time

import pytest

import streamkit

TTX_PAGE = 150
SUBTITLE = "Untertitel"


@pytest.fixture(params=[False, True], ids=["debug-off", "debug-on"])
def debug(request, monkeypatch):
    """Debug mode off and on: with it on, the renderers of stage F run in
    the shadow of the old paths, which must show the same all the same."""
    from lindvb import debuglog
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: request.param)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append(" ".join(p)))
    return lines if request.param else None


@pytest.fixture
def player(debug):
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
    streamkit.gst("teletextdec", "tsdemux", "avdec_h264")
    from lindvb.config import DEFAULTS
    from lindvb.player import Player
    p = Player(Gtk.Picture(), dict(DEFAULTS, deinterlace=False, video_decoder="software"))
    shown = []
    p.connect("subtitle-text", lambda _p, text: shown.append(text))
    p.shown = shown
    p.log = debug
    yield p
    p.stop()


def _run(seconds, until=None):
    from gi.repository import GLib
    loop = GLib.MainLoop()
    end = time.monotonic() + seconds

    def tick():
        if time.monotonic() > end or (until and until()):
            loop.quit()
            return False
        return True
    GLib.timeout_add(50, tick)
    loop.run()


@pytest.fixture(scope="module")
def teletext_ts(tmp_path_factory):
    return streamkit.make_teletext_ts(tmp_path_factory.mktemp("ttx") / "ttx.ts", seconds=12)


def _position(player):
    from gi.repository import Gst
    ok, pos = player.playbin.query_position(Gst.Format.TIME)
    return pos if ok else -1


def test_teletext_subtitles_end_to_end(player, teletext_ts):
    """Das Erste, ZDF (broadcast): the subtitle page from the PMT is offered,
    starts off, shows its rows in order once picked, and Off clears them."""
    player.play_uri(teletext_ts.as_uri())
    _run(10, lambda: player._ttx_pages and player._ttx_dec is not None)
    labels, cur = player.subtitle_tracks()
    assert len(labels) == 1 and str(TTX_PAGE) in labels[0], labels
    assert cur == -1 and player.has_teletext()
    _run(1.5)
    assert not [t for t in player.shown if t]           # off: nothing is shown
    player.select_subtitle(0)
    assert player.subtitle_tracks()[1] == 0 and player.ttx_page == TTX_PAGE
    _run(8, lambda: len([t for t in player.shown if t]) >= 3)
    texts = [t for t in player.shown if t]
    assert len(texts) >= 3 and all(t.startswith(SUBTITLE) for t in texts), player.shown
    numbers = [int(t.split()[-1]) for t in texts]
    assert numbers == sorted(set(numbers))              # in order, each once
    pos = _position(player)
    _run(1)
    assert _position(player) > pos                      # the picture goes on
    player.select_subtitle(-1)
    assert player.shown[-1] == "" and player.subtitle_tracks()[1] == -1
    before = len(player.shown)
    _run(1.5)
    assert player.shown[before:] == []                  # and stays off
    if player.log is not None:
        # the teletext renderer ran next to it and decoded the same texts
        reports = [ln for ln in player.log if ln.startswith("Teletext renderer (shadow")]
        assert len(reports) == 1, player.log
        assert reports[0].endswith("the same texts") and f"ttx:{TTX_PAGE}" in reports[0], reports


# -- the interface -------------------------------------------------------------------

from lindvb import subrender as sr  # noqa: E402
from lindvb.trackcatalog import SUBTITLE as SUB, Track  # noqa: E402

PAGE_150 = Track(SUB, sr.TELETEXT, "deu", origin=f"ttx:{TTX_PAGE}")


class _Fake(sr.Renderer):
    def __init__(self, kind):
        self.kind = kind
        self._state = sr.State(kind)
        self.calls = []

    def show(self, track):
        self.calls.append(("show", track.origin))
        self._state = sr.State(self.kind, on=True, track=track.origin)

    def off(self):
        self.calls.append(("off",))
        self._state = sr.State(self.kind)

    def state(self):
        return self._state


def test_exactly_one_renderer_is_on():
    ttx, dvb, c708 = _Fake(sr.TELETEXT), _Fake(sr.DVB_SUB), _Fake(sr.CEA708)
    sw = sr.Switch([ttx, dvb, c708])
    assert sw.active is None
    assert sw.show(PAGE_150) is ttx and sw.active is ttx
    sw.show(Track(SUB, sr.DVB_SUB, origin="pid:0x80"))
    assert sw.active is dvb and not ttx.state().on and ttx.calls[-1] == ("off",)
    assert c708.calls == []                        # one that was off is left alone
    # a track no renderer can show: everything off
    assert sw.show(Track(SUB, "ARIB-B24", origin="pid:0x90")) is None and sw.active is None
    assert sw.show(None) is None
    sw.show(Track(SUB, sr.CEA708, origin="cc:1"))
    sw.off()                                       # "Off" switches all of them off
    assert sw.active is None and all(r.calls[-1] == ("off",) for r in (ttx, dvb, c708))


def test_teletext_text():
    assert sr.teletext_text(b"  Untertitel 1 \n\n  zwei  \n\x00") == "Untertitel 1\nzwei"
    assert sr.teletext_text(b"\x00\x00") == ""


def test_shadow_report():
    state = sr.State(sr.TELETEXT, on=True, track=PAGE_150.origin, data=40)
    sh = sr.Shadow(PAGE_150.origin)
    assert sh.report(state) is None                # nothing shown on either side
    for t in ("a", "b", "c"):
        sh.saw_old(t)
        sh.saw_new(t)
    sh.saw_old("d")                                # still on its way to the renderer
    sh.saw_new("")
    assert sh.report(state).endswith("3 texts, the old path 4; data 40, dropped 0; "
                                     "the same texts")
    sh = sr.Shadow(PAGE_150.origin)
    for t in ("a", "b", "c"):
        sh.saw_old(t)
    sh.saw_new("c")
    sh.saw_new("x")
    sh.saw_new("y")
    line = sh.report(state)
    assert "missing 'a', 'b', extra 'x'" in line


# -- the teletext renderer on its own ------------------------------------------------

def _run_renderer(ts, renderer, on_at=0, timeout=30, release=None):
    """Play *ts* through tsdemux to a fake video sink and the renderer; switch
    it on at the teletext buffer *on_at* (None: never). Returns the video
    buffers that went through and the seconds that took; *release()* runs
    before the pipeline stops."""
    _GLib, Gst = streamkit.gst("teletextdec", "tsdemux")
    pl = Gst.parse_launch(f"filesrc location={ts} ! tsdemux name=d d. ! queue ! "
                          "fakesink name=v sync=false")
    pl.add(renderer.element)
    seen, video = [0], [0]

    def count(_pad, _info):
        if seen[0] == on_at:
            renderer.show(PAGE_150)
        seen[0] += 1
        return Gst.PadProbeReturn.OK

    def video_buffer(_pad, _info):
        video[0] += 1
        return Gst.PadProbeReturn.OK

    def added(_d, pad):
        if pad.get_name().startswith("private"):
            pad.add_probe(Gst.PadProbeType.BUFFER, count)
            pad.link(renderer.element.get_static_pad("sink"))
    pl.get_by_name("d").connect("pad-added", added)
    pl.get_by_name("v").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, video_buffer)
    t0 = time.monotonic()
    pl.set_state(Gst.State.PLAYING)
    while time.monotonic() < t0 + timeout and video[0] < 250:
        time.sleep(0.05)
    took = time.monotonic() - t0
    pl.get_bus().timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS)
    if release is not None:
        release()
    pl.set_state(Gst.State.NULL)
    return video[0], took


def test_teletext_renderer_reports_data_and_text(teletext_ts):
    streamkit.gst("teletextdec")
    texts = []
    r = sr.TeletextRenderer(texts.append, sync=False)
    assert r.can(PAGE_150) and not r.can(Track(SUB, sr.DVB_SUB))
    _run_renderer(teletext_ts, r, on_at=3)
    s = r.state()
    assert s.on and s.track == PAGE_150.origin and s.data == 9 and s.decoded >= 7
    assert texts[0] == f"{SUBTITLE} 3" and s.last == texts[-1]
    r.off()
    assert not r.state().on and texts[-1] == ""        # the shown text is cleared


def test_off_takes_in_no_data(teletext_ts):
    streamkit.gst("teletextdec")
    texts = []
    r = sr.TeletextRenderer(texts.append, sync=False)
    _run_renderer(teletext_ts, r, on_at=None)
    assert texts == [] and r.state() == sr.State(sr.TELETEXT)
    # another page than the stream's: data arrives, nothing is decoded
    r = sr.TeletextRenderer(texts.append, sync=False)
    r.show(Track(SUB, sr.TELETEXT, origin="ttx:777"))
    _run_renderer(teletext_ts, r, on_at=None)
    assert r.state().data > 0 and r.state().decoded == 0 and texts == []
    r.show(Track(SUB, sr.TELETEXT, origin="ttx:x"))   # no page: off
    assert not r.state().on


def test_a_standing_renderer_never_holds_up_the_picture(teletext_ts):
    """The renderer's text callback hangs: the video goes on, the renderer
    loses subtitles (concept: "verliert Untertitel, hält aber nie Bild
    oder Ton an")."""
    import threading
    streamkit.gst("teletextdec")
    stuck = threading.Event()
    r = sr.TeletextRenderer(lambda _t: stuck.wait(30), queue_buffers=2, sync=False)
    try:
        video, took = _run_renderer(teletext_ts, r, on_at=0, timeout=20, release=stuck.set)
    finally:
        stuck.set()
    assert video >= 250 and took < 10
    assert r.state().dropped > 0


# -- text cues (WebVTT) ----------------------------------------------------------------

from test_hls_renditions import ard  # noqa: E402,F401 - the WebVTT stream sample

WEBVTT_TRACK = Track(SUB, sr.WEBVTT, origin="text")


def test_webvtt_subtitles_end_to_end(player, ard):  # noqa: F811
    """ARD Mediathek, tagesschau24 (HLS with a WebVTT rendition): picked, the
    cues are shown; with debug mode on, the text cue renderer read along
    and had the same cues at the same times."""
    url, _folder = ard
    player.settings["stream_engine"] = "playbin3"
    player.play_uri(url)
    _run(15, lambda: player.subtitle_tracks()[0] and player._played)
    _run(1)
    if player.log is None:
        assert player._cue_shadow is None                # no renderer beside it at all
    else:
        assert player._cue_shadow.state() == sr.State(sr.WEBVTT)   # off: reads nothing
    player.select_subtitle(0)
    _run(15, lambda: player.subtitle_tracks()[1] == 0 and player._played)
    _run(4)
    assert player._cue_shadow is None if player.log is None else player._cue_shadow.state().on
    player.select_subtitle(-1)
    _run(15, lambda: player.subtitle_tracks()[1] == -1 and player._played)
    _run(1)
    player.stop()
    if player.log is not None:
        reports = [ln for ln in player.log if ln.startswith("WebVTT renderer (shadow")]
        assert reports, player.log
        assert all(r.endswith("the same cues at the same times") for r in reports), reports
        counts = [int(r.split(": ", 1)[1].split()[0]) for r in reports]
        assert sum(counts) >= 2, reports


def test_cue_text():
    assert sr.cue_text(b"<i>Untertitel</i> &amp; mehr\n  zwei \n\x00") == "Untertitel & mehr\nzwei"
    assert sr.cue_text(b"<b></b>\n") == ""


def test_stream_time():
    _GLib, Gst = streamkit.gst()
    seg = Gst.Segment()
    seg.init(Gst.Format.TIME)
    seg.start = 10 * Gst.SECOND
    seg.time = 100 * Gst.SECOND               # after a seek: the position stays
    assert sr.stream_time(seg, 12 * Gst.SECOND) == 102 * Gst.SECOND
    assert sr.stream_time(seg, Gst.CLOCK_TIME_NONE) is None
    assert sr.stream_time(None, 12 * Gst.SECOND) is None
    assert sr.stream_time(seg, 5 * Gst.SECOND) is None   # before the segment
    # a seek into a cue: shown from where the seek went, one that ended before not at all
    assert sr.stream_time(seg, 9 * Gst.SECOND, 2 * Gst.SECOND) == 100 * Gst.SECOND
    assert sr.stream_time(seg, 8 * Gst.SECOND, Gst.SECOND) is None
    assert sr.stream_time(seg, 12 * Gst.SECOND, Gst.SECOND) == 102 * Gst.SECOND


def test_cue_shadow_report():
    state = sr.State(sr.WEBVTT, on=True, track="text", data=6)
    sh = sr.CueShadow("text")
    assert sh.report(state) is None
    ms = 1_000_000
    for i, t in enumerate(("a", "b", "c")):
        sh.saw_old(t, i * 1000 * ms)
        sh.saw_new(t, i * 1000 * ms + 10 * ms)           # within the tolerance
    sh.saw_old("d", 3000 * ms)                           # still on its way
    sh.saw_new("", None)
    sh.saw_old("before the seek", None)                  # not shown: does not count
    assert sh.report(state) == ("WebVTT renderer (shadow, text): 3 cues, the old path 4; "
                                "data 6, dropped 0; the same cues at the same times")
    sh = sr.CueShadow("text")
    sh.saw_old("a", 0)
    sh.saw_old("b", 1000 * ms)
    sh.saw_new("a", 500 * ms)                            # the same text, half a second late
    sh.saw_new("b", 1000 * ms)
    assert "missing 'a', extra 'a'" in sh.report(state)
    # parsed ahead of the picture: only what was due up to the position counts
    sh = sr.CueShadow("text")
    for i in range(2):
        sh.saw_old(f"c{i}", i * 1000 * ms)
    for i in range(6):
        sh.saw_new(f"c{i}", i * 1000 * ms)
    assert "extra 'c2', 'c3', 'c4'" in sh.report(state)
    assert sh.report(state, until=1500 * ms).endswith(
        "2 cues, the old path 2; data 6, dropped 0; the same cues at the same times")


def _vtt_sample(Gst, text, start_s):
    """A WebVTT segment as hlsdemux2 hands it to playbin: one buffer, its
    own timestamp map."""
    data = ("WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:324000000,LOCAL:00:00:00.000\n\n"
            f"00:00:{start_s:02d}.000 --> 00:00:{start_s:02d}.800\n{text}\n\n").encode()
    return Gst.Buffer.new_wrapped(data)


def _feed_vtt(renderer, n=4):
    _GLib, Gst = streamkit.gst("subparse", "appsrc", "appsink")
    caps = Gst.Caps.from_string("application/x-subtitle-vtt")
    seg = Gst.Segment()
    seg.init(Gst.Format.TIME)
    for i in range(n):
        renderer.feed(_vtt_sample(Gst, f"{SUBTITLE} {i}", i), caps, seg)


def test_text_cue_renderer_decodes_cues():
    import threading
    streamkit.gst("subparse", "appsrc", "appsink")
    got, done = [], threading.Event()

    def on_cue(text, rt):
        got.append((text, rt))
        if len(got) == 4:
            done.set()
    r = sr.TextCueRenderer(on_cue)
    try:
        assert r.can(WEBVTT_TRACK) and not r.can(PAGE_150)
        r.show(WEBVTT_TRACK)
        _feed_vtt(r)
        assert done.wait(10), got
        assert [t for t, _rt in got] == [f"{SUBTITLE} {i}" for i in range(4)]
        times = [rt for _t, rt in got]
        assert all(t is not None for t in times) and times == sorted(times)
        assert times[1] - times[0] == 1_000_000_000      # the cue times, one second apart
        s = r.state()
        assert s.on and s.track == "text" and s.data == 4 and s.decoded == 4 and s.dropped == 0
        r.off()
        assert r.state() == sr.State(sr.WEBVTT)
    finally:
        r.close()


def test_text_cue_renderer_off_takes_in_no_data():
    streamkit.gst("subparse", "appsrc", "appsink")
    got = []
    r = sr.TextCueRenderer(lambda t, rt: got.append(t))
    try:
        _feed_vtt(r)
        time.sleep(0.5)
        assert got == [] and r.state() == sr.State(sr.WEBVTT)
    finally:
        r.close()


def test_a_standing_cue_renderer_never_blocks_the_feed():
    """The cue callback hangs: feeding goes on at once, the renderer drops."""
    import threading
    streamkit.gst("subparse", "appsrc", "appsink")
    stuck = threading.Event()
    r = sr.TextCueRenderer(lambda t, rt: stuck.wait(30), queue_buffers=2)
    try:
        r.show(WEBVTT_TRACK)
        t0 = time.monotonic()
        _feed_vtt(r, n=40)
        assert time.monotonic() - t0 < 2
        time.sleep(0.3)
        assert r.state().data == 40
    finally:
        stuck.set()
        r.close()


# -- text cues (TTML) ------------------------------------------------------------------

@pytest.fixture(scope="module")
def dash_ttml(tmp_path_factory):
    streamkit.gst("dashdemux", "ttmlparse", "ttmlrender")
    folder = tmp_path_factory.mktemp("dash-ttml")
    mpd = streamkit.make_dash_ttml(folder, seconds=16)
    srv = streamkit.Server(folder)
    yield srv.url(mpd.name)
    srv.close()


def test_ttml_subtitles_end_to_end(player, dash_ttml):
    """ARD, ZDF over DVB-I (DASH with EBU-TT-D in segments): picked, the
    subtitles are drawn by GStreamer's TTML renderer; with debug mode on,
    the text cue renderer read along with ttmlparse and had the same cues
    at the same times."""
    from gi.repository import Gst
    parsed = []

    def child(_overlay, element):
        f = element.get_factory()
        if f is not None and f.get_name() == "ttmlparse":
            element.get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, lambda _p, _i: (parsed.append(1), Gst.PadProbeReturn.OK)[1])

    def deep(_bin, _sub, element):
        f = element.get_factory()
        if f is not None and f.get_name() == "subtitleoverlay":
            element.connect("element-added", child)
    build = player._build

    def watched(*a, **k):
        pb = build(*a, **k)
        pb.connect("deep-element-added", deep)
        return pb
    player._build = watched
    player.play_uri(dash_ttml)
    _run(15, lambda: player.subtitle_tracks()[0] and player._played)
    assert len(player.subtitle_tracks()[0]) == 1 and player.subtitle_tracks()[1] == -1
    if player.log is not None:
        assert player._cue_shadow.state() == sr.State(sr.WEBVTT)   # off: reads nothing
    player.select_subtitle(0)
    _run(14, lambda: len(parsed) >= 4)
    assert len(parsed) >= 4                              # the old path draws them
    player.select_subtitle(-1)
    _run(1)
    player.stop()
    if player.log is not None:
        reports = [ln for ln in player.log if ln.startswith("TTML renderer (shadow")]
        assert len(reports) == 1, player.log
        assert reports[0].endswith("the same cues at the same times"), reports
        # the cues due up to where the picture was (ttmlparse is a cue ahead)
        assert int(reports[0].split(": ", 1)[1].split()[0]) >= 2, reports


def test_text_cue_renderer_decodes_ttml():
    import threading
    _GLib, Gst = streamkit.gst("ttmlparse", "subparse", "appsrc", "appsink")
    got, done = [], threading.Event()

    def on_cue(text, rt):
        got.append((text, rt))
        if len(got) == 3:
            done.set()
    r = sr.TextCueRenderer(on_cue)
    try:
        ttml = Track(SUB, sr.TTML, "deu", origin="text")
        assert r.can(ttml)
        r.show(ttml)
        seg = Gst.Segment()
        seg.init(Gst.Format.TIME)
        doc = streamkit.TTML_HEAD + "".join(streamkit.ttml_cue(i) for i in range(3)) + \
            streamkit.TTML_TAIL
        r.feed(Gst.Buffer.new_wrapped(doc.encode()),
               Gst.Caps.from_string("application/ttml+xml"), seg)
        assert done.wait(10), got
        assert r.parser == "ttmlparse"
        assert [t for t, _rt in got] == [f"{SUBTITLE} {i}" for i in range(3)]
        assert [rt for _t, rt in got] == [i * 1_000_000_000 for i in range(3)]
        s = r.state()
        assert s.kind == sr.TTML and s.data == 1 and s.decoded == 3
        # WebVTT after it: the pipeline is made again with subparse
        _feed_vtt(r, n=1)
        assert r.parser == "subparse"
    finally:
        r.close()
    assert r.pipeline is None


# -- DASH subtitles switched on during playback ----------------------------------------

@pytest.fixture(scope="module")
def dash_ttml_file(tmp_path_factory):
    streamkit.gst("dashdemux", "ttmlparse", "ttmlrender")
    folder = tmp_path_factory.mktemp("dash-ttml-file")
    mpd = streamkit.make_dash_ttml(folder, seconds=16, single_file=True)
    srv = streamkit.Server(folder)
    yield srv.url(mpd.name)
    srv.close()


def _ttml_cues_after_pick(player, url):
    """Play *url*, pick its subtitles after 2 s: (second the picture was at
    when picked, [seconds of the cues ttmlparse handed the renderer], seconds
    the first one took)."""
    from gi.repository import Gst
    cues, picked = [], [0.0]

    def child(_overlay, element):
        f = element.get_factory()
        if f is not None and f.get_name() == "ttmlparse":
            element.get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER,
                lambda _p, i: (cues.append((i.get_buffer().pts / Gst.SECOND, time.monotonic())),
                               Gst.PadProbeReturn.OK)[1])

    def deep(_bin, _sub, element):
        f = element.get_factory()
        if f is not None and f.get_name() == "subtitleoverlay":
            element.connect("element-added", child)
    build = player._build

    def watched(*a, **k):
        pb = build(*a, **k)
        pb.connect("deep-element-added", deep)
        return pb
    player._build = watched
    player.play_uri(url)
    _run(15, lambda: player.subtitle_tracks()[0] and player._played)
    _run(2)
    at = _position(player) / Gst.SECOND
    picked[0] = time.monotonic()
    player.select_subtitle(0)
    _run(6, lambda: len(cues) >= 3)
    first = cues[0][1] - picked[0] if cues else None
    return at, [round(c) for c, _t in cues], first


@pytest.mark.parametrize("sample", ["dash_ttml", "dash_ttml_file"])
def test_dash_subtitles_picked_later_come_at_once(player, sample, request):
    """ARD Mediathek, DVB-I (DASH with TTML in segments, or as one file for
    the whole programme): picked while it plays, the subtitles come from
    where the picture is, within a few seconds; before, one file never
    came and segments only after 7 to 10 s."""
    at, cues, first = _ttml_cues_after_pick(player, request.getfixturevalue(sample))
    assert cues, "no subtitles"
    assert first < 3, first
    assert cues[0] <= at + 1 and cues == sorted(cues), (at, cues)
    assert player.subtitle_tracks()[1] == 0
    pos = _position(player)
    _run(1)
    assert _position(player) > pos                      # the picture goes on
    before = len(cues)
    player.select_subtitle(0)                           # the same again: no new fetch
    assert len(cues) == before


class _FakePlaybin:
    def __init__(self, seekable):
        self.seekable, self.seeks = seekable, []

    def query_position(self, _fmt):
        return True, 5_000_000_000

    def query(self, q):
        from gi.repository import Gst
        q.set_seeking(Gst.Format.TIME, self.seekable, 0, 16_000_000_000)
        return True

    def seek(self, rate, _fmt, _flags, _st, pos, _et, _end):
        self.seeks.append((rate, pos))
        return True


def test_subtitles_fetched_again_only_for_dash(player):
    """Only DASH on classic playbin seeks, only when the shown text stream
    changes, and not where the stream cannot seek (live without a time
    shift window): there the subtitles come with the next segments, as
    before."""
    lines = []
    from lindvb import debuglog
    real_write = debuglog.write
    debuglog.write = lambda _c, *p: lines.append(" ".join(p))
    try:
        player.uri = "http://127.0.0.1/x.mpd"
        for dash, seekable, shown, key, seeks in (
                (True, True, None, 0, [(1.0, 5_000_000_000)]),
                (True, True, 0, 0, []),                    # already shown
                (True, True, 0, 1, [(1.0, 5_000_000_000)]),   # another track
                (False, True, None, 0, []),                # not DASH
                (True, False, None, 0, [])):               # cannot seek
            pb = _FakePlaybin(seekable)
            player.playbin, player._dash = pb, dash
            player._fetch_subtitles_again(shown, key)
            assert pb.seeks == seeks, (dash, seekable, shown, key)
        assert lines[-1] == "subtitles on: the stream cannot seek, they come with the next segments"
        player.rate = 1.5
        pb = _FakePlaybin(True)
        player.playbin, player._dash = pb, True
        player._fetch_subtitles_again(None, 0)
        assert pb.seeks == [(1.5, 5_000_000_000)]       # the play rate stays
    finally:
        debuglog.write = real_write
        player.playbin, player.uri, player.rate = None, None, 1.0
