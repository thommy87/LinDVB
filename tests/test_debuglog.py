"""Debug mode (Preferences ▸ Debug): the log of what LinDVB does."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lindvb import debuglog


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(debuglog, "ENV", False)
    path = tmp_path / "debug.log"
    debuglog.configure({}, log_path=path)          # off, writing to tmp_path
    yield path
    debuglog.configure({})
    debuglog._path = None


def test_off_writes_nothing(log):
    debuglog.write("Test", "hello")
    assert not log.exists()
    assert not debuglog.enabled()


def test_header_and_lines_without_personal_data(log):
    home = str(Path.home())
    settings = {"debug_mode": True, "hbbtv_pin": "geheim", "hbbtv_pin_salt": "salz",
                "hbbtv_device_uuid": "1234", "recording_dir": home + "/Videos/LinDVB",
                "tuners": [{"adapter": 0}], "timeshift_mode": "disk"}
    debuglog.configure(settings, log_path=log)
    assert debuglog.enabled()
    debuglog.write("Player", f"play file / address file://{home}/Videos/a.ts")
    debuglog.write("Player", "play live stream http://alex:topsecret@iptv.example/live.m3u8"
                             "?token=abc123&channel=7")
    text = log.read_text(encoding="utf-8")
    assert "LinDVB" in text and "debug mode switched on" in text and "Python" in text
    assert "timeshift_mode='disk'" in text and "tuners='<1 entries>'" in text
    assert "geheim" not in text and "salz" not in text and "1234" not in text
    assert home not in text and "~/Videos/a.ts" in text
    assert "topsecret" not in text and "abc123" not in text
    assert "http://***@iptv.example/live.m3u8?token=***&channel=7" in text


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_terminal_output_and_exceptions_are_captured(log, capsys):
    debuglog.configure({"debug_mode": True}, log_path=log)
    print("Stream analysis: something odd")
    try:
        raise ValueError("kaputt")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    t = threading.Thread(target=lambda: 1 / 0, name="worker")
    t.start()
    t.join()
    text = log.read_text(encoding="utf-8")
    assert "Terminal: Stream analysis: something odd" in text
    assert "ValueError: kaputt" in text
    assert "in thread worker" in text and "ZeroDivisionError" in text
    assert "something odd" in capsys.readouterr().out      # the terminal still gets it
    debuglog.configure({}, log_path=log)
    assert not isinstance(sys.stdout, debuglog._Tee)
    assert sys.excepthook is not debuglog._excepthook


def test_size_is_limited(log, monkeypatch):
    monkeypatch.setattr(debuglog, "MAX_BYTES", 2000)
    debuglog.configure({"debug_mode": True}, log_path=log)
    for i in range(200):
        debuglog.write("Test", f"line {i} " + "x" * 50)
    older = log.with_name("debug.log.1")
    assert older.exists()
    assert older.stat().st_size < 2200 and (not log.exists() or log.stat().st_size < 2200)
    assert "line 199" in (log.read_text() if log.exists() else older.read_text())


def test_save_as_and_clear(log, tmp_path, monkeypatch):
    # room for the header that clear() writes anew (it grew by the open files
    # limit), small enough that the 30 lines rotate once
    monkeypatch.setattr(debuglog, "MAX_BYTES", 800)
    debuglog.configure({"debug_mode": True}, log_path=log)
    for i in range(30):
        debuglog.write("Test", f"line {i:02d} " + "y" * 30)
    assert log.with_name("debug.log.1").exists()
    target = tmp_path / "copy.log"
    debuglog.save_as(target)
    copy = target.read_text(encoding="utf-8")
    assert copy.index("line 2") < copy.index("line 29")      # older file first
    debuglog.clear()
    assert not log.with_name("debug.log.1").exists()
    assert "log cleared" in log.read_text(encoding="utf-8")


def test_gstreamer_warnings_land_in_the_log(log):
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    debuglog.configure({"debug_mode": True}, log_path=log)
    src = Gst.ElementFactory.make("filesrc")
    src.set_property("location", "/nonexistent/lindvb-test.ts")
    src.set_state(Gst.State.PAUSED)
    src.set_state(Gst.State.NULL)
    text = log.read_text(encoding="utf-8")
    assert "GStreamer" in text and "lindvb-test.ts" in text, text


def test_hbbtv_diagnostic_log_becomes_debug_mode(tmp_path, monkeypatch):
    from lindvb import config
    path = tmp_path / "settings.json"
    path.write_text('{"hbbtv_debug_log": true}')
    monkeypatch.setattr(config.Settings, "path", path)
    s = config.Settings()
    assert s["debug_mode"] is True and "hbbtv_debug_log" not in s
    path.write_text("{}")
    assert config.Settings()["debug_mode"] is False


def test_describe_channel():
    from lindvb.channels import Channel
    c = Channel("Das Erste HD", delsys="dvb-s2", frequency=11494000, polarity="h",
                symbol_rate=22000, sid=10301, tsid=1019, onid=1)
    d = debuglog.describe_channel(c)
    assert "'Das Erste HD'" in d and "11494000 kHz h" in d and "sid 10301" in d
    assert debuglog.describe_channel(None) == "-"


def test_open_files_are_counted_by_kind(log, tmp_path, monkeypatch):
    import os
    assert debuglog.open_files_summary() == "", "debug mode off: nothing"
    debuglog.configure({"debug_mode": True}, log_path=log)
    assert debuglog.open_files_summary().startswith("open files ")
    fds = tmp_path / "fd"
    fds.mkdir()
    links = ["socket:[1]", "socket:[2]", "pipe:[3]", "anon_inode:[eventfd]",
             "/dev/dri/renderD128", "/dev/snd/pcmC0D0p"] + ["/tmp/x.ts"] * 3
    for i, target in enumerate(links):
        os.symlink(target, fds / str(i))
    out = debuglog.open_files_summary(str(fds))
    assert out.startswith("open files 9/")
    assert "socket 2" in out and "pipe 1" in out and "eventfd 1" in out
    assert "dri 1" in out and "snd 1" in out and "file 3" in out
    assert out.endswith("3x /tmp/x.ts")
    monkeypatch.setattr(debuglog, "_on", False)
    assert debuglog.open_files_summary(str(fds)) == ""


def test_graphics_buffers_are_counted_by_driver(log, tmp_path):
    """Alex's log: 578 of 670 open files were graphics buffers (/dmabuf:),
    piling up while watching; the summary names the driver that made them."""
    import os
    debuglog.configure({"debug_mode": True}, log_path=log)
    fds, infos = tmp_path / "fd", tmp_path / "fdinfo"
    fds.mkdir()
    infos.mkdir()
    for i in range(3):
        os.symlink("/dmabuf:", fds / str(i))
        (infos / str(i)).write_text(f"pos:\t0\nflags:\t02\nsize:\t{40_000_000}\n"
                                    "count:\t1\nexp_name:\ti915\n")
    os.symlink("/dmabuf:", fds / "3")           # no fdinfo: the driver unknown
    os.symlink("socket:[7]", fds / "4")
    out = debuglog.open_files_summary(str(fds))
    assert out.startswith("open files 5/")
    assert "dmabuf 4" in out and "socket 1" in out and "file 1" not in out
    assert out.endswith("; graphics buffers i915 3 (120 MB), ? 1 (0 MB); sizes 3x 40 MB, 1x 0 KB")


def test_graphics_buffer_sizes_tell_pictures_from_small_buffers():
    """Alex's logs: ~300 graphics buffers of only 7 MB together; the three most
    frequent sizes show what kind they are."""
    part = debuglog._dmabuf_part({"i915": [303, 300 * 24_576 + 3 * 3_110_400]},
                                 {24_576: 300, 3_110_400: 3})
    assert part == "; graphics buffers i915 303 (17 MB); sizes 300x 25 KB, 3x 3 MB"


def test_pipelines_and_gpu_elements_are_counted_until_they_go(log, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    import gc
    monkeypatch.setattr(debuglog, "_alive", {})
    el = Gst.ElementFactory.make("identity")
    debuglog.track(el, "pipeline")
    assert debuglog.alive_summary() == "", "debug mode off: nothing counted"
    debuglog.configure({"debug_mode": True}, log_path=log)
    debuglog.track(el, "pipeline")
    other = Gst.ElementFactory.make("identity")
    debuglog.track(other, "vah264dec")
    assert debuglog.alive_summary() == "alive: pipeline 1, vah264dec 1"
    del el
    gc.collect()
    assert debuglog.alive_summary() == "alive: vah264dec 1"
    del other
    gc.collect()
    assert debuglog.alive_summary() == ""


def test_the_player_counts_only_gpu_elements(log, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    try:
        from lindvb import player
    except (ValueError, ImportError):
        pytest.skip("GTK 4 missing")
    monkeypatch.setattr(debuglog, "_alive", {})
    debuglog.configure({"debug_mode": True}, log_path=log)

    def element(plugin, name):
        factory = SimpleNamespace(get_plugin_name=lambda: plugin, get_name=lambda: name)
        return SimpleNamespace(get_factory=lambda: factory, weak_ref=lambda cb: None)
    for plugin, name in (("va", "vah264dec"), ("va", "vadeinterlace"), ("nvcodec", "nvh264dec"),
                         ("libav", "avdec_h264"), ("coreelements", "queue")):
        player._track_gpu_element(None, None, element(plugin, name))
    assert debuglog.alive_summary() == "alive: nvh264dec 1, vadeinterlace 1, vah264dec 1"



def test_window_logs_the_open_files_when_their_count_changes(log, monkeypatch):

    import gi
    for lib, version in (("Gst", "1.0"), ("Gtk", "4.0"), ("Gdk", "4.0")):
        try:
            gi.require_version(lib, version)
        except ValueError:
            pytest.skip(f"{lib} {version} missing")
    from lindvb.ui.window import MainWindow
    debuglog.configure({"debug_mode": True}, log_path=log)
    monkeypatch.setattr(debuglog, "_alive", {})
    win = SimpleNamespace(_open_files_logged="", player=SimpleNamespace(
        video_decoder=("vah264dec", "vaapi"), video_deinterlacer="vadeinterlace"))
    looks = iter(["open files 10/1024: dmabuf 4", "open files 10/1024: dmabuf 4",
                  "open files 11/1024: dmabuf 5"])
    monkeypatch.setattr(debuglog, "open_files_summary", lambda: next(looks))
    for _ in range(3):
        assert MainWindow._log_open_files(win) is True
    lines = [line for line in log.read_text().splitlines() if " Diag: " in line]
    assert len(lines) == 2
    assert lines[0].endswith("open files 10/1024: dmabuf 4; video decoder vah264dec, "
                             "deinterlacer vadeinterlace")
    assert "open files 11/1024" in lines[1]
    # the same files, but a pipeline more alive: logged too
    monkeypatch.setattr(debuglog, "open_files_summary", lambda: "open files 11/1024: dmabuf 5")
    monkeypatch.setattr(debuglog, "_alive", {"pipeline": 2})
    MainWindow._log_open_files(win)
    lines = [line for line in log.read_text().splitlines() if " Diag: " in line]
    assert len(lines) == 3 and lines[2].endswith("; alive: pipeline 2")


def test_an_element_reported_twice_is_counted_once(log, monkeypatch):
    """Alex's log said "vadeinterlace 2" with one pipeline: deep-element-added
    names the elements of the video filter again when playsink takes it in."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    import gc
    monkeypatch.setattr(debuglog, "_alive", {})
    monkeypatch.setattr(debuglog, "_tracked", set())
    debuglog.configure({"debug_mode": True}, log_path=log)
    el = Gst.ElementFactory.make("identity")
    debuglog.track(el, "vadeinterlace")
    debuglog.track(el, "vadeinterlace")
    assert debuglog.alive_summary() == "alive: vadeinterlace 1"
    del el
    gc.collect()
    assert debuglog.alive_summary() == ""


