# SPDX-License-Identifier: GPL-2.0-or-later
"""Sorting and grouping of the channel list (no GTK)."""

from .i18n import _

# the "Sort by" choices of the channel list: (setting value, label)
SORTS = (("number", _("Number")), ("az", _("Name A–Z")), ("za", _("Name Z–A")),
         ("provider", _("Provider")), ("transponder", _("Transponder")),
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
        if v and v not in seen:
            seen[v] = True
    return list(seen)
