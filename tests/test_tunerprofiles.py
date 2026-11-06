"""Tuner profiles and the choice of a tuner (no hardware needed)."""

import ctypes

import pytest

from lindvb import tunerprofiles as tp
from lindvb.channels import Channel

SETTINGS = {"lnb_lof1": 9750000, "lnb_lof2": 10600000, "lnb_slof": 11700000,
            "adapters": [0], "frontend": 0}


def sat(name, freq, pol="h", sid=1, diseqc=-1, tsid=0):
    return Channel(name, delsys="dvb-s2", frequency=freq, polarity=pol, symbol_rate=27500,
                   sid=sid, tsid=tsid, onid=1 if tsid else 0, diseqc=diseqc, pmt_pid=100 + sid)


def cable(name, freq, sid=1):
    return Channel(name, delsys="dvb-c-a", frequency=freq, symbol_rate=6900, sid=sid,
                   pmt_pid=100 + sid)


class Cap:
    def __init__(self, channel, profile, users):
        self.channel, self.tuner, self.users = channel, profile, set(users)
        self.channels = {channel.key: channel}
        self.adapter = profile["adapter"]


def prof(adapter, systems=("dvb-s", "dvb-s2"), **kw):
    p = tp.new_profile(adapter, 0, systems=systems)
    p.update(kw)
    return p


def test_kernel_struct_sizes():
    # struct dtv_property is packed to 76 bytes on every architecture
    assert ctypes.sizeof(tp._Property) == 76
    assert tp.FE_GET_PROPERTY & 0xFFFF == 0x6F53


def test_probe_missing_frontend():
    assert tp.probe_delivery_systems(99, 0) == []


def test_supports_and_labels():
    s2 = prof(0)
    assert tp.supports(s2, "dvb-s") and tp.supports(s2, "dvb-s2")
    assert not tp.supports(s2, "dvb-c-a")
    assert tp.supports(prof(1, ("dvb-t2",)), "dvb-t")
    assert tp.supports(prof(2, ()), "dvb-c-a")              # unknown: anything
    assert tp.systems_label(["dvb-s", "dvb-s2", "dvb-c-a"]) == "DVB-S, DVB-S2, DVB-C"


def test_active_profiles_order_and_legacy():
    s = dict(SETTINGS, adapters=[2, 3], frontend=1)
    assert [(p["adapter"], p["frontend"]) for p in tp.active_profiles(s)] == [(2, 1), (3, 1)]
    s = dict(SETTINGS, tuners=[prof(0), prof(1, priority=5), prof(2, enabled=False)])
    assert [p["adapter"] for p in tp.active_profiles(s)] == [1, 0]
    filled = tp.active_profiles(dict(SETTINGS, tuners=[prof(0, ())]),
                                systems_for=lambda a, f: ["dvb-c-a"])
    assert filled[0]["systems"] == ["dvb-c-a"]


def test_untouched_setting_uses_every_tuner_found():
    found = [prof(0), prof(1, ("dvb-c-a",))]
    assert len(tp.active_profiles(SETTINGS, detect=lambda: found)) == 2
    chosen = dict(SETTINGS, adapters=[1])
    assert [p["adapter"] for p in tp.active_profiles(chosen, detect=lambda: found)] == [1]


def test_merge_profiles_keeps_choices():
    saved = [dict(prof(0), priority=3, group="A", name="old"), prof(5)]
    detected = [dict(prof(0), name="TBS"), prof(1, ("dvb-c-a",))]
    out = tp.merge_profiles(saved, detected)
    by = {p["adapter"]: p for p in out}
    assert by[0]["name"] == "TBS" and by[0]["priority"] == 3 and by[0]["group"] == "A"
    assert by[1]["present"] and by[1]["enabled"]
    assert not by[5]["present"]
    legacy = tp.merge_profiles([], detected, legacy_adapters=[1])
    assert [p["enabled"] for p in legacy] == [False, True]


def test_same_transponder():
    a = sat("A", 11494000, "h", 1)
    assert tp.same_transponder(a, sat("B", 11493000, "h", 2))
    assert not tp.same_transponder(a, sat("B", 11494000, "v", 2))
    assert not tp.same_transponder(a, sat("B", 11494000, "h", 2, diseqc=1))
    assert not tp.same_transponder(a, cable("C", 11494000))
    assert not tp.same_transponder(sat("A", 11494000, tsid=1), sat("B", 11494000, tsid=2))
    assert tp.same_transponder(cable("A", 330000), cable("B", 330000, 2))


