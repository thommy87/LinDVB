"""Channel search, tuning and EPG against the kernel's virtual DVB tuner (vidtv).

vidtv ("sudo modprobe vidtv") simulates a DVB-T/T2/C/S/S2 frontend that sends
one radio service, "Beethoven", with PAT/PMT/SDT/EIT. These tests run the same
code paths as a real tuner: dvbv5-scan with LinDVB's scan files, the capture
pipeline (dvbsrc ! tsparse ! multifilesink) and the DVB EIT parser. They are
skipped when no vidtv adapter is present; see docs/TESTING.md.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from lindvb.channels import parse_dvbv5
from lindvb.scan import (BUNDLED_TABLES, find_adapters, is_virtual_adapter,
                         load_transponder_list, scan_command)

VIDTV = [(a, f) for a, f, _ in find_adapters() if is_virtual_adapter(a, f)]
pytestmark = pytest.mark.skipif(not VIDTV, reason="no vidtv adapter (sudo modprobe vidtv)")


def scan(table, tmp_path, lnb=""):
    adapter, frontend = VIDTV[0]
    if not shutil.which("dvbv5-scan"):
        pytest.skip("dvbv5-scan is missing")
    tpl = load_transponder_list(BUNDLED_TABLES / table)
    out = tmp_path / "channels.conf"
    cmd = scan_command(adapter, frontend, tpl.path, str(out), lnb=lnb)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert out.exists(), proc.stdout + proc.stderr
    return parse_dvbv5(out.read_text(errors="replace"))


@pytest.mark.parametrize("table", ["vidtv-dvbt", "vidtv-dvbc"])
def test_scan_finds_the_virtual_service(table, tmp_path):
    chans = scan(table, tmp_path)
    names = [c.name.lower() for c in chans]
    assert any("beethoven" in n for n in names), names
    ch = next(c for c in chans if "beethoven" in c.name.lower())
    assert ch.sid and ch.audio_pids and ch.frequency == 474000


def test_tune_capture_and_epg(tmp_path, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    if Gst.ElementFactory.find("dvbsrc") is None:
        pytest.skip("dvbsrc is missing (gst-plugins-bad)")
    from lindvb.config import DEFAULTS
    from lindvb.tuner import Capture

    ch = next(c for c in scan("vidtv-dvbt", tmp_path) if "beethoven" in c.name.lower())
    settings = dict(DEFAULTS, timeshift_dir=str(tmp_path / "ts"), timeshift_max_mb=256)
    adapter, frontend = VIDTV[0]
    cap = Capture(ch, adapter, frontend, settings)
    events, errors = [], []
    cap.connect("eit", lambda _c, key, evs: events.extend(evs))
    cap.connect("error", lambda _c, msg: errors.append(msg))
    cap.start()
    loop = GLib.MainLoop()
    deadline = time.monotonic() + 15

    def check():
        _, end = cap.store.bounds()
        if errors or (end > 200_000 and events) or time.monotonic() > deadline:
            loop.quit()
            return False
        return True

    GLib.timeout_add(200, check)
    loop.run()
    _, received = cap.store.bounds()
    cap.destroy()
    assert not errors, errors
    assert received > 200_000, f"only {received} bytes from the virtual tuner"
    assert events, "no EIT events from the virtual tuner"
