"""DiSEqC 1.1 switches: commands, the setup, the direct tuner and the port labels."""

import pytest

from lindvb import diseqc, directtune, festats, unicable
from lindvb.channels import Channel

LNB = (9750000, 10600000, 11700000)


def sat(freq, pol="h", port=-1):
    return Channel("X", delsys="dvb-s2", frequency=freq, polarity=pol, symbol_rate=27500,
                   diseqc=port, sid=1, pmt_pid=101)


def test_mode_and_config():
    assert diseqc.mode({}) == "1.0" and diseqc.mode({"diseqc": "x"}) == "1.0"
    assert diseqc.mode({"diseqc": "1.1u"}) == "1.1u"
    # DiSEqC 1.0: dvbsrc tunes ports 1 to 4 itself
    assert diseqc.config({}, -1) is None and diseqc.config({}, 3) is None
    # port 5 and higher only exist behind a 1.1 switch
    assert diseqc.config({}, 4) == {"switch": "1.1"}
    assert diseqc.config({"diseqc": "1.1"}, 0) == {"switch": "1.1"}
    assert diseqc.config({"diseqc": "1.1u"}, -1) == {"switch": "1.1u"}
    assert diseqc.is_switch({"switch": "1.1"})
    assert not diseqc.is_switch({"standard": "en50494", "frequency": 1210000})
    assert not diseqc.is_switch(None)


def test_max_ports():
    assert diseqc.max_ports(None) == 4
    assert diseqc.max_ports({"diseqc": "1.1"}) == 16
    assert diseqc.max_ports({"type": "satip", "diseqc": "1.1"}) == 4
    uc = {"standard": "en50494", "frequency": 1210000}
    assert diseqc.max_ports({"unicable": uc}) == 2
    assert diseqc.max_ports({"unicable": dict(uc, standard="en50607")}) == 64


def test_cascade_commands():
    # port 7 = uncommitted input 2 (index 1), committed input 4 (index 3)
    low_h = diseqc.commands({"switch": "1.1"}, sat(11494000, "h", 6), LNB)
    assert low_h == [bytes([0xE0, 0x10, 0x39, 0xF1]), bytes([0xE0, 0x10, 0x38, 0xFA])]
    # port 16, vertical, high band
    high_v = diseqc.commands({"switch": "1.1"}, sat(12188000, "v", 15), LNB)
    assert high_v == [bytes([0xE0, 0x10, 0x39, 0xF3]), bytes([0xE0, 0x10, 0x38, 0xFD])]
    # port 1: the same committed byte libdvbv5 sends for DiSEqC 1.0
    assert diseqc.commands({"switch": "1.1"}, sat(11494000, "h"), LNB)[1][3] == 0xF2


def test_uncommitted_only():
    assert diseqc.commands({"switch": "1.1u"}, sat(11494000, "h", 9), LNB) == \
        [bytes([0xE0, 0x10, 0x39, 0xF9])]


def test_port_out_of_range():
    with pytest.raises(ValueError):
        diseqc.commands({"switch": "1.1"}, sat(11494000, "h", 16), LNB)


def test_band():
    assert diseqc.band(sat(11494000), LNB) == (1744000, False)
    assert diseqc.band(sat(12188000), LNB) == (1588000, True)
    # a single-oscillator LNB (no switch frequency) never goes high band
    assert diseqc.band(sat(11494000), (10750000, 0, 0)) == (744000, False)


def test_send_sequence():
    calls = []
    diseqc.send(9, {"switch": "1.1"}, sat(12188000, "v", 5), LNB, repeats=1,
                ioctl=lambda fd, req, arg: calls.append((req, arg)), sleep=lambda s: None)
    reqs = [r for r, _ in calls]
    # tone off, voltage for the polarisation, the pair twice, then the band's tone
    assert reqs[:2] == [diseqc.FE_SET_TONE, diseqc.FE_SET_VOLTAGE]
    assert calls[1][1] == diseqc.SEC_VOLTAGE_13
    cmds = [a for r, a in calls if r == diseqc.FE_DISEQC_SEND_MASTER_CMD]
    assert len(cmds) == 4
    assert cmds[0][:4] == bytes([0xE0, 0x10, 0x39, 0xF1]) and cmds[0][6] == 4
    assert cmds[1][2] == 0x38
    assert calls[-1] == (diseqc.FE_SET_TONE, diseqc.SEC_TONE_ON)


def test_direct_tuner_tunes_the_lnb_frequency():
    calls = []

    def ioctl(fd, req, arg):
        if req == directtune.FE_SET_PROPERTY:
            n = arg.num
            props = {arg.props[i].cmd: arg.props[i].u.data for i in range(n)}
            calls.append((req, props))
        else:
            calls.append((req, arg))

    tuner = directtune.DirectTuner(0, 0, opener=lambda p, f: 5, closer=lambda fd: None,
                                   ioctl=ioctl, sleep=lambda s: None)
    tuner.open()
    tuner.tune({"switch": "1.1"}, sat(11494000, "h", 4), LNB)
    reqs = [r for r, _ in calls]
    assert diseqc.FE_DISEQC_SEND_MASTER_CMD in reqs
    tuned = [a for r, a in calls if r == directtune.FE_SET_PROPERTY][-1]
    assert tuned[festats.DTV_FREQUENCY] == 1744000          # not a user band
    # a single-cable setup still goes the single-cable way
    calls.clear()
    tuner.tune({"standard": "en50494", "slot": 0, "frequency": 1210000}, sat(11494000), LNB)
    tuned = [a for r, a in calls if r == directtune.FE_SET_PROPERTY][-1]
    assert tuned[festats.DTV_FREQUENCY] == 1210000


def test_port_labels():
    labels = diseqc.port_labels(16)
    assert len(labels) == 17 and labels[1] == "Port 1 (A/A)" and labels[16] == "Port 16"
    assert len(diseqc.port_labels(4)) == 5


def test_describe():
    assert "16" in diseqc.describe({"diseqc": "1.1"})
    assert "4" in diseqc.describe({})
    assert "64" in diseqc.describe({"unicable": {"standard": "en50607", "frequency": 1210000}})
    assert unicable.POSITIONS["en50494"] == 2
