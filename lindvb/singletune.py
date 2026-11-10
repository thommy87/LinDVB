# SPDX-License-Identifier: GPL-2.0-or-later
"""One frequency tuned by hand: what it takes to tune it, and how the channels
it carries compare with the ones the channel list already has for it.

The comparison only looks at the channels of the same transponder: the same
reception type, frequency, polarisation and DiSEqC port. A channel of the same
name on another transponder, another reception type or the internet is left
alone. The module has no GTK; the dialog is lindvb.ui.tune.
"""

from collections import Counter
from dataclasses import dataclass, field

from . import satedit
from . import scanlists as sl
from .scan import Transponder, TransponderList, write_scan_file

# what the dialog offers per reception type (dvbsrc nicks)
SYSTEMS = {sl.SAT: ("dvb-s", "dvb-s2"), sl.CABLE: ("dvb-c-a", "dvb-c-b"),
           sl.TERR: ("dvb-t", "dvb-t2", "atsc", "isdb-t", "dtmb")}
SYSTEM_LABELS = {"dvb-s": "DVB-S", "dvb-s2": "DVB-S2", "dvb-c-a": "DVB-C",
                 "dvb-c-b": "DVB-C (Annex B, QAM)", "dvb-t": "DVB-T", "dvb-t2": "DVB-T2",
                 "atsc": "ATSC", "isdb-t": "ISDB-T", "dtmb": "DTMB"}
MODULATIONS = {
    "dvb-s": ("auto", "qpsk"), "dvb-s2": satedit.MODULATIONS,
    "dvb-c-a": ("auto", "qam-16", "qam-32", "qam-64", "qam-128", "qam-256"),
    "dvb-c-b": ("qam-256", "qam-64"), "atsc": ("8vsb", "16vsb"),
}
MOD_LABELS = {"qpsk": "QPSK", "8psk": "8PSK", "16apsk": "16APSK", "32apsk": "32APSK",
              "qam-16": "QAM 16", "qam-32": "QAM 32", "qam-64": "QAM 64", "qam-128": "QAM 128",
              "qam-256": "QAM 256", "8vsb": "8VSB", "16vsb": "16VSB"}
BANDWIDTHS = (8000000, 7000000, 6000000, 5000000, 10000000, 1712000)     # Hz
BANDWIDTH_SYSTEMS = ("dvb-t", "dvb-t2", "isdb-t", "dtmb")
DEFAULT_BANDWIDTH = {"isdb-t": 6000000}
SYMBOL_RATE_SYSTEMS = ("dvb-s", "dvb-s2", "dvb-c-a", "dvb-c-b")
FREQ_RANGE = {sl.SAT: satedit.FREQ_RANGE, sl.CABLE: (42000, 1002000),
              sl.TERR: (42000, 1002000)}                                   # kHz
SR_RANGE = {sl.SAT: satedit.SR_RANGE, sl.CABLE: (1000, 7200)}             # kBd

# how far apart (kHz) two frequencies may be and still mean the same transponder:
# lists round differently, tuners report what they locked on
TOLERANCE = {sl.SAT: 2000, sl.CABLE: 1500, sl.TERR: 1500}

SAME, CHANGED, NEW, GONE = "same", "changed", "new", "gone"
ORDER = (SAME, CHANGED, NEW, GONE)


@dataclass
class Tuning:
    """What the user entered. *frequency* in kHz, *symbol_rate* in kBd."""
    delsys: str
    frequency: int
    polarity: str = ""
    symbol_rate: int = 0
    modulation: str = "auto"
    fec: str = "auto"
    bandwidth_hz: int = 0
    stream_id: int = -1          # DVB-T2: the PLP, -1: the tuner's choice
    diseqc: int = -1             # -1 none, 0..3 = port 1..4

    @property
    def family(self):
        return sl.FAMILY_OF.get(self.delsys, sl.TERR)


def systems_for(family, tuner_systems=()):
    """The systems of *family* to offer for a tuner that receives *tuner_systems*
    (empty: unknown, so all of them)."""
    offered = SYSTEMS.get(family, ())
    if not tuner_systems:
        return list(offered)
    return [s for s in offered if s in tuner_systems] or list(offered)


