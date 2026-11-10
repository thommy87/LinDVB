# SPDX-License-Identifier: GPL-2.0-or-later
"""SAT>IP (DVB over IP) client: servers on the network as tuners.

A SAT>IP server (a SAT>IP box or LNB, minisatip, a FRITZ!Box Cable, Digital
Devices Octopus NET, ...) has one or more DVB tuners and sends what they
receive as an MPEG transport stream in RTP. LinDVB talks RTSP to it: SETUP
with the tuning data in the URL query, PLAY, OPTIONS as keep-alive, and PLAY
with ``pids=`` when the PID filter changes. The stream comes over UDP or, for
networks that block it, interleaved in the RTSP connection (TCP). The server
reports level, lock and quality in RTCP (APP packets ``SES1``).

The servers are kept in the setting ``satip_servers``::

    {"id": 1, "host": "192.168.178.1", "port": 554, "name": "FRITZ!Box 6591 Cable",
     "uuid": "...", "caps": "DVBC-4", "transport": "udp"}

Each tuner of a server becomes a tuner profile (lindvb.tunerprofiles) with
``"type": "satip"`` and an adapter number from :data:`ADAPTER_BASE` up, which
no real /dev/dvb adapter uses. The tuner choice, timeshift, recordings, EPG
and HbbTV then work as for local tuners (lindvb.tuner.Capture).

The module has no GStreamer dependency.
"""

import random
import re
import select
import socket
import struct
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

ADAPTER_BASE = 1000
TUNERS_PER_SERVER = 16           # adapter numbers each server may use
DEFAULT_PORT = 554
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_ST = "urn:ses-com:device:SatIPServer:1"
USER_AGENT = "LinDVB SAT>IP"

# X_SATIPCAP names -> dvbsrc delivery systems a tuner of that kind receives
CAP_SYSTEMS = {
    "DVBS2": ["dvb-s", "dvb-s2"], "DVBS": ["dvb-s"],
    "DVBT2": ["dvb-t", "dvb-t2"], "DVBT": ["dvb-t"],
    "DVBC2": ["dvb-c-a", "dvb-c2"], "DVBC": ["dvb-c-a"],
}
# reception types to choose from for a server added by hand (no X_SATIPCAP)
MANUAL_CAPS = ("DVBS2", "DVBC", "DVBT2")

# dvbsrc modulation nicks -> SAT>IP mtype
MTYPES = {"qpsk": "qpsk", "8psk": "8psk", "16apsk": "16apsk", "32apsk": "32apsk",
          "qam-16": "16qam", "qam-32": "32qam", "qam-64": "64qam", "qam-128": "128qam",
          "qam-256": "256qam"}
FECS = {"1/2", "2/3", "3/4", "3/5", "4/5", "5/6", "7/8", "8/9", "9/10"}


class SatipError(RuntimeError):
    pass


# -- servers and tuner profiles ----------------------------------------------------

def is_satip(profile):
    return bool(profile) and profile.get("type") == "satip"


def parse_caps(caps):
    """[("DVBS2", 4), ("DVBT", 2)] from an X_SATIPCAP string ("DVBS2-4,DVBT-2")."""
    out = []
    for part in (caps or "").replace(" ", "").split(","):
        name, _, n = part.partition("-")
        name = name.upper()
        if name in CAP_SYSTEMS:
            out.append((name, int(n) if n.isdigit() else 1))
    return out


def server_systems(caps):
    systems = []
    for name, _n in parse_caps(caps):
        systems += [s for s in CAP_SYSTEMS[name] if s not in systems]
    return systems


def tuner_count(caps):
    """How many streams the server serves at once (the largest tuner group;
    the groups are often the same tuners with different reception types)."""
    counts = [n for _name, n in parse_caps(caps)]
    return max(1, min(TUNERS_PER_SERVER, max(counts) if counts else 1))


def next_server_id(servers):
    return max([int(s.get("id", 0) or 0) for s in servers] + [0]) + 1


def server_label(server):
    name = (server.get("name") or "").strip()
    host = server.get("host", "")
    return f"{name} ({host})" if name and name != host else host


