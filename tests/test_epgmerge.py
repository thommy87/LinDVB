# SPDX-License-Identifier: GPL-2.0-or-later
"""EPG source priority: merging EIT, provider EPG, DVB-I and XMLTV."""

import json

from lindvb import epgmerge
from lindvb.channels import Channel
from lindvb.epg import EPGStore, Event

H = 3600
T0 = 1_900_000_000 // H * H         # a whole hour, far in the future
NOW = T0 + 10 * 60                  # 10 minutes into the first programme


def ev(start_h, dur_min, title, desc="", **kw):
    return Event(int(T0 + start_h * H), dur_min * 60, title, description=desc, **kw)


def titles(evs):
    return [e.title for e in evs]


def test_single_source_is_returned_sorted():
    out = epgmerge.merge({"eit": [ev(1, 60, "B"), ev(0, 60, "A")]}, now=NOW)
    assert titles(out) == ["A", "B"]


def test_auto_now_next_from_eit_preview_from_xmltv_without_duplicates():
    # EIT: only now/next, the programme runs 5 minutes late
    eit = [ev(0, 65, "News live"), ev(65 / 60, 60, "Film")]
    # XMLTV: a week ahead with descriptions, the original times
    xmltv = [ev(h, 60, t, "about " + t) for h, t in
             [(0, "News"), (1, "Film"), (2, "Talk"), (3, "Late show")]]
    xmltv += [ev(24 * d, 60, f"Day {d}", "x") for d in range(1, 7)]
    out = epgmerge.merge({"eit": eit, "xmltv": xmltv}, now=NOW)
    # now and next as broadcast, then XMLTV; nothing twice
    assert titles(out)[:4] == ["News live", "Film", "Talk", "Late show"]
    assert len(out) == 4 + 6
    assert out[0].start == T0 and out[0].duration == 65 * 60
    # the EIT film borrowed the XMLTV description
    assert out[1].description == "about Film"
    # the talk show after the late film starts when the film ends
    assert (out[2].start, out[2].end) == (T0 + 2 * H + 5 * 60, T0 + 3 * H)
    starts = [e.start for e in out]
    assert starts == sorted(starts)


def test_gaps_are_filled_from_lower_sources():
    xmltv = [ev(0, 60, "A", "d"), ev(1, 60, "B", "d"), ev(5, 60, "F", "d")] + \
        [ev(24 * 5, 60, "far", "d")]
    dvbi = [ev(h, 60, f"dvbi{h}") for h in range(0, 8)]
    out = epgmerge.merge({"xmltv": xmltv, "dvbi": dvbi}, now=NOW)
    # xmltv is the most complete (5 days); dvbi fills 2:00-5:00 and 6:00-8:00
    assert titles(out) == ["A", "B", "dvbi2", "dvbi3", "dvbi4", "F", "dvbi6", "dvbi7", "far"]


def test_slightly_shifted_times_do_not_duplicate():
    eit = [ev(0, 60, "A"), ev(1, 60, "B"), ev(2, 60, "C")]
    xmltv = [ev(1 + 1 / 60, 60, "B", "d"), ev(2 + 1 / 60, 60, "C", "d"), ev(3, 60, "D", "d")]
    out = epgmerge.merge({"eit": eit, "xmltv": xmltv}, order=["eit", "xmltv"], now=NOW)
    assert titles(out) == ["A", "B", "C", "D"]
    assert out[1].description == "d" and out[2].description == "d"


def test_custom_order_wins_for_now_next_too():
    eit = [ev(0, 60, "EIT now"), ev(1, 60, "EIT next")]
    xmltv = [ev(0, 60, "XML now", "d"), ev(1, 60, "XML next", "d")]
    out = epgmerge.merge({"eit": eit, "xmltv": xmltv}, order=["xmltv", "eit"], now=NOW)
    assert titles(out) == ["XML now", "XML next"]
    out = epgmerge.merge({"eit": eit, "xmltv": xmltv}, order=["eit"], now=NOW)
    assert titles(out) == ["EIT now", "EIT next"]


