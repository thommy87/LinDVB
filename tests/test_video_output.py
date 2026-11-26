"""gtk4paintablesink on trial in the AppImage and the Snap (concept §10).

The packages that bring the element themselves use it only when switched on;
the system packages use it whenever it is installed, as before. On trial it
needs OpenGL, and when it fails while playing the rest of the session copies
the frames again (the appsink path every package has used so far).
"""

import time
import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gtk  # noqa: E402

    from lindvb import player as player_mod  # noqa: E402
except (ValueError, ImportError):
    pytest.skip("GTK 4 missing", allow_module_level=True)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(player_mod, "_gl_output_failed", False)
    monkeypatch.delenv(player_mod.VIDEO_OUTPUT_ENV, raising=False)
    for var in ("FLATPAK_ID", "SNAP", "APPIMAGE", "APPDIR"):
        monkeypatch.delenv(var, raising=False)
    if player_mod.compat.bundled():
        pytest.skip("runs inside a package")


def _with_element(monkeypatch, present=True):
    real = Gst.ElementFactory.find
    monkeypatch.setattr(player_mod.Gst.ElementFactory, "find", lambda name: (
        (object() if present else None) if name == "gtk4paintablesink" else real(name)))


@pytest.mark.parametrize("package", ["", "Flatpak"])
def test_system_packages_use_the_element_as_before(monkeypatch, package):
    _with_element(monkeypatch)
    if package:
        monkeypatch.setenv("FLATPAK_ID", "io.github.lindvb")
    assert player_mod.gtk4_sink_wanted({"video_output_gl": False}) is True


@pytest.mark.parametrize("env, var", [("APPIMAGE", "/x.AppImage"), ("SNAP", "/snap/lindvb/1")])
def test_appimage_and_snap_only_when_switched_on(monkeypatch, env, var):
    _with_element(monkeypatch)
    monkeypatch.setenv(env, var)
    assert player_mod.gtk4_sink_wanted({}) is False                     # the default
    assert player_mod.gtk4_sink_wanted({"video_output_gl": False}) is False
    assert player_mod.gtk4_sink_wanted({"video_output_gl": True}) is True
    monkeypatch.setenv(player_mod.VIDEO_OUTPUT_ENV, "gl")
    assert player_mod.gtk4_sink_wanted({}) is True
    monkeypatch.setenv(player_mod.VIDEO_OUTPUT_ENV, "copy")
    assert player_mod.gtk4_sink_wanted({"video_output_gl": True}) is False


def test_never_without_the_element_or_after_it_failed(monkeypatch):
    _with_element(monkeypatch, present=False)
    assert player_mod.gtk4_sink_wanted({"video_output_gl": True}) is False
    _with_element(monkeypatch)
    player_mod.gl_output_failed()
    assert player_mod.gtk4_sink_wanted({"video_output_gl": True}) is False


def test_switched_off_copies_the_frames():
    if not Gtk.init_check():
        pytest.skip("no display")
    vb = player_mod.VideoBin(Gtk.Picture(), False)
    assert vb.kind == "copy" and not vb.trial
    assert any(e.get_factory().get_name() == "appsink" for e in player_mod._sink_candidates(vb.sink)
               if e.get_factory() is not None)


def _fake_gtk4(monkeypatch, gl_context):
    """gtk4paintablesink stand-in: an identity whose paintable has *gl-context*."""
    paintable = types.SimpleNamespace(
        find_property=lambda name: name == "gl-context",
        get_property=lambda name: gl_context)
    real_make = Gst.ElementFactory.make

    def make(factory, name=None):
        if factory != "gtk4paintablesink":
            return real_make(factory, name)
        el = real_make("fakesink", name)
        return types.SimpleNamespace(
            el=el, get_property=lambda prop: paintable if prop == "paintable" else None)
    monkeypatch.setattr(player_mod.Gst.ElementFactory, "make", make)


def test_on_trial_without_opengl_copies_the_frames(monkeypatch):
    if not Gtk.init_check():
        pytest.skip("no display")
    monkeypatch.setenv("APPIMAGE", "/x.AppImage")
    _fake_gtk4(monkeypatch, None)
    vb = player_mod.VideoBin(Gtk.Picture(), True)
    assert vb.kind == "copy" and not vb.trial


def test_the_trial_sink_needs_a_gl_context(monkeypatch):
    if not Gtk.init_check():
        pytest.skip("no display")
    if Gst.ElementFactory.find("glsinkbin") is None:
        pytest.skip("glsinkbin missing")
    monkeypatch.setenv("APPIMAGE", "/x.AppImage")
    _fake_gtk4(monkeypatch, object())
    vb = player_mod.VideoBin.__new__(player_mod.VideoBin)
    vb.trial = False
    sink = Gst.ElementFactory.make("gtk4paintablesink")
    assert vb._trial_sink(sink) is sink and vb.trial