def server_profiles(settings):
    """One tuner profile per tuner of each configured SAT>IP server."""
    out = []
    for srv in settings.get("satip_servers") or []:
        if not srv.get("host"):
            continue
        sid = int(srv.get("id", 0) or 0)
        n = tuner_count(srv.get("caps"))
        base = server_label(srv) or srv["host"]
        for i in range(n):
            out.append({
                "type": "satip", "adapter": ADAPTER_BASE + sid * TUNERS_PER_SERVER + i,
                "frontend": 0, "name": f"SAT>IP {base}" + (f" #{i + 1}" if n > 1 else ""),
                "systems": server_systems(srv.get("caps")), "enabled": True, "priority": 0,
                "group": "", "server": sid, "server_label": base, "host": srv["host"],
                "port": int(srv.get("port") or DEFAULT_PORT),
                "transport": srv.get("transport") or "udp"})
    return out


def merge_profiles(saved, settings):
    """*saved* profiles with the SAT>IP tuners brought up to date: the user's
    choices (on/off, priority, name) stay, the server data is taken from the
    settings, tuners of removed servers go and new ones are added."""
    current = {p["adapter"]: p for p in server_profiles(settings)}
    out = []
    for p in saved:
        if not is_satip(p):
            out.append(p)
            continue
        cur = current.pop(p.get("adapter"), None)
        if cur is None:
            continue
        q = dict(cur)
        for k in ("enabled", "priority", "label"):
            if k in p:
                q[k] = p[k]
        out.append(q)
    out += current.values()
    return out


# -- the tuning request ------------------------------------------------------------

def _mhz(khz):
    s = f"{khz / 1000:.3f}".rstrip("0").rstrip(".")
    return s


def tuning_query(ch, pids="all"):
    """The SAT>IP URL query (without "?") for channel *ch*.

    *pids*: "all", "none", or an iterable / colon list of PIDs (8192 = all).
    """
    q = []
    d = ch.delsys
    if d in ("dvb-s", "dvb-s2"):
        q.append(("src", str(ch.diseqc + 1 if ch.diseqc >= 0 else 1)))
        q.append(("freq", _mhz(ch.frequency)))
        q.append(("pol", (ch.polarity or "h").lower()[:1]))
        q.append(("ro", "0.35"))
        q.append(("msys", "dvbs2" if d == "dvb-s2" else "dvbs"))
        mt = MTYPES.get(ch.modulation)
        if d == "dvb-s":
            q.append(("mtype", "qpsk"))
        elif mt:
            q.append(("mtype", mt))
        if d == "dvb-s2":
            q.append(("plts", "off"))
        q.append(("sr", str(ch.symbol_rate)))
        if ch.fec in FECS:
            q.append(("fec", ch.fec.replace("/", "")))
        if d == "dvb-s2" and ch.stream_id >= 0:
            q.append(("isi", str(ch.stream_id)))
    elif d in ("dvb-t", "dvb-t2"):
        q.append(("freq", _mhz(ch.frequency)))
        bw = (ch.bandwidth_hz or 8000000) / 1e6
        q.append(("bw", f"{bw:g}"))
        q.append(("msys", "dvbt2" if d == "dvb-t2" else "dvbt"))
        mt = MTYPES.get(ch.modulation)
        if mt and mt.endswith("qam") or ch.modulation == "qpsk":
            q.append(("mtype", mt))
        if d == "dvb-t2" and ch.stream_id >= 0:
            q.append(("plp", str(ch.stream_id)))
    elif d.startswith("dvb-c"):
        q.append(("freq", _mhz(ch.frequency)))
        q.append(("msys", "dvbc2" if d == "dvb-c2" else "dvbc"))
        if d == "dvb-c2":
            q.append(("bw", f"{(ch.bandwidth_hz or 8000000) / 1e6:g}"))
            if ch.stream_id >= 0:
                q.append(("plp", str(ch.stream_id)))
        else:
            mt = MTYPES.get(ch.modulation)
            if mt and mt.endswith("qam"):
                q.append(("mtype", mt))
            q.append(("sr", str(ch.symbol_rate or 6900)))
    else:
        raise SatipError(f"SAT>IP does not carry {d} channels.")
    q.append(("pids", pid_list(pids)))
    return "&".join(f"{k}={v}" for k, v in q)


