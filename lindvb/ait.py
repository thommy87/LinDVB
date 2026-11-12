# SPDX-License-Identifier: GPL-2.0-or-later
"""HbbTV application signalling: the Application Information Table (AIT).

ETSI TS 102 809 (signalling) and ETSI TS 102 796 (HbbTV 2.0.4 = v1.7.1):
the PMT announces the AIT on a PID with stream_type 0x05 and an
application_signalling_descriptor (0x6F). The AIT (table_id 0x74) lists the
applications of the service with their control code (AUTOSTART, PRESENT,
KILL ...), where to load them from (HTTP URL or DSM-CC object carousel) and
the HbbTV version they need.
"""

import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field

from .ca import parse_pmt

AIT_STREAM_TYPE = 0x05
APPLICATION_SIGNALLING_DESCRIPTOR = 0x6F
AIT_TABLE_ID = 0x74
HBBTV_APP_TYPE = 0x0010

# application_control_code
AUTOSTART = 0x01
PRESENT = 0x02
DESTROY = 0x03
KILL = 0x04
PREFETCH = 0x05
REMOTE = 0x06
DISABLED = 0x07
PLAYBACK_AUTOSTART = 0x08

# descriptor tags (TS 102 809 table 5.3.6)
APPLICATION_DESCRIPTOR = 0x00
APPLICATION_NAME_DESCRIPTOR = 0x01
TRANSPORT_PROTOCOL_DESCRIPTOR = 0x02
SIMPLE_APPLICATION_LOCATION_DESCRIPTOR = 0x15
APPLICATION_USAGE_DESCRIPTOR = 0x16
SIMPLE_APPLICATION_BOUNDARY_DESCRIPTOR = 0x17
PARENTAL_RATING_DESCRIPTOR = 0x55

# transport protocols
PROTOCOL_OBJECT_CAROUSEL = 0x0001
PROTOCOL_HTTP = 0x0003

# application_usage_descriptor: the digital teletext application (TS 102 796 7.2.3.2)
USAGE_TELETEXT = 0x01

# The HbbTV version this terminal implements: 2.0.4 is TS 102 796 v1.7.1.
TERMINAL_VERSION = (1, 7, 1)

VISIBILITY_NOT_VISIBLE = 0
VISIBILITY_NOT_VISIBLE_TO_USERS = 1
VISIBILITY_VISIBLE = 3


@dataclass
class Transport:
    protocol: int
    label: int
    url_bases: list = field(default_factory=list)      # HTTP: base URLs
    url_extensions: list = field(default_factory=list)
    remote: bool = False                                # carousel on another service
    onid: int = 0
    tsid: int = 0
    sid: int = 0
    component_tag: int = -1                             # carousel component


@dataclass
class AitApp:
    org_id: int
    app_id: int
    control_code: int
    names: dict = field(default_factory=dict)          # ISO 639 language -> name
    profiles: list = field(default_factory=list)       # (profile, (major, minor, micro))
    service_bound: bool = True
    visibility: int = VISIBILITY_VISIBLE
    priority: int = 0
    labels: list = field(default_factory=list)         # transport protocol labels
    transports: list = field(default_factory=list)     # Transport (own descriptors)
    location: str = ""                                  # initial path + query
    boundaries: list = field(default_factory=list)
    usage: int = -1
    parental: list = field(default_factory=list)       # (country, rating)

    @property
    def key(self):
        """Identifies the application across AIT versions and services."""
        return (self.org_id, self.app_id)

    def name(self, languages=()):
        for lang in languages:
            if lang in self.names:
                return self.names[lang]
        return next(iter(self.names.values()), "")

    def supported(self, version=TERMINAL_VERSION):
        """True when this terminal meets one of the app's HbbTV profile versions."""
        if not self.profiles:
            return True
        return any(v <= version for _profile, v in self.profiles)


