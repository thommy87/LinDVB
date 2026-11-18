import time

import pytest

from lindvb import listfilter as lf
from lindvb import scanlists
from lindvb.channels import Channel
from lindvb.listfilter import State


def ch(name, **kw):
    return Channel(name=name, **kw)


TVH = dict(delsys="iptv", url="http://tvh/stream", tvh_server="http://tvh:9981", root="Home")
ASTRA = ch("Das Erste HD", delsys="dvb-s2", root="Astra-1KR-19.2E", diseqc=0)
ASTRA_ENC = ch("Sky Cinema", delsys="dvb-s2", root="Astra-1L-19.2E", encrypted=True)
HOTBIRD = ch("Rai 1", delsys="dvb-s", root="Hotbird-13.0E", diseqc=1)
PORT = ch("Mystery", delsys="dvb-s", root="Dish", diseqc=2)
CABLE = ch("ZDF", delsys="dvb-c-a", root="Vodafone", encrypted=True)
TERR = ch("WDR UHD", delsys="dvb-t2", root="Köln")
WEB = ch("Web TV", delsys="iptv", url="http://x", root="List 1")
WEB2 = ch("Web Sport", delsys="iptv", url="http://y", root="List 2")
DVBI = ch("DVB-I One", delsys="iptv", url="http://z", root="DE", dvbi_id="tag:1")
TVH_NEWS = ch("n-tv", groups=["News"], **TVH)
TVH_SPORT = ch("Sport1 HD", groups=["Sport", "13E"], **TVH)
ALL = [ASTRA, ASTRA_ENC, HOTBIRD, PORT, CABLE, TERR, WEB, WEB2, DVBI, TVH_NEWS, TVH_SPORT]
INFOS = [lf.info(c) for c in ALL]
CAT = lf.catalogue(ALL, INFOS, radio=False)


def shown(st, channels=ALL):
    return [c.name for c in channels if lf.matches(lf.info(c), st)]


def test_sources_of_the_channels():
    assert [lf.source_of(c) for c in (ASTRA, CABLE, TERR, WEB, DVBI, TVH_NEWS)] == \
        ["sat", "cable", "terr", "iptv", "dvbi", "tvh:http://tvh:9981"]
    assert CAT.sources == (
        ("sat", "Satellite"), ("cable", "Cable"), ("terr", "Terrestrial"),
        ("iptv", "Internet streams"), ("dvbi", "DVB-I"), ("tvh:http://tvh:9981", "Tvheadend: Home"))


def test_satellite_areas_are_orbital_positions_then_ports():
    assert lf.info(ASTRA).areas == {"sat/pos:192"} == lf.info(ASTRA_ENC).areas
    assert lf.info(PORT).areas == {"sat/port:2"}
    sections = lf.areas(CAT, frozenset({"sat"}))
    assert sections == [("Satellite", [("sat/pos:192", "19.2°E (Astra 1KR, Astra 1L)", ""),
                                       ("sat/pos:130", "13.0°E (Hotbird)", ""),
                                       ("sat/port:2", "DiSEqC port 3", "")])]


# channels found by a search: the group is the position, the satellites come
# from the transponder lists of the channel search
SCANNED = [ch("Das Erste HD", delsys="dvb-s2", root="19.2°E", frequency=11494000, polarity="h"),
           ch("ZDF HD", delsys="dvb-s2", root="19.2°E", frequency=11362000, polarity="h"),
           ch("Arte", delsys="dvb-s", root="19.2°E", frequency=10743750, polarity="h"),
           ch("Unknown", delsys="dvb-s", root="19.2°E", frequency=12999000, polarity="v"),
           ch("Rai 1", delsys="dvb-s2", root="13.0°E", frequency=10992000, polarity="v")]


