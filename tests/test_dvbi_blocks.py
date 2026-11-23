# SPDX-License-Identifier: GPL-2.0-or-later
"""Stage C, part 2 (concept section 9): DVB-I list blocks. The country's list
comes first, lists the viewer adds follow with numbers from 1000, 2000 ...;
a service in two lists is one channel from the earlier block, the other copy
a spare instance for failover; a block is removed as a whole."""

import types

import pytest

from lindvb import dvbi
from lindvb.channels import Channel, ChannelDB

from test_dvbi import SCANNED, SERVICE_LIST, FakeNet


def _svc(uid, lcn, block="", url=""):
    return Channel(name=uid.upper(), delsys="iptv", url=url or f"https://{block or 'de'}/{uid}",
                   dvbi_id=uid, lcn=lcn, dvbi_block=block,
                   dvbi_instances=[{"kind": "dash", "url": url or f"https://{block or 'de'}/{uid}"}])


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    d = ChannelDB()
    d.channels = [Channel(name="Sat 1", delsys="dvb-s2", frequency=11494, sid=1)]
    return d


ORDER = ["", "own1", "own2"]


def keys(db):
    return [(c.dvbi_id or c.name, c.dvbi_block, c.number) for c in db.channels]


def test_country_list_first_then_added_lists_with_their_numbers(db):
    db.replace_dvbi([_svc("a", 1), _svc("b", 2)], "", ORDER)
    db.replace_dvbi([_svc("c", 1001), _svc("d", 1002)], "own1", ORDER)
    assert keys(db) == [("a", "", 1), ("b", "", 2), ("c", "own1", 1001), ("d", "own1", 1002),
                        ("Sat 1", "", 1003)]
    # updating the country's list leaves the added list alone
    added, updated, removed = db.replace_dvbi([_svc("a", 1), _svc("e", 3)], "", ORDER)
    assert (added, updated, removed) == (1, 1, 1)
    assert [k[0] for k in keys(db)] == ["a", "e", "c", "d", "Sat 1"]


def test_a_service_in_two_lists_is_one_channel_from_the_earlier(db):
    db.replace_dvbi([_svc("a", 1), _svc("b", 2)], "", ORDER)
    added, updated, removed = db.replace_dvbi([_svc("b", 1001, "own1"), _svc("c", 1002, "own1")],
                                              "own1", ORDER)
    assert added == 1 and [k[:2] for k in keys(db) if k[0] == "b"] == [("b", "")]
    b = db.by_key("dvbi:b")
    # the added list's copy is a spare instance of the channel (failover)
    assert {"kind": "dash", "url": "https://own1/b", "block": "own1"} in b.dvbi_instances
    # the country's list is updated: the spare stays
    db.replace_dvbi([_svc("a", 1), _svc("b", 2)], "", ORDER)
    assert any(d.get("block") == "own1" for d in db.by_key("dvbi:b").dvbi_instances)
    # the added list is updated: its spare is made anew, not doubled
    db.replace_dvbi([_svc("b", 1001, "own1"), _svc("c", 1002, "own1")], "own1", ORDER)
    assert sum(d.get("block") == "own1" for d in db.by_key("dvbi:b").dvbi_instances) == 1
    # failover sees it as another instance
    alts = dvbi.alternatives(db.by_key("dvbi:b"), [])
    assert [a.url for a in alts] == ["https://own1/b"]


def test_the_earlier_list_takes_a_service_over(db):
    """The added list was there first; the country's list then brings the
    same service: one channel, the country's, with the favourite kept."""
    db.replace_dvbi([_svc("b", 1001, "own1"), _svc("c", 1002, "own1")], "own1", ORDER)
    db.by_key("dvbi:b").favourite = True
    added, updated, removed = db.replace_dvbi([_svc("a", 1), _svc("b", 2)], "", ORDER)
    b = [c for c in db.channels if c.dvbi_id == "b"]
    assert len(b) == 1 and b[0].dvbi_block == "" and b[0].favourite and b[0].number == 2
    assert {"kind": "dash", "url": "https://own1/b", "block": "own1"} in b[0].dvbi_instances
    assert (added, updated, removed) == (1, 1, 0)


