# SPDX-License-Identifier: GPL-2.0-or-later
"""Tuner profiles and the choice of a tuner for a channel.

A profile describes one DVB frontend LinDVB may use::

    {"adapter": 0, "frontend": 0, "name": "TBS 6903", "systems": ["dvb-s", "dvb-s2"],
     "enabled": True, "priority": 0, "group": "", "lnb": [9750000, 10600000, 11700000]}

* ``systems``: the delivery systems the frontend reports (DTV_ENUM_DELSYS).
  Empty means unknown: the tuner is then offered every channel.
* ``priority``: higher numbers are used first.
* ``group``: tuners with the same non-empty group hang on one antenna cable
  (loop-through, or a single LNB output behind a splitter). They can only
  receive the same satellite band, polarisation and DiSEqC port at a time,
  unless they use Unicable / JESS: then each needs a user band of its own.
* ``unicable``: the single-cable setup (see lindvb.unicable), or missing.
* ``lnb``: the tuner's own LNB oscillators (low, high, switch frequency in kHz),
  or missing for the global LNB settings.
* ``type``: ``"satip"`` for a tuner of a SAT>IP server on the network
  (lindvb.satip), with ``host``, ``port`` and ``transport``; missing for a
  local DVB frontend.

The module has no GStreamer dependency so the tuner choice can be tested
without hardware. :class:`lindvb.tuner.TunerManager` applies it.
"""

import ctypes
import fcntl
import os

from .channels import SAT_SYSTEMS

# the cable name of tuners switched to "shared antenna cable" without a name of their own
DEFAULT_GROUP = "1"

# enum fe_delivery_system (linux/dvb/frontend.h) -> dvbsrc nick
DELSYS_NAMES = {
    1: "dvb-c-a", 2: "dvb-c-b", 3: "dvb-t", 4: "dss", 5: "dvb-s", 6: "dvb-s2", 7: "dvb-h",
    8: "isdb-t", 9: "isdb-s", 10: "isdb-c", 11: "atsc", 12: "atsc-mh", 13: "dtmb",
    14: "cmmb", 15: "dab", 16: "dvb-t2", 17: "turbo", 18: "dvb-c-c", 19: "dvb-c2",
}

# A channel of the key's system also plays on a tuner of these systems.
COMPATIBLE = {
    "dvb-s": ("dvb-s", "dvb-s2"),
    "dvb-t": ("dvb-t", "dvb-t2"),
    "dvb-c": ("dvb-c-a", "dvb-c-c"),
    "dvb-c-a": ("dvb-c-a", "dvb-c-c"),
    "dvb-c-c": ("dvb-c-c", "dvb-c-a"),
}

LABELS = {"dvb-s": "DVB-S", "dvb-s2": "DVB-S2", "dvb-c-a": "DVB-C", "dvb-c-b": "DVB-C (Annex B)",
          "dvb-c-c": "DVB-C (Annex C)", "dvb-c2": "DVB-C2", "dvb-t": "DVB-T", "dvb-t2": "DVB-T2",
          "atsc": "ATSC", "isdb-t": "ISDB-T", "isdb-s": "ISDB-S", "dtmb": "DTMB",
          "turbo": "Turbo", "dss": "DSS"}

# Two channel list entries of one transponder can differ by a few MHz
# (rounding of the satellite frequency, offsets in cable lists).
SAT_TOLERANCE_KHZ = 3000
TOLERANCE_KHZ = 500

# -- delivery systems of a frontend (DVB API v5) ---------------------------------

DTV_ENUM_DELSYS = 44


class _Buffer(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("data", ctypes.c_uint8 * 32), ("len", ctypes.c_uint32),
                ("reserved1", ctypes.c_uint32 * 3), ("reserved2", ctypes.c_void_p)]


class _Stat(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("scale", ctypes.c_uint8), ("value", ctypes.c_int64)]


class _Stats(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("len", ctypes.c_uint8), ("stat", _Stat * 4)]


class _Value(ctypes.Union):
    _fields_ = [("data", ctypes.c_uint32), ("st", _Stats), ("buffer", _Buffer)]


class _Property(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("cmd", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 3),
                ("u", _Value), ("result", ctypes.c_int)]


class _Properties(ctypes.Structure):
    _fields_ = [("num", ctypes.c_uint32), ("props", ctypes.POINTER(_Property))]


def _ior(kind, nr, size):
    return (2 << 30) | (size << 16) | (ord(kind) << 8) | nr


FE_GET_PROPERTY = _ior("o", 83, ctypes.sizeof(_Properties))


