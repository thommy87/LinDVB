"""Video Off (audio only) stays switched off when the pipeline is rebuilt."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
try:
    gi.require_version("Gtk", "4.0")
except ValueError:
    pytest.skip("GTK 4 missing", allow_module_level=True)
from gi.repository import Gst, Gtk  # noqa: E402

from lindvb.player import PLAY_FLAGS_VIDEO, Player  # noqa: E402

Gst.init(None)


@pytest.fixture
def player():
    if not Gtk.init_check():
        pytest.skip("no display")
    if Gst.ElementFactory.find("playbin") is None:
        pytest.skip("playbin missing")
    from lindvb.config import DEFAULTS
    p = Player(Gtk.Picture(), dict(DEFAULTS, deinterlace=False, video_decoder="software"))
    yield p
    p.stop()


def test_video_off_survives_rebuild(player):
    player.disable_video(True)
    pb = player._build()            # internet streams build a new pipeline per start
    assert not pb.get_property("flags") & PLAY_FLAGS_VIDEO
    player.disable_video(False)
    pb = player._build()
    assert pb.get_property("flags") & PLAY_FLAGS_VIDEO