def check(t):
    """The field ("frequency", "symbol_rate", "system", "polarity") that makes the
    tuning unusable, or "" when it is fine."""
    fam = t.family
    if t.delsys not in SYSTEMS.get(fam, ()):
        return "system"
    lo, hi = FREQ_RANGE[fam]
    if not lo <= t.frequency <= hi:
        return "frequency"
    if fam == sl.SAT:
        return satedit.check(transponder(t))
    if t.delsys in SYMBOL_RATE_SYSTEMS:
        lo, hi = SR_RANGE[sl.CABLE]
        if not lo <= t.symbol_rate <= hi:
            return "symbol_rate"
    mods = MODULATIONS.get(t.delsys)
    if mods and t.modulation not in mods:
        return "system"
    return ""


def transponder(t):
    return Transponder(t.delsys, t.frequency, t.polarity, t.symbol_rate, t.modulation, t.fec,
                       t.bandwidth_hz)


def transponder_list(t, name=""):
    """The one transponder as a list, as the search engines take it (satellite)."""
    return TransponderList(name, "sat", [transponder(t)])


def scan_file(t):
    """The initial tuning file for dvbv5-scan with just this frequency."""
    if t.family == sl.SAT:
        return write_scan_file(transponder_list(t))
    e = sl._transponder_entry(transponder(t))
    if t.delsys not in SYMBOL_RATE_SYSTEMS:
        e.pop("INNER_FEC", None)
    if t.delsys == "dvb-t2" and t.stream_id >= 0:
        e["STREAM_ID"] = str(t.stream_id)
    return sl.write_entries([e])


# -- which channels belong to the transponder ------------------------------------------

def _port(diseqc):
    """Without a switch the dish is what a switch has on its first port."""
    return max(int(diseqc), 0)


def same_mux(c, t):
    """True if channel *c* is on the transponder tuned with *t* (a Tuning, or
    another channel)."""
    if getattr(c, "is_iptv", False) or getattr(c, "dvbi_id", ""):
        return False
    fam = sl.FAMILY_OF.get(c.delsys)
    if fam is None or fam != sl.FAMILY_OF.get(t.delsys):
        return False
    if abs(c.frequency - t.frequency) > TOLERANCE[fam]:
        return False
    if fam == sl.SAT:
        if (c.polarity or "h") != (t.polarity or "h"):
            return False
        if _port(c.diseqc) != _port(t.diseqc):
            return False
    if c.stream_id >= 0 and t.stream_id >= 0 and c.stream_id != t.stream_id:
        return False
    return True


def on_mux(channels, t):
    return [c for c in channels if same_mux(c, t)]


def target_group(channels, t, fallback=""):
    """The group new channels of the transponder go to: the one its channels are
    in, else the one of the other channels received the same way, else *fallback*."""
    roots = Counter(c.root for c in on_mux(channels, t) if c.root)
    if not roots:
        fam = t.family
        roots = Counter(
            c.root for c in channels
            if c.root and not c.is_iptv and not c.dvbi_id and sl.FAMILY_OF.get(c.delsys) == fam
            and (fam != sl.SAT or _port(c.diseqc) == _port(t.diseqc)))
    return roots.most_common(1)[0][0] if roots else fallback


# -- comparing -----------------------------------------------------------------------

@dataclass
class Row:
    """One line of the result. *found*: the channel as tuned now (None: gone),
    *old*: the channel in the list (None: new), *changes*: [(field, old, new)]."""
    status: str
    found: object = None
    old: object = None
    changes: list = field(default_factory=list)

    @property
    def channel(self):
        return self.found if self.found is not None else self.old


def _pids(v):
    return sorted(set(v or ()))


