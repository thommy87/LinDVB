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

The PTS of a stream can jump without a flush: the timeshift buffer of an
internet channel holds what the reader's mpegtsmux made, and a reader that
connects again starts a muxer with its PTS from the beginning. Each run of
PTS without such a jump is an epoch (:func:`is_jump`); the first frame
after the first PES of a new epoch gives that epoch its offset, and a cue
taken from the same bytes (lindvb.cuebuffer) is mapped with the offset of
its own epoch (:class:`Epochs` counts them in the bytes the player feeds).
"""

import threading
from collections import deque

from gi.repository import Gst

PTS_HZ = 90000
PTS_WRAP = 1 << 33
KEEP = 4000                      # video PES times kept to check the frames against
ANCHORS = 100                    # offsets kept (one a start or seek)
TS_PACKET = 188
JUMP_BACK = PTS_HZ              # a video PES this much before the last one: a new epoch
JUMP_AHEAD = 10 * PTS_HZ        # or this much after it
TABLES_WAIT = 4 << 20            # bytes looked at for a PAT and PMT before the first PES counts anyway


def pes_pts(packet):
    """The PTS (90 kHz ticks) of a video PES that starts in this TS packet,
    else None."""
    got = pes_stream_pts(packet)
    return got[1] if got is not None and 0xe0 <= got[0] <= 0xef else None


def pes_stream_pts(packet):
    """(stream id, PTS ticks) of a PES with a PTS that starts in this TS
    packet, else None."""
    if len(packet) < TS_PACKET or packet[0] != 0x47 or not packet[1] & 0x40:
        return None
    afc = (packet[3] >> 4) & 3
    i = 4
    if afc & 2:
        i += 1 + packet[4]
    if not afc & 1 or i + 14 > TS_PACKET:
        return None
    if packet[i:i + 3] != b"\0\0\1" or not 0xbd <= packet[i + 3] <= 0xef:
        return None
    if not packet[i + 7] & 0x80:             # PTS_DTS_flags: no PTS
        return None
    b = packet[i + 9:i + 14]
    return packet[i + 3], ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def is_jump(prev, cur):
    """Video PES PTS *cur* (ticks) after *prev* starts a new epoch: further
    back than B-frames go, or further ahead than a segment lasts."""
    if prev is None:
        return False
    d = (cur - prev) % PTS_WRAP
    if d >= PTS_WRAP // 2:
        d -= PTS_WRAP
    return d < -JUMP_BACK or d > JUMP_AHEAD


def video_pes(data):
    """[(position, PTS ticks)] of the video PES that start in whole TS
    packets of *data* (aligned to its packets or not)."""
    return _scan(data)[0]


def _scan(data):
    """(video_pes(data), where the packet starts that *data* ends before
    it is whole, or -1)."""
    found, n = [], len(data)
    i = data.find(b"\x47")
    while 0 <= i and i + TS_PACKET <= n:
        if data[i] != 0x47:                  # lost the packet start: look again
            i = data.find(b"\x47", i + 1)
            continue
        if data[i + 1] & 0x40:
            p = pes_pts(data[i:i + TS_PACKET])
            if p is not None:
                found.append((i, p))
        i += TS_PACKET
    return found, i


def pat_pmt_pids(packet):
    """The PMT PIDs a PAT section starting in this TS packet names, else None."""
    if len(packet) < TS_PACKET or packet[0] != 0x47 or not packet[1] & 0x40 or \
            ((packet[1] & 0x1F) << 8 | packet[2]):
        return None
    afc = (packet[3] >> 4) & 3
    i = 4 + (1 + packet[4] if afc & 2 else 0)
    if not afc & 1 or i >= TS_PACKET:
        return None
    i += 1 + packet[i]                       # pointer field
    if i + 8 > TS_PACKET or packet[i] != 0:
        return None
    end = min(TS_PACKET, i + 3 + (((packet[i + 1] & 0x0F) << 8) | packet[i + 2]) - 4)
    pids = set()
    for k in range(i + 8, end - 3, 4):
        if packet[k] << 8 | packet[k + 1]:   # program 0 is the NIT
            pids.add((packet[k + 2] & 0x1F) << 8 | packet[k + 3])
    return pids


class _Tables:
    """Where tsdemux can first make frames: it takes no PES before it has
    the PAT and then the PMT, so the first frame out is the first video PES
    that starts after the first PMT (a start in the middle of a stream).
    Bytes without a PAT go as they come."""

    def __init__(self):
        self.ready = False
        self._pmts = set()
        self._looked = 0

    def ready_at(self, data):
        """-1 when ready before *data*, the position in it from which the
        PES count, or None when not yet."""
        if self.ready:
            return -1
        self._looked += len(data)
        i, n = data.find(b"\x47"), len(data)
        while 0 <= i and i + TS_PACKET <= n:
            if data[i] != 0x47:
                i = data.find(b"\x47", i + 1)
                continue
            if data[i + 1] & 0x40:
                pid = (data[i + 1] & 0x1F) << 8 | data[i + 2]
                if pid == 0:
                    self._pmts |= pat_pmt_pids(data[i:i + TS_PACKET]) or set()
                elif pid in self._pmts:
                    self.ready = True
                    return i
            i += TS_PACKET
        if self._looked > TABLES_WAIT:       # a stream without them: as it comes
            self.ready = True
            return -1
        return None


class Epochs:
    """Counts the epochs in the bytes as they go by: :meth:`at` gives the
    epoch at each of the positions in a piece of data."""

    def __init__(self):
        self.epoch = 0
        self._last = None

    def at(self, data, positions):
        out, pes = [], video_pes(data)
        k = 0
        for pos in positions:
            while k < len(pes) and pes[k][0] < pos:
                self._step(pes[k][1])
                k += 1
            out.append(self.epoch)
        for _pos, p in pes[k:]:
            self._step(p)
        return out

    def _step(self, p):
        if is_jump(self._last, p):
            self.epoch += 1
        self._last = p


class TsClock:
    def __init__(self, running=False):
        self.running = running               # frames by running time, not stream time (remux)
        self._lock = threading.Lock()
        self._rest = b""                     # the start of a TS packet the next buffer ends
        self._seen = set()                   # video PES PTS (ticks) not yet matched
        self._order = deque()
        self._first = None                   # the first video PES since the anchor went
        self._anchored = False
        self.offset = None                   # stream time minus MPEG-TS time, s
        self.offsets = {}                    # epoch -> offset (since the start or flush)
        self.epoch = 0                       # the epoch of the frames that come out
        self._in_epoch = 0                   # the epoch of the PES that came in
        self._last_in = None
        self._epoch_first = {}               # epoch -> its first video PES
        self._tables = _Tables()
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
                    seg = ev.parse_segment()
                    t = (seg.to_running_time if self.running else seg.to_stream_time)(
                        Gst.Format.TIME, buf.pts)
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
            self.offsets = {}
            self.epoch = self._in_epoch = 0
            self._last_in = None
            self._epoch_first = {}

    def feed_ts(self, data):
        """TS bytes as they come to tsdemux (not always whole packets). Only
        the packets that start a PES are looked at."""
        data = self._rest + data
        found, i = _scan(data)
        with self._lock:
            self._rest = data[i:] if i >= 0 else b""
            if not self._anchored:
                ready = self._tables.ready_at(data)
                if ready is not None and ready >= 0:
                    # the PES before the first PMT: tsdemux makes no frame of them
                    after = [p for pos, p in found if pos > ready]
                    self._first = after[0] if after else None
                elif found and self._first is None:
                    self._first = found[0][1]
            for _pos, p in found:
                if is_jump(self._last_in, p):
                    self._in_epoch += 1
                    self._epoch_first[self._in_epoch] = p
                self._last_in = p
                self._seen.add(p)
                self._order.append(p)
            while len(self._order) > KEEP:
                self._seen.discard(self._order.popleft())

    def frame(self, t):
        """A video frame left tsdemux at stream time *t* (s)."""
        with self._lock:
            if not self._anchored:
                if self._first is not None:
                    self._anchor(t, self._first, self.epoch)
                    self._anchored = True
                return
            self.frames += 1
            ticks = round((t - self.offset) * PTS_HZ) % PTS_WRAP
            hit = next((p for p in (ticks, ticks - 1, ticks + 1) if p in self._seen), None)
            if hit is None and self.epoch + 1 in self._epoch_first:
                # the first frame of the next epoch: its first PES
                self.epoch += 1
                self._anchor(t, self._epoch_first.pop(self.epoch), self.epoch)
                self.matched += 1
            elif hit is None:
                self.missed += 1
            else:
                self.matched += 1
                self._seen.discard(hit)

    def _anchor(self, t, first, epoch):
        self.offset = t - first / PTS_HZ
        self.offsets[epoch] = self.offset
        self.anchors.append(self.offset)
        self.starts += 1
        del self.anchors[:-ANCHORS]
        self._seen.discard(first)

    # -- main loop -----------------------------------------------------------------

    def stream_time(self, mpegts, near=None, epoch=None):
        """The stream time (s) of MPEG-TS time *mpegts* (s), the wrap taken
        into account near stream time *near*; None without an offset (for
        *epoch*: once that epoch's first frame came out)."""
        offset = self.offset if epoch is None else self.offsets.get(epoch)
        if offset is None:
            return None
        t = mpegts + offset
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
