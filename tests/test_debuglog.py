"""Debug mode (Preferences ▸ Debug): the log of what LinDVB does."""

import sys
import threading
from pathlib import Path

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
    monkeypatch.setattr(debuglog, "MAX_BYTES", 500)
    debuglog.configure({"debug_mode": True}, log_path=log)
    for i in range(30):
        debuglog.write("Test", f"line {i:02d} " + "y" * 30)
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
