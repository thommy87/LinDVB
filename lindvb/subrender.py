# SPDX-License-Identifier: GPL-2.0-or-later
"""The subtitle renderers (stage F of the concept "Streams, Timeshift,
Untertitel und Zusatzton", section 5).

Every kind of subtitle gets its own renderer behind the same small
interface:

* ``can(track)``: the renderer shows this track of the track catalog
  (lindvb.trackcatalog.Track; its format decides),
* ``show(track)``: show it,
* ``off()``: show nothing and take in no more data,
* ``state()``: a :class:`State`: on or off, the track, whether data arrives
  and whether text was decoded.

There are five: DVB subtitles (bitmaps), teletext (teletextdec),
CEA-608, CEA-708 and text cues (WebVTT/TTML). Exactly one is on
(:class:`Switch`); "Off" switches all of them off. Renderers do not know
each other and share nothing but which one is on.

No renderer sits in the video's clock path: each hangs on a branch of its
own behind a leaky queue (``leaky=downstream``), so a renderer that stands
loses subtitles but never holds up picture or sound.

The renderers take over one at a time; until one does, the old path of the
player stays and the renderer runs in its shadow when debug mode is on
(:class:`Shadow` writes what the two showed to the debug log). Teletext is
the first.
"""

import bisect
from dataclasses import dataclass

TELETEXT = "Teletext"            # lindvb.trackcatalog formats
DVB_SUB = "DVB-SUB"
WEBVTT = "WebVTT"
TTML = "TTML"
CEA608 = "CEA-608"
CEA708 = "CEA-708"

# buffers a renderer's queue holds before it throws the oldest away:
# teletext comes in a few PES packets a frame, so about two seconds
QUEUE_BUFFERS = 100
# text cues come as whole WebVTT segments (one a few seconds)
CUE_QUEUE_BUFFERS = 30
# what playsink hands its overlay, and the parser the old path takes for it
# (subtitleoverlay: subparse; ttmlparse once lindvb.player.enable_ttml gave
# it a rank)
CUE_PARSERS = {
    "application/x-subtitle-vtt": ("subparse", WEBVTT),
    "application/x-subtitle": ("subparse", WEBVTT),
    "application/ttml+xml": ("ttmlparse", TTML),
}
CUE_CAPS = tuple(CUE_PARSERS)
# a cue shown by both paths counts as on time when they agree this closely
CUE_TOLERANCE_NS = 40_000_000
# a cue that names no end is drawn this long at most
CUE_DEFAULT_NS = 8_000_000_000
# cues the drawer keeps (a few minutes of subtitles)
CUE_KEEP = 500
# the drawer looks at the playback position at least this often while on,
# and never more often than this (ms)
DRAW_TICK_MAX_MS = 250
DRAW_TICK_MIN_MS = 10
# after a start or a seek it looks every 40 ms (a frame at 25 Hz) until the
# position goes at the playback speed again, measured over 200 ms; standing
# (paused) it stops looking closely after ten such measures (two seconds)
DRAW_TICK_FAST_MS = 40
DRAW_PACE_NS = 200_000_000
DRAW_FAST_LOOKS = 10
# a cue drawn this soon after its time is on time: a frame or two and the main loop
DRAW_TOLERANCE_NS = 100_000_000


@dataclass
class State:
    """What a renderer reports (concept: "whether data arrives and whether
    text was decoded")."""

    kind: str
    on: bool = False
    track: str = ""              # the origin of the track shown ("ttx:150"), "" when off
    data: int = 0                # buffers taken in since it went on (CEA-608: byte pairs
                                 # of its field, CEA-708: frames with DTVCC data; no padding)
    decoded: int = 0             # texts decoded since it went on
    dropped: int = 0             # times its queue was full and threw data away
    last: str = ""               # the text shown last


class Renderer:
    """The interface; a renderer of *kind* shows the tracks of that format."""

    kind = ""

    def can(self, track):
        return getattr(track, "format", "") == self.kind

    def show(self, track):
        raise NotImplementedError

    def off(self):
        raise NotImplementedError

    def state(self):
        raise NotImplementedError


class Switch:
    """Exactly one renderer is on."""

    def __init__(self, renderers=()):
        self.renderers = list(renderers)

    def renderer_for(self, track):
        return next((r for r in self.renderers if track is not None and r.can(track)), None)

    def show(self, track):
        """Show *track* with its renderer and switch all others off; a track
        no renderer can show switches everything off. Returns the renderer."""
        r = self.renderer_for(track)
        for other in self.renderers:
            if other is not r and other.state().on:
                other.off()
        if r is not None:
            r.show(track)
        return r

    def off(self):
        for r in self.renderers:
            r.off()

    @property
    def active(self):
        return next((r for r in self.renderers if r.state().on), None)


def teletext_text(data):
    """The subtitle text of a teletextdec buffer: its rows, trimmed."""
    text = bytes(data).decode("utf-8", "replace").replace("\x00", "")
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())


def _page(track):
    origin = getattr(track, "origin", "")
    try:
        return int(origin.split(":", 1)[1]) if origin.startswith("ttx:") else 0
    except ValueError:
        return 0


