# SPDX-License-Identifier: GPL-2.0-or-later
"""Settings of older versions: the subtitles in the buffer (a test switch,
off, before 0.23) are on, and timeshift remembers where its buffer lived."""

import json

from lindvb import config, timeshift


def _load(tmp_path, monkeypatch, saved):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(saved))
    monkeypatch.setattr(config.Settings, "path", path)
    return config.Settings()


def test_the_buffer_subtitles_are_on_once_then_as_chosen(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, {"subtitles_in_buffer": False, "theme": "dark"})
    assert s["subtitles_in_buffer"] is True and s["subtitles_in_buffer_set"]
    s["subtitles_in_buffer"] = False
    s.save()
    s = config.Settings()
    assert s["subtitles_in_buffer"] is False             # chosen since: kept
    assert config.DEFAULTS["subtitles_in_buffer"] is True


def test_timeshift_keeps_its_buffer_while_off(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, {"timeshift_mode": "ram"})
    assert s["timeshift_store"] == "ram" and timeshift.store(s) == "ram"
    s = _load(tmp_path, monkeypatch, {"timeshift_mode": "off", "timeshift_store": "ram"})
    assert not timeshift.enabled(s) and timeshift.store(s) == "ram"
    s = _load(tmp_path, monkeypatch, {"timeshift_mode": "off"})
    assert timeshift.store(s) == "disk"
