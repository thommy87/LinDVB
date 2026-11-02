"""Finding recordings on disk for the Recordings tab."""

import os

from lindvb.recordings import find_recordings, human_size


def _touch(path, mtime, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))


def test_finds_media_files_newest_first(tmp_path):
    _touch(tmp_path / "old.ts", 1000)
    _touch(tmp_path / "Das Erste" / "Tagesschau.mkv", 3000)
    _touch(tmp_path / "radio.mp2", 2000)
    _touch(tmp_path / "notes.txt", 4000)
    _touch(tmp_path / ".hidden.ts", 5000)
    names = [r.path.name for r in find_recordings([tmp_path])]
    assert names == ["Tagesschau.mkv", "radio.mp2", "old.ts"]


def test_missing_and_duplicate_folders(tmp_path):
    _touch(tmp_path / "a.ts", 1000)
    recs = find_recordings([tmp_path, str(tmp_path), tmp_path / "missing", ""])
    assert len(recs) == 1
    assert recs[0].title == "a"
    assert not recs[0].is_audio


def test_depth_limit(tmp_path):
    _touch(tmp_path / "1" / "2" / "3" / "4" / "5" / "deep.ts", 1000)
    _touch(tmp_path / "1" / "shallow.ts", 1000)
    names = [r.path.name for r in find_recordings([tmp_path], max_depth=2)]
    assert names == ["shallow.ts"]


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(3 * 1024 ** 3) == "3.0 GB"
