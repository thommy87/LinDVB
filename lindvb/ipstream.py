# SPDX-License-Identifier: GPL-2.0-or-later
"""Internet streams through the timeshift buffer, and reconnecting them.

Internet TV channels (M3U, Xtream, DVB-I, addresses) used to go straight to
playbin, so pausing one only stopped the download. Now LinDVB reads the
stream itself and writes it into a capture (lindvb.tuner.Capture) like a
tuner's: pause, jumping back, "Keep Timeshift as Recording" and recordings
with teletext and EPG data then work the same way.

How a stream is read depends on what it is:

* MPEG transport stream over HTTP (most M3U and Xtream ``ts`` channels):
  read as it comes (``HttpReader``), every PID kept (teletext, subtitles,
  EIT, the AIT for HbbTV).
* HLS and MPEG-DASH, and anything over HTTP that is not a transport stream:
  GStreamer downloads it (``GstReader``: urisourcebin, the adaptive demuxer,
  parsebin) and mpegtsmux packs the streams into a transport stream. An HLS
  stream made of TS segments is passed on as it is.
* UDP / RTP multicast: GStreamer receives it (``GstReader``), RTP unpacked.

Adaptive streams (HLS / DASH): with the quality "auto" the demuxer still
switches between the variants as the connection allows, and the buffer
keeps what came. HLS and DASH segments start on a key frame, so a change of
picture size inside the buffer plays back without a hitch; a dip stays in
the buffer at the lower quality. "best" keeps the highest variant, which
looks the same all the time but stalls on a slow connection.

A connection that breaks is opened again after 1, 2, 4, 8, 15 and 30
seconds (``RECONNECT_DELAYS``, then every 30 s up to ``RECONNECT_GIVE_UP``
seconds in all); what arrives meanwhile is missing from the buffer. Only a
stream that never delivered anything fails at once, so the caller can fall
back (DVB-I: another instance, otherwise playing it directly).

``StreamReader`` is the reconnecting HTTP reader; lindvb.tvheadend builds
its stream reader (with the server login) on it.

Internet radio goes through the buffer too. The station's song titles (ICY
``StreamTitle``, which GStreamer hands on as tags) would get lost in the
transport stream mpegtsmux makes, so ``GstReader`` writes them into it
itself: one packet on a PID of its own (``META_PID``) whenever the title
changes and again every ``META_REPEAT`` seconds. The player reads them
back at the place it plays (lindvb.player), so a jump back in the buffer
shows the song that ran then. Players that do not know the PID skip it.
"""

import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .i18n import _

# Captures of internet streams get adapter numbers from here up: no
# /dev/dvb adapter, SAT>IP server (100+) or Tvheadend stream (2000+) uses them.
ADAPTER_BASE = 3000
CHUNK = 188 * 348                # about 64 KiB of transport stream packets
CLIENT_NAME = "LinDVB"
TS_PACKET = 188
TS_SYNC = 0x47
REDIRECT_TIMEOUT = 8             # seconds for resolving a gateway redirect
# HTTP answers that mean "not for you": the stream itself is there
REFUSED_CODES = (401, 403, 451)

RECONNECT_DELAYS = (1, 2, 4, 8, 15, 30)     # seconds before each new attempt
RECONNECT_GIVE_UP = 180                     # seconds without a stream before giving up
# a stream that ran this long since its last reconnect starts the delays anew
STABLE_SECONDS = 30

QUALITIES = ("auto", "best")

# song titles in the buffer (see the module notes); mpegtsmux uses low PIDs
META_PID = 0x1FF0
META_MAGIC = b"LinDVB-meta\x01"
META_REPEAT = 2.0               # seconds between repeats of the same title
META_FIELDS = ("title", "organization")
_META_HEAD = bytes((TS_SYNC, 0x40 | META_PID >> 8, META_PID & 0xFF))