def test_satellites_come_from_the_transponder_lists(monkeypatch):
    index = {(192, "h", 11494): {"Astra 1N"}, (192, "h", 11362): {"Astra 1M"},
             (192, "h", 10744): {"Astra 1M", "Astra 1P"}, (130, "v", 10992): {"Hot Bird 13G"}}
    monkeypatch.setattr(scanlists, "_satellite_index", lambda: index)
    scanlists.satellites.cache_clear()
    try:
        infos = [lf.info(c) for c in SCANNED]
        assert infos[2].areas == {"sat/pos:192", "sat/sat:192:Astra 1M", "sat/sat:192:Astra 1P"}
        assert infos[3].areas == {"sat/pos:192"}          # a transponder no list has
        cat = lf.catalogue(SCANNED, infos, radio=False)
        items = lf.areas(cat, frozenset())[0][1]
        assert items == [("sat/pos:192", "19.2°E", ""),
                         ("sat/sat:192:Astra 1M", "Astra 1M", "sat/pos:192"),
                         ("sat/sat:192:Astra 1N", "Astra 1N", "sat/pos:192"),
                         ("sat/sat:192:Astra 1P", "Astra 1P", "sat/pos:192"),
                         ("sat/pos:130", "13.0°E (Hot Bird 13G)", "")]
        one = State(areas=frozenset({"sat/sat:192:Astra 1M"}))
        assert [c.name for c, i in zip(SCANNED, infos) if lf.matches(i, one)] == \
            ["ZDF HD", "Arte"]
        assert lf.fit(one, cat) == one
    finally:
        scanlists.satellites.cache_clear()


def test_terrestrial_regions_and_their_channels():
    chans = [ch("Das Erste", delsys="dvb-t2", root="Berlin", frequency=490000),
             ch("ZDF", delsys="dvb-t2", root="Berlin", frequency=506000),
             ch("rbb", delsys="dvb-t2", root="Berlin", frequency=490000),
             ch("NDR", delsys="dvb-t2", root="Hamburg", frequency=226500)]
    infos = [lf.info(c) for c in chans]
    assert infos[0].areas == {"terr/root:Berlin", "terr/mux:490000:Berlin"}
    items = lf.areas(lf.catalogue(chans, infos, radio=False), frozenset())[0][1]
    assert items == [("terr/root:Berlin", "Berlin", ""),
                     ("terr/mux:490000:Berlin", "K 23 · 490 MHz", "terr/root:Berlin"),
                     ("terr/mux:506000:Berlin", "K 25 · 506 MHz", "terr/root:Berlin"),
                     ("terr/root:Hamburg", "Hamburg (K 12 · 226.5 MHz)", "")]
    st = State(areas=frozenset({"terr/mux:490000:Berlin"}))
    assert [c.name for c, i in zip(chans, infos) if lf.matches(i, st)] == ["Das Erste", "rbb"]
    assert lf.channel_label(522250) == "522.25 MHz"


def test_satellites_of_the_bundled_lists():
    assert "Astra 1P" in scanlists.satellites(192, 10758000, "v")
    # two spellings of one satellite in the lists are one name
    assert len([n for n in scanlists.satellites(130, 10727000, "h") if "13F" in n]) == 1


ITEMS = (("p", "19.2°E", ""), ("a", "Astra 1M", "p"), ("b", "Astra 1N", "p"),
         ("c", "Astra 1P", "p"), ("q", "13.0°E", ""))


@pytest.mark.parametrize("chosen, key, on, result", [
    (set(), "p", True, {"p"}),                       # a position stands for its satellites
    ({"a"}, "p", True, {"p"}),
    ({"p", "q"}, "p", False, {"q"}),
    ({"p"}, "b", False, {"a", "c"}),                 # one satellite off: its siblings stay
    ({"a", "b"}, "c", True, {"p"}),                  # the last one on: the position
    ({"a"}, "a", False, set()),
    (set(), "q", True, {"q"}),
])
def test_ticking_positions_and_satellites(chosen, key, on, result):
    assert lf.tick(ITEMS, frozenset(chosen), key, on) == result


