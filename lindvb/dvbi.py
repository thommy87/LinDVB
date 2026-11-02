"""DVB-I service discovery (ETSI TS 103 770) following the German DTVP profile.

DVB-I describes TV and radio services in an XML *service list* that is found
through a *service list registry*. A service can be delivered in several ways
(service instances): over the air (DVB-T2), cable, satellite or the internet
(DVB-DASH, Icecast radio). The receiver picks the best instance it can play.

This module is the part that runs without GTK:

* the registry query and its answer (the list offerings, the regulator's
  default list first)
* parsing service lists: services, instances, names, logos, regions with
  postcodes, LCN tables with LCN ranges
* choosing the region for a postcode and building the channel line-up in LCN
  order for that region
* turning services into LinDVB channels: a broadcast instance is matched to a
  channel from the user's own channel search (DVB triplet with wildcards), an
  internet instance becomes an internet channel
* logo download with a cache, and the service list refresh with
  If-Modified-Since (at least every 24 hours, the last list stays in use when
  the download fails)

The German profile is "DTVP DVB-I Implementierungsprofil V1.1.0" (2026), on
top of TS 103 770 V1.2.1 profile IP2. Requirement numbers (IT-n) in the
comments refer to it.
"""

import base64
import email.utils
import hashlib
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from .channels import Channel

# National service list registry (IT-7). The DVB subdomain is tried first
# when it exists; the DTVP registry is the fallback.
REGISTRIES = ("https://deu.csr.dvbservices.org/query", "https://csr.dvb-i-slr.de/query")
TARGET_COUNTRY = "DEU"
# Delivery types LinDVB can receive (registry query parameter values).
DELIVERY = ("dvb-dash", "dvb-t", "dvb-c", "dvb-s")

USER_AGENT = "LinDVB DVB-I"
UPDATE_INTERVAL = 24 * 3600          # IT-18: check at least every 24 hours

HOW_SERVICE_LOGO = ":1001.2"         # urn:dvb:metadata:cs:HowRelatedCS:20xx:1001.2
HOW_LIST_LOGO = ":1001.1"
HOW_OUT_OF_SERVICE = ":1000.1"

PRIVACY_NOTICE = (
    "DVB-I loads the channel list from the internet. To do so, LinDVB contacts the "
    "German service list registry (csr.dvb-i-slr.de), the server of the service list "
    "you choose and the servers that host the channel logos. These servers see your "
    "IP address and the time of the request. If you enter a postcode, it is sent to "
    "the service list server so that it can return the channels of your region. "
    "LinDVB sends no other personal data and keeps the list and logos only on this "
    "computer. Internet channels are streamed from the broadcasters' servers, which "
    "may log your requests according to their own privacy policies.")