@dataclass
class Ait:
    app_type: int
    version: int
    test: bool = False
    apps: list = field(default_factory=list)
    transports: list = field(default_factory=list)     # common loop

    def find(self, key):
        return next((a for a in self.apps if a.key == key), None)

    def transport_for(self, app):
        """The Transport an application is loaded with, by its labels: HTTP first."""
        pool = app.transports + self.transports
        wanted = [t for t in pool if not app.labels or t.label in app.labels]
        for proto in (PROTOCOL_HTTP, PROTOCOL_OBJECT_CAROUSEL):
            for t in wanted:
                if t.protocol == proto:
                    return t
        return None

    def url(self, app):
        """Entry URL of an application. HTTP: base URL + location; carousel:
        a dvb:// URL (TS 102 851) the terminal resolves itself; '' if unknown."""
        t = self.transport_for(app)
        if t is None:
            return ""
        if t.protocol == PROTOCOL_HTTP:
            if not t.url_bases:
                return ""
            return join_url(t.url_bases[0], app.location)
        if t.protocol == PROTOCOL_OBJECT_CAROUSEL:
            # dvb://<onid>.<tsid>.<sid>.<component tag in hex>/<path>
            ids = (f"{t.onid:x}.{t.tsid:x}.{t.sid:x}" if t.remote else "current")
            return f"dvb://{ids}.{t.component_tag:x}/{app.location.lstrip('/')}"
        return ""

    def autostart(self, languages=()):
        """The AUTOSTART application to launch on this service, highest priority first."""
        cands = [a for a in self.apps if a.control_code == AUTOSTART and a.supported()
                 and self.url(a)]
        cands.sort(key=lambda a: -a.priority)
        return cands[0] if cands else None

    def teletext(self):
        """The application that replaces teletext (application_usage_descriptor 0x01)."""
        for a in self.apps:
            if a.usage == USAGE_TELETEXT and a.control_code in (AUTOSTART, PRESENT) \
                    and a.supported() and self.url(a):
                return a
        return None


def join_url(base, location):
    if not location:
        return base
    if "://" in location:
        return location
    if base.endswith("/") and location.startswith("/"):
        return base + location[1:]
    if not base.endswith("/") and not location.startswith(("/", "?", "#")):
        return base + "/" + location
    return base + location


# -- XML AIT (TS 102 809 clause 5.4) ---------------------------------------------------

XML_AIT_TYPE = "application/vnd.dvb.ait+xml"
_CONTROL_CODES = {"AUTOSTART": AUTOSTART, "PRESENT": PRESENT, "DESTROY": DESTROY, "KILL": KILL,
                  "PREFETCH": PREFETCH, "REMOTE": REMOTE, "DISABLED": DISABLED,
                  "PLAYBACK_AUTOSTART": PLAYBACK_AUTOSTART}
_VISIBILITY = {"NOT_VISIBLE_ALL": VISIBILITY_NOT_VISIBLE,
               "NOT_VISIBLE_USERS": VISIBILITY_NOT_VISIBLE_TO_USERS,
               "VISIBLE_ALL": VISIBILITY_VISIBLE}
_USAGES = {"urn:dvb:mhp:2009:digitalText": USAGE_TELETEXT}


def _xl(e):
    return e.tag.rsplit("}", 1)[-1]


def _xkid(el, name):
    return next((c for c in el if _xl(c) == name), None)


def _xtext(el):
    return (el.text or "").strip() if el is not None else ""


def _xint(text, default=0):
    try:
        return int(text, 0) if text.lower().startswith("0x") else int(text)
    except (ValueError, AttributeError):
        return default