def test_half_ticked_position():
    assert lf.tick_state(ITEMS, {"a"}, "p") == "part"
    assert lf.tick_state(ITEMS, {"p"}, "b") == "on"
    assert lf.tick_state(ITEMS, set(), "p") == "off"
    assert lf.tick_state(ITEMS, {"q"}, "q") == "on"


def test_areas_leave_out_sources_with_one_area():
    heads = [h for h, _items in lf.areas(CAT, frozenset())]
    assert heads == ["Satellite", "Internet streams"]          # cable, terrestrial: one each
    assert lf.areas(CAT, frozenset({"cable"})) == []


def test_sources_and_areas_filter():
    assert shown(State(sources=frozenset({"cable", "terr"}))) == ["ZDF", "WDR UHD"]
    # an area narrows only its own source
    st = State(sources=frozenset({"sat", "iptv"}), areas=frozenset({"sat/pos:130"}))
    assert shown(st) == ["Rai 1", "Web TV", "Web Sport"]
    st = State(areas=frozenset({"sat/pos:192", "iptv/root:List 2"}))
    assert shown(st) == ["Das Erste HD", "Sky Cinema", "ZDF", "WDR UHD", "Web Sport",
                         "DVB-I One", "n-tv", "Sport1 HD"]


def test_filter_sections():
    assert shown(State(reception="enc")) == ["Sky Cinema", "ZDF"]
    # internet streams are free to air, Tvheadend decrypts itself
    assert "Web TV" in shown(State(reception="fta")) and "n-tv" in shown(State(reception="fta"))
    assert shown(State(qualities=frozenset({"UHD", "HD"}))) == \
        ["Das Erste HD", "WDR UHD", "Sport1 HD"]
    # a tag shows the channels that have it, combined with the other sections
    assert shown(State(tags=frozenset({"Sport"}))) == ["Sport1 HD"]
    st = State(tags=frozenset({"News", "Sport"}), qualities=frozenset({"SD"}))
    assert "n-tv" in shown(st) and "Sport1 HD" not in shown(st)


def test_offer_follows_the_sources():
    o = lf.offer(CAT, frozenset({"iptv"}))
    assert not o.reception and o.qualities and o.tags == []
    o = lf.offer(CAT, frozenset())
    assert o.reception and o.tags == ["13E", "News", "Sport"]
    assert lf.offer(lf.catalogue([WEB], [lf.info(WEB)], radio=True), frozenset()).empty
    assert "transponder" not in dict(lf.sorts(CAT, frozenset({"iptv"})))
    assert "transponder" in dict(lf.sorts(CAT, frozenset()))


def test_fit_drops_what_is_no_longer_offered():
    st = State(sources=frozenset({"iptv", "gone"}), areas=frozenset({"sat/pos:192"}),
               reception="fta", qualities=frozenset({"HD"}), tags=frozenset({"News"}),
               sort="transponder", value="11494 H")
    assert lf.fit(st, CAT) == State(sources=frozenset({"iptv"}),
                                                       qualities=frozenset({"HD"}))
    st = State(sources=frozenset({"sat"}), areas=frozenset({"sat/pos:192"}), reception="fta",
               sort="provider", value="ARD")
    assert lf.fit(st, CAT) == st
    radio = lf.catalogue(ALL, INFOS, radio=True)
    assert lf.fit(State(qualities=frozenset({"HD"})), radio) == State()


def test_settings_round_trip_and_bad_values():
    st = State(frozenset({"sat"}), frozenset({"sat/pos:192"}), "enc", frozenset({"HD"}),
               frozenset({"News"}), "provider", "ARD")
    s = {}
    lf.save(s, "tv", st)
    assert lf.load(s, "tv") == st and lf.load(s, "radio") == State()
    bad = {"list_filters": {"tv": {"sources": "sat", "reception": "x", "qualities": ["8K"],
                                   "sort": "nope", "value": 3}}}
    assert lf.load(bad, "tv") == State()