class TeletextRenderer(Renderer):
    """Teletext subtitle pages, decoded by teletextdec.

    ``element`` is a bin to link the teletext pad of tsdemux to: leaky
    queue, teletextdec, appsink. *on_text(text)* gets each new text and ""
    when the page goes off; it is called from a streaming thread.

    *warm*: keep decoding while off (showing nothing), as the old path does.
    The stream is demultiplexed ahead of the picture, so a renderer that
    starts taking in data only when it goes on misses the text already on
    its way (up to about a second), which the old path shows."""

    kind = TELETEXT

    def __init__(self, on_text, queue_buffers=QUEUE_BUFFERS, sync=True, warm=False,
                 page=100):
        from gi.repository import Gst
        self._on_text = on_text
        self._state = State(TELETEXT)
        self.element = Gst.Bin.new("teletext-renderer")
        q = Gst.ElementFactory.make("queue")
        q.set_property("leaky", 2)                       # downstream: drop the oldest
        q.set_property("max-size-buffers", queue_buffers)
        q.set_property("max-size-time", 0)
        q.set_property("max-size-bytes", 0)
        q.connect("overrun", self._overrun)
        dec = Gst.ElementFactory.make("teletextdec")
        dec.set_property("page", page)
        dec.set_property("subtitles-mode", True)
        dec.set_property("subtitles-template", "%s\n")
        cf = Gst.ElementFactory.make("capsfilter")
        cf.set_property("caps", Gst.Caps.from_string("text/x-raw,format=utf-8"))
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("sync", sync)
        sink.set_property("async", False)
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 4)
        sink.set_property("drop", True)
        sink.connect("new-sample", self._on_sample)
        chain = (q, dec, cf, sink)
        for e in chain:
            self.element.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        pad = q.get_static_pad("sink")
        self.element.add_pad(Gst.GhostPad.new("sink", pad))
        # "Off" takes in no data (unless warm): buffers are dropped before the queue
        pad.add_probe(Gst.PadProbeType.BUFFER, self._intake)
        self._dec = dec
        self.warm = warm

    # -- the interface ---------------------------------------------------------------

    def show(self, track):
        page = _page(track)
        if not page:
            self.off()
            return
        self._dec.set_property("page", page)
        self._state = State(TELETEXT, on=True, track=track.origin)

    def off(self):
        was = self._state
        self._state = State(TELETEXT)
        if was.on and was.last:
            self._on_text("")

    def state(self):
        return self._state

    # -- streaming threads -------------------------------------------------------------

    def _intake(self, _pad, _info):
        from gi.repository import Gst
        s = self._state
        if s.on:
            s.data += 1
        elif not self.warm:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def _overrun(self, _queue):
        self._state.dropped += 1

    def _on_sample(self, sink):
        from gi.repository import Gst
        sample = sink.emit("pull-sample")
        s = self._state
        if sample is None or not s.on:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        text = teletext_text(info.data)
        buf.unmap(info)
        if text != s.last:
            s.last = text
            s.decoded += 1
            self._on_text(text)
        return Gst.FlowReturn.OK


# -- CEA-608 (lindvb.cc608) ------------------------------------------------------------

# byte pairs a CEA-608 renderer holds before it throws the oldest away:
# one or two a frame, so about ten seconds
CC_QUEUE_PAIRS = 600
# the CEA-608 renderer decodes what came this often (main loop)
CC_DECODE_MS = 100


def cc608_pairs(data, fmt="cc_data"):
    """[(field, b1, b2)] of one buffer of a caption pad (lindvb.captions
    formats: cc_data and cdp already turned into cc_data, s334-1a, raw)."""
    out = []
    if fmt == "raw":
        return [(1, data[i], data[i + 1]) for i in range(0, len(data) - 1, 2)]
    for i in range(0, len(data) - 2, 3):
        b0, b1, b2 = data[i], data[i + 1], data[i + 2]
        if fmt == "s334-1a":
            out.append((1 if b0 & 0x80 else 2, b1, b2))
        elif b0 & 0x04 and b0 & 0x03 < 2:            # cc_valid, NTSC field 1 or 2
            out.append(((b0 & 0x03) + 1, b1, b2))
    return out


def _cc_channel(track):
    """The CEA-608 channel (1-4) of a track ("cc:CC3"), 0 when it names none."""
    origin = getattr(track, "origin", "") or ""
    name = origin.split(":", 1)[-1].upper()
    return int(name[2]) if name.startswith("CC") and name[2:3] in "1234" and name[2:] else 0


