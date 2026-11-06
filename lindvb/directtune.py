# SPDX-License-Identifier: GPL-2.0-or-later
"""Tuning a frontend without dvbsrc, for single-cable (Unicable / JESS) tuners.

The tuner keeps ``/dev/dvb/adapterN/frontendM`` open for as long as it
receives (closing it switches the LNB power off), sends the single-cable
command, tunes the user band with FE_SET_PROPERTY, sets the PID filters on the
demux device and reads the transport stream from the DVR device in a thread
(:meth:`DirectTuner.read_loop`), which hands it to a GStreamer appsrc.

Everything here is plain Linux DVB API v5 through ioctls, so the module can
be tested with fake ioctls and no hardware.
"""

import ctypes
import errno
import fcntl
import os
import select
import time

from . import festats, unicable

FE_SET_PROPERTY = (1 << 30) | (ctypes.sizeof(festats.DtvProperties) << 16) | (ord("o") << 8) | 82

DTV_TUNE = 1
DTV_CLEAR = 2
NO_STREAM_ID_FILTER = 0xFFFFFFFF
MODULATION_QPSK = 0
FEC_AUTO = 9
PILOT_AUTO = 2
ROLLOFF_AUTO = 3


class DmxPesFilterParams(ctypes.Structure):
    _fields_ = [("pid", ctypes.c_uint16), ("input", ctypes.c_int), ("output", ctypes.c_int),
                ("pes_type", ctypes.c_int), ("flags", ctypes.c_uint32)]


DMX_SET_PES_FILTER = (1 << 30) | (ctypes.sizeof(DmxPesFilterParams) << 16) | (ord("o") << 8) | 44
DMX_SET_BUFFER_SIZE = (ord("o") << 8) | 45
DMX_IN_FRONTEND = 0
DMX_OUT_TS_TAP = 2
DMX_PES_OTHER = 20
DMX_IMMEDIATE_START = 4
ALL_PIDS = 0x2000
DVR_BUFFER = 4 * 1024 * 1024


def tuning_properties(channel, if_khz):
    """[(DTV command, value)] that tune *channel* at the intermediate frequency *if_khz*."""
    props = [(festats.DTV_DELIVERY_SYSTEM, festats.DELIVERY_SYSTEMS.index(channel.delsys)),
             (festats.DTV_FREQUENCY, int(if_khz)),
             (festats.DTV_SYMBOL_RATE, int(channel.symbol_rate) * 1000)]
    mod = channel.modulation
    if mod and mod != "auto" and mod in festats.MODULATIONS:
        props.append((festats.DTV_MODULATION, festats.MODULATIONS.index(mod)))
    elif channel.delsys == "dvb-s":
        props.append((festats.DTV_MODULATION, MODULATION_QPSK))
    fec = channel.fec if channel.fec in festats.CODE_RATES else "auto"
    props.append((festats.DTV_INNER_FEC, festats.CODE_RATES.index(fec)))
    if channel.delsys != "dvb-s":
        props += [(festats.DTV_PILOT, PILOT_AUTO), (festats.DTV_ROLLOFF, ROLLOFF_AUTO)]
    props.append((festats.DTV_STREAM_ID,
                  channel.stream_id if channel.stream_id >= 0 else NO_STREAM_ID_FILTER))
    props.append((DTV_TUNE, 0))
    return props


def dvb_stats(fe):
    """dvbsrc-like figures (signal, snr 0..65535, ber, unc, lock) from lindvb.festats.Frontend.read()."""
    st = {}
    status = fe.get("status") or {}
    st["lock"] = bool(status.get("lock"))
    if "signal_rel" in fe:
        st["signal"] = int(fe["signal_rel"] * 65535)
    elif "signal_dbm" in fe:            # -75 dBm (weak) .. -25 dBm (strong)
        st["signal"] = int(min(1.0, max(0.0, (fe["signal_dbm"] + 75) / 50)) * 65535)
    if "cnr_rel" in fe:
        st["snr"] = int(fe["cnr_rel"] * 65535)
    elif "cnr_db" in fe:                # 0 .. 15 dB
        st["snr"] = int(min(1.0, max(0.0, fe["cnr_db"] / 15)) * 65535)
    if "post_ber" in fe:
        st["ber"] = int(fe["post_ber"] * 1e9)
    if "block_errors" in fe:
        st["unc"] = fe["block_errors"]
    return st


