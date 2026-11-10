"""Tuning a single frequency: the scan file and the comparison with the channel list."""

import copy

from lindvb import scanlists as sl
from lindvb import singletune as st
from lindvb.channels import Channel, parse_dvbv5
from lindvb.scan import scan_command


def ch(name, sid, **kw):
    d = dict(delsys="dvb-s2", root="Astra", frequency=11494000, polarity="h",
             symbol_rate=22000, sid=sid, tsid=1019, onid=1, video_pid=sid + 100,
             audio_pids=[sid + 200], diseqc=0)
    d.update(kw)
    return Channel(name, **d)


class DB:
    def __init__(self, channels):
        self.channels = list(channels)

    def renumber(self):
        for i, c in enumerate(self.channels, 1):
            c.number = i


SAT = st.Tuning("dvb-s2", 11494000, "h", 22000, "8psk", "2/3", diseqc=0)


def test_scan_file_satellite_and_terrestrial():
    c = parse_dvbv5(st.scan_file(SAT))[0]
    assert (c.delsys, c.frequency, c.polarity, c.symbol_rate, c.modulation) == \
        ("dvb-s2", 11494000, "h", 22000, "8psk")
    t = st.Tuning("dvb-t2", 474000, bandwidth_hz=8000000, stream_id=1)
    text = st.scan_file(t)
    assert "INNER_FEC" not in text
    c = parse_dvbv5(text)[0]
    assert (c.delsys, c.frequency, c.bandwidth_hz, c.stream_id) == ("dvb-t2", 474000, 8000000, 1)
    c = parse_dvbv5(st.scan_file(st.Tuning("dvb-c-a", 114000, symbol_rate=6900,
                                           modulation="qam-256")))[0]
    assert (c.delsys, c.frequency, c.symbol_rate, c.modulation) == \
        ("dvb-c-a", 114000, 6900, "qam-256")


