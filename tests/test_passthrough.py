"""Audio passthrough: AC-3 / E-AC-3 / DTS undecoded to an AV receiver."""

import pytest

from lindvb import passthrough


def test_codecs_from_settings():
    assert passthrough.codecs({}) == []
    assert passthrough.codecs({"audio_passthrough": []}) == []
    assert passthrough.codecs({"audio_passthrough": ["dts", "AC3", "dts", "mp2"]}) == ["ac3", "dts"]
    assert passthrough.codecs({"audio_passthrough": "eac3, ac3"}) == ["ac3", "eac3"]


def test_caps_string():
    assert passthrough.caps_string([]) == "audio/x-raw"
    s = passthrough.caps_string(["ac3", "dts"])
    assert s.startswith("audio/x-raw; ") and "audio/x-ac3" in s and "audio/x-dts" in s
    assert "audio/x-raw" not in passthrough.caps_string(["eac3"], raw=False)


def test_sink_spec():
    assert passthrough.sink_spec("") == ("pulsesink", None)
    assert passthrough.sink_spec("  ") == ("pulsesink", None)
    assert passthrough.sink_spec("hdmi:CARD=PCH,DEV=0") == ("alsasink", "hdmi:CARD=PCH,DEV=0")
    assert passthrough.describe(["ac3", "dts"]) == "AC-3, DTS"


# -- with GStreamer -----------------------------------------------------------

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
try:
    gi.require_version("Gtk", "4.0")
except ValueError:
    pytest.skip("GTK 4 missing", allow_module_level=True)
from gi.repository import GLib, Gst, Gtk  # noqa: E402

Gst.init(None)


@pytest.fixture
def make_player(monkeypatch):
    if not Gtk.init_check():
        pytest.skip("no display")
    if Gst.ElementFactory.find("playbin") is None:
        pytest.skip("playbin missing")
    from lindvb.config import DEFAULTS
    from lindvb.player import Player
    # fakesink stands in for the receiver's output: it takes every format
    monkeypatch.setattr(passthrough, "sink_spec", lambda device: ("fakesink", None))
    players = []

    def make(**settings):
        p = Player(Gtk.Picture(), dict(DEFAULTS, deinterlace=False, video_decoder="software",
                                       **settings))
        players.append(p)
        return p
    yield make
    for p in players:
        p.stop()


def test_off_keeps_the_audio_filter(make_player):
    p = make_player()
    pb = p._build()
    assert pb.get_property("audio-filter") is not None
    assert p.passthrough_formats == ()


def test_on_sets_the_passthrough_sink(make_player):
    p = make_player(audio_passthrough=["ac3", "dts"])
    pb = p._build()
    sink = pb.get_property("audio-sink")
    assert sink is not None and sink.get_name() == "passthrough-sink"
    assert pb.get_property("audio-filter") is None and p.eq is None
    assert p.passthrough_formats == ("ac3", "dts")


def test_radio_is_decoded(make_player):
    p = make_player(audio_passthrough=["ac3"])
    p.radio = True
    pb = p._build()
    assert pb.get_property("audio-filter") is not None
    assert p.passthrough_formats == ()


def test_sink_without_the_formats_decodes(make_player, monkeypatch):
    from lindvb.player import Player
    monkeypatch.setattr(Player, "_sink_accepts", staticmethod(lambda sink, wanted: []))
    p = make_player(audio_passthrough=["ac3"])
    pb = p._build()
    assert pb.get_property("audio-filter") is not None
    assert p.passthrough_formats == ()


def _ac3_ts(tmp_path):
    for f in ("avenc_ac3", "ac3parse", "mpegtsmux", "avenc_mp2", "mpegaudioparse"):
        if Gst.ElementFactory.find(f) is None:
            pytest.skip(f"{f} missing")
    path = tmp_path / "ac3.ts"
    pipe = Gst.parse_launch(
        f"mpegtsmux name=m ! filesink location={path} "
        "audiotestsrc num-buffers=100 ! audio/x-raw,channels=6,rate=48000 ! audioconvert ! "
        "avenc_ac3 ! ac3parse ! m. "
        "audiotestsrc num-buffers=100 ! audioconvert ! avenc_mp2 ! mpegaudioparse ! m.")
    pipe.set_state(Gst.State.PLAYING)
    msg = pipe.get_bus().timed_pop_filtered(10 * Gst.SECOND,
                                            Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    assert msg is not None and msg.type == Gst.MessageType.EOS
    return path


@pytest.mark.parametrize("pb3", [False, True])
def test_ac3_reaches_the_sink_undecoded(make_player, tmp_path, pb3):
    """The AC-3 track arrives compressed, the MPEG track decoded, also after
    switching between them."""
    if pb3 and Gst.ElementFactory.find("playbin3") is None:
        pytest.skip("playbin3 missing")
    path = _ac3_ts(tmp_path)
    p = make_player(audio_passthrough=["ac3"])
    pb = p._build(pb3)
    sink = None
    it = pb.get_property("audio-sink").iterate_sinks()
    while True:
        res, el = it.next()
        if res != Gst.IteratorResult.OK:
            break
        sink = el
    seen = []

    def probe(_pad, info):
        ev = info.get_event()
        if ev.type == Gst.EventType.CAPS:
            seen.append(ev.parse_caps().get_structure(0).get_name())
        return Gst.PadProbeReturn.OK
    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, probe)
    errors = []
    p.connect("error", lambda _p, text: errors.append(text))
    pb.set_property("uri", Gst.filename_to_uri(str(path)))
    pb.set_state(Gst.State.PLAYING)
    ctx = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + 4_000_000
    while not seen and GLib.get_monotonic_time() < deadline:
        ctx.iteration(False)
    if not pb3:
        # switch to the other track and back: PCM <-> AC-3 in one pipeline
        cur = pb.get_property("current-audio")
        for i in (1 - cur, cur):
            n = len(seen)
            pb.set_property("current-audio", i)
            deadline = GLib.get_monotonic_time() + 4_000_000
            while len(seen) == n and GLib.get_monotonic_time() < deadline:
                ctx.iteration(False)
    pb.set_state(Gst.State.NULL)
    assert not errors
    assert "audio/x-ac3" in seen
    if not pb3:
        assert "audio/x-raw" in seen
