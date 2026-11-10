"""The user's own transponder lists of satellite positions."""

from lindvb import satedit
from lindvb import scanlists as sl
from lindvb.scan import Transponder


def _choice():
    tps = [{"DELIVERY_SYSTEM": "DVBS", "FREQUENCY": "11494000", "POLARIZATION": "HORIZONTAL",
            "SYMBOL_RATE": "22000000", "INNER_FEC": "2/3"},
           {"DELIVERY_SYSTEM": "DVBS2", "FREQUENCY": "11362000", "POLARIZATION": "HORIZONTAL",
            "SYMBOL_RATE": "22000000", "MODULATION": "PSK/8", "INNER_FEC": "2/3"}]
    lst = sl.ScanList("dvb-s/Astra-19.2E", "dvb-s", region="Astra", position=192,
                      channels=tps)
    return sl.Choice("19.2°E", [lst], position=192)


def test_not_edited_uses_the_lists(tmp_path):
    c = _choice()
    assert not satedit.is_edited(192, tmp_path)
    assert satedit.load(192, tmp_path) is None
    tpl = satedit.satellite_list(c, (), tmp_path)
    assert [t.frequency for t in tpl.transponders] == [11362000, 11494000]


def test_save_load_reset(tmp_path):
    c = _choice()
    mine = [Transponder("dvb-s2", 12603000, "h", 22000, "8psk", "3/4"),
            Transponder("dvb-s", 10744000, "h", 22000, "qpsk", "5/6")]
    satedit.save(192, mine, tmp_path)
    assert satedit.is_edited(192, tmp_path)
    assert [(t.frequency, t.delsys, t.fec) for t in satedit.load(192, tmp_path)] == \
        [(10744000, "dvb-s", "5/6"), (12603000, "dvb-s2", "3/4")]
    tpl = satedit.satellite_list(c, ("dvb-s", "dvb-s2"), tmp_path)
    assert tpl.name == "19.2°E" and len(tpl.transponders) == 2
    # a DVB-S tuner leaves the DVB-S2 transponders out, as with the lists
    assert [t.frequency for t in satedit.satellite_list(c, ("dvb-s",), tmp_path).transponders] \
        == [10744000]
    satedit.reset(192, tmp_path)
    satedit.reset(192, tmp_path)                     # twice is fine
    assert not satedit.is_edited(192, tmp_path)
    assert len(satedit.satellite_list(c, (), tmp_path).transponders) == 2


def test_broken_file_and_bad_entries(tmp_path):
    (tmp_path / "sat-130.json").write_text("{nonsense")
    assert satedit.load(130, tmp_path) is None
    (tmp_path / "sat-130.json").write_text(
        '{"transponders": [{"frequency": 11034000, "polarity": "v", "symbol_rate": 27500},'
        ' {"frequency": 99, "polarity": "h", "symbol_rate": 27500}, {"polarity": "h"}]}')
    assert [t.frequency for t in satedit.load(130, tmp_path)] == [11034000]


def test_check():
    good = Transponder("dvb-s2", 11494000, "h", 22000, "8psk", "2/3")
    assert satedit.check(good) == ""
    assert satedit.check(Transponder("dvb-s", 11494000, "h", 22000, "8psk", "2/3")) == "system"
    assert satedit.check(Transponder("dvb-s", 500, "h", 22000)) == "frequency"
    assert satedit.check(Transponder("dvb-s", 11494000, "x", 22000)) == "polarity"
    assert satedit.check(Transponder("dvb-s", 11494000, "h", 10)) == "symbol_rate"


def test_only():
    c = _choice()
    tpl = sl.satellite_list(c)
    one = satedit.only(tpl, {(11494, "h")})
    assert [t.frequency for t in one.transponders] == [11494000]
    assert satedit.key(one.transponders[0]) == (11494, "h")


def test_circular_polarisation_reaches_the_scan_file():
    from lindvb.scan import TransponderList, write_scan_file
    tpl = TransponderList("36.0°E", "sat", [Transponder("dvb-s2", 11727000, "l", 27500),
                                             Transponder("dvb-s2", 11766000, "r", 27500)])
    text = write_scan_file(tpl)
    assert "POLARIZATION = LEFT" in text and "POLARIZATION = RIGHT" in text
    assert sl._transponder_entry(tpl.transponders[0])["POLARIZATION"] == "LEFT"
