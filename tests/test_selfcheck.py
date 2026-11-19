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
