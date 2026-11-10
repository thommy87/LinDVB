"""The signal meter of the channel search."""

from lindvb.scan import signal_meter


def test_nothing_known():
    assert signal_meter({}) == {"lock": None, "level": None, "quality": None}
    assert signal_meter(None) == {"lock": None, "level": None, "quality": None}


def test_dvb_api_decibels():
    m = signal_meter({"signal_dbm": -55.0, "cnr_db": 12.0, "status": {"lock": True}})
    assert m["lock"] is True
    assert m["level"] == (0.5, "-55.0 dBm")
    assert m["quality"] == (0.8, "12.0 dB")


def test_dvb_api_relative_and_clamped():
    m = signal_meter({"signal_rel": 0.75, "cnr_rel": 1.2, "status": {"lock": False}})
    assert m["lock"] is False
    assert m["level"] == (0.75, "75 %")
    assert m["quality"] == (1.0, "100 %")
    assert signal_meter({"signal_dbm": -120.0})["level"] == (0.0, "-120.0 dBm")
    assert signal_meter({"cnr_db": 30.0})["quality"] == (1.0, "30.0 dB")


def test_satip_status():
    m = signal_meter({"signal": 65535, "lock": True, "snr": 0})
    assert m == {"lock": True, "level": (1.0, "100 %"), "quality": (0.0, "0 %")}
