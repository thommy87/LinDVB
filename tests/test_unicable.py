"""Unicable / JESS: commands, the direct tuner and the single-cable channel search."""

import pytest

from lindvb import directtune, festats, scscan, tunerprofiles as tp, unicable
from lindvb.channels import Channel

LNB = (9750000, 10600000, 11700000)
SETTINGS = {"lnb_lof1": 9750000, "lnb_lof2": 10600000, "lnb_slof": 11700000,
            "adapters": [0], "frontend": 0}


def sat(freq, pol="h", diseqc=-1, sid=1):
    return Channel("X", delsys="dvb-s2", frequency=freq, polarity=pol, symbol_rate=27500,
                   diseqc=diseqc, sid=sid, pmt_pid=100 + sid)


def test_en50494_command():
    uc = {"standard": "en50494", "slot": 0, "frequency": 1210000}
    # 11494 MHz H, low band: IF 1744 MHz, T = (1744 + 1210 + 2) / 4 - 350 = 389
    assert unicable.command(uc, sat(11494000, "h"), LNB) == bytes([0xE0, 0x10, 0x5A, 0x09, 0x85])
    # slot 3, position B, vertical, high band
    uc = {"standard": "en50494", "slot": 3, "frequency": 2040000}
    msg = unicable.command(uc, sat(12188000, "v", diseqc=1), LNB)
    t = (1588 + 2040 + 2) // 4 - 350
    assert msg == bytes([0xE0, 0x10, 0x5A, 3 << 5 | 1 << 4 | 1 << 2 | t >> 8, t & 0xFF])
    uc["pin"] = 17
    assert unicable.command(uc, sat(12188000, "v"), LNB)[2] == 0x5C
    assert unicable.command(uc, sat(12188000, "v"), LNB)[-1] == 17


def test_en50607_command():
    uc = {"standard": "en50607", "slot": 2, "frequency": 1420000}
    # 12188 MHz V, high band: IF 1588 MHz, T = 1488
    assert unicable.command(uc, sat(12188000, "v"), LNB) == bytes([0x70, 0x15, 0xD0, 0x01])
    msg = unicable.command(dict(uc, slot=31, pin=5), sat(11494000, "h", diseqc=5), LNB)
    assert msg[0] == 0x71 and msg[1] >> 3 == 31 and msg[3] == (5 << 2 | 2) and msg[4] == 5


def test_config_and_templates():
    assert unicable.config({}) is None
    assert unicable.config({"unicable": {"standard": "x", "frequency": 1}}) is None
    uc = {"standard": "en50607", "slot": 0, "frequency": 1210000}
    assert unicable.config({"unicable": uc}) is uc
    for kind, freqs in unicable.TEMPLATES.values():
        assert kind in unicable.STANDARDS and len(freqs) <= unicable.SLOTS[kind]
        assert all(950 <= f <= 2150 for f in freqs)
    assert "user band 1 at 1210 MHz" in unicable.describe(uc)


def test_send_sequence():
    calls = []
    unicable.send(9, b"\xe0\x10\x5a\x09\x85", repeats=1,
                  ioctl=lambda fd, req, arg: calls.append((req, arg)), sleep=lambda s: None)
    reqs = [r for r, _ in calls]
    assert reqs[:2] == [unicable.FE_SET_TONE, unicable.FE_SET_VOLTAGE]
    assert reqs.count(unicable.FE_DISEQC_SEND_MASTER_CMD) == 2
    cmd = next(a for r, a in calls if r == unicable.FE_DISEQC_SEND_MASTER_CMD)
    assert cmd[:5] == b"\xe0\x10\x5a\x09\x85" and cmd[6] == 5
    # 18 V for each command, back to 13 V after it
    volts = [a for r, a in calls if r == unicable.FE_SET_VOLTAGE]
    assert volts == [0, 1, 0, 1, 0]


def test_tuning_properties():
    props = dict(directtune.tuning_properties(sat(11494000), 1210000))
    assert props[festats.DTV_FREQUENCY] == 1210000
    assert props[festats.DTV_SYMBOL_RATE] == 27500000
    assert props[festats.DTV_DELIVERY_SYSTEM] == festats.DELIVERY_SYSTEMS.index("dvb-s2")
    assert props[festats.DTV_STREAM_ID] == directtune.NO_STREAM_ID_FILTER
    assert directtune.DTV_TUNE in props


def test_direct_tuner_with_fake_device():
    opened, closed, calls = [], [], []

    def opener(path, flags):
        opened.append(path)
        return len(opened) + 10

    tuner = directtune.DirectTuner(1, 0, opener=opener, closer=closed.append,
                                   ioctl=lambda fd, req, arg: calls.append((fd, req)),
                                   sleep=lambda s: None)
    tuner.open()
    assert opened == ["/dev/dvb/adapter1/frontend0", "/dev/dvb/adapter1/dvr0"]
    tuner.tune({"standard": "en50494", "slot": 1, "frequency": 1420000}, sat(11494000), LNB)
    reqs = [r for fd, r in calls if fd == tuner.fe]
    assert reqs[0] == directtune.FE_SET_PROPERTY and reqs[-1] == directtune.FE_SET_PROPERTY
    assert unicable.FE_DISEQC_SEND_MASTER_CMD in reqs
    tuner.set_pids([0, 17, 100])
    assert sorted(tuner.filters) == [0, 17, 100]
    tuner.set_pids([0, 8192])
    assert list(tuner.filters) == [directtune.ALL_PIDS]
    tuner.close()
    assert tuner.fe is None and tuner.dvr is None and not tuner.filters
    assert len(closed) == 6               # 3 + 1 filters, dvr, frontend


