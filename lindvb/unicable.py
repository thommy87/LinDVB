# SPDX-License-Identifier: GPL-2.0-or-later
"""Single-cable reception: Unicable I (EN 50494) and Unicable II / JESS (EN 50607).

All tuners hang on one cable. Each tuner has its own user band (SCR slot) at a
fixed intermediate frequency. To receive a transponder, the tuner sends a
DiSEqC command that tells the LNB or multiswitch to put that transponder into
its user band, then tunes to the user band frequency.

The tuner profile (see lindvb.tunerprofiles) carries the setup::

    "unicable": {"standard": "en50494", "slot": 0, "frequency": 1210000, "pin": None}

* ``standard``: "en50494" (8 user bands, 4 MHz steps, positions A/B) or
  "en50607" (32 user bands, 1 MHz steps, 64 positions)
* ``slot``: the user band, counted from 0
* ``frequency``: the user band centre in kHz
* ``pin``: the PIN of a protected user band, or None

The satellite position is the channel's DiSEqC port (0 = A, 1 = B, ...).

dvbsrc cannot send these commands, and the kernel switches the LNB power off
when the frontend is closed, which makes the LNB forget the command. So a
single-cable tuner is driven by lindvb.directtune: it keeps the frontend open,
sends the command, tunes the user band and reads the stream from the DVR
device.
"""

import fcntl
import random
import time

STANDARDS = ("en50494", "en50607")
SLOTS = {"en50494": 8, "en50607": 32}

# User band frequencies (MHz) of common devices, in slot order. The device's
# label or manual has the real ones.
TEMPLATES = {
    "Unicable, 4 user bands (1210-2040 MHz)": ("en50494", [1210, 1420, 1680, 2040]),
    "Unicable, 8 user bands (1284-2096 MHz)": ("en50494", [1284, 1400, 1516, 1632, 1748,
                                                           1864, 1980, 2096]),
    "JESS, 16 user bands (985-2040 MHz)": ("en50607", [1210, 1420, 1680, 2040, 985, 1050,
                                                       1115, 1275, 1340, 1485, 1550, 1615,
                                                       1745, 1810, 1875, 1940]),
}

# Linux DVB frontend ioctls (linux/dvb/frontend.h)
FE_DISEQC_SEND_MASTER_CMD = 0x40076F3F      # _IOW('o', 63, struct dvb_diseqc_master_cmd)
FE_SET_TONE = 0x6F42                        # _IO('o', 66)
FE_SET_VOLTAGE = 0x6F43                     # _IO('o', 67)
SEC_VOLTAGE_13, SEC_VOLTAGE_18 = 0, 1
SEC_TONE_OFF = 1

def config(profile):
    """The tuner's single-cable setup, or None for an ordinary LNB."""
    uc = (profile or {}).get("unicable")
    if not uc or uc.get("standard") not in STANDARDS or not uc.get("frequency"):
        return None
    return uc


def _band(channel, lnb):
    """(intermediate frequency in kHz, high band) with the LNB's standard oscillators."""
    lof1, lof2, slof = lnb
    high = channel.frequency >= slof
    return abs(channel.frequency - (lof2 if high else lof1)), high


def command(uc, channel, lnb):
    """The DiSEqC message (bytes) that puts *channel*'s transponder into the user band."""
    fif, high = _band(channel, lnb)
    pol_h = (channel.polarity or "h").lower() in ("h", "l")
    pos = max(channel.diseqc, 0)
    slot = int(uc.get("slot", 0))
    ub_mhz = int(uc["frequency"]) // 1000
    pin = uc.get("pin")
    if uc["standard"] == "en50494":
        t = (fif // 1000 + ub_mhz + 2) // 4 - 350
        if not 0 <= t < 1024:
            raise ValueError(f"transponder out of range for the user band ({t})")
        d1 = (slot & 0x07) << 5 | (pos & 1) << 4 | pol_h << 3 | high << 2 | (t >> 8) & 0x03
        if pin is not None:
            return bytes([0xE0, 0x10, 0x5C, d1, t & 0xFF, int(pin) & 0xFF])
        return bytes([0xE0, 0x10, 0x5A, d1, t & 0xFF])
    t = round(fif / 1000) - 100
    if not 0 <= t < 2048:
        raise ValueError(f"transponder out of range for the user band ({t})")
    d1 = (slot & 0x1F) << 3 | (t >> 8) & 0x07
    d3 = (pos & 0x3F) << 2 | pol_h << 1 | high
    if pin is not None:
        return bytes([0x71, d1, t & 0xFF, d3, int(pin) & 0xFF])
    return bytes([0x70, d1, t & 0xFF, d3])


def _master_cmd(msg):
    buf = bytearray(7)
    buf[:len(msg)] = msg
    buf[6] = len(msg)
    return bytes(buf)


def send(fd, msg, repeats=2, ioctl=fcntl.ioctl, sleep=time.sleep):
    """Send *msg* as a single-cable command on the open frontend *fd*.

    The command goes out at 18 V and the line returns to 13 V afterwards, as
    the standards ask, without the 22 kHz tone. It is repeated after a random
    pause, since another receiver on the cable may have sent at the same time.
    Raises OSError.
    """
    ioctl(fd, FE_SET_TONE, SEC_TONE_OFF)
    ioctl(fd, FE_SET_VOLTAGE, SEC_VOLTAGE_13)
    sleep(0.05)
    cmd = _master_cmd(msg)
    for i in range(1 + max(0, repeats)):
        if i:
            sleep(random.uniform(0.068, 0.118))
        ioctl(fd, FE_SET_VOLTAGE, SEC_VOLTAGE_18)
        sleep(0.015)
        ioctl(fd, FE_DISEQC_SEND_MASTER_CMD, cmd)
        sleep(0.05)
        ioctl(fd, FE_SET_VOLTAGE, SEC_VOLTAGE_13)


def describe(uc):
    std = "Unicable II / JESS (EN 50607)" if uc["standard"] == "en50607" else "Unicable (EN 50494)"
    return f"{std}, user band {int(uc.get('slot', 0)) + 1} at {int(uc['frequency']) // 1000} MHz"
