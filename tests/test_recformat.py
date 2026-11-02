"""Recording container rules, PID selection and file naming (no GStreamer needed)."""

import time
from datetime import datetime

from lindvb.channels import Channel
from lindvb.recformat import (EXTRAS, PidFilter, allowed_formats, audio_extension, choose_audio,
                              choose_from_pmt, effective_format, expand_scheme, keep_pids,
                              needs_pmt, pmt_audio_tracks)


def test_teletext_and_epg_force_ts():
    assert allowed_formats(["teletext"]) == ["TS"]
    assert allowed_formats(["epg_data"]) == ["TS"]
    assert effective_format("MKV", ["teletext"]) == "TS"
    assert effective_format("Audio", ["epg_data"], radio=True) == "TS"


def test_subtitles_allow_ts_and_mkv():
    assert allowed_formats(["subtitles"]) == ["TS", "MKV"]
    assert effective_format("MP4", ["subtitles"]) == "TS"
    assert effective_format("MKV", ["subtitles"]) == "MKV"


def test_plain_video_and_audio_allows_everything():
    assert allowed_formats([]) == ["TS", "MPG", "MP4", "MKV"]
    assert allowed_formats([], radio=True) == ["Audio", "TS"]


def _ch():
    return Channel("Das Erste HD", sid=28106, pmt_pid=5100, pcr_pid=5101, video_pid=5101,
                   audio_pids=[5102, 5103, 5106], teletext_pid=5104, subtitle_pids=[5105])


def test_keep_pids():
    ch = _ch()
    everything = keep_pids(ch, EXTRAS, "TS")
    assert {0, 0x12, 5100, 5101, 5102, 5103, 5104, 5105, 5106} <= everything
    bare = keep_pids(ch, [], "MKV", "first")
    assert 5102 in bare and 5103 not in bare and 5104 not in bare and 0x12 not in bare
    subs = keep_pids(ch, ["subtitles"], "MKV")
    assert 5104 in subs and 5105 in subs          # teletext carries the subtitle pages
    assert keep_pids(Channel("Unknown"), [], "TS") is None
    assert {5102, 5103, 5106} <= keep_pids(ch, [], "MKV", "all")
    first = keep_pids(ch, EXTRAS, "TS", "first")
    assert 5102 in first and 5103 not in first and 5106 not in first
    mp4 = keep_pids(ch, [], "MP4", "all")            # MP4 keeps one audio track
    assert 5102 in mp4 and 5103 not in mp4


def _pmt(streams):
    """A PMT section; streams are (stream_type, pid, descriptor bytes)."""
    body = bytes([0x6D, 0xCA, 0xC1, 0, 0, 0xE0 | (5101 >> 8), 5101 & 0xFF, 0xF0, 0x00])
    for st, pid, desc in streams:
        body += bytes([st, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0 | (len(desc) >> 8), len(desc) & 0xFF])
        body += desc
    length = len(body) + 4
    return bytes([0x02, 0xB0 | (length >> 8), length & 0xFF]) + body + b"\0\0\0\0"


def _lang(code, audio_type=0):
    return bytes([0x0A, 4]) + code.encode() + bytes([audio_type])


PMT = _pmt([(0x1B, 5101, b""),                                  # H.264 video
            (0x03, 5102, _lang("deu")),                         # MPEG audio, German
            (0x03, 5103, _lang("deu", 3)),                      # German audio description
            (0x06, 5106, bytes([0x6A, 1, 0]) + _lang("ger")),   # AC-3, German
            (0x04, 5107, _lang("eng")),                         # English
            (0x06, 5104, bytes([0x56, 5]) + b"deu\x09\x00")])   # teletext


def test_pmt_audio_tracks():
    assert pmt_audio_tracks(PMT) == [(5102, "deu", False), (5103, "deu", True),
                                     (5106, "ger", False), (5107, "eng", False)]


