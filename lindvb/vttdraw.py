# SPDX-License-Identifier: GPL-2.0-or-later
"""The WebVTT subtitles of an HLS rendition, drawn by LinDVB (stage F).

With GStreamer 1.24, hlsdemux2 holds picture and sound back while it plays
a WebVTT rendition whose segments carry no time stamps (PBS, ARD-alpha,
3sat; issue #234). LinDVB therefore never hands the subtitle track to
playbin3: it loads the rendition itself (lindvb.vttload), ties each cue's
MPEG-TS time to the picture with the video clock (lindvb.tsclock) and draws
it at its time over the picture (subrender.CueDrawer), like teletext
subtitles. The shadow of 27.11.2026 showed the clock holding on every
station (all frames matched) and the cues coming 6 to 14 s before they
are due.

A cue segment without X-TIMESTAMP-MAP has its times on MPEG-TS time 0
(RFC 8216, 3.5). Cues that come before the clock has its offset wait for
it (bounded). :class:`VttDraw` lives on the main loop; the loader's thread
hands it the cues through GLib.idle_add.

With ``LINDVB_WEBVTT_HLSDEMUX=1`` hlsdemux2 plays them as before stage F
(:func:`old_path`), with the stall watchdog.
"""

import os

from .subrender import CueDrawer, Spread
from .vttload import VttShadow

PENDING = 500                    # cues kept while the clock has no offset yet


def old_path():
    """hlsdemux2 plays the WebVTT subtitles instead of LinDVB: only when
    asked for (``LINDVB_WEBVTT_HLSDEMUX=1``)."""
    return os.environ.get("LINDVB_WEBVTT_HLSDEMUX") == "1"


class VttDraw:
    """Draws the cues of rendition *name*: *clock()* is the video clock
    (tsclock.TsClock) or None, *position()* the stream time of the picture
    shown (ns) or None, *on_text(text)* shows the text ("" clears it)."""

    def __init__(self, name, clock, position, on_text, rate=lambda: 1.0):
        self.name = name
        self._clock = clock
        self._position = position
        self._on_text = on_text
        self.log = VttShadow(name)       # what the loader brought, for the debug log
        self.pending = []                # [(text, start, end, epoch)] MPEG-TS s, before the offset
        self.dropped = 0                 # cues thrown away waiting for the offset
        self.drawn = 0
        self.missed = 0                  # over before they could be drawn
        self.late = Spread()             # s after its time a cue was first drawn
        self.drawer = CueDrawer(position, self._draw, rate, on_shown=self._shown)

    def _draw(self, text, _pos):
        self._on_text(text)

    def _shown(self, _text, _start, late):
        if late is None:
            self.missed += 1
        else:
            self.drawn += 1
            self.late.note(late / 1e9)

    def _pos_s(self):
        pos = self._position()
        return pos / 1e9 if pos is not None else None

    def cue(self, text, start, end, mapped, epoch=None):
        """A cue of the loader (main loop): MPEG-TS times in s when *mapped*,
        else the times written in the segment, which are on MPEG-TS time 0.
        *epoch*: the stretch of the timeshift buffer the cue came from
        (lindvb.tsclock), mapped with that stretch's offset."""
        self.log.saw_new(text, start, end, mapped, self._pos_s())
        self.pending.append((text, start, end, epoch))
        if len(self.pending) > PENDING:
            del self.pending[0]
            self.dropped += 1
        self.flush()

    def flush(self):
        """Hand the drawer the cues waiting, once the clock has its offset;
        True while some still wait."""
        clock = self._clock()
        if not self.pending or clock is None or clock.offset is None:
            return bool(self.pending)
        near = self._pos_s()
        waiting = []
        for text, start, end, epoch in self.pending:
            t0 = clock.stream_time(start, near, epoch)
            if t0 is None:
                waiting.append((text, start, end, epoch))     # its stretch not played yet
                continue
            t1 = clock.stream_time(end, t0, epoch)
            self.drawer.add(text, int(t0 * 1e9), int(t1 * 1e9))
        self.pending = waiting
        return bool(waiting)

    def timed(self):
        """The clock has its offset: the cues can be drawn at their time."""
        clock = self._clock()
        return clock is not None and clock.offset is not None

    def start(self):
        self.drawer.start()

    def stop(self):
        self.drawer.stop()

    def report(self, state):
        """The debug log's line: what the loader brought, the clock, and
        what was drawn."""
        line = self.log.report(state, self._clock())
        line = line.replace("WebVTT loader (shadow, ", "WebVTT subtitles (LinDVB draws, ", 1)
        line = line.replace("; the old path 0 cues", "", 1)          # there is none
        drew = f"; {self.drawn} cues drawn"
        if self.late.n:
            drew += (f", {self.late.mean:.3f} s after their time on average "
                     f"(most {self.late.high:.3f} s)")
        drew += f", {self.missed} over before they could be drawn"
        if self.pending or self.dropped:
            drew += f", {len(self.pending) + self.dropped} never timed (no offset)"
        return line + drew