class Cea608Renderer(Renderer):
    """CEA-608 captions of one channel (CC1-CC4), decoded by
    lindvb.cc608 (GStreamer's CEA-608 decoders are in gst-plugins-rs, which
    most systems lack).

    :meth:`feed` gets the caption data of a frame (a streaming thread) and
    only puts its byte pairs in a bounded queue: a full queue throws the
    oldest away (counted as dropped), so the video thread never waits. The
    main loop decodes what came every :data:`CC_DECODE_MS`; *on_text(text,
    t)* gets each change of what the channel shows, with the stream time of
    the frame that brought it ("" when it goes off). Drawing it (Pango in
    GTK) comes with the takeover; in the shadow it is only noted."""

    kind = CEA608

    def __init__(self, on_text, queue_pairs=CC_QUEUE_PAIRS, timer=True):
        from collections import deque
        self._on_text = on_text
        self._state = State(CEA608)
        self._queue = deque()
        self._queue_pairs = queue_pairs
        self._decoder = None
        self._timer = None
        self._use_timer = timer          # False (tests): decode by hand (see :meth:`decode`)

    def can(self, track):
        return getattr(track, "format", "") == CEA608 and _cc_channel(track) > 0

    def show(self, track):
        from . import cc608
        channel = _cc_channel(track)
        if not channel:
            self.off()
            return
        self.off()
        self._decoder = cc608.Cea608Decoder(channel)
        self._state = State(CEA608, on=True, track=getattr(track, "origin", ""))
        if self._use_timer:
            from gi.repository import GLib
            self._timer = GLib.timeout_add(CC_DECODE_MS, self._tick)

    def off(self):
        was = self._state
        self._state = State(CEA608)
        if self._timer is not None:
            from gi.repository import GLib
            GLib.source_remove(self._timer)
            self._timer = None
        self._queue.clear()
        self._decoder = None
        if was.on and was.last:
            self._on_text("", None)

    def state(self):
        return self._state

    # -- streaming threads -------------------------------------------------------------

    def feed(self, data, fmt="cc_data", t=None):
        """The caption data of one frame; never blocks."""
        s, dec = self._state, self._decoder
        if not s.on or dec is None:
            return
        q = self._queue
        for field, b1, b2 in cc608_pairs(data, fmt):
            if field != dec.field or not (b1 & 0x7f or b2 & 0x7f):     # not its field, padding
                continue
            s.data += 1
            if len(q) >= self._queue_pairs:
                q.popleft()
                s.dropped += 1
            q.append((b1, b2, t))

    # -- main loop ---------------------------------------------------------------------

    def _tick(self):
        try:
            self.decode()
        except Exception:  # noqa: BLE001 - a renderer must never cost the playback
            pass
        return self._state.on

    def decode(self):
        """Decode what came; the text shown changes on_text."""
        s, dec, q = self._state, self._decoder, self._queue
        while q and dec is not None:
            b1, b2, t = q.popleft()
            if dec.feed(b1, b2):
                text = dec.text
                if text:
                    s.decoded += 1
                s.last = text
                self._on_text(text, t)


# frames of caption data a CEA-708 renderer holds before it throws the oldest away
CC_QUEUE_FRAMES = 300


def _cc_service(track):
    """The CEA-708 service of a track ("cc:1"), 0 when it names none."""
    name = (getattr(track, "origin", "") or "").split(":", 1)[-1]
    return int(name) if name.isdigit() and 1 <= int(name) <= 63 else 0


def _dtvcc_size(header):
    """The bytes of a DTVCC packet, its header byte included (CEA-708 5)."""
    code = header & 0x3f
    return 128 if code == 0 else code * 2


class Cea708Renderer(Renderer):
    """CEA-708 captions of one service, decoded by lindvb.cc708; the
    player draws them (stage F), in place of cc708overlay.

    As :class:`Cea608Renderer`: :meth:`feed` (video thread) only queues the
    DTVCC part of each frame's cc_data, bounded; the main loop puts the
    packets together, decodes the blocks of its service every
    :data:`CC_DECODE_MS` and hands *on_text(text, t)* each change of what
    the service shows."""

    kind = CEA708

    def __init__(self, on_text, queue_frames=CC_QUEUE_FRAMES, timer=True):
        from collections import deque
        self._on_text = on_text
        self._state = State(CEA708)
        self._queue = deque()
        self._queue_frames = queue_frames
        self._use_timer = timer
        self._decoder = None
        self._timer = None
        self._packet = bytearray()
        self._packet_t = None

    def can(self, track):
        return getattr(track, "format", "") == CEA708 and _cc_service(track) > 0

    def show(self, track):
        from . import cc708
        service = _cc_service(track)
        self.off()
        if not service:
            return
        self._decoder = cc708.Cea708Decoder(service)
        self._state = State(CEA708, on=True, track=getattr(track, "origin", ""))
        if self._use_timer:
            from gi.repository import GLib
            self._timer = GLib.timeout_add(CC_DECODE_MS, self._tick)

    def off(self):
        was = self._state
        self._state = State(CEA708)
        if self._timer is not None:
            from gi.repository import GLib
            GLib.source_remove(self._timer)
            self._timer = None
        self._queue.clear()
        self._decoder = None
        self._packet, self._packet_t = bytearray(), None
        if was.on and was.last:
            self._on_text("", None)

    def state(self):
        return self._state

    def feed(self, data, fmt="cc_data", t=None):
        """The caption data of one frame (cc_data); never blocks."""
        s = self._state
        if not s.on or fmt != "cc_data":
            return
        triplets = [data[i:i + 3] for i in range(0, len(data) - 2, 3) if data[i] & 0x03 >= 2]
        if not any(tr[0] & 0x04 for tr in triplets):        # none, or padding only
            return
        dtvcc = b"".join(triplets)
        s.data += 1
        if len(self._queue) >= self._queue_frames:
            self._queue.popleft()
            s.dropped += 1
        self._queue.append((dtvcc, t))

    def _tick(self):
        try:
            self.decode()
        except Exception:  # noqa: BLE001 - a renderer must never cost the playback
            pass
        return self._state.on

    def decode(self):
        """Put the packets together and decode them; changes go to on_text."""
        q = self._queue
        while q and self._decoder is not None:
            data, t = q.popleft()
            for i in range(0, len(data) - 2, 3):
                self._triplet(data[i], data[i + 1], data[i + 2], t)

    def _triplet(self, b0, b1, b2, t):
        """A packet ends when it has its size or the next one starts.
        Padding (cc_valid 0) does not end it: broadcasters spread a packet
        over frames, a few triplets each, the rest of each frame padding."""
        valid, start = b0 & 0x04, b0 & 0x03 == 3
        if not valid:
            return
        if start:
            self._end()
            self._packet, self._packet_t = bytearray([b1, b2]), t
        elif self._packet:
            self._packet += bytes([b1, b2])
        if self._packet and len(self._packet) >= _dtvcc_size(self._packet[0]):
            self._end()                                     # complete: no need to wait

    def _end(self):
        from .captions import dtvcc_blocks
        packet, t = bytes(self._packet), self._packet_t
        self._packet, self._packet_t = bytearray(), None
        dec, s = self._decoder, self._state
        if not packet or dec is None:
            return
        for svc, block in dtvcc_blocks(packet):
            if svc == dec.service and dec.feed(block):
                text = dec.text
                if text:
                    s.decoded += 1
                s.last = text
                self._on_text(text, t)


