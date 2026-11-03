"""Parser tests that run without GTK, GStreamer or DVB hardware."""

import time

from lindvb.channels import (Channel, ChannelDB, parse_dvbv5, parse_dvbviewer_ini, parse_m3u,
                             parse_vdr, parse_csv, write_csv, write_dvbv5, write_m3u)
from lindvb.epg import EPGStore, Event, parse_xmltv
from lindvb.recorder import file_name
from lindvb.scan import parse_transponder_ini, write_scan_file

DVBV5 = """[Example HD]
\tSERVICE_ID = 1001
\tVIDEO_PID = 101
\tAUDIO_PID = 102 103
\tPID_06 = 104
\tFREQUENCY = 11494000
\tPOLARIZATION = HORIZONTAL
\tSYMBOL_RATE = 22000000
\tINNER_FEC = 2/3
\tMODULATION = PSK/8
\tDELIVERY_SYSTEM = DVBS2

[Cable One]
\tSERVICE_ID = 7
\tAUDIO_PID = 70
\tFREQUENCY = 330000000
\tSYMBOL_RATE = 6900000
\tMODULATION = QAM/256
\tDELIVERY_SYSTEM = DVBC/ANNEX_A
"""


def test_dvbv5_roundtrip():
    chans = parse_dvbv5(DVBV5)
    sat, cable = chans
    assert sat.delsys == "dvb-s2" and sat.frequency == 11494000 and sat.polarity == "h"
    assert sat.symbol_rate == 22000 and sat.modulation == "8psk" and sat.fec == "2/3"
    assert sat.audio_pids == [102, 103] and sat.video_pid == 101
    assert cable.delsys == "dvb-c-a" and cable.frequency == 330000 and cable.radio
    again = parse_dvbv5(write_dvbv5(chans))
    assert [(c.name, c.frequency, c.sid, c.modulation) for c in again] == \
        [(c.name, c.frequency, c.sid, c.modulation) for c in chans]


def test_vdr():
    line = "Example HD;Provider:11494:HC23M5O35P0S1:S19.2E:22000:101=2:102=deu,103=mis;106=deu:104:0:1001:1:1019:0"
    c = parse_vdr(line)[0]
    assert c.name == "Example HD" and c.category == "Provider"
    assert c.delsys == "dvb-s2" and c.frequency == 11494000 and c.polarity == "h"
    assert c.video_pid == 101 and c.audio_pids[:2] == [102, 103] and c.teletext_pid == 104
    assert (c.sid, c.onid, c.tsid) == (1001, 1, 1019) and not c.encrypted


def test_dvbviewer_ini_merges_audio_tracks():
    text = """[Channel0]
TunerType=1
Root=Sat
Category=Test
Name=Example
SID=1001
PMTPID=100
VPID=101
APID=102
Frequency=11494
Polarity=h
Symbolrate=22000
SatModulation=134
Encrypted=24
[Channel1]
TunerType=1
Root=Sat
Name=Example (AC3)
SID=1001
VPID=101
APID=106
Frequency=11494
Polarity=h
Symbolrate=22000
SatModulation=134
[channel2]
TunerType=6
Root=Web
Name=Stream
VPID=0
APID=1
URL=https://example.org/live.m3u8
"""
    chans = parse_dvbviewer_ini(text)
    assert len(chans) == 2
    sat, web = chans
    assert sat.delsys == "dvb-s2" and sat.modulation == "8psk"
    assert sat.audio_pids == [102, 106] and sat.frequency == 11494000
    assert web.is_iptv and web.radio and web.url.endswith(".m3u8")


def test_m3u_and_csv():
    chans = parse_m3u('#EXTM3U\n#EXTINF:-1 group-title="News",News 24\nhttp://example.org/a.ts\n')
    assert chans[0].name == "News 24" and chans[0].category == "News" and chans[0].is_iptv
    assert "http://example.org/a.ts" in write_m3u(chans)
    back = parse_csv(write_csv(parse_dvbv5(DVBV5)))
    assert back[0].audio_pids == [102, 103] and back[1].radio


def test_merge_keeps_favourites(tmp_path, monkeypatch):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "c.json")
    db = ChannelDB()
    db.merge(parse_dvbv5(DVBV5))
    db.channels[0].favourite = True
    added, updated = db.merge(parse_dvbv5(DVBV5))
    assert (added, updated) == (0, 2) and db.channels[0].favourite
    db.save()
    assert ChannelDB().channels[0].favourite


