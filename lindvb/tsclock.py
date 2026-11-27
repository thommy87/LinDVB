# SPDX-License-Identifier: GPL-2.0-or-later
"""The MPEG-TS time of the picture against its stream time (debug mode).

LinDVB's WebVTT loader (lindvb.vttload) gives each cue its MPEG-TS time
from the segment's X-TIMESTAMP-MAP (90 kHz, the clock of the video's PTS).
Drawing a cue at the right time needs the stream time (the playback
position) of the frame with that MPEG-TS time. hlsdemux2 hands on frames
whose times no longer show the MPEG-TS time, and the old path's cue times
are no reference either (with PBS and SR they come from the "segment
position" fallback of buffers without time).

:class:`TsClock` watches the tsdemux inside the HLS demuxer: on its sink
the PTS of the video PES packets as they are in the segments, on its video
src pads the stream time of the frames it makes of them. The first frame
after a start or flush and the first video PES are the same packet, which
gives the offset (stream time minus MPEG-TS time). Every later frame
checks it: its stream time minus the offset is the MPEG-TS time of a PES
seen at the sink, or the offset no longer holds (counted as missed). The
probes only note; they never hold a buffer.
"""

import threading
from collections import deque

from gi.repository import Gst

PTS_HZ = 90000
PTS_WRAP = 1 << 33
KEEP = 4000                      # video PES times kept to check the frames against
ANCHORS = 100                    # offsets kept (one a start or seek)
TS_PACKET = 188


def pes_pts(packet):
    """The PTS (90 kHz ticks) of a video PES that starts in this TS packet,
    else None."""
    if len(packet) < TS_PACKET or packet[0] != 0x47 or not packet[1] & 0x40:
        return None
    afc = (packet[3] >> 4) & 3
    i = 4
    if afc & 2:
        i += 1 + packet[4]
    if not afc & 1 or i + 14 > TS_PACKET:
        return None
    if packet[i:i + 3] != b"\0\0\1" or not 0xe0 <= packet[i + 3] <= 0xef:
        return None
    if not packet[i + 7] & 0x80:             # PTS_DTS_flags: no PTS
        return None
    b = packet[i + 9:i + 14]
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


class TsClock:
    def __init__(self):
        self._lock = threading.Lock()
        self._rest = b""                     # the start of a TS packet the next buffer ends
        self._seen = set()                   # video PES PTS (ticks) not yet matched
        self._order = deque()
        self._first = None                   # the first video PES since the anchor went
        self._anchored = False
        self.offset = None                   # stream time minus MPEG-TS time, s
        self.anchors = []                    # the offsets taken (a flush takes a new one)
        self.starts = 0
        self.frames = 0                      # frames checked against the offset
        self.matched = 0
        self.missed = 0

    # -- streaming threads ---------------------------------------------------------

    def watch(self, tsdemux):
        tsdemux.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, self._probe_ts)
        tsdemux.connect("pad-added", self._on_pad)

    def _on_pad(self, _demux, pad):
        if pad.get_name().startswith("video"):
            pad.add_probe(Gst.PadProbeType.BUFFER, self._probe_frame)

    def _probe_ts(self, _pad, info):
        try:
            if info.type & Gst.PadProbeType.BUFFER:
                buf = info.get_buffer()
                ok, m = buf.map(Gst.MapFlags.READ)
                if ok:
                    data = bytes(m.data)
                    buf.unmap(m)
                    self.feed_ts(data)
            else:
                ev = info.get_event()
                if ev is not None and ev.type == Gst.EventType.FLUSH_STOP:
                    self.restart()
        except Exception:  # noqa: BLE001 - a probe that notes must never cost the stream
            pass
        return Gst.PadProbeReturn.OK

    def _probe_frame(self, pad, info):
        try:
            buf = info.get_buffer()
            if buf.pts != Gst.CLOCK_TIME_NONE:
                ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
                if ev is not None:
                    t = ev.parse_segment().to_stream_time(Gst.Format.TIME, buf.pts)
                    if t != Gst.CLOCK_TIME_NONE:
                        self.frame(t / Gst.SECOND)
        except Exception:  # noqa: BLE001
            pass
        return Gst.PadProbeReturn.OK

    def restart(self):
        """A flush: the next frame and PES take a new offset."""
        with self._lock:
            self._rest = b""
            self._first = None
            self._anchored = False
            self._seen.clear()
            self._order.clear()

    def feed_ts(self, data):
        """TS bytes as they come to tsdemux (not always whole packets). Only
        the packets that start a PES are looked at."""
        data = self._rest + data
        found, n = [], len(data)
        i = data.find(b"\x47")
        while 0 <= i and i + TS_PACKET <= n:
            if data[i] != 0x47:                  # lost the packet start: look again
                i = data.find(b"\x47", i + 1)
                continue
            if data[i + 1] & 0x40:
                p = pes_pts(data[i:i + TS_PACKET])
                if p is not None:
                    found.append(p)
            i += TS_PACKET
        with self._lock:
            self._rest = data[i:] if i >= 0 else b""
            if found and self._first is None and not self._anchored:
                self._first = found[0]
            for p in found:
                self._seen.add(p)
                self._order.append(p)
            while len(self._order) > KEEP:
                self._seen.discard(self._order.popleft())

    def frame(self, t):
        """A video frame left tsdemux at stream time *t* (s)."""
        with self._lock:
            if not self._anchored:
                if self._first is not None:
                    self.offset = t - self._first / PTS_HZ
                    self.anchors.append(self.offset)
                    self.starts += 1
                    del self.anchors[:-ANCHORS]
                    self._anchored = True
                    self._seen.discard(self._first)
                return
            self.frames += 1
            ticks = round((t - self.offset) * PTS_HZ) % PTS_WRAP
            hit = next((p for p in (ticks, ticks - 1, ticks + 1) if p in self._seen), None)
            if hit is None:
                self.missed += 1
            else:
                self.matched += 1
                self._seen.discard(hit)

    # -- main loop -----------------------------------------------------------------

    def stream_time(self, mpegts, near=None):
        """The stream time (s) of MPEG-TS time *mpegts* (s), the wrap taken
        into account near stream time *near*; None without an offset."""
        if self.offset is None:
            return None
        t = mpegts + self.offset
        if near is not None:
            wrap = PTS_WRAP / PTS_HZ
            t += round((near - t) / wrap) * wrap
        return t

    def summary(self):
        if self.offset is None:
            return "no video PES seen in the segments"
        anchors = self.anchors
        spread = max(anchors) - min(anchors)
        return (f"MPEG-TS time to stream time {self.offset:+.3f} s "
                f"({self.starts} starts, offsets spread {spread:.3f} s); "
                f"{self.matched} of {self.frames} frames matched it, {self.missed} not")