def test_a_block_is_removed_as_a_whole(db):
    db.replace_dvbi([_svc("a", 1), _svc("b", 2)], "", ORDER)
    db.replace_dvbi([_svc("b", 1001, "own1"), _svc("c", 1002, "own1")], "own1", ORDER)
    assert db.remove_dvbi_block("own1") == 1
    assert [k[0] for k in keys(db)] == ["a", "b", "Sat 1"]
    assert all("block" not in d for d in db.by_key("dvbi:b").dvbi_instances)
    # the country's list goes, the added list stays
    db.replace_dvbi([_svc("c", 1002, "own1")], "own1", ORDER)
    assert db.remove_dvbi_block("") == 2
    assert [k[0] for k in keys(db)] == ["c", "Sat 1"]


def test_an_own_channel_order_is_kept(db):
    db.manual_order = True
    db.replace_dvbi([_svc("a", 1), _svc("b", 2)], "", ORDER)
    db.replace_dvbi([_svc("c", 1001, "own1")], "own1", ORDER)
    db.channels[:] = [db.channels[i] for i in (3, 2, 1, 0)]    # c b a Sat
    db.renumber()
    db.replace_dvbi([_svc("a", 1), _svc("b", 2), _svc("e", 3)], "", ORDER)
    assert [k[0] for k in keys(db)] == ["c", "b", "a", "Sat 1", "e"]


def test_lists_from_before_are_the_country_block(db):
    """Migration: channels saved before list blocks have no block, so they
    are the country's list."""
    old = Channel.from_dict({"name": "ARD", "dvbi_id": "tag:ard", "delsys": "iptv",
                             "url": "https://x", "lcn": 1})
    assert old.dvbi_block == ""
    db.channels.insert(0, old)
    added, updated, removed = db.replace_dvbi([_svc("tag:ard", 1)])
    assert (added, updated, removed) == (0, 1, 0)


# ---------------------------------------------------------------- settings of the blocks

def test_adding_and_removing_lists():
    s = {"dvbi_list_url": "https://de/list.xml"}
    a = dvbi.add_own_list(s, "https://x/a.xml", "A", "aut")
    b = dvbi.add_own_list(s, "https://x/b.xml", "", "")
    assert (a["id"], a["slot"], a["country"], a["name"]) == ("own1", 1, "AUT", "A")
    assert (b["id"], b["slot"], b["name"]) == ("own2", 2, "x")
    assert dvbi.add_own_list(s, "https://x/a.xml") is None            # already there
    assert dvbi.add_own_list(s, "https://de/list.xml") is None        # the country's list
    assert dvbi.block_order(s) == ["", "own1", "own2"]
    dvbi.remove_own_list(s, "own1")
    c = dvbi.add_own_list(s, "https://x/c.xml")
    assert (c["id"], c["slot"]) == ("own1", 1)          # the free number range again
    assert dvbi.block_order(s) == ["", "own2", "own1"]  # but after the earlier lists
    for i in range(dvbi.MAX_OWN_LISTS):
        dvbi.add_own_list(s, f"https://y/{i}.xml")
    assert len(dvbi.own_lists(s)) == dvbi.MAX_OWN_LISTS


def test_due_lists_and_reload_after_a_removal():
    s = {}
    a = dvbi.add_own_list(s, "https://x/a.xml")
    b = dvbi.add_own_list(s, "https://x/b.xml")
    a["last_check"] = b["last_check"] = 10**9
    b["etag"] = "e"
    assert dvbi.due_lists(s, now=10**9 + 60) == []
    assert dvbi.due_lists(s, now=10**9 + 25 * 3600) == [a, b]
    s["dvbi_auto_update"] = False
    assert dvbi.due_lists(s, now=10**9 + 25 * 3600) == []
    s["dvbi_auto_update"] = True
    dvbi.reload_lists_after(s, "own1")
    assert a["last_check"] == 10**9 and b["last_check"] == 0 and b["etag"] == ""
    dvbi.reload_lists_after(s, "")
    assert a["last_check"] == 0