def test_transponder_list(tmp_path):
    p = tmp_path / "DVB-S_0192.ini"
    p.write_text("[SATTYPE]\n1=0192\n2=Test Sat\n\n[DVB]\n0=2\n1=10744000,H,22000,56,1,|A\n"
                 "2=11494000,H,22000,23,134,|B\n")
    tl = parse_transponder_ini(p)
    assert tl.kind == "sat" and len(tl.transponders) == 2
    assert tl.transponders[0].delsys == "dvb-s" and tl.transponders[0].fec == "5/6"
    assert tl.transponders[1].delsys == "dvb-s2"
    text = write_scan_file(tl)
    assert "DELIVERY_SYSTEM = DVBS2" in text and "SYMBOL_RATE = 22000000" in text


def test_epg_now_next(tmp_path, monkeypatch):
    monkeypatch.setattr(EPGStore, "path", tmp_path / "epg.json")
    epg = EPGStore()
    now = int(time.time())
    epg.add("1:2", Event(now - 600, 1800, "Current"))
    epg.add("1:2", Event(now + 1200, 1800, "Later"))
    cur, nxt = epg.now_next("1:2")
    assert cur.title == "Current" and nxt.title == "Later"
    assert [k for k, _ in epg.search("later")] == ["1:2"]


def test_xmltv():
    xml = b"""<tv><channel id="a"><display-name>News 24</display-name></channel>
    <programme channel="a" start="20300101120000 +0000" stop="20300101130000 +0000">
    <title>Midday</title><desc>Text</desc></programme></tv>"""
    (name, ev), = parse_xmltv(xml)
    assert name == "news 24" and ev.title == "Midday" and ev.duration == 3600


def test_file_name_scheme():
    name = file_name("%station_%date_%event", "Das/Erste", "A: B", 0)
    assert "/" not in name and name.endswith(".ts") and ":" not in name


def test_channel_pids():
    c = Channel(name="x", pmt_pid=100, video_pid=101, audio_pids=[102], teletext_pid=104)
    assert {0, 17, 18, 100, 101, 102, 104} <= set(c.pids())


def test_transponder_lists_of_other_tv_systems(tmp_path):
    """DVBViewer's ISDB.ini, DTMB.ini, DVB-T_Australia.ini and ATSC lists."""
    def tl(fname, title, rows):
        p = tmp_path / fname
        p.write_text(f"[SATTYPE]\n1=5000\n2={title}\n\n[DVB]\n0={len(rows)}\n"
                     + "".join(f"{i}={r}\n" for i, r in enumerate(rows, 1)))
        return parse_transponder_ini(p)

    isdb = tl("ISDB.ini", "ISBD-T", ["473000,6"])
    assert isdb.kind == "terrestrial" and isdb.transponders[0].delsys == "isdb-t"
    assert "DELIVERY_SYSTEM = ISDBT" in write_scan_file(isdb)
    assert "BANDWIDTH_HZ = 6000000" in write_scan_file(isdb)

    dtmb = tl("DTMB.ini", "DTMB", ["474000,8,0,|CH21"])
    assert dtmb.transponders[0].delsys == "dtmb" and dtmb.transponders[0].label == "CH21"
    assert "DELIVERY_SYSTEM = DTMB" in write_scan_file(dtmb)

    au = tl("DVB-T_Australia.ini", "DVB-T Australia", ["177500,7"])
    assert au.transponders[0].delsys == "dvb-t"
    assert au.transponders[0].bandwidth_hz == 7000000

    t2 = tl("DVB-T2_Germany.ini", "DVB-T2 Deutschland", ["474000,8,4"])
    assert t2.transponders[0].delsys == "dvb-t2"

    us = tl("ATSC_America.ini", "America ATSC", ["57000,6"])
    assert us.kind == "atsc" and "MODULATION = VSB/8" in write_scan_file(us)
    cable = tl("ATSC_America_Cable.ini", "America Cable", ["57000,6"])
    text = write_scan_file(cable)
    assert "DELIVERY_SYSTEM = DVBC/ANNEX_B" in text and "MODULATION = QAM/256" in text
