# SPDX-License-Identifier: GPL-2.0-or-later
"""The self-check every package runs in CI before a release."""

import io
import tomllib
from pathlib import Path

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
    for name in ("check_gtk", "check_elements", "check_playback", "check_hbbtv",
                 "check_tls", "check_stream"):
        monkeypatch.setattr(selfcheck, name, lambda r, n=name: calls.append(n))
    out = io.StringIO()
    assert selfcheck.run(["--offline"], out) == 0
    assert "check_tls" not in calls and "check_stream" not in calls
    calls.clear()
    assert selfcheck.run([], io.StringIO()) == 0
    assert "check_tls" in calls and "check_stream" in calls


def test_warn_does_not_fail():
    r = selfcheck.Report(io.StringIO())
    r.warn("x")
    assert not r.failed and "WARN  x" in r.out.getvalue()


def test_streams_are_https():
    assert selfcheck.STREAMS and all(u.startswith("https://") for u in selfcheck.STREAMS)
