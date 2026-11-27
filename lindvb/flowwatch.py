# SPDX-License-Identifier: GPL-2.0-or-later
"""What flows through some pads: for the debug log when the picture stands
still with subtitles on.

PBS and ARD-alpha over HLS: with the WebVTT subtitles on, the picture
stands still after a few seconds (logs since 24.11.2026); the player then
opens the stream again without them. Which part waits for which is not
known: the demuxer may hold picture and sound back for a subtitle track
that does not move on, or the subtitle overlay may hold the picture back
waiting for subtitles. A probe on the demuxer's pads and on the overlay's
notes, per pad, the buffers, the last running time, how long ago it came,
the buffers without a time and the gaps; the line at the stall shows
where the flow stopped. The probes only note, they never hold a buffer.
"""

import time

from gi.repository import Gst


def clock_text(ns):
    """2:01:10.0 for a running time in ns."""
    if ns is None:
        return "-"
    s = ns / Gst.SECOND
    return f"{int(s // 3600)}:{int(s // 60 % 60):02d}:{s % 60:04.1f}"


class Flow:
    def __init__(self):
        self.buffers = 0
        self.untimed = 0                # buffers without a time stamp
        self.gaps = 0                   # GAP events (a sparse stream saying "nothing until")
        self.last = None                # the running time of the last buffer or gap
        self.at = None                  # when it came (monotonic s)

    def describe(self, now):
        if not self.buffers and not self.gaps:
            return "nothing"
        parts = [f"{self.buffers} buffers"]
        if self.untimed:
            parts.append(f"{self.untimed} without time")
        if self.gaps:
            parts.append(f"{self.gaps} gaps")
        ago = f" ({now - self.at:.1f} s ago)" if self.at is not None else ""
        return ", ".join(parts) + f", last {clock_text(self.last)}{ago}"


class FlowWatch:
    """Probes that note what passes some pads, by a name each."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.flows = {}                 # {name: Flow}, in the order they were watched

    def watch(self, pad, name):
        while name in self.flows:       # a second pad of the same name
            name += "'"
        flow = self.flows[name] = Flow()
        pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                      self._probe, flow)
        return name

    def _probe(self, pad, info, flow):
        try:
            self._note(pad, info, flow)
        except Exception:  # noqa: BLE001 - a probe that notes must never cost the stream
            pass
        return Gst.PadProbeReturn.OK

    def _note(self, pad, info, flow):
        if info.type & Gst.PadProbeType.BUFFER:
            buf = info.get_buffer()
            flow.buffers += 1
            ts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else buf.dts
            if ts == Gst.CLOCK_TIME_NONE:
                flow.untimed += 1
            else:
                flow.last = self._running(pad, ts)
            flow.at = self.clock()
            return
        ev = info.get_event()
        if ev is not None and ev.type == Gst.EventType.GAP:
            ts, _dur = ev.parse_gap()
            flow.gaps += 1
            flow.last = self._running(pad, ts)
            flow.at = self.clock()

    @staticmethod
    def _running(pad, ts):
        ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
        if ev is None:
            return ts
        rt = ev.parse_segment().to_running_time(Gst.Format.TIME, ts)
        return None if rt == Gst.CLOCK_TIME_NONE else rt

    def summary(self):
        """One line: each pad's flow."""
        now = self.clock()
        return "; ".join(f"{name}: {flow.describe(now)}" for name, flow in self.flows.items()) \
            or "no pads watched"
