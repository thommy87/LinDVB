# SPDX-License-Identifier: GPL-2.0-or-later
"""The check for a newer release (lindvb.updatecheck, issue #206): only
where nothing else updates LinDVB, once a day, silent when offline."""

import io
import json
import subprocess
import time
import urllib.error
from pathlib import Path

from lindvb import __version__, updatecheck as uc

ROOT = Path(__file__).resolve().parent.parent


def test_which_packages_look_for_new_versions():
    checks = lambda env={}, **stamp: uc.install(env, stamp)["checks"]  # noqa: E731
    assert checks(package="appimage", channel="release")
    assert checks(package="deb", channel="release")
    assert checks(package="flatpak", channel="release")
    # updated by snapd, the software centre or pacman
    assert not checks({"SNAP": "/snap/lindvb/81", "SNAP_REVISION": "81"})
    assert not checks({"SNAP": "/snap/lindvb/81"}, package="appimage", channel="release")
    assert not checks(package="flathub", channel="release")
    assert not checks(package="aur", channel="release")
    # a build of main and a source checkout are no release
    assert not checks(package="appimage", channel="nightly", commit="abc")
    assert not checks()


def test_what_the_about_dialog_says():
    assert uc.describe(uc.install({}, {"package": "appimage", "channel": "release"})) == "AppImage"
    assert uc.describe(uc.install({}, {"package": "appimage", "channel": "nightly",
                                       "commit": "abc1234def567890"}), "0.23.0") == \
        "AppImage, development build 0.23.0+abc1234def56"
    assert uc.describe(uc.install({"SNAP": "/snap/x", "SNAP_REVISION": "81"}, {})) == \
        "Snap, revision 81"
    assert uc.describe(uc.install({}, {"package": "flathub"})) == "Flatpak (Flathub)"
    assert uc.describe(uc.install({}, {})) == ""


def test_versions_compare_as_numbers():
    assert uc.is_newer("0.23.1", "0.23.0") and uc.is_newer("v0.23.10", "0.23.9")
    assert uc.is_newer("1.0", "0.99.9") and not uc.is_newer("0.23", "0.23.0")
    assert not uc.is_newer("0.22.1", "0.23.0") and not uc.is_newer("nightly", "0.23.0")
    assert not uc.is_newer("", "0.23.0")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_latest_release_from_the_github_api():
    seen = []

    def opener(req, timeout):
        seen.append((req.full_url, req.get_header("User-agent"), timeout))
        return _Resp(json.dumps({"tag_name": "v0.23.1",
                                 "html_url": uc.RELEASES + "/tag/v0.23.1"}).encode())
    assert uc.fetch_latest(opener=opener) == ("0.23.1", uc.RELEASES + "/tag/v0.23.1")
    assert seen == [(uc.LATEST_API, f"LinDVB/{__version__}", uc.TIMEOUT)]

    def offline(req, timeout):
        raise urllib.error.URLError("no network")
    assert uc.fetch_latest(opener=offline) is None
    # no version, or a link that leads elsewhere
    assert uc.fetch_latest(opener=lambda r, timeout: _Resp(b'{"tag_name": "latest"}')) is None
    assert uc.fetch_latest(opener=lambda r, timeout: _Resp(
        b'{"tag_name": "v9.0", "html_url": "https://example.com/x"}')) == ("9.0", uc.RELEASES)
    assert uc.fetch_latest(opener=lambda r, timeout: _Resp(b"<html>")) is None


RELEASE = {"package": "appimage", "channel": "release", "checks": True}


def _settings(**kw):
    s = {"update_check": True, "update_checked_at": 0, "update_latest": "", "update_url": ""}
    s.update(kw)
    return s


def test_check_once_a_day_and_report_a_newer_release():
    s, found, asked = _settings(), [], []

    def fetch():
        asked.append(1)
        return "99.0", uc.RELEASES + "/tag/v99.0"
    uc.check(s, lambda *a: found.append(a), RELEASE, fetch, run=lambda f: f())
    assert asked == [1] and found == [("99.0", uc.RELEASES + "/tag/v99.0")]
    assert s["update_latest"] == "99.0" and time.time() - s["update_checked_at"] < 5
    # the same day: not asked again, the newer release is reported from before
    uc.check(s, lambda *a: found.append(a), RELEASE, fetch, run=lambda f: f())
    assert asked == [1] and len(found) == 2
    # a day later: asked again
    s["update_checked_at"] -= uc.DAY + 1
    uc.check(s, lambda *a: found.append(a), RELEASE, fetch, run=lambda f: f())
    assert asked == [1, 1]


def test_no_notice_when_up_to_date_offline_or_switched_off():
    found = []
    s = _settings()
    uc.check(s, lambda *a: found.append(a), RELEASE, lambda: (__version__, uc.RELEASES),
             run=lambda f: f())
    assert found == [] and s["update_checked_at"]
    # offline: nothing said, asked again at the next start
    s = _settings()
    uc.check(s, lambda *a: found.append(a), RELEASE, lambda: None, run=lambda f: f())
    assert found == [] and s["update_checked_at"] == 0
    # switched off, or a package that does not check: GitHub is not asked
    asked = []
    for settings, info in ((_settings(update_check=False), RELEASE),
                           (_settings(), uc.install({"SNAP": "/snap/x"}, {}))):
        uc.check(settings, lambda *a: found.append(a), info,
                 lambda: asked.append(1) or ("99.0", uc.RELEASES), run=lambda f: f())
    assert asked == [] and found == []


def test_the_source_tree_carries_no_build_stamp():
    """Only the package builds write lindvb/_build.json: a checkout is no release."""
    assert not uc.STAMP.exists()
    assert uc.install({}, None)["package"] == ""


def test_appimage_build_stamps_release_or_nightly(tmp_path):
    """packaging/build-appdir.sh: a release for the tag of the version
    (RELEASE_TAG of the Release workflow), else a nightly."""
    script = (ROOT / "packaging" / "build-appdir.sh").read_text()
    start = script.index("# Which package this is")
    end = script.index('echo "== build stamp')
    (tmp_path / "lindvb").mkdir()
    version = uc.version_key(__version__) and __version__
    for tag, channel in ((f"v{version}", "release"), ("", None), ("v0.0.1", "nightly")):
        subprocess.run(["bash", "-c", script[start:end]], check=True, env={
            "SRC": str(ROOT), "SITE": str(tmp_path), "RELEASE_TAG": tag, "PATH": "/usr/bin:/bin"})
        stamp = json.loads((tmp_path / "lindvb" / "_build.json").read_text())
        assert stamp["package"] == "appimage"
        if channel:
            assert stamp["channel"] == channel
        assert uc.install({}, stamp)["checks"] == (stamp["channel"] == "release")


def test_flathub_manifest_has_no_update_check():
    text = (ROOT / "flatpak" / "io.github.thommy87.LinDVB.yml").read_text()
    assert text.count('"package":"flatpak"') == 1          # flathub-manifest.sh swaps it
    assert "_build.json" in (ROOT / "packaging" / "build-deb.sh").read_text()
    assert '"package": "aur"' in (ROOT / "packaging" / "aur" / "PKGBUILD").read_text()
