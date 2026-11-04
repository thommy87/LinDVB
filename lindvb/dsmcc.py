"""DSM-CC for HbbTV: object carousels and stream events.

ETSI TR 101 202 / ISO/IEC 13818-6 as profiled by ETSI TS 102 809 and
TS 102 796 (HbbTV 2.0.4):

* **Object carousel**: a file system broadcast in a loop on one or more
  elementary streams (stream_type 0x0B). The DSI (table 0x3B) names the
  service gateway (the root directory), DIIs (0x3B) list the modules, DDBs
  (0x3C) carry the module blocks. A complete module holds BIOP messages:
  directories, files, streams and stream event objects. Applications are
  loaded from it through ``dvb://`` URLs (TS 102 851).
* **Stream events**: stream event descriptors (0x1A) in DSM-CC sections with
  table_id 0x3D on the stream a stream event object (or an XML event
  description) points to. Applications subscribe with
  ``video/broadcast.addStreamEventListener()``.

Pure Python and without GLib: the capture feeds sections from its streaming
thread; lookups come from the main loop and take a lock.

``CarouselWriter`` builds carousels and stream event sections for tests and
for tools/hbbtv-test-ts.py.
"""

import struct
import threading
import zlib
from dataclasses import dataclass, field

DSMCC_UN_MESSAGES = 0x3B           # DSI, DII
DSMCC_DDB = 0x3C
DSMCC_STREAM_DESCRIPTORS = 0x3D

MSG_DII = 0x1002
MSG_DDB = 0x1003
MSG_DSI = 0x1006

TAG_OBJECT_LOCATION = 0x49534F50
TAG_CONN_BINDER = 0x49534F40
TAG_BIOP_PROFILE = 0x49534F06

BIOP_DELIVERY_PARA_USE = 0x0016
BIOP_OBJECT_USE = 0x0017
STR_NPT_USE = 0x000B
STR_STATUS_AND_EVENT_USE = 0x000C
STR_EVENT_USE = 0x000D

STREAM_EVENT_DESCRIPTOR = 0x1A
COMPRESSED_MODULE_DESCRIPTOR = 0x09

CAROUSEL_STREAM_TYPES = (0x0B, 0x0C, 0x0D)
STREAM_IDENTIFIER_DESCRIPTOR = 0x52
CAROUSEL_IDENTIFIER_DESCRIPTOR = 0x13
ASSOCIATION_TAG_DESCRIPTOR = 0x14


# -- CRC ---------------------------------------------------------------------------

