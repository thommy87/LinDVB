# SPDX-License-Identifier: GPL-2.0-or-later
from lindvb import picons
from lindvb.channels import Channel


def test_snp_and_position():
    assert picons.snp_name("Das Erste HD") == "daserstehd"
    assert picons.snp_name("ProSieben MAXX") == "prosiebenmaxx"
    assert picons.snp_name("Sky Sport 1 & 2+") == "skysport1and2plus"
    assert picons.snp_name("Arte ü") == "arteu"
    assert picons.orbital_position("Astra 19.2°E") == 192
    assert picons.orbital_position("Hotbird 13,0E") == 130
    assert picons.orbital_position("Thor 0.8°W") == 3592
    assert picons.orbital_position("Kabel") is None


def test_find_by_reference_then_name(tmp_path):
    for n in ("1_0_19_283D_3FB_1_C00000_0_0_0.png",       # Das Erste HD, 19.2°E
              "1_0_19_283D_3FB_1_820000_0_0_0.png",       # same ids at 13°E
              "1_0_1_2B66_3F3_1_FFFF0000_0_0_0.png",      # a cable channel
              "zdfhd.png", "readme.txt"):
        (tmp_path / n).write_bytes(b"x")
    ard = Channel("Das Erste HD", delsys="dvb-s2", root="Astra 19.2°E", sid=0x283D,
                  tsid=0x3FB, onid=1)
    hot = Channel("Das Erste HD", delsys="dvb-s2", root="Hotbird 13.0°E", sid=0x283D,
                  tsid=0x3FB, onid=1)
    cab = Channel("Kabel 1", delsys="dvb-c-a", sid=0x2B66, tsid=0x3F3, onid=1)
    zdf = Channel("ZDF HD", delsys="dvb-s2", root="Astra 19.2°E", sid=11110, tsid=1011, onid=1)
    none = Channel("Unknown", delsys="dvb-t2", sid=1, tsid=2, onid=3)
    folders = [str(tmp_path / "missing"), str(tmp_path)]
    assert picons.find(ard, folders).endswith("C00000_0_0_0.png")
    assert picons.find(hot, folders).endswith("820000_0_0_0.png")
    assert picons.find(cab, folders).endswith("FFFF0000_0_0_0.png")
    assert picons.find(zdf, folders).endswith("zdfhd.png")
    assert picons.find(none, folders) is None
    # an unknown satellite position still finds the reference
    ard.root = ""
    assert "283D_3FB_1" in picons.find(ard, folders)


def test_find_plain_file_names(tmp_path):
    # logos named after the channel, as the settings text promises
    for n in ("Bayern 3.png", "Radio_Bremen.svg", "arte.png", "arte HD.png"):
        (tmp_path / n).write_bytes(b"x")
    b3 = Channel("Bayern 3", delsys="dvb-s2", root="Astra 19.2°E", sid=1, tsid=2, onid=3)
    rb = Channel("Radio Bremen", delsys="dvb-c")
    arte = Channel("ARTE HD", delsys="dvb-s2", root="Astra 19.2°E")
    assert picons.find(b3, [str(tmp_path)]).endswith("Bayern 3.png")
    assert picons.find(rb, [str(tmp_path)]).endswith("Radio_Bremen.svg")
    assert picons.find(arte, [str(tmp_path)]).endswith("arte HD.png")
