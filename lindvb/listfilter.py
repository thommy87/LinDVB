# SPDX-License-Identifier: GPL-2.0-or-later
"""The channel list filter in four linked steps (no GTK).

1. **Source**, ticked: satellite, cable, terrestrial, internet streams, DVB-I
   and each Tvheadend server. Nothing ticked is all sources.
2. **Area** of the sources ticked, ticked: the orbital positions (else the
   DiSEqC ports) of the satellite channels with their satellites, the lists
   of the others. An area narrows only the channels of its own source. Which
   satellite a channel is on comes from the transponder lists of the channel
   search (lindvb.scanlists.satellites).
3. **Filter**: reception (all, free to air, encrypted; offered with
   broadcast channels, internet streams count as free to air), picture
   quality (UHD, HD, SD; TV only) and the Tvheadend tags. Within a section a
   channel needs one of the entries ticked, across the sections all of them.
4. **Sort** order, and for a grouping order one value (lindvb.chansort).

Each step offers only what suits the choices of the steps before it. What a
channel brings to the steps is worked out once per channel (:func:`info`),
what all channels of a list mode offer once per list (:func:`catalogue`): a
click in a step costs no pass over the channels, filtering the list compares
a few sets per channel.

The choices are kept per list mode ("tv", "radio") in the setting
``list_filters``; :func:`migrate` turns the settings of older versions into it.
"""

import re
from dataclasses import dataclass, field, replace

from . import chansort
from .i18n import _
from .scanlists import format_position, parse_position, satellites

BROADCAST = ("sat", "cable", "terr")
QUALITIES = ("UHD", "HD", "SD")
RECEPTIONS = ("all", "fta", "enc")
MODES = ("tv", "radio")
TVH = "tvh:"                     # a Tvheadend server's source key: "tvh:<server address>"
_ORDER = ("sat", "cable", "terr", "iptv", "dvbi")
_SORTS = tuple(k for k, _l in chansort.SORTS)
_POS_SUFFIX = re.compile(r"[-_ ]*\d{1,3}(?:[.,]\d)?\s*°?\s*[EOW]$", re.I)


@dataclass(frozen=True)
class Info:
    """What one channel brings to the steps."""
    source: str
    areas: frozenset = frozenset()
    quality: str = ""
    tags: frozenset = frozenset()
    encrypted: bool = False          # broadcast channels only (Tvheadend decrypts itself)


@dataclass(frozen=True)
class State:
    """The choices of the four steps; an empty set is "all"."""
    sources: frozenset = frozenset()
    areas: frozenset = frozenset()
    reception: str = "all"
    qualities: frozenset = frozenset()
    tags: frozenset = frozenset()
    sort: str = "number"
    value: str = ""                  # the one value of a grouping sort shown ("" = all)

    def to_setting(self):
        return {"sources": sorted(self.sources), "areas": sorted(self.areas),
                "reception": self.reception, "qualities": sorted(self.qualities),
                "tags": sorted(self.tags), "sort": self.sort, "value": self.value}

    @classmethod
    def from_setting(cls, value):
        value = value if isinstance(value, dict) else {}

        def keys(name):
            v = value.get(name)
            return frozenset(x for x in v if isinstance(x, str)) if isinstance(v, list) \
                else frozenset()

        def one(name, allowed, default):
            v = value.get(name)
            return v if v in allowed else default
        v = value.get("value")
        return cls(keys("sources"), keys("areas"), one("reception", RECEPTIONS, "all"),
                   keys("qualities") & frozenset(QUALITIES), keys("tags"),
                   one("sort", _SORTS, "number"), v if isinstance(v, str) else "")


# -- per channel ------------------------------------------------------------------

def source_of(c):
    """The source key of *c* (see the module notes)."""
    if c.tvh_server:
        return TVH + c.tvh_server
    if c.dvbi_id:
        return "dvbi"
    return chansort.source_kind(c)