# hlsdemux2 / dashdemux2: start on a good quality instead of climbing up
# through every variant (each switch can stall the picture), and only stop
# to buffer when less than a few seconds are left. On ARD alpha the old
# setting stalled once for 2.6 s and went through five picture sizes in 90 s.
ADAPTIVE_DEMUX_SETTINGS = {"start-bitrate": 5_000_000,              # bit/s
                           "low-watermark-time": 3_000_000_000}     # ns
ADAPTIVE_DEMUXERS = ("hlsdemux", "dashdemux", "hlsdemux2", "dashdemux2", "mssdemux",
                     "mssdemux2")


# -- identifying ourselves to a broadcaster -----------------------------------

_user_agent = None


def user_agent():
    """The user agent for stream requests: the same HbbTV terminal agent the
    built-in browser sends (TS 102 796 7.3.2.4).

    A DVB-I service list points at the broadcasters' own CDNs, and several of
    them (ARD's among them) answer 403 Forbidden to a client they do not
    recognise. "LinDVB" alone, or GStreamer's default agent, is such a client;
    the HbbTV agent says what LinDVB is: a TV terminal playing a DVB-I service.
    """
    global _user_agent
    if _user_agent is None:
        try:
            from . import hbbtv
            _user_agent = hbbtv.user_agent(hbbtv.terminal_version())
        except (ImportError, AttributeError):                # pragma: no cover
            _user_agent = CLIENT_NAME
    return _user_agent


def http_headers(extra=None):
    """Request headers for a stream or manifest over HTTP."""
    headers = {"User-Agent": user_agent()}
    headers.update(extra or {})
    return headers


def setup_http_source(src):
    """A GStreamer HTTP source (souphttpsrc, curlhttpsrc) for a stream: send
    our user agent and follow the redirects of a DVB-I gateway."""
    if src.find_property("user-agent") is not None:
        src.set_property("user-agent", user_agent())
    if src.find_property("automatic-redirect") is not None:
        src.set_property("automatic-redirect", True)