class DVBIError(Exception):
    """A registry or service list request failed. ``code`` is the DVB-I status
    (e.g. ERROR_INVALID_POSTCODE) when the server sent one, else ""."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- XML helpers

def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _kids(el, name):
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _kid(el, name):
    for c in el if el is not None else ():
        if _local(c.tag) == name:
            return c
    return None


def _all(el, name):
    return [c for c in el.iter() if _local(c.tag) == name] if el is not None else []


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def _lang(el):
    return el.get(XML_LANG, "").lower() if el is not None else ""


def pick_text(elements, languages=("de", "deu", "ger", "en", "eng")):
    """Text of the element in the first preferred language, else the first one."""
    elements = [e for e in elements if _text(e)]
    if not elements:
        return ""
    for lang in languages:
        for e in elements:
            if _lang(e) == lang or _lang(e).split("-")[0] == lang:
                return _text(e)
    for e in elements:                    # no xml:lang: inherits the document's
        if not _lang(e):
            return _text(e)
    return _text(elements[0])


def _bool(value, default):
    if value is None:
        return default
    return value.strip().lower() in ("true", "1")


def _int(value, default=None):
    if value in (None, ""):
        return default
    s = str(value).strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        return default


XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _inherit_lang(el, lang):
    """Copy xml:lang down the tree, so each text element knows its language."""
    lang = el.get(XML_LANG) or lang
    if lang and not el.get(XML_LANG):
        el.set(XML_LANG, lang)
    for c in el:
        _inherit_lang(c, lang)


def _parse(data):
    try:
        root = ET.fromstring(data)
        _inherit_lang(root, "")
        return root
    except ET.ParseError as e:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        code = error_code(text)
        if code:
            raise DVBIError(code.replace("_", " ").title(), code) from None
        raise DVBIError(f"Invalid XML: {e}") from None


def error_code(text):
    """The DVB-I error status (ERROR_...) in a server answer, or ""."""
    m = re.search(r"\bERROR_[A-Z_]+\b", text or "")
    return m.group(0) if m else ""


# ---------------------------------------------------------------- registry

@dataclass
class ListOffering:
    name: str
    url: str
    provider: str = ""
    default: bool = False        # the regulator's default list (shown first, IT-8)
    official: bool = False       # registry, provider or list flagged by the regulator
    delivery: list = field(default_factory=list)
    postcode: bool = False       # server-side region selection by postcode
    list_id: str = ""
    logo: str = ""


def registry_query_url(base, country=TARGET_COUNTRY, delivery=DELIVERY):
    """Query URL for the registry (IT-7, IT-8: TargetCountry=DEU, never
    regulatorListFlag=false)."""
    params = [("TargetCountry", country)] + [("Delivery[]", d) for d in delivery]
    sep = "&" if "?" in base else "?"
    return base + sep + urllib.parse.urlencode(params)


_DELIVERY_NAMES = {"DASHDelivery": "dvb-dash", "DVBTDelivery": "dvb-t", "DVBCDelivery": "dvb-c",
                   "DVBSDelivery": "dvb-s", "ApplicationDelivery": "application",
                   "MulticastTSDelivery": "dvb-iptv", "RTSPDelivery": "dvb-iptv",
                   "OtherDelivery": "other"}


def _media_uris(related, how_suffix):
    out = []
    for rm in related:
        how = _kid(rm, "HowRelated")
        if how is None or not (how.get("href") or "").endswith(how_suffix):
            continue
        for uri in _all(rm, "MediaUri"):
            if _text(uri):
                out.append(_text(uri))
    return out


def parse_registry(data):
    """List offerings from a ServiceListEntryPoints document, the regulator's
    default list(s) first, the others in the order of the document (IT-8)."""
    root = _parse(data)
    if _local(root.tag) != "ServiceListEntryPoints":
        raise DVBIError(f"Not a service list registry answer ({_local(root.tag)})")
    entity = _kid(root, "ServiceListRegistryEntity")
    entity_flag = _bool(entity.get("regulatorFlag") if entity is not None else None, False)
    offerings = []
    for po in _kids(root, "ProviderOffering"):
        prov = _kid(po, "Provider")
        prov_name = pick_text(_kids(prov, "Name")) if prov is not None else ""
        prov_flag = _bool(prov.get("regulatorFlag") if prov is not None else None, False)
        for slo in _kids(po, "ServiceListOffering"):
            uri_el = _kid(slo, "ServiceListURI")
            url = _text(_kid(uri_el, "URI")) if uri_el is not None else ""
            if not url:
                continue
            list_flag = _bool(slo.get("regulatorListFlag"), False)
            delivery_el = _kid(slo, "Delivery")
            delivery = [_DELIVERY_NAMES.get(_local(d.tag), _local(d.tag))
                        for d in (delivery_el if delivery_el is not None else [])]
            srs = _kid(slo, "SRSSupport")
            logos = _media_uris(_kids(slo, "RelatedMaterial"), HOW_LIST_LOGO)
            offerings.append(ListOffering(
                name=pick_text(_kids(slo, "ServiceListName")) or prov_name or url,
                url=url, provider=prov_name, default=list_flag,
                official=list_flag or prov_flag or entity_flag, delivery=delivery,
                postcode=_bool(srs.get("postcode") if srs is not None else None, False),
                list_id=_text(_kid(slo, "ServiceListId")), logo=logos[0] if logos else ""))
    # IT-8: no list carries regulatorListFlag -> a list from a regulator
    # provider is the default
    if not any(o.default for o in offerings):
        for o in offerings:
            if o.official:
                o.default = True
                break
    return [o for o in offerings if o.default] + [o for o in offerings if not o.default]


# ---------------------------------------------------------------- service lists

@dataclass
class Instance:
    kind: str                    # dvb-t, dvb-c, dvb-s, dash, icecast, other
    priority: int = 0
    url: str = ""
    content_type: str = ""
    sid: int = None
    tsid: int = None
    onid: int = None
    network_ids: list = field(default_factory=list)
    orbital: str = ""
    frequency: int = 0
    polarization: str = ""
    symbol_rate: int = 0
    modulation_system: str = ""
    drm: list = field(default_factory=list)      # DRMSystemId values
    ca: list = field(default_factory=list)       # CASystemId values
    availability: bool = False                   # has an availability window
    display_name: str = ""


@dataclass
class Service:
    uid: str
    name: str
    provider: str = ""
    radio: bool = False
    instances: list = field(default_factory=list)
    target_regions: list = field(default_factory=list)
    logos: list = field(default_factory=list)
    alt_names: list = field(default_factory=list)
    cg_service_ref: str = ""
    cg_source_ref: str = ""
    minimum_age: int = 0


@dataclass
class Region:
    id: str
    name: str
    selectable: bool = True
    postcodes: list = field(default_factory=list)
    ranges: list = field(default_factory=list)      # (from, to) strings
    wildcards: list = field(default_factory=list)
    parent: str = ""
    depth: int = 0


@dataclass
class LCNEntry:
    number: int
    ref: str
    selectable: bool = True
    visible: bool = True


@dataclass
class LCNTable:
    regions: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    ranges: list = field(default_factory=list)       # (start, end, fill_gaps)


@dataclass
class ServiceList:
    name: str = ""
    provider: str = ""
    version: int = 0
    services: list = field(default_factory=list)
    regions: list = field(default_factory=list)      # all regions, depth first
    lcn_tables: list = field(default_factory=list)
    content_guides: dict = field(default_factory=dict)   # CGSID -> {schedule, program, ...}

    def region(self, rid):
        return next((r for r in self.regions if r.id == rid), None)

    def ancestors(self, rid):
        """The region and its parents, the region first."""
        out = []
        r = self.region(rid)
        while r is not None and r.id not in out:
            out.append(r.id)
            r = self.region(r.parent) if r.parent else None
        return out


def _instance(el):
    inst = Instance(kind="other", priority=_int(el.get("priority"), 1000))
    inst.display_name = pick_text(_kids(el, "DisplayName"))
    for cp in _kids(el, "ContentProtection"):
        inst.drm += [_text(x) for x in _all(cp, "DRMSystemId") if _text(x)]
        inst.ca += [_text(x) for x in _all(cp, "CASystemId") if _text(x)]
    inst.availability = _kid(el, "Availability") is not None
    for child in el:
        name = _local(child.tag)
        if name in ("DVBTDeliveryParameters", "DVBCDeliveryParameters", "DVBSDeliveryParameters"):
            inst.kind = {"DVBT": "dvb-t", "DVBC": "dvb-c", "DVBS": "dvb-s"}[name[:4]]
            trip = _kid(child, "DVBTriplet")
            if trip is not None:
                inst.sid = _int(trip.get("serviceId"))
                inst.tsid = _int(trip.get("tsId"))
                inst.onid = _int(trip.get("origNetId"))
            inst.network_ids = [_int(_text(n)) for n in _kids(child, "NetworkID")
                                if _int(_text(n)) is not None]
            if inst.kind == "dvb-s":
                inst.orbital = _text(_kid(child, "OrbitalPosition"))
                inst.frequency = _int(_text(_kid(child, "Frequency")), 0)
                inst.polarization = _text(_kid(child, "Polarization"))
                inst.symbol_rate = _int(_text(_kid(child, "SymbolRate")), 0)
                inst.modulation_system = _text(_kid(child, "ModulationSystem"))
        elif name == "DASHDeliveryParameters":
            inst.kind = "dash"
            loc = _kid(child, "UriBasedLocation")
            inst.url = _text(_kid(loc, "URI")) if loc is not None else ""
            inst.content_type = loc.get("contentType", "") if loc is not None else ""
        elif name in ("IdentifierBasedDeliveryParameters", "OtherDeliveryParameters"):
            # Icecast radio (IT-3, IT-6): the stream URL with an audio content type
            uri = _text(child) if name == "IdentifierBasedDeliveryParameters" else \
                _text(next(iter(_all(child, "URI")), None))
            ctype = child.get("contentType", "")
            if uri:
                inst.url = uri
                inst.content_type = ctype
                inst.kind = "icecast" if ctype.startswith("audio/") or "icecast" in ctype \
                    else "other"
    return inst


def _regions(el, parent="", depth=0, out=None):
    out = [] if out is None else out
    for r in _kids(el, "Region"):
        reg = Region(id=r.get("regionID", ""), name=pick_text(_kids(r, "RegionName")),
                     selectable=_bool(r.get("selectable"), True), parent=parent, depth=depth)
        reg.postcodes = [_text(p) for p in _kids(r, "Postcode") if _text(p)]
        reg.ranges = [(p.get("from", ""), p.get("to", "")) for p in _kids(r, "PostcodeRange")]
        reg.wildcards = [_text(p) for p in _kids(r, "WildcardPostcode") if _text(p)]
        reg.name = reg.name or reg.id
        out.append(reg)
        _regions(r, reg.id, depth + 1, out)
    return out


def _is_radio(service_el):
    st = _kid(service_el, "ServiceType")
    href = (st.get("href") or "") if st is not None else ""
    return "radio" in href.lower()


def parse_service_list(data):
    root = _parse(data)
    if _local(root.tag) != "ServiceList":
        raise DVBIError(f"Not a DVB-I service list ({_local(root.tag)})")
    sl = ServiceList(name=pick_text(_kids(root, "Name")),
                     provider=pick_text(_kids(root, "ProviderName")),
                     version=_int(root.get("version"), 0))
    rl = _kid(root, "RegionList")
    if rl is not None:
        sl.regions = _regions(rl)
    lcnl = _kid(root, "LCNTableList")
    for t in _kids(lcnl, "LCNTable"):
        tab = LCNTable(regions=[_text(r) for r in _kids(t, "TargetRegion") if _text(r)])
        for e in _kids(t, "LCN"):
            num = _int(e.get("channelNumber"))
            if num is None or not e.get("serviceRef"):
                continue
            tab.entries.append(LCNEntry(num, e.get("serviceRef"),
                                        _bool(e.get("selectable"), True),
                                        _bool(e.get("visible"), True)))
        for r in _kids(t, "LCNRange"):
            start = _int(r.get("start"))
            if start is not None:
                tab.ranges.append((start, _int(r.get("end"), start + 9999),
                                   r.get("fillMethod") == "fillGaps"))
        sl.lcn_tables.append(tab)
    for cg in _all(root, "ContentGuideSource"):
        cgsid = cg.get("CGSID", "")
        sl.content_guides[cgsid] = {
            key: _text(next(iter(_all(_kid(cg, el), "URI")), None))
            for key, el in (("schedule", "ScheduleInfoEndpoint"), ("program", "ProgramInfoEndpoint"),
                            ("group", "GroupInfoEndpoint"), ("episodes", "MoreEpisodesEndpoint"))
            if _kid(cg, el) is not None}
    for s in _kids(root, "Service"):
        uid = _text(_kid(s, "UniqueIdentifier"))
        if not uid:
            continue
        svc = Service(uid=uid, name=pick_text(_kids(s, "ServiceName")) or uid,
                      provider=pick_text(_kids(s, "ProviderName")), radio=_is_radio(s))
        svc.instances = [_instance(i) for i in _kids(s, "ServiceInstance")]
        svc.instances.sort(key=lambda i: i.priority)          # IT-10: 1 = best
        svc.target_regions = [_text(r) for r in _kids(s, "TargetRegion") if _text(r)]
        svc.logos = _media_uris(_kids(s, "RelatedMaterial"), HOW_SERVICE_LOGO)
        for inst in _kids(s, "ServiceInstance"):
            svc.alt_names += [_text(a) for a in _kids(inst, "AltServiceName") if _text(a)]
        svc.cg_service_ref = _text(_kid(s, "ContentGuideServiceRef"))
        ref = _kid(s, "ContentGuideSourceRef")
        own = _kid(s, "ContentGuideSource")
        svc.cg_source_ref = _text(ref) if ref is not None else \
            (own.get("CGSID", "") if own is not None else "")
        pr = _kid(s, "ParentalRating")
        if pr is not None:
            svc.minimum_age = _int(_text(next(iter(_all(pr, "MinimumAge")), None)), 0) or 0
        sl.services.append(svc)
    return sl


# ---------------------------------------------------------------- regions and postcodes

def valid_postcode(text):
    """German postcode: five digits."""
    return bool(re.fullmatch(r"\d{5}", (text or "").strip()))


def _wildcard(pattern, postcode):
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.fullmatch(rx, postcode) is not None


def region_matches(region, postcode):
    pc = postcode.strip()
    if pc in region.postcodes:
        return True
    for lo, hi in region.ranges:
        if lo and hi and len(lo) == len(pc) == len(hi) and lo <= pc <= hi:
            return True
    return any(_wildcard(w, pc) for w in region.wildcards)


def regions_for_postcode(sl, postcode):
    """Selectable regions that contain *postcode*, most specific first; a
    region is left out when one of its sub-regions matches. When a region
    matches but cannot be selected, its parent is returned."""
    hits = [r for r in sl.regions if region_matches(r, postcode)]
    hits = [r for r in hits if r.selectable] or \
        [sl.region(r.parent) for r in hits if r.parent and sl.region(r.parent)]
    # a region whose sub-region matches too is not a choice of its own
    inner = set()
    for r in hits:
        if r:
            inner.update(sl.ancestors(r.id)[1:])
    seen, out = set(), []
    for r in sorted(hits, key=lambda r: -r.depth):
        if r and r.id not in seen and r.id not in inner:
            seen.add(r.id)
            out.append(r)
    return out


def selectable_regions(sl):
    return [r for r in sl.regions if r.selectable]


# ---------------------------------------------------------------- line-up

def lcn_table_for(sl, region_id):
    """LCN table for the region: the nearest one by region hierarchy, else the
    table without a target region, else the first table."""
    if not sl.lcn_tables:
        return None
    for rid in sl.ancestors(region_id) if region_id else []:
        for t in sl.lcn_tables:
            if rid in t.regions:
                return t
    return next((t for t in sl.lcn_tables if not t.regions), sl.lcn_tables[0])


@dataclass
class LineupEntry:
    service: Service
    lcn: int
    selectable: bool = True
    visible: bool = True


def lineup(sl, region_id=None):
    """Services of the region in LCN order (IT-5, IT-9). Services the LCN table
    does not number get numbers from its LCN ranges, else after the highest
    LCN. Service prominence is not used for the order."""
    area = set(sl.ancestors(region_id)) if region_id else set()
    services = [s for s in sl.services
                if not s.target_regions or not region_id or area & set(s.target_regions)]
    table = lcn_table_for(sl, region_id)
    by_ref = {}
    if table:
        for e in table.entries:
            by_ref.setdefault(e.ref, e)
    used = {e.number for e in by_ref.values()}
    out, unnumbered = [], []
    for s in services:
        e = by_ref.get(s.uid)
        if e:
            out.append(LineupEntry(s, e.number, e.selectable, e.visible))
        else:
            unnumbered.append(s)

    def free_numbers():
        for start, end, fill_gaps in (table.ranges if table else []):
            first = start if fill_gaps else max([start - 1] + [n for n in used
                                                              if start <= n <= end]) + 1
            for n in range(first, end + 1):
                if n not in used:
                    yield n
        n = max(used, default=0) + 1
        while True:
            if n not in used:
                yield n
            n += 1

    numbers = free_numbers()
    for s in unnumbered:
        n = next(numbers)
        used.add(n)
        out.append(LineupEntry(s, n))
    out.sort(key=lambda e: e.lcn)
    return out


# ---------------------------------------------------------------- instances -> channels

def _family(delsys):
    if delsys.startswith("dvb-t"):
        return "dvb-t"
    if delsys.startswith("dvb-c"):
        return "dvb-c"
    if delsys in ("dvb-s", "dvb-s2", "turbo"):
        return "dvb-s"
    return ""


def triplet_matches(inst, ch):
    """IT-11/IT-12: serviceId must match; tsId and origNetId only when the
    service list gives them (wildcard triplet) and the channel knows them."""
    if inst.sid is None or ch.sid != inst.sid or _family(ch.delsys) != inst.kind:
        return False
    if inst.onid is not None and ch.onid and ch.onid != inst.onid:
        return False
    if inst.tsid is not None and ch.tsid and ch.tsid != inst.tsid:
        return False
    return True


def _match_score(inst, ch):
    score = 0
    if inst.onid is not None and ch.onid == inst.onid:
        score += 2
    if inst.tsid is not None and ch.tsid == inst.tsid:
        score += 2
    if inst.kind == "dvb-s" and inst.frequency and ch.frequency:
        score += abs(ch.frequency - inst.frequency) < 5000
    return score


def find_broadcast(inst, scanned):
    hits = [c for c in scanned if triplet_matches(inst, c)]
    if not hits:
        return None
    return max(hits, key=lambda c: _match_score(inst, c))


def instance_info(inst):
    d = asdict(inst)
    return {k: v for k, v in d.items() if v not in (None, "", [], 0, False) or k == "kind"}


def resolve(service, scanned):
    """The channel for the best playable instance in priority order (IT-10), or
    (None, reason). A broadcast instance needs a matching channel from the
    channel search; DRM-protected internet instances cannot be played."""
    reasons = []
    for inst in service.instances:
        if inst.kind in ("dvb-t", "dvb-c", "dvb-s"):
            ch = find_broadcast(inst, scanned)
            if ch is None:
                reasons.append(f"{inst.kind.upper()} not in the channel list")
                continue
            c = Channel.from_dict(asdict(ch))
            c.favourite = False
            c.xmltv_id = ""
            c.xmltv_manual = False
            return c, inst
        if inst.kind in ("dash", "icecast") and inst.url:
            if inst.drm:
                reasons.append("internet stream with DRM")
                continue
            return Channel(name=service.name, delsys="iptv", url=inst.url,
                           radio=service.radio or inst.kind == "icecast"), inst
    return None, "; ".join(reasons) or "no playable instance"


def pick_logo(urls, dark=True):
    """IT-6: lists carry 16:9 colour, light and dark variants. On LinDVB's dark
    channel list the variant for dark backgrounds is preferred."""
    if not urls:
        return ""
    want, avoid = ("dark", "light") if dark else ("light", "dark")
    for u in urls:
        if want in u.lower() and avoid not in u.lower():
            return u
    for u in urls:
        if avoid not in u.lower():
            return u
    return urls[0]


@dataclass
class BuildResult:
    channels: list = field(default_factory=list)
    broadcast: int = 0
    internet: int = 0
    missing: list = field(default_factory=list)    # (service name, reason)


def build_channels(sl, region_id, scanned, list_name=""):
    """LinDVB channels for the line-up of *region_id*. *scanned* are the
    channels of the user's own channel search (DVB-T/C/S)."""
    scanned = [c for c in scanned if not c.is_iptv and not c.dvbi_id]
    group = list_name or sl.name or "DVB-I"
    res = BuildResult()
    for entry in lineup(sl, region_id):
        svc = entry.service
        ch, inst = resolve(svc, scanned)
        if ch is None:
            res.missing.append((svc.name, inst))
            continue
        ch.name = svc.name
        ch.root = group
        ch.category = svc.provider or ch.category
        if svc.radio:
            ch.radio = True
        ch.dvbi_id = svc.uid
        ch.lcn = entry.lcn
        ch.number = entry.lcn
        ch.selectable = entry.selectable
        ch.visible = entry.visible
        ch.logo_url = pick_logo(svc.logos)
        ch.dvbi_instances = [instance_info(i) for i in svc.instances]
        ch.dvbi_alt_names = list(svc.alt_names)
        ch.dvbi_cg = svc.cg_service_ref
        if ch.is_iptv:
            res.internet += 1
        else:
            res.broadcast += 1
        res.channels.append(ch)
    return res