def parse_xml_ait(data, version=0):
    """An XML AIT (ServiceDiscovery/ApplicationDiscovery, as a DVB-I service
    list links it or a companion POSTs it) as an Ait, or None. Only HbbTV
    applications with an HTTP transport are kept."""
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, TypeError, ValueError):
        return None
    ait = Ait(app_type=HBBTV_APP_TYPE, version=version)
    label = 1
    for el in root.iter():
        if _xl(el) != "Application":
            continue
        ident = _xkid(el, "applicationIdentifier")
        desc = _xkid(el, "applicationDescriptor")
        if desc is None:
            continue
        typ = _xkid(desc, "type")
        kind = _xtext(_xkid(typ, "OtherApp")) if typ is not None else ""
        if kind and "hbbtv" not in kind.lower():
            continue                                 # not an HbbTV application
        app = AitApp(org_id=_xint(_xtext(_xkid(ident, "orgId")) if ident is not None else ""),
                     app_id=_xint(_xtext(_xkid(ident, "appId")) if ident is not None else ""),
                     control_code=_CONTROL_CODES.get(_xtext(_xkid(desc, "controlCode")).upper(),
                                                     PRESENT))
        for n in el:
            if _xl(n) == "appName" and _xtext(n):
                app.names[(n.get("Language") or "und").lower()] = _xtext(n)
        vis = _xtext(_xkid(desc, "visibility")).upper()
        app.visibility = _VISIBILITY.get(vis, VISIBILITY_VISIBLE)
        app.service_bound = _xtext(_xkid(desc, "serviceBound")).lower() not in ("false", "0")
        app.priority = _xint(_xtext(_xkid(desc, "priority")))
        for v in desc:
            if _xl(v) == "mhpVersion":
                app.profiles.append((_xint(_xtext(_xkid(v, "profile"))),
                                     (_xint(_xtext(_xkid(v, "versionMajor"))),
                                      _xint(_xtext(_xkid(v, "versionMinor"))),
                                      _xint(_xtext(_xkid(v, "versionMicro"))))))
            elif _xl(v) == "ParentalRating":
                rating = _xint(_xtext(v), -1)
                if rating >= 0:
                    app.parental.append(((v.get("Region") or "").upper()[:3], rating))
        usage = _xkid(el, "applicationUsageDescriptor")
        if usage is not None:
            app.usage = _USAGES.get(_xtext(_xkid(usage, "ApplicationUsage")), -1)
        boundary = _xkid(el, "applicationBoundary")
        if boundary is not None:
            app.boundaries = [_xtext(b) for b in boundary if _xtext(b)]
        for tr in el:
            if _xl(tr) != "applicationTransport":
                continue
            bases = [_xtext(b) for b in tr if _xl(b) == "URLBase" and _xtext(b)]
            if bases and bases[0].startswith(("http://", "https://")):
                app.transports.append(Transport(
                    PROTOCOL_HTTP, label, url_bases=bases,
                    url_extensions=[_xtext(b) for b in tr if _xl(b) == "URLExtension"]))
                app.labels.append(label)
                label += 1
        app.location = _xtext(_xkid(el, "applicationLocation"))
        if not app.transports and app.location.startswith(("http://", "https://")):
            app.transports.append(Transport(PROTOCOL_HTTP, label, url_bases=[app.location]))
            app.labels.append(label)
            app.location = ""
            label += 1
        if app.transports:
            ait.apps.append(app)
    return ait if ait.apps else None


def ait_for_url(url, control_code=AUTOSTART, service_bound=True):
    """A one-application AIT for an application given by its URL alone (a
    DVB-I linked application of type HTML). The ids come from the URL, so the
    same URL is the same application again."""
    crc = zlib.crc32(url.encode())
    app = AitApp(org_id=0, app_id=(crc & 0x3FFF) | 0x4000, control_code=control_code,
                 service_bound=service_bound, labels=[1],
                 transports=[Transport(PROTOCOL_HTTP, 1, url_bases=[url])])
    return Ait(app_type=HBBTV_APP_TYPE, version=0, apps=[app])


# -- PMT ------------------------------------------------------------------------

def ait_pids_from_pmt(section, app_type=HBBTV_APP_TYPE):
    """(program_number, [AIT PIDs]) for the HbbTV AITs a PMT section announces."""
    pmt = parse_pmt(section)
    pids = []
    for stream_type, pid, _ca, info in pmt.streams:
        if stream_type != AIT_STREAM_TYPE:
            continue
        found, types = False, []
        for tag, body in descriptors(info):
            if tag == APPLICATION_SIGNALLING_DESCRIPTOR:
                found = True
                for k in range(0, len(body) - 2, 3):
                    types.append(((body[k] & 0x7F) << 8) | body[k + 1])
        if found and (not types or app_type in types):
            pids.append(pid)
    return pmt.program_number, pids


