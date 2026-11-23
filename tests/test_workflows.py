# SPDX-License-Identifier: GPL-2.0-or-later
"""The CI workflows are valid YAML. GitHub runs a workflow it cannot parse
as an instant failure with no jobs, which is easy to miss (the first lint
workflow had an unquoted ": " in a command)."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / ".github").glob("workflows/*.yml")) + \
    sorted((ROOT / ".github").glob("actions/*/action.yml"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_workflow_is_valid_yaml(path):
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    assert "jobs" in data or "runs" in data, "neither jobs nor runs"
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps", []):
            assert "run" in step or "uses" in step, step
