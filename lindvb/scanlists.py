# SPDX-License-Identifier: GPL-2.0-or-later
"""The transponder lists the channel search offers, sorted the way people pick them.

LinDVB ships the tables of `dtv-scan-tables <https://git.linuxtv.org/dtv-scan-tables.git>`_
as kept up to date in Tvheadend's copy (https://github.com/tvheadend/dtv-scan-tables)
(GPL-2.0 / LGPL-2.1, packed by ``tools/update_scantables.py`` into
``scantables/dtv-scan-tables.json.gz``). Lists installed on the system, in
folders the user added and DVBViewer style ``.ini`` lists are added to them.

* Cable, terrestrial, ATSC and ISDB-T lists are sorted by **country** and then
  by **region or provider**. A country without a list, and every country,
  also gets a blind scan over the whole channel raster.
* Satellite lists are sorted by **orbital position**. All lists of one
  position (Astra 1KR, 1L, 1M, 1N at 19.2°E) form one entry, so one DiSEqC
  port covers them; their transponders are merged without duplicates.

Which lists are shown follows the delivery systems the tuner reports. The
module has no GTK; the dialog is lindvb.ui.scan.
"""

import gzip
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .channels import DVBV5_DELSYS, DVBV5_MOD, decode_text
from .i18n import N_, _
from .scan import (BUNDLED_TABLES, SCAN_TABLE_DIRS, Transponder, TransponderList,
                   parse_transponder_ini)

BUNDLED_INDEX = BUNDLED_TABLES / "dtv-scan-tables.json.gz"

# reception types (the same keys as lindvb.tunerprofiles.FAMILIES)
SAT, CABLE, TERR = "sat", "cab", "ter"

# the delivery system of a list -> the tuner systems that receive it
RECEIVERS = {
    "dvb-s": ("dvb-s", "dvb-s2"), "dvb-s2": ("dvb-s2",),
    "dvb-c-a": ("dvb-c-a", "dvb-c-c"), "dvb-c-b": ("dvb-c-b",),
    "dvb-t": ("dvb-t", "dvb-t2"), "dvb-t2": ("dvb-t2",),
    "atsc": ("atsc",), "isdb-t": ("isdb-t",), "dtmb": ("dtmb",),
}
FAMILY_OF = {"dvb-s": SAT, "dvb-s2": SAT, "dvb-c-a": CABLE, "dvb-c-b": CABLE,
             "dvb-t": TERR, "dvb-t2": TERR, "atsc": TERR, "isdb-t": TERR, "dtmb": TERR}

# pseudo countries at the end of the country list
OTHER_COUNTRY = "~other"     # no list for the country: blind scan only
OWN_LISTS = "~own"           # lists from added folders that name no country
VIRTUAL = "~vidtv"           # the kernel's virtual test tuner