# ---------------------------------------------------------------- network

def with_postcode(url, postcode):
    """IT-5: server-side region selection, <ServiceList_URL>?postcode=<postcode>."""
    if not postcode:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode({"postcode": postcode.strip()})


@dataclass
class Response:
    status: int
    data: bytes = b""
    last_modified: str = ""
    etag: str = ""


def fetch(url, if_modified_since="", etag="", timeout=20):
    """GET *url*. Returns a Response; 304 when unchanged. Raises DVBIError."""
    if url.startswith("data:"):
        return Response(200, decode_data_uri(url))
    headers = {"User-Agent": USER_AGENT}
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(),
                            resp.headers.get("Last-Modified", ""), resp.headers.get("ETag", ""))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Response(304)
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        code = error_code(body)
        raise DVBIError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}"
                        + (f" ({code})" if code else ""), code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        raise DVBIError(f"{urllib.parse.urlsplit(url).netloc or url}: {reason}") from None


def decode_data_uri(uri):
    head, _, payload = uri.partition(",")
    if head.endswith(";base64"):
        return base64.b64decode(payload)
    return urllib.parse.unquote_to_bytes(payload)


def query_registry(registries=REGISTRIES, fetcher=fetch):
    """Ask the registries in order; the first that answers wins (IT-7)."""
    errors = []
    for base in registries:
        try:
            resp = fetcher(registry_query_url(base))
            return parse_registry(resp.data), base
        except DVBIError as e:
            errors.append(str(e))
    raise DVBIError("No service list registry could be reached: " + "; ".join(errors))


