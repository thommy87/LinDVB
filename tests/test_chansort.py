from lindvb.channels import Channel
from lindvb import chansort


def ch(name, **kw):
    return Channel(name=name, **kw)


def test_quality_and_values():
    a = ch("Das Erste HD", category="ARD", frequency=11494000, polarity="h")
    b = ch("arte", category="ARD", frequency=10743750, polarity="h")
    c = ch("ZDF UHD", category="ZDFvision", frequency=11954000, polarity="h")
    r = ch("Radio 1", radio=True, category="ARD")
    assert [chansort.quality(x) for x in (a, b, c, r)] == ["HD", "SD", "UHD", ""]
    assert chansort.values([a, b, c], "provider") == ["ARD", "ZDFvision"]
    assert chansort.values([a, b, c], "quality") == ["UHD", "HD", "SD"]
    assert chansort.values([a, b, c], "transponder") == ["10744 H", "11494 H", "11954 H"]
    assert chansort.sort_key(a, "number") is None


def test_sort_orders():
    xs = [ch("b", category="Y"), ch("A", category="X"), ch("c", category="")]
    by = lambda s: [c.name for c in sorted(xs, key=lambda c: chansort.sort_key(c, s))]  # noqa: E731
    assert by("az") == ["A", "b", "c"]
    assert by("provider") == ["A", "b", "c"]      # without a provider last
    iptv = ch("Web", delsys="iptv", url="http://x")
    assert chansort.transponder(iptv) == ""


def test_sources():
    s1 = ch("Das Erste", delsys="dvb-s2", diseqc=0, root="Astra 19.2°E")
    s2 = ch("Rai 1", delsys="dvb-s2", diseqc=1, root="Hotbird 13.0°E")
    cab = ch("ZDF", delsys="dvb-c-a", root="Vodafone")
    web = ch("Web", delsys="iptv", url="http://x", root="List 1")
    assert chansort.sources([s1, cab]) == [("src:sat", "Satellite"), ("src:cable", "Cable")]
    assert chansort.sources([s1, s2]) == [("src:sat:0", "Satellite · DiSEqC port 1"),
                                         ("src:sat:1", "Satellite · DiSEqC port 2")]
    keys = [k for k, _l in chansort.sources([s1, s2, cab, web])]
    assert keys == ["src:sat", "src:sat:0", "src:sat:1", "src:cable", "src:iptv"]
    assert chansort.sources([s1]) == [] and chansort.sources([web]) == []
    assert chansort.in_source(s2, "src:sat") and chansort.in_source(s2, "src:sat:1")
    assert not chansort.in_source(s1, "src:sat:1") and not chansort.in_source(cab, "src:sat")
    assert chansort.in_source(web, "src:iptv")
    assert chansort.source_kind(ch("T", delsys="dvb-t2")) == "terr"