class _KeepMethod(urllib.request.HTTPRedirectHandler):
    """Notes the redirect chain instead of swallowing it."""

    def __init__(self, seen, steps):
        self.seen, self.steps = seen, steps

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.seen.append(newurl)
        self.steps.append(f"{code} -> {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def resolve_redirects(url, timeout=REDIRECT_TIMEOUT, prefer_https=True, trace=None):
    """The address a DVB-I gateway URL ends at, following its redirects
    ourselves, or *url* when it does not redirect.

    DVB-I services point at a gateway (``https://itv.ard.de/dvbi/dash/36/…``)
    that redirects to the CDN carrying the stream, often to plain ``http``.
    Resolving the chain here keeps the player on a single hop, and with
    *prefer_https* a target that also answers over https is used encrypted.

    *trace* is a list that collects the status of every request made, so a
    debug log says which hop refuses the stream and over which scheme.
    """
    steps = trace if trace is not None else []
    seen = []
    opener = urllib.request.build_opener(_KeepMethod(seen, steps))
    req = urllib.request.Request(url, headers=http_headers(), method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            final = resp.geturl()
            steps.append(f"{getattr(resp, 'status', 200)} {final}")
    except urllib.error.HTTPError as e:
        final = seen[-1] if seen else url
        steps.append(f"{e.code} {final}")
        e.close()
    except (urllib.error.URLError, OSError, ValueError) as e:
        steps.append(f"failed {url}")
        raise OSError(str(getattr(e, "reason", e)) or e.__class__.__name__) from None
    if not final:
        return url
    if prefer_https and final.startswith("http://") and url.startswith("https://"):
        secure = "https://" + final[len("http://"):]
        ok, status = _answers(secure, timeout)
        steps.append(f"{status} {secure}")
        if ok:
            return secure
    return final


def host_addresses(url, limit=4):
    """The addresses a stream's host resolves to, as text for a debug log.

    A refusal that only hits the country-restricted variants of a stream
    (ZDF's ``/de/`` while ``/dach/`` plays) is about where the connection
    appears to come from, so the next log should say whether it went out
    over IPv6 or IPv4.
    """
    host = urllib.parse.urlsplit(url or "").hostname
    if not host:
        return ""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as e:
        return f"{host}: {e}"
    seen = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return f"{host}: " + ", ".join(seen[:limit])


def _answers(url, timeout=REDIRECT_TIMEOUT):
    """(can *url* be read?, the status or reason of that one request)."""
    req = urllib.request.Request(url, headers=http_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
            return True, getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        e.close()
        return False, e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, str(getattr(e, "reason", e)) or e.__class__.__name__


# -- what a channel is ---------------------------------------------------------

def can_buffer(url):
    """True for addresses LinDVB can read into a capture itself."""
    scheme = urllib.parse.urlsplit(url or "").scheme.lower()
    return scheme in ("http", "https", "udp", "rtp")


def stream_kind(url):
    """"adaptive" (HLS / DASH / Smooth Streaming), "multicast" (UDP / RTP) or
    "http" (anything else over HTTP; usually a transport stream)."""
    parts = urllib.parse.urlsplit(url or "")
    scheme = parts.scheme.lower()
    if scheme in ("udp", "rtp"):
        return "multicast"
    path = (parts.path + "?" + parts.query).lower()
    if ".m3u8" in path or ".mpd" in path or "/manifest" in path or ".ism" in path:
        return "adaptive"
    return "http"


def is_stream(profile):
    """True for the tuner profile of an internet stream capture."""
    return bool(profile) and profile.get("type") == "stream"


def stream_profile(adapter, channel):
    """A tuner profile (lindvb.tunerprofiles) for the capture of one stream."""
    host = urllib.parse.urlsplit(channel.url).hostname or channel.url
    return {"type": "stream", "adapter": adapter, "frontend": 0,
            "name": _("Internet stream {host}").format(host=host)}


def free_adapter(used):
    """The first adapter number for a new stream that *used* does not hold."""
    a = ADAPTER_BASE
    while a in used:
        a += 1
    return a


def looks_like_ts(data):
    """True when *data* starts like an MPEG transport stream: sync bytes 188
    apart (five of them, or all there are in a short piece)."""
    n = len(data)
    if n < 2 * TS_PACKET:
        return n > 0 and data[0] == TS_SYNC and n % TS_PACKET == 0
    for start in range(TS_PACKET):
        if data[start] != TS_SYNC:
            continue
        positions = range(start, min(n, start + TS_PACKET * 5), TS_PACKET)
        if len(positions) >= 2 and all(data[i] == TS_SYNC for i in positions):
            return True
    return False


# -- song titles in the buffer -------------------------------------------------

def meta_packet(fields, cc=0):
    """One transport stream packet that carries *fields* (title, organization).

    The payload is ``META_MAGIC``, one length byte and ``key=value`` lines in
    UTF-8; a title too long for the packet is cut short."""
    room = TS_PACKET - 4 - len(META_MAGIC) - 1
    lines = []
    for key in META_FIELDS:
        value = " ".join(str(fields.get(key) or "").split())
        if value:
            lines.append(f"{key}={value}")
    body = "\n".join(lines).encode("utf-8")
    if len(body) > room:
        body = body[:room].decode("utf-8", "ignore").encode("utf-8")
    payload = META_MAGIC + bytes((len(body),)) + body
    payload += b"\xff" * (TS_PACKET - 4 - len(payload))
    return _META_HEAD + bytes((0x10 | (cc & 0x0F),)) + payload


def parse_meta_packet(packet):
    """The fields of a packet made by meta_packet(), or None."""
    if len(packet) < TS_PACKET or packet[:3] != _META_HEAD:
        return None
    start = 4
    if packet[start:start + len(META_MAGIC)] != META_MAGIC:
        return None
    start += len(META_MAGIC)
    n = packet[start]
    body = bytes(packet[start + 1:start + 1 + n]).decode("utf-8", "replace")
    fields = {}
    for line in body.split("\n"):
        key, sep, value = line.partition("=")
        if sep and key in META_FIELDS and value:
            fields[key] = value
    return fields


def find_meta(data):
    """[(position, fields)] of the song title packets in *data* (a piece of
    transport stream, aligned to its packets or not)."""
    found = []
    i = data.find(_META_HEAD)
    while 0 <= i <= len(data) - TS_PACKET:
        fields = parse_meta_packet(data[i:i + TS_PACKET])
        if fields is not None:
            found.append((i, fields))
            i += TS_PACKET
        else:
            i += 1
        i = data.find(_META_HEAD, i)
    return found


def tag_fields(taglist):
    """Title and station name from a GStreamer tag list (Gst.TAG_TITLE and
    Gst.TAG_ORGANIZATION are "title" and "organization")."""
    out = {}
    for key in META_FIELDS:
        ok, value = taglist.get_string(key)
        if ok and value and value.strip():
            out[key] = value.strip()
    return out


def multicast_source(url):
    """(udp uri, SSM source or None, rtp?) of a udp:// or rtp:// address
    (rtp://source@group:port is source-specific multicast)."""
    import re
    m = re.match(r"(rtp|udp)://(?:([^@/]+)@)?([^:/]+):(\d+)", url or "")
    if not m:
        return url, None, (url or "").startswith("rtp://")
    return f"udp://{m.group(3)}:{m.group(4)}", m.group(2), m.group(1) == "rtp"


class NotTransportStream(Exception):
    """The address answers with something else than a transport stream."""


class Refused(OSError):
    """The server refused the request (HTTP 401, 403 or 451)."""


class Retry:
    """The reconnect delays of one stream: ``next()`` gives the delay before
    the next attempt, or None when it is time to give up."""

    def __init__(self, delays=RECONNECT_DELAYS, give_up=RECONNECT_GIVE_UP,
                 stable=STABLE_SECONDS, clock=time.monotonic):
        self.delays = tuple(delays)
        self.give_up = give_up
        self.stable = stable
        self.clock = clock
        self.attempt = 0
        self.lost_at = None          # when the stream broke (first failed attempt)
        self.up_since = None         # when data flowed again

    def running(self):
        """Data arrives (again)."""
        if self.up_since is None:
            self.up_since = self.clock()

    def broken(self):
        """The connection broke: the delay before the next attempt, or None."""
        now = self.clock()
        if self.up_since is not None and now - self.up_since >= self.stable:
            self.attempt, self.lost_at = 0, None      # it ran fine for a while
        self.up_since = None
        if self.lost_at is None:
            self.lost_at = now
        if now - self.lost_at >= self.give_up:
            return None
        d = self.delays[min(self.attempt, len(self.delays) - 1)]
        self.attempt += 1
        return d


# -- HTTP ------------------------------------------------------------------------

class StreamReader:
    """Reads a stream over HTTP in a thread and hands it on in chunks.

    *on_data(bytes)* gets the stream. *on_status(kind, message)* reports
    ``"reconnecting"`` (the connection broke, another attempt follows) and
    ``"reconnected"``. *on_error(message)* ends it: the first connection
    failed, or reconnecting gave up (never after close()). *on_other()*
    replaces on_error when the address answers with something that is not a
    transport stream (when *expect_ts* is set).
    """

    name = "stream"

    def __init__(self, url, on_data, on_error=None, on_status=None, on_other=None,
                 reconnect=True, expect_ts=False, retry=None):
        self.url = url
        self.on_data = on_data
        self.on_error = on_error
        self.on_status = on_status
        self.on_other = on_other
        self.reconnect = reconnect
        self.expect_ts = expect_ts
        self.retry = retry or Retry()
        self.closed = False
        self.received = 0
        self.reconnects = 0
        self._resp = None
        self._thread = None
        self._wake = threading.Event()

    # subclasses: open the connection (a response with read()/read1())
    def _open(self):
        try:
            return self._request(self.url)
        except Refused as e:
            # a gateway that redirects to a CDN refusing us: follow the
            # redirect ourselves and ask the CDN straight away (DVB-I)
            try:
                target = resolve_redirects(self.url)
            except OSError:
                raise OSError(str(e)) from None
            if target == self.url:
                raise OSError(str(e)) from None
            self.url = target
            return self._request(target)

    def _request(self, url):
        req = urllib.request.Request(url, headers=http_headers())
        try:
            return urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as e:
            text = f"HTTP {e.code} {e.reason}"
            e.close()
            if e.code in REFUSED_CODES:
                raise Refused(text) from None
            raise OSError(text) from None
        except urllib.error.URLError as e:
            raise OSError(str(e.reason)) from None

    @property
    def host(self):
        return urllib.parse.urlsplit(self.url).netloc

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"{self.name}-stream")
        self._thread.start()

    def _run(self):
        first = True
        while not self.closed:
            reason = ""
            try:
                self._session(first)
                reason = _("the server ended the stream")
            except NotTransportStream:
                if not self.closed and self.on_other is not None:
                    self.on_other()
                return
            except (OSError, ValueError) as e:
                reason = str(e) or e.__class__.__name__
            if self.closed:
                return
            if first and not self.received:
                self._fail(f"{self.host}: {reason}")
                return
            first = False
            delay = self.retry.broken() if self.reconnect else None
            if delay is None:
                self._fail(f"{self.host}: {reason}")
                return
            self.reconnects += 1
            self._status("reconnecting", _("{host}: {reason} – connecting again in {s} s").format(
                host=self.host, reason=reason, s=delay))
            self._wake.wait(delay)
            self._wake.clear()

    def _session(self, first):
        resp = self._open()
        self._resp = resp
        if self.closed:
            self._close_resp(resp)
            return
        checked = not (first and self.expect_ts)
        head = b""
        try:
            while not self.closed:
                data = resp.read1(CHUNK) if hasattr(resp, "read1") else resp.read(CHUNK)
                if not checked:
                    # look at the first few packets before passing anything on
                    head += data
                    if data and len(head) < 5 * TS_PACKET:
                        continue
                    if not head:
                        return
                    if not looks_like_ts(head):
                        raise NotTransportStream()
                    checked, data = True, head
                if not data:
                    return
                if self.retry.up_since is None:
                    self.retry.running()
                    if self.reconnects:
                        self._status("reconnected", _("{host}: connected again").format(
                            host=self.host))
                self.received += len(data)
                self.on_data(data)
        finally:
            self._close_resp(resp)

    @staticmethod
    def _close_resp(resp):
        try:
            resp.close()
        except OSError:
            pass

    def _status(self, kind, message):
        if not self.closed and self.on_status is not None:
            self.on_status(kind, message)

    def _fail(self, message):
        if not self.closed and self.on_error is not None:
            self.on_error(message)

    def close(self):
        """Stop reading (the server frees what it holds for the connection)."""
        self.closed = True
        self._wake.set()
        resp = self._resp
        if resp is None:
            return
        import socket
        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)      # wakes up the blocked read
        except OSError:
            pass


HttpReader = StreamReader


# -- GStreamer -----------------------------------------------------------------------

class GstReader:
    """Receives a stream with GStreamer and hands on a transport stream.

    HLS, DASH, other HTTP media and UDP/RTP multicast: urisourcebin fetches
    it, parsebin takes it apart; a transport stream (multicast, HLS with TS
    segments) is passed on unchanged, other streams are packed by mpegtsmux.
    Same callbacks as StreamReader; errors and the end of the stream lead to
    a new pipeline after the reconnect delays. The bus is watched on the
    GLib main loop.

    With *meta* set (internet radio), the song title and station name the
    stream sends as tags go into the packed transport stream as packets of
    their own (meta_packet).
    """

    def __init__(self, url, on_data, on_error=None, on_status=None, quality="auto",
                 reconnect=True, retry=None, meta=False):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
        self.Gst, self.GLib = Gst, GLib
        if not Gst.is_initialized():
            Gst.init(None)
        self.url = url
        self.on_data = on_data
        self.on_error = on_error
        self.on_status = on_status
        self.quality = quality if quality in QUALITIES else "auto"
        self.reconnect = reconnect
        self.retry = retry or Retry()
        self.closed = False
        self.received = 0
        self.reconnects = 0
        self.passthrough = None          # True: TS passed on; False: remuxed
        self.pipeline = None
        self._gen = 0                    # pipeline generation (stale bus messages)
        self._timer = 0
        self._lock = threading.Lock()
        self.meta = meta
        self.meta_fields = {}            # the latest title and station name
        self._meta_due = False           # changed since the last packet
        self._meta_sent = 0.0            # time.monotonic() of the last packet
        self._meta_cc = 0

    @property
    def host(self):
        parts = urllib.parse.urlsplit(self.url)
        return parts.netloc or self.url

    def start(self):
        self._build()

    # -- pipeline ----------------------------------------------------------------
    def _build(self):
        Gst = self.Gst
        self._gen += 1
        gen = self._gen
        self.passthrough = None
        pl = Gst.Pipeline.new(f"ipstream{gen}")
        src = Gst.ElementFactory.make("urisourcebin")
        if src is None:
            self._fail("GStreamer element 'urisourcebin' is missing (gst-plugins-base)")
            return
        uri, ssm, rtp = multicast_source(self.url) if stream_kind(self.url) == "multicast" \
            else (self.url, None, False)
        src.set_property("uri", uri)
        src.connect("source-setup", self._on_source_setup, ssm, rtp)
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        sink.set_property("max-buffers", 0)
        sink.connect("new-sample", self._on_sample, gen)
        pl.add(src)
        pl.add(sink)
        self._sink = sink
        self._mux = None
        pl.connect("deep-element-added", self._on_element)
        src.connect("pad-added", self._on_source_pad, pl)
        bus = pl.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message, gen)
        self.pipeline = pl
        if pl.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._broken(gen, _("could not open the stream"))

    def _on_source_setup(self, _bin, source, ssm, rtp):
        Gst = self.Gst
        f = source.get_factory()
        name = f.get_name() if f else ""
        if name == "udpsrc":
            if ssm and source.find_property("multicast-source"):
                source.set_property("multicast-source", ssm)
            if rtp:
                source.set_property("caps", Gst.Caps.from_string(
                    "application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T"))
        else:
            setup_http_source(source)

    def _on_element(self, _pl, _bin, element):
        """The adaptive demuxer: the variant choice (auto or the best one)."""
        f = element.get_factory()
        if f is None or f.get_name() not in ADAPTIVE_DEMUXERS:
            return
        settings = dict(ADAPTIVE_DEMUX_SETTINGS)
        if self.quality == "best":
            # a fixed (very high) connection speed makes the demuxer take
            # the best variant and stay there
            for prop in ("connection-bitrate", "connection-speed", "start-bitrate"):
                spec = element.find_property(prop)
                if spec is not None:
                    settings[prop] = getattr(spec, "maximum", 0) or 0
        for k, v in settings.items():
            if element.find_property(k) is not None and v:
                try:
                    element.set_property(k, v)
                except (TypeError, OverflowError, ValueError):
                    pass

    def _on_source_pad(self, _src, pad, pl):
        Gst = self.Gst
        parse = Gst.ElementFactory.make("parsebin")
        # a transport stream stays one: parsebin hands it on without tsdemux
        parse.connect("autoplug-continue", self._autoplug_continue)
        parse.connect("pad-added", self._on_parsed_pad, pl)
        pl.add(parse)
        parse.sync_state_with_parent()
        pad.link(parse.get_static_pad("sink"))

    @staticmethod
    def _autoplug_continue(_bin, _pad, caps):
        s = caps.get_structure(0) if caps and caps.get_size() else None
        return not (s is not None and s.get_name() == "video/mpegts")

    def _caps_name(self, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        return caps.get_structure(0).get_name() if caps and caps.get_size() else ""

    def _on_tag_event(self, _pad, info):
        """Streaming thread: keep the song title and station name of the stream."""
        ev = info.get_event()
        if ev is not None and ev.type == self.Gst.EventType.TAG:
            new = tag_fields(ev.parse_tag())
            with self._lock:
                fields = dict(self.meta_fields, **new)
                if fields != self.meta_fields:
                    self.meta_fields = fields
                    self._meta_due = True
        return self.Gst.PadProbeReturn.OK

    def _meta_out(self, data):
        """*data* with a song title packet in front when one is due."""
        if not self.meta or self.passthrough is not False or not data or \
                data[0] != TS_SYNC or len(data) % TS_PACKET:
            return data
        now = time.monotonic()
        with self._lock:
            fields = self.meta_fields
            if not fields or not (self._meta_due or now - self._meta_sent >= META_REPEAT):
                return data
            self._meta_due, self._meta_sent = False, now
            cc, self._meta_cc = self._meta_cc, (self._meta_cc + 1) & 0x0F
        return meta_packet(fields, cc) + data

    def _on_parsed_pad(self, _parse, pad, pl):
        Gst = self.Gst
        name = self._caps_name(pad)
        if self.meta:
            pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_tag_event)
        with self._lock:
            if name == "video/mpegts" and self.passthrough is None:
                self.passthrough = True
                q = Gst.ElementFactory.make("queue")
                pl.add(q)
                q.sync_state_with_parent()
                pad.link(q.get_static_pad("sink"))
                q.link(self._sink)
                return
            if self.passthrough:
                self._drop(pad, pl)
                return
            self.passthrough = False
            if self._mux is None:
                self._mux = Gst.ElementFactory.make("mpegtsmux")
                if self._mux.find_property("alignment") is not None:
                    self._mux.set_property("alignment", 7)
                pl.add(self._mux)
                self._mux.link(self._sink)
                self._mux.sync_state_with_parent()
            chain = [Gst.ElementFactory.make("queue")]
            # H.264 / H.265 as mpegtsmux takes it (byte-stream), with the
            # parameter sets before every key frame: a jump back in the buffer
            # and a change of picture size (another variant) decode at once
            fmt = {"video/x-h264": "h264parse", "video/x-h265": "h265parse"}.get(name)
            if fmt and Gst.ElementFactory.find(fmt):
                parser = Gst.ElementFactory.make(fmt)
                parser.set_property("config-interval", -1)
                cf = Gst.ElementFactory.make("capsfilter")
                cf.set_property("caps", Gst.Caps.from_string(
                    f"{name},stream-format=byte-stream,alignment=au"))
                chain += [parser, cf]
            for e in chain:
                pl.add(e)
            for a, b in zip(chain, chain[1:]):
                a.link(b)
            for e in chain:
                e.sync_state_with_parent()
            pad.link(chain[0].get_static_pad("sink"))
            out = chain[-1].get_static_pad("src")
            tmpl = self._mux.get_pad_template("sink_%d")
            caps = pad.get_current_caps() or pad.query_caps(None)
            mpad = None
            if fmt or (tmpl is not None and caps is not None and
                       caps.can_intersect(tmpl.get_caps())):
                mpad = self._mux.request_pad_simple("sink_%d")
            if mpad is None or out.link(mpad) != Gst.PadLinkReturn.OK:
                # a stream mpegtsmux does not take (e.g. TTML subtitles): drop it
                if mpad is not None:
                    self._mux.release_request_pad(mpad)
                fake = Gst.ElementFactory.make("fakesink")
                fake.set_property("sync", False)
                fake.set_property("async", False)
                pl.add(fake)
                fake.sync_state_with_parent()
                out.link(fake.get_static_pad("sink"))

    def _drop(self, pad, pl):
        fake = self.Gst.ElementFactory.make("fakesink")
        fake.set_property("sync", False)
        fake.set_property("async", False)
        pl.add(fake)
        fake.sync_state_with_parent()
        pad.link(fake.get_static_pad("sink"))

    def _on_sample(self, sink, gen):
        Gst = self.Gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        if self.closed or gen != self._gen:
            return Gst.FlowReturn.FLUSHING
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        received = len(data)
        data = self._meta_out(data)
        if self.retry.up_since is None:
            self.retry.running()
            if self.reconnects:
                self.GLib.idle_add(self._status, "reconnected",
                                   _("{host}: connected again").format(host=self.host))
        self.received += received
        self.on_data(data)
        return Gst.FlowReturn.OK

    def _on_message(self, _bus, msg, gen):
        Gst = self.Gst
        if gen != self._gen or self.closed:
            return
        if msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            self._broken(gen, err.message)
        elif msg.type == Gst.MessageType.EOS:
            self._broken(gen, _("the server ended the stream"))

    def _teardown(self):
        pl, self.pipeline = self.pipeline, None
        if pl is not None:
            pl.get_bus().remove_signal_watch()
            pl.set_state(self.Gst.State.NULL)

    def _broken(self, gen, reason):
        if gen != self._gen or self.closed:
            return
        self._gen += 1                   # ignore what the old pipeline still says
        self._teardown()
        if not self.received:
            self._fail(f"{self.host}: {reason}")
            return
        delay = self.retry.broken() if self.reconnect else None
        if delay is None:
            self._fail(f"{self.host}: {reason}")
            return
        self.reconnects += 1
        self._status("reconnecting", _("{host}: {reason} – connecting again in {s} s").format(
            host=self.host, reason=reason, s=delay))
        self._timer = self.GLib.timeout_add(int(delay * 1000), self._again)

    def _again(self):
        self._timer = 0
        if not self.closed:
            self._build()
        return False

    def _status(self, kind, message):
        if not self.closed and self.on_status is not None:
            self.on_status(kind, message)
        return False

    def _fail(self, message):
        if not self.closed and self.on_error is not None:
            self.on_error(message)

    def close(self):
        self.closed = True
        if self._timer:
            self.GLib.source_remove(self._timer)
            self._timer = 0
        self._teardown()


