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

There will be five: DVB subtitles (bitmaps), teletext (teletextdec),
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

from dataclasses import dataclass

TELETEXT = "Teletext"            # lindvb.trackcatalog formats
DVB_SUB = "DVB-SUB"
CEA608 = "CEA-608"
CEA708 = "CEA-708"

# buffers a renderer's queue holds before it throws the oldest away:
# teletext comes in a few PES packets a frame, so about two seconds
QUEUE_BUFFERS = 100


@dataclass
class State:
    """What a renderer reports (concept: "whether data arrives and whether
    text was decoded")."""

    kind: str
    on: bool = False
    track: str = ""              # the origin of the track shown ("ttx:150"), "" when off
    data: int = 0                # buffers taken in since it went on
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