def differences(old, new, detailed=False):
    """[(field, old value, new value)] where the tuned channel *new* differs from
    the listed one *old*. *detailed*: *new* was read from the stream itself (PAT,
    PMT, SDT), so the PMT, teletext and subtitle PIDs and the encryption count too;
    dvbv5-scan does not report those."""
    out = []

    def diff(name, a, b):
        if a != b:
            out.append((name, a, b))

    if new.name and not (new.name.startswith("Service ") and old.name):
        diff("name", old.name, new.name)
    diff("video_pid", old.video_pid, new.video_pid)
    diff("audio_pids", _pids(old.audio_pids), _pids(new.audio_pids))
    for name in ("tsid", "onid"):
        if getattr(new, name):
            diff(name, getattr(old, name), getattr(new, name))
    diff("delsys", old.delsys, new.delsys)
    if new.symbol_rate and abs(new.symbol_rate - old.symbol_rate) > 2:
        diff("symbol_rate", old.symbol_rate, new.symbol_rate)
    if new.bandwidth_hz and old.bandwidth_hz:
        diff("bandwidth_hz", old.bandwidth_hz, new.bandwidth_hz)
    if detailed:
        if new.pmt_pid:
            diff("pmt_pid", old.pmt_pid, new.pmt_pid)
        diff("teletext_pid", old.teletext_pid, new.teletext_pid)
        diff("subtitle_pids", _pids(old.subtitle_pids), _pids(new.subtitle_pids))
        diff("encrypted", bool(old.encrypted), bool(new.encrypted))
    return out


def compare(found, existing, t, detailed=False):
    """Compare the channels *found* on the transponder tuned with *t* with the
    channel list *existing*. Returns the rows in the order unchanged, changed,
    new, gone; inside each by name."""
    found = on_mux(found, t)
    listed = on_mux(existing, t)
    by_sid = {}
    for c in listed:
        by_sid.setdefault(c.sid, []).append(c)
    rows, seen = [], set()
    for c in found:
        if c.sid in seen:            # a service reported twice
            continue
        seen.add(c.sid)
        olds = by_sid.pop(c.sid, None)
        if not olds:
            rows.append(Row(NEW, found=c))
            continue
        for old in olds:             # the list may hold a service twice
            changes = differences(old, c, detailed)
            rows.append(Row(CHANGED if changes else SAME, c, old, changes))
    for olds in by_sid.values():
        rows += [Row(GONE, old=old) for old in olds]
    rows.sort(key=lambda r: (ORDER.index(r.status), r.channel.name.lower(), r.channel.sid))
    return rows


def counts(rows):
    c = Counter(r.status for r in rows)
    return {s: c.get(s, 0) for s in ORDER}


# -- saving --------------------------------------------------------------------------

def _update(old, new, changes):
    for name, _a, _b in changes:
        value = getattr(new, name)
        setattr(old, name, list(value) if isinstance(value, list) else value)
    names = {name for name, _a, _b in changes}
    if "video_pid" in names:
        old.radio = new.radio
        old.pcr_pid = new.pcr_pid or new.video_pid
    if names & {"delsys", "symbol_rate"}:
        # the tuning data go together
        old.modulation, old.fec = new.modulation, new.fec


def _listed(db, channel):
    return any(c is channel for c in db.channels)


def apply(db, rows, favs=None, group=""):
    """Carry out *rows* (the ticked ones) on the channel database *db*: add the
    new channels (to *group*), update the changed ones and remove the gone ones.
    Unchanged channels stay as they are. *favs* (lindvb.favourites) keeps the
    favourites of a channel whose key changes. Returns (added, updated, removed);
    the caller saves the database."""
    added = updated = removed = 0
    for r in rows:
        if r.status == NEW:
            if group:
                r.found.root = group
            db.channels.append(r.found)
            added += 1
        elif r.status == CHANGED and _listed(db, r.old):
            key = r.old.key
            _update(r.old, r.found, r.changes)
            if favs is not None:
                favs.rekey(key, r.old.key)
            updated += 1
        elif r.status == GONE and _listed(db, r.old):
            db.channels[:] = [c for c in db.channels if c is not r.old]
            removed += 1
    if added or removed:
        db.renumber()
    return added, updated, removed
