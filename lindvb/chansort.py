# SPDX-License-Identifier: GPL-2.0-or-later
"""Sorting and grouping of the channel list (no GTK)."""

from .i18n import _

# the "Sort by" choices of the channel list: (setting value, label)
SORTS = (("number", _("Number")), ("az", _("Name A–Z")), ("za", _("Name Z–A")),
         ("provider", _("Provider / category")), ("transponder", _("Transponder")),
         ("quality", _("Picture quality")))
GROUPED = ("provider", "transponder", "quality")   # these offer a value to pick


def quality(c):
    """"UHD", "HD", "SD" from the name (as the HD filter), "" for radio."""
    if c.radio:
        return ""
    n = c.name.upper()
    if "UHD" in n or "4K" in n:
        return "UHD"
    if "HD" in n or "HEVC" in n:
        return "HD"
    return "SD"


def transponder(c):
    """A readable transponder name, "" for internet streams."""
    if c.is_iptv or not c.frequency:
        return ""
    if c.is_sat:
        return f"{c.frequency / 1000:.0f} {(c.polarity or 'h').upper()}"
    return f"{c.frequency / 1000:.1f} MHz"


def group_value(c, sort):
    """The value of *c* the list groups by for *sort* ("" = none)."""
    if sort == "provider":
        return c.category or ""
    if sort == "transponder":
        return transponder(c)
    if sort == "quality":
        return quality(c)
    return ""


def group_values(c, sort):
    """All values of *c* the list groups by for *sort*: a Tvheadend channel
    can be in several groups (its channel tags), the others in one."""
    if sort == "provider" and c.groups:
        return list(c.groups)
    v = group_value(c, sort)
    return [v] if v else []


def in_group(c, sort, value):
    """True when *c* belongs to the group *value* of *sort*."""
    return value in group_values(c, sort)


_QUALITY_ORDER = {"UHD": 0, "HD": 1, "SD": 2, "": 3}


def sort_key(c, sort):
    """Key for *sort*; ``None`` for "number" (the list order stays)."""
    name = c.name.casefold()
    if sort == "az" or sort == "za":
        return name
    if sort == "provider":
        return (not c.category, c.category.casefold(), name)
    if sort == "transponder":
        return (c.is_iptv or not c.frequency, c.root, c.delsys, c.frequency,
                (c.polarity or "").lower(), name)
    if sort == "quality":
        return (_QUALITY_ORDER[quality(c)], name)
    return None


def values(channels, sort):
    """The distinct group values among *channels*, in list order of *sort*."""
    seen = {}
    for c in sorted(channels, key=lambda c: sort_key(c, sort)):
        for v in group_values(c, sort):
            seen.setdefault(v, True)
    if sort == "provider":
        return sorted(seen, key=str.casefold)
    return list(seen)


# -- sources ------------------------------------------------------------------
# Besides the groups (the satellite, network, import file or IPTV list a
# channel came from) the group dropdown offers the reception paths when the
# list mixes several: satellite / cable / terrestrial / internet streams, and
# the DiSEqC ports of a dish switch. Their keys start with "src:".
SOURCE_PREFIX = "src:"
_KINDS = ("sat", "cable", "terr", "iptv")


def source_kind(c):
    """"sat", "cable", "terr" (antenna) or "iptv"."""
    if c.is_iptv:
        return "iptv"
    if c.is_sat:
        return "sat"
    return "cable" if c.delsys.startswith("dvb-c") else "terr"


def _kind_label(kind):
    return {"sat": _("Satellite"), "cable": _("Cable"), "terr": _("Terrestrial"),
            "iptv": _("Internet streams")}[kind]


def sources(channels):
    """The source entries for *channels* as (key, label): one per reception
    path when there are several, one per DiSEqC port when satellite channels
    sit behind different ports. Empty when the list has just one source."""
    kinds, ports = set(), set()
    for c in channels:
        k = source_kind(c)
        kinds.add(k)
        if k == "sat":
            ports.add(c.diseqc if c.diseqc >= 0 else -1)
    out = []
    for k in _KINDS:
        if k not in kinds:
            continue
        if len(kinds) > 1:
            out.append((SOURCE_PREFIX + k, _kind_label(k)))
        if k == "sat" and len(ports) > 1:
            out += [(f"{SOURCE_PREFIX}sat:{p}",
                     _("Satellite · DiSEqC port {n}").format(n=p + 1))
                    for p in sorted(ports) if p >= 0]
    return out


def in_source(c, key):
    """Whether *c* belongs to the source entry *key* (see ``sources``)."""
    kind, _sep, port = key[len(SOURCE_PREFIX):].partition(":")
    if source_kind(c) != kind:
        return False
    return not port or c.diseqc == int(port)