def test_dvb_stats():
    st = directtune.dvb_stats({"status": {"lock": True}, "cnr_db": 7.5, "signal_dbm": -50.0,
                               "post_ber": 1e-7, "block_errors": 3})
    assert st["lock"] and st["snr"] == 32767 and st["signal"] == 32767 and st["unc"] == 3


def test_single_cable_tuners_share_a_cable():
    uc = {"standard": "en50494", "frequency": 1210000}
    s0 = dict(tp.new_profile(0, systems=("dvb-s2",)), group="sc", unicable=dict(uc, slot=0))
    s1 = dict(tp.new_profile(1, systems=("dvb-s2",)), group="sc", unicable=dict(uc, slot=1))

    class Cap:
        def __init__(self, ch, prof):
            self.channel, self.tuner, self.users = ch, prof, {"rec"}
            self.channels = {ch.key: ch}

    rec = Cap(sat(11494000, "h"), s0)
    # another band and polarisation on the same cable: fine with single cable
    assert tp.choose(sat(12188000, "v"), "live", [s0, s1], {0: rec}, SETTINGS)[1] is s1
    # the same user band twice is not
    s1["unicable"]["slot"] = 0
    with pytest.raises(tp.NoTuner):
        tp.choose(sat(12188000, "v"), "live", [s0, s1], {0: rec}, SETTINGS)


# -- the channel search ----------------------------------------------------------

def _section(tid, ext, body, extra=b""):
    data = bytes([ext >> 8, ext & 0xFF, 0xC1, 0, 0]) + extra + body
    length = len(data) + 4
    return bytes([tid, 0xB0 | length >> 8, length & 0xFF]) + data + b"\0\0\0\0"


def _packet(pid, section):
    payload = b"\0" + section
    head = bytes([0x47, 0x40 | pid >> 8, pid & 0xFF, 0x10])
    return head + payload + b"\xff" * (184 - len(payload))


def _service(sid, name, provider, stype=1, scrambled=False):
    desc = bytes([stype, len(provider)]) + provider + bytes([len(name)]) + name
    desc = bytes([0x48, len(desc)]) + desc
    return bytes([sid >> 8, sid & 0xFF, 0xFC, (4 << 5) | (scrambled << 4) | len(desc) >> 8,
                  len(desc) & 0xFF]) + desc


def test_transponder_scan_builds_channels():
    pat = _section(0x00, 1101, bytes([0, 0, 0xE0, 0x10, 0, 28, 0xE1, 0x00, 0, 29, 0xE1, 0x10]))
    sdt = _section(0x42, 1101, _service(28, b"Das Erste", b"ARD") +
                   _service(29, b"Radio", b"ARD", stype=2), extra=bytes([0, 1, 0xFF]))
    pmt1 = _section(0x02, 28, bytes([0xE1, 0x01, 0xF0, 0x00,
                                     0x1B, 0xE1, 0x01, 0xF0, 0x00,
                                     0x03, 0xE1, 0x02, 0xF0, 0x00]))
    pmt2 = _section(0x02, 29, bytes([0xE1, 0x11, 0xF0, 0x00, 0x03, 0xE1, 0x11, 0xF0, 0x00]))
    ts = scscan.TransponderScan()
    assert ts.feed(_packet(0, pat))                      # the PMT PIDs are now known
    assert ts.wanted_pids() == {0, 0x11, 0x100, 0x110}
    ts.feed(_packet(0x11, sdt) + _packet(0x100, pmt1))
    assert not ts.complete
    ts.feed(_packet(0x110, pmt2))
    assert ts.complete
    tpl = scscan._Tp("dvb-s2", 11494000, "h", 22000)
    chans = {c.name: c for c in ts.channels(tpl, "Astra", diseqc=0)}
    tv, radio = chans["Das Erste"], chans["Radio"]
    assert (tv.sid, tv.tsid, tv.onid, tv.pmt_pid, tv.video_pid, tv.audio_pids) == \
        (28, 1101, 1, 0x100, 0x101, [0x102])
    assert tv.category == "ARD" and tv.root == "Astra" and not tv.radio
    assert radio.radio and radio.frequency == 11494000 and radio.polarity == "h"


def test_transponders_from_lists(tmp_path):
    from lindvb.scan import Transponder, TransponderList
    tpl = TransponderList("x", "sat", [Transponder("dvb-s2", 11494000, "h", 22000),
                                       Transponder("dvb-s2", 11494000, "h", 22000),
                                       Transponder("dvb-s", 12188000, "v", 27500)], "")
    assert [(t.frequency, t.polarity) for t in scscan.transponders(tpl)] == \
        [(11494000, "h"), (12188000, "v")]
    f = tmp_path / "S19.2E"
    f.write_text("[CHANNEL]\n\tDELIVERY_SYSTEM = DVBS2\n\tFREQUENCY = 10744000\n"
                 "\tPOLARIZATION = HORIZONTAL\n\tSYMBOL_RATE = 22000000\n")
    tps = scscan.transponders(TransponderList("y", "sat", [], str(f)))
    assert [(t.frequency, t.symbol_rate) for t in tps] == [(10744000, 22000)]
