# SPDX-License-Identifier: GPL-2.0-or-later
"""Reception figures straight from the Linux DVB frontend (DVB API v5 statistics).

dvbsrc only reports the old, driver-scaled values (FE_READ_SNR & co., see
lindvb.signalq). Most current drivers also fill the v5 statistics: carrier to
noise ratio in 0.001 dB, signal level in 0.001 dBm, bit error counts before and
after the error correction and the block counters. The channel analysis reads
them with FE_GET_PROPERTY on a read-only handle of the frontend, which the
kernel allows next to the tuning dvbsrc. It also reads back what the frontend
actually tuned (modulation, code rate, roll-off, pilots, guard interval ...),
which is often more than the channel list knows.

Everything here is optional: no device, an old driver or an ioctl error give
an empty result and the analysis falls back on dvbsrc's figures.
"""

import ctypes
import fcntl
import os

FE_GET_PROPERTY = 0x80106F53
FE_READ_STATUS = 0x80046F45

DTV_FREQUENCY = 3
DTV_MODULATION = 4
DTV_BANDWIDTH_HZ = 5
DTV_SYMBOL_RATE = 8
DTV_INNER_FEC = 9
DTV_PILOT = 12
DTV_ROLLOFF = 13
DTV_DELIVERY_SYSTEM = 17
DTV_CODE_RATE_HP = 36
DTV_GUARD_INTERVAL = 38
DTV_TRANSMISSION_MODE = 39
DTV_STREAM_ID = 42
DTV_STAT_SIGNAL_STRENGTH = 62
DTV_STAT_CNR = 63
DTV_STAT_PRE_ERROR_BIT_COUNT = 64
DTV_STAT_PRE_TOTAL_BIT_COUNT = 65
DTV_STAT_POST_ERROR_BIT_COUNT = 66
DTV_STAT_POST_TOTAL_BIT_COUNT = 67
DTV_STAT_ERROR_BLOCK_COUNT = 68
DTV_STAT_TOTAL_BLOCK_COUNT = 69

FE_SCALE_NOT_AVAILABLE, FE_SCALE_DECIBEL, FE_SCALE_RELATIVE, FE_SCALE_COUNTER = 0, 1, 2, 3

STATUS_BITS = (("signal", 0x01), ("carrier", 0x02), ("viterbi", 0x04), ("sync", 0x08),
               ("lock", 0x10))

MODULATIONS = ["qpsk", "qam-16", "qam-32", "qam-64", "qam-128", "qam-256", "auto", "8vsb",
               "16vsb", "8psk", "16apsk", "32apsk", "dqpsk", "qam-4-nr", "qam-1024",
               "qam-4096", "8apsk-l", "16apsk-l", "32apsk-l", "64apsk", "64apsk-l"]
CODE_RATES = ["none", "1/2", "2/3", "3/4", "4/5", "5/6", "6/7", "7/8", "8/9", "auto", "3/5",
              "9/10", "2/5", "1/3", "1/4", "5/9", "7/9", "8/15", "11/15", "13/18", "9/20",
              "11/20", "23/36", "25/36", "13/45", "26/45", "28/45", "32/45", "77/90", "11/45",
              "4/15", "14/45", "7/15"]
ROLLOFFS = ["0.35", "0.20", "0.25", "auto", "0.15", "0.10", "0.05"]
PILOTS = ["on", "off", "auto"]
GUARDS = ["1/32", "1/16", "1/8", "1/4", "auto", "1/128", "19/128", "19/256", "PN420",
          "PN595", "PN945", "1/64"]
TRANSMISSION_MODES = ["2k", "8k", "auto", "4k", "1k", "16k", "32k", "C1", "C3780"]
DELIVERY_SYSTEMS = ["undefined", "dvb-c-a", "dvb-c-b", "dvb-t", "dss", "dvb-s", "dvb-s2",
                    "dvb-h", "isdb-t", "isdb-s", "isdb-c", "atsc", "atsc-mh", "dtmb", "cmmb",
                    "dab", "dvb-t2", "turbo", "dvb-c-c", "dvb-c2"]


class DtvStats(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("scale", ctypes.c_uint8), ("value", ctypes.c_int64)]


class DtvFeStats(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("len", ctypes.c_uint8), ("stat", DtvStats * 4)]


class _Buffer(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("data", ctypes.c_uint8 * 32), ("len", ctypes.c_uint32),
                ("reserved1", ctypes.c_uint32 * 3), ("reserved2", ctypes.c_void_p)]


class _U(ctypes.Union):
    _pack_ = 1
    _fields_ = [("data", ctypes.c_uint32), ("st", DtvFeStats), ("buffer", _Buffer)]


class DtvProperty(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("cmd", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 3), ("u", _U),
                ("result", ctypes.c_int)]


class DtvProperties(ctypes.Structure):
    _fields_ = [("num", ctypes.c_uint32), ("props", ctypes.POINTER(DtvProperty))]


TUNING = (DTV_DELIVERY_SYSTEM, DTV_FREQUENCY, DTV_MODULATION, DTV_SYMBOL_RATE, DTV_INNER_FEC,
          DTV_PILOT, DTV_ROLLOFF, DTV_BANDWIDTH_HZ, DTV_CODE_RATE_HP, DTV_GUARD_INTERVAL,
          DTV_TRANSMISSION_MODE, DTV_STREAM_ID)