class CaptionShadow:
    """What a closed caption renderer (CEA-608, CEA-708) decoded: in the
    shadow beside the renderer that draws, or (*drawn*) the one that draws.
    The line says what it decoded and shows a few of the captions, to set
    beside what the picture showed."""

    KEEP = 500

    def __init__(self, track="", drawn=False):
        self.track = track
        self.drawn = drawn
        self.captions = []           # [(text, stream time)]

    def saw_new(self, text, t):
        """A change of what the channel shows: a line that grows (roll-up and
        paint-on write a pair at a time) counts once, at its first time."""
        if not text:
            return
        if self.captions and text.startswith(self.captions[-1][0]):
            self.captions[-1] = (text, self.captions[-1][1])
            return
        self.captions = (self.captions + [(text, t)])[-self.KEEP:]

    def report(self, state):
        """None when no caption data came."""
        if not state.data:
            return None
        where, did = (self.track, "drawn") if self.drawn else (f"shadow, {self.track}", "decoded")
        head = (f"{state.kind} renderer ({where}): {len(self.captions)} captions "
                f"{did}; data {state.data}, dropped {state.dropped}")
        if not self.captions:
            return head + "; the caption data held no text for this channel"
        texts = [c[0].replace("\n", " / ") for c in self.captions]
        return head + f"; first {_few(texts[:3])}, last {texts[-1][:60]!r}"


# changes of what a closed caption channel shows that wait for their frame
CC_DRAW_KEEP = 300


class CaptionDrawer:
    """Shows what a closed caption channel shows when its frame is shown.

    A caption renderer hears of each change (:meth:`add`, main loop) with
    the stream time of the frame that brought it, a little before that
    frame reaches the screen. Every :data:`CC_DECODE_MS` the drawer looks
    at the playback position (*position()*: stream time in ns, None when
    there is none) and hands *on_text(text)* the latest change due there,
    once ("" when the captions go off). A change without a time, or while
    there is no position, is due at once. A time going back (a seek) drops
    the changes still to come: the frames bring them again."""

    def __init__(self, position, on_text, tick_ms=CC_DECODE_MS):
        from collections import deque
        self._position = position
        self._on_text = on_text
        self._tick_ms = tick_ms
        self._changes = deque()      # [(stream time or None, text)], in the order they came
        self._shown = ""
        self._source = 0

    def add(self, text, t):
        ch = self._changes
        if t is not None:
            while ch and ch[-1][0] is not None and ch[-1][0] > t:
                ch.pop()
        ch.append((t, text))
        while len(ch) > CC_DRAW_KEEP:
            ch.popleft()

    def start(self):
        from gi.repository import GLib
        if not self._source:
            self._source = GLib.timeout_add(self._tick_ms, self._tick)

    def stop(self):
        from gi.repository import GLib
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        self._changes.clear()
        if self._shown:
            self._shown = ""
            self._on_text("")

    def _tick(self):
        try:
            self.tick()
        except Exception:  # noqa: BLE001 - drawing must never cost the playback
            pass
        return bool(self._source)

    def tick(self):
        """Show what is due now."""
        pos, ch, due = self._position(), self._changes, None
        while ch and (ch[0][0] is None or pos is None or ch[0][0] <= pos):
            due = ch.popleft()
        if due is not None and due[1] != self._shown:
            self._shown = due[1]
            self._on_text(due[1])


# PES packets of DVB subtitles the renderer's queue holds (a display set is
# one or two; a few a second at most)
DVBSUB_QUEUE_BUFFERS = 100