def test_scan_command_only_the_file(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/dvbv5-scan")
    assert "-F" in scan_command(0, 0, "in", "out", file_only=True)
    assert "-F" not in scan_command(0, 0, "in", "out")


def test_check():
    assert st.check(SAT) == ""
    assert st.check(st.Tuning("dvb-s", 11494000, "h", 22000, "8psk")) == "system"
    assert st.check(st.Tuning("dvb-c-a", 114000, symbol_rate=6900)) == ""
    assert st.check(st.Tuning("dvb-c-a", 114000, symbol_rate=0)) == "symbol_rate"
    assert st.check(st.Tuning("dvb-t", 5000, bandwidth_hz=8000000)) == "frequency"
    assert st.check(st.Tuning("atsc", 473000, modulation="8vsb")) == ""


def test_systems_for_the_tuner():
    assert st.systems_for(sl.SAT, ["dvb-s"]) == ["dvb-s"]
    assert st.systems_for(sl.TERR, ["dvb-t", "dvb-t2", "dvb-c-a"]) == ["dvb-t", "dvb-t2"]
    assert st.systems_for(sl.CABLE, []) == ["dvb-c-a", "dvb-c-b"]


def test_same_mux_only_this_transponder():
    assert st.same_mux(ch("a", 1), SAT)
    assert st.same_mux(ch("a", 1, frequency=11493000, delsys="dvb-s", diseqc=-1), SAT)
    assert not st.same_mux(ch("a", 1, polarity="v"), SAT)
    assert not st.same_mux(ch("a", 1, diseqc=1), SAT)              # another satellite
    assert not st.same_mux(ch("a", 1, frequency=11523000), SAT)
    assert not st.same_mux(Channel("a", delsys="iptv", url="http://x"), SAT)
    # the same frequency number on cable is not the terrestrial one
    ter = st.Tuning("dvb-t2", 474000, bandwidth_hz=8000000)
    assert st.same_mux(Channel("a", delsys="dvb-t", frequency=474000), ter)
    assert not st.same_mux(Channel("a", delsys="dvb-c-a", frequency=474000), ter)
    assert not st.same_mux(Channel("a", delsys="dvb-t2", frequency=474000, stream_id=1),
                           st.Tuning("dvb-t2", 474000, stream_id=0))


def test_compare_and_order():
    listed = [ch("Same", 1), ch("Audio", 2), ch("Renamed", 3), ch("Gone", 4),
              ch("Elsewhere", 5, frequency=11362000),
              Channel("Same", delsys="iptv", url="http://x")]
    found = [copy.deepcopy(c) for c in listed[:3]]
    found[1].audio_pids = [202, 302]
    found[2].name = "New name"
    found += [ch("Brand new", 9), ch("Other mux", 7, frequency=12000000)]
    rows = st.compare(found, listed, SAT)
    assert [(r.status, r.channel.name) for r in rows] == [
        (st.SAME, "Same"), (st.CHANGED, "Audio"), (st.CHANGED, "New name"),
        (st.NEW, "Brand new"), (st.GONE, "Gone")]
    assert rows[1].changes == [("audio_pids", [202], [202, 302])]
    assert rows[2].changes == [("name", "Renamed", "New name")]
    assert st.counts(rows) == {st.SAME: 1, st.CHANGED: 2, st.NEW: 1, st.GONE: 1}


def test_differences_dvbv5_does_not_know_everything():
    old = ch("A", 1, pmt_pid=100, teletext_pid=33, encrypted=True)
    new = ch("A", 1)
    assert st.differences(old, new) == []
    assert [n for n, _a, _b in st.differences(old, new, detailed=True)] == \
        ["teletext_pid", "encrypted"]
    # the list rounds the frequency differently, a default name does not replace a name
    new = ch("Service 1", 1, frequency=11493500, symbol_rate=21999)
    assert st.differences(old, new) == []


def test_apply_ticked_rows():
    listed = [ch("Same", 1), ch("Audio", 2, favourite=True, number=2), ch("Gone", 4),
              ch("Keep", 5)]
    db = DB(listed)
    found = [copy.deepcopy(listed[0]), copy.deepcopy(listed[1]), ch("Brand new", 9, root="x")]
    found[1].audio_pids = [202, 302]
    rows = st.compare(found, db.channels, SAT)
    ticked = [r for r in rows if r.status != st.SAME and r.channel.name != "Keep"]
    assert st.apply(db, ticked, group="Astra") == (1, 1, 1)
    assert [c.name for c in db.channels] == ["Same", "Audio", "Keep", "Brand new"]
    audio = db.channels[1]
    assert audio is listed[1] and audio.audio_pids == [202, 302] and audio.favourite
    assert db.channels[-1].root == "Astra" and db.channels[-1].number == 4
    # tuned again: everything applied is unchanged, the unticked line is still there
    again = st.compare(found, db.channels, SAT)
    assert st.counts(again) == {st.SAME: 3, st.CHANGED: 0, st.NEW: 0, st.GONE: 1}


def test_apply_keeps_favourites_when_the_key_changes():
    old = ch("A", 1, delsys="dvb-s")
    db = DB([old])
    new = copy.deepcopy(old)
    new.delsys = "dvb-s2"
    moved = []

    class Favs:
        def rekey(self, a, b):
            moved.append((a, b))

    rows = st.compare([new], db.channels, SAT)
    assert rows[0].status == st.CHANGED
    st.apply(db, rows, Favs())
    assert moved == [("dvb-s:11494000:h:1", "dvb-s2:11494000:h:1")]


def test_target_group():
    chans = [ch("a", 1, root="Astra 19.2°E"), ch("b", 2, root="Hotbird", diseqc=1,
                                                frequency=11000000)]
    assert st.target_group(chans, SAT, "19.2°E") == "Astra 19.2°E"
    other = st.Tuning("dvb-s2", 12000000, "v", 27500, diseqc=1)
    assert st.target_group(chans, other, "13.0°E") == "Hotbird"
    assert st.target_group(chans, st.Tuning("dvb-s", 12000000, "v", 27500, diseqc=2),
                           "28.2°E") == "28.2°E"
    assert st.target_group(chans, st.Tuning("dvb-c-a", 114000, symbol_rate=6900), "Cable") == "Cable"