def probe_delivery_systems(adapter, frontend=0):
    """The delivery systems (dvbsrc nicks) the frontend supports, [] if unknown.

    Opens the frontend read-only, which works while another program tunes it.
    """
    prop = _Property(cmd=DTV_ENUM_DELSYS)
    props = _Properties(num=1, props=ctypes.pointer(prop))
    try:
        fd = os.open(f"/dev/dvb/adapter{adapter}/frontend{frontend}", os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        fcntl.ioctl(fd, FE_GET_PROPERTY, props)
    except OSError:
        return []
    finally:
        os.close(fd)
    n = min(int(prop.u.buffer.len), 32)
    return [DELSYS_NAMES[v] for v in prop.u.buffer.data[:n] if v in DELSYS_NAMES]


def systems_label(systems):
    """"DVB-S, DVB-S2, DVB-C" for a list of delivery systems."""
    return ", ".join(LABELS.get(s, s.upper()) for s in systems)


# -- profiles --------------------------------------------------------------------

def new_profile(adapter, frontend=0, name="", systems=(), enabled=True):
    return {"adapter": int(adapter), "frontend": int(frontend), "name": name or f"Adapter {adapter}",
            "systems": list(systems), "enabled": bool(enabled), "priority": 0, "group": ""}


def legacy_profiles(settings):
    """Profiles from the older settings: a list of adapters and one frontend."""
    fe = int(settings.get("frontend", 0) or 0)
    return [new_profile(a, fe) for a in (settings.get("adapters") or [0])]


def detect_tuners(probe=probe_delivery_systems):
    """Profiles for the frontends present now (disabled flags left to the caller)."""
    from .scan import find_adapters
    return [new_profile(a, f, name, probe(a, f)) for a, f, name in find_adapters()]


def merge_profiles(saved, detected, legacy_adapters=None):
    """The saved profiles, completed with the frontends found now.

    Saved profiles keep the user's choices and take the detected name and
    systems; a frontend seen for the first time is added, enabled when it is
    in *legacy_adapters* (or when there is no such list). Saved profiles of
    hardware that is missing now are kept, with ``present`` False.
    """
    out = []
    found = {(p["adapter"], p["frontend"]): p for p in detected}
    for p in saved:
        q = dict(p)
        if q.get("type") == "satip":         # a network tuner (lindvb.satip)
            q["present"] = True
            out.append(q)
            continue
        d = found.pop((q["adapter"], q["frontend"]), None)
        q["present"] = d is not None
        if d is not None:
            q["name"] = d["name"]
            q["systems"] = d["systems"] or q.get("systems", [])
        out.append(q)
    for key, d in found.items():
        q = dict(d)
        q["present"] = True
        if legacy_adapters is not None and not uses_default_adapters(legacy_adapters):
            q["enabled"] = d["adapter"] in legacy_adapters
        out.append(q)
    out.sort(key=lambda p: (p["adapter"], p["frontend"]))
    return out


def uses_default_adapters(adapters):
    """True for the untouched older adapter setting ([0]): then every tuner found is used."""
    return list(adapters or []) in ([], [0])


def configured_profiles(settings, detect=None):
    """The profiles in the settings, else ones made from the older adapter list.

    With the untouched older setting, *detect()* (the tuners present) is used
    when it finds any, so a second tuner works without setting anything up.
    The tuners of the SAT>IP servers (lindvb.satip) are always included.
    """
    from . import satip
    tuners = settings.get("tuners") or []
    if tuners:
        return satip.merge_profiles([dict(p) for p in tuners], settings)
    if detect is not None and uses_default_adapters(settings.get("adapters")):
        found = detect()
        if found or settings.get("satip_servers"):
            return satip.merge_profiles(found, settings)
    return satip.merge_profiles(legacy_profiles(settings), settings)


def active_profiles(settings, systems_for=None, detect=None):
    """Enabled profiles, highest priority first (then by adapter).

    *systems_for(adapter, frontend)* fills in the delivery systems of a profile
    that has none stored.
    """
    out = []
    for i, p in enumerate(configured_profiles(settings, detect)):
        if not p.get("enabled", True):
            continue
        if not p.get("systems") and systems_for is not None:
            p["systems"] = list(systems_for(p["adapter"], p.get("frontend", 0)))
        out.append((-int(p.get("priority", 0) or 0), i, p))
    return [p for _, _, p in sorted(out, key=lambda t: t[:2])]


# the reception types a combined tuner can be set to (profile["use"])
FAMILIES = {
    "sat": ("dvb-s", "dvb-s2", "turbo", "dss", "isdb-s"),
    "ter": ("dvb-t", "dvb-t2", "isdb-t", "atsc", "dtmb"),
    "cab": ("dvb-c", "dvb-c-a", "dvb-c-b", "dvb-c-c", "dvb-c2"),
}


def family_of(delsys):
    """"sat", "ter" or "cab" for a delivery system, "" if none."""
    for fam, systems in FAMILIES.items():
        if delsys in systems:
            return fam
    return ""


def families(profile):
    """The reception types the tuner's systems cover, in FAMILIES order."""
    have = {family_of(s) for s in profile.get("systems") or []}
    return [f for f in FAMILIES if f in have]


def display_name(profile):
    """The user's own name for the tuner, else the detected one."""
    return (profile.get("label") or "").strip() or profile.get("name") or \
        f"Adapter {profile.get('adapter', 0)}"


def supports(profile, delsys):
    """True if the tuner can receive channels of *delsys*.

    A combined tuner set to one reception type (``use``) only takes that one.
    """
    use = profile.get("use") or ""
    if use and len(families(profile)) > 1 and family_of(delsys) not in ("", use):
        return False
    systems = profile.get("systems") or []
    if not systems:
        return True
    return any(s in systems for s in COMPATIBLE.get(delsys, (delsys,)))


def tuner_lnb(profile, settings):
    """(LOF low, LOF high, switch frequency) in kHz for this tuner."""
    lnb = (profile or {}).get("lnb")
    if lnb and len(lnb) == 3 and all(lnb):
        return tuple(int(x) for x in lnb)
    return (int(settings["lnb_lof1"]), int(settings["lnb_lof2"]), int(settings["lnb_slof"]))


# -- transponders and shared cables ----------------------------------------------

def _family(delsys):
    if delsys in SAT_SYSTEMS:
        return "sat"
    for prefix in ("dvb-c", "dvb-t", "isdb-t", "atsc", "dtmb"):
        if delsys.startswith(prefix):
            return prefix
    return delsys


def same_transponder(a, b):
    """True if channels *a* and *b* are on one transponder (one tuner gets both)."""
    if a.is_iptv or b.is_iptv or _family(a.delsys) != _family(b.delsys):
        return False
    if a.tsid and b.tsid and a.onid and b.onid and (a.tsid, a.onid) != (b.tsid, b.onid):
        return False
    tol = SAT_TOLERANCE_KHZ if a.is_sat else TOLERANCE_KHZ
    if abs(a.frequency - b.frequency) > tol or a.stream_id != b.stream_id:
        return False
    if a.is_sat and ((a.polarity or "h").lower() != (b.polarity or "h").lower() or
                     a.diseqc != b.diseqc):
        return False
    return True


def sat_state(channel, lnb):
    """What a satellite channel asks of the cable: (DiSEqC port, band, polarisation)."""
    band = "high" if channel.frequency >= lnb[2] else "low"
    return (channel.diseqc, band, (channel.polarity or "h").lower())


def cable_conflict(profile, channel, busy, settings):
    """The busy tuner on the same cable that keeps *profile* from tuning *channel*.

    *busy* holds (profile, channel) pairs of the tuners in use. Returns the
    blocking pair, or None when the tuner is free to tune.
    """
    group = (profile.get("group") or "").strip()
    if not group or not channel.is_sat:
        return None
    from . import unicable
    uc = unicable.config(profile)
    want = sat_state(channel, tuner_lnb(profile, settings))
    for other, ch in busy:
        if other.get("adapter") == profile.get("adapter") or \
                (other.get("group") or "").strip() != group or not ch.is_sat:
            continue
        other_uc = unicable.config(other)
        if uc is not None or other_uc is not None:
            # single cable: every tuner has its own user band, only one per band
            if uc is not None and other_uc is not None and \
                    int(uc.get("slot", 0)) == int(other_uc.get("slot", 0)):
                return other, ch
            continue
        if sat_state(ch, tuner_lnb(other, settings)) != want:
            return other, ch
    return None


# -- the choice ------------------------------------------------------------------

ANALYSIS = "analysis"           # the user of a capture for a channel analysis


class NoTuner(RuntimeError):
    pass


def choose(channel, user, profiles, captures, settings, can_share=True):
    """Decide how to receive *channel* for *user* ("live" or a recording key).

    *captures* maps adapter -> capture (with ``channel``, ``channels``, ``users``
    and ``tuner``). Returns one of

    * ``("join", capture)``: the capture already carries the channel,
    * ``("share", capture)``: the capture's tuner is on the channel's transponder,
    * ``("tune", profile, preempt)``: tune *profile*; *preempt* is the capture
      of the live view or of a background channel analysis to stop first, or None.

    A channel analysis (*user* ``ANALYSIS``) only takes a free tuner.

    Raises :class:`NoTuner` with a message for the user.
    """
    for cap in captures.values():
        if channel.key in getattr(cap, "channels", {cap.channel.key}):
            return ("join", cap)
    if can_share:
        for cap in captures.values():
            if same_transponder(cap.channel, channel):
                return ("share", cap)

    suitable = [p for p in profiles if supports(p, channel.delsys)]
    if not suitable:
        raise NoTuner(f"No tuner for {systems_label([channel.delsys]) or channel.delsys} channels.")

    def busy(without=None):
        return [(c.tuner, c.channel) for a, c in captures.items() if a != without]

    for p in suitable:
        if p["adapter"] not in captures and not cable_conflict(p, channel, busy(), settings):
            return ("tune", p, None)

    if user == ANALYSIS:
        raise NoTuner("No free tuner for the analysis.")
    # Take a tuner from a channel analysis in the background, else from the
    # live view (a recording always may; the live view gives up its own tuner
    # when it switches channel). Stopping it may also free a tuner on its cable.
    for only in ({ANALYSIS}, {"live"}):
        for a, cap in captures.items():
            if cap.users != only:
                continue
            for p in suitable:
                if p["adapter"] in captures and p["adapter"] != a:
                    continue
                if not cable_conflict(p, channel, busy(without=a), settings):
                    return ("tune", p, cap)
    if user == "live":
        raise NoTuner("All tuners are busy recording.")
    raise NoTuner("No free tuner for the recording.")