def pid_list(pids):
    """The ``pids=`` value: "all", "none" or "0,16,17"."""
    if isinstance(pids, str):
        if pids in ("all", "none"):
            return pids
        pids = [int(x) for x in re.split(r"[:,\s]+", pids) if x.isdigit()]
    pids = sorted({int(p) for p in pids})
    if not pids:
        return "none"
    if 8192 in pids:
        return "all"
    return ",".join(map(str, pids))


# -- RTP / RTCP --------------------------------------------------------------------

def rtp_payload(packet):
    """The payload of an RTP packet (bytes), or b"" if it is not one."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return b""
    cc = packet[0] & 0x0F
    off = 12 + 4 * cc
    if packet[0] & 0x10:                         # header extension
        if len(packet) < off + 4:
            return b""
        off += 4 + 4 * struct.unpack_from("!H", packet, off + 2)[0]
    end = len(packet)
    if packet[0] & 0x20 and end > off:           # padding
        end -= packet[-1]
    return bytes(packet[off:end]) if end > off else b""


def rtcp_app_strings(data):
    """The strings of the SES1 APP packets in a compound RTCP packet."""
    out = []
    off = 0
    while off + 4 <= len(data):
        if data[off] >> 6 != 2:
            break
        pt = data[off + 1]
        length = (struct.unpack_from("!H", data, off + 2)[0] + 1) * 4
        pkt = data[off:off + length]
        if pt == 204 and len(pkt) >= 16 and pkt[8:12] == b"SES1":
            n = struct.unpack_from("!H", pkt, 14)[0]
            out.append(pkt[16:16 + n].decode("ascii", "replace").rstrip("\0"))
        off += max(length, 4)
    return out


def parse_status(text):
    """dvbsrc-like figures from a SAT>IP status string
    ("ver=1.0;src=1;tuner=1,224,1,15,11494.00,h,...;pids=0,17").

    level 0..255 and quality 0..15 become signal and snr 0..65535.
    """
    fields = dict(p.partition("=")[::2] for p in text.split(";") if "=" in p)
    tuner = fields.get("tuner", "").split(",")
    if len(tuner) < 4:
        return {}
    try:
        level, lock, quality = int(tuner[1]), int(tuner[2]), int(tuner[3])
    except ValueError:
        return {}
    return {"signal": min(65535, level * 65535 // 255), "lock": bool(lock),
            "snr": min(65535, quality * 65535 // 15) if lock else 0}


# -- RTSP --------------------------------------------------------------------------

def parse_response(raw):
    """(status code, headers with lower-case names, body) of an RTSP response."""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    try:
        code = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise SatipError(f"Not an RTSP response: {lines[0]!r}") from None
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return code, headers, body


class Session:
    """One RTSP session on a SAT>IP server: one tuner, one stream.

    *on_data(bytes)* gets the transport stream and *on_status(dict)* the
    signal figures, both in the receiving thread.
    """

    TIMEOUT = 5.0                    # seconds to wait for an answer
    CHUNK = 7 * 188 * 24             # TS bytes handed on at once

    def __init__(self, host, port=DEFAULT_PORT, transport="udp", on_data=None, on_status=None,
                 connect=socket.create_connection):
        self.host, self.port = host, int(port or DEFAULT_PORT)
        self.transport = transport if transport in ("udp", "tcp") else "udp"
        self.on_data = on_data or (lambda data: None)
        self.on_status = on_status or (lambda st: None)
        self._connect = connect
        self.sock = None
        self.rtp = self.rtcp = None
        self.session = ""
        self.stream_id = ""
        self.keepalive = 30.0
        self._cseq = 0
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._answers = {}
        self._waiting = set()
        self._buf = b""
        self._pending = bytearray()
        self._pending_t = 0.0
        self._last_req = 0.0
        self._closed = False             # close() was called
        self.dead = False                # the connection is gone
        self._thread = None
        self.status = {}
        self.error = ""

    @property
    def base(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"rtsp://{host}:{self.port}/"

    # requests
    def _send(self, method, url, headers=(), wait=False):
        with self._send_lock:
            self._cseq += 1
            cseq = self._cseq
            if wait:
                with self._cond:             # before the answer can come
                    self._waiting.add(cseq)
            lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", f"User-Agent: {USER_AGENT}"]
            if self.session:
                lines.append(f"Session: {self.session}")
            lines += list(headers)
            self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            self._last_req = time.monotonic()
        return cseq

    def request(self, method, url, headers=(), wait=True):
        cseq = self._send(method, url, headers, wait)
        if not wait:
            return None
        end = time.monotonic() + self.TIMEOUT
        try:
            with self._cond:
                while cseq not in self._answers:
                    left = end - time.monotonic()
                    if left <= 0 or self._closed or self.dead:
                        raise SatipError(f"{self.host}: no answer to {method}"
                                         + (f" ({self.error})" if self.error else ""))
                    self._cond.wait(left)
                code, headers_, _body = self._answers.pop(cseq)
        finally:
            with self._cond:
                self._waiting.discard(cseq)
        if code != 200:
            raise SatipError(f"{self.host}: {method} failed with RTSP {code}"
                             + (f" ({headers_['x-satip-error']})"
                                if "x-satip-error" in headers_ else ""))
        return headers_

    def open(self, query):
        """SETUP and PLAY the stream for *query* (from :func:`tuning_query`)."""
        try:
            self.sock = self._connect((self.host, self.port), self.TIMEOUT)
        except OSError as e:
            raise SatipError(f"Could not connect to {self.host}:{self.port}: "
                             f"{e.strerror or e}") from None
        self.sock.settimeout(None)
        if self.transport == "udp":
            transport = f"RTP/AVP;unicast;client_port={self._open_udp()}"
        else:
            transport = "RTP/AVP/TCP;interleaved=0-1"
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"satip-{self.host}")
        self._thread.start()
        try:
            h = self.request("SETUP", f"{self.base}?{query}", [f"Transport: {transport}"])
            sess = h.get("session", "")
            self.session = sess.split(";")[0].strip()
            m = re.search(r"timeout=(\d+)", sess)
            if m:
                self.keepalive = max(5.0, int(m.group(1)) / 2)
            self.stream_id = h.get("com.ses.streamid", "").strip()
            if not self.session:
                raise SatipError(f"{self.host}: the server gave no session for the stream.")
            self.request("PLAY", self._stream_url())
        except Exception:
            self.close()
            raise

    def _stream_url(self, query=""):
        url = f"{self.base}stream={self.stream_id}" if self.stream_id else self.base
        return url + (f"?{query}" if query else "")

    def set_pids(self, pids):
        """Change the PID filter (does not wait for the answer)."""
        if self.session and not self._closed:
            try:
                self.request("PLAY", self._stream_url(f"pids={pid_list(pids)}"), wait=False)
            except OSError as e:
                self.error = str(e)

    def close(self):
        if self._closed:
            return
        if self.session and self.sock is not None and not self.dead:
            try:
                self.request("TEARDOWN", self._stream_url(), wait=False)
            except OSError:
                pass
        self._closed = True
        for s in (self.sock, self.rtp, self.rtcp):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        with self._cond:
            self._cond.notify_all()

    # receiving
    def _open_udp(self):
        """Bind an even RTP port and the RTCP port after it; "port-port+1"."""
        for _ in range(50):
            port = random.randrange(40000, 60000, 2)
            rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                rtp.bind(("", port))
                rtcp.bind(("", port + 1))
            except OSError:
                rtp.close()
                rtcp.close()
                continue
            try:
                rtp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            self.rtp, self.rtcp = rtp, rtcp
            return f"{port}-{port + 1}"
        raise SatipError("No free UDP ports for the stream.")

    def _loop(self):
        socks = [s for s in (self.sock, self.rtp, self.rtcp) if s is not None]
        try:
            while not self._closed and not self.dead:
                try:
                    ready, _, _ = select.select(socks, [], [], 0.2)
                except (OSError, ValueError):
                    break
                for s in ready:
                    if s is self.sock:
                        data = s.recv(65536)
                        if not data:
                            self.error = "the server closed the connection"
                            self.dead = True
                            break
                        self._rtsp_bytes(data)
                    elif s is self.rtp:
                        self._ts(rtp_payload(s.recv(65536)))
                    else:
                        self._rtcp(s.recv(65536))
                self._flush(force=False)
                if self.session and time.monotonic() - self._last_req > self.keepalive:
                    self.request("OPTIONS", self.base, wait=False)
        except OSError as e:
            if not self._closed:
                self.error = e.strerror or str(e)
        finally:
            with self._cond:
                self.dead = True
                self._cond.notify_all()

    def _rtsp_bytes(self, data):
        """RTSP answers and, with TCP, the interleaved RTP/RTCP frames."""
        self._buf += data
        buf = self._buf
        while buf:
            if buf[:1] == b"$":
                if len(buf) < 4:
                    break
                chan, n = buf[1], struct.unpack_from("!H", buf, 2)[0]
                if len(buf) < 4 + n:
                    break
                frame, buf = buf[4:4 + n], buf[4 + n:]
                if chan == 0:
                    self._ts(rtp_payload(frame))
                elif chan == 1:
                    self._rtcp(frame)
                continue
            if not buf.startswith(b"RTSP"):
                if len(buf) < 4 and b"RTSP".startswith(buf):
                    break
                # neither a frame nor an answer: skip to the next one
                nxt = [i for i in (buf.find(b"$", 1), buf.find(b"RTSP", 1)) if i > 0]
                buf = buf[min(nxt):] if nxt else b""
                continue
            end = buf.find(b"\r\n\r\n")
            if end < 0:
                break
            code, headers, _body = parse_response(buf[:end + 4])
            n = int(headers.get("content-length", "0") or 0)
            if len(buf) < end + 4 + n:
                break
            body = buf[end + 4:end + 4 + n]
            buf = buf[end + 4 + n:]
            try:
                cseq = int(headers.get("cseq", "0"))
            except ValueError:
                cseq = 0
            with self._cond:
                if cseq in self._waiting:
                    self._answers[cseq] = (code, headers, body)
                    self._cond.notify_all()
        self._buf = buf

    def _ts(self, payload):
        if not payload:
            return
        if not self._pending:
            self._pending_t = time.monotonic()
        self._pending += payload
        self._flush(force=False)

    def _flush(self, force=True):
        if self._pending and (force or len(self._pending) >= self.CHUNK or
                              time.monotonic() - self._pending_t > 0.05):
            data, self._pending = bytes(self._pending), bytearray()
            self.on_data(data)

    def _rtcp(self, data):
        for text in rtcp_app_strings(data):
            st = parse_status(text)
            if st:
                self.status = st
                self.on_status(st)


# -- finding servers ---------------------------------------------------------------

def parse_ssdp(data):
    """The headers (lower-case names) of an SSDP answer."""
    out = {}
    for line in data.decode("latin-1", "replace").split("\r\n")[1:]:
        k, _, v = line.partition(":")
        if k:
            out[k.strip().lower()] = v.strip()
    return out


def parse_description(xml_text):
    """(friendly name, UDN, X_SATIPCAP) from a UPnP device description."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "", "", ""
    name = udn = caps = ""
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "friendlyName" and not name:
            name = (el.text or "").strip()
        elif tag == "UDN" and not udn:
            udn = (el.text or "").strip()
        elif tag == "X_SATIPCAP" and not caps:
            caps = (el.text or "").strip()
    return name, udn, caps