def test_choose_audio():
    tracks = pmt_audio_tracks(PMT)
    assert choose_audio(tracks, "all") == [5102, 5103, 5106, 5107]
    assert choose_audio(tracks, "language", "deu") == [5102, 5106]    # no audio description
    assert choose_audio(tracks, "language", "eng") == [5107]
    assert choose_audio(tracks, "language", "eng", single=True) == [5107]
    assert choose_audio(tracks, "all", "eng", single=True) == [5107]
    assert choose_audio(tracks, "language", "fra") == [5102]          # no match: first track
    assert choose_audio(tracks, "language", "") == [5102]
    assert choose_audio(tracks, "first", "eng") == [5102]
    assert choose_audio([], "all") == []


def test_choose_from_pmt_replaces_audio_pids():
    ch = _ch()
    pids = keep_pids(ch, EXTRAS, "TS", "language")
    chosen = choose_from_pmt(PMT, pids, ch.audio_pids, "language", "eng")
    assert 5107 in chosen and 5102 not in chosen and 5103 not in chosen
    assert {5100, 5101, 5104, 5105} <= chosen
    assert choose_from_pmt(b"junk", pids, ch.audio_pids, "language", "eng") == pids
    # English is not captured (only the channel list's PIDs are): first captured track.
    only = choose_from_pmt(PMT, pids, ch.audio_pids, "language", "eng", captured=ch.audio_pids)
    assert 5102 in only and 5107 not in only
    assert needs_pmt("language", "MKV", "deu") and not needs_pmt("language", "MKV", "")
    assert needs_pmt("all", "MP4", "deu") and not needs_pmt("all", "TS", "deu")
    assert not needs_pmt("first", "MP4", "deu")


def test_pid_filter_holds_until_pmt():
    pmt = _pmt([(0x03, 300, _lang("deu")), (0x03, 400, _lang("eng"))])
    data = _pkt(300) + _pkt(400) + _pkt(100, True, b"\x00" + pmt) + _pkt(300) + _pkt(400)
    f = PidFilter({100, 300}, pmt_pid=100,
                  choose=lambda sec: choose_from_pmt(sec, {100, 300}, [300], "language", "eng"))
    out = f.feed(data[:400]) + f.feed(data[400:])
    assert out == _pkt(400) + _pkt(100, True, b"\x00" + pmt) + _pkt(400)
    # Without a PMT the held packets come out with the given PIDs at the end.
    f2 = PidFilter({300}, pmt_pid=100, choose=lambda sec: {400})
    assert f2.feed(_pkt(300) + _pkt(400)) == b""
    assert f2.flush() == _pkt(300)


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


# -- internet streams -------------------------------------------------------------

def test_internet_streams_ignore_extras():
    from lindvb.recformat import usable_extras
    assert usable_extras(["teletext", "subtitles"], iptv=True) == []
    assert allowed_formats(["teletext", "epg_data"], iptv=True) == ["TS", "MPG", "MP4", "MKV"]
    assert effective_format("MP4", ["teletext"], iptv=True) == "MP4"
    assert effective_format("MP4", ["teletext"]) == "TS"


def test_unavailable_reason():
    from lindvb.recformat import unavailable_reason
    assert unavailable_reason("MKV", ["subtitles"]) == ""
    assert unavailable_reason("MP4", ["subtitles"]) == "MP4 cannot hold subtitles."
    assert unavailable_reason("MKV", ["teletext", "subtitles"]) == "Matroska cannot hold teletext."
    assert unavailable_reason("MP4", ["teletext"], iptv=True) == ""


def test_choose_streams_hls():
    from lindvb.recformat import choose_streams
    streams = [("v", "video", "", True), ("a-mul", "audio", "mul", False),
               ("a-deu", "audio", "deu", True), ("a-eng", "audio", "en", False),
               ("t", "other", "deu", False)]
    # the default audio stream counts as the first track
    assert choose_streams(streams, "first") == ["v", "a-deu"]
    assert choose_streams(streams, "language", "eng", single=True) == ["v", "a-eng"]
    assert choose_streams(streams, "all", "eng", single=True) == ["v", "a-eng"]
    assert choose_streams(streams, "language", "fra", single=True) == ["v", "a-deu"]
    assert choose_streams(streams, "all") == ["v", "a-deu", "a-mul", "a-eng"]
    assert choose_streams([("a", "audio", "", False)], "all", single=True) == ["a"]