def test_choose_join_share_and_type():
    s0, c1 = prof(0), prof(1, ("dvb-c-a",))
    profiles = [s0, c1]
    a = sat("A", 11494000)
    rec = Cap(a, s0, {"rec"})
    assert tp.choose(a, "live", profiles, {0: rec}, SETTINGS) == ("join", rec)
    assert tp.choose(sat("B", 11494000, sid=2), "live", profiles, {0: rec}, SETTINGS) == \
        ("share", rec)
    # an encrypted channel with its own descrambler does not share
    how = tp.choose(sat("B", 11494000, sid=2), "live", [s0, prof(2)], {0: rec}, SETTINGS,
                    can_share=False)
    assert how[0] == "tune" and how[1]["adapter"] == 2
    # a satellite channel never lands on the cable tuner
    with pytest.raises(tp.NoTuner, match="busy recording"):
        tp.choose(sat("X", 12187000), "live", profiles, {0: rec}, SETTINGS)
    how = tp.choose(cable("K", 330000), "live", profiles, {0: rec}, SETTINGS)
    assert how == ("tune", c1, None)
    with pytest.raises(tp.NoTuner, match="No tuner for DVB-C"):
        tp.choose(cable("K", 330000), "live", [s0], {}, SETTINGS)


def test_choose_preempts_live_for_recording():
    s0 = prof(0)
    live = Cap(sat("A", 11494000), s0, {"live"})
    how = tp.choose(sat("B", 12187000), "rec", [s0], {0: live}, SETTINGS)
    assert how == ("tune", s0, live)
    busy = Cap(sat("A", 11494000), s0, {"rec"})
    with pytest.raises(tp.NoTuner, match="No free tuner"):
        tp.choose(sat("B", 12187000), "rec2", [s0], {0: busy}, SETTINGS)


def test_shared_cable():
    s0, s1 = prof(0, group="lnb1"), prof(1, group="lnb1")
    rec = Cap(sat("A", 11494000, "h"), s0, {"rec"})            # low band, horizontal
    # same band and polarisation: the second tuner may tune
    how = tp.choose(sat("B", 11000000, "h"), "live", [s0, s1], {0: rec}, SETTINGS)
    assert how == ("tune", s1, None)
    # vertical or high band would change the cable for the recording
    for ch in (sat("B", 11000000, "v"), sat("B", 12187000, "h")):
        with pytest.raises(tp.NoTuner):
            tp.choose(ch, "live", [s0, s1], {0: rec}, SETTINGS)
    # without a shared cable the tuners are independent
    how = tp.choose(sat("B", 12187000, "v"), "live", [prof(0), prof(1)], {0: rec}, SETTINGS)
    assert how[0] == "tune" and how[1]["adapter"] == 1
    # a live view on the cable is stopped for a recording on the other tuner
    live = Cap(sat("A", 11494000, "h"), s0, {"live"})
    how = tp.choose(sat("B", 12187000, "v"), "rec", [s0, s1], {0: live}, SETTINGS)
    assert how[0] == "tune" and how[2] is live


def test_tuner_lnb():
    assert tp.tuner_lnb(prof(0), SETTINGS) == (9750000, 10600000, 11700000)
    assert tp.tuner_lnb(prof(0, lnb=[10750000, 10750000, 11700000]), SETTINGS)[0] == 10750000


def test_tuner_manager_shares_and_releases(monkeypatch):
    try:
        from lindvb import tuner
    except (ImportError, ValueError):
        pytest.skip("GStreamer is missing")

    class FakeCapture:
        def __init__(self, channel, adapter, frontend, settings, provider=None, tuner=None):
            self.channel, self.adapter, self.frontend, self.tuner = channel, adapter, frontend, tuner
            self.channels = {channel.key: channel}
            self.users = set()
            self.destroyed = False

        def add_channel(self, channel):
            self.channels[channel.key] = channel

        def start(self):
            pass

        def destroy(self):
            self.destroyed = True

    monkeypatch.setattr(tuner, "Capture", FakeCapture)
    s = dict(SETTINGS, tuners=[prof(0), prof(1, ("dvb-c-a",))])
    tm = tuner.TunerManager(s, probe=lambda a, f: [])
    a, b = sat("A", 11494000), sat("B", 11494000, sid=2)
    rec = tm.acquire(a, "rec")
    live = tm.acquire(b, "live")
    assert live is rec and set(rec.channels) == {a.key, b.key}
    k = tm.acquire(cable("K", 330000), "rec2")
    assert k.adapter == 1 and k.tuner["systems"] == ["dvb-c-a"]
    tm.release(rec, "rec")
    assert not rec.destroyed
    tm.release(rec, "live")
    assert rec.destroyed and 0 not in tm.captures


def test_analysis_takes_only_free_tuners_and_gives_way():
    s0 = prof(0)
    live = Cap(sat("A", 11494000), s0, {"live"})
    with pytest.raises(tp.NoTuner, match="analysis"):
        tp.choose(sat("B", 12187000), tp.ANALYSIS, [s0], {0: live}, SETTINGS)
    bg = Cap(sat("A", 11494000), s0, {tp.ANALYSIS})
    assert tp.choose(sat("B", 12187000), "live", [s0], {0: bg}, SETTINGS) == ("tune", s0, bg)
    # the analysis goes before the live view
    s1 = prof(1)
    how = tp.choose(sat("C", 12500000), "rec", [s0, s1],
                    {0: live, 1: Cap(sat("B", 12187000), s1, {tp.ANALYSIS})}, SETTINGS)
    assert how[1] is s1 and how[2].users == {tp.ANALYSIS}
