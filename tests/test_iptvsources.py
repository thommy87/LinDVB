"""IPTV lists: HLS detection, tvg-logo, Xtream, update and remove (no GTK)."""
import pytest

from lindvb import iptvsources
from lindvb.channels import Channel, ChannelDB, import_data

M3U = b"""#EXTM3U
#EXTINF:-1 tvg-id="a" tvg-logo="http://x/a.png" group-title="News",A
http://s/a.m3u8
#EXTINF:-1,B
http://s/b.m3u8
"""
HLS = b"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000
high/index.m3u8
"""


def test_tvg_logo_and_single_hls_stream(tmp_path):
    a, b = import_data(M3U, "list.m3u")
    assert a.logo_url == "http://x/a.png" and b.logo_url == ""
    (one,) = import_data(HLS, "master.m3u8", source="https://cdn/live/master.m3u8")
    assert one.url == "https://cdn/live/master.m3u8" and one.name == "master"
    f = tmp_path / "news.m3u8"
    f.write_bytes(HLS)
    (local,) = import_data(HLS, str(f), source=str(f))
    assert local.url == "file://" + str(f)
    with pytest.raises(ValueError):
        import_data(HLS, "master.m3u8")


def test_xtream_url_and_display():
    url = iptvsources.xtream_url("tv.example:8080/", "me", "s3cret")
    assert url == "http://tv.example:8080/get.php?username=me&password=s3cret&type=m3u_plus&output=ts"
    assert iptvsources.display_location({"kind": "xtream", "location": url}) == \
        "tv.example:8080 (me)"


def test_update_keeps_favourites_and_drops_vanished(monkeypatch):
    db = ChannelDB.__new__(ChannelDB)
    db.channels = []
    db.merge(import_data(M3U, "list.m3u"))
    dvb = Channel(name="ZDF", frequency=11953000, sid=1, root="list")
    db.channels.append(dvb)
    db.channels[0].favourite = True
    new = [c for c in import_data(M3U, "list.m3u") if c.name == "A"]
    added, updated, gone = iptvsources.replace(db, "list", new)
    assert (added, updated, gone) == (0, 1, 1)
    assert [c.name for c in db.channels] == ["A", "ZDF"] and db.channels[0].favourite
    assert iptvsources.remove(db, "list") == 1 and db.channels == [dvb]
    srcs = iptvsources.remember([], "url", "http://l", "list", 2, now=5)
    srcs = iptvsources.remember(srcs, "url", "http://l", "list", 3, now=9)
    assert srcs == [{"kind": "url", "location": "http://l", "root": "list", "count": 3, "date": 9}]
    assert iptvsources.forget(srcs, "http://l") == []


PLUS = ('﻿#EXTM3U x-tvg-url="http://h/xmltv.php"\r\n'
        '#EXTINF:-1 tvg-id="ard.de" tvg-name="DE: Das Erste, HD" tvg-logo="" '
        'group-title="DE | Vollprogramm",DE: Das Erste, HD\r\n'
        'http://h:80/u/p/1.ts|User-Agent=VLC\r\n'
        '#EXTINF:-1 tvg-name="Film" group-title="Filme",Film\r\n'
        'http://h:80/movie/u/p/2.mkv\r\n'
        '#EXTINF:-1,Radio\r\n#EXTGRP:Radio\r\nhttp://h:80/u/p/3.ts\r\n').encode("utf-8")


def test_m3u_plus_with_bom_from_get_php():
    """Xtream m3u_plus lists come as "get.php" and often start with a BOM."""
    url = iptvsources.xtream_url("h:80", "u", "p", "m3u8")
    assert url.endswith("type=m3u_plus&output=m3u8")
    chans = import_data(PLUS, "get.php", source=url)
    assert [c.name for c in chans] == ["DE: Das Erste, HD", "Film", "Radio"]
    assert chans[0].url == "http://h:80/u/p/1.ts" and chans[0].tvg_id == "ard.de"
    assert chans[0].category == "DE | Vollprogramm" and chans[2].category == "Radio"
    assert [c.name for c in iptvsources.live_only(chans)] == ["DE: Das Erste, HD", "Radio"]


@pytest.mark.parametrize("name", ["tv.m3uplus", "tv.m3u8plus", "tv.m3u_plus", "TV.M3U8_PLUS"])
def test_m3u_plus_file_names(name):
    raw = b'#EXTINF:-1 group-title="A",One\nhttp://s/1\n'      # no #EXTM3U header
    (one,) = import_data(raw, name)
    assert one.name == "One" and one.root.lower() == "tv"


def test_list_names_and_rename():
    xt = iptvsources.xtream_url("tv.example:8080", "me", "pw")
    assert iptvsources.default_name("xtream", xt, "get") == "tv.example:8080 (me)"
    assert iptvsources.default_name("url", "https://cdn.example/live/index.m3u8",
                                    "index") == "cdn.example"
    assert iptvsources.default_name("url", "https://x/de.m3u", "de") == "de"
    srcs = iptvsources.remember([], "url", "http://a/index.m3u", "a", 1, now=1)
    assert iptvsources.unique_name(srcs, "a", "http://b/index.m3u") == "a 2"
    assert iptvsources.unique_name(srcs, "a", "http://a/index.m3u") == "a"
    db = ChannelDB.__new__(ChannelDB)
    db.channels = []
    db.merge([Channel(name="A", delsys="iptv", url="http://s/a", root="a")])
    srcs, used = iptvsources.rename(db, srcs, "http://a/index.m3u", " My TV ")
    assert used == "My TV" and srcs[0]["root"] == "My TV" and db.channels[0].root == "My TV"
