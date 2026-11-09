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
