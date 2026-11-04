"""The OSD's quality of service bar: DVB frontend statistics and internet streams."""

from lindvb import signalq
from lindvb.signalq import BAD, FAIR, GOOD, NONE, POOR, Meter, dvb_quality, level_for


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_levels():
    assert level_for(None) == NONE
    assert level_for(0) == NONE
    assert level_for(0.1) == BAD
    assert level_for(0.4) == POOR
    assert level_for(0.6) == FAIR
    assert level_for(0.7) == GOOD
    assert level_for(1.0) == GOOD


def test_no_lock_is_no_signal():
    assert dvb_quality({}) == 0
    assert dvb_quality({"lock": False, "signal": 60000, "snr": 60000}) == 0


def test_dvb_scales():
    full = dvb_quality({"lock": True, "signal": 65535, "snr": 65535})
    assert full == 1.0
    weak = dvb_quality({"lock": True, "signal": 20000, "snr": 10000})
    assert level_for(weak) == BAD
    # SNR in 0.1 dB (driver reports dB): 15 dB of 20 dB
    assert abs(dvb_quality({"lock": True, "snr": 150}) - 0.75) < 1e-6
    # locked but the driver reports no values: the picture is there
    assert dvb_quality({"lock": True}) == 1.0


def test_uncorrected_blocks_pull_down():
    clk = Clock()
    m = Meter(clk)
    m.reset("dvb")
    good = {"lock": True, "signal": 60000, "snr": 60000, "unc": 5}
    m.feed_stats(good)
    assert m.level == GOOD                     # a running counter: the first value is history
    m.feed_stats(dict(good, unc=9))
    assert m.level == BAD
    clk.t += signalq.UNC_PENALTY_SECONDS + 1
    m.feed_stats(dict(good, unc=9))
    assert m.level == GOOD


def test_stream_states():
    clk = Clock()
    m = Meter(clk)
    m.reset("iptv")
    assert m.level == NONE
    m.feed_buffering(20)
    assert m.level == BAD
    m.feed_buffering(60)
    assert m.level == FAIR
    m.feed_buffering(100)
    m.stream_playing()
    assert m.level == GOOD
    # had to stop and fill the buffer again
    m.feed_buffering(40)
    assert m.level == POOR
    m.feed_buffering(100)
    m.stream_playing()
    assert m.level == FAIR
    clk.t += signalq.REBUFFER_PENALTY_SECONDS + 1
    m.tick()
    assert m.level == GOOD
    m.stream_failed()
    assert m.level == NONE


def test_buffering_never_green():
    m = Meter(Clock())
    m.reset("iptv")
    m.feed_buffering(99)
    assert m.level != GOOD
