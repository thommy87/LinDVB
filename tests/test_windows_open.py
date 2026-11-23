# SPDX-License-Identifier: GPL-2.0-or-later
"""Every window of LinDVB opens. 0.22.0 shipped preferences that did not open:
a NameError while building them, in code no test ran."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from windowsmoke import WINDOWS

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("gi")


@pytest.mark.parametrize("name", sorted(WINDOWS))
def test_window_opens(name, tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / ".config"),
               XDG_DATA_HOME=str(tmp_path / ".local/share"), XDG_CACHE_HOME=str(tmp_path / ".cache"),
               PYTHONPATH=str(ROOT))
    env = {k: v for k, v in env.items() if not k.startswith("SNAP")}
    out = subprocess.run([sys.executable, str(ROOT / "tests" / "windowsmoke.py"), name],
                         env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    if out.stdout.startswith("SKIP"):
        pytest.skip(out.stdout.strip())
    assert out.returncode == 0 and f"OK {name}" in out.stdout, out.stdout + out.stderr