def discover(timeout=2.5, fetch=None):
    """SAT>IP servers on the local network (SSDP M-SEARCH), as server dicts
    without an id."""
    fetch = fetch or _fetch
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           f"ST: {SSDP_ST}\r\n"
           f"USER-AGENT: Linux UPnP/1.1 {USER_AGENT}\r\n\r\n").encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    found = {}
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.3)
        for _ in range(2):
            sock.sendto(msg, SSDP_ADDR)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            h = parse_ssdp(data)
            if SSDP_ST not in h.get("st", "") + h.get("nt", ""):
                continue
            loc = h.get("location", "")
            if loc and loc not in found:
                found[loc] = addr[0]
    except OSError:
        pass
    finally:
        sock.close()
    out, seen = [], set()
    for loc, host in found.items():
        name, udn, caps = "", "", ""
        try:
            name, udn, caps = parse_description(fetch(loc))
        except Exception:                # noqa: BLE001 - unreachable description
            pass
        key = udn or host
        if key in seen:
            continue
        seen.add(key)
        out.append({"host": host, "port": DEFAULT_PORT, "name": name or host, "uuid": udn,
                    "caps": caps, "transport": "udp"})
    return out


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=3) as r:      # noqa: S310 - LAN device
        return r.read(256 * 1024).decode("utf-8", "replace")