def area_keys(c, source):
    """The areas of *c*, each "<source>/<kind>:<value>": a satellite
    channel's orbital position from its group name with the satellites whose
    transponder list has its transponder ("sat/sat:<position>:<name>"), else
    its DiSEqC port, else its group; a terrestrial channel's group (the region
    searched) with its channel ("terr/mux:<kHz>:<group>"); the group (list,
    network, import) of the others."""
    if source.startswith(TVH):
        return frozenset()
    if source == "sat":
        pos = parse_position(c.root) if c.root else None
        if pos is not None:
            sats = satellites(pos, c.frequency, c.polarity) if c.frequency else ()
            return frozenset({f"sat/pos:{pos}"} | {f"sat/sat:{pos}:{n}" for n in sats})
        if c.diseqc >= 0:
            return frozenset({f"sat/port:{c.diseqc}"})
    if not c.root:
        return frozenset()
    if source == "terr" and c.frequency:
        return frozenset({f"terr/root:{c.root}", f"terr/mux:{c.frequency}:{c.root}"})
    return frozenset({f"{source}/root:{c.root}"})


def info(c):
    src = source_of(c)
    tags = frozenset(c.groups) if src.startswith(TVH) else frozenset()
    return Info(src, area_keys(c, src), chansort.quality(c), tags,
                bool(c.encrypted) and src in BROADCAST)


def matches(i, st):
    """Whether a channel with the Info *i* passes steps 1 to 3 of *st*."""
    if st.sources and i.source not in st.sources:
        return False
    if st.areas and not i.areas & st.areas and _has_area_of(st.areas, i.source):
        return False
    if st.reception == "fta" and i.encrypted or st.reception == "enc" and not i.encrypted:
        return False
    if st.qualities and i.quality not in st.qualities:
        return False
    if st.tags and not i.tags & st.tags:
        return False
    return True


def _has_area_of(areas, source):
    prefix = source + "/"
    return any(a.startswith(prefix) for a in areas)


# -- what the steps offer -----------------------------------------------------------

def source_label(key, names=None):
    if key.startswith(TVH):
        return "Tvheadend: " + ((names or {}).get(key) or key[len(TVH):])
    return {"sat": _("Satellite"), "cable": _("Cable"), "terr": _("Terrestrial"),
            "iptv": _("Internet streams"), "dvbi": "DVB-I"}[key]


@dataclass(frozen=True)
class Catalogue:
    """What the channels of one list mode offer to the steps, gathered in one
    pass over them (:func:`catalogue`), so a step changed costs no pass."""
    sources: tuple = ()              # ((key, label), …) in the order of step 1
    areas: dict = field(default_factory=dict)    # source -> {area: {group names}}
    tags: dict = field(default_factory=dict)     # source -> {tags}
    radio: bool = False


def catalogue(channels, infos, radio):
    names, areas, tags = {}, {}, {}
    for c, i in zip(channels, infos):
        src = i.source
        if src not in names:
            names[src] = c.root
        for a in i.areas:
            areas.setdefault(src, {}).setdefault(a, set()).add(c.root)
        if i.tags:
            tags.setdefault(src, set()).update(i.tags)
    keys = [k for k in _ORDER if k in names] + sorted(k for k in names if k.startswith(TVH))
    return Catalogue(tuple((k, source_label(k, names)) for k in keys), areas, tags, radio)


def _chosen(cat, chosen):
    return [k for k, _l in cat.sources if not chosen or k in chosen]


def areas(cat, chosen):
    """Step 2: [(heading, [(key, label, parent key or "")])], one section per
    source ticked (per source when none is) that has at least two entries.
    The satellites of an orbital position, the channels of a terrestrial
    region follow it as its children when it has several; one satellite (or
    the group name) only names the position: "13.0°E (Hot Bird 13F)"."""
    labels = dict(cat.sources)
    out = []
    for src in _chosen(cat, chosen):
        entries = cat.areas.get(src, {})
        items = []
        for key in sorted((k for k in entries if not parent_of(k)),
                          key=_area_order):
            kids = sorted((k for k in entries if parent_of(k) == key), key=_area_order)
            if len(kids) > 1:
                items += [(key, area_label(key), "")] + [(k, area_label(k), key) for k in kids]
            else:
                names = [area_label(k) for k in kids] or _group_names(key, entries[key])
                items.append((key, _named(area_label(key), names), ""))
        if len(items) >= 2:
            out.append((labels[src], items))
    return out