def test_an_added_list_is_loaded_into_its_block(tmp_path):
    net = FakeNet({"https://other.example/list.xml": SERVICE_LIST})
    s = {"dvbi_country": "ITA", "dvbi_list_url": "https://it/list.xml", "dvbi_last_check": 5,
         "dvbi_etag": "it-etag", "dvbi_list_label": "Mine"}
    block = dvbi.add_own_list(s, "https://other.example/list.xml", "Other", "DEU")
    db = ChannelDB.__new__(ChannelDB)
    db.channels = []
    up = dvbi.Updater(s, tmp_path / "c", tmp_path / "l", fetcher=net, block=block)
    res = up.run(SCANNED, logos=False)
    assert not res.error and res.build.channels
    up.apply(res, db, now=10**9)
    chans = db.dvbi_channels()
    assert chans and all(c.dvbi_block == "own1" and c.dvbi_country == "DEU" for c in chans)
    assert min(c.lcn for c in chans) > 1000                 # its own number range
    assert all(c.root.startswith("DVB-I") and "Mine" not in c.root for c in chans)
    # the block keeps its own state; the country's list is untouched
    assert block["last_check"] == 10**9 and block["last_modified"]
    assert s["dvbi_last_check"] == 5 and s["dvbi_etag"] == "it-etag"
    assert s["dvbi_list_url"] == "https://it/list.xml"
    assert (tmp_path / "c" / "servicelist-own1.xml").exists()
    assert not (tmp_path / "c" / "servicelist.xml").exists()
    assert dvbi.LANGUAGES == dvbi.use_country("ITA").languages   # the chosen country's


REGISTRY_WITH_COUNTRY = b"""<?xml version="1.0" encoding="UTF-8"?>
<ServiceListEntryPoints xmlns="urn:dvb:metadata:servicelistdiscovery:2024">
  <ServiceListRegistryEntity><Name>CSR</Name></ServiceListRegistryEntity>
  <ProviderOffering>
    <Provider><Name>ORF</Name></Provider>
    <ServiceListOffering>
      <ServiceListName>ORF DVB-I</ServiceListName>
      <ServiceListURI contentType="application/xml"><URI>https://orf.example/list.xml</URI></ServiceListURI>
      <TargetCountry>AUT</TargetCountry>
    </ServiceListOffering>
  </ProviderOffering>
</ServiceListEntryPoints>"""


def test_probe_tells_lists_and_registries_apart():
    net = FakeNet({"https://csr.example/query?": REGISTRY_WITH_COUNTRY,
                   "https://csr.example/query": dvbi.DVBIError("HTTP 400"),
                   "https://list.example/": SERVICE_LIST,
                   "https://junk.example/": b"<html><body>hello</body></html>"})
    offers, sl = dvbi.probe_address("https://list.example/l.xml", fetcher=net)
    assert offers is None and sl.name == "Deutschland"
    offers, sl = dvbi.probe_address("https://csr.example/query", fetcher=net)
    assert sl is None and offers[0].url == "https://orf.example/list.xml"
    assert offers[0].countries == ["AUT"]
    with pytest.raises(dvbi.DVBIError):
        dvbi.probe_address("https://junk.example/x", fetcher=net)


# ---------------------------------------------------------------- the panel

pytest.importorskip("gi")

from test_dvbi_country_choice import FakeDB, panel_factory  # noqa: E402


@pytest.fixture
def panel_for(monkeypatch):
    yield panel_factory(monkeypatch)


class BlockDB(FakeDB):
    def remove_dvbi_block(self, block):
        return ChannelDB.remove_dvbi_block(self, block)

    def renumber(self):
        pass


