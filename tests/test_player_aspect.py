"""Anamorphic video keeps its picture shape (issue #122).

SD 16:9 channels send 720x576 with pixel-aspect-ratio 64/45 and some HD
channels 1440x1080 with 4/3. The software video output (appsink -> texture,
used when gtk4paintablesink is missing) must stretch them to square pixels,
or 16:9 SD shows squeezed to roughly 4:3.
"""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

try:
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # noqa: E402

    from lindvb import player as player_mod  # noqa: E402
except (ValueError, ImportError):
    pytest.skip("GTK 4 missing", allow_module_level=True)


@pytest.mark.parametrize("caps, size", [
    ("video/x-raw,width=720,height=576,pixel-aspect-ratio=64/45", (1024, 576)),
    ("video/x-raw,width=720,height=576,pixel-aspect-ratio=16/15", (768, 576)),
    ("video/x-raw,width=1440,height=1080,pixel-aspect-ratio=4/3", (1920, 1080)),
    ("video/x-raw,width=1920,height=1080,pixel-aspect-ratio=1/1", (1920, 1080)),
    ("video/x-raw,width=720,height=576", (720, 576)),      # no PAR: square pixels
])
def test_display_size(caps, size):
    assert player_mod.display_size(Gst.Caps.from_string(caps).get_structure(0)) == size


@pytest.mark.parametrize("w, h, par, out", [
    (720, 576, "64/45", (1024, 576)),
    (720, 576, "16/15", (768, 576)),
    (1440, 1080, "4/3", (1920, 1080)),
    (1280, 720, "1/1", (1280, 720)),
])
def test_software_output_applies_par(monkeypatch, w, h, par, out):
    if not Gtk.init_check():
        pytest.skip("no display")
    for name in ("videotestsrc", "videoscale", "appsink"):
        if Gst.ElementFactory.find(name) is None:
            pytest.skip(f"{name} missing")
    real_make = Gst.ElementFactory.make
    monkeypatch.setattr(player_mod.Gst.ElementFactory, "make",
                        lambda f, n=None: None if f == "gtk4paintablesink" else real_make(f, n))
    vb = player_mod.VideoBin(Gtk.Picture())
    monkeypatch.undo()

    pipe = Gst.Pipeline.new()
    src = Gst.ElementFactory.make("videotestsrc")
    src.set_property("num-buffers", 1)
    cf = Gst.ElementFactory.make("capsfilter")
    cf.set_property("caps", Gst.Caps.from_string(
        f"video/x-raw,width={w},height={h},pixel-aspect-ratio={par}"))
    for e in (src, cf, vb.sink):
        pipe.add(e)
    src.link(cf)
    cf.link(vb.sink)
    pipe.set_state(Gst.State.PLAYING)
    msg = pipe.get_bus().timed_pop_filtered(5 * Gst.SECOND,
                                            Gst.MessageType.EOS | Gst.MessageType.ERROR)
    try:
        assert msg is not None and msg.type == Gst.MessageType.EOS
        assert vb._pending is not None
        assert vb._pending[1:3] == out
    finally:
        pipe.set_state(Gst.State.NULL)
