"""Internet guides (XMLTV sources): the list, migration, channels per source (no GTK)."""
import gzip

from lindvb import xmltvsources
from lindvb.epg import EPGStore, Event, read_xmltv

GUIDE = b"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="cnn.us"><display-name>CNN</display-name></channel>
  <programme start="20300101120000 +0000" stop="20300101130000 +0000" channel="cnn.us">
    <title>News</title>
  </programme>
</tv>
"""


def test_names():
    assert xmltvsources.default_name("url", "https://free-epg.de/api/epg/us.xml.gz") == \
        "us (free-epg.de)"
    assert xmltvsources.default_name("url", "http://iptv.example/epg.xml") == "iptv.example"
    assert xmltvsources.default_name("file", "/home/me/German IPTV.xml") == "German IPTV"
    assert xmltvsources.normalize_url(" free-epg.de/x.xml ") == "https://free-epg.de/x.xml"


def test_add_touch_rename_forget():
    srcs, e = xmltvsources.add([], "url", "https://a/us.xml.gz")
    assert e["name"] == "us (a)" and e["date"] == 0
    same, e2 = xmltvsources.add(srcs, "url", "https://a/us.xml.gz")
    assert same == srcs and e2 is e
    srcs, e3 = xmltvsources.add(srcs, "url", "https://a/us.xml.gz?x=1")
    assert e3["name"] == "us (a) 2"
    srcs = xmltvsources.touch(srcs, "https://a/us.xml.gz", 3, 40, now=5)
    assert srcs[0]["programmes"] == 40 and srcs[0]["date"] == 5
    srcs = xmltvsources.touch(srcs, "https://a/us.xml.gz", 0, 0, error="timeout")
    assert srcs[0]["programmes"] == 40 and srcs[0]["error"] == "timeout"
    srcs, used = xmltvsources.rename(srcs, "https://a/us.xml.gz", "US")
    assert used == "US" and srcs[0]["name"] == "US"
    assert [s["location"] for s in xmltvsources.forget(srcs, "https://a/us.xml.gz")] == \
        ["https://a/us.xml.gz?x=1"]


def test_migrate_the_old_single_address():
    saved = {"xmltv_url": "https://free-epg.de/api/epg/de.xml.gz"}
    xmltvsources.migrate(saved)
    assert "xmltv_url" not in saved
    assert [s["location"] for s in saved["xmltv_sources"]] == \
        ["https://free-epg.de/api/epg/de.xml.gz"]
    empty = {"xmltv_url": ""}
    xmltvsources.migrate(empty)
    assert empty == {}


def test_fetch_file_and_channels_per_source(tmp_path):
    f = tmp_path / "us.xml.gz"
    f.write_bytes(gzip.compress(GUIDE))
    chans, items = read_xmltv(xmltvsources.fetch({"kind": "file", "location": str(f)}))
    assert chans == {"cnn.us": ["CNN"]} and len(items) == 1

    cache = tmp_path / "cache.json"
    by = {"a": {"cnn.us": ["CNN"], "bbc.uk": ["BBC One"]}, "b": {"cnn.us": ["CNN US"]}}
    xmltvsources.save_channels(by, cache)
    assert xmltvsources.load_channels(cache) == by
    srcs = [{"location": "a"}, {"location": "b"}]
    assert xmltvsources.all_channels(by, srcs) == {"cnn.us": ["CNN", "CNN US"],
                                                   "bbc.uk": ["BBC One"]}
    assert xmltvsources.orphaned(by, srcs, "a") == ["bbc.uk"]
    assert xmltvsources.orphaned(by, srcs, "b") == []


def test_store_drops_a_source():
    store = EPGStore(persist=False)
    store.add("xmltv:bbc.uk", Event(1900000000, 60, "News"), "xmltv")
    store.add("xmltv:bbc.uk", Event(1900000000, 60, "Nachrichten"), "eit")
    store.drop("xmltv:bbc.uk", "xmltv")
    assert list(store.events["xmltv:bbc.uk"]) == ["eit"]
    store.drop("xmltv:bbc.uk", "eit")
    assert "xmltv:bbc.uk" not in store.events
    store.drop("missing", "xmltv")
