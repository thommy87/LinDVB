# SPDX-License-Identifier: GPL-2.0-or-later
import os
import time

from lindvb import timeshift
from lindvb.timeshift import MB


def settings(tmp_path, **kw):
    s = {"timeshift_dir": str(tmp_path / "ts"), "timeshift_max_mb": 4096,
         "timeshift_ram_mb": 1024, "timeshift_mode": "disk"}
    s.update(kw)
    return s


def test_unknown_mode_is_disk(tmp_path):
    assert timeshift.mode(settings(tmp_path, timeshift_mode="nonsense")) == "disk"
    assert timeshift.mode({}) == "disk"


def test_disk_plan_uses_the_set_size(tmp_path):
    p = timeshift.plan(settings(tmp_path), _free=100_000)
    assert p.full and p.dir == tmp_path / "ts"
    assert p.size_mb == 4096 and p.note == ""


def test_disk_plan_keeps_a_reserve_on_a_full_disk(tmp_path):
    p = timeshift.plan(settings(tmp_path), _free=3000)
    assert p.full and p.size_mb <= 3000 - timeshift.DISK_RESERVE_MB
    assert p.note


def test_disk_without_room_falls_back_to_the_small_buffer(tmp_path):
    p = timeshift.plan(settings(tmp_path), _free=1000)
    assert not p.full and p.note


def test_off_is_a_small_buffer(tmp_path):
    p = timeshift.plan(settings(tmp_path, timeshift_mode="off"))
    assert not p.full
    assert p.size_mb == timeshift.SMALL_SEGMENTS * timeshift.SMALL_SEGMENT_BYTES // MB


def test_ram_plan_is_capped_by_half_the_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(timeshift, "ram_dir", lambda: tmp_path / "shm")
    s = settings(tmp_path, timeshift_mode="ram", timeshift_ram_mb=12000)
    p = timeshift.plan(s, _mem=(16000, 15000))
    assert p.full and p.dir == tmp_path / "shm" and p.size_mb <= 8000


def test_ram_plan_follows_available_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(timeshift, "ram_dir", lambda: tmp_path / "shm")
    s = settings(tmp_path, timeshift_mode="ram", timeshift_ram_mb=4096)
    p = timeshift.plan(s, _mem=(16000, 3000))
    assert p.full and p.size_mb <= 3000 - timeshift.RAM_RESERVE_MB and p.note
    p = timeshift.plan(s, _mem=(16000, 1100))
    assert not p.full and p.note


def test_ram_recording_only_gets_the_small_buffer(tmp_path, monkeypatch):
    monkeypatch.setattr(timeshift, "ram_dir", lambda: tmp_path / "shm")
    s = settings(tmp_path, timeshift_mode="ram")
    assert not timeshift.plan(s, live=False, _mem=(16000, 15000)).full
    assert timeshift.plan(s, live=True, _mem=(16000, 15000)).full


def test_cleanup_removes_only_stale_segment_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(timeshift, "ram_dir", lambda: None)
    s = settings(tmp_path)
    base = tmp_path / "ts"
    stale = base / "a0-abc123"
    fresh = base / "a1-def456"
    foreign = base / "a2-zzz999"
    for d in (stale, fresh, foreign):
        d.mkdir(parents=True)
        (d / "seg00000.ts").write_bytes(b"x")
    (foreign / "holiday.jpg").write_bytes(b"x")
    old = time.time() - 2 * timeshift.STALE_SECONDS
    for d in (stale, foreign):
        for f in d.iterdir():
            os.utime(f, (old, old))
    removed = timeshift.cleanup_stale(s)
    assert removed == [stale]
    assert fresh.exists() and foreign.exists()


def test_duration_hint():
    h = timeshift.duration_hint(4096)
    assert "SD" in h and "HD" in h and "UHD" in h


def _capture(tmp_path, monkeypatch, **kw):
    import pytest
    Gst = pytest.importorskip("gi.repository.Gst")
    Gst.init(None)
    from lindvb import tuner
    from lindvb.channels import Channel
    from lindvb.config import DEFAULTS
    monkeypatch.setattr(timeshift, "ram_dir", lambda: tmp_path / "shm")
    monkeypatch.setattr(timeshift, "_meminfo", lambda: (16000, 15000))
    monkeypatch.setenv("LINDVB_TEST_TS", str(tmp_path / "none.ts"))
    s = dict(DEFAULTS, **settings(tmp_path, **kw))
    ch = Channel("X", delsys="dvb-t2", frequency=538000, sid=28, pmt_pid=0x100)
    return tuner.Capture(ch, 0, 0, s), s


def test_capture_without_timeshift_has_the_small_buffer(tmp_path, monkeypatch):
    cap, _s = _capture(tmp_path, monkeypatch, timeshift_mode="off")
    try:
        assert not cap.timeshift
        assert cap._sink.get_property("max-files") == timeshift.SMALL_SEGMENTS
        assert cap.store.dir.parent == tmp_path / "shm"
    finally:
        cap.destroy()


def test_capture_in_ram_grows_when_the_live_view_joins(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("gi.repository.Gst")
    from lindvb import tuner
    from lindvb.channels import Channel
    cap, s = _capture(tmp_path, monkeypatch, timeshift_mode="ram")
    cap.destroy()
    ch = Channel("X", delsys="dvb-t2", frequency=538000, sid=28, pmt_pid=0x100)
    cap = tuner.Capture(ch, 0, 0, s, live=False)
    try:
        assert not cap.timeshift
        cap.set_live()
        assert cap.timeshift
        assert cap._sink.get_property("max-file-size") == timeshift.SEGMENT_BYTES
        assert cap._sink.get_property("max-files") == 1024 * MB // timeshift.SEGMENT_BYTES
    finally:
        cap.destroy()


def test_private_dir_refuses_a_symlink_and_tightens_the_mode(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "lindvb-link"
    link.symlink_to(target)
    assert timeshift.private_dir(link) is None
    d = tmp_path / "lindvb-1000"
    d.mkdir(mode=0o777)
    os.chmod(d, 0o777)
    assert timeshift.private_dir(d) == d
    assert d.stat().st_mode & 0o777 == 0o700
    new = timeshift.private_dir(tmp_path / "fresh")
    assert new is not None and new.stat().st_mode & 0o777 == 0o700
