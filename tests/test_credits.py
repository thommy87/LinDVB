# SPDX-License-Identifier: GPL-2.0-or-later
"""Version, copyright and third-party credits stay complete and in step."""

import re
from pathlib import Path

import lindvb
from lindvb.credits import CREDITS

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_pyproject():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == lindvb.__version__


def test_credits_name_the_code_lindvb_contains():
    text = " ".join(p for _, people in CREDITS for p in people)
    for name in ("VDR EEPG", "GOOM", "dash.js", "GStreamer", "GTK", "WebKitGTK"):
        assert name in text, name
    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    for path in ("lindvb/epgsources.py", "lindvb/freesat_tables.py", "lindvb/goom.py",
                 "lindvb/vendor/dash.mediaplayer.min.js"):
        assert path in notices and (ROOT / path).exists(), path


def test_every_source_file_has_a_licence_line():
    files = list((ROOT / "lindvb").rglob("*.py")) + [ROOT / "lindvb" / "hbbtv.js"]
    missing = [str(f.relative_to(ROOT)) for f in files
               if "SPDX-License-Identifier: GPL-2.0-or-later" not in
               "".join(f.read_text(encoding="utf-8").splitlines(True)[:2])]
    assert not missing, missing
