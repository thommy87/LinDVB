# SPDX-License-Identifier: GPL-2.0-or-later
"""Sorting and grouping of the channel list (no GTK)."""

from .i18n import _
from .media_info import video_class

# the "Sort by" choices of the channel list: (setting value, label)
SORTS = (("number", _("Number")), ("az", _("Name A–Z")), ("za", _("Name Z–A")),
         ("provider", _("Provider / category")), ("transponder", _("Transponder")),
         ("quality", _("Video resolution")))
GROUPED = ("provider", "transponder", "quality")   # these offer a value to pick


# DVB service types (EN 300 468, service_descriptor) that tell the picture:
# MPEG-2 and H.264 SD, MPEG-2 and H.264 HD (with their NVOD and 3D kinds),
# HEVC (HD or better) and HEVC UHD
HEVC = 0x1F
SERVICE_QUALITY = {0x01: "SD", 0x16: "SD", 0x17: "SD", 0x18: "SD",
                   0x11: "HD", 0x19: "HD", 0x1A: "HD", 0x1B: "HD", 0x1C: "HD", 0x1D: "HD",
                   0x1E: "HD", HEVC: "HD", 0x20: "UHD"}


def named_quality(name):
    """"UHD", "HD", "SD" from a channel name alone."""
    n = name.upper()
    if "UHD" in n or "4K" in n:
        return "UHD"
    if "HD" in n or "HEVC" in n:
        return "HD"
    return "SD"


def quality(c):
    """"UHD", "HD", "SD" for a TV channel, "" for radio: the picture seen
    while watching it, else its service type, else its name."""
    if c.radio:
        return ""
    if c.seen_video:
        return c.seen_video
    named = named_quality(c.name)
    if c.service_type == HEVC and named == "UHD":
        return "UHD"
    return SERVICE_QUALITY.get(c.service_type) or named


def learn_video(c, width, height):
    """Remember the class of the picture *c* shows (width x height). True
    when that changes what ``quality`` says."""
    vc = video_class(width, height)
    if c.radio or not vc or vc[0] == c.seen_video:
        return False
    before = quality(c)
    c.seen_video = vc[0]
    return quality(c) != before


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


def in_group(c, sort, value):
    """True when *c* belongs to the group *value* of *sort*."""
    return group_value(c, sort) == value


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
        v = group_value(c, sort)
        if v:
            seen.setdefault(v, True)
    if sort == "provider":
        return sorted(seen, key=str.casefold)
    return list(seen)


# -- sources ------------------------------------------------------------------
# The channel editor's group dropdown offers the reception paths when the list
# mixes several: satellite / cable / terrestrial / internet streams, and the
# DiSEqC ports of a dish switch. Their keys start with "src:". (The channel
# list has its own filter steps, lindvb.listfilter.)
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
