"""HbbTV 2.0 companion screens (ETSI TS 102 796 v1.7.1 clause 14) and
inter-device media synchronisation (clause 13, ETSI TS 103 286-2).

Network services of the terminal, in an asyncio loop of their own thread:

* **App to app** (14.5): a WebSocket server that pairs an application's
  connection to the local base URL with a companion's connection to the
  remote base URL on the same endpoint name, then relays their messages.
* **DIAL** (14.7): SSDP discovery and the DIAL REST service, so companion
  apps find the terminal, learn its app-to-app and sync URLs and can launch
  a broadcast-independent HbbTV application with an XML AIT (14.6.2).
* **Terminal discovery**: other HbbTV terminals on the network over DIAL.
* **CSS-CII, CSS-WC, CSS-TS** (TS 103 286-2): content identification, the
  wall clock (UDP) and timeline synchronisation for companion screens.

Only the local app-to-app endpoint (127.0.0.1) runs by default; everything
that listens on the network needs the "companion screens" setting.

Pure asyncio, no GLib: the terminal passes call_soon (GLib.idle_add) for
callbacks into the main loop.
"""

import asyncio
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SSDP_ADDR, SSDP_PORT = "239.255.255.250", 1900
DIAL_ST = "urn:dial-multiscreen-org:service:dial:1"
HBBTV_NS = "urn:hbbtv:HbbTVCompanionScreen:2014"
PAIRING_COMPLETED = "pairingcompleted"
MAX_MESSAGE = 1 << 20


def wall_clock_ns():
    """The terminal's wall clock for CSS-WC and CSS-TS (monotonic)."""
    return time.monotonic_ns()


def lan_address():
    """The address other devices reach this computer at (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# -- HTTP and WebSocket ------------------------------------------------------------

async def read_request(reader):
    """(method, path, headers, body) of one HTTP request, or None."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
            ConnectionError):
        return None
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, path, _version = lines[0].split(" ", 2)
    except ValueError:
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    body = b""
    n = int(headers.get("content-length", "0") or 0)
    if 0 < n <= MAX_MESSAGE:
        try:
            body = await asyncio.wait_for(reader.readexactly(n), 10)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return None
    return method.upper(), path, headers, body


def http_response(status, reason, body=b"", ctype="text/plain", extra=None):
    head = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}",
            "Connection: close", "Access-Control-Allow-Origin: *"]
    if body:
        head.append(f"Content-Type: {ctype}")
    for k, v in (extra or {}).items():
        head.append(f"{k}: {v}")
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


