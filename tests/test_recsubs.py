# SPDX-License-Identifier: GPL-2.0-or-later
"""The subtitles of the timeshift buffer in recordings (lindvb.recsubs):
.srt files beside TS recordings, their times as players count them."""

import json

from lindvb import cuebuffer as cb
from lindvb import library
from lindvb.recsubs import FileTimes, SrtFile, TsSubtitles, srt_paths, srt_time
from lindvb.tsclock import PTS_HZ, PTS_WRAP, TS_PACKET


def _pts_bytes(pts):
    return bytes([0x21 | ((pts >> 29) & 0x0e), (pts >> 22) & 0xff, 0x01 | ((pts >> 14) & 0xfe),
                  (pts >> 7) & 0xff, 0x01 | ((pts << 1) & 0xfe)])


def _pes(pts, stream_id=0xe0, pid=0x100):
    """A TS packet that starts a PES of *stream_id* at *pts* (ticks)."""
    head = bytes([0x47, 0x40 | pid >> 8, pid & 0xff, 0x10])
    body = b"\0\0\1" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + _pts_bytes(pts % PTS_WRAP)
    return (head + body).ljust(TS_PACKET, b"\xff")


def _video(t, pid=0x100):
    return _pes(round(t * PTS_HZ), 0xe0, pid)


def _sound(t):
    return _pes(round(t * PTS_HZ), 0xc0, 0x101)


def test_srt_times():
    assert srt_time(0) == "00:00:00,000"
    assert srt_time(3725.0456) == "01:02:05,046"
    assert srt_time(-1) == "00:00:00,000"


def test_the_file_starts_at_its_first_picture_or_the_sound_just_before_it():
    times = FileTimes()
    times.feed(_video(3600.0) + _video(3600.04))
    assert times.file_time(3605.0, 0) == 5.0
    times = FileTimes()
    # sound 0.2 s before the first picture, sent before and after it
    times.feed(_sound(3599.9) + _video(3600.0) + _sound(3599.8) + _video(3600.04))
    assert abs(times.file_time(3605.0, 0) - 5.2) < 1e-6
    # a picture shown before the first one sent (B-frames)
    times = FileTimes()
    times.feed(_video(3600.12) + _video(3600.04) + _video(3600.08) + _video(3601.5))
    assert abs(times.file_time(3605.0, 0) - 4.96) < 1e-6
    # sound from long before is not this file's start
    times = FileTimes()
    times.feed(_sound(3000.0) + _video(3600.0))
    assert times.file_time(3601.0, 0) == 1.0
    assert times.file_time(3601.0, 1) is None


def test_bytes_cut_anywhere_and_the_wrap():
    data = b"".join(_video(95443.0 + k * 0.04) for k in range(40))   # past 2^33 ticks
    times = FileTimes()
    for k in range(0, len(data), 100):
        times.feed(data[k:k + 100])
    assert abs(times.file_time((95444.0 * PTS_HZ % PTS_WRAP) / PTS_HZ, 0) - 1.0) < 1e-6


def test_after_a_jump_the_next_stretch_follows_on():
    times = FileTimes()
    times.feed(b"".join(_video(3600 + k * 0.04) for k in range(250)))   # 10 s
    times.feed(b"".join(_video(3600 + k * 0.04) for k in range(50)))    # the reader connected again
    assert times.epoch == 1
    assert abs(times.file_time(3601.0, 1) - (9.96 + 0.04 + 1.0)) < 1e-6
    assert abs(times.file_time(3601.0, 0) - 1.0) < 1e-6


def test_the_srt_file_is_made_with_its_first_cue(tmp_path):
    f = SrtFile(tmp_path / "a.srt")
    assert not f.write(-3.0, -1.0, "before the file")
    assert not f.write(1.0, 2.0, "  \n")
    assert not f.path.exists()
    assert f.write(-0.5, 1.5, "Guten\n\nAbend")
    assert f.write(61.25, 62.0, "zwei")
    f.close()
    assert f.path.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,500\nGuten\nAbend\n\n"
        "2\n00:01:01,250 --> 00:01:02,000\nzwei\n\n")


def test_srt_names_beside_the_recording(tmp_path):
    rec = tmp_path / "Film 28.11.2026.ts"
    paths = srt_paths(rec, ["English", "Deutsch", "Deutsch/2"])
    assert paths["English"].name == "Film 28.11.2026.srt"
    assert paths["Deutsch"].name == "Film 28.11.2026.Deutsch.srt"
    assert paths["Deutsch/2"].name == "Film 28.11.2026.Deutsch_2.srt"


def test_cues_of_the_buffer_into_srt_files(tmp_path):
    pids = cb.cue_pids(["English", "Deutsch"])
    rec = tmp_path / "rec.ts"
    subs = TsSubtitles(rec, lambda: pids)
    cc = 0
    data = _sound(3599.9) + _video(3600.0)
    for n in range(1, 4):
        data += _video(3600.0 + n)
        cue, cc = cb.cue_packets(pids["English"], f"Live {n}", 3600.0 + n, 3600.8 + n, cc)
        data += cue
    cue, cc = cb.cue_packets(pids["Deutsch"], "Hallo", 3602.5, 3603.0, cc)
    data += cue + b"\x47\x1f\xff\x10" + b"\xff" * 184
    # the file keeps what the filter left: the same packets here, read in two
    cut = 5 * TS_PACKET
    subs.feed(data[:cut], data[:cut])
    subs.feed(data[cut:], data[cut:])
    files = subs.close()
    assert sorted(p.name for p in files) == ["rec.Deutsch.srt", "rec.srt"]
    english = (tmp_path / "rec.srt").read_text(encoding="utf-8")
    assert english.startswith("1\n00:00:01,100 --> 00:00:01,900\nLive 1\n\n2\n00:00:02,100")
    assert "3\n00:00:03,100 --> 00:00:03,900\nLive 3\n" in english
    assert (tmp_path / "rec.Deutsch.srt").read_text(encoding="utf-8") == \
        "1\n00:00:02,600 --> 00:00:03,100\nHallo\n\n"
    assert subs.written == 4 and subs.dropped == 0, subs.report()


def test_deleting_a_recording_takes_its_srt_files(tmp_path, monkeypatch):
    rec = tmp_path / "rec.ts"
    rec.write_bytes(b"")
    library.write_info(rec, "Test HLS", "Titel")
    for name in ("rec.srt", "rec.Deutsch.srt", "other.srt"):
        (tmp_path / name).write_text("1\n")
    library.add_subtitle_files(rec, [tmp_path / "rec.srt", tmp_path / "rec.Deutsch.srt"])
    info = json.loads(library.sidecar_path(rec).read_text())
    assert info["subtitle_files"] == ["rec.Deutsch.srt", "rec.srt"] and info["title"] == "Titel"
    # a name that leads elsewhere is never followed
    info["subtitle_files"].append("../outside.srt")
    library.sidecar_path(rec).write_text(json.dumps(info))
    (tmp_path.parent / "outside.srt").write_text("1\n")
    library.delete_info(rec)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.srt", "rec.ts"]
    assert (tmp_path.parent / "outside.srt").exists()
