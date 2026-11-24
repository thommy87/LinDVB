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
