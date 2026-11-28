# SPDX-License-Identifier: GPL-2.0-or-later
"""Is there a newer LinDVB? (issue #206)

Only where nothing else updates LinDVB: the AppImage, the .deb and the
Flatpak bundle of a GitHub release. The Snap (every channel) and the
Flathub and AUR packages are updated by snapd, the software centre or
pacman; the AppImage built from main ("nightly") and a source checkout
are no release, so they are not compared with releases.

Which package LinDVB came in is written into lindvb/_build.json when it is
built (packaging/build-appdir.sh, build-deb.sh, the Flatpak manifests and
the PKGBUILD); the Snap is told by its environment.

The check asks the GitHub API for the latest release, at most once a day,
in the background, and says nothing when it fails (offline). Preferences
> Internet > Updates switches it off.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

LATEST_API = "https://api.github.com/repos/thommy87/LinDVB/releases/latest"
RELEASES = "https://github.com/thommy87/LinDVB/releases"
DAY = 24 * 3600
TIMEOUT = 10
STAMP = Path(__file__).with_name("_build.json")

# packages whose updates come from elsewhere
_UPDATED_ELSEWHERE = {"snap", "flathub", "aur"}
PACKAGE_NAMES = {"appimage": "AppImage", "deb": ".deb", "flatpak": "Flatpak",
                 "flathub": "Flatpak (Flathub)", "aur": "AUR", "snap": "Snap"}


def _stamp(path=STAMP):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def install(env=None, stamp=None):
    """How this LinDVB was installed: {"package", "channel", "commit",
    "revision", "checks"}. *checks*: compare with the GitHub releases."""
    env = os.environ if env is None else env
    stamp = _stamp() if stamp is None else stamp
    info = {"package": str(stamp.get("package") or ""),
            "channel": str(stamp.get("channel") or "release"),
            "commit": str(stamp.get("commit") or "")[:12], "revision": ""}
    if env.get("SNAP"):
        info.update(package="snap", channel="", revision=env.get("SNAP_REVISION", ""))
    info["checks"] = bool(info["package"]) and info["package"] not in _UPDATED_ELSEWHERE \
        and info["channel"] == "release"
    return info


def describe(info, version=__version__):
    """What the About dialog says about the installation: "AppImage",
    "Snap, revision 81", "AppImage, development build 0.23.0+abc1234",
    "" for a source checkout or an unknown package."""
    name = PACKAGE_NAMES.get(info.get("package"), "")
    if not name:
        return ""
    if info.get("revision"):
        return f"{name}, revision {info['revision']}"
    if info.get("channel") == "nightly":
        build = f"{version}+{info['commit']}" if info.get("commit") else version
        return f"{name}, development build {build}"
    return name


def version_key(text):
    """'v0.23.1' -> (0, 23, 1); None when it is no version."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(latest, current=__version__):
    a, b = version_key(latest), version_key(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def fetch_latest(timeout=TIMEOUT, opener=urllib.request.urlopen):
    """(version, page) of the latest release, or None."""
    req = urllib.request.Request(LATEST_API, headers={
        "User-Agent": f"LinDVB/{__version__}", "Accept": "application/vnd.github+json"})
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read(256 * 1024).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    tag = str(data.get("tag_name") or "") if isinstance(data, dict) else ""
    if version_key(tag) is None:
        return None
    page = str(data.get("html_url") or "")
    if not page.startswith(RELEASES + "/"):
        page = RELEASES
    return tag.lstrip("v"), page


def known(settings):
    """(version, page) of a newer release learnt earlier, or None."""
    latest = settings.get("update_latest", "")
    if latest and is_newer(latest):
        return latest, settings.get("update_url", "") or RELEASES
    return None


def due(settings, info, now=None):
    """Ask GitHub now? Only where the check is on and runs, once a day."""
    if not info.get("checks") or not settings.get("update_check", True):
        return False
    now = time.time() if now is None else now
    last = settings.get("update_checked_at", 0) or 0
    return not (0 <= now - last < DAY)


def check(settings, on_newer, info=None, fetch=fetch_latest, run=None):
    """Look for a newer release in the background when one is due, and call
    ``on_newer(version, page)`` (from that thread) when there is one. A
    newer release learnt before is reported at once, without asking."""
    info = install() if info is None else info
    if not info.get("checks") or not settings.get("update_check", True):
        return
    if not due(settings, info):
        found = known(settings)
        if found:
            on_newer(*found)
        return

    def work():
        res = fetch()
        if res is None:
            return                     # offline or no answer: try again at the next start
        settings["update_checked_at"] = int(time.time())
        settings["update_latest"], settings["update_url"] = res
        if is_newer(res[0]):
            on_newer(*res)

    if run is not None:
        run(work)
    else:
        threading.Thread(target=work, name="lindvb-update-check", daemon=True).start()