# -- the source of a capture -------------------------------------------------------

class StreamSource:
    """What a capture reads an internet channel with: picks the reader for
    the address and switches from HTTP to GStreamer when an HTTP address is
    not a transport stream. Same interface as the readers. *meta*: write the
    song titles into the stream (internet radio; see GstReader)."""

    def __init__(self, url, on_data, on_error=None, on_status=None, quality="auto",
                 reconnect=True, meta=False):
        self.url = url
        self.meta = meta
        self.on_data = on_data
        self.on_error = on_error
        self.on_status = on_status
        self.quality = quality
        self.reconnect = reconnect
        self.closed = False
        self.kind = stream_kind(url)
        self.reader = None

    @property
    def received(self):
        return self.reader.received if self.reader is not None else 0

    def start(self):
        if self.kind == "http":
            self.reader = StreamReader(self.url, self.on_data, self.on_error, self.on_status,
                                       on_other=self._other, reconnect=self.reconnect,
                                       expect_ts=True)
            self.reader.start()
        else:
            self._start_gst()

    def _start_gst(self):
        if self.closed:
            return False
        try:
            self.reader = GstReader(self.url, self.on_data, self.on_error, self.on_status,
                                    quality=self.quality, reconnect=self.reconnect,
                                    meta=self.meta)
        except (ImportError, ValueError) as e:
            if self.on_error is not None:
                self.on_error(str(e))
            return False
        self.reader.start()
        return False

    def _other(self):
        """HTTP reader thread: not a transport stream; GStreamer takes it apart."""
        from gi.repository import GLib
        GLib.idle_add(self._start_gst)

    def close(self):
        self.closed = True
        if self.reader is not None:
            self.reader.close()