class WebSocket:
    """The server side of one WebSocket connection (RFC 6455)."""

    def __init__(self, reader, writer, path):
        self.reader, self.writer, self.path = reader, writer, path
        self.closed = False
        self._send_lock = asyncio.Lock()

    @staticmethod
    def accept_key(key):
        return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()

    @classmethod
    async def handshake(cls, reader, writer, path, headers):
        key = headers.get("sec-websocket-key")
        if "websocket" not in headers.get("upgrade", "").lower() or not key:
            return None
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {cls.accept_key(key)}\r\n\r\n").encode())
        await writer.drain()
        return cls(reader, writer, path)

    async def send(self, data, opcode=None):
        if self.closed:
            return
        if opcode is None:
            opcode = 0x1 if isinstance(data, str) else 0x2
        if isinstance(data, str):
            data = data.encode()
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        async with self._send_lock:
            try:
                self.writer.write(head + data)
                await self.writer.drain()
            except (ConnectionError, RuntimeError):
                self.closed = True

    async def recv(self):
        """The next message (str or bytes), or None when the connection ended."""
        parts, kind = [], None
        while True:
            try:
                b0, b1 = await self.reader.readexactly(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", await self.reader.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await self.reader.readexactly(8))[0]
                if n > MAX_MESSAGE:
                    await self.close(1009)
                    return None
                mask = await self.reader.readexactly(4) if b1 & 0x80 else None
                data = await self.reader.readexactly(n)
            except (asyncio.IncompleteReadError, ConnectionError):
                self.closed = True
                return None
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            opcode = b0 & 0x0F
            if opcode == 0x8:
                await self.close()
                return None
            if opcode == 0x9:
                await self.send(data, 0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind, parts = opcode, [data]
            elif opcode == 0x0 and kind is not None:
                parts.append(data)
            else:
                continue
            if b0 & 0x80:
                whole = b"".join(parts)
                return whole.decode("utf-8", "replace") if kind == 0x1 else whole

    async def close(self, code=1000):
        if self.closed:
            return
        await self.send(struct.pack(">H", code), 0x8)
        self.closed = True
        try:
            self.writer.close()
        except RuntimeError:
            pass


async def ws_connect(url, timeout=5):
    """A small WebSocket client (tests, terminal discovery): returns WebSocket-like
    (send, recv, close) over a client connection."""
    parts = urlsplit(url)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(parts.hostname, parts.port or 80), timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    path = parts.path + (("?" + parts.query) if parts.query else "")
    writer.write((f"GET {path or '/'} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    if b" 101 " not in head.split(b"\r\n")[0]:
        writer.close()
        raise ConnectionError(head.split(b"\r\n")[0].decode("latin-1"))
    return ClientWebSocket(reader, writer, path)


class ClientWebSocket(WebSocket):
    async def send(self, data, opcode=None):
        if self.closed:
            return
        if opcode is None:
            opcode = 0x1 if isinstance(data, str) else 0x2
        if isinstance(data, str):
            data = data.encode()
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        try:
            self.writer.write(head + mask + body)
            await self.writer.drain()
        except (ConnectionError, RuntimeError):
            self.closed = True


# -- app to app ----------------------------------------------------------------------

class App2App:
    """Pairs local (application) and remote (companion) WebSocket connections
    by endpoint name and relays their messages (TS 102 796 14.5)."""

    def __init__(self):
        self.waiting = {"local": {}, "remote": {}}    # side -> endpoint -> [WebSocket]

    async def serve(self, ws, side, endpoint):
        other = "remote" if side == "local" else "local"
        queue = self.waiting[other].get(endpoint) or []
        peer = None
        while queue and peer is None:
            cand = queue.pop(0)
            peer = None if cand.closed else cand
        pending = None
        if peer is not None:
            ws.peer, peer.peer = peer, ws
            await ws.send(PAIRING_COMPLETED)
            await peer.send(PAIRING_COMPLETED)
            peer.paired.set()
        else:
            ws.peer, ws.paired = None, asyncio.Event()
            self.waiting[side].setdefault(endpoint, []).append(ws)
            while not ws.paired.is_set():
                pending = pending or asyncio.ensure_future(ws.recv())
                waiter = asyncio.ensure_future(ws.paired.wait())
                done, _ = await asyncio.wait({pending, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if pending in done:
                    msg, pending = pending.result(), None
                    if msg is None:                       # gone before a partner came
                        waiter.cancel()
                        lst = self.waiting[side].get(endpoint, [])
                        if ws in lst:
                            lst.remove(ws)
                        if ws.peer is not None:
                            await ws.peer.close()
                        return
                    # a message before pairing has nobody to go to: dropped
                else:
                    break
        while True:
            if pending is not None:
                msg, pending = await pending, None
            else:
                msg = await ws.recv()
            if msg is None:
                await ws.peer.close()
                return
            await ws.peer.send(msg)


# -- XML AIT (DIAL launch) -----------------------------------------------------------

def parse_xml_ait(data):
    """The first application of an XML AIT (TS 102 809 5.4, as sent by a
    companion to launch an app): {url, org_id, app_id, name} or None."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    def local(e):
        return e.tag.rsplit("}", 1)[-1]

    for app in root.iter():
        if local(app) != "Application":
            continue
        out = {"url": "", "org_id": 0, "app_id": 0, "name": ""}
        base = loc = ""
        for e in app.iter():
            tag, text = local(e), (e.text or "").strip()
            if tag == "appName" and not out["name"]:
                out["name"] = text
            elif tag == "orgId":
                out["org_id"] = int(text, 0) if text else 0
            elif tag == "appId":
                out["app_id"] = int(text, 0) if text else 0
            elif tag == "URLBase" and not base:
                base = text
            elif tag == "applicationLocation":
                loc = text
        from .ait import join_url
        out["url"] = join_url(base, loc) if base else loc
        if out["url"].startswith(("http://", "https://")):
            return out
    return None


# -- the service ----------------------------------------------------------------------

class CompanionService:
    """All HbbTV network services of the terminal.

    start(network=False) runs the local app-to-app endpoint; with network=True
    also DIAL (SSDP + REST), the remote app-to-app endpoint and CSS-CII,
    CSS-WC and CSS-TS. on_launch(info, reply) (main loop) is asked when a
    companion launches an application: reply(True) starts it."""

    def __init__(self, user_agent="", friendly_name="LinDVB", on_launch=None, call_soon=None,
                 device_uuid=None, ssdp_port=SSDP_PORT):
        self.user_agent = user_agent
        self.friendly_name = friendly_name
        self.on_launch = on_launch
        self.call_soon = call_soon or (lambda fn: fn())
        self.uuid = device_uuid or str(uuid.uuid4())
        self.ssdp_port = ssdp_port
        self.app2app = App2App()
        self.loop = None
        self.thread = None
        self.local_port = 0
        self.lan_port = 0
        self.wc_port = 0
        self.host = "127.0.0.1"
        self.network = False
        self.cii = {"protocolVersion": "1.1", "contentId": None, "contentIdStatus": "final",
                    "presentationStatus": ["okay"], "mrsUrl": None, "wcUrl": None, "tsUrl": None,
                    "timelines": []}
        self.timelines = {}             # selector -> control timestamp dict, or None
        self._cii_clients = set()
        self._ts_clients = {}           # WebSocket -> setup (dict)
        self._servers = []
        self._ready = threading.Event()

    # -- life --

    def start(self, network=False):
        if self.thread is not None:
            return
        self.network = network
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="hbbtv-cs", daemon=True)
        self.thread.start()
        self._ready.wait(10)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start_servers())
        self._ready.set()
        self.loop.run_forever()

    async def _start_servers(self):
        local = await asyncio.start_server(lambda r, w: self._client(r, w, "local"), "127.0.0.1", 0)
        self._servers.append(local)
        self.local_port = local.sockets[0].getsockname()[1]
        if not self.network:
            return
        self.host = lan_address()
        lan = await asyncio.start_server(lambda r, w: self._client(r, w, "lan"), "0.0.0.0", 0)
        self._servers.append(lan)
        self.lan_port = lan.sockets[0].getsockname()[1]
        wc, _ = await self.loop.create_datagram_endpoint(lambda: WallClockProtocol(),
                                                         local_addr=("0.0.0.0", 0))
        self._servers.append(wc)
        self.wc_port = wc.get_extra_info("sockname")[1]
        self.cii["wcUrl"] = f"udp://{self.host}:{self.wc_port}"
        self.cii["tsUrl"] = f"ws://{self.host}:{self.lan_port}/ts"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self.ssdp_port))
            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            ssdp, _ = await self.loop.create_datagram_endpoint(lambda: SsdpProtocol(self), sock=sock)
            self._servers.append(ssdp)
        except OSError:
            pass                       # no multicast here; DIAL still answers on its URL

    def stop(self):
        if self.loop is None:
            return

        def shutdown():
            for s in self._servers:
                s.close()
            self.loop.stop()
        self.loop.call_soon_threadsafe(shutdown)
        self.thread.join(5)
        self.loop, self.thread = None, None
        self._servers = []

    # -- URLs (TS 102 796 8.2.6, hbbtvCSManager) --

    @property
    def app2app_local_base(self):
        return f"ws://127.0.0.1:{self.local_port}/hbbtv/" if self.local_port else ""

    @property
    def app2app_remote_base(self):
        return f"ws://{self.host}:{self.lan_port}/hbbtv/" if self.lan_port else ""

    @property
    def inter_dev_sync_url(self):
        return f"ws://{self.host}:{self.lan_port}/cii" if self.lan_port else ""

    @property
    def app_launch_url(self):
        return f"http://{self.host}:{self.lan_port}/apps/HbbTV" if self.lan_port else ""

    @property
    def location(self):
        return f"http://{self.host}:{self.lan_port}/dd.xml"

    # -- state from the terminal (any thread) --

    def set_content(self, content_id, presentation="okay", timelines=None):
        """CSS-CII: what the terminal presents (a dvb:// URL of the service)."""
        def update():
            self.cii["contentId"] = content_id
            self.cii["presentationStatus"] = [presentation]
            if timelines is not None:
                self.cii["timelines"] = timelines
            for ws in list(self._cii_clients):
                asyncio.ensure_future(ws.send(json.dumps(self.cii)))
        self._soon(update)

    def set_timeline(self, selector, content_time, wall_ns, speed=1.0):
        """CSS-TS: a control timestamp of the master timeline; content_time
        None = the timeline is not available."""
        ct = {"contentTime": None if content_time is None else str(int(content_time)),
              "wallClockTime": str(int(wall_ns)),
              "timelineSpeedMultiplier": None if content_time is None else speed}

        def update():
            self.timelines[selector] = ct
            for ws, setup in list(self._ts_clients.items()):
                if setup.get("timelineSelector") == selector:
                    asyncio.ensure_future(ws.send(json.dumps(ct)))
        self._soon(update)

    def _soon(self, fn):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(fn)

    # -- connections --

    async def _client(self, reader, writer, where):
        req = await read_request(reader)
        if req is None:
            writer.close()
            return
        method, path, headers, body = req
        clean = path.split("?")[0]
        try:
            if clean.startswith("/hbbtv/") and len(clean) > 7:
                ws = await WebSocket.handshake(reader, writer, clean, headers)
                if ws is not None:
                    side = "local" if where == "local" else "remote"
                    await self.app2app.serve(ws, side, clean[7:])
                    return
            if where == "lan":
                if clean == "/cii":
                    ws = await WebSocket.handshake(reader, writer, clean, headers)
                    if ws is not None:
                        await self._serve_cii(ws)
                        return
                if clean == "/ts":
                    ws = await WebSocket.handshake(reader, writer, clean, headers)
                    if ws is not None:
                        await self._serve_ts(ws)
                        return
                writer.write(await self._dial(method, clean, headers, body))
            else:
                writer.write(http_response(404, "Not Found"))
            await writer.drain()
        except (ConnectionError, RuntimeError):
            pass
        finally:
            try:
                writer.close()
            except RuntimeError:
                pass

    async def _serve_cii(self, ws):
        self._cii_clients.add(ws)
        try:
            await ws.send(json.dumps(self.cii))
            while await ws.recv() is not None:
                pass
        finally:
            self._cii_clients.discard(ws)

    async def _serve_ts(self, ws):
        try:
            first = await ws.recv()
            try:
                setup = json.loads(first) if first else None
            except ValueError:
                setup = None
            if not isinstance(setup, dict) or "timelineSelector" not in setup:
                await ws.close(1003)
                return
            self._ts_clients[ws] = setup
            stem = setup.get("contentIdStem", "")
            cid = self.cii.get("contentId") or ""
            ct = self.timelines.get(setup["timelineSelector"])
            if ct is None or not cid.startswith(stem):
                ct = {"contentTime": None, "wallClockTime": str(wall_clock_ns()),
                      "timelineSpeedMultiplier": None}
            await ws.send(json.dumps(ct))
            while await ws.recv() is not None:    # AptEptLpt messages: noted, not needed
                pass
        finally:
            self._ts_clients.pop(ws, None)

    # -- DIAL (14.7) --

    def device_description(self):
        return (f'<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0">'
                '<specVersion><major>1</major><minor>0</minor></specVersion><device>'
                '<deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>'
                f'<friendlyName>{_x(self.friendly_name)}</friendlyName>'
                '<manufacturer>LinDVB</manufacturer><modelName>LinDVB</modelName>'
                f'<UDN>uuid:{self.uuid}</UDN></device></root>').encode()

    def dial_app(self):
        ns = f'xmlns:hbbtv="{HBBTV_NS}"'
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<service xmlns="urn:dial-multiscreen-org:schemas:dial" dialVer="2.1">'
                '<name>HbbTV</name><options allowStop="false"/><state>running</state>'
                '<additionalData>'
                f'<hbbtv:X_HbbTV_App2AppURL {ns}>{_x(self.app2app_remote_base)}</hbbtv:X_HbbTV_App2AppURL>'
                f'<hbbtv:X_HbbTV_InterDevSyncURL {ns}>{_x(self.inter_dev_sync_url)}'
                '</hbbtv:X_HbbTV_InterDevSyncURL>'
                f'<hbbtv:X_HbbTV_UserAgent {ns}>{_x(self.user_agent)}</hbbtv:X_HbbTV_UserAgent>'
                '</additionalData></service>').encode()

    async def _dial(self, method, path, headers, body):
        if path == "/dd.xml" and method == "GET":
            return http_response(200, "OK", self.device_description(), 'text/xml; charset="utf-8"',
                                 {"Application-URL": f"http://{self.host}:{self.lan_port}/apps/"})
        if path.rstrip("/") == "/apps/HbbTV":
            if method == "GET":
                return http_response(200, "OK", self.dial_app(), 'text/xml; charset="utf-8"')
            if method == "POST":
                info = parse_xml_ait(body)
                if info is None:
                    return http_response(400, "Bad Request")
                ok = await self._ask_launch(info)
                if ok:
                    return http_response(201, "Created", extra={
                        "Location": f"http://{self.host}:{self.lan_port}/apps/HbbTV/run"})
                return http_response(403, "Forbidden")
        if path.startswith("/apps/"):
            return http_response(404, "Not Found")
        return http_response(404, "Not Found")

    async def _ask_launch(self, info):
        if self.on_launch is None:
            return False
        fut = self.loop.create_future()

        def reply(ok):
            self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(bool(ok)))
        self.call_soon(lambda: self.on_launch(info, reply))
        try:
            return await asyncio.wait_for(fut, 60)
        except asyncio.TimeoutError:
            return False

    # -- discovery of other terminals --

    def discover_terminals(self, callback, timeout=3.0):
        """callback([{enum_id, friendly_name, X_HbbTV_App2AppURL,
        X_HbbTV_InterDevSyncURL, X_HbbTV_UserAgent}]) in the main loop."""
        if self.loop is None:
            self.call_soon(lambda: callback([]))
            return
        asyncio.run_coroutine_threadsafe(self._discover(callback, timeout), self.loop)

    async def _discover(self, callback, timeout):
        found = []
        try:
            locations = await ssdp_search(DIAL_ST, timeout, self.ssdp_port)
            for n, loc in enumerate(sorted(set(locations))):
                if loc == self.location:
                    continue
                t = await fetch_dial_terminal(loc)
                if t is not None:
                    t["enum_id"] = n + 1
                    found.append(t)
        except OSError:
            pass
        self.call_soon(lambda: callback(found))


def _x(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, service):
        self.service = service

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        text = data.decode("latin-1", "replace")
        if not text.startswith("M-SEARCH"):
            return
        st = ""
        for line in text.split("\r\n"):
            if line.lower().startswith("st:"):
                st = line[3:].strip()
        if st not in (DIAL_ST, "ssdp:all"):
            return
        s = self.service
        reply = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nEXT:\r\n"
                 f"LOCATION: {s.location}\r\nSERVER: Linux/1.0 UPnP/1.1 LinDVB/1.0\r\n"
                 f"ST: {DIAL_ST}\r\nUSN: uuid:{s.uuid}::{DIAL_ST}\r\nBOOTID.UPNP.ORG: 1\r\n\r\n")
        self.transport.sendto(reply.encode(), addr)


async def ssdp_search(st, timeout=3.0, port=SSDP_PORT, addr=SSDP_ADDR):
    """LOCATIONs of the devices that answer an SSDP M-SEARCH for *st*."""
    loop = asyncio.get_running_loop()
    found = []

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, _addr):
            for line in data.decode("latin-1", "replace").split("\r\n"):
                if line.lower().startswith("location:"):
                    found.append(line.split(":", 1)[1].strip())

    transport, _ = await loop.create_datagram_endpoint(P, local_addr=("0.0.0.0", 0))
    msg = (f"M-SEARCH * HTTP/1.1\r\nHOST: {addr}:{port}\r\nMAN: \"ssdp:discover\"\r\n"
           f"MX: {max(1, int(timeout))}\r\nST: {st}\r\n\r\n").encode()
    try:
        transport.sendto(msg, (addr, port))
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return found


async def http_get(url, timeout=3.0):
    """(status, headers, body) of a plain HTTP GET."""
    parts = urlsplit(url)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(parts.hostname, parts.port or 80), timeout)
    try:
        writer.write(f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
                     "Connection: close\r\n\r\n".encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(MAX_MESSAGE), timeout)
    finally:
        writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if len(lines[0].split(" ")) > 1 else 0
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


async def fetch_dial_terminal(location):
    """The HbbTV terminal behind a DIAL device description, or None."""
    try:
        status, headers, body = await http_get(location)
        if status != 200 or "application-url" not in headers:
            return None
        name = ""
        try:
            for e in ET.fromstring(body).iter():
                if e.tag.rsplit("}", 1)[-1] == "friendlyName":
                    name = (e.text or "").strip()
        except ET.ParseError:
            pass
        app_url = headers["application-url"].rstrip("/") + "/HbbTV"
        status, _h, body = await http_get(app_url)
        if status != 200:
            return None
        out = {"friendly_name": name, "X_HbbTV_App2AppURL": "", "X_HbbTV_InterDevSyncURL": "",
               "X_HbbTV_UserAgent": ""}
        for e in ET.fromstring(body).iter():
            tag = e.tag.rsplit("}", 1)[-1]
            if tag in out and tag != "friendly_name":
                out[tag] = (e.text or "").strip()
        return out if out["X_HbbTV_App2AppURL"] else None
    except (OSError, ValueError, asyncio.TimeoutError, ET.ParseError):
        return None


# -- CSS-WC (TS 103 286-2 clause 8) -----------------------------------------------------

WC_REQUEST, WC_RESPONSE = 0, 1


def wc_message(msg_type, originate, receive=0, transmit=0, precision=-20, max_freq_error=0):
    """A 32-byte wall clock message; times in nanoseconds."""
    def split(ns):
        return ns // 1_000_000_000, ns % 1_000_000_000
    o, r, t = split(originate), split(receive), split(transmit)
    return struct.pack(">BBbBIIIIIII", 0, msg_type, precision, 0, max_freq_error,
                       o[0], o[1], r[0], r[1], t[0], t[1])


def parse_wc(data):
    if len(data) < 32 or data[0] != 0:
        return None
    _v, mtype, precision, _r, mfe, os_, on, rs, rn, ts, tn = struct.unpack(">BBbBIIIIIII", data[:32])
    return {"type": mtype, "precision": precision, "max_freq_error": mfe,
            "originate": os_ * 1_000_000_000 + on, "receive": rs * 1_000_000_000 + rn,
            "transmit": ts * 1_000_000_000 + tn, "raw_originate": data[8:16]}


class WallClockProtocol(asyncio.DatagramProtocol):
    """Answers wall clock requests with the terminal's wall clock."""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        received = wall_clock_ns()
        msg = parse_wc(data)
        if msg is None or msg["type"] != WC_REQUEST:
            return
        reply = bytearray(wc_message(WC_RESPONSE, 0, received, wall_clock_ns()))
        reply[8:16] = msg["raw_originate"]            # echoed exactly as the client sent it
        self.transport.sendto(bytes(reply), addr)
