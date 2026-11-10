# SPDX-License-Identifier: GPL-2.0-or-later
"""Transponder lists of satellite positions as the user edited them.

The channel search merges the lists of one orbital position (lindvb.scanlists).
When the user changes that list in the transponder editor, the result is kept
in ``$XDG_DATA_HOME/lindvb/transponders/sat-<position>.json`` and used instead
of the lists; removing the file goes back to them. The module has no GTK; the
editor is lindvb.ui.scan.
"""

import json
from pathlib import Path

from . import config
from . import scanlists as sl
from .scan import Transponder, TransponderList

EDIT_DIR = config.DATA_DIR / "transponders"

# what the editor offers (dvbsrc nicks, as lindvb.scan.Transponder has them)
SAT_SYSTEMS = ("dvb-s", "dvb-s2")
POLARITIES = ("h", "v", "l", "r")     # linear, and circular left / right
MODULATIONS = ("auto", "qpsk", "8psk", "16apsk", "32apsk")
# every code rate of the DVB API (lindvb.festats.CODE_RATES), smallest first
FECS = ("auto", "1/4", "1/3", "2/5", "1/2", "3/5", "2/3", "3/4", "4/5", "5/6", "6/7", "7/8",
        "8/9", "9/10", "13/45", "9/20", "11/20", "5/9", "26/45", "23/36", "25/36", "13/18",
        "7/9", "77/90", "8/15", "11/15", "32/45", "28/45", "11/45", "4/15", "14/45", "7/15")
FREQ_RANGE = (2000000, 40000000)      # kHz: S band to Ka band
SR_RANGE = (50, 120000)               # kBd


def _path(position, root=None):
    return Path(root or EDIT_DIR) / f"sat-{int(position)}.json"


def key(t):
    """What tells two transponders apart: frequency (MHz), polarisation."""
    return (t.frequency // 1000, t.polarity)


def check(t):
    """The field ("frequency", "polarity", "symbol_rate", "system") that makes *t*
    unusable, or "" when it is fine."""
    if not FREQ_RANGE[0] <= t.frequency <= FREQ_RANGE[1]:
        return "frequency"
    if t.polarity not in POLARITIES:
        return "polarity"
    if not SR_RANGE[0] <= t.symbol_rate <= SR_RANGE[1]:
        return "symbol_rate"
    if t.delsys not in SAT_SYSTEMS or t.modulation not in MODULATIONS or t.fec not in FECS:
        return "system"
    if t.delsys == "dvb-s" and t.modulation not in ("auto", "qpsk"):
        return "system"
    return ""


def load(position, root=None):
    """The edited transponders of *position* (tenths of a degree), None if not edited."""
    try:
        data = json.loads(_path(position, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    out = []
    for d in data.get("transponders", []) if isinstance(data, dict) else []:
        try:
            t = Transponder(str(d.get("delsys", "dvb-s")), int(d["frequency"]),
                            str(d.get("polarity", "h")), int(d.get("symbol_rate", 0)),
                            str(d.get("modulation", "auto")), str(d.get("fec", "auto")))
        except (KeyError, TypeError, ValueError):
            continue
        if not check(t):
            out.append(t)
    return out


def save(position, transponders, root=None):
    """Keep *transponders* as the list of *position*. Raises OSError."""
    p = _path(position, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"position": int(position),
            "transponders": [{"delsys": t.delsys, "frequency": t.frequency,
                              "polarity": t.polarity, "symbol_rate": t.symbol_rate,
                              "modulation": t.modulation, "fec": t.fec}
                             for t in sorted(transponders, key=lambda t: (t.frequency, t.polarity))]}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def reset(position, root=None):
    """Go back to the lists of *position*."""
    try:
        _path(position, root).unlink()
    except FileNotFoundError:
        pass


def is_edited(position, root=None):
    return position is not None and _path(position, root).is_file()


def satellite_list(choice, systems=(), root=None):
    """lindvb.scanlists.satellite_list, or the user's own version of the position.

    DVB-S2 transponders are left out for a tuner that only has DVB-S, as there.
    """
    edited = load(choice.position, root) if choice.position is not None else None
    if edited is None:
        return sl.satellite_list(choice, systems)
    no_s2 = bool(systems) and "dvb-s2" not in systems
    tps = [t for t in edited if not (no_s2 and t.delsys == "dvb-s2")]
    tps.sort(key=lambda t: (t.frequency, t.polarity))
    return TransponderList(sl.format_position(choice.position), "sat", tps)


def only(tplist, keys):
    """*tplist* with only the transponders whose :func:`key` is in *keys*."""
    return TransponderList(tplist.name, tplist.kind,
                           [t for t in tplist.transponders if key(t) in keys], tplist.path)