# English names, as in ISO 3166 (common names); translated through the catalogues
COUNTRIES = {
    "ad": N_("Andorra"), "ar": N_("Argentina"), "at": N_("Austria"), "au": N_("Australia"),
    "ax": N_("Åland Islands"), "be": N_("Belgium"), "bg": N_("Bulgaria"), "br": N_("Brazil"),
    "ca": N_("Canada"), "ch": N_("Switzerland"), "cn": N_("China"), "co": N_("Colombia"),
    "cz": N_("Czechia"), "de": N_("Germany"), "dk": N_("Denmark"), "ee": N_("Estonia"),
    "es": N_("Spain"), "fi": N_("Finland"), "fr": N_("France"), "gr": N_("Greece"),
    "hk": N_("Hong Kong"), "hr": N_("Croatia"), "hu": N_("Hungary"), "ie": N_("Ireland"),
    "id": N_("Indonesia"), "il": N_("Israel"), "ir": N_("Iran"), "is": N_("Iceland"), "it": N_("Italy"),
    "ke": N_("Kenya"), "kr": N_("South Korea"), "li": N_("Liechtenstein"),
    "lt": N_("Lithuania"), "lu": N_("Luxembourg"), "lv": N_("Latvia"),
    "mk": N_("North Macedonia"),
    "nl": N_("Netherlands"), "no": N_("Norway"), "nz": N_("New Zealand"), "pl": N_("Poland"),
    "pt": N_("Portugal"), "ro": N_("Romania"), "ru": N_("Russia"), "se": N_("Sweden"),
    "si": N_("Slovenia"), "sk": N_("Slovakia"), "tw": N_("Taiwan"), "ua": N_("Ukraine"),
    "ug": N_("Uganda"), "uk": N_("United Kingdom"), "us": N_("United States"),
    "vn": N_("Vietnam"), "za": N_("South Africa"),
}
# DVBViewer names its lists after the country in English (DVB-T2_Germany.ini)
_COUNTRY_BY_NAME = {v.lower(): k for k, v in COUNTRIES.items()}
_COUNTRY_BY_NAME.update({"america": "us", "united kingdom": "uk", "uk": "uk",
                         "england": "uk", "czech republic": "cz", "taiwan": "tw"})

# blind scans: the channel raster of a region
BLIND_TERR = {
    # key: (label, dtv-scan-tables list or None, generated raster)
    "eu": (N_("All frequencies, 7/8 MHz (Europe, Africa, Asia)"), "auto-Default", None),
    "au": (N_("All frequencies, 7 MHz (Australia)"), "auto-Australia", None),
    "us6": (N_("All frequencies, 6 MHz (Americas)"), None,
            [(177000 + 6000 * i, 6) for i in range(7)] +
            [(473000 + 6000 * i, 6) for i in range(38)]),
    "tw": (N_("All frequencies, 6 MHz (Taiwan)"), "auto-Taiwan", None),
}
BLIND_FOR_COUNTRY = {"au": "au", "tw": "tw", "co": "us6", "pa": "us6", "us": "us6",
                     "ca": "us6", "mx": "us6"}
BLIND_ISDB = "br-Brazil"                                 # every ISDB-T channel (6 MHz)
BLIND_ATSC = "us-ATSC-center-frequencies-8VSB"           # every ATSC channel
BLIND_CABLE = [(f, sr, mod) for f in range(114000, 858001, 8000)
               for sr, mod in ((6900, "QAM/256"), (6900, "QAM/64"), (6875, "QAM/256"))]
BLIND_DTMB = [(f, 8) for f in range(474000, 858001, 8000)]
# a frontend that does not lock gives up after about this long (dvbv5-scan)
SECONDS_PER_ENTRY = 1.5


@dataclass
class ScanList:
    """One transponder list: a dtv-scan-tables file, an .ini or a file in a folder."""
    key: str                     # unique, e.g. "dvb-t/de-Berlin" or a path
    system: str                  # dvbsrc nick of its transponders: dvb-s, dvb-c-a, atsc ...
    country: str = ""            # ISO code ("uk" as dtv-scan-tables spells it), or a pseudo one
    region: str = ""             # region, provider or satellite name
    position: int | None = None  # satellites: tenths of a degree, east positive
    title: str = ""
    channels: list | None = None  # [{"DELIVERY_SYSTEM": ..., ...}], None: read from path
    path: str = ""

    @property
    def family(self):
        return FAMILY_OF.get(self.system, TERR)

    def entries(self):
        """The [CHANNEL] blocks of the list as dicts of dvbv5 keys."""
        if self.channels is None:
            self.channels = _read_file(self.path)
        return self.channels


@dataclass
class Choice:
    """One entry the user can pick: a list, several lists of one position, or a blind scan."""
    label: str
    lists: list = field(default_factory=list)
    blind: str = ""              # BLIND_TERR key, "cable", "dtmb"
    position: int | None = None


# -- reading lists ---------------------------------------------------------------------