class DvbSubRenderer(Renderer):
    """DVB subtitles (bitmaps) of one PID, decoded by lindvb.dvbsub apart
    from the old path (dvbsuboverlay).

    :meth:`feed` (a streaming thread) only puts the PES data in a bounded
    queue, the oldest thrown away when it is full (counted as dropped). A
    thread of its own decodes it: a display set of HD subtitles takes some
    ten milliseconds in Python, which the main loop is not to wait for.
    *on_page(page, t)* gets each display set (a lindvb.dvbsub.Page) with the
    stream time of the packet that ended it, on that thread (None, None
    when the renderer goes off). Drawing comes with the takeover; in the
    shadow it is only noted."""

    kind = DVB_SUB

    def __init__(self, on_page, queue_buffers=DVBSUB_QUEUE_BUFFERS, thread=True):
        import threading
        from collections import deque
        self._on_page = on_page
        self._state = State(DVB_SUB)
        self._queue = deque()
        self._queue_buffers = queue_buffers
        self._use_thread = thread        # False (tests): decode by hand (see :meth:`decode`)
        self._decoder = None
        self._lock = threading.Lock()   # one decodes at a time: its thread or decode()
        self._wake = threading.Event()

    def show(self, track):
        import threading
        from . import dvbsub
        self.off()
        decoder = self._decoder = dvbsub.DvbSubDecoder()
        self._state = State(DVB_SUB, on=True, track=getattr(track, "origin", ""))
        if self._use_thread:
            threading.Thread(target=self._work, args=(decoder,), name="lindvb-dvbsub",
                             daemon=True).start()

    def off(self):
        was = self._state
        self._state = State(DVB_SUB)
        with self._lock:
            self._queue.clear()
            self._decoder = None
        self._wake.set()                # its thread ends
        if was.on and was.last:
            self._on_page(None, None)

    def state(self):
        return self._state

    def feed(self, data, t=None):
        """The data of one PES packet; never blocks."""
        s = self._state
        if not s.on or not data:
            return
        s.data += 1
        if len(self._queue) >= self._queue_buffers:
            self._queue.popleft()
            s.dropped += 1
        self._queue.append((bytes(data), t))
        self._wake.set()

    def _work(self, decoder):
        while self._decoder is decoder:
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self.decode()
            except Exception:  # noqa: BLE001 - a renderer must never cost the playback
                pass

    def decode(self):
        """Decode what came; each display set goes to on_page."""
        with self._lock:
            q, s, dec = self._queue, self._state, self._decoder
            while q and dec is not None:
                data, t = q.popleft()
                page = dec.feed(data)
                if page is None:
                    continue
                if page.pixels or page.text:
                    s.decoded += 1
                s.last = page.describe()
                self._on_page(page, t)


class DvbSubShadow:
    """What the DVB-SUB renderer decoded beside the old path,
    dvbsuboverlay: that blends the bitmaps into the picture and lets nothing
    be read back, so the line says what the renderer decoded (pages shown,
    where on the display, how many cleared) to set beside what the picture
    showed."""

    KEEP = 500

    def __init__(self, track=""):
        self.track = track
        self.pages = []                 # [(describe(), stream time)] of the pages shown
        self.cleared = 0

    def saw_page(self, page, t):
        if page is None:
            return
        if page.pixels or page.text:
            self.pages = (self.pages + [(page.describe(), t)])[-self.KEEP:]
        else:
            self.cleared += 1

    def report(self, state):
        """None when no subtitle data came."""
        if not state.data:
            return None
        head = (f"{state.kind} renderer (shadow, {self.track}): {len(self.pages)} subtitle "
                f"pages shown, {self.cleared} cleared; data {state.data}, dropped {state.dropped}")
        if not self.pages:
            return head + "; the subtitle data showed nothing"
        shown = [p[0] for p in self.pages]
        return head + f"; first {_few(shown[:3])}, last {shown[-1][:60]!r}"


def cue_text(data):
    """The plain text of a parsed cue (subparse: Pango markup), its lines trimmed."""
    import html
    import re
    text = bytes(data).decode("utf-8", "replace").replace("\x00", "")
    text = html.unescape(re.sub(r"<[^>]*>", "", text))
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())


def stream_span(segment, pts, duration=None):
    """(start, end): the stream times (ns) a cue at *pts* lasting *duration*
    is shown from and to in *segment*, the playback position, the same
    before and after a seek. A cue that began before the segment (a seek
    into it) is shown from where the seek went; (None, None) when the cue
    is not shown in it at all, or has no time; end is None when the cue
    has no duration."""
    from gi.repository import Gst
    if segment is None or pts is None or pts == Gst.CLOCK_TIME_NONE:
        return None, None
    end = None
    if duration is not None and duration != Gst.CLOCK_TIME_NONE:
        inside, pts, stop = segment.clip(Gst.Format.TIME, pts, pts + duration)
        if not inside:
            return None, None
        end = segment.to_stream_time(Gst.Format.TIME, stop)
        end = None if end == Gst.CLOCK_TIME_NONE else end
    t = segment.to_stream_time(Gst.Format.TIME, pts)
    return (None, None) if t == Gst.CLOCK_TIME_NONE else (t, end)


def stream_time(segment, pts, duration=None):
    """The stream time (ns) a cue is shown from (:func:`stream_span`), or None."""
    return stream_span(segment, pts, duration)[0]


