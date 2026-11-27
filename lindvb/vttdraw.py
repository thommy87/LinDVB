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
"""

from .subrender import CueDrawer, Spread
from .vttload import VttShadow

PENDING = 500                    # cues kept while the clock has no offset yet


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
        self.pending = []                # [(text, start, end)] MPEG-TS s, before the offset
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

    def cue(self, text, start, end, mapped):
        """A cue of the loader (main loop): MPEG-TS times in s when *mapped*,
        else the times written in the segment, which are on MPEG-TS time 0."""
        self.log.saw_new(text, start, end, mapped, self._pos_s())
        self.pending.append((text, start, end))
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
        for text, start, end in self.pending:
            t0 = clock.stream_time(start, near)
            t1 = clock.stream_time(end, t0)
            self.drawer.add(text, int(t0 * 1e9), int(t1 * 1e9))
        self.pending = []
        return False

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
