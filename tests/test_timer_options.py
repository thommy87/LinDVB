"""Timers: general settings vs own options, series scope (no GStreamer)."""
from types import SimpleNamespace

from lindvb import recorder
from lindvb.recorder import SeriesRule, Timer, TimerManager


def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(TimerManager, "path", tmp_path / "timers.json")
    monkeypatch.setattr(recorder.GLib, "timeout_add_seconds", lambda *a: 0)
    settings = {"rec_tv_format": "MKV", "rec_radio_format": "TS", "rec_audio_mode": "all",
                "lead_minutes": 0, "follow_minutes": 0}
    return TimerManager(SimpleNamespace(settings=settings))


def test_general_timer_follows_the_preferences(tmp_path, monkeypatch):
    tm = manager(tmp_path, monkeypatch)
    ch = SimpleNamespace(key="k", name="Ch")
    t = tm.new_timer(ch, "News", 0, 60)
    assert t.general
    tm.app.settings["rec_tv_format"] = "MP4"
    assert tm.options_of(t)["tv_format"] == "MP4"
    own = Timer("k", "Ch", "News", 0, 60, tv_format="TS")
    assert not own.general and tm.options_of(own)["tv_format"] == "TS"


def test_series_options_reach_every_scheduled_timer(tmp_path, monkeypatch):
    tm = manager(tmp_path, monkeypatch)
    tm.rules = [SeriesRule("Tatort")]
    a = Timer("k", "Ch", "Tatort", 0, 60, series=True, general=True)
    b = Timer("k", "Ch", "Tatort", 100, 160, series=True, general=True)
    other = Timer("k", "Ch", "News", 0, 60, general=True)
    tm.timers = [a, b, other]
    tm.set_series_options(a, {"tv_format": "MP4"})
    assert tm.rules[0].options == {"tv_format": "MP4"}
    assert (a.tv_format, b.tv_format, b.general) == ("MP4", "MP4", False)
    assert other.general and other.tv_format == "TS"
    tm.set_series_options(a, None)                 # back to the preferences
    assert tm.rules[0].options == {} and a.general and b.general