def descriptors(loop):
    """(tag, body) of each descriptor in a descriptor loop."""
    i, n = 0, len(loop)
    while i + 2 <= n:
        tag, length = loop[i], loop[i + 1]
        if i + 2 + length > n:
            break
        yield tag, bytes(loop[i + 2:i + 2 + length])
        i += 2 + length


# -- AIT --------------------------------------------------------------------------

def parse_ait_section(section):
    """Parse one AIT section. Returns (Ait, section_number, last_section_number).

    Raises ValueError for anything that is not a well-formed AIT section."""
    if len(section) < 16 or section[0] != AIT_TABLE_ID:
        raise ValueError("not an AIT section")
    length = ((section[1] & 0x0F) << 8) | section[2]
    end = 3 + length
    if end > len(section) or length < 13:
        raise ValueError("bad AIT section length")
    test = bool(section[3] & 0x80)
    app_type = ((section[3] & 0x7F) << 8) | section[4]
    version = (section[5] >> 1) & 0x1F
    number, last = section[6], section[7]
    ait = Ait(app_type, version, test)
    i = 8
    common_len = ((section[i] & 0x0F) << 8) | section[i + 1]
    i += 2
    for tag, body in descriptors(section[i:i + common_len]):
        if tag == TRANSPORT_PROTOCOL_DESCRIPTOR:
            t = _transport(body)
            if t:
                ait.transports.append(t)
    i += common_len
    if i + 2 > end - 4:
        raise ValueError("truncated AIT")
    loop_len = ((section[i] & 0x0F) << 8) | section[i + 1]
    i += 2
    loop_end = min(i + loop_len, end - 4)
    while i + 9 <= loop_end:
        org = int.from_bytes(section[i:i + 4], "big")
        aid = int.from_bytes(section[i + 4:i + 6], "big")
        code = section[i + 6]
        dlen = ((section[i + 7] & 0x0F) << 8) | section[i + 8]
        i += 9
        app = AitApp(org, aid, code)
        for tag, body in descriptors(section[i:i + dlen]):
            _app_descriptor(app, tag, body)
        ait.apps.append(app)
        i += dlen
    return ait, number, last


def _app_descriptor(app, tag, body):
    if tag == APPLICATION_DESCRIPTOR and body:
        plen = body[0]
        for k in range(1, 1 + plen - 4, 5):
            profile = (body[k] << 8) | body[k + 1]
            app.profiles.append((profile, (body[k + 2], body[k + 3], body[k + 4])))
        k = 1 + plen
        if k + 2 <= len(body):
            app.service_bound = bool(body[k] & 0x80)
            app.visibility = (body[k] >> 5) & 0x03
            app.priority = body[k + 1]
            app.labels = list(body[k + 2:])
    elif tag == APPLICATION_NAME_DESCRIPTOR:
        k = 0
        while k + 4 <= len(body):
            lang = body[k:k + 3].decode("latin-1", "replace")
            n = body[k + 3]
            app.names[lang] = dvb_text(body[k + 4:k + 4 + n])
            k += 4 + n
    elif tag == TRANSPORT_PROTOCOL_DESCRIPTOR:
        t = _transport(body)
        if t:
            app.transports.append(t)
    elif tag == SIMPLE_APPLICATION_LOCATION_DESCRIPTOR:
        app.location = body.decode("utf-8", "replace")
    elif tag == SIMPLE_APPLICATION_BOUNDARY_DESCRIPTOR and body:
        k = 1
        for _ in range(body[0]):
            if k >= len(body):
                break
            n = body[k]
            app.boundaries.append(body[k + 1:k + 1 + n].decode("utf-8", "replace"))
            k += 1 + n
    elif tag == APPLICATION_USAGE_DESCRIPTOR and body:
        app.usage = body[0]
    elif tag == PARENTAL_RATING_DESCRIPTOR:
        for k in range(0, len(body) - 3, 4):
            app.parental.append((body[k:k + 3].decode("latin-1", "replace"), body[k + 3]))


