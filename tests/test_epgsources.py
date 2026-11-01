"""Provider EPG (MediaHighway / Freesat), DVB text and EPG language tests."""

import calendar
import os
import time

from lindvb.channels import Channel
from lindvb.epg import apply_timezone, posix_tz, read_xmltv
from lindvb.epgsources import (DEFAULT_PROVIDER_FREQS, MHWGrabber, ProviderEPG, dvb_text,
                               freesat_decode, freesat_encode, mpeg_crc, norm_lang,
                               parse_eit_raw, pick_language, provider_pids,
                               provider_types_for)


def _bcd(n):
    return ((n // 10) << 4) | (n % 10)


def _mjd(t):
    return t // 86400 + 40587


def _short(lang, name, text):
    body = lang.encode() + bytes([len(name)]) + name.encode("latin-1") + \
        bytes([len(text)]) + text.encode("latin-1")
    return bytes([0x4D, len(body)]) + body


def _eit(sid, tsid, events, table_id=0x50):
    loop = b""
    for event_id, start, dur, descs in events:
        mjd = _mjd(start)
        h, m, s = time.gmtime(start)[3:6]
        d = b"".join(descs)
        loop += bytes([event_id >> 8, event_id & 0xFF, mjd >> 8, mjd & 0xFF,
                       _bcd(h), _bcd(m), _bcd(s),
                       _bcd(dur // 3600), _bcd(dur % 3600 // 60), _bcd(dur % 60),
                       (len(d) >> 8) & 0x0F, len(d) & 0xFF]) + d
    body = bytes([sid >> 8, sid & 0xFF, 0xC1, 0, 0, tsid >> 8, tsid & 0xFF, 0, 1, 0, table_id]) + loop
    length = len(body) + 4
    head = bytes([table_id, 0xF0 | (length >> 8), length & 0xFF])
    sec = head + body
    return sec + mpeg_crc(sec).to_bytes(4, "big")


START = calendar.timegm((2030, 1, 1, 20, 15, 0))


def test_eit_raw_language_preference():
    sec = _eit(28106, 1101, [(7, START, 5400, [_short("eng", "News", "Evening"),
                                               _short("deu", "Nachrichten", "Abend")])])
    key, events = parse_eit_raw(sec, "ger")
    assert key == "1101:28106"
    ev, = events
    assert (ev.title, ev.subtitle, ev.start, ev.duration) == ("Nachrichten", "Abend", START, 5400)
    assert parse_eit_raw(sec, "")[1][0].title == "News"        # no preference: first
    assert parse_eit_raw(sec, "fra")[1][0].title == "News"     # missing: first


def test_language_helpers():
    assert norm_lang("ger") == "deu" and norm_lang("de") == "deu" and norm_lang("DE-at") == "deu"
    assert pick_language([("en", 1), ("de", 2)], "deu") == 2
    assert pick_language([], "deu") is None


def test_dvb_text():
    assert dvb_text(b"\x15Gr\xc3\xbc\xc3\x9fe") == "Grüße"              # UTF-8
    assert dvb_text(b"\x05M\xfcnchen") == "München"                     # ISO 8859-9
    assert dvb_text(b"M\xc8unchen") == "München"                        # ISO 6937 diaeresis
    assert dvb_text(b"Stra\xfbe") == "Straße"
    assert dvb_text(b"Line\x8aBreak") == "Line\nBreak"


def test_freesat_roundtrip():
    for table in (1, 2):
        data = freesat_encode("The Big Bang Theory", table)
        assert data[:2] == bytes([0x1F, table])
        assert freesat_decode(data) == "The Big Bang Theory"
        assert dvb_text(data) == "The Big Bang Theory"


def test_freesat_eit_through_packets():
    title = freesat_encode("EastEnders")
    body = b"eng" + bytes([len(title)]) + title + b"\x00"
    sec = _eit(10301, 2050, [(1, START, 1800, [bytes([0x4D, len(body)]) + body])], 0x60)
    got = []
    epg = ProviderEPG(["freesat"], lambda k, e: got.append((k, e)))
    # one TS packet on PID 3842 with the section
    payload = b"\x00" + sec
    pkt = bytes([0x47, 0x40 | (3842 >> 8), 3842 & 0xFF, 0x10]) + payload
    pkt += b"\xff" * (188 - len(pkt))
    epg.feed_buffer(pkt + pkt)                       # repeated section is ignored
    assert len(got) == 1
    key, (ev,) = got[0]
    assert key == "2050:10301" and ev.title == "EastEnders"


def _mhw2_channels(chans):
    sec = bytearray([0xC8, 0, 0, 0x00]) + bytearray(116) + bytearray([len(chans)])
    for onid, tsid, sid, _name in chans:
        sec += bytes([onid >> 8, onid & 0xFF, tsid >> 8, tsid & 0xFF, sid >> 8, sid & 0xFF, 0, 0])
    for *_ids, name in chans:
        sec += bytes([len(name)]) + name.encode()
    return bytes(sec)


def _mhw2_titles(entries):
    sec = bytearray([0xE6, 0, 0]) + bytearray(15)
    for ch, start, minutes, title, event_id in entries:
        mjd = _mjd(start)
        h, m = time.gmtime(start)[3:5]
        sec += bytes([ch]) + bytes(10)
        sec += bytes([mjd >> 8, mjd & 0xFF, _bcd(h), _bcd(m), 0,
                      (minutes << 4) >> 8, (minutes << 4) & 0xFF, len(title)])
        sec += title.encode() + bytes([0, event_id >> 8, event_id & 0xFF])
    return bytes(sec)


def _mhw2_summary(event_id, text):
    sec = bytearray([0x96, 0, 0, event_id >> 8, event_id & 0xFF]) + bytearray(9)
    sec += bytes([len(text)]) + text.encode() + bytes([0]) + bytes(4)
    return bytes(sec)


def test_mhw2_titles_channels_and_summaries():
    g = MHWGrabber()
    # titles before channels are kept until the channel table arrives
    assert g.feed(0x234, _mhw2_titles([(1, START, 90, "Pelicula", 0x1234)])) == []
    out = g.feed(0x231, _mhw2_channels([(1, 1032, 30701, "Uno"), (1, 1032, 30702, "Dos")]))
    (key, (ev,)), = out
    assert key == "1032:30702" and ev.title == "Pelicula" and ev.duration == 5400
    assert ev.start == START
    (key, (ev2,)), = g.feed(0x236, _mhw2_summary(0x1234, "Una historia."))
    assert ev2 is ev and ev.description == "Una historia."


def test_provider_matching_and_pids():
    ch = Channel(name="x", delsys="dvb-s2", frequency=11427500, polarity="h")
    assert provider_types_for(ch, DEFAULT_PROVIDER_FREQS) == ["freesat"]
    assert provider_types_for(Channel(name="y", delsys="dvb-s", frequency=11428000, polarity="v"),
                              DEFAULT_PROVIDER_FREQS) == []
    assert provider_types_for(Channel(name="z", delsys="dvb-c-a", frequency=11428000),
                              DEFAULT_PROVIDER_FREQS) == []
    assert provider_pids(["mhw2", "freesat"]) == [0x231, 0x234, 0x236, 3842, 3843]


def test_dvbsrc_pids_include_provider_epg():
    from lindvb.config import DEFAULTS
    from lindvb.tuner import dvbsrc_properties
    ch = Channel(name="x", delsys="dvb-s2", frequency=10847000, polarity="v", pmt_pid=100, sid=1)
    pids = dvbsrc_properties(ch, dict(DEFAULTS), 0, 0)["pids"].split(":")
    assert {"561", "564", "566", "100"} <= set(pids)


def test_manual_timezone():
    old = os.environ.get("TZ")
    try:
        assert posix_tz(60) == "GMT-01:00" and posix_tz(-330) == "GMT+05:30"
        apply_timezone({"epg_tz_auto": False, "epg_tz_offset": 120})
        assert time.localtime(0).tm_hour == 2
        apply_timezone({"epg_tz_auto": True})
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_xmltv_language_and_channels():
    xml = b"""<tv><channel id="DasErste.de"><display-name>Das Erste</display-name>
    <display-name>ARD</display-name></channel>
    <programme channel="DasErste.de" start="20300101120000 +0000" stop="20300101130000 +0000">
    <title lang="en">Daily News</title><title lang="de">Tagesschau</title></programme></tv>"""
    channels, items = read_xmltv(xml, "deu")
    assert channels == {"DasErste.de": ["Das Erste", "ARD"]}
    (cid, ev), = items
    assert cid == "DasErste.de" and ev.title == "Tagesschau"
    assert read_xmltv(xml)[1][0][1].title == "Daily News"
