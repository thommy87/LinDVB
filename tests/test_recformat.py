"""Recording container rules, PID selection and file naming (no GStreamer needed)."""

import time
from datetime import datetime

from lindvb.channels import Channel
from lindvb.recformat import (EXTRAS, PidFilter, allowed_formats, audio_extension,
                              effective_format, expand_scheme, keep_pids)


def test_teletext_and_epg_force_ts():
    assert allowed_formats(["teletext"]) == ["TS"]
    assert allowed_formats(["epg_data"]) == ["TS"]
    assert effective_format("MKV", ["teletext"]) == "TS"
    assert effective_format("Audio", ["epg_data"], radio=True) == "TS"


def test_subtitles_allow_ts_and_mkv():
    assert allowed_formats(["subtitles"]) == ["TS", "MKV"]
    assert allowed_formats(["subtitles", "all_audio"]) == ["TS", "MKV"]
    assert effective_format("MP4", ["subtitles"]) == "TS"
    assert effective_format("MKV", ["subtitles"]) == "MKV"


def test_plain_video_and_audio_allows_everything():
    assert allowed_formats([]) == ["TS", "MPG", "MP4", "MKV"]
    assert allowed_formats(["all_audio"]) == ["TS", "MPG", "MP4", "MKV"]
    assert allowed_formats([], radio=True) == ["Audio", "TS"]
    assert allowed_formats(["all_audio"], radio=True) == ["TS"]


def _ch():
    return Channel("Das Erste HD", sid=28106, pmt_pid=5100, pcr_pid=5101, video_pid=5101,
                   audio_pids=[5102, 5103, 5106], teletext_pid=5104, subtitle_pids=[5105])


def test_keep_pids():
    ch = _ch()
    everything = keep_pids(ch, EXTRAS, "TS")
    assert {0, 0x12, 5100, 5101, 5102, 5103, 5104, 5105, 5106} <= everything
    bare = keep_pids(ch, [], "MKV")
    assert 5102 in bare and 5103 not in bare and 5104 not in bare and 0x12 not in bare
    subs = keep_pids(ch, ["subtitles"], "MKV")
    assert 5104 in subs and 5105 in subs          # teletext carries the subtitle pages
    assert keep_pids(Channel("Unknown"), [], "TS") is None


def test_expand_scheme():
    start = datetime(2026, 10, 31, 21, 30).timestamp()
    end = datetime(2026, 10, 31, 22, 45).timestamp()
    assert expand_scheme("%date_%time_%station_%name", "Das Erste HD", "Tatort: Ein/Fall", start, end) \
        == "10-31_21-30_Das Erste HD_Tatort_ Ein_Fall"
    assert expand_scheme("%station/%year/%name %time-%end", "ZDF", "heute", start, end) \
        == "ZDF/2026/heute 21-30-22-45"
    assert expand_scheme("%event", "ZDF", "", start) == "Recording"
    assert expand_scheme("", "ZDF", "x", start) == "ZDF_10-31_21-30_x"


def _pkt(pid, pusi=False, payload=b""):
    head = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, 0x10])
    return (head + payload).ljust(188, b"\xff")


def test_pid_filter_drops_unwanted_and_handles_split_packets():
    pmt = bytes([0x02, 0xB0, 0x0D, 0x6D, 0xCA, 0xC1, 0, 0, 0xE0, 0x00, 0xF0, 0x00]) + b"\0\0\0\0"
    data = _pkt(0) + _pkt(100, True, b"\x00" + pmt) + _pkt(200) + _pkt(300) + _pkt(200)
    f = PidFilter({0, 100, 200}, pmt_pid=100)
    out = f.feed(data[:250]) + f.feed(data[250:600]) + f.feed(data[600:])
    assert len(out) == 4 * 188
    assert f.pmt == pmt
    # Garbage before a packet is skipped.
    f2 = PidFilter({200})
    assert f2.feed(b"\x00\x01" + _pkt(200) + _pkt(300)) == _pkt(200)


def test_audio_extension():
    assert audio_extension("audio/mpeg", 1, 2) == ".mp2"
    assert audio_extension("audio/mpeg", 1, 3) == ".mp3"
    assert audio_extension("audio/mpeg", 4) == ".aac"
    assert audio_extension("audio/x-ac3") == ".ac3"


def test_old_timers_keep_their_times():
    from lindvb.recorder import Timer
    now = int(time.time())
    t = Timer("k", "Ch", "Old", now, now + 3600)
    assert t.rec_start == now and t.rec_end == now + 3600
    t.lead, t.follow = 2, 5
    assert t.rec_start == now - 120 and t.rec_end == now + 3600 + 300
    assert t.records and not t.tunes