def _transport(body):
    if len(body) < 3:
        return None
    t = Transport((body[0] << 8) | body[1], body[2])
    sel = body[3:]
    if t.protocol == PROTOCOL_HTTP:
        k = 0
        while k < len(sel):
            n = sel[k]
            if k + 1 + n + 1 > len(sel):
                break
            t.url_bases.append(sel[k + 1:k + 1 + n].decode("utf-8", "replace"))
            k += 1 + n
            count = sel[k]
            k += 1
            for _ in range(count):
                if k >= len(sel):
                    break
                m = sel[k]
                t.url_extensions.append(sel[k + 1:k + 1 + m].decode("utf-8", "replace"))
                k += 1 + m
    elif t.protocol == PROTOCOL_OBJECT_CAROUSEL and sel:
        t.remote = bool(sel[0] & 0x80)
        k = 1
        if t.remote and len(sel) >= 7:
            t.onid = (sel[1] << 8) | sel[2]
            t.tsid = (sel[3] << 8) | sel[4]
            t.sid = (sel[5] << 8) | sel[6]
            k = 7
        if k < len(sel):
            t.component_tag = sel[k]
    return t


def dvb_text(raw):
    """Text in the DVB character coding (EN 300 468 annex A), enough for names."""
    if not raw:
        return ""
    first = raw[0]
    if first >= 0x20:
        return raw.decode("latin-1", "replace")     # default table (≈ ISO 6937)
    if first == 0x15:
        return raw[1:].decode("utf-8", "replace")
    if first == 0x10 and len(raw) >= 3:
        return raw[3:].decode(f"iso8859-{raw[2]}", "replace") if 1 <= raw[2] <= 15 \
            else raw[3:].decode("latin-1", "replace")
    if 0x01 <= first <= 0x0B:
        try:
            return raw[1:].decode(f"iso8859-{first + 4}", "replace")
        except LookupError:
            pass
    return raw[1:].decode("latin-1", "replace")


class AitCollector:
    """Puts multi-section AITs together. feed() returns a complete Ait when the
    last missing section of a new version arrives, else None."""

    def __init__(self, app_type=HBBTV_APP_TYPE):
        self.app_type = app_type
        self._version = None
        self._parts = {}
        self.current = None

    def feed(self, section):
        try:
            ait, number, last = parse_ait_section(section)
        except ValueError:
            return None
        if ait.app_type != self.app_type:
            return None
        if self.current is not None and ait.version == self.current.version:
            return None                                  # repetition
        if ait.version != self._version:
            self._version, self._parts = ait.version, {}
        self._parts[number] = ait
        if len(self._parts) < last + 1 or any(n not in self._parts for n in range(last + 1)):
            return None
        whole = Ait(ait.app_type, ait.version, ait.test)
        for n in range(last + 1):
            whole.apps += self._parts[n].apps
            whole.transports += self._parts[n].transports
        self.current, self._parts = whole, {}
        return whole


VIDEO_STREAM_TYPES = (0x01, 0x02, 0x10, 0x1B, 0x24, 0x42)