class TextCueRenderer(Renderer):
    """Text cues (WebVTT, SRT, TTML) parsed by the parser the old path uses
    for them (:data:`CUE_PARSERS`).

    It runs in a small pipeline of its own beside the playback (appsrc,
    leaky queue, parser, appsink), so it can never hold up picture or
    sound: :meth:`feed` hands it a copy of each subtitle buffer the overlay's
    own parser gets, with its caps and segment; the pipeline is made for the
    parser those caps need, and made again when they change. *on_cue(text,
    start, end)* gets each cue it decoded, from a streaming thread; start
    and end are the stream times (ns, the playback position) the cue is
    shown from and to (end None when the cue names none).

    :class:`CueDrawer` draws them at their time (Pango in GTK, without
    textoverlay or ttmlrender); in the shadow it only notes what it would
    draw and when."""

    kind = WEBVTT

    def __init__(self, on_cue, queue_buffers=CUE_QUEUE_BUFFERS, warm=False):
        self._on_cue = on_cue
        self._queue_buffers = queue_buffers
        self._state = State(WEBVTT)
        self.warm = warm
        self.pipeline = None
        self.parser = ""             # the parser of the pipeline there is
        self._src = None
        self._caps = None

    def _make(self, parser):
        """The pipeline appsrc ! queue ! *parser* ! appsink, playing."""
        from gi.repository import Gst
        self._close_pipeline()
        pipeline = Gst.Pipeline.new("text-cue-renderer")
        src = Gst.ElementFactory.make("appsrc")
        src.set_property("format", Gst.Format.TIME)
        src.set_property("handle-segment-change", True)
        src.set_property("max-buffers", self._queue_buffers)
        src.set_property("leaky-type", 2)          # downstream: drop the oldest
        q = Gst.ElementFactory.make("queue")
        q.set_property("leaky", 2)
        q.set_property("max-size-buffers", self._queue_buffers)
        q.set_property("max-size-time", 0)
        q.set_property("max-size-bytes", 0)
        q.connect("overrun", self._overrun)
        parse = Gst.ElementFactory.make(parser)
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("sync", False)     # the due time is compared, not waited for
        sink.set_property("emit-signals", True)
        sink.connect("new-sample", self._on_sample)
        if parse is None:
            raise RuntimeError(f"{parser} missing")
        chain = (src, q, parse, sink)
        for e in chain:
            pipeline.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        pipeline.set_state(Gst.State.PLAYING)
        self.pipeline, self._src, self.parser, self._caps = pipeline, src, parser, None

    def _close_pipeline(self):
        from gi.repository import Gst
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline, self._src, self.parser, self._caps = None, None, "", None

    # -- the interface ---------------------------------------------------------------

    def can(self, track):
        return getattr(track, "format", "") in (WEBVTT, TTML)

    def show(self, track):
        self._state = State(getattr(track, "format", "") or WEBVTT, on=True,
                            track=getattr(track, "origin", ""))

    def off(self):
        self._state = State(self._state.kind)

    def state(self):
        return self._state

    def close(self):
        self.off()
        self._close_pipeline()

    # -- streaming threads -------------------------------------------------------------

    def feed(self, buf, caps, segment):
        """A subtitle buffer of the playback (one streaming thread); never blocks."""
        from gi.repository import Gst
        s = self._state
        if not s.on and not self.warm:
            return
        if caps is None or not caps.get_size():
            return
        parser = CUE_PARSERS.get(caps.get_structure(0).get_name(), ("", ""))[0]
        if not parser:
            return
        if s.on:
            s.data += 1
        if parser != self.parser:
            self._make(parser)
        src = self._src
        if src is None:                  # closed meanwhile
            return
        if self._caps is None or not caps.is_equal(self._caps):
            self._caps = caps
            src.set_property("caps", caps)
        sample = Gst.Sample.new(buf.copy(), caps, segment, None)
        if src.emit("push-sample", sample) != Gst.FlowReturn.OK:
            s.dropped += 1

    def _overrun(self, _queue):
        self._state.dropped += 1

    def _on_sample(self, sink):
        from gi.repository import Gst
        sample = sink.emit("pull-sample")
        s = self._state
        if sample is None or not s.on:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        text = cue_text(info.data)
        buf.unmap(info)
        if text:
            s.last = text
            s.decoded += 1
            self._on_cue(text, *stream_span(sample.get_segment(), buf.pts, buf.duration))
        return Gst.FlowReturn.OK


class CueTimeline:
    """The cues to draw, by stream time (ns): which are due at a position.

    The same cue coming again (a seek fetches the subtitles anew) is kept
    once; only the last :data:`CUE_KEEP` cues are kept."""

    def __init__(self):
        self.cues = []               # [(start, end, text)], by start

    def add(self, text, start, end=None):
        """The cue (start, end, text) added; None when it is there already
        or has no text or time."""
        if not text or start is None:
            return None
        if end is None or end <= start:
            end = start + CUE_DEFAULT_NS
        cue = (start, end, text)
        i = bisect.bisect_left(self.cues, cue)
        if i < len(self.cues) and self.cues[i] == cue:
            return None
        self.cues.insert(i, cue)
        del self.cues[:-CUE_KEEP]
        return cue

    def at(self, pos):
        """The cues shown at *pos*, by start."""
        return tuple(c for c in self.cues if c[0] <= pos < c[1])

    def next_change(self, pos):
        """The next stream time after *pos* a cue starts or ends, or None."""
        times = [t for c in self.cues for t in c[:2] if t > pos]
        return min(times) if times else None

    def clear(self):
        self.cues = []


