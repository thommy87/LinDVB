# SPDX-License-Identifier: GPL-2.0-or-later
"""tools/perfcheck.py: what its comparison calls worse and what a leak."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _tool():
    spec = importlib.util.spec_from_file_location("perfcheck", ROOT / "tools" / "perfcheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _report(**scenarios):
    return {"scenarios": scenarios}


def test_noise_is_not_worse():
    pc = _tool()
    base = _report(play={"cpu_per_s": 0.50, "rss_mb": 230.0, "late_frames": 0})
    head = _report(play={"cpu_per_s": 0.55, "rss_mb": 240.0, "late_frames": 3})
    assert {v for *_r, v in pc.compare(base, head)} == {"ok"}


def test_more_cpu_is_worse_but_does_not_fail(tmp_path):
    pc = _tool()
    base = _report(play={"cpu_per_s": 0.50, "rss_mb": 230.0})
    head = _report(play={"cpu_per_s": 0.80, "rss_mb": 230.0})
    assert ("play", "cpu_per_s", 0.50, 0.80, "worse") in pc.compare(base, head)
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "h.json").write_text(json.dumps(head))
    summary = tmp_path / "summary.md"
    assert pc.main(["compare", str(tmp_path / "b.json"), str(tmp_path / "h.json"),
                    "--summary", str(summary)]) == 0
    assert "⚠ worse" in summary.read_text()


def test_threads_growing_with_channel_changes_fail(tmp_path):
    pc = _tool()
    base = _report(switches={"threads_growth": 0, "fds_growth": 1})
    head = _report(switches={"threads_growth": 30, "fds_growth": 1})
    rows = pc.compare(base, head)
    assert ("switches", "threads_growth", 0, 30, "leak") in rows
    assert ("switches", "fds_growth", 1, 1, "ok") in rows
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "h.json").write_text(json.dumps(head))
    assert pc.main(["compare", str(tmp_path / "b.json"), str(tmp_path / "h.json")]) == 1


def test_a_new_scenario_has_nothing_to_compare_with():
    pc = _tool()
    assert pc.compare(_report(), _report(reader={"cpu_per_s": 0.1})) == []