def tick(items, chosen, key, on):
    """*chosen* with the entry *key* of *items* ((key, label[, parent]), …)
    ticked or not. A parent ticked stands for all its children; ticking the
    last child of a parent ticks the parent instead, unticking one child of a
    parent ticked keeps its siblings."""
    kids = {}
    for item in items:
        if len(item) > 2 and item[2]:
            kids.setdefault(item[2], set()).add(item[0])
    parent = next((item[2] for item in items if item[0] == key and len(item) > 2), "")
    out = set(chosen)
    if parent and parent in out:
        out = (out - {parent}) | kids[parent]
    out = (out | {key}) if on else (out - {key})
    out -= kids.get(key, set())
    if parent and kids[parent] <= out:
        out = (out - kids[parent]) | {parent}
    return frozenset(out)


def tick_state(items, chosen, key):
    """"on", "off" or "part" (some of its children ticked) for *key*."""
    parent = next((item[2] for item in items if item[0] == key and len(item) > 2), "")
    if key in chosen or parent in chosen:
        return "on"
    kids = {item[0] for item in items if len(item) > 2 and item[2] == key}
    return "part" if kids & chosen else "off"


def parent_of(key):
    """The orbital position of a satellite's area key, the region of a
    terrestrial channel's, else ""."""
    if key.startswith("sat/sat:"):
        return "sat/pos:" + key[len("sat/sat:"):].partition(":")[0]
    if key.startswith("terr/mux:"):
        return "terr/root:" + key[len("terr/mux:"):].partition(":")[2]
    return ""


def channel_label(khz):
    """"K 23 · 490 MHz" for the European UHF / VHF band III raster, else "490 MHz"."""
    mhz = khz / 1000
    text = f"{mhz:g} MHz"
    if 474 <= mhz <= 858 and (mhz - 306) % 8 == 0:
        return f"K {int((mhz - 306) / 8)} · {text}"
    if 177.5 <= mhz <= 226.5 and (mhz - 142.5) % 7 == 0:
        return f"K {int((mhz - 142.5) / 7)} · {text}"
    return text


def _group_names(key, roots):
    if not key.startswith("sat/pos:"):
        return []
    names = {_POS_SUFFIX.sub("", r).replace("-", " ").replace("_", " ").strip() for r in roots}
    return sorted(names - {""}, key=str.casefold)


def _named(label, names):
    if not names:
        return label
    return f"{label} ({', '.join(names[:3])}{', …' if len(names) > 3 else ''})"


def area_label(key):
    kind, _sep, value = key.partition("/")[2].partition(":")
    if kind == "pos":
        return format_position(int(value))
    if kind == "port":
        return _("DiSEqC port {n}").format(n=int(value) + 1)
    if kind == "sat":
        return value.partition(":")[2]
    if kind == "mux":
        return channel_label(int(value.partition(":")[0]))
    return value


def _area_order(key):
    kind, _sep, value = key.partition("/")[2].partition(":")
    if kind == "root":
        return (2, 0, value.casefold())
    if kind == "sat":
        pos, _sep, name = value.partition(":")
        return (0, -int(pos), name.casefold())
    if kind == "mux":
        return (3, int(value.partition(":")[0]), "")
    return (0 if kind == "pos" else 1, -int(value) if kind == "pos" else int(value), "")


@dataclass
class Offer:
    """Step 3: the sections the filter shows."""
    reception: bool = False
    qualities: bool = False
    tags: list = field(default_factory=list)

    @property
    def empty(self):
        return not (self.reception or self.qualities or self.tags)


def offer(cat, chosen):
    """Step 3 for the sources ticked (all when none is): reception when there
    are broadcast channels, quality for TV, the tags of the Tvheadend servers."""
    srcs = _chosen(cat, chosen)
    tags = set().union(*(cat.tags.get(k, ()) for k in srcs))
    return Offer(any(k in BROADCAST for k in srcs), not cat.radio,
                 sorted(tags, key=str.casefold))


