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