class CueDrawer:
    """Draws the text cues at their time.

    From the main loop it looks at the playback position (*position()*: the
    stream time in ns, None when there is none) and hands *on_text(text,
    position)* the text due there each time the cues due change: their
    lines in order, "" when none is. Between the cues it sleeps until the
    next one starts or ends, at most :data:`DRAW_TICK_MAX_MS`, so it costs
    a few wake-ups a second and only while it is on. *rate()* is the
    playback speed, *clock()* a monotonic clock in ns.

    Right after a start or a seek the position stands, jumps or runs ahead
    of the clock while the playback catches up; then the drawer looks every
    :data:`DRAW_TICK_FAST_MS` for a while (:data:`DRAW_FAST_LOOKS` looks of
    :data:`DRAW_PACE_NS`), and sooner still when the position runs faster.

    *on_shown(text, start, late)* hears, once for each cue, how long (ns of
    the clock) after its time it was first drawn, and None for a cue that
    was over before it could be (it came after its end, or the position
    jumped over it)."""

    MAX_SPEED = 8.0              # a position jumping further is a seek, not speed

    def __init__(self, position, on_text, rate=lambda: 1.0, clock=None, on_shown=None):
        import time
        self.timeline = CueTimeline()
        self._position = position
        self._on_text = on_text
        self._on_shown = on_shown
        self._rate = rate
        self._clock = clock or time.monotonic_ns
        self._shown = ()             # the cues drawn now
        self._source = 0
        self._last = None            # (clock, position) when it last looked
        self._pace = None            # (clock, position) the pace is measured from
        self._speed = 1.0            # how fast the position went, at least the rate
        self._fast = 0               # looks of DRAW_PACE_NS left to look closely
        self._pending = {}           # cue -> clock it came; not judged yet

    @property
    def running(self):
        return bool(self._source)

    def add(self, text, start, end=None):
        """A cue (main loop); a running drawer looks again at once."""
        cue = self.timeline.add(text, start, end)
        if cue is not None:
            self._pending[cue] = self._clock()
        if self._source:
            self._schedule(0)

    def start(self):
        if not self._source:
            self._schedule(0)

    def stop(self):
        """Nothing more drawn; the cues kept are dropped."""
        from gi.repository import GLib
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        self.timeline.clear()
        self._pending = {}
        self._last = self._pace = None
        if self._shown:
            self._shown = ()
            self._on_text("", None)

    def tick(self):
        """Draw what is due now; the ms until it should look again."""
        pos, now = self._position(), self._clock()
        if pos is None:
            self._last = self._pace = None
            return DRAW_TICK_MAX_MS
        prev, self._last = self._last, (now, pos)
        self._measure(now, pos)
        due = self.timeline.at(pos)
        if due != self._shown:
            self._shown = due
            self._on_text("\n".join(c[2] for c in due), pos)
        self._judge(due, now, pos, prev)
        nxt = self.timeline.next_change(pos)
        ms = DRAW_TICK_MAX_MS if nxt is None else int((nxt - pos) / 1e6 / self._speed) + 1
        if self._fast:
            ms = min(ms, DRAW_TICK_FAST_MS)
        return max(DRAW_TICK_MIN_MS, min(DRAW_TICK_MAX_MS, ms))

    def _measure(self, now, pos):
        """The pace of the position over the last DRAW_PACE_NS or more: at
        the playback speed it is regular; standing, it uses up the close
        looks; jumping or running at another speed, it earns them again."""
        rate = abs(self._rate() or 1.0)
        if self._pace is None:
            self._pace, self._speed, self._fast = (now, pos), rate, DRAW_FAST_LOOKS
            return
        dt, dp = now - self._pace[0], pos - self._pace[1]
        if dt < DRAW_PACE_NS:
            return
        self._pace = (now, pos)
        ran = dp / dt
        self._speed = max(rate, ran) if 0 < ran <= self.MAX_SPEED else rate
        if abs(ran - rate) <= 0.25 * rate:
            self._fast = 0
        elif dp:
            self._fast = DRAW_FAST_LOOKS
        else:
            self._fast = max(0, self._fast - 1)

    def _judge(self, due, now, pos, prev):
        """Each cue once: drawn now, and how late; or over already."""
        for cue in [c for c in self._pending if c in due or c[1] <= pos]:
            came = self._pending.pop(cue)
            late = self._late(cue, came, now, pos, prev) if cue in due else None
            if self._on_shown is not None:
                self._on_shown(cue[2], cue[0], late)

    @staticmethod
    def _late(cue, came, now, pos, prev):
        """How long after the position reached the cue it was drawn: between
        two looks the position is taken to have gone on evenly; a cue that
        came later is late from when it came."""
        start = cue[0]
        if prev is not None and came <= prev[0] and prev[1] < start <= pos:
            due = prev[0] + (start - prev[1]) * (now - prev[0]) / (pos - prev[1])
            return int(now - due)
        return int(now - max(came, prev[0] if prev is not None else came))

    def _schedule(self, ms):
        from gi.repository import GLib
        if self._source:
            GLib.source_remove(self._source)
        self._source = GLib.timeout_add(ms, self._run)

    def _run(self):
        from gi.repository import GLib
        self._source = 0
        try:
            ms = self.tick()
        except Exception:  # noqa: BLE001 - a bad cue must not end the drawing
            ms = DRAW_TICK_MAX_MS
        self._source = GLib.timeout_add(ms, self._run)
        return False


