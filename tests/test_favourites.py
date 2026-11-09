"""Favourites lists: migration, membership, order, renames (no GTK)."""

import json

from lindvb.channels import Channel, ChannelDB
from lindvb.favourites import FavouriteLists, fav_index, filter_keys, filter_setting


def _db(tmp_path, monkeypatch, n=5, favs=()):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    monkeypatch.setattr(FavouriteLists, "path", tmp_path / "favourites.json")
    db = ChannelDB()
    db.channels = [Channel(name=f"C{i}", delsys="iptv", url=f"http://x/{i}",
                           favourite=i in favs) for i in range(n)]
    db.renumber()
    return db


def test_first_start_makes_one_list_of_the_old_favourites(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, favs=(1, 3))
    favs = FavouriteLists(db)
    assert len(favs) == 1
    assert [c.name for c in favs.channels(0)] == ["C1", "C3"]
    assert [c.favourite for c in db.channels] == [False, True, False, True, False]


def test_lists_order_and_save(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    favs = FavouriteLists(db)
    c = db.channels
    sport = favs.create("Sport")
    for x in (c[4], c[0], c[2]):
        favs.add(sport, x)
    assert [x.name for x in favs.channels(sport)] == ["C4", "C0", "C2"]
    assert favs.move(sport, c[2], -1)
    assert [x.name for x in favs.channels(sport)] == ["C4", "C2", "C0"]
    assert favs.move_to_end(sport, c[0], top=True)
    assert [x.name for x in favs.channels(sport)] == ["C0", "C4", "C2"]
    assert favs.move_to(sport, c[0], c[2])          # down: after the target
    assert [x.name for x in favs.channels(sport)] == ["C4", "C2", "C0"]
    assert favs.move_to(sport, c[0], c[4])          # up: before the target
    assert [x.name for x in favs.channels(sport)] == ["C0", "C4", "C2"]
    assert not favs.move(sport, c[0], -1)           # already first
    # the channel numbers of the full list are not touched
    assert [x.number for x in c] == [1, 2, 3, 4, 5]
    favs.save()
    again = FavouriteLists(db)
    assert again.names() == ["Favourites", "Sport"]
    assert [x.name for x in again.channels(1)] == ["C0", "C4", "C2"]
    assert json.loads((tmp_path / "favourites.json").read_text())["lists"][1]["name"] == "Sport"


def test_membership_flag_names_and_delete(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    favs = FavouriteLists(db)
    c = db.channels
    kids = favs.create("Kids")
    favs.add(0, c[1])
    favs.add(kids, c[1])
    assert c[1].favourite and favs.member_of(c[1]) == [0, kids]
    favs.remove(0, c[1])
    assert c[1].favourite                           # still in "Kids"
    favs.toggle(kids, c[1])
    assert not c[1].favourite
    assert favs.create("Kids") == 2 and favs.names()[2] == "Kids 2"
    favs.rename(2, "Kids")
    assert favs.names()[2] == "Kids 2"              # names stay unique
    favs.add(2, c[0])
    favs.delete(2)
    assert favs.names() == ["Favourites", "Kids"] and not c[0].favourite
    assert favs.move_list(1, -1) == 0 and favs.names() == ["Kids", "Favourites"]


def test_rekey_and_missing_channels(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    favs = FavouriteLists(db)
    c = db.channels
    favs.add(0, c[2])
    favs.add(0, c[3])
    old = c[2].key
    c[2].url = "http://y/2"
    favs.rekey(old, c[2].key)
    assert [x.name for x in favs.channels(0)] == ["C2", "C3"]
    db.channels.remove(c[3])                        # gone for now: skipped, kept
    assert [x.name for x in favs.channels(0)] == ["C2"]
    assert len(favs.lists[0]["keys"]) == 2


def test_filter_settings():
    assert filter_keys(2) == ["all", "fav:0", "fav:1", "fta", "enc", "hd", "iptv"]
    assert filter_setting(1) == "fav:0" and filter_setting(2) == "fta"   # old index
    assert filter_setting(99) == "all" and filter_setting(None) == "all"
    assert filter_setting("fav:3") == "fav:3"
    assert fav_index("fav:3") == 3 and fav_index("hd") is None