def load_service_list(url, postcode="", last_modified="", etag="", fetcher=fetch):
    """Download a service list. Returns (ServiceList or None when unchanged,
    Response). With a postcode the server selects the region (IT-5); a
    rejected postcode raises DVBIError with code ERROR_INVALID_POSTCODE."""
    resp = fetcher(with_postcode(url, postcode), if_modified_since=last_modified, etag=etag)
    if resp.status == 304:
        return None, resp
    text_head = resp.data[:4096].decode("utf-8", "replace")
    if "<" not in text_head:
        code = error_code(text_head)
        raise DVBIError(code.replace("_", " ").title() if code else "Empty answer", code)
    return parse_service_list(resp.data), resp


def logo_file(folder, url):
    ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    if url.startswith("data:"):
        ext = "." + url[5:].split(";", 1)[0].split("/")[-1].replace("jpeg", "jpg").replace("svg+xml", "svg")
    if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"):
        ext = ".png"
    return os.path.join(folder, hashlib.sha1(url.encode()).hexdigest()[:20] + ext)


def download_logos(channels, folder, fetcher=fetch, max_age=7 * 24 * 3600):
    """Fetch the logos into *folder* (a cache keyed by URL) and set
    ``channel.logo`` to the file. Returns the number of logos downloaded."""
    os.makedirs(folder, exist_ok=True)
    count = 0
    done = {}
    for c in channels:
        url = c.logo_url
        if not url:
            c.logo = ""
            continue
        path = done.get(url) or logo_file(folder, url)
        fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age
        if url not in done and not fresh:
            try:
                data = fetcher(url).data
                if data:
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
                    count += 1
            except DVBIError:
                pass
        done[url] = path
        c.logo = path if os.path.exists(path) else ""
    return count


