# SPDX-License-Identifier: GPL-2.0-or-later
"""A small, read-only client of Tvheadend's own protocol HTSP (port 9982).

LinDVB takes the channels and streams of a Tvheadend server over HTTP
(lindvb.tvheadend). What HTTP does not tell a user without admin rights is
which network (satellite position, cable or antenna network) and which
services a channel comes from; HTSP does, to every user who may stream: a
channel's ``services`` carry the service's name as "network/mux/service".
This module logs in, asks for the channel list (``enableAsyncMetadata`` up
to ``initialSyncCompleted``) and hangs up. Streams and the guide stay on HTTP.

HTSP messages (htsmsg) are length-prefixed maps of typed fields::

    message := length:u32be field*
    field   := type:u8 namelength:u8 datalength:u32be name data

with the types map, signed integer (little endian, as few bytes as needed),
string, binary and list (a map whose fields have no names).

No GTK.
"""

import hashlib
import socket
import struct

PORT = 9982
VERSION = 34                         # the HTSP version LinDVB speaks (the server may speak less)
MAX_MESSAGE = 16 * 1024 * 1024
MAP, S64, STR, BIN, LIST = 1, 2, 3, 4, 5


class HtspError(Exception):
    pass


# -- htsmsg ------------------------------------------------------------------------

def encode(msg):
    """The bytes of the message *msg* (a dict), with its length in front."""
    body = b"".join(_field(k, v) for k, v in msg.items())
    return struct.pack(">I", len(body)) + body


def _field(name, value):
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        kind, data = S64, (value & (2 ** 64 - 1)).to_bytes(8, "little").rstrip(b"\0")
    elif isinstance(value, str):
        kind, data = STR, value.encode()
    elif isinstance(value, (bytes, bytearray)):
        kind, data = BIN, bytes(value)
    elif isinstance(value, dict):
        kind, data = MAP, b"".join(_field(k, v) for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        kind, data = LIST, b"".join(_field("", v) for v in value)
    else:
        raise TypeError(f"htsmsg has no field type for {type(value).__name__}")
    name = name.encode()
    return struct.pack(">BBI", kind, len(name), len(data)) + name + data


def decode(body):
    """The message (a dict) in *body*, the bytes after the length."""
    return _fields(body, False)


def _fields(data, as_list):
    out, i = ([] if as_list else {}), 0
    while i + 6 <= len(data):
        kind, nlen, dlen = struct.unpack_from(">BBI", data, i)
        i += 6
        name = data[i:i + nlen].decode("utf-8", "replace")
        value = data[i + nlen:i + nlen + dlen]
        i += nlen + dlen
        if kind == S64:
            v = int.from_bytes(value, "little")
            value = v - 2 ** 64 if len(value) == 8 and v >= 2 ** 63 else v
        elif kind == STR:
            value = value.decode("utf-8", "replace")
        elif kind in (MAP, LIST):
            value = _fields(value, kind == LIST)
        elif kind != BIN:
            continue                 # a type this client does not need
        if as_list:
            out.append(value)
        else:
            out[name] = value
    return out


# -- the connection ----------------------------------------------------------------

class Connection:
    def __init__(self, host, port=PORT, timeout=10):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise HtspError(f"{host}:{port}: {e.strerror or e}") from e
        self.seq = 0

    def close(self):
        self.sock.close()

    def send(self, method, **args):
        self.seq += 1
        self.sock.sendall(encode({"method": method, "seq": self.seq, **args}))
        return self.seq

    def read(self):
        size = struct.unpack(">I", self._exactly(4))[0]
        if size > MAX_MESSAGE:
            raise HtspError("message too long")
        return decode(self._exactly(size))

    def call(self, method, **args):
        """Send a request and return its reply (messages in between are dropped)."""
        seq = self.send(method, **args)
        while True:
            msg = self.read()
            if msg.get("seq") == seq:
                if msg.get("error"):
                    raise HtspError(f"{method}: {msg['error']}")
                return msg

    def _exactly(self, n):
        buf = bytearray()
        while len(buf) < n:
            try:
                part = self.sock.recv(n - len(buf))
            except OSError as e:
                raise HtspError(str(e)) from e
            if not part:
                raise HtspError("the server closed the connection")
            buf += part
        return bytes(buf)


def login(conn, user="", password=""):
    """hello and, with a user name, authenticate (SHA-1 of password and the
    server's challenge). Raises HtspError when the server refuses."""
    hello = conn.call("hello", htspversion=VERSION, clientname="LinDVB",
                      **({"username": user} if user else {}))
    if user:
        digest = hashlib.sha1(password.encode() + hello.get("challenge", b"")).digest()
        reply = conn.call("authenticate", username=user, digest=digest)
        if reply.get("noaccess"):
            raise HtspError("wrong user name or password")
    return hello


def channels(host, user="", password="", port=PORT, timeout=10):
    """The channels the user may watch, as the server's channelAdd messages
    (channelName, channelNumber, tags, services: [{name, type, …}], …)."""
    conn = Connection(host, port, timeout)
    try:
        login(conn, user, password)
        conn.send("enableAsyncMetadata")
        out = []
        while True:
            msg = conn.read()
            method = msg.get("method")
            if method == "channelAdd":
                out.append(msg)
            elif method == "initialSyncCompleted":
                return out
            elif msg.get("noaccess"):
                raise HtspError("wrong user name or password")
    finally:
        conn.close()


def network_of(service_name):
    """The network of a service named "network/mux/service" ("" when it is
    not named so)."""
    parts = service_name.split("/")
    return parts[0].strip() if len(parts) >= 3 else ""