# -- channel search ----------------------------------------------------------------

def scan(profile, tps, diseqc=-1, root="", log=print, cancelled=lambda: False,
         lock_wait=4.0, read_wait=8.0, session_factory=None):
    """Search the transponders *tps* (lindvb.scscan._Tp like objects) on a SAT>IP tuner.

    Returns the channels found (lindvb.channels.Channel).
    """
    from .scscan import TransponderScan
    make = session_factory or Session
    found = []
    for i, tp in enumerate(tps, 1):
        if cancelled():
            break
        if getattr(tp, "is_sat", False):
            tp.diseqc = diseqc
            log(f"Scanning frequency {i}/{len(tps)}: {tp.frequency // 1000} MHz "
                f"{tp.polarity.upper()} {tp.symbol_rate} kBd")
        else:
            log(f"Scanning frequency {i}/{len(tps)}: {_mhz(tp.frequency)} MHz")
        ts = TransponderScan()
        lock = threading.Lock()
        state = {"new": False, "data": False}

        def data(chunk, ts=ts, state=state):
            with lock:
                state["data"] = True
                if ts.feed(chunk):
                    state["new"] = True

        sess = make(profile["host"], profile.get("port", DEFAULT_PORT),
                    profile.get("transport", "udp"), on_data=data)
        try:
            sess.open(tuning_query(tp, sorted(ts.wanted_pids())))
        except SatipError as e:
            log(f"  {e}")
            continue
        try:
            end = time.monotonic() + lock_wait
            while time.monotonic() < end and not cancelled() and not state["data"] \
                    and not sess.status.get("lock"):
                time.sleep(0.1)
            if not state["data"] and not sess.status.get("lock"):
                log("  no signal")
                continue
            end = time.monotonic() + read_wait
            while time.monotonic() < end and not cancelled():
                with lock:
                    done, new = ts.complete, state["new"]
                    state["new"] = False
                if done:
                    break
                if new:
                    sess.set_pids(sorted(ts.wanted_pids()))
                time.sleep(0.1)
        finally:
            sess.close()
        with lock:
            chans = ts.channels(tp, root, diseqc if getattr(tp, "is_sat", False) else -1)
        for c in chans:
            c.bandwidth_hz = getattr(tp, "bandwidth_hz", 0) or c.bandwidth_hz
        log(f"  {len(chans)} services")
        found += chans
    return found


class ScanTp:
    """A cable or terrestrial transponder for :func:`scan`, from a Channel."""

    def __init__(self, ch):
        self.delsys, self.frequency, self.polarity = ch.delsys, ch.frequency, ""
        self.symbol_rate, self.modulation, self.fec = ch.symbol_rate, ch.modulation, ch.fec
        self.bandwidth_hz, self.stream_id = ch.bandwidth_hz, ch.stream_id
        self.diseqc = -1
        self.is_sat = False


def entry_transponders(channels):
    """Cable / terrestrial transponders from parsed dvbv5 entries, without doubles."""
    out, seen = [], set()
    for c in channels:
        key = (c.delsys, c.frequency // 100, c.stream_id)
        if key in seen or c.is_sat:
            continue
        seen.add(key)
        out.append(ScanTp(c))
    return out