@pytest.mark.parametrize("old, tv, extra", [
    ({"list_filter": "fta"}, State(reception="fta"), {}),
    ({"list_filter": 2}, State(reception="fta"), {}),             # an index, two versions ago
    ({"list_filter": "hd"}, State(qualities=frozenset({"UHD", "HD"})), {}),
    ({"list_filter": "iptv"}, State(sources=frozenset({"iptv", "dvbi"})), {}),
    ({"list_filter": "fav:2"}, State(), {"fav_last": 2, "list_tab": "favourites"}),
    ({"list_filter": 1}, State(), {"fav_last": 0, "list_tab": "favourites"}),
    ({"list_group": "src:cable"}, State(sources=frozenset({"cable"})), {}),
    ({"list_group": "src:sat:1"},
     State(sources=frozenset({"sat"}), areas=frozenset({"sat/pos:130"})), {}),
    ({"list_group": "Astra-1KR-19.2E"},
     State(sources=frozenset({"sat"}), areas=frozenset({"sat/pos:192"})), {}),
    ({"list_group": "Home", "list_tag": "News"},
     State(sources=frozenset({"tvh:http://tvh:9981"}), tags=frozenset({"News"})), {}),
    ({"list_sort": "provider", "list_sort_value": "ARD"}, State(sort="provider", value="ARD"), {}),
])
def test_migrate_older_settings(old, tv, extra):
    s = dict(old)
    lf.migrate(s, ALL)
    assert lf.load(s, "tv") == tv
    assert lf.load(s, "radio") == State(**{**tv.__dict__, "qualities": frozenset()})
    assert {k: v for k, v in s.items() if k != "list_filters"} == extra


def test_migrate_keeps_new_settings():
    s = {"list_filters": {"tv": {"reception": "enc"}}, "list_filter": "fta"}
    lf.migrate(s, ALL)
    assert s == {"list_filters": {"tv": {"reception": "enc"}}}
    s = {}
    lf.migrate(s, ALL)
    assert s == {}


# -- quality gate: a long list stays quick -------------------------------------------

def _many(n=20000):
    kinds = [dict(delsys="dvb-s2", root=f"{p / 10:.1f}°E", diseqc=p % 4, polarity=pol,
                  frequency=f) for p in (192, 130, 235, 282)
             for f in range(10700000, 12750000, 39000) for pol in "hv"] + \
            [dict(delsys="dvb-c-a", root="Kabel"), dict(delsys="dvb-t2", root="Berlin"),
             dict(delsys="iptv", url="http://x", root="List"),
             dict(TVH, groups=["News", "Sport"])]
    return [ch(f"Channel {n} {'HD' if n % 3 else ''}", number=n, encrypted=n % 5 == 0,
               category=f"Provider {n % 40}", **kinds[n % len(kinds)]) for n in range(n)]


def test_twenty_thousand_channels_filter_quickly():
    chans = _many()
    scanlists._satellite_index()             # read once per session, with the channel search
    t = time.perf_counter()
    infos = [lf.info(c) for c in chans]
    cat = lf.catalogue(chans, infos, radio=False)
    prepare = time.perf_counter() - t
    st = State(sources=frozenset({"sat", "tvh:http://tvh:9981"}),
               areas=frozenset({"sat/pos:192", "sat/pos:130"}), reception="fta",
               qualities=frozenset({"HD"}), tags=frozenset({"Sport"}))
    t = time.perf_counter()
    n = sum(1 for i in infos if lf.matches(i, st))
    refilter = time.perf_counter() - t
    t = time.perf_counter()
    lf.fit(st, cat)
    lf.areas(cat, st.sources)
    lf.offer(cat, st.sources)
    lf.sorts(cat, st.sources)
    steps = time.perf_counter() - t
    assert 0 < n < len(chans)
    assert refilter < 0.05, f"filtering took {refilter * 1000:.0f} ms"
    assert steps < 0.005, f"the steps took {steps * 1000:.1f} ms"
    assert prepare < 0.3, f"preparing took {prepare * 1000:.0f} ms"