def _error_from(el, dbg="some error"):
    return Gst.Message.new_error(el, GLib.Error("failed"), dbg)


def _video(trial):
    sink = Gst.Bin.new("glsinkbin-like")
    inner = Gst.ElementFactory.make("fakesink", "inner")
    sink.add(inner)
    return types.SimpleNamespace(trial=trial, sink=sink, picture=None), inner


def test_errors_of_the_trial_output_are_recognised():
    video, inner = _video(True)
    other = Gst.ElementFactory.make("identity", "decoder")
    assert player_mod.trial_output_error(video, _error_from(inner), "x")
    assert player_mod.trial_output_error(video, _error_from(video.sink), "x")
    # a picture it refused shows as not-negotiated from upstream
    assert player_mod.trial_output_error(
        video, _error_from(other), "streaming stopped, reason not-negotiated (-4)")
    assert not player_mod.trial_output_error(video, _error_from(other), "x")
    video.trial = False                  # the output every package had: unchanged
    assert not player_mod.trial_output_error(video, _error_from(inner), "x")


def test_player_falls_back_to_copying_once(monkeypatch):
    video, inner = _video(True)
    later = []
    monkeypatch.setattr(player_mod.GLib, "idle_add", lambda *a: later.append(a))
    made = []
    monkeypatch.setattr(player_mod, "VideoBin", lambda pic, gtk4: made.append(gtk4) or
                        types.SimpleNamespace(trial=False, sink=None, picture=pic))
    stub = types.SimpleNamespace(video=video, _token=3, _video_error_token=-1,
                                 _video_fallback=lambda token: None)
    msg = _error_from(inner)
    assert player_mod.Player._video_output_failed(stub, msg, GLib.Error("x"), "x")
    assert made == [False] and stub._video_error_token == 3 and player_mod._gl_output_failed
    assert len(later) == 1 and later[0][1] == 3
    # more errors of the pipeline being replaced are swallowed, not reported
    assert player_mod.Player._video_output_failed(stub, msg, GLib.Error("x"), "x")
    assert len(later) == 1
    # the next pipeline (copying) reports its errors as before
    stub._token = 4
    assert not player_mod.Player._video_output_failed(stub, msg, GLib.Error("x"), "x")


def test_the_fallback_plays_on_and_says_so():
    calls = []
    stub = types.SimpleNamespace(_token=5, capture=None, uri="https://x/a.m3u8",
                                 restart=lambda: calls.append("restart"),
                                 emit=lambda *a: calls.append(a[0]))
    assert player_mod.Player._video_fallback(stub, 5) is False
    assert calls == ["restart", "notice"]
    calls.clear()
    player_mod.Player._video_fallback(stub, 4)         # another channel meanwhile
    assert calls == []


def test_the_real_element_shows_pictures_with_opengl(monkeypatch):
    """With the element built (packaging/build-gtk4sink.sh) and OpenGL: the
    trial path is glsinkbin and the paintable gets the frames' size."""
    if Gst.ElementFactory.find("gtk4paintablesink") is None:
        pytest.skip("gtk4paintablesink is not installed")
    if not Gtk.init_check():
        pytest.skip("no display")
    monkeypatch.setenv("APPIMAGE", "/x.AppImage")
    picture = Gtk.Picture()
    vb = player_mod.VideoBin(picture, True)
    if not vb.trial:
        pytest.skip("no OpenGL here")
    assert vb.kind == "gtk4paintablesink (OpenGL)"
    pipe = Gst.parse_launch("videotestsrc num-buffers=3 ! video/x-raw,width=160,height=120 "
                            "! identity name=last")
    pipe.add(vb.sink)
    pipe.get_by_name("last").link(vb.sink)
    pipe.set_state(Gst.State.PLAYING)
    paintable = picture.get_paintable()
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and paintable.get_intrinsic_width() <= 0:
        ctx.iteration(False)
        time.sleep(0.01)
    size = paintable.get_intrinsic_width(), paintable.get_intrinsic_height()
    pipe.set_state(Gst.State.NULL)
    assert size == (160, 120)


def test_self_check_shows_the_sample_through_it():
    """--self-check decodes its H.264 sample into the element with OpenGL
    (the AppImage has no videotestsrc); its CI requires the OK line."""
    if Gst.ElementFactory.find("gtk4paintablesink") is None:
        pytest.skip("gtk4paintablesink is not installed")
    if not Gtk.init_check():
        pytest.skip("no display")
    import io
    from lindvb import selfcheck
    out = io.StringIO()
    selfcheck.check_video_output(selfcheck.Report(out))
    if out.getvalue().startswith("WARN"):
        pytest.skip("no OpenGL here")
    assert out.getvalue().startswith(
        "OK    video output through the graphics card (gtk4paintablesink, OpenGL): 320x180"), \
        out.getvalue()
