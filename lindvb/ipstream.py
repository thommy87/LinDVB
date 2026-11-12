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
"""

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
USER_AGENT = "LinDVB"
TS_PACKET = 188
TS_SYNC = 0x47

RECONNECT_DELAYS = (1, 2, 4, 8, 15, 30)     # seconds before each new attempt
RECONNECT_GIVE_UP = 180                     # seconds without a stream before giving up
# a stream that ran this long since its last reconnect starts the delays anew
STABLE_SECONDS = 30

QUALITIES = ("auto", "best")

# hlsdemux2 / dashdemux2: start on a good quality instead of climbing up
# through every variant (each switch can stall the picture), and only stop
# to buffer when less than a few seconds are left. On ARD alpha the old
# setting stalled once for 2.6 s and went through five picture sizes in 90 s.
ADAPTIVE_DEMUX_SETTINGS = {"start-bitrate": 5_000_000,              # bit/s
                           "low-watermark-time": 3_000_000_000}     # ns
ADAPTIVE_DEMUXERS = ("hlsdemux", "dashdemux", "hlsdemux2", "dashdemux2", "mssdemux",
                     "mssdemux2")


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
        req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
        try:
            return urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as e:
            raise OSError(f"HTTP {e.code} {e.reason}") from None
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
    """

    def __init__(self, url, on_data, on_error=None, on_status=None, quality="auto",
                 reconnect=True, retry=None):
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
        elif source.find_property("user-agent") is not None:
            source.set_property("user-agent", USER_AGENT)

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

    def _on_parsed_pad(self, _parse, pad, pl):
        Gst = self.Gst
        name = self._caps_name(pad)
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
        if self.retry.up_since is None:
            self.retry.running()
            if self.reconnects:
                self.GLib.idle_add(self._status, "reconnected",
                                   _("{host}: connected again").format(host=self.host))
        self.received += len(data)
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
    not a transport stream. Same interface as the readers."""

    def __init__(self, url, on_data, on_error=None, on_status=None, quality="auto",
                 reconnect=True):
        self.url = url
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
                                    quality=self.quality, reconnect=self.reconnect)
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
