"""The transponder lists of the channel search: bundled dtv-scan-tables, sorting, blind scans."""

from lindvb import scanlists as sl


def lists():
    return sl.all_lists(system_dirs=())


def test_bundled_tables_are_there():
    bundled, commit = sl.bundled_lists()
    assert commit.startswith("97129f50")
    keys = {x.key for x in bundled}
    for key in ("dvb-t/de-Berlin", "dvb-c/de-Berlin", "dvb-s/Astra-1KR-19.2E", "atsc/us-CA-SF-Bay-Area",
                "isdb-t/br-Brazil"):
        assert key in keys
    assert len(bundled) > 3900
    assert (sl.BUNDLED_TABLES / "dtv-scan-tables-COPYING").exists()
    assert (sl.BUNDLED_TABLES / "dtv-scan-tables-COPYING.LGPL").exists()


def test_parse_position_and_format():
    assert sl.parse_position("Astra-1KR-19.2E") == 192
    assert sl.parse_position("Thor-5-0.8W") == -8
    assert sl.parse_position("Astra 19,2°E") == 192
    assert sl.parse_position("de-Berlin") is None
    assert sl.format_position(192) == "19.2°E"
    assert sl.format_position(-8) == "0.8°W"


def test_families_follow_the_tuner():
    assert sl.families_for(["dvb-c-a"]) == [sl.CABLE]
    assert sl.families_for(["dvb-t", "dvb-t2", "dvb-c-a"]) == [sl.CABLE, sl.TERR]
    assert sl.families_for(["dvb-s", "dvb-s2"]) == [sl.SAT]
    assert sl.families_for([]) == [sl.SAT, sl.CABLE, sl.TERR]


def test_cable_tuner_gets_cable_lists_by_country():
    ls = lists()
    codes = [c for c, _n in sl.countries(ls, sl.CABLE, ["dvb-c-a"])]
    assert "de" in codes and "at" in codes and codes[-1] == sl.OTHER_COUNTRY
    assert "us" not in codes                      # US cable is annex B
    regions = sl.regions(ls, sl.CABLE, ["dvb-c-a"], "de")
    assert "Kabel Vodafone Dresden" in [c.label for c in regions]
    assert all(x.family == sl.CABLE for c in regions for x in c.lists)
    assert regions[-1].blind == "cable"
    # a DVB-C list never offers satellites
    assert sl.positions(ls, ["dvb-c-a"]) == []


def test_terrestrial_regions_and_blind_scan():
    ls = lists()
    de = sl.regions(ls, sl.TERR, ["dvb-t", "dvb-t2"], "de")
    labels = [c.label for c in de]
    assert labels[0] == "Whole country" and "Bayern" in labels
    assert de[-1].blind == "eu"
    au = sl.regions(ls, sl.TERR, ["dvb-t"], "au")
    assert au[-1].blind == "au"
    other = sl.regions(ls, sl.TERR, ["dvb-t", "dvb-t2"], sl.OTHER_COUNTRY)
    assert [c.blind for c in other] == ["eu", "au", "us6", "tw"]
    # ATSC and ISDB-T only for tuners that have them
    assert "us" not in [c for c, _n in sl.countries(ls, sl.TERR, ["dvb-t"])]
    assert "us" in [c for c, _n in sl.countries(ls, sl.TERR, ["atsc"])]
    assert "br" in [c for c, _n in sl.countries(ls, sl.TERR, ["isdb-t"])]


def test_blind_scan_entries():
    eu = sl.scan_entries(sl.Choice("", blind="eu"), ["dvb-t"])
    assert len(eu) == 57 and {e["DELIVERY_SYSTEM"] for e in eu} == {"DVBT"}
    eu2 = sl.scan_entries(sl.Choice("", blind="eu"), ["dvb-t", "dvb-t2"])
    assert len(eu2) == 114 and {e["DELIVERY_SYSTEM"] for e in eu2} == {"DVBT", "DVBT2"}
    us6 = sl.scan_entries(sl.Choice("", blind="us6"), ["dvb-t"])
    assert us6[0]["BANDWIDTH_HZ"] == "6000000" and us6[-1]["FREQUENCY"] == "695000000"
    cab = sl.scan_entries(sl.Choice("", blind="cable"), ["dvb-c-a"])
    assert cab[0]["FREQUENCY"] == "114000000" and len(cab) == 282
    text = sl.write_entries(cab[:1])
    assert text.startswith("[CHANNEL]\n\tDELIVERY_SYSTEM = DVBC/ANNEX_A\n")
    assert sl.estimate_minutes(282) == 7


