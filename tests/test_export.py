"""Channel list export: what each format takes, the LinDVB channel file, and
favourites lists coming back on import (no GTK)."""

import json

import pytest

from lindvb.channels import (Channel, ChannelDB, export_channels, export_fits, import_data,
                             parse_m3u, write_lindvb_json)
from lindvb.favourites import FavouriteLists


def _channels():
    return [Channel(name="Das Erste", delsys="dvb-s2", root="S19.2E", frequency=11494000,
                    polarity="h", symbol_rate=22000, sid=10301, tsid=1019, onid=1),
            Channel(name="Arte", delsys="iptv", url="https://x/arte.m3u8", category="de",
                    tvg_id="arte.de", logo_url="https://x/arte.png", logo="/home/a/arte.png"),
            Channel(name="Radio Eins", delsys="iptv", url="https://x/eins.mp3", radio=True),
            # DVB-I service received over the internet, with its other instances
            Channel(name="ZDF", delsys="iptv", url="https://x/zdf.mpd", dvbi_id="tag:zdf",
                    lcn=2, dvbi_instances=[{"delsys": "dvb-s2", "frequency": 11953000}]),
            # DVB-I service with no stream address (broadcast only)
            Channel(name="3sat", delsys="iptv", dvbi_id="tag:3sat")]


def test_each_format_takes_its_channels():
    chans = _channels()
    assert [c.name for c in chans if export_fits(c, "m3u")] == ["Arte", "Radio Eins", "ZDF"]
    assert [c.name for c in chans if export_fits(c, "dvbv5")] == ["Das Erste"]
    for fmt in ("csv", "json"):
        assert all(export_fits(c, fmt) for c in chans)


def test_only_internet_channels_give_an_empty_dvbv5_file_but_a_full_m3u():
    """Issue #110: with only IPTV and DVB-I lists the default .conf came out empty."""
    iptv = [c for c in _channels() if c.is_iptv]
    text, n = export_channels(iptv, "dvbv5")
    assert (text, n) == ("", 0)
    text, n = export_channels(iptv, "m3u")
    assert n == 3
    assert text.startswith("#EXTM3U")


def test_m3u_keeps_logo_and_radio():
    text, _n = export_channels(_channels(), "m3u")
    assert 'tvg-logo="https://x/arte.png"' in text
    assert 'radio="true"' in text
    back = {c.name: c for c in parse_m3u(text)}
    assert back["Arte"].logo_url == "https://x/arte.png"
    assert back["Arte"].tvg_id == "arte.de"
    assert back["Radio Eins"].radio and not back["Arte"].radio


def test_lindvb_file_round_trip_with_favourites():
    chans = _channels()
    lists = [{"name": "Sport", "keys": [chans[3].key, "url:https://gone", chans[0].key]},
             {"name": "Empty here", "keys": ["url:https://gone"]}]
    text, n = export_channels(chans, "json", lists)
    assert n == len(chans)
    data = json.loads(text)
    assert data["format"] == "lindvb-channels"
    # favourites keep only the channels in the file; empty lists are left out
    assert data["favourites"] == [{"name": "Sport", "keys": [chans[3].key, chans[0].key]}]
    # the downloaded logo is a file on this computer only
    assert all("logo" not in d for d in data["channels"])
    back = import_data(text.encode(), "backup.json")
    assert [c.name for c in back] == [c.name for c in chans]
    zdf = back[3]
    assert zdf.dvbi_id == "tag:zdf" and zdf.lcn == 2
    assert zdf.dvbi_instances == [{"delsys": "dvb-s2", "frequency": 11953000}]
    assert back[0].frequency == 11494000 and back[0].sid == 10301
    assert back[1].logo == "" and back[1].logo_url == "https://x/arte.png"


def test_lindvb_file_is_found_by_content():
    text = write_lindvb_json(_channels()[:1])
    assert [c.name for c in import_data(text.encode(), "no-extension")] == ["Das Erste"]


def test_broken_lindvb_file():
    with pytest.raises(ValueError):
        import_data(b'{"format": "lindvb-channels", "channels": [', "x.json")


def test_favourites_lists_come_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    monkeypatch.setattr(FavouriteLists, "path", tmp_path / "favourites.json")
    db = ChannelDB()
    db.channels = _channels()
    favs = FavouriteLists(db)
    favs.lists = [{"name": "Sport", "keys": [db.channels[0].key]}]
    changed = favs.merge_lists([
        {"name": "Sport", "keys": [db.channels[0].key, db.channels[1].key]},
        {"name": "Kids", "keys": [db.channels[2].key, db.channels[2].key]},
        {"name": "", "keys": [db.channels[3].key]},
        {"name": "Nothing", "keys": []}])
    assert changed == 2
    assert favs.names() == ["Sport", "Kids"]
    assert [c.name for c in favs.channels(0)] == ["Das Erste", "Arte"]
    assert [c.name for c in favs.channels(1)] == ["Radio Eins"]
    assert db.channels[2].favourite
    assert favs.merge_lists([{"name": "Sport", "keys": [db.channels[1].key]}]) == 0
