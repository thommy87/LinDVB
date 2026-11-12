"""The channel list order: DVB-I numbers (LCN) or an order of the user's own,
and the group name of the DVB-I channels (no GTK)."""

from lindvb.channels import Channel, ChannelDB, number_order
from lindvb.dvbi import list_label


def _ch(name, lcn=0):
    return Channel(name=name, delsys="dvb-t2", frequency=500000 + ord(name[0]), sid=ord(name[0]),
                   lcn=lcn, dvbi_id=f"svc-{name}" if lcn else "")


def _db(tmp_path, monkeypatch, manual=False):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    db = ChannelDB()
    db.manual_order = manual
    db.channels = [_ch("A", 1), _ch("B", 2), _ch("C", 3), _ch("x"), _ch("y")]
    db.renumber()
    return db


def names(chans):
    return "".join(c.name for c in chans)


def test_lcn_channels_come_first_in_lcn_order():
    chans = [_ch("x"), _ch("B", 2), _ch("A", 1)]
    assert [(c.name, n) for c, n in number_order(chans)] == [("A", 1), ("B", 2), ("x", 3)]
    assert [c.number for c in chans] == [0, 0, 0]         # number_order changes nothing


def test_a_moved_dvbi_channel_snaps_back_without_an_own_order(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    db.channels[0], db.channels[1] = db.channels[1], db.channels[0]
    db.renumber()
    assert names(db.channels) == "ABCxy"                  # the old "arrows do nothing"


def test_an_own_order_keeps_the_list_order(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, manual=True)
    db.channels[:] = [db.channels[i] for i in (3, 1, 0, 4, 2)]
    db.renumber()
    assert names(db.channels) == "xBAyC"
    assert [c.number for c in db.channels] == [1, 2, 3, 4, 5]


def test_dvbi_update_keeps_the_own_order(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, manual=True)
    db.channels[:] = [db.channels[i] for i in (2, 3, 0, 4, 1)]       # C x A y B
    db.renumber()
    added, updated, removed = db.replace_dvbi([_ch("A", 1), _ch("C", 2), _ch("D", 3)])
    assert (added, updated, removed) == (1, 2, 1)
    assert names(db.channels) == "CxAyD"                  # B gone, D new at the end


def test_dvbi_update_without_own_order_uses_the_lcns(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    db.replace_dvbi([_ch("C", 2), _ch("A", 1)])
    assert names(db.channels) == "ACxy"


def test_list_label():
    # a registry list with a generic name: the country (and provider) say which one
    assert list_label(list_name="Source List", country="Italy") == "DVB-I Italy"
    assert list_label(list_name="Service List", sl_name="Service List", provider="IRIB",
                      country="Iran") == "DVB-I Iran · IRIB"
    assert list_label(list_name="Lista nazionale", provider="Rai", country="Italy") == \
        "DVB-I Italy · Lista nazionale · Rai"
    # a list loaded by its address: its own name, else the host
    assert list_label(list_name="lists.example.org", url="https://lists.example.org/sl.xml",
                      manual=True) == "DVB-I · lists.example.org"
    assert list_label(list_name="lists.example.org", sl_name="Community TV",
                      url="https://lists.example.org/sl.xml", manual=True) == \
        "DVB-I · Community TV"
    assert list_label(sl_name="DVB-I Testlist", manual=True) == "DVB-I Testlist"
    # the name set in the preferences wins
    assert list_label(custom="  Mein DVB-I ", list_name="Source List", country="Italy") == \
        "Mein DVB-I"