def test_one_position_covers_every_satellite_there():
    pos = sl.positions(lists(), ["dvb-s", "dvb-s2"])
    astra = pos[sl.find_position(pos, 192)]
    names = {x.key for x in astra.lists}
    assert {"dvb-s/Astra-1KR-19.2E", "dvb-s/Astra-1L-19.2E", "dvb-s/Astra-1M-19.2E",
            "dvb-s/Astra-1N-19.2E"} <= names
    assert astra.label.startswith("19.2°E (Astra 1KR")
    # Astra 2E (28.5°E) belongs to the 28.2°E dish position
    a28 = pos[sl.find_position(pos, 282)]
    assert "dvb-s/Astra-2E-28.5E" in {x.key for x in a28.lists}
    assert a28.position == 282
    # merged without duplicates: fewer transponders than the lists have together
    tpl = sl.satellite_list(astra, ["dvb-s", "dvb-s2"])
    total = sum(len(x.entries()) for x in astra.lists)
    keys = [(t.frequency // 1000, t.polarity) for t in tpl.transponders]
    assert len(keys) < total and len(keys) == len(set(keys)) and tpl.name == "19.2°E"
    # a DVB-S only tuner gets no DVB-S2 transponders
    s1 = sl.satellite_list(astra, ["dvb-s"])
    assert s1.transponders and all(t.delsys == "dvb-s" for t in s1.transponders)
    # positions run from east to west
    assert [c.position for c in pos] == sorted((c.position for c in pos), reverse=True)


def test_own_folder_and_dvbviewer_ini(tmp_path):
    (tmp_path / "DVB-C_de_Vodafone.ini").write_text(
        "[SATTYPE]\n1=4000\n2=Vodafone\n[DVB]\n0=1\n1=114000,256,6900\n")
    (tmp_path / "DVB-S_0130.ini").write_text(
        "[SATTYPE]\n1=130\n2=Hotbird 13.0°E\n[DVB]\n0=1\n1=10719,V,27500,56,1\n")
    (tmp_path / "my-list").write_text(
        "[CHANNEL]\n\tDELIVERY_SYSTEM = DVBT2\n\tFREQUENCY = 538000000\n\tBANDWIDTH_HZ = 8000000\n")
    ls = sl.all_lists([tmp_path], system_dirs=())
    vf = [c for c in sl.regions(ls, sl.CABLE, ["dvb-c-a"], "de") if c.label == "Vodafone"]
    assert vf and sl.scan_entries(vf[0])[0]["FREQUENCY"] == "114000000"
    hb = sl.positions(ls, ["dvb-s", "dvb-s2"])
    hb = hb[sl.find_position(hb, 130)]
    assert any(x.key.endswith("DVB-S_0130.ini") for x in hb.lists)
    own = sl.regions(ls, sl.TERR, ["dvb-t2"], sl.OWN_LISTS)
    assert [c.label for c in own] == ["my-list"]
    assert sl.OWN_LISTS in [c for c, _n in sl.countries(ls, sl.TERR, ["dvb-t2"])]


def test_system_table_with_bundled_name_is_left_out(tmp_path):
    d = tmp_path / "dvb-t"
    d.mkdir()
    (d / "de-Berlin").write_text("[CHANNEL]\n\tDELIVERY_SYSTEM = DVBT\n\tFREQUENCY = 1000\n")
    (d / "xx-Nowhere").write_text("[CHANNEL]\n\tDELIVERY_SYSTEM = DVBT\n\tFREQUENCY = 1000\n")
    ls = sl.all_lists(system_dirs=[tmp_path])
    assert [x.key for x in ls].count("dvb-t/de-Berlin") == 1


def test_default_target():
    ls = lists()
    pos = sl.positions(ls, [])
    assert sl.default_target(pos[sl.find_position(pos, 192)], sl.SAT) == "19.2°E"
    de = sl.regions(ls, sl.CABLE, ["dvb-c-a"], "de")[0]
    assert sl.default_target(de, sl.CABLE) == "Cable Germany"
