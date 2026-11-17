# SPDX-License-Identifier: GPL-2.0-or-later
"""DiSEqC switches: 1.0 (up to 4 satellites) and 1.1 (up to 16 satellites).

A channel's ``diseqc`` field is the switch port counted from 0 (-1: no switch).

* DiSEqC 1.0 ("committed" command, E0 10 38): ports 1 to 4. dvbsrc and
  dvbv5-scan send it themselves, so LinDVB only passes the port on.
* DiSEqC 1.1 ("uncommitted" command, E0 10 39): up to 16 inputs. dvbsrc and
  dvbv5-scan cannot send it, so a tuner with a 1.1 switch is driven by
  lindvb.directtune, as a single-cable tuner is: LinDVB sends the commands,
  sets voltage and tone and tunes the frontend itself.

The tuner profile (see lindvb.tunerprofiles) names the switch::

    "diseqc": "1.1"

* ``"1.0"`` or missing: a committed switch, ports 1 to 4. A channel on port 5
  or higher is still tuned as on a cascade (below), so nothing is lost.
* ``"1.1"``: a 1.1 switch or a cascade of an uncommitted switch in front of
  committed ones. Port p (0..15) sends uncommitted input p // 4, then
  committed input p % 4 with polarisation and band. This is the usual layout
  of 8-, 10- and 16-input switches.
* ``"1.1u"``: an uncommitted switch alone. Port p sends uncommitted input p
  only; polarisation and band come from voltage and tone.

The commands are sent uncommitted first (the switch nearest the receiver
chooses the branch), then committed, and the pair once more, as switches
that missed the first command after power-up need it.
"""

import fcntl
import time

from . import unicable

MODES = ("1.0", "1.1", "1.1u")
COMMITTED_PORTS = 4
MAX_PORTS = 16

FE_DISEQC_SEND_MASTER_CMD = unicable.FE_DISEQC_SEND_MASTER_CMD
FE_SET_TONE = unicable.FE_SET_TONE
FE_SET_VOLTAGE = unicable.FE_SET_VOLTAGE
SEC_VOLTAGE_13, SEC_VOLTAGE_18 = unicable.SEC_VOLTAGE_13, unicable.SEC_VOLTAGE_18
SEC_TONE_ON, SEC_TONE_OFF = 0, 1


def mode(profile):
    """The switch the tuner profile names: "1.0", "1.1" or "1.1u"."""
    m = (profile or {}).get("diseqc")
    return m if m in MODES else "1.0"


def config(profile, port=-1):
    """The 1.1 switch setup for lindvb.directtune, or None when dvbsrc can tune.

    A 1.1 switch always needs it; a 1.0 tuner only for a channel on port 5 or
    higher, which no committed switch has.
    """
    m = mode(profile)
    if m == "1.0":
        if int(port) < COMMITTED_PORTS:
            return None
        m = "1.1"
    return {"switch": m}


def is_switch(setup):
    """True for a setup from :func:`config` (not a single-cable one)."""
    return bool(setup) and setup.get("switch") in MODES


def max_ports(profile):
    """How many satellites the tuner's reception path tells apart."""
    prof = profile or {}
    if prof.get("type") == "satip":
        return COMMITTED_PORTS
    uc = unicable.config(prof)
    if uc is not None:
        return unicable.POSITIONS[uc["standard"]]
    return MAX_PORTS if mode(prof) != "1.0" else COMMITTED_PORTS


def band(channel, lnb):
    """(intermediate frequency in kHz, high band) with the LNB's oscillators."""
    lof1, lof2, slof = lnb
    high = bool(lof2) and bool(slof) and channel.frequency >= slof
    return abs(channel.frequency - (lof2 if high else lof1)), high


def _horizontal(channel):
    return (channel.polarity or "h").lower() in ("h", "l")


def commands(setup, channel, lnb):
    """The DiSEqC messages (bytes) that switch to *channel*'s port, in order."""
    port = max(int(channel.diseqc), 0)
    if port >= MAX_PORTS:
        raise ValueError(f"DiSEqC port {port + 1} is out of range (1 to {MAX_PORTS})")
    _if, high = band(channel, lnb)
    out = []
    if setup["switch"] == "1.1u":
        out.append(bytes([0xE0, 0x10, 0x39, 0xF0 | port]))
        return out
    out.append(bytes([0xE0, 0x10, 0x39, 0xF0 | port // 4]))
    committed = 0xF0 | (port % 4) << 2 | _horizontal(channel) << 1 | high
    out.append(bytes([0xE0, 0x10, 0x38, committed]))
    return out


def send(fd, setup, channel, lnb, repeats=1, ioctl=fcntl.ioctl, sleep=time.sleep):
    """Switch to *channel*'s port on the open frontend *fd* and leave voltage
    and tone set for its polarisation and band. Raises OSError/ValueError."""
    msgs = commands(setup, channel, lnb)
    _if, high = band(channel, lnb)
    ioctl(fd, FE_SET_TONE, SEC_TONE_OFF)
    ioctl(fd, FE_SET_VOLTAGE, SEC_VOLTAGE_18 if _horizontal(channel) else SEC_VOLTAGE_13)
    sleep(0.02)
    for i in range(1 + max(0, repeats)):
        for msg in msgs:
            ioctl(fd, FE_DISEQC_SEND_MASTER_CMD, unicable.master_cmd(msg))
            sleep(0.1 if i == 0 else 0.03)
    sleep(0.02)
    ioctl(fd, FE_SET_TONE, SEC_TONE_ON if high else SEC_TONE_OFF)


def port_label(port):
    """The name of port *port* (0-based) in the dropdowns."""
    from .i18n import _
    committed = [_("Port 1 (A/A)"), _("Port 2 (B/A)"), _("Port 3 (A/B)"), _("Port 4 (B/B)")]
    if 0 <= port < len(committed):
        return committed[port]
    return _("Port {n}").format(n=port + 1)


def port_labels(n):
    """The port dropdown: "None", then *n* ports."""
    from .i18n import _
    return [_("None")] + [port_label(p) for p in range(n)]


def describe(profile):
    """One line on what the tuner's switch tells apart, for the channel search."""
    from .i18n import _
    prof = profile or {}
    if prof.get("type") == "satip":
        return _("SAT>IP: up to 4 satellites (source 1 to 4).")
    uc = unicable.config(prof)
    if uc is not None:
        return _("Single cable: up to {n} satellite positions.").format(
            n=unicable.POSITIONS[uc["standard"]])
    m = mode(prof)
    if m == "1.1":
        return _("DiSEqC 1.1 switch: up to 16 satellites (uncommitted and committed).")
    if m == "1.1u":
        return _("DiSEqC 1.1 switch: up to 16 satellites (uncommitted only).")
    return _("DiSEqC 1.0 switch: up to 4 satellites. DiSEqC 1.1 (up to 16 satellites) "
             "and Unicable (up to 64 positions) are chosen in the tuner settings.")
