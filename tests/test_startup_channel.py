"""The channel tuned at start: last, first of the list or a chosen one (no GTK)."""

from lindvb.channels import Channel, ChannelDB, startup_channel


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    db = ChannelDB()
    db.channels = [Channel(name=f"C{i}", delsys="iptv", url=f"http://x/{i}") for i in range(3)]
    db.renumber()
    return db


def test_last_channel_is_the_default(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    last = db.channels[2]
    assert startup_channel({"last_channel": last.key}, db, db.channels[0]) is last
    assert startup_channel({}, db, db.channels[0]) is None


def test_first_channel_of_the_list(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    s = {"startup_channel": "first", "last_channel": db.channels[2].key}
    assert startup_channel(s, db, db.channels[1]) is db.channels[1]
    assert startup_channel(s, db, None) is None


def test_chosen_channel(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    s = {"startup_channel": "fixed", "startup_channel_key": db.channels[1].key,
         "last_channel": db.channels[2].key}
    assert startup_channel(s, db, db.channels[0]) is db.channels[1]
    s["startup_channel_key"] = "gone"
    assert startup_channel(s, db, db.channels[0]) is None
