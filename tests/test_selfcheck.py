# SPDX-License-Identifier: GPL-2.0-or-later
"""The self-check every package runs in CI before a release."""

import io
import tomllib
from pathlib import Path

import pytest

from lindvb import selfcheck

ROOT = Path(__file__).resolve().parent.parent


def test_sample_is_a_transport_stream():
    data = selfcheck.SAMPLE.read_bytes()
    assert 10_000 < len(data) < 200_000
    assert len(data) % 188 == 0
    assert all(data[i] == 0x47 for i in range(0, len(data), 188))


def test_sample_is_packaged():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "selfcheck.ts" in meta["tool"]["setuptools"]["package-data"]["lindvb"]


def test_report_fails_once_anything_fails():
    out = io.StringIO()
    r = selfcheck.Report(out)
    r.ok("a")
    assert not r.failed
    r.fail("b")
    r.ok("c")
    assert r.failed
    assert out.getvalue().splitlines() == ["OK    a", "FAIL  b", "OK    c"]


def test_every_package_runs_it():
    wf = ROOT / ".github" / "workflows"
    for name in ("appimage.yml", "packages.yml", "flatpak.yml", "aur.yml", "snap.yml"):
        assert "--self-check" in (wf / name).read_text(), name


def test_offline_skips_network_checks(monkeypatch):
    calls = []
    for name in ("check_gtk", "check_preferences", "check_elements", "check_playback",
                 "check_hbbtv", "check_tls", "check_proxy", "check_stream", "check_iptv"):
        monkeypatch.setattr(selfcheck, name, lambda r, n=name: calls.append(n))
    out = io.StringIO()
    assert selfcheck.run(["--offline"], out) == 0
    assert not {"check_tls", "check_proxy", "check_stream", "check_iptv"} & set(calls)
    calls.clear()
    assert selfcheck.run([], io.StringIO()) == 0
    assert {"check_tls", "check_proxy", "check_stream", "check_iptv"} <= set(calls)


def _isolated(monkeypatch, tmp_path):
    from lindvb import config
    from lindvb.channels import ChannelDB
    monkeypatch.setattr(config.Settings, "path", tmp_path / "settings.json")
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")


def test_preferences_are_opened(monkeypatch, tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display")
    _isolated(monkeypatch, tmp_path)
    r = selfcheck.Report(io.StringIO())
    selfcheck.check_preferences(r)
    assert not r.failed and "OK    preferences open (" in r.out.getvalue()
    assert not (tmp_path / "settings.json").exists()          # nothing written


def test_preferences_that_do_not_open_fail_the_check(monkeypatch, tmp_path):
    """What 0.22.0 shipped with: a NameError while the window is built."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display")
    _isolated(monkeypatch, tmp_path)
    from lindvb.ui import dialogs

    def broken(self, *a):
        raise NameError("name 'folder' is not defined")
    monkeypatch.setattr(dialogs.PrefsWindow, "__init__", broken)
    for name in ("check_gtk", "check_elements", "check_playback", "check_hbbtv"):
        monkeypatch.setattr(selfcheck, name, lambda r: None)
    out = io.StringIO()
    assert selfcheck.run(["--offline"], out) == 1
    assert "FAIL  check_preferences: NameError: name 'folder' is not defined" in out.getvalue()


def test_warn_does_not_fail():
    r = selfcheck.Report(io.StringIO())
    r.warn("x")
    assert not r.failed and "WARN  x" in r.out.getvalue()


def test_streams_are_https():
    assert selfcheck.STREAMS and all(u.startswith("https://") for u in selfcheck.STREAMS)


def test_snap_uses_libproxy_resolver():
    """A snap must not ask the desktop portal for proxies (it refuses them)."""
    text = (ROOT / "snap" / "snapcraft.yaml").read_text()
    assert "GIO_USE_PROXY_RESOLVER: libproxy" in text
    assert "- network-status" in text


def test_appimage_bundles_the_checked_plugins():
    """The AppImage copies plugins by name: those of the elements the
    self-check asks for beyond playback (0.20.2 lacked level, so the loudness
    meter was dead, and debug and closedcaption)."""
    text = (ROOT / "packaging" / "build-appdir.sh").read_text()
    plugins = text.split('PLUGINS="', 1)[1].split('"', 1)[0].split()
    for element, plugin in (("level", "level"), ("capssetter", "debug"),
                            ("aacparse", "audioparsers")):
        assert element in selfcheck.ELEMENTS
        assert plugin in plugins, plugin
    assert selfcheck.CC_OVERLAY == "cc708overlay"
    assert "closedcaption" in plugins


def test_silence_is_noticed():
    assert selfcheck.silent({"audio": 10, "peak": 0})
    assert selfcheck.silent({"audio": 10, "peak": 1})            # about -90 dBFS
    assert not selfcheck.silent({"audio": 10, "peak": 3000})     # about -21 dBFS
    assert not selfcheck.silent({"audio": 0, "peak": 0})         # no sound at all: elsewhere


def test_missing_optional_elements_only_warn(monkeypatch):
    """Without textoverlay (text subtitles) or gtk4paintablesink LinDVB still
    plays: the self-check says what is lost, and does not fail."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    find = Gst.ElementFactory.find
    monkeypatch.setattr(Gst.ElementFactory, "find",
                        lambda name: None if name in selfcheck.OPTIONAL_ELEMENTS else find(name))
    r = selfcheck.Report(io.StringIO())
    selfcheck.check_elements(r)
    out = r.out.getvalue()
    assert "WARN  GStreamer element textoverlay is missing: text subtitles" in out
    assert "WARN  GStreamer element gtk4paintablesink is missing" in out
    assert not any(line.startswith("FAIL") and n in line for line in out.splitlines()
                   for n in selfcheck.OPTIONAL_ELEMENTS)


@pytest.mark.parametrize("bundled,fails", [("Snap", True), ("AppImage", True),
                                           ("Flatpak", False), ("", False)])
def test_text_subtitles_are_required_where_lindvb_brings_them(monkeypatch, bundled, fails):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    from lindvb import compat
    Gst.init(None)
    find = Gst.ElementFactory.find
    monkeypatch.setattr(Gst.ElementFactory, "find",
                        lambda name: None if name == "textoverlay" else find(name))
    monkeypatch.setattr(compat, "bundled", lambda: bundled)
    r = selfcheck.Report(io.StringIO())
    selfcheck.check_elements(r)
    line = next(ln for ln in r.out.getvalue().splitlines() if "textoverlay" in ln)
    assert line.startswith("FAIL" if fails else "WARN")