def _crc_table():
    table = []
    for n in range(256):
        c = n << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        table.append(c & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def crc32(data):
    """MPEG-2 CRC32 (0 over a whole section including its CRC)."""
    crc = 0xFFFFFFFF
    t = _CRC
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ t[(crc >> 24) ^ b]
    return crc


# -- sections ------------------------------------------------------------------------

class SectionReader:
    """Sections of one PID from TS packets, also when a section ends in the
    packet that starts the next one (pointer_field), which carousels do all
    the time."""

    def __init__(self):
        self._buf = bytearray()
        self._cc = None

    def feed(self, packet):
        afc = (packet[3] >> 4) & 0x03
        if afc in (0, 2):
            return []
        cc = packet[3] & 0x0F
        if self._cc is not None and cc == self._cc:
            return []                                   # duplicate packet
        lost = self._cc is not None and cc != (self._cc + 1) & 0x0F
        self._cc = cc
        off = 4 + (1 + packet[4] if afc == 3 else 0)
        if off >= 188:
            return []
        payload = bytes(packet[off:188])
        out = []
        if packet[1] & 0x40:
            pointer = payload[0]
            if self._buf and not lost:
                self._buf += payload[1:1 + pointer]
                out += self._take()
            self._buf = bytearray(payload[1 + pointer:])
        elif self._buf and not lost:
            self._buf += payload
        else:
            self._buf = bytearray()
            return out
        out += self._take()
        return out

    def _take(self):
        out = []
        while len(self._buf) >= 3 and self._buf[0] != 0xFF:
            total = 3 + (((self._buf[1] & 0x0F) << 8) | self._buf[2])
            if len(self._buf) < total:
                break
            out.append(bytes(self._buf[:total]))
            del self._buf[:total]
        if self._buf[:1] == b"\xff":
            self._buf = bytearray()
        return out


def section_ok(section):
    """Length, and the CRC32 when the section has one (section_syntax_indicator)."""
    if len(section) < 12:
        return False
    if section[1] & 0x80:
        return crc32(section) == 0
    return True


# -- BIOP primitives ---------------------------------------------------------------

@dataclass
class Tap:
    id: int
    use: int
    association_tag: int
    selector: bytes = b""

    @property
    def transaction_id(self):
        if len(self.selector) >= 6:
            return struct.unpack(">I", self.selector[2:6])[0]
        return None


@dataclass
class Ior:
    type_id: str
    carousel_id: int = 0
    module_id: int = 0
    object_key: bytes = b""
    taps: list = field(default_factory=list)


def _taps(b, i, count):
    taps = []
    for _ in range(count):
        tid, use, tag, slen = struct.unpack(">HHHB", b[i:i + 7])
        taps.append(Tap(tid, use, tag, bytes(b[i + 7:i + 7 + slen])))
        i += 7 + slen
    return taps, i


def parse_ior(b, i=0):
    """IOP::IOR at b[i:]. Returns (Ior, end)."""
    n = struct.unpack(">I", b[i:i + 4])[0]
    type_id = bytes(b[i + 4:i + 4 + n]).rstrip(b"\x00").decode("latin-1")
    i += 4 + n + (-n % 4)
    ior = Ior(type_id)
    profiles = struct.unpack(">I", b[i:i + 4])[0]
    i += 4
    for _ in range(profiles):
        tag, length = struct.unpack(">II", b[i:i + 8])
        body = b[i + 8:i + 8 + length]
        i += 8 + length
        if tag != TAG_BIOP_PROFILE or len(body) < 2:
            continue
        k, count = 2, body[1]
        for _ in range(count):
            ctag = struct.unpack(">I", body[k:k + 4])[0]
            clen = body[k + 4]
            comp = body[k + 5:k + 5 + clen]
            k += 5 + clen
            if ctag == TAG_OBJECT_LOCATION and len(comp) >= 9:
                ior.carousel_id, ior.module_id = struct.unpack(">IH", comp[:6])
                klen = comp[8]
                ior.object_key = bytes(comp[9:9 + klen])
            elif ctag == TAG_CONN_BINDER and comp:
                ior.taps, _ = _taps(comp, 1, comp[0])
    return ior, i


# -- the carousel ---------------------------------------------------------------------

@dataclass
class Binding:
    name: str
    kind: str
    ior: Ior


@dataclass
class BiopObject:
    kind: str                        # "fil", "dir", "srg", "str", "ste"
    key: bytes
    content: bytes = b""             # file
    bindings: list = field(default_factory=list)     # dir / srg
    event_names: list = field(default_factory=list)  # ste
    event_ids: list = field(default_factory=list)
    taps: list = field(default_factory=list)          # str / ste

    def event_tag(self):
        """Association tag of the stream with this object's stream event descriptors."""
        for use in (STR_EVENT_USE, STR_STATUS_AND_EVENT_USE):
            for t in self.taps:
                if t.use == use:
                    return t.association_tag
        return self.taps[0].association_tag if self.taps else None


def parse_biop(data):
    """All BIOP messages of a module: [BiopObject]."""
    out, i, n = [], 0, len(data)
    while i + 12 <= n and data[i:i + 4] == b"BIOP":
        size = struct.unpack(">I", data[i + 8:i + 12])[0]
        msg, i = data[i + 12:i + 12 + size], i + 12 + size
        try:
            obj = _biop_message(msg)
        except (struct.error, IndexError, ValueError):
            continue
        if obj is not None:
            out.append(obj)
    return out


def _biop_message(m):
    k = 0
    klen = m[k]
    key = bytes(m[k + 1:k + 1 + klen])
    k += 1 + klen
    kind_len = struct.unpack(">I", m[k:k + 4])[0]
    kind = bytes(m[k + 4:k + 4 + kind_len]).rstrip(b"\x00").decode("latin-1")
    k += 4 + kind_len
    info_len = struct.unpack(">H", m[k:k + 2])[0]
    info = m[k + 2:k + 2 + info_len]
    k += 2 + info_len
    contexts, k = m[k], k + 1                             # service context list
    for _ in range(contexts):
        k += 6 + struct.unpack(">H", m[k + 4:k + 6])[0]
    body_len = struct.unpack(">I", m[k:k + 4])[0]
    body = m[k + 4:k + 4 + body_len]
    obj = BiopObject(kind, key)
    if kind == "fil":
        n = struct.unpack(">I", body[:4])[0]
        obj.content = bytes(body[4:4 + n])
    elif kind in ("dir", "srg"):
        count = struct.unpack(">H", body[:2])[0]
        j = 2
        for _ in range(count):
            names = body[j]
            j += 1
            name = kind_ = ""
            for _ in range(names):
                idl = body[j]
                name = bytes(body[j + 1:j + 1 + idl]).rstrip(b"\x00").decode("utf-8", "replace")
                j += 1 + idl
                kl = body[j]
                kind_ = bytes(body[j + 1:j + 1 + kl]).rstrip(b"\x00").decode("latin-1")
                j += 1 + kl
            j += 1                                        # bindingType
            ior, j = parse_ior(body, j)
            j += 2 + struct.unpack(">H", body[j:j + 2])[0]  # child objectInfo
            obj.bindings.append(Binding(name, kind_, ior))
    elif kind in ("str", "ste"):
        dlen = info[0]
        j = 1 + dlen + 4 + 4 + 3                          # Stream::Info_T
        if kind == "ste" and j + 2 <= len(info):
            count = struct.unpack(">H", info[j:j + 2])[0]
            j += 2
            for _ in range(count):
                nl = info[j]
                obj.event_names.append(bytes(info[j + 1:j + 1 + nl]).rstrip(b"\x00")
                                       .decode("utf-8", "replace"))
                j += 1 + nl
        obj.taps, j = _taps(body, 1, body[0])
        if kind == "ste" and j < len(body):
            count = body[j]
            obj.event_ids = list(struct.unpack(f">{count}H", body[j + 1:j + 1 + 2 * count]))
    return obj


@dataclass
class Module:
    id: int
    size: int
    version: int
    block_size: int
    tags: list = field(default_factory=list)   # association tags of its DDBs
    compressed: bool = False
    blocks: dict = field(default_factory=dict)
    done: bool = False


class ObjectCarousel:
    """One object carousel, put together from its DSM-CC sections.

    feed(section) takes DSI, DII and DDB sections; lookup(path) answers from
    the modules that are complete. on_change() (streaming thread) is called
    whenever a module completes or the carousel changes version."""

    def __init__(self, carousel_id=None, on_change=None):
        self.carousel_id = carousel_id
        self.on_change = on_change
        self.gateway = None                     # Ior of the service gateway
        self.modules = {}                       # module id -> Module
        self.objects = {}                       # (module id, key) -> BiopObject
        self.wanted_tags = set()                # association tags of DII/DDB streams
        self._dsi_tid = None
        self._lock = threading.Lock()

    # -- input (streaming thread) --

    def feed(self, section):
        if not section_ok(section) or len(section) < 20:
            return
        tid = section[0]
        body = section[8:-4] if section[1] & 0x80 else section[8:]
        if len(body) < 12 or body[0] != 0x11 or body[1] != 0x03:
            return
        msg_id, transaction = struct.unpack(">HI", body[2:8])
        adapt = body[9]
        msg = body[12 + adapt:]
        try:
            if tid == DSMCC_UN_MESSAGES and msg_id == MSG_DSI:
                self._dsi(transaction, msg)
            elif tid == DSMCC_UN_MESSAGES and msg_id == MSG_DII:
                self._dii(msg)
            elif tid == DSMCC_DDB and msg_id == MSG_DDB:
                self._ddb(msg)
        except (struct.error, IndexError, ValueError):
            pass

    def _dsi(self, transaction, msg):
        if self._dsi_tid == transaction:
            return
        clen = struct.unpack(">H", msg[20:22])[0]
        k = 22 + clen
        plen = struct.unpack(">H", msg[k:k + 2])[0]
        ior, _ = parse_ior(msg[k + 2:k + 2 + plen])
        if self.carousel_id is not None and ior.carousel_id != self.carousel_id:
            return
        with self._lock:
            changed = self._dsi_tid is not None
            self._dsi_tid = transaction
            self.carousel_id = ior.carousel_id
            self.gateway = ior
            for t in ior.taps:
                if t.use == BIOP_DELIVERY_PARA_USE:
                    self.wanted_tags.add(t.association_tag)
        if changed or self.objects:
            self._changed()

    def _dii(self, msg):
        download_id, block_size = struct.unpack(">IH", msg[:6])
        if self.carousel_id is not None and download_id != self.carousel_id:
            return
        k = 16
        clen = struct.unpack(">H", msg[k:k + 2])[0]
        k += 2 + clen
        count = struct.unpack(">H", msg[k:k + 2])[0]
        k += 2
        changed = False
        with self._lock:
            for _ in range(count):
                mid, size, version, ilen = struct.unpack(">HIBB", msg[k:k + 8])
                info = msg[k + 8:k + 8 + ilen]
                k += 8 + ilen
                old = self.modules.get(mid)
                if old is not None and old.version == version and old.size == size:
                    continue
                mod = Module(mid, size, version, block_size)
                if len(info) >= 13:
                    taps, j = _taps(info, 13, info[12])
                    mod.tags = [t.association_tag for t in taps if t.use == BIOP_OBJECT_USE]
                    self.wanted_tags.update(mod.tags)
                    ulen = info[j] if j < len(info) else 0
                    for tag, d in _descs(info[j + 1:j + 1 + ulen]):
                        if tag == COMPRESSED_MODULE_DESCRIPTOR:
                            mod.compressed = True
                if old is not None:
                    self.objects = {k_: v for k_, v in self.objects.items() if k_[0] != mid}
                    changed = True
                self.modules[mid] = mod
        if changed:
            self._changed()

    def _ddb(self, msg):
        mid, version, _r, block = struct.unpack(">HBBH", msg[:6])
        data = msg[6:]
        with self._lock:
            mod = self.modules.get(mid)
            if mod is None or mod.done or mod.version != version:
                return
            mod.blocks.setdefault(block, bytes(data))
            count = -(-mod.size // mod.block_size) if mod.block_size else 0
            if len(mod.blocks) < count:
                return
            raw = b"".join(mod.blocks[n] for n in range(count) if n in mod.blocks)[:mod.size]
            if len(raw) < mod.size:
                return
            mod.done, mod.blocks = True, {}
            if mod.compressed:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    mod.done = False
                    return
            for obj in parse_biop(raw):
                self.objects[(mid, obj.key)] = obj
        self._changed()

    def _changed(self):
        if self.on_change:
            self.on_change()

    # -- lookups (any thread) --

    @property
    def ready(self):
        return self.gateway is not None and self._object(self.gateway) is not None

    def _object(self, ior):
        return self.objects.get((ior.module_id, ior.object_key))

    def lookup(self, path):
        """The object at *path* ("dir/file.html"), None when it is not (yet)
        there, False when the carousel is complete enough to say it is missing."""
        with self._lock:
            if self.gateway is None:
                return None
            obj = self._object(self.gateway)
            for part in [p for p in path.split("/") if p and p != "."]:
                if obj is None:
                    return None
                if obj.kind not in ("dir", "srg"):
                    return False
                b = next((b for b in obj.bindings if b.name == part), None)
                if b is None:
                    return False
                obj = self._object(b.ior)
            return obj

    def files(self):
        """All complete file paths (for tests and debugging)."""
        out = []

        def walk(obj, prefix):
            for b in obj.bindings:
                child = self._object(b.ior)
                if child is None:
                    continue
                if child.kind in ("dir", "srg"):
                    walk(child, prefix + b.name + "/")
                else:
                    out.append(prefix + b.name)
        with self._lock:
            root = self._object(self.gateway) if self.gateway else None
            if root is not None:
                walk(root, "")
        return sorted(out)


def _descs(loop):
    i = 0
    while i + 2 <= len(loop):
        tag, n = loop[i], loop[i + 1]
        yield tag, bytes(loop[i + 2:i + 2 + n])
        i += 2 + n


# -- stream events -------------------------------------------------------------------

@dataclass
class StreamEvent:
    event_id: int
    npt: int
    data: bytes
    version: int


def parse_stream_events(section):
    """Stream event descriptors of a table 0x3D section: [StreamEvent]."""
    if not section or section[0] != DSMCC_STREAM_DESCRIPTORS or not section_ok(section):
        return []
    version = (section[5] >> 1) & 0x1F
    end = len(section) - 4 if section[1] & 0x80 else len(section)
    out = []
    for tag, body in _descs(section[8:end]):
        if tag == STREAM_EVENT_DESCRIPTOR and len(body) >= 10:
            eid = struct.unpack(">H", body[:2])[0]
            npt = int.from_bytes(body[2:10], "big") & ((1 << 33) - 1)
            out.append(StreamEvent(eid, npt, bytes(body[10:]), version))
    return out


class StreamEventFilter:
    """Fires each stream event once per new version of its descriptor."""

    def __init__(self):
        self._seen = {}

    def feed(self, section):
        out = []
        for ev in parse_stream_events(section):
            if self._seen.get(ev.event_id) != ev.version:
                self._seen[ev.event_id] = ev.version
                out.append(ev)
        return out


# -- PMT --------------------------------------------------------------------------------

def components_from_pmt(pmt):
    """component_tag -> (pid, stream_type, carousel_id or None) of a parsed PMT.

    The association tags of an object carousel are the component tags of the
    streams (stream_identifier_descriptor), or association_tag_descriptors."""
    from .ait import descriptors
    out = {}
    for stream_type, pid, _ca, info in pmt.streams:
        tags, carousel = [], None
        for tag, body in descriptors(info):
            if tag == STREAM_IDENTIFIER_DESCRIPTOR and body:
                tags.append(body[0])
            elif tag == ASSOCIATION_TAG_DESCRIPTOR and len(body) >= 2:
                tags.append(struct.unpack(">H", body[:2])[0])
            elif tag == CAROUSEL_IDENTIFIER_DESCRIPTOR and len(body) >= 4:
                carousel = struct.unpack(">I", body[:4])[0]
        for t in tags:
            out.setdefault(t, (pid, stream_type, carousel))
    return out


def event_description(xml, name):
    """(component tag, event id) of *name* in an XML stream event description
    (TS 102 796 8.2.1.2: dsmcc / dsmcc_object / stream_event), or None."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for obj in root.iter():
        if obj.tag.rsplit("}", 1)[-1] != "dsmcc_object":
            continue
        for ev in obj:
            if ev.tag.rsplit("}", 1)[-1] == "stream_event" and ev.get("stream_event_name") == name:
                try:
                    return int(obj.get("component_tag")), int(ev.get("stream_event_id"))
                except (TypeError, ValueError):
                    return None
    return None


def parse_dvb_url(url):
    """(ids, component_tag or None, path) of dvb://<ids>[.<ctag>]/<path>.

    ids is "current" or the "onid.tsid.sid" triplet in hex."""
    rest = url.split("://", 1)[1] if "://" in url else url
    host, _, path = rest.partition("/")
    path = path.split("?")[0].split("#")[0]
    parts = [p for p in host.split(".")]
    tag = None
    if parts and parts[0].lower() == "current":
        if len(parts) > 1 and parts[1]:
            tag = int(parts[1], 16)
        ids = "current"
    else:
        if len(parts) >= 4 and parts[3]:
            tag = int(parts[3].split("$")[0], 16)
        ids = ".".join(parts[:3])
    from urllib.parse import unquote
    return ids, tag, unquote(path)


MIME = {
    "html": "text/html", "htm": "text/html", "xhtml": "text/html", "cehtml": "text/html",
    "js": "application/javascript", "css": "text/css", "json": "application/json",
    "xml": "application/xml", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp", "txt": "text/plain",
    "ttf": "font/ttf", "otf": "font/otf", "woff": "font/woff", "woff2": "font/woff2",
    "mp4": "video/mp4", "mp3": "audio/mpeg", "ttml": "application/ttml+xml",
}


def mime_type(path):
    return MIME.get(path.rsplit(".", 1)[-1].lower(), "application/octet-stream")


# -- writing carousels (tests, test streams) ---------------------------------------

def _ior(type_id, carousel_id, module_id, key, tag, transaction=None):
    loc = struct.pack(">IHBBB", carousel_id, module_id, 1, 0, len(key)) + key
    tap_sel = struct.pack(">HII", 1, transaction or 0, 0xFFFFFFFF)
    use = BIOP_DELIVERY_PARA_USE if transaction is not None else BIOP_OBJECT_USE
    binder = bytes([1]) + struct.pack(">HHHB", 0, use, tag, len(tap_sel)) + tap_sel
    comps = (struct.pack(">IB", TAG_OBJECT_LOCATION, len(loc)) + loc
             + struct.pack(">IB", TAG_CONN_BINDER, len(binder)) + binder)
    profile = bytes([0, 2]) + comps
    tid = type_id.encode() + b"\x00"
    return (struct.pack(">I", len(tid)) + tid + b"\x00" * (-len(tid) % 4)
            + struct.pack(">II", 1, TAG_BIOP_PROFILE) + struct.pack(">I", len(profile)) + profile)


def _biop(key, kind, info, body):
    k = kind.encode() + b"\x00"
    m = (bytes([len(key)]) + key + struct.pack(">I", len(k)) + k
         + struct.pack(">H", len(info)) + info + b"\x00"
         + struct.pack(">I", len(body)) + body)
    return b"BIOP\x01\x00\x00\x00" + struct.pack(">I", len(m)) + m


def _dsmcc_section(table_id, ext, payload, version=0):
    sec = bytearray([table_id, 0, 0]) + struct.pack(">H", ext) + bytes([0xC1 | (version << 1), 0, 0]) \
        + payload
    length = len(sec) - 3 + 4
    sec[1], sec[2] = 0xB0 | (length >> 8), length & 0xFF
    return bytes(sec) + struct.pack(">I", crc32(sec))


def _un_header(msg_id, transaction, body):
    return struct.pack(">BBHIBBH", 0x11, 0x03, msg_id, transaction, 0xFF, 0, len(body)) + body


class CarouselWriter:
    """Builds the sections of a one-module object carousel.

    files maps paths to bytes; events maps paths of stream event objects to
    ({name: event id}, association tag of the event stream)."""

    def __init__(self, files, events=None, carousel_id=7, tag=0x0C, version=0,
                 block_size=1000, compress=False):
        self.files, self.events = dict(files), dict(events or {})
        self.carousel_id, self.tag, self.version = carousel_id, tag, version
        self.block_size, self.compress = block_size, compress

    def sections(self):
        msgs, keys = [], {}

        def key_for(path):
            if path not in keys:
                keys[path] = struct.pack(">I", len(keys) + 1)
            return keys[path]

        tree = {}
        for path in list(self.files) + list(self.events):
            node = tree
            parts = path.strip("/").split("/")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = path
        cid, tag = self.carousel_id, self.tag

        def emit(node, prefix, kind):
            bindings = []
            for name, child in sorted(node.items()):
                cpath = prefix + name
                if isinstance(child, dict):
                    emit(child, cpath + "/", "dir")
                    ckind = "dir"
                elif cpath in self.events:
                    ckind = "ste"
                else:
                    ckind = "fil"
                k = key_for(cpath + ("/" if ckind == "dir" else ""))
                n = name.encode() + b"\x00"
                ck = ckind.encode() + b"\x00"
                info = struct.pack(">Q", len(self.files[cpath])) if ckind == "fil" else b""
                bindings.append(bytes([1, len(n)]) + n + bytes([len(ck)]) + ck + b"\x01"
                                + _ior(ckind, cid, 1, k, tag) + struct.pack(">H", len(info)) + info)
            body = struct.pack(">H", len(bindings)) + b"".join(bindings)
            msgs.append(_biop(key_for(prefix if prefix else ""), kind, b"", body))

        for path, data in self.files.items():
            msgs.append(_biop(key_for(path), "fil", struct.pack(">Q", len(data)),
                              struct.pack(">I", len(data)) + data))
        for path, (names, etag) in self.events.items():
            ev_names = b"".join(bytes([len(n.encode()) + 1]) + n.encode() + b"\x00" for n in names)
            info = bytes([0]) + struct.pack(">II", 0, 0) + bytes([0, 0, 1]) \
                + struct.pack(">H", len(names)) + ev_names
            tap = struct.pack(">HHHB", 0, STR_EVENT_USE, etag, 0)
            ids = list(names.values())
            body = bytes([1]) + tap + bytes([len(ids)]) + struct.pack(f">{len(ids)}H", *ids)
            msgs.append(_biop(key_for(path), "ste", info, body))
        emit(tree, "", "srg")
        module = b"".join(msgs)
        size_raw = len(module)
        info_extra = b""
        if self.compress:
            module = zlib.compress(module)
            info_extra = bytes([COMPRESSED_MODULE_DESCRIPTOR, 5, 8]) + struct.pack(">I", size_raw)
        v = self.version
        dii_tid = 0x80000002 | (v << 16)
        tap = struct.pack(">HHHB", 0, BIOP_OBJECT_USE, tag, 0)
        minfo = struct.pack(">III", 0, 0, 0) + bytes([1]) + tap + bytes([len(info_extra)]) + info_extra
        dii = (struct.pack(">IHBBII", cid, self.block_size, 0, 0, 0, 0) + struct.pack(">H", 0)
               + struct.pack(">H", 1) + struct.pack(">HIBB", 1, len(module), v, len(minfo)) + minfo
               + struct.pack(">H", 0))
        gw = _ior("srg", cid, 1, key_for(""), tag, transaction=dii_tid)
        dsi = b"\xff" * 20 + struct.pack(">H", 0) + struct.pack(">H", len(gw)) + gw
        out = [_dsmcc_section(DSMCC_UN_MESSAGES, 0, _un_header(MSG_DSI, 0x80000000 | (v << 16), dsi), v),
               _dsmcc_section(DSMCC_UN_MESSAGES, dii_tid & 0xFFFF, _un_header(MSG_DII, dii_tid, dii), v)]
        for n in range(0, len(module), self.block_size):
            blk = struct.pack(">HBBH", 1, v, 0xFF, n // self.block_size) + module[n:n + self.block_size]
            out.append(_dsmcc_section(DSMCC_DDB, 1, _un_header(MSG_DDB, cid, blk), v))
        return out


def stream_event_section(event_id, data=b"", version=0, npt=0):
    """A table 0x3D section with one stream event descriptor."""
    body = struct.pack(">H", event_id) + (npt | (0x7FFFFFFF << 33)).to_bytes(8, "big") + data
    return _dsmcc_section(DSMCC_STREAM_DESCRIPTORS, event_id,
                          bytes([STREAM_EVENT_DESCRIPTOR, len(body)]) + body, version)


def ts_packets(pid, sections, cc=0):
    """TS packets for *sections* on *pid*, packed back to back (sections may
    end and start in the same packet). Returns (bytes, next continuity counter)."""
    data = b"".join(sections)
    starts, acc = [], 0
    for s in sections:
        starts.append(acc)
        acc += len(s)
    out, pos, si = [], 0, 0
    while pos < len(data):
        while si < len(starts) and starts[si] < pos:
            si += 1
        nxt = starts[si] if si < len(starts) else None
        if nxt is not None and nxt <= pos + 182:
            chunk, pusi = bytes([nxt - pos]) + data[pos:pos + 183], 0x40
            pos += len(chunk) - 1
        elif nxt is not None and nxt == pos + 183:
            chunk, pusi = data[pos:nxt], 0          # stuffing; the next packet starts it
            pos = nxt
        else:
            chunk, pusi = data[pos:pos + 184], 0
            pos += len(chunk)
        out.append(bytes([0x47, pusi | (pid >> 8), pid & 0xFF, 0x10 | (cc & 0x0F)])
                   + chunk + b"\xff" * (184 - len(chunk)))
        cc += 1
    return b"".join(out), cc


# -- the service's DSM-CC ------------------------------------------------------------

class DsmccService:
    """Object carousels and stream event listeners of the tuned service.

    monitor is the capture's lindvb.ait.AitMonitor (PMT components, section
    feeds); add_pid(pid) adds a PID to the demux filter; call_soon(fn) runs
    fn in the main loop (GLib.idle_add). Everything but the section
    callbacks runs in the main loop; callbacks to the user run there too."""

    def __init__(self, monitor, add_pid=None, call_soon=None):
        self.monitor = monitor
        self.add_pid = add_pid or (lambda pid: None)
        self.call_soon = call_soon or (lambda fn: fn())
        self.carousels = {}            # component tag -> ObjectCarousel
        self._carousel_pids = {}       # component tag -> {pid}
        self._waiting = []             # functions to retry when the carousel changes
        self._listeners = {}           # handle -> (tag, event id, callback)
        self._event_pids = {}          # pid -> (StreamEventFilter, fn)
        self._next = 1
        self.closed = False
        if monitor is not None:
            monitor.on_pmt = lambda comps: self.call_soon(self._pmt_changed)

    def close(self):
        self.closed = True
        if self.monitor is None:
            return
        self.monitor.on_pmt = None
        for tag, pids in self._carousel_pids.items():
            for pid in pids:
                self.monitor.unwatch(pid)
        for pid in self._event_pids:
            self.monitor.unwatch(pid)
        self._carousel_pids, self._event_pids = {}, {}
        self._waiting = []

    def _pid(self, tag):
        if self.monitor is None or tag is None:
            return None
        c = self.monitor.components.get(tag)
        return c[0] if c else None

    # -- carousels --

    def carousel(self, tag):
        """The carousel on component *tag*, mounted on first use; None while
        the PMT does not know the tag (yet)."""
        oc = self.carousels.get(tag)
        if oc is not None:
            return oc
        pid = self._pid(tag)
        if pid is None:
            return None
        cid = self.monitor.components[tag][2]
        oc = ObjectCarousel(cid, on_change=lambda: self.call_soon(lambda: self._carousel_changed(tag)))
        self.carousels[tag] = oc
        self._feed_carousel(tag, pid)
        return oc

    def _feed_carousel(self, tag, pid):
        pids = self._carousel_pids.setdefault(tag, set())
        if pid in pids:
            return
        pids.add(pid)
        oc = self.carousels[tag]
        self.monitor.watch(pid, lambda _p, s: oc.feed(s) if s[0] in (0x3B, 0x3C) else None)
        self.add_pid(pid)

    def _carousel_changed(self, tag):
        if self.closed:
            return
        oc = self.carousels.get(tag)
        if oc is not None:
            for t in list(oc.wanted_tags):          # DIIs and DDBs on other streams
                pid = self._pid(t)
                if pid is not None:
                    self._feed_carousel(tag, pid)
        self._retry()

    def when_ready(self, fn):
        """Call fn() now and again on every carousel or PMT change until it
        returns True (main loop)."""
        if not fn():
            self._waiting.append(fn)

    def _retry(self):
        waiting, self._waiting = self._waiting, []
        for fn in waiting:
            if not fn():
                self._waiting.append(fn)

    def _pmt_changed(self):
        if self.closed:
            return
        for tag in list(self.carousels):
            pid = self._pid(tag)
            if pid is not None:
                self._feed_carousel(tag, pid)
        for h, (tag, eid, cb) in list(self._listeners.items()):
            self._feed_events(tag)
        self._retry()

    def resolve(self, url, default_tag=None):
        """Look up a dvb:// URL: (ObjectCarousel or None, object or None/False)."""
        try:
            _ids, tag, path = parse_dvb_url(url)
        except ValueError:
            return None, False
        if tag is None:
            tag = default_tag
        oc = self.carousel(tag) if tag is not None else None
        if oc is None:
            return None, None
        return oc, oc.lookup(path)

    # -- stream events --

    def add_listener(self, tag, event_id, callback):
        """Call callback(StreamEvent) for each new event *event_id* on the
        stream with component *tag*. Returns a handle for remove_listener()."""
        h = self._next
        self._next += 1
        self._listeners[h] = (tag, event_id, callback)
        self._feed_events(tag)
        return h

    def remove_listener(self, handle):
        self._listeners.pop(handle, None)

    def _feed_events(self, tag):
        pid = self._pid(tag)
        if pid is None or pid in self._event_pids:
            return
        flt = StreamEventFilter()

        def on_section(_pid, section, tag=tag):
            if section[0] != DSMCC_STREAM_DESCRIPTORS:
                return
            for ev in flt.feed(section):
                self.call_soon(lambda ev=ev: self._deliver(tag, ev))
        self._event_pids[pid] = (flt, on_section)
        self.monitor.watch(pid, on_section)
        self.add_pid(pid)

    def _deliver(self, tag, ev):
        if self.closed:
            return
        for _h, (t, eid, cb) in list(self._listeners.items()):
            if t == tag and eid == ev.event_id:
                cb(ev)
