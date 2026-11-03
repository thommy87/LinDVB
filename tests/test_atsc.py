"""ATSC: PSIP guide (VCT, EIT, ETT, STT) from raw sections, virtual channel numbers."""

import struct

import pytest

from lindvb import atsc
from lindvb.channels import parse_dvbv5, write_dvbv5

gi = pytest.importorskip("gi")
try:
    gi.require_version("GstMpegts", "1.0")
    from gi.repository import GstMpegts  # noqa: E402
    GstMpegts.initialize()
except (ImportError, ValueError):
    GstMpegts = None


def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else \
                (crc << 1) & 0xFFFFFFFF
    return crc


def section(table_id, ext, body):
    """A long-form PSIP section with CRC; ``body`` starts at protocol_version."""
    length = 5 + len(body) + 4
    head = struct.pack(">BHHBBB", table_id, 0xF000 | length, ext, 0xC1, 0, 0)
    data = head + body
    return data + struct.pack(">I", crc32_mpeg(data))


def mstring(text, lang=b"eng"):
    raw = text.encode()
    return bytes([1]) + lang + bytes([1, 0, 0, len(raw)]) + raw


def tvct(tsid, channels):
    body = bytes([0, len(channels)])
    for name, major, minor, program, source in channels:
        short = name.encode("utf-16-be").ljust(14, b"\0")[:14]
        nums = (0xF << 20) | (major << 10) | minor
        body += short + struct.pack(">I", (nums << 8) | 0x04)        # + modulation 8VSB
        body += struct.pack(">IHHHHH", 0, tsid, program, 0x0002 | 0x0200, source, 0xFC00)
    body += struct.pack(">H", 0xFC00)
    return section(0xC8, tsid, body)


def eit(source_id, events):
    body = bytes([0, len(events)])
    for event_id, gps_start, length, title in events:
        t = mstring(title)
        body += struct.pack(">HI", 0xC000 | event_id, gps_start)
        body += ((0x3 << 22) | (1 << 20) | length).to_bytes(3, "big")    # ETM in ETT
        body += bytes([len(t)]) + t + struct.pack(">H", 0xF000)
    return section(0xCB, source_id, body)


def ett(source_id, event_id, text):
    body = bytes([0]) + struct.pack(">I", atsc.etm_id(source_id, event_id)) + mstring(text)
    return section(0xCC, 0, body)


def test_psip_guide():
    guide = atsc.AtscGuide("eng")
    feed = guide.feed_bytes
    gps = 1_500_000_000
    # the EIT may come before the channel table: it waits for the VCT
    assert feed(eit(3, [(42, gps, 1800, "Evening News")])) == []
    out = feed(tvct(0x0815, [("KABC", 7, 1, 3, 3)]))
    assert out and out[0][0] == f"{0x0815}:3"
    ev = out[0][1][0]
    assert ev.title == "Evening News" and ev.duration == 1800
    assert ev.start == gps + atsc.GPS_EPOCH - atsc.DEFAULT_LEAP_SECONDS
    assert guide.channels[3] == ("7.1", "KABC")
    # the description arrives later in the ETT and updates the event
    (key, (again,)), = feed(ett(3, 42, "Local and world news."))
    assert key == f"{0x0815}:3" and again.description == "Local and world news."
    # once known, EIT events come out at once
    (key, evs), = feed(eit(3, [(43, gps + 1800, 3600, "Movie")]))
    assert [e.title for e in evs] == ["Movie"]
    # the STT corrects the leap seconds for later events
    feed(section(0xCD, 0, bytes([0]) + struct.pack(">IBH", gps, 19, 0)))
    (key, evs), = feed(eit(3, [(44, gps + 5400, 60, "Weather")]))
    assert evs[0].start == gps + 5400 + atsc.GPS_EPOCH - 19


@pytest.mark.skipif(GstMpegts is None, reason="GstMpegts missing")
def test_psip_from_gstreamer_sections():
    """The way the tuner gets them: GstMpegts sections (read as raw bytes)."""
    guide = atsc.AtscGuide()
    guide.feed(GstMpegts.Section.new(0x1FFB, tvct(1, [("WXYZ", 4, 2, 5, 9)])), GstMpegts)
    (key, evs), = guide.feed(GstMpegts.Section.new(
        0x1D00, eit(9, [(1, 1_500_000_000, 600, "Show")])), GstMpegts)
    assert key == "1:5" and evs[0].title == "Show"
    assert guide.channels[5] == ("4.2", "WXYZ")


def test_gps_time():
    assert atsc.gps_to_unix(0, 0) == 315964800
    assert atsc.etm_id(3, 42) == (3 << 16) | (42 << 2) | 2


def test_dvbv5_keeps_virtual_channel_and_ts_id():
    text = ("[KABC-HD]\n\tSERVICE_ID = 3\n\tVCHANNEL = 7.1\n\tTRANSPORT_ID = 2069\n"
            "\tVIDEO_PID = 49\n\tAUDIO_PID = 52\n\tFREQUENCY = 177000000\n"
            "\tMODULATION = VSB/8\n\tDELIVERY_SYSTEM = ATSC\n")
    (c,) = parse_dvbv5(text)
    assert (c.vchannel, c.tsid, c.sid, c.delsys) == ("7.1", 2069, 3, "atsc")
    assert c.epg_key == "2069:3"
    (again,) = parse_dvbv5(write_dvbv5([c]))
    assert (again.vchannel, again.tsid) == ("7.1", 2069)


def test_atsc_captures_whole_stream():
    from lindvb.channels import Channel
    from lindvb.config import DEFAULTS
    from lindvb.tuner import dvbsrc_properties
    c = Channel(name="KABC", delsys="atsc", frequency=177000, sid=3, pmt_pid=48,
                video_pid=49, audio_pids=[52], modulation="8vsb")
    assert dvbsrc_properties(c, DEFAULTS, 0, 0)["pids"] == "8192"