def test_adding_a_service_list_makes_a_block_and_loads_it(panel_for):
    p = panel_for("DEU")
    calls = []
    p.app.update_dvbi = lambda **kw: calls.append(kw) or True
    p.own_entry.set_text("https://other.example/list.xml")
    p._own_probed("https://other.example/list.xml", "AUT", None,
                  types.SimpleNamespace(name="Other list"), "")
    b = dvbi.own_lists(p.s)
    assert [(x["url"], x["name"], x["country"]) for x in b] == [
        ("https://other.example/list.xml", "Other list", "AUT")]
    assert calls and calls[0]["block"] == "own1" and calls[0]["force"]
    assert p.own_entry.get_text() == ""
    # once only
    p._own_probed("https://other.example/list.xml", "AUT", None,
                  types.SimpleNamespace(name="Other list"), "")
    assert len(dvbi.own_lists(p.s)) == 1 and "already" in p.own_status.get_text()


def test_a_registry_offers_its_lists(panel_for, monkeypatch):
    p = panel_for("DEU")
    shown = []
    monkeypatch.setattr(p, "_choose_offer", lambda offers, country: shown.append(offers))
    offers = [dvbi.ListOffering("ORF", "https://orf/list.xml", countries=["AUT"])]
    p._own_probed("https://csr/query", "", offers, None, "")
    assert shown == [offers] and dvbi.own_lists(p.s) == []
    p._own_probed("https://x", "", None, None, "HTTP 404")
    assert "HTTP 404" in p.own_status.get_text()


def test_the_country_of_an_added_list_defaults_to_the_chosen_country(panel_for):
    p = panel_for("ITA")
    codes = [c for c, _n in p.own_countries]
    assert codes[p.own_country_dd.get_selected()] == "ITA" and codes[0] == ""
    p = panel_for("")
    assert p.own_country_dd.get_selected() == 0                # unknown
    # the add row is there before a country is chosen
    assert p.own_entry.get_parent().get_parent() is p.box and not p.body.get_visible()


def test_removing_an_added_list_keeps_the_country_list(panel_for):
    p = panel_for("DEU")
    p.app.db = BlockDB(0)
    p.app.db.channels = [_svc("a", 1), _svc("c", 1001, "own1")]
    p.s["dvbi_own_lists"] = []
    dvbi.add_own_list(p.s, "https://x/own.xml", "Own", "AUT")
    p._show_own_lists()
    p._remove_own("own1")
    assert [c.dvbi_id for c in p.app.db.channels] == ["a"] and dvbi.own_lists(p.s) == []


def test_a_country_change_removes_only_the_country_list(panel_for, monkeypatch):
    p = panel_for("DEU")
    p.app.db = BlockDB(0)
    p.app.db.channels = [_svc("a", 1), _svc("c", 1001, "own1")]
    for c in p.app.db.channels:
        c.dvbi_country = "DEU"
    p.app.db.channels[1].dvbi_country = "AUT"
    asked = []
    monkeypatch.setattr(p, "_confirm_switch", lambda code, n: asked.append((code, n)))
    codes = [c for c, _n, _ok in p.country_rows]
    p.country_dd.set_selected(codes.index("ITA"))
    assert asked == [("ITA", 1)]                          # the country's list only
    p._switch_answer("ITA", True)
    assert [c.dvbi_id for c in p.app.db.channels] == ["c"]


def test_the_next_due_list_is_updated(monkeypatch):
    from lindvb.app import LinDVBApp as App
    s = {"dvbi_list_url": "https://de", "dvbi_last_check": 10**12}
    a = dvbi.add_own_list(s, "https://x/a.xml")
    calls = []
    fake = types.SimpleNamespace(settings=s, _dvbi_busy=False,
                                 update_dvbi=lambda **kw: calls.append(kw) or True)
    assert App.dvbi_next(fake)
    assert calls[0]["block"] == "own1"
    a["last_check"] = 10**12
    calls.clear()
    assert not App.dvbi_next(fake) and calls == []
    s["dvbi_last_check"] = 0                             # the country's list first
    a["last_check"] = 0
    App.dvbi_next(fake)
    assert "block" not in calls[0]
    fake._dvbi_busy = True
    assert not App.dvbi_next(fake)
