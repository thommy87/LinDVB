# SPDX-License-Identifier: GPL-2.0-or-later
"""How much the reception has to spare, and what a transponder can carry.

``required_cn`` is the carrier to noise ratio (dB) a modulation and code rate
needs for a quasi error free picture over an ideal (Gaussian) channel, from the
ETSI standards: EN 302 307 table 13 for DVB-S2, EN 300 421 for DVB-S (Eb/N0
turned into C/N), EN 300 744 table A.1 for DVB-T, the DVB-T2 implementation
guidelines (TR 102 831) and common minimum figures for cable QAM. The margin
("Reserve") is the measured C/N minus that figure: how much rain fade, a
misaligned dish or a long cable the reception can still take.

``capacity_bps`` is the useful transport stream rate of a transponder from its
symbol rate, modulation and code rate (DVB-S/S2/C) or bandwidth, modulation,
code rate and guard interval (DVB-T).
"""

import math

DVB_S2 = {
    "qpsk": {"1/4": -2.35, "1/3": -1.24, "2/5": -0.30, "1/2": 1.00, "3/5": 2.23, "2/3": 3.10,
             "3/4": 4.03, "4/5": 4.68, "5/6": 5.18, "8/9": 6.20, "9/10": 6.42},
    "8psk": {"3/5": 5.50, "2/3": 6.62, "3/4": 7.91, "5/6": 9.35, "8/9": 10.69, "9/10": 10.98},
    "16apsk": {"2/3": 8.97, "3/4": 10.21, "4/5": 11.03, "5/6": 11.61, "8/9": 12.89,
               "9/10": 13.13},
    "32apsk": {"3/4": 12.73, "4/5": 13.64, "5/6": 14.28, "8/9": 15.69, "9/10": 16.05},
}
DVB_S = {"1/2": 4.15, "2/3": 5.9, "3/4": 6.9, "5/6": 7.9, "7/8": 8.5}
DVB_T = {
    "qpsk": {"1/2": 3.1, "2/3": 4.9, "3/4": 5.9, "5/6": 6.9, "7/8": 7.7},
    "qam-16": {"1/2": 8.8, "2/3": 11.1, "3/4": 12.5, "5/6": 13.5, "7/8": 13.9},
    "qam-64": {"1/2": 14.4, "2/3": 16.5, "3/4": 18.0, "5/6": 19.3, "7/8": 20.1},
}
DVB_T2 = {
    "qpsk": {"1/2": 1.0, "3/5": 2.2, "2/3": 3.1, "3/4": 4.1, "4/5": 4.7, "5/6": 5.2},
    "qam-16": {"1/2": 6.2, "3/5": 7.6, "2/3": 8.9, "3/4": 10.0, "4/5": 10.8, "5/6": 11.3},
    "qam-64": {"1/2": 9.9, "3/5": 11.8, "2/3": 13.0, "3/4": 14.4, "4/5": 15.3, "5/6": 15.9},
    "qam-256": {"1/2": 13.2, "3/5": 15.6, "2/3": 17.1, "3/4": 18.8, "4/5": 19.9, "5/6": 20.7},
}
CABLE = {"qam-16": 19.0, "qam-32": 22.0, "qam-64": 25.0, "qam-128": 28.0, "qam-256": 31.0}
BITS = {"qpsk": 2, "8psk": 3, "16apsk": 4, "32apsk": 5, "qam-16": 4, "qam-32": 5, "qam-64": 6,
        "qam-128": 7, "qam-256": 8, "8vsb": 3}
GUARD = {"1/4": 1 / 4, "1/8": 1 / 8, "1/16": 1 / 16, "1/32": 1 / 32}


def normalise_fec(fec):
    """'2/3' from dvbsrc nicks such as '2/3', '2_3' or 'fec-2-3'."""
    if not fec:
        return ""
    f = str(fec).lower().replace("fec", "").replace("_", "/").replace("-", "/").strip("/ ")
    return f if "/" in f else ""


def normalise_modulation(mod, delsys=""):
    m = str(mod or "").lower().replace("_", "-")
    m = {"psk-8": "8psk", "apsk-16": "16apsk", "apsk-32": "32apsk", "qam16": "qam-16",
         "qam64": "qam-64", "qam256": "qam-256", "vsb-8": "8vsb"}.get(m, m)
    if m in ("", "auto") and delsys == "dvb-s":
        return "qpsk"
    return m


def _rate(fec):
    try:
        a, b = fec.split("/")
        return int(a) / int(b)
    except (ValueError, ZeroDivisionError):
        return None


def required_cn(delsys, modulation, fec):
    """C/N (dB) for a quasi error free picture, or None when not known."""
    mod = normalise_modulation(modulation, delsys)
    fec = normalise_fec(fec)
    if delsys == "dvb-s":
        return DVB_S.get(fec)
    if delsys == "dvb-s2":
        return DVB_S2.get(mod or "qpsk", {}).get(fec)
    if delsys == "dvb-t":
        return DVB_T.get(mod, {}).get(fec)
    if delsys == "dvb-t2":
        return DVB_T2.get(mod, {}).get(fec)
    if delsys.startswith("dvb-c"):
        return CABLE.get(mod)
    return None


def margin(cn_db, delsys, modulation, fec):
    need = required_cn(delsys, modulation, fec)
    if cn_db is None or need is None:
        return None
    return cn_db - need


def capacity_bps(delsys, modulation, fec, symbol_rate=0, bandwidth_hz=0, guard="", pilot=""):
    """Useful TS bit rate of the transponder, or None when it cannot be worked out.
    *symbol_rate* in symbols per second."""
    mod = normalise_modulation(modulation, delsys)
    r = _rate(normalise_fec(fec))
    if delsys == "dvb-s" and symbol_rate and r:
        return symbol_rate * 2 * r * 188 / 204
    if delsys == "dvb-s2" and symbol_rate and r:
        bits = BITS.get(mod or "qpsk", 2)
        kbch = r * 64800 - 192            # BCH parity of a normal frame
        payload = kbch - 80               # minus the BB header
        slots = 64800 / bits / 90
        pilots = (math.ceil(slots / 16) - 1) * 36 if pilot == "on" else 0
        symbols = slots * 90 + 90 + pilots
        return symbol_rate * payload / symbols
    if delsys.startswith("dvb-c") and symbol_rate and mod in BITS:
        return symbol_rate * BITS[mod] * 188 / 204
    if delsys == "dvb-t" and r and mod in BITS and guard in GUARD:
        bw = (bandwidth_hz or 8_000_000) / 1e6
        tu = 224e-6 * 8 / bw
        return 1512 * BITS[mod] * r * 188 / 204 / (tu * (1 + GUARD[guard]))
    return None


def snr_db_from_dvbsrc(snr):
    """dvbsrc's SNR value in dB, when the driver reports 0.1 dB steps (see signalq)."""
    if snr is None or snr <= 0 or snr > 1000:
        return None
    return snr / 10.0