def test_auto_without_programme_on_air_uses_most_complete():
    eit = [ev(2, 60, "EIT later")]
    xmltv = [ev(h, 60, f"x{h}", "d") for h in range(1, 48)]
    out = epgmerge.merge({"eit": eit, "xmltv": xmltv}, now=NOW)
    assert "EIT later" not in titles(out) and out[0].title == "x1"


def test_provider_epg_beats_dvbi_for_now_next():
    prov = [ev(0, 60, "prov now"), ev(1, 60, "prov next")]
    dvbi = [ev(h, 60, f"d{h}", "x") for h in range(0, 30)]
    out = epgmerge.merge({"provider": prov, "dvbi": dvbi}, now=NOW)
    assert titles(out)[:3] == ["prov now", "prov next", "d2"]


def test_normalize_order():
    assert epgmerge.normalize_order(["xmltv", "bogus", "xmltv"]) == \
        ["xmltv", "eit", "provider", "dvbi"]
    assert epgmerge.normalize_order(None) == epgmerge.DEFAULT_ORDER


def test_store_merges_linked_keys_and_channel_order(tmp_path, monkeypatch):
    store = EPGStore(persist=False)
    store.add("1:2", ev(0, 60, "EIT now"), "eit")
    store.add("1:2", ev(1, 60, "EIT next"), "eit")
    store.add("1:2", ev(0, 60, "prov now"), "provider")
    for h in range(0, 30):
        store.add("xmltv:a.de", ev(h, 60, f"x{h}", "d"), "xmltv")
    store.linker = lambda: {"1:2": ["xmltv:a.de"]}
    store.relink()
    monkeypatch.setattr("time.time", lambda: NOW)
    now, nxt = store.now_next("1:2")
    assert (now.title, nxt.title) == ("EIT now", "EIT next")
    assert len(store.for_key("1:2")) == 30
    # the linked XMLTV key is part of the channel, not a guide of its own
    assert store.keys() == ["1:2"]
    assert [k for k, _e in store.search("x5")] == ["1:2"]
    # per channel override
    store.channel_orders = {"1:2": ["xmltv", "eit", "provider", "dvbi"]}
    store.linker = lambda: {"1:2": ["xmltv:a.de"]}
    store._changed()
    assert store.now_next("1:2")[0].title == "x0"
    # global custom order
    store.channel_orders = {}
    store.set_priority("custom", ["provider", "eit"])
    store.linker = lambda: {"1:2": ["xmltv:a.de"]}
    assert store.now_next("1:2")[0].title == "prov now"


def test_store_keeps_sources_apart():
    """A provider guide no longer overwrites the EIT of the same key."""
    store = EPGStore(persist=False)
    store.add("1:2", ev(0, 60, "A"), "eit")
    store.add("1:2", ev(0, 30, "B"), "provider")
    assert set(store.events["1:2"]) == {"eit", "provider"}


def test_store_reads_old_cache_and_writes_sources(tmp_path, monkeypatch):
    path = tmp_path / "epg.json"
    monkeypatch.setattr(EPGStore, "path", path)
    old = {"1:2": [vars(ev(0, 60, "A"))], "xmltv:a": [vars(ev(0, 60, "B"))]}
    old = {k: [dict(e, access=[]) for e in v] for k, v in old.items()}
    path.write_text(json.dumps(old))
    store = EPGStore()
    assert store.events["1:2"].keys() == {"eit"}
    assert store.events["xmltv:a"].keys() == {"xmltv"}
    store.save()
    data = json.loads(path.read_text())
    assert data["version"] == 2 and data["events"]["xmltv:a"]["xmltv"][0]["title"] == "B"
    again = EPGStore()
    assert again.events.keys() == store.events.keys()


def test_channel_links():
    dvb = Channel(name="Das Erste", delsys="dvb-s2", frequency=11494, sid=28106, tsid=1101)
    assert dvb.epg_links == ["1101:28106"]
    dvb.dvbi_id = "tag:ard"
    dvb.dvbi_guide = {"schedule": "https://example.org/cg"}
    dvb.xmltv_id = "daserste.de"
    assert dvb.epg_links == ["1101:28106", "dvbi:tag:ard", "xmltv:daserste.de"]