STATS = (DTV_STAT_SIGNAL_STRENGTH, DTV_STAT_CNR, DTV_STAT_PRE_ERROR_BIT_COUNT,
         DTV_STAT_PRE_TOTAL_BIT_COUNT, DTV_STAT_POST_ERROR_BIT_COUNT,
         DTV_STAT_POST_TOTAL_BIT_COUNT, DTV_STAT_ERROR_BLOCK_COUNT, DTV_STAT_TOTAL_BLOCK_COUNT)


def _name(table, i):
    return table[i] if 0 <= i < len(table) else str(i)


def decode_tuning(values):
    """Readable tuning parameters from {DTV_* command: u32 value}."""
    out = {}
    if DTV_DELIVERY_SYSTEM in values:
        out["delsys"] = _name(DELIVERY_SYSTEMS, values[DTV_DELIVERY_SYSTEM])
    for cmd, key, table in ((DTV_MODULATION, "modulation", MODULATIONS),
                            (DTV_INNER_FEC, "fec", CODE_RATES),
                            (DTV_CODE_RATE_HP, "code_rate_hp", CODE_RATES),
                            (DTV_PILOT, "pilot", PILOTS), (DTV_ROLLOFF, "rolloff", ROLLOFFS),
                            (DTV_GUARD_INTERVAL, "guard", GUARDS),
                            (DTV_TRANSMISSION_MODE, "transmission_mode", TRANSMISSION_MODES)):
        if cmd in values:
            out[key] = _name(table, values[cmd])
    for cmd, key in ((DTV_FREQUENCY, "frequency"), (DTV_SYMBOL_RATE, "symbol_rate"),
                     (DTV_BANDWIDTH_HZ, "bandwidth_hz"), (DTV_STREAM_ID, "stream_id")):
        if cmd in values:
            out[key] = values[cmd]
    return out


def decode_stats(stats):
    """{cnr_db, signal_dbm, signal_rel, pre_ber, post_ber, ...} from
    {DTV_STAT_*: (scale, value)} of the first layer."""
    out = {}
    scale, v = stats.get(DTV_STAT_CNR, (0, 0))
    if scale == FE_SCALE_DECIBEL:
        out["cnr_db"] = v / 1000.0
    elif scale == FE_SCALE_RELATIVE:
        out["cnr_rel"] = (v & 0xFFFF) / 65535.0
    scale, v = stats.get(DTV_STAT_SIGNAL_STRENGTH, (0, 0))
    if scale == FE_SCALE_DECIBEL:
        out["signal_dbm"] = v / 1000.0
    elif scale == FE_SCALE_RELATIVE:
        out["signal_rel"] = (v & 0xFFFF) / 65535.0
    for err, total, key in ((DTV_STAT_PRE_ERROR_BIT_COUNT, DTV_STAT_PRE_TOTAL_BIT_COUNT, "pre"),
                            (DTV_STAT_POST_ERROR_BIT_COUNT, DTV_STAT_POST_TOTAL_BIT_COUNT, "post"),
                            (DTV_STAT_ERROR_BLOCK_COUNT, DTV_STAT_TOTAL_BLOCK_COUNT, "block")):
        se, e = stats.get(err, (0, 0))
        st, t = stats.get(total, (0, 0))
        if se == FE_SCALE_COUNTER and st == FE_SCALE_COUNTER:
            out[key + "_errors"] = e
            out[key + "_total"] = t
    return out


class Frontend:
    """A read-only handle of /dev/dvb/adapterN/frontendM."""

    def __init__(self, adapter, frontend=0, root="/dev/dvb"):
        self.path = f"{root}/adapter{adapter}/frontend{frontend}"
        self.fd = None
        self._prev = {}
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self.fd = None

    @property
    def available(self):
        return self.fd is not None

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _get(self, cmds):
        arr = (DtvProperty * len(cmds))()
        for i, c in enumerate(cmds):
            arr[i].cmd = c
        props = DtvProperties(len(cmds), ctypes.cast(arr, ctypes.POINTER(DtvProperty)))
        fcntl.ioctl(self.fd, FE_GET_PROPERTY, props)
        return arr

    def status(self):
        if self.fd is None:
            return None
        buf = ctypes.c_uint32(0)
        try:
            fcntl.ioctl(self.fd, FE_READ_STATUS, buf)
        except OSError:
            return None
        return {name: bool(buf.value & bit) for name, bit in STATUS_BITS}

    def tuning(self):
        if self.fd is None:
            return {}
        try:
            arr = self._get(TUNING)
        except OSError:
            return {}
        return decode_tuning({p.cmd: p.u.data for p in arr if p.result == 0})

    def read(self):
        """Current statistics, with the bit error *rates* over the last interval."""
        if self.fd is None:
            return {}
        try:
            arr = self._get(STATS)
        except OSError:
            return {}
        raw = {}
        for p in arr:
            if p.u.st.len:
                s = p.u.st.stat[0]
                raw[p.cmd] = (s.scale, s.value)
        out = decode_stats(raw)
        for key in ("pre", "post"):
            e, t = out.get(key + "_errors"), out.get(key + "_total")
            if e is None or t is None:
                continue
            pe, pt = self._prev.get(key, (None, None))
            if pe is not None and t > pt:
                out[key + "_ber"] = max(0, e - pe) / (t - pt)
            self._prev[key] = (e, t)
        st = self.status()
        if st is not None:
            out["status"] = st
        return out