def test_pipelines_of_the_same_name_are_counted_apart(log, monkeypatch):
    """Every pipeline is called "player": the new one, made while the old one
    still goes, was not counted (the log of 26.11. showed no pipeline)."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    import gc
    monkeypatch.setattr(debuglog, "_alive", {})
    monkeypatch.setattr(debuglog, "_tracked", set())
    debuglog.configure({"debug_mode": True}, log_path=log)
    old = Gst.ElementFactory.make("playbin", "player")
    debuglog.track(old, "pipeline")
    new = Gst.ElementFactory.make("playbin", "player")
    debuglog.track(new, "pipeline")
    assert debuglog.alive_summary() == "alive: pipeline 2"
    del old
    gc.collect()
    assert debuglog.alive_summary() == "alive: pipeline 1"
    del new
    gc.collect()
    assert debuglog.alive_summary() == ""


def test_settings_changed_in_preferences_are_named():
    before = {"video_decoder": "auto", "deinterlace": True, "tvh_password": "a",
              "iptv_sources": [1]}
    after = {"video_decoder": "software", "deinterlace": True, "tvh_password": "b",
             "iptv_sources": [1, 2], "video_output_gl": True}
    assert debuglog.settings_changes(before, after) == (
        "iptv_sources <1 entries> -> <2 entries>, video_decoder 'auto' -> 'software', "
        "video_output_gl None -> True")
    assert debuglog.settings_changes(after, dict(after)) == ""


def test_the_change_lands_in_the_log(log):
    debuglog.configure({"debug_mode": True}, log_path=log)
    debuglog.log_settings_changes({"deinterlace": True}, {"deinterlace": True})
    debuglog.log_settings_changes({"video_decoder": "auto"}, {"video_decoder": "vaapi"})
    text = log.read_text(encoding="utf-8")
    assert text.count("settings changed") == 1
    assert "LinDVB: settings changed: video_decoder 'auto' -> 'vaapi'" in text


def test_gpu_displays_shared_by_context_are_counted(log, monkeypatch):
    """The VA-API display holds the driver's resources: is one kept while
    the pipelines go?"""
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    try:
        from lindvb import player
    except (ValueError, ImportError):
        pytest.skip("GTK 4 missing")
    monkeypatch.setattr(debuglog, "_alive", {})
    monkeypatch.setattr(debuglog, "_tracked", set())
    debuglog.configure({"debug_mode": True}, log_path=log)
    display = Gst.ElementFactory.make("identity")     # stands in for a GstVaDisplay

    def message(ctx_type, field):
        st = Gst.Structure.new_empty("context")
        st.set_value(field, display)
        ctx = SimpleNamespace(get_context_type=lambda: ctx_type, get_structure=lambda: st)
        return SimpleNamespace(parse_have_context=lambda: ctx)
    player._track_gpu_context(None, message("gst.va.display.handle", "gst-display"))
    player._track_gpu_context(None, message("gst.va.display.handle", "gst-display"))
    player._track_gpu_context(None, message("some.other", "x"))
    assert debuglog.alive_summary() == "alive: VA display 1"


def test_the_contexts_the_video_sink_holds(monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    try:
        from lindvb import player
    except (ValueError, ImportError):
        pytest.skip("GTK 4 missing")
    sink = Gst.Bin.new("videoout")
    inner = Gst.ElementFactory.make("fakesink")
    sink.add(inner)
    assert player.held_contexts(sink) == []
    inner.set_context(Gst.Context.new("gst.va.display.handle", True))
    assert player.held_contexts(sink) == ["gst.va.display.handle"]