class CueShadow:
    """The cues the old path (subtitleoverlay's subparse or ttmlparse) and
    the text cue renderer beside it decoded, with the stream time each is
    due; the report says whether both had the same cues at the same times.

    The old path hands on a cue only when the picture gets there, the
    renderer (not drawing yet) as soon as it is parsed: cues due after the
    position the playback had reached (*until*) are left out on both sides.

    It also notes how soon :class:`CueDrawer` drew each cue (:meth:`drew`);
    :meth:`drawing_report` says whether each cue of the old path was drawn
    within :data:`DRAW_TOLERANCE_NS` after its time."""

    KEEP = 500

    def __init__(self, track=""):
        self.track = track
        self.old = []                # [(text, stream time)]
        self.new = []
        self.drawn = []              # [(cue text, its time, ns drawn late or None)]

    def saw_old(self, text, rt):
        if text:
            self.old = (self.old + [(text, rt)])[-self.KEEP:]

    def saw_new(self, text, rt):
        if text:
            self.new = (self.new + [(text, rt)])[-self.KEEP:]

    def drew(self, text, start, late):
        """The drawer first drew the cue *text* due at *start* *late* ns
        after its time; None: it was over before it could be."""
        self.drawn = (self.drawn + [(text, start, late)])[-self.KEEP:]

    def _drawn(self, cue):
        """The drawing of the cue: (late or None,), () when the drawer never
        had it."""
        text, t = cue
        found = [(late,) for d, start, late in self.drawn
                 if d == text and abs(start - t) <= CUE_TOLERANCE_NS]
        return found[0] if found else ()

    def drawing_report(self, state, until=None):
        """The line on the drawing: None when the old path had no cue due."""
        old = self._due(self.old, until)
        drawn = [(text, self._drawn((text, t))) for text, t in old]
        if drawn and drawn[-1][1] == ():     # the last one may be on its way
            drawn = drawn[:-1]
        if not drawn:
            return None
        missing = [text for text, d in drawn if d == ()]
        over = [text for text, d in drawn if d == (None,)]
        late = [(text, d[0]) for text, d in drawn
                if d and d[0] is not None and d[0] > DRAW_TOLERANCE_NS]
        shown = len(drawn) - len(missing) - len(over)
        head = f"{state.kind} drawing (shadow, {self.track}): {shown} of {len(drawn)} cues drawn"
        if not missing and not over and not late:
            return head + f", all within {DRAW_TOLERANCE_NS // 1_000_000} ms of their time"
        parts = [head]
        if late:
            worst = max(late, key=lambda c: c[1])
            parts.append(f"{len(late)} late (worst {worst[0][:40]!r} {worst[1] / 1e6:+.0f} ms)")
        if over:
            parts.append(f"over before they could be drawn {_few(over)}")
        if missing:
            parts.append(f"never drawn {_few(missing)}")
        return "; ".join(parts)

    @staticmethod
    def _match(cue, others):
        text, rt = cue
        for t, r in others:
            if t == text and abs(rt - r) <= CUE_TOLERANCE_NS:
                return True
        return False

    @staticmethod
    def _due(cues, until):
        """The cues shown up to *until*: one without a time is not shown."""
        return [c for c in cues if c[1] is not None and
                (until is None or c[1] <= until + CUE_TOLERANCE_NS)]

    def report(self, state, until=None):
        """None when no cue was due yet; when the subtitle data held no cue
        at all (a stream sending empty WebVTT segments), the line says so."""
        if not self.old and not self.new and state.data:
            return (f"{state.kind} renderer (shadow, {self.track}): 0 cues, the old path 0; "
                    f"data {state.data}, dropped {state.dropped}; the subtitle data held no text")
        old, new = self._due(self.old, until), self._due(self.new, until)
        if not old and not new:
            return None
        missing = [c for c in old if not self._match(c, new)]
        extra = [c for c in new if not self._match(c, old)]
        # the last cue of a side may still be on its way to the other
        if missing[-1:] == old[-1:]:
            missing = missing[:-1]
        if extra[-1:] == new[-1:]:
            extra = extra[:-1]
        head = (f"{state.kind} renderer (shadow, {self.track}): {len(new)} cues, "
                f"the old path {len(old)}; data {state.data}, dropped {state.dropped}")
        if not missing and not extra:
            return head + "; the same cues at the same times"
        return head + (f"; missing {_few([t for t, _r in missing])}, "
                       f"extra {_few([t for t, _r in extra])}")


class Shadow:
    """What the old path showed and what the renderer running next to it
    decoded, for one track; :meth:`report` is the debug log line."""

    KEEP = 500                   # texts kept per side

    def __init__(self, track=""):
        self.track = track
        self.old = []
        self.new = []

    def saw_old(self, text):
        if text:
            self.old = (self.old + [text])[-self.KEEP:]

    def saw_new(self, text):
        if text:
            self.new = (self.new + [text])[-self.KEEP:]

    def report(self, state):
        """None when neither showed anything; a text still on its way (the
        last one of a side) does not count as a difference."""
        if not self.old and not self.new:
            return None
        missing = [t for t in self.old if t not in self.new]
        extra = [t for t in self.new if t not in self.old]
        if missing[-1:] == self.old[-1:]:
            missing = missing[:-1]
        if extra[-1:] == self.new[-1:]:
            extra = extra[:-1]
        head = (f"{state.kind} renderer (shadow, {self.track}): {len(self.new)} texts, "
                f"the old path {len(self.old)}; data {state.data}, dropped {state.dropped}")
        if not missing and not extra:
            return head + "; the same texts"
        return head + f"; missing {_few(missing)}, extra {_few(extra)}"


def _few(texts):
    shown = ", ".join(repr(t[:40]) for t in texts[:3])
    more = f" and {len(texts) - 3} more" if len(texts) > 3 else ""
    return (shown + more) or "none"