def sorts(cat, chosen):
    """Step 4: the sort orders; by transponder only with broadcast channels."""
    tuned = any(k in BROADCAST for k in _chosen(cat, chosen))
    return [(k, lbl) for k, lbl in chansort.SORTS if k != "transponder" or tuned]


def fit(st, cat):
    """*st* without the choices the steps no longer offer: a source that is
    gone, an area of a source not ticked, "free to air" with only internet
    streams ticked, a sort order not offered. The sort value is checked by
    the caller against the values of the channels shown."""
    chosen = st.sources & {k for k, _l in cat.sources}
    offered = {item[0] for _h, items in areas(cat, chosen) for item in items}
    o = offer(cat, chosen)
    srt = st.sort if st.sort in {k for k, _l in sorts(cat, chosen)} else "number"
    return replace(st, sources=frozenset(chosen), areas=st.areas & offered,
                   reception=st.reception if o.reception else "all",
                   qualities=st.qualities if o.qualities else frozenset(),
                   tags=st.tags & frozenset(o.tags), sort=srt,
                   value=st.value if srt == st.sort else "")


# -- settings ---------------------------------------------------------------------

def load(settings, mode):
    return State.from_setting((settings.get("list_filters") or {}).get(mode))


def save(settings, mode, st):
    settings["list_filters"] = {**(settings.get("list_filters") or {}), mode: st.to_setting()}


def rename_group(settings, source, old, new):
    """The groups *old* of *source* are now called *new*: an area ticked
    for one of them stays ticked."""
    gone = {f"{source}/root:{name}" for name in old}
    for mode in MODES:
        st = load(settings, mode)
        if st.areas & gone:
            save(settings, mode, replace(st, areas=(st.areas - gone) | {f"{source}/root:{new}"}))


_OLD_KEYS = ("list_filter", "list_group", "list_tag", "list_sort", "list_sort_value")
_OLD_FILTERS = ("all", "fav:0", "fta", "enc", "hd", "iptv")    # before "list_filter" held keys


def migrate(settings, channels):
    """Turn the list settings of older versions (one filter, one group, one
    tag, one sort for TV and radio together) into the choices of both list
    modes, and drop them. A favourites list kept as the filter opens that
    list in the favourites tab."""
    old = {k: settings.pop(k) for k in _OLD_KEYS if k in settings}
    if not old or settings.get("list_filters"):
        return
    f = old.get("list_filter")
    if isinstance(f, int) and not isinstance(f, bool):
        f = _OLD_FILTERS[f] if 0 <= f < len(_OLD_FILTERS) else "all"
    if isinstance(f, str) and f.startswith("fav:") and f[4:].isdigit():
        settings["fav_last"], settings["list_tab"] = int(f[4:]), "favourites"
    srcs, ars = set(), set()
    group = old.get("list_group") or ""
    if group:
        for c in channels:
            if _in_old_group(c, group):
                src = source_of(c)
                srcs.add(src)
                if not group.startswith("src:") or group.count(":") == 2:   # a group, a port
                    ars |= {k for k in area_keys(c, src) if not parent_of(k)}
    if f == "iptv":
        srcs |= {"iptv", "dvbi"}
    tag = old.get("list_tag")
    srt = old.get("list_sort") if old.get("list_sort") in _SORTS else "number"
    value = old.get("list_sort_value") if isinstance(old.get("list_sort_value"), str) else ""
    st = State(frozenset(srcs), frozenset(ars), f if f in ("fta", "enc") else "all",
               frozenset({"UHD", "HD"}) if f == "hd" else frozenset(),
               frozenset({tag}) if isinstance(tag, str) and tag else frozenset(), srt, value)
    for mode in MODES:
        save(settings, mode, replace(st, qualities=frozenset()) if mode == "radio" else st)


def _in_old_group(c, group):
    """Whether *c* was in the old group dropdown's entry *group*: a source
    ("src:sat", "src:sat:<DiSEqC port>", "src:iptv" …) or a group name."""
    if not group.startswith("src:"):
        return c.root == group
    kind, _sep, port = group[4:].partition(":")
    return chansort.source_kind(c) == kind and (not port or c.diseqc == int(port))