class DirectTuner:
    """One single-cable tuner, open from :meth:`open` to :meth:`close`."""

    def __init__(self, adapter, frontend=0, root="/dev/dvb", opener=os.open, closer=os.close,
                 ioctl=fcntl.ioctl, sleep=time.sleep):
        self.dir = f"{root}/adapter{adapter}"
        self.frontend = frontend
        self._open, self._close, self._ioctl, self._sleep = opener, closer, ioctl, sleep
        self.fe = None
        self.dvr = None
        self.filters = {}            # pid -> demux fd

    def open(self):
        """Open the frontend and the DVR device. Raises OSError."""
        self.fe = self._open(f"{self.dir}/frontend{self.frontend}", os.O_RDWR | os.O_NONBLOCK)
        try:
            self.dvr = self._open(f"{self.dir}/dvr0", os.O_RDONLY)
        except OSError:
            self._close(self.fe)
            self.fe = None
            raise
        try:
            self._ioctl(self.dvr, DMX_SET_BUFFER_SIZE, DVR_BUFFER)
        except OSError:
            pass                      # the driver's default buffer then

    def tune(self, uc, channel, lnb):
        """Send the single-cable command and tune the user band. Raises OSError/ValueError."""
        msg = unicable.command(uc, channel, lnb)
        clear = festats.DtvProperty(cmd=DTV_CLEAR)
        self._set([clear])
        unicable.send(self.fe, msg, ioctl=self._ioctl, sleep=self._sleep)
        arr = []
        for cmd, value in tuning_properties(channel, int(uc["frequency"])):
            p = festats.DtvProperty(cmd=cmd)
            p.u.data = value & 0xFFFFFFFF
            arr.append(p)
        self._set(arr)

    def _set(self, props):
        arr = (festats.DtvProperty * len(props))(*props)
        req = festats.DtvProperties(len(props), ctypes.cast(arr, ctypes.POINTER(festats.DtvProperty)))
        self._ioctl(self.fe, FE_SET_PROPERTY, req)

    def set_pids(self, pids):
        """Pass these PIDs (8192 or 0x2000: the whole transponder) to the DVR device."""
        want = {ALL_PIDS} if any(p >= ALL_PIDS for p in pids) else set(pids)
        for pid in [p for p in self.filters if p not in want]:
            self._close(self.filters.pop(pid))
        for pid in sorted(want - set(self.filters)):
            fd = self._open(f"{self.dir}/demux0", os.O_RDWR | os.O_NONBLOCK)
            params = DmxPesFilterParams(pid=pid, input=DMX_IN_FRONTEND, output=DMX_OUT_TS_TAP,
                                        pes_type=DMX_PES_OTHER, flags=DMX_IMMEDIATE_START)
            try:
                self._ioctl(fd, DMX_SET_PES_FILTER, params)
            except OSError:
                self._close(fd)
                raise
            self.filters[pid] = fd

    def read_loop(self, deliver, chunk=188 * 348):
        """Read the DVR device until :meth:`close`, calling *deliver(bytes)*.

        A full kernel buffer (EOVERFLOW) loses some packets, and reading goes on.
        """
        while True:
            fd = self.dvr
            if fd is None:
                return
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    continue
                data = os.read(fd, chunk)
            except OSError as e:
                if e.errno in (errno.EOVERFLOW, errno.EAGAIN, errno.EINTR):
                    continue
                return                # closed under us, or the device is gone
            except ValueError:
                return
            if data:
                deliver(data)

    def close(self):
        for fd in self.filters.values():
            self._close(fd)
        self.filters.clear()
        for name in ("dvr", "fe"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    self._close(fd)
                except OSError:
                    pass
                setattr(self, name, None)