def parse_table(text):
    """(title, [{KEY: value} of each [CHANNEL] block]) of a dvbv5 scan table."""
    title, blocks, cur = "", [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not title and not blocks:
                title = line.lstrip("# ").strip()
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = {}
            blocks.append(cur)
        elif "=" in line and cur is not None:
            k, _sep, v = line.partition("=")
            cur[k.strip()] = v.strip()
    return title, [b for b in blocks if "FREQUENCY" in b and "DELIVERY_SYSTEM" in b]


def _read_file(path):
    try:
        text = decode_text(Path(path).read_bytes())
    except OSError:
        return []
    if "[SATTYPE]" in text:
        return [_transponder_entry(t) for t in parse_transponder_ini(path).transponders]
    return parse_table(text)[1]


def _transponder_entry(t):
    from .channels import DVBV5_DELSYS_REV, DVBV5_MOD_REV
    e = {"DELIVERY_SYSTEM": DVBV5_DELSYS_REV.get(t.delsys, "DVBT")}
    if t.delsys in ("dvb-s", "dvb-s2"):
        e["FREQUENCY"] = str(t.frequency)
        e["POLARIZATION"] = "HORIZONTAL" if t.polarity == "h" else "VERTICAL"
    else:
        e["FREQUENCY"] = str(t.frequency * 1000)
    if t.symbol_rate:
        e["SYMBOL_RATE"] = str(t.symbol_rate * 1000)
    if t.bandwidth_hz:
        e["BANDWIDTH_HZ"] = str(t.bandwidth_hz)
    if t.modulation in DVBV5_MOD_REV:
        e["MODULATION"] = DVBV5_MOD_REV[t.modulation]
    e["INNER_FEC"] = t.fec.upper() if t.fec not in ("", "auto") else "AUTO"
    return e


def _system_of(entries):
    """The delivery system most of the entries use (dvbsrc nick)."""
    c = Counter(DVBV5_DELSYS.get(e.get("DELIVERY_SYSTEM", ""), "") for e in entries)
    c.pop("", None)
    if not c:
        return ""
    if c.get("dvb-s") and c.get("dvb-s2"):
        return "dvb-s"           # a satellite list with both: every DVB-S tuner takes part of it
    if c.get("dvb-t") and c.get("dvb-t2"):
        return "dvb-t"
    return c.most_common(1)[0][0]


_POS = re.compile(r"[-_ ]?(\d{1,3}(?:[.,]\d)?)\s*°?\s*([EOW])$", re.I)


def parse_position(text):
    """Tenths of a degree (east positive) at the end of a name: "Astra-1KR-19.2E" -> 192."""
    m = _POS.search(text.strip())
    if not m:
        return None
    deg = round(float(m.group(1).replace(",", ".")) * 10)
    return -deg if m.group(2).upper() == "W" else deg


def format_position(pos):
    """192 -> "19.2°E", -8 -> "0.8°W"."""
    return f"{abs(pos) / 10:.1f}°{'W' if pos < 0 else 'E'}"


def _pretty(name):
    """"Kabel_Vodafone_Dresden" -> "Kabel Vodafone Dresden", "CrystalPalace" -> "Crystal Palace"."""
    name = name.replace("_", " ")
    return re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", name).strip()


def _from_table(family, name, title, channels=None, path=""):
    """A ScanList from a dtv-scan-tables file name (dvb-t/de-Berlin, dvb-s/Astra-1KR-19.2E)."""
    system = _system_of(channels) if channels is not None else ""
    if not system:
        system = {"dvb-s": "dvb-s", "dvb-c": "dvb-c-a", "dvb-t": "dvb-t",
                  "atsc": "atsc", "isdb-t": "isdb-t", "dtmb": "dtmb"}.get(family, "dvb-t")
    key = f"{family}/{name}"
    if family == "dvb-s":
        pos = parse_position(name)
        sat = _POS.sub("", name).replace("-&-", " & ").replace("-", " ").strip()
        return ScanList(key, system, region=sat or name, position=pos, title=title,
                        channels=channels, path=path)
    cc, _sep, rest = name.partition("-")
    cc = cc.lower()
    if cc == "auto" or cc not in COUNTRIES or not rest:
        cc = ""
    if cc == "br" and re.match(r"[a-z]{2}-", rest):
        state, _sep, city = rest.partition("-")
        region = f"{state.upper()} – {_pretty(city)}"
    elif rest.lower() == "all":
        region = _("Whole country")
    else:
        region = _pretty(rest)
    return ScanList(key, system, cc, region if cc else _pretty(name), title=title,
                    channels=channels, path=path)


@lru_cache(maxsize=1)
def bundled_lists(index=BUNDLED_INDEX):
    """The lists of the bundled dtv-scan-tables, and the commit they come from."""
    try:
        data = json.loads(gzip.decompress(Path(index).read_bytes()))
    except (OSError, ValueError):
        return (), ""
    out = tuple(_from_table(t["family"], t["name"], t.get("title", ""), t["channels"])
                for t in data.get("tables", []))
    return out, data.get("commit", "")


def _virtual_lists():
    out = []
    for f in sorted(BUNDLED_TABLES.glob("vidtv-*")):
        title, ch = parse_table(f.read_text())
        out.append(ScanList(f"vidtv/{f.name}", _system_of(ch) or "dvb-t", VIRTUAL,
                            _("Virtual tuner (vidtv)") + f" – {f.name[6:].upper()}",
                            position=192 if "dvbs" in f.name else None, title=title,
                            channels=ch, path=str(f)))
    return out


def _from_file(path, own=False):
    """A ScanList for a file in a folder: dtv-scan-tables layout, .ini or anything dvbv5."""
    p = Path(path)
    try:
        text = decode_text(p.read_bytes())
    except OSError:
        return None
    if "[SATTYPE]" in text:
        tpl = parse_transponder_ini(p)
        entries = [_transponder_entry(t) for t in tpl.transponders]
        if not entries:
            return None
        system = _system_of(entries)
        if tpl.kind == "sat":
            pos = _ini_position(p)
            name = _POS.sub("", tpl.name).strip(" -") or tpl.name
            return ScanList(str(p), system, region=name, position=pos, title=tpl.name,
                            channels=entries, path=str(p))
        cc, region = _ini_country(p.stem, tpl.name)
        return ScanList(str(p), system, cc or OWN_LISTS, region, title=tpl.name,
                        channels=entries, path=str(p))
    title, entries = parse_table(text)
    if not entries:
        return None
    family = p.parent.name if p.parent.name in ("dvb-s", "dvb-c", "dvb-t", "atsc", "isdb-t") \
        else ""
    system = _system_of(entries)
    if not family:
        family = {"dvb-s": "dvb-s", "dvb-s2": "dvb-s", "dvb-c-a": "dvb-c",
                  "dvb-c-b": "atsc"}.get(system, "dvb-t")
    sl = _from_table(family, p.name, title, entries, str(p))
    sl.key = str(p)
    if sl.family != SAT and not sl.country:
        sl.country, sl.region = (OWN_LISTS if own else ""), _pretty(p.name)
    if sl.family == SAT and sl.position is None:
        sl.position = parse_position(title)
    return sl


def _ini_position(path):
    import configparser
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string(decode_text(Path(path).read_bytes()))
        pos = int(cp.get("SATTYPE", "1", fallback="") or 0)
    except (OSError, ValueError, configparser.Error):
        return parse_position(Path(path).stem)
    if not pos:
        return parse_position(Path(path).stem)
    return pos - 3600 if pos > 1800 else pos


def _ini_country(stem, name):
    """("de", "Vodafone") for DVB-C_de_Vodafone, ("de", "…") for DVB-T2_Germany."""
    parts = re.split(r"[_ ]", stem, maxsplit=2)
    if len(parts) >= 2 and parts[1].lower() in COUNTRIES:
        return parts[1].lower(), _pretty(parts[2]) if len(parts) > 2 else _("Whole country")
    rest = stem.split("_", 1)[1] if "_" in stem else stem
    cc = _COUNTRY_BY_NAME.get(rest.replace("_", " ").lower(), "")
    return cc, (name if not cc else _("Whole country"))


def all_lists(extra_dirs=(), system_dirs=SCAN_TABLE_DIRS):
    """Every list: the bundled tables, then the system's and the added folders' ones.

    A system table with the name of a bundled one is older and left out.
    """
    bundled, _commit = bundled_lists()
    out = list(bundled) + _virtual_lists()
    have = {sl.key for sl in out}
    for own, dirs in ((False, system_dirs), (True, extra_dirs)):
        for d in dirs:
            root = Path(d).expanduser()
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if not f.is_file() or f.name.startswith(".") or "channels-conf" in f.parts:
                    continue
                if not own and f"{f.parent.name}/{f.name}" in have:
                    continue
                sl = _from_file(f, own)
                if sl is not None and (own or sl.country or sl.family == SAT):
                    out.append(sl)
    return out


# -- what a tuner may pick ------------------------------------------------------------

def receivable(sl, systems):
    """True if a tuner with *systems* (empty: unknown) can use the list."""
    if not systems:
        return True
    return any(s in systems for s in RECEIVERS.get(sl.system, (sl.system,)))


def families_for(systems):
    """The reception types a tuner offers, in the order sat, cable, terrestrial."""
    if not systems:
        return [SAT, CABLE, TERR]
    have = set()
    for s in systems:
        for fam, recv in ((SAT, ("dvb-s", "dvb-s2", "turbo")),
                          (CABLE, ("dvb-c-a", "dvb-c-b", "dvb-c-c", "dvb-c2")),
                          (TERR, ("dvb-t", "dvb-t2", "atsc", "isdb-t", "dtmb"))):
            if s in recv:
                have.add(fam)
    return [f for f in (SAT, CABLE, TERR) if f in have]


def country_name(cc):
    if cc == OTHER_COUNTRY:
        return _("Other country (no list)")
    if cc == OWN_LISTS:
        return _("Own lists")
    if cc == VIRTUAL:
        return _("Virtual tuner (vidtv)")
    return _(COUNTRIES[cc]) if cc in COUNTRIES else cc.upper()


def _sort_key(text):
    """Sort "Åland" with "Aland", not after "Z"."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def countries(lists, family, systems):
    """[(code, name)] of the countries with lists for the tuner, by name, then the pseudo ones."""
    have = {sl.country for sl in lists
            if sl.family == family and sl.country and receivable(sl, systems)}
    real = sorted((c for c in have if not c.startswith("~")), key=lambda c: _sort_key(country_name(c)))
    tail = [c for c in (OWN_LISTS, VIRTUAL) if c in have]
    if family == TERR or family == CABLE:
        tail.append(OTHER_COUNTRY)
    return [(c, country_name(c)) for c in real + tail]


def regions(lists, family, systems, country):
    """The Choices of a country: its lists by name, then the matching blind scans."""
    out = []
    if country != OTHER_COUNTRY:
        mine = [sl for sl in lists if sl.family == family and sl.country == country
                and receivable(sl, systems)]
        whole = _("Whole country")
        mine.sort(key=lambda sl: (sl.region != whole, _sort_key(sl.region)))
        out = [Choice(sl.region, [sl]) for sl in mine]
        if country in (OWN_LISTS, VIRTUAL):
            return out
    return out + blind_choices(family, systems, country)


def blind_choices(family, systems, country=""):
    """The blind scans a tuner can run, the one of the country's raster first."""
    out = []
    if family == CABLE:
        if receivable(ScanList("", "dvb-c-a"), systems):
            out.append(Choice(_("All frequencies (blind scan)"), blind="cable"))
        return out
    if receivable(ScanList("", "dvb-t"), systems):
        keys = list(BLIND_TERR)
        if country != OTHER_COUNTRY:
            first = BLIND_FOR_COUNTRY.get(country, "eu")
            keys = [first]
        out += [Choice(_(BLIND_TERR[k][0]), blind=k) for k in keys]
    if receivable(ScanList("", "isdb-t"), systems) and (country in ("", OTHER_COUNTRY, "br", "ar")
                                                        or not systems):
        out.append(Choice(_("All frequencies, 6 MHz (ISDB-T)"), blind="isdb"))
    if receivable(ScanList("", "atsc"), systems) and (country in ("", OTHER_COUNTRY, "us", "ca")
                                                      or not systems):
        out.append(Choice(_("All frequencies (ATSC)"), blind="atsc"))
    if receivable(ScanList("", "dtmb"), systems) and systems:
        out.append(Choice(_("All frequencies, 8 MHz (DTMB)"), blind="dtmb"))
    return out


# -- satellites -----------------------------------------------------------------------

def positions(lists, systems, tolerance=3):
    """One Choice per orbital position, east to west.

    Lists whose positions lie within *tolerance* tenths of a degree of each other
    form one entry (Astra 2E at 28.5°E belongs to 28.2°E, Thor 5/6/7 at 0.6–0.8°W
    and Intelsat 10-02 at 1.0°W to one dish position).
    """
    sats = sorted((sl for sl in lists if sl.family == SAT and sl.position is not None
                   and receivable(sl, systems)), key=lambda sl: -sl.position)
    groups = []
    for sl in sats:
        if groups and groups[-1][-1].position - sl.position <= tolerance:
            groups[-1].append(sl)
        else:
            groups.append([sl])
    out = []
    for g in groups:
        common = Counter(sl.position for sl in g).most_common()
        top = max(n for _p, n in common)
        mean = sum(sl.position for sl in g) / len(g)
        pos = min((p for p, n in common if n == top), key=lambda p: abs(p - mean))
        names = []
        for sl in g:
            for name in sl.region.split(" & "):
                if sl.country != VIRTUAL and name.strip() and name.strip() not in names:
                    names.append(name.strip())
        label = format_position(pos)
        if names:
            shown = ", ".join(names[:4]) + (", …" if len(names) > 4 else "")
            label += f" ({shown})"
        out.append(Choice(label, g, position=pos))
    return out


def find_position(choices, pos):
    """Index of the Choice for *pos* (tenths of a degree), -1 if none."""
    for i, c in enumerate(choices):
        if c.position is not None and abs(c.position - pos) <= 3:
            return i
    return -1


# -- turning a choice into a scan -----------------------------------------------------

def _to_transponder(e):
    delsys = DVBV5_DELSYS.get(e.get("DELIVERY_SYSTEM", ""), "dvb-s")
    fec = e.get("INNER_FEC", "AUTO").upper()
    fec = fec if re.fullmatch(r"\d+/\d+", fec) else "auto"
    t = Transponder(delsys, int(e.get("FREQUENCY", 0) or 0),
                    e.get("POLARIZATION", "H")[:1].lower(),
                    int(e.get("SYMBOL_RATE", 0) or 0) // 1000,
                    DVBV5_MOD.get(e.get("MODULATION", "AUTO"), "auto"), fec.lower())
    return t


def satellite_list(choice, systems=()):
    """A TransponderList (kind "sat") of every transponder of the choice's lists.

    Duplicates (same polarisation, frequency within 2 MHz) are left out; DVB-S2
    transponders are left out for a tuner that only has DVB-S.
    """
    no_s2 = bool(systems) and "dvb-s2" not in systems
    tps = []
    for sl in choice.lists:
        for e in sl.entries():
            if "FREQUENCY" not in e:
                continue
            t = _to_transponder(e)
            if t.frequency <= 0 or (no_s2 and t.delsys == "dvb-s2"):
                continue
            tps.append(t)
    tps.sort(key=lambda t: (t.polarity, t.frequency))
    out = []
    for t in tps:
        if out and out[-1].polarity == t.polarity and t.frequency - out[-1].frequency <= 2000:
            continue
        out.append(t)
    out.sort(key=lambda t: (t.frequency, t.polarity))
    name = format_position(choice.position) if choice.position is not None else choice.label
    return TransponderList(name, "sat", out)


def scan_entries(choice, systems=()):
    """The dvbv5 [CHANNEL] blocks to search for a cable or terrestrial choice."""
    t2 = bool(systems) and "dvb-t2" in systems
    if choice.blind == "cable":
        return [{"DELIVERY_SYSTEM": "DVBC/ANNEX_A", "FREQUENCY": str(f * 1000),
                 "SYMBOL_RATE": str(sr * 1000), "MODULATION": mod, "INNER_FEC": "AUTO"}
                for f, sr, mod in BLIND_CABLE]
    if choice.blind == "dtmb":
        return [{"DELIVERY_SYSTEM": "DTMB", "FREQUENCY": str(f * 1000),
                 "BANDWIDTH_HZ": str(bw * 1000000)} for f, bw in BLIND_DTMB]
    if choice.blind in ("isdb", "atsc"):
        name = f"isdb-t/{BLIND_ISDB}" if choice.blind == "isdb" else f"atsc/{BLIND_ATSC}"
        return _bundled_entries(name)
    if choice.blind in BLIND_TERR:
        _label, table, raster = BLIND_TERR[choice.blind]
        if table:
            base = _bundled_entries(f"dvb-t/{table}")
        else:
            base = [{"DELIVERY_SYSTEM": "DVBT", "FREQUENCY": str(f * 1000),
                     "BANDWIDTH_HZ": str(bw * 1000000)} for f, bw in raster]
        base = [dict(e, DELIVERY_SYSTEM="DVBT") for e in base]
        if t2:
            # a DVB-T2 multiplex does not lock with DELIVERY_SYSTEM = DVBT on most drivers
            base += [dict(e, DELIVERY_SYSTEM="DVBT2") for e in base]
        return base
    out = []
    for sl in choice.lists:
        for e in sl.entries():
            if e.get("DELIVERY_SYSTEM") == "DVBT2" and systems and not t2:
                continue
            out.append(dict(e))
    return out


def _bundled_entries(key):
    bundled, _commit = bundled_lists()
    for sl in bundled:
        if sl.key == key:
            return [dict(e) for e in sl.entries()]
    return []


def write_entries(entries):
    """An initial tuning file in dvbv5 format from [CHANNEL] blocks."""
    out = []
    for e in entries:
        out.append("[CHANNEL]")
        out += [f"\t{k} = {v}" for k, v in e.items()]
        out.append("")
    return "\n".join(out) + "\n"


def estimate_minutes(n_entries):
    """Roughly how long a search of *n_entries* frequencies takes when few of them lock."""
    return max(1, round(n_entries * SECONDS_PER_ENTRY / 60))


def default_target(choice, family):
    """The channel list group a search fills when the user names none."""
    if family == SAT and choice.position is not None:
        return format_position(choice.position)
    if choice.lists and len(choice.lists) == 1:
        sl = choice.lists[0]
        if sl.country and not sl.country.startswith("~"):
            fam = {CABLE: _("Cable"), TERR: _("Terrestrial")}.get(family, "")
            return f"{fam} {country_name(sl.country)}".strip()
        return sl.region
    return {CABLE: _("Cable"), TERR: _("Terrestrial")}.get(family, choice.label)