def update_due(settings, now=None):
    now = time.time() if now is None else now
    return bool(settings.get("dvbi_list_url")) and \
        now - float(settings.get("dvbi_last_check") or 0) >= UPDATE_INTERVAL


def http_date(ts):
    return email.utils.formatdate(ts, usegmt=True)


# ---------------------------------------------------------------- refresh

@dataclass
class UpdateResult:
    changed: bool = False                # a new list was loaded (or rebuilt)
    build: BuildResult = None
    service_list: ServiceList = None
    region: str = ""
    regions: list = field(default_factory=list)   # candidates when ambiguous
    last_modified: str = ""
    etag: str = ""
    error: str = ""
    error_code: str = ""


class Updater:
    """Loads the chosen service list and builds the channels. ``run`` does the
    network work and may be called from a thread; ``apply`` changes the
    channel list and the settings and belongs on the main thread."""

    def __init__(self, settings, cache_dir, logo_dir, fetcher=fetch):
        self.settings = settings
        self.cache_dir = str(cache_dir)
        self.logo_dir = str(logo_dir)
        self.fetcher = fetcher

    @property
    def cache_file(self):
        return os.path.join(self.cache_dir, "servicelist.xml")

    def _cached(self):
        try:
            with open(self.cache_file, "rb") as f:
                return parse_service_list(f.read())
        except (OSError, DVBIError):
            return None

    def choose_region(self, sl, postcode, stored=""):
        """(region id, candidates). A stored choice wins while the list still
        has it; otherwise the postcode decides (IT-5)."""
        if stored and sl.region(stored):
            return stored, []
        if not sl.regions:
            return "", []
        cands = regions_for_postcode(sl, postcode) if postcode else []
        if len(cands) == 1:
            return cands[0].id, []
        return (cands[0].id if cands else ""), cands

    def run(self, scanned, force=False, region=None, logos=True):
        s = self.settings
        res = UpdateResult()
        url = s.get("dvbi_list_url")
        if not url:
            res.error = "No DVB-I service list chosen"
            return res
        postcode = s.get("dvbi_postcode", "")
        first = not s.get("dvbi_last_modified") and not s.get("dvbi_etag")
        sl = None
        try:
            # IT-18: If-Modified-Since, except on the first (postcode) request
            sl, resp = load_service_list(
                url, postcode,
                last_modified="" if force or first else s.get("dvbi_last_modified", ""),
                etag="" if force or first else s.get("dvbi_etag", ""),
                fetcher=self.fetcher)
            res.last_modified, res.etag = resp.last_modified, resp.etag
            if sl is not None:
                os.makedirs(self.cache_dir, exist_ok=True)
                tmp = self.cache_file + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(resp.data)
                os.replace(tmp, self.cache_file)
        except DVBIError as e:
            # IT-18: keep using the last list when the download fails
            res.error, res.error_code = str(e), e.code
            return res
        if sl is None:                   # 304: unchanged
            if not force and region is None:
                return res
            sl = self._cached()
            if sl is None:
                res.error = "The service list is unchanged but no copy is cached"
                return res
        rid, cands = self.choose_region(sl, postcode,
                                        region if region is not None else s.get("dvbi_region", ""))
        res.region, res.regions, res.service_list = rid, cands, sl
        res.build = build_channels(sl, rid, scanned, s.get("dvbi_list_name") or sl.name)
        if logos:
            download_logos(res.build.channels, self.logo_dir, self.fetcher)
        res.changed = True
        return res

    def apply(self, res, db, now=None):
        """Put the result into the channel list. Returns a status message."""
        s = self.settings
        if res.error:
            s["dvbi_last_check"] = now or time.time()   # retry at the next interval
            if res.error_code == "ERROR_INVALID_POSTCODE":
                return "DVB-I: the postcode was rejected, check it in the DVB-I setup"
            return f"DVB-I update failed, the last channel list stays in use ({res.error})"
        s["dvbi_last_check"] = now or time.time()
        if res.last_modified:
            s["dvbi_last_modified"] = res.last_modified
        if res.etag:
            s["dvbi_etag"] = res.etag
        if not res.changed:
            return "DVB-I: the service list is up to date"
        s["dvbi_region"] = res.region
        added, updated, removed = db.replace_dvbi(res.build.channels)
        b = res.build
        msg = (f"DVB-I: {len(b.channels)} channels ({b.broadcast} antenna/cable/satellite, "
               f"{b.internet} internet)")
        if added or removed:
            msg += f", {added} new, {removed} removed"
        if b.missing:
            msg += f"; {len(b.missing)} not receivable (run a channel search for broadcast ones)"
        if res.regions and len(res.regions) > 1:
            msg += "; several regions match the postcode, choose yours in the DVB-I setup"
        return msg