# -- the cues in the timeshift buffer (stage H) ------------------------------------

BUFFER_KEEP = 300                # cues kept a rendition, to show when it is switched on
BUFFER_LOOK_MS = 250             # how often cues waiting for their stretch are looked at


class BufferCues:
    """The cues of the subtitle renditions in the timeshift buffer of an
    internet channel (lindvb.cuebuffer), as the player's feeder finds them:
    kept per rendition, and drawn (:class:`VttDraw`) for the one switched on,
    with the video clock of the buffer's tsdemux. Main loop."""

    def __init__(self, clock, position, on_text, rate=lambda: 1.0):
        from collections import deque
        self._deque = deque
        self._clock = clock
        self._position = position
        self._on_text = on_text
        self._rate = rate
        self.pids = {}                   # rendition name -> PID
        self.recent = {}                 # PID -> deque of (text, start, end, epoch)
        self.draw = None
        self.name = None
        self.found = 0
        self._timer = 0

    def set_pids(self, pids):
        """{name: PID} of the capture; True when it changed."""
        pids = dict(pids or {})
        if pids == self.pids:
            return False
        self.pids = pids
        return True

    def add(self, pid, text, start, end, epoch):
        """A cue the feeder found (start, end on the buffer's timeline, s)."""
        self.found += 1
        keep = self.recent.setdefault(pid, self._deque(maxlen=BUFFER_KEEP))
        keep.append((text, start, end, epoch))
        if self.draw is not None and self.pids.get(self.name) == pid:
            self.draw.cue(text, start, end, True, epoch)

    def show(self, name):
        """Draw rendition *name* (None: nothing)."""
        self.off()
        if name is None or name not in self.pids:
            return
        self.name = name
        self.draw = VttDraw(name, self._clock, self._position, self._on_text, self._rate)
        for text, start, end, epoch in self.recent.get(self.pids[name], ()):
            self.draw.cue(text, start, end, True, epoch)
        self.draw.start()
        from gi.repository import GLib
        self._timer = GLib.timeout_add(BUFFER_LOOK_MS, self._look)

    def _look(self):
        if self.draw is not None:
            self.draw.flush()
        return self.draw is not None

    def off(self):
        """Nothing drawn; the report of what was, or None."""
        draw, self.draw = self.draw, None
        self.name = None
        if self._timer:
            from gi.repository import GLib
            GLib.source_remove(self._timer)
            self._timer = 0
        if draw is None:
            return None
        draw.stop()
        return (f"subtitles from the buffer ({draw.name}): {draw.drawn} cues drawn"
                + (f", {draw.late.mean:.3f} s after their time on average "
                   f"(most {draw.late.high:.3f} s)" if draw.late.n else "")
                + f", {draw.missed} over before they could be drawn"
                + (f", {len(draw.pending)} never timed" if draw.pending else ""))
