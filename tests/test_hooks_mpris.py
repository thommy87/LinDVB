"""Script hooks and the MPRIS media-control interface."""

import os
import shutil
import subprocess
import sys

import pytest

from lindvb import hooks
from lindvb.channels import Channel


def test_hook_scripts_get_details(tmp_path):
    out = tmp_path / "out.txt"
    script = tmp_path / "recording-started.sh"
    script.write_text(f"#!/bin/sh\necho \"$LINDVB_EVENT|$LINDVB_CHANNEL|$LINDVB_TITLE|"
                      f"$LINDVB_FILE\" > {out}\n")
    script.chmod(0o755)
    (tmp_path / "recording-started.txt").write_text("not executable")
    assert hooks.scripts_for("recording-started", tmp_path) == [script]
    assert hooks.scripts_for("recording-stopped", tmp_path) == []
    ch = Channel(name="ZDF HD", sid=11110, tsid=1011, number=2)
    (p,) = hooks.run("recording-started", (ch, {"title": "Krimi", "file": "/v/k.ts"}), tmp_path)
    p.wait(5)
    assert out.read_text().strip() == "recording-started|ZDF HD|Krimi|/v/k.ts"


def test_hook_environment_for_reminder():
    from lindvb.reminders import Reminder
    env = hooks.environment("reminder", (Reminder("k", "arte", 1800000000, 60, "Doku"),))
    assert env == {"LINDVB_EVENT": "reminder", "LINDVB_CHANNEL": "arte",
                   "LINDVB_TITLE": "Doku", "LINDVB_START": "1800000000"}


def test_mpris_metadata():
    pytest.importorskip("gi")
    from lindvb.mpris import build_metadata
    md = build_metadata({"title": "Das Erste HD", "artist": "Tagesschau", "track": 3,
                         "art": "file:///logo.png", "length": 90.5})
    assert md["xesam:title"].unpack() == "Das Erste HD"
    assert md["xesam:artist"].unpack() == ["Tagesschau"]
    assert md["mpris:length"].unpack() == 90500000
    assert md["mpris:trackid"].unpack().endswith("/Track/3")


SERVER = r'''
import sys
sys.path.insert(0, sys.argv[1])
from gi.repository import GLib
from lindvb.mpris import MprisServer

class Player:
    playbin, paused, volume, rate = object(), False, 0.5, 1.0
    def position(self): return None
    def jump(self, s): calls.append(("jump", s))
    def seek_to(self, s): pass

class Win:
    player = Player()
    def mpris_status(self): return "Paused" if self.player.paused else "Playing"
    def player_pause(self):
        self.player.paused = not self.player.paused
        calls.append(("pause",))
    def step_item(self, n): calls.append(("step", n)); print("step", n, flush=True)
    def stop_playback(self): pass
    def present(self): pass

calls = []
srv = MprisServer(None, Win())
srv.update(title="ZDF HD", artist="heute")
GLib.timeout_add(8000, lambda: loop.quit())
loop = GLib.MainLoop()
loop.run()
'''


@pytest.mark.skipif(not shutil.which("dbus-run-session") or not shutil.which("gdbus"),
                    reason="needs dbus-run-session and gdbus")
def test_mpris_on_the_session_bus(tmp_path):
    pytest.importorskip("gi")
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (f"{sys.executable} {server} {root} & sleep 2; "
              "gdbus call --session -d org.mpris.MediaPlayer2.lindvb -o /org/mpris/MediaPlayer2 "
              "-m org.freedesktop.DBus.Properties.Get org.mpris.MediaPlayer2.Player Metadata; "
              "gdbus call --session -d org.mpris.MediaPlayer2.lindvb -o /org/mpris/MediaPlayer2 "
              "-m org.mpris.MediaPlayer2.Player.Next; sleep 1; kill %1")
    res = subprocess.run(["dbus-run-session", "--", "sh", "-c", script], capture_output=True,
                         text=True, timeout=30)
    assert "'xesam:title': <'ZDF HD'>" in res.stdout, res.stdout + res.stderr
    assert "step 1" in res.stdout, res.stdout + res.stderr


def test_pip_takes_internet_channels_only():
    pytest.importorskip("gi")
    try:
        from lindvb.ui.pip import can_show
    except (ImportError, ValueError):
        pytest.skip("GTK 4 missing")
    assert can_show(Channel(name="arte", delsys="iptv", url="https://example.org/a.m3u8"))
    assert not can_show(Channel(name="arte HD", delsys="dvb-s2", sid=10302))
    assert not can_show(None)