class AitMonitor:
    """Watches a transport stream for the AIT of one service.

    feed_packet() takes TS packets; on_pids(pids) is called when the AIT PIDs
    are known and on_ait(ait) for every new AIT version.

    It also serves DSM-CC (lindvb.dsmcc): ``components`` maps the PMT's
    component tags to (pid, stream_type, carousel_id), on_pmt() announces
    changes of it, and watch(pid, fn) hands every section of *pid* to fn.
    All callbacks run in the thread that feeds packets."""

    def __init__(self, sid, pmt_pid=0, on_ait=None, on_pids=None):
        from .ca import SectionAssembler
        self._asm_cls = SectionAssembler
        self.sid = sid
        self.pmt_pid = pmt_pid
        self.ait_pids = set()
        self.on_ait = on_ait
        self.on_pids = on_pids
        self._pat = SectionAssembler() if not pmt_pid else None
        self._pmt = SectionAssembler()
        self._pmt_version = None
        self._ait_asm = {}
        self.collector = AitCollector()
        self.components = {}
        self.on_pmt = None
        self.video_pid = 0
        self.on_pts = None            # callable(pts in 90 kHz, monotonic ns): broadcast timeline
        self._pts_at = 0
        self._watched = {}            # pid -> (SectionReader, [fn]); replaced, never mutated

    def _pes_pts(self, packet):
        """The PTS of a video PES that starts in *packet*, twice a second at most."""
        import time
        now = time.monotonic_ns()
        if now - self._pts_at < 500_000_000:
            return
        afc = (packet[3] >> 4) & 0x03
        off = 4 + (1 + packet[4] if afc == 3 else 0)
        p = packet[off:]
        if len(p) < 14 or p[0] != 0 or p[1] != 0 or p[2] != 1 or not p[7] & 0x80:
            return
        pts = (((p[9] >> 1) & 0x07) << 30) | (p[10] << 22) | ((p[11] >> 1) << 15) \
            | (p[12] << 7) | (p[13] >> 1)
        self._pts_at = now
        self.on_pts(pts, now)

    def watch(self, pid, fn):
        from .dsmcc import SectionReader
        w = dict(self._watched)
        reader, fns = w.get(pid, (SectionReader(), []))
        w[pid] = (reader, fns + [fn])
        self._watched = w

    def unwatch(self, pid, fn=None):
        w = dict(self._watched)
        if pid in w:
            reader, fns = w[pid]
            fns = [f for f in fns if fn is not None and f is not fn]
            if fns:
                w[pid] = (reader, fns)
            else:
                del w[pid]
        self._watched = w

    def feed_buffer(self, data):
        n = len(data)
        i = data.find(0x47)
        while 0 <= i and i + 188 <= n:
            if data[i] != 0x47:
                i = data.find(0x47, i + 1)
                continue
            self.feed_packet(memoryview(data)[i:i + 188])
            i += 188

    def feed_packet(self, packet):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid == self.video_pid and self.on_pts is not None and packet[1] & 0x40:
            self._pes_pts(packet)
        watched = self._watched.get(pid)
        if watched is not None:
            reader, fns = watched
            for section in reader.feed(packet):
                for fn in fns:
                    fn(pid, section)
        if pid in self._ait_asm:
            for section in self._ait_asm[pid].feed(packet):
                if section and section[0] == AIT_TABLE_ID:
                    ait = self.collector.feed(section)
                    if ait is not None and self.on_ait:
                        self.on_ait(ait)
        elif pid == 0 and self._pat is not None:
            from .ca import pat_first_program, pat_pmt_pid
            for section in self._pat.feed(packet):
                if section and not self.sid:
                    # a stream of one service (Tvheadend): take the service it names
                    self.sid = pat_first_program(section)
                found = pat_pmt_pid(section, self.sid) if section else 0
                if found:
                    self.pmt_pid = found
                    self._pat = None
                    break
        elif pid == self.pmt_pid and pid:
            for section in self._pmt.feed(packet):
                if not section or section[0] != 0x02 or len(section) < 6:
                    continue
                version = (section[5] >> 1) & 0x1F
                if version == self._pmt_version:
                    continue
                try:
                    program, pids = ait_pids_from_pmt(section)
                except ValueError:
                    continue
                if self.sid and program != self.sid:
                    continue
                self._pmt_version = version
                from .dsmcc import components_from_pmt
                pmt = parse_pmt(section)
                self.components = components_from_pmt(pmt)
                self.video_pid = next((p for t, p, _c, _i in pmt.streams if t in VIDEO_STREAM_TYPES), 0)
                if self.on_pmt:
                    self.on_pmt(self.components)
                pids = set(pids)
                if pids != self.ait_pids:
                    self.ait_pids = pids
                    self._ait_asm = {p: self._asm_cls() for p in pids}
                    self.collector = AitCollector()
                    if self.on_pids:
                        self.on_pids(sorted(pids))
