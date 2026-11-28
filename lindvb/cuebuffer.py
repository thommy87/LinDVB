# SPDX-License-Identifier: GPL-2.0-or-later
"""Subtitle cues in the timeshift buffer (stage H of the concept).

The WebVTT subtitles of an HLS channel never reach the buffer through the
demuxer: hlsdemux2 (GStreamer 1.24) holds picture and sound back for them,
and mpegtsmux has no stream type for text. So far a channel with subtitles
switched on played without timeshift. Now the buffer reader loads the
subtitle renditions itself (lindvb.vttload) and writes each cue into the
buffer as a packet of its own, with the time in the buffer when it is due:
jumping back and recordings then keep the subtitles.

One TS packet holds one cue, or one part of a long cue: a PES of
private_stream_1 (0xBD) whose PTS is the cue's start on the buffer's
timeline, then ``CUE_MAGIC``, the part and the number of parts, the
duration (90 kHz) and the text in UTF-8. Each rendition gets a PID of its
own from ``CUE_PID_BASE`` up (worked out from its name, as the audio
renditions' are). The PIDs are not in the PMT, as the song titles of
internet radio are not (lindvb.ipstream.META_PID): demuxers and other
players skip them.
"""

import zlib

TS_PACKET = 188
TS_SYNC = 0x47
PTS_HZ = 90000
PTS_WRAP = 1 << 33

CUE_PID_BASE = 0x1FC0
CUE_PIDS = 16
CUE_MAGIC = b"LVCUE\x01"
CUE_PARTS = 8                    # a cue longer than this many packets is cut short
_PES_HEAD = 14                   # start code, stream id, length, flags, PTS
_FIXED = len(CUE_MAGIC) + 2 + 4  # part, parts, duration
CUE_ROOM = TS_PACKET - 4 - 1 - _PES_HEAD - _FIXED   # text bytes a packet holds
_CUE_HEAD = bytes((TS_SYNC, 0x40 | CUE_PID_BASE >> 8))  # a packet that starts a cue (PIDs 0x1FCx)


def cue_pid(name, taken=()):
    """The PID of the subtitle rendition called *name* in the buffer;
    *taken* holds the PIDs already used (the next free one is taken then)."""
    first = zlib.crc32((name or "").encode("utf-8")) % CUE_PIDS
    for k in range(CUE_PIDS):
        pid = CUE_PID_BASE + (first + k) % CUE_PIDS
        if pid not in taken:
            return pid
    return None


def cue_pids(names):
    """{name: PID} for the renditions *names*, in their order."""
    out, taken = {}, set()
    for name in names:
        pid = cue_pid(name, taken)
        if pid is None:
            break
        out[name] = pid
        taken.add(pid)
    return out


def is_cue_pid(pid):
    return CUE_PID_BASE <= pid < CUE_PID_BASE + CUE_PIDS


def _pts_bytes(ticks):
    t = int(ticks) % PTS_WRAP
    return bytes((0x21 | ((t >> 29) & 0x0E), (t >> 22) & 0xFF, ((t >> 14) & 0xFE) | 1,
                  (t >> 7) & 0xFF, ((t << 1) & 0xFE) | 1))


def _pts_of(b):
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def _split(text, room, parts):
    """*text* in UTF-8 as at most *parts* pieces of at most *room* bytes,
    cut between characters."""
    data = text.encode("utf-8")
    out = []
    while data and len(out) < parts:
        piece = data[:room]
        if len(data) > room:
            piece = piece.decode("utf-8", "ignore").encode("utf-8")
        out.append(piece)
        data = data[len(piece):]
    return out or [b""]


def cue_packets(pid, text, start, end, cc=0):
    """(TS packets, next continuity counter) carrying the cue *text* from
    *start* to *end* (s on the buffer's timeline) on *pid*."""
    ticks = round(start * PTS_HZ)
    dur = max(0, min(0xFFFFFFFF, round((end - start) * PTS_HZ)))
    pieces = _split(text, CUE_ROOM, CUE_PARTS)
    out = []
    for i, piece in enumerate(pieces):
        body = CUE_MAGIC + bytes((i, len(pieces))) + dur.to_bytes(4, "big") + piece
        pes_len = 3 + 5 + len(body)                  # flags, header length, PTS, body
        pes = b"\0\0\1\xbd" + pes_len.to_bytes(2, "big") + b"\x80\x80\x05" + \
            _pts_bytes(ticks) + body
        head = bytes((TS_SYNC, 0x40 | (pid >> 8) & 0x1F, pid & 0xFF))
        room = TS_PACKET - 4 - len(pes)
        if room > 0:                                 # adaptation field stuffing
            af = bytes((room - 1,)) + (b"\0" + b"\xff" * (room - 2) if room > 1 else b"")
            out.append(head + bytes((0x30 | cc,)) + af + pes)
        else:
            out.append(head + bytes((0x10 | cc,)) + pes)
        cc = (cc + 1) & 0x0F
    return b"".join(out), cc


def parse_cue_packet(packet):
    """(pid, PTS ticks, duration ticks, part, parts, text bytes) of a packet
    made by cue_packets(), else None."""
    if len(packet) < TS_PACKET or packet[0] != TS_SYNC or not packet[1] & 0x40:
        return None
    pid = (packet[1] & 0x1F) << 8 | packet[2]
    if not is_cue_pid(pid):
        return None
    afc = (packet[3] >> 4) & 3
    i = 4 + (1 + packet[4] if afc & 2 else 0)
    if not afc & 1 or i + _PES_HEAD + _FIXED > TS_PACKET:
        return None
    pes = packet[i:TS_PACKET]
    if pes[:4] != b"\0\0\1\xbd" or not pes[7] & 0x80:
        return None
    end = 6 + int.from_bytes(pes[4:6], "big")
    body = pes[9 + pes[8]:end]
    if body[:len(CUE_MAGIC)] != CUE_MAGIC or len(body) < _FIXED:
        return None
    k = len(CUE_MAGIC)
    part, parts = body[k], body[k + 1]
    dur = int.from_bytes(body[k + 2:k + 6], "big")
    return pid, _pts_of(pes[9:14]), dur, part, parts, bytes(body[_FIXED:])


class CueReader:
    """Finds the cues in transport stream data as it goes by (aligned to its
    packets or not): :meth:`feed` gives [(position, pid, start s, end s,
    text)] of the cues whose last part is in the data."""

    def __init__(self):
        self._parts = {}             # (pid, PTS) -> [pieces]

    def feed(self, data):
        found = []
        for i in _find_all(data, _CUE_HEAD):
            if data[i + 2:i + 3] and data[i + 2] & 0xF0 != CUE_PID_BASE & 0xF0:
                continue
            got = parse_cue_packet(data[i:i + TS_PACKET])
            if got is None:
                continue
            pid, ticks, dur, part, parts, piece = got
            key = (pid, ticks)
            pieces = self._parts.setdefault(key, [None] * max(1, parts))
            if part < len(pieces):
                pieces[part] = piece
            if part + 1 >= len(pieces):
                del self._parts[key]
                if all(p is not None for p in pieces):
                    text = b"".join(pieces).decode("utf-8", "replace")
                    start = ticks / PTS_HZ
                    found.append((i, pid, start, start + dur / PTS_HZ, text))
        if len(self._parts) > 4 * CUE_PIDS:          # parts whose last never came
            self._parts.clear()
        return found


class CueFinder:
    """The player's side: the cues in the bytes it feeds from the buffer
    (lindvb.player.Feeder), each with its epoch (lindvb.tsclock) counted
    from the first video PES fed, which is the epoch the player's video
    clock starts with. The bytes just before the place playback starts go
    through :meth:`before` first: a cue that began there still shows."""

    EARLY = 64                   # cues kept until the first video PES is fed

    def __init__(self):
        from .tsclock import Epochs
        self._reader = CueReader()
        self._epochs = Epochs()
        self._base = None            # the epoch of the first video PES fed
        self._early = []             # (pid, start, end, text, epoch) before that

    def before(self, data):
        """Bytes before the start: their cues wait for the first PES fed."""
        self._early += self._find(data)[0]
        del self._early[:-self.EARLY]

    def feed(self, data):
        """Bytes fed: [(pid, start s, end s, text, epoch)] of their cues,
        and of those found before, once the first video PES is fed."""
        found, base = self._find(data, self._base is None)
        if self._base is None:
            if base is None:
                self._early += found
                del self._early[:-self.EARLY]
                return []
            self._base = base
            found, self._early = self._early + found, []
        return [(pid, t0, t1, text, e - self._base) for pid, t0, t1, text, e in found
                if e >= self._base]      # from before a jump: not what plays

    def _find(self, data, first=False):
        from .tsclock import video_pes
        cues = self._reader.feed(data)
        marks = [(pos, k) for k, (pos, *_rest) in enumerate(cues)]
        if first:
            pes = video_pes(data)
            if pes:
                marks.append((pes[0][0] + 1, -1))    # just after the first PES
        marks.sort()
        epochs = self._epochs.at(data, [pos for pos, _k in marks])
        at = dict(zip((k for _pos, k in marks), epochs))
        found = [(pid, t0, t1, text, at[k])
                 for k, (_pos, pid, t0, t1, text) in enumerate(cues)]
        return found, at.get(-1)


def _find_all(data, needle):
    i = data.find(needle)
    while i >= 0:
        yield i
        i = data.find(needle, i + 1)


# -- the reader's side: writing the cues ------------------------------------------

CUE_LEAD = 2.0                   # s: a cue goes into the buffer this long before it is due
CUE_HOLD = 500                   # cues held at most (the oldest go first)
CUE_STALE = 30.0                 # s: a cue over this long before the buffer got to it is left out
CUE_PICTURES = 2000              # video PES remembered: when each was handed on
KICK_STEP = 0.5                  # s of new segment time that wakes the loaders
KICK_GAP = 0.5                   # s at least between two wake-ups


class CueWriter:
    """The buffer reader's side (lindvb.ipstream.GstReader): the cues of the
    subtitle renditions (lindvb.vttload, MPEG-TS time) are held until the
    buffer is about to reach them, then go in as packets with their time on
    the buffer's timeline.

    That timeline is the reader's own: mpegtsmux stamps what it packs anew.
    A video clock (lindvb.tsclock.TsClock) ties the two together: the video
    PES of the segments (at the tsdemux inside the HLS demuxer) against the
    video PES the reader hands on. A new pipeline (the reader connected
    again) starts a new clock; the cues still held then go in on its
    timeline. Holding them until shortly before they are due keeps each cue
    in the same stretch of buffer as its picture, so the player maps it with
    the offset of that stretch (tsclock epochs).

    The loaders read their playlists every target duration; the demuxer
    may have the picture of a new segment up to that long before them, and
    a cue that comes after its picture has been played is lost. So new
    segment time at the demuxer wakes them (*on_new*, at most every
    KICK_GAP s)."""

    def __init__(self, pids=None, lead=CUE_LEAD):
        import threading
        from .tsclock import TsClock
        self._TsClock = TsClock
        self._lock = threading.Lock()
        self.pids = dict(pids or {})      # rendition name -> PID
        self.lead = lead
        self.clock = TsClock()
        self._held = []                   # [(start, end, name, text)], MPEG-TS s, by start
        self._cc = {}
        self._ready = b""                 # packets waiting for data that starts on a packet
        self.last = None                  # PTS (s) of the latest video PES handed on
        self.on_new = None                # wakes the loaders (new segment time came)
        self._kick_at = None              # segment PTS (ticks) of the last wake-up
        self._kick_time = 0.0
        self.kicks = 0
        self._aligned = True              # the last data handed on ended on a packet
        self.between = 0                  # cues written between two pieces of video
        self.came = self.written = self.late = self.stale = self.dropped = 0
        self.behind = 0.0                 # s: the most a cue went in after its picture came
        from collections import deque
        self._came = deque(maxlen=CUE_PICTURES)   # (PTS s, monotonic s) of the video handed on

    def set_pids(self, pids):
        with self._lock:
            self.pids = dict(pids)

    def restart(self):
        """A new pipeline: its muxer starts its PTS again."""
        with self._lock:
            self.clock = self._TsClock()
            self.last = None
            self._kick_at = None
            self._came.clear()

    def watch_input(self, tsdemux):
        """The tsdemux inside the HLS demuxer: its sink sees the segments."""
        from gi.repository import Gst
        clock = self.clock
        tsdemux.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, clock._probe_ts)

    def cue(self, name, text, start, end, _mapped=True):
        """A cue of rendition *name* (loader thread): MPEG-TS times in s (a
        segment without X-TIMESTAMP-MAP has its times on MPEG-TS time 0)."""
        import bisect
        with self._lock:
            if name not in self.pids:
                return
            self.came += 1
            bisect.insort(self._held, (start, end, name, text))
            if len(self._held) > CUE_HOLD:
                del self._held[0]
                self.dropped += 1

    def out(self, data, aligned=True, same=False):
        """*data* the reader hands on (streaming thread): the packets of the
        cues due now, to go after it (after the first video PES of a new
        pipeline, so the player counts them in its epoch); with *same* the
        data is also the segments' (the transport stream passed on as it
        came)."""
        import time
        from .tsclock import PTS_HZ, video_pes
        now = time.monotonic()
        with self._lock:
            clock = self.clock
            if same:
                clock.feed_ts(data)
            for _pos, p in video_pes(data):
                clock.frame(p / PTS_HZ)
                self.last = p / PTS_HZ
                self._came.append((self.last, now))
            kick = self._new_segment_time(clock)
            out = self._ready + self._due(clock)
            self._aligned = aligned
            if not aligned:
                self._ready, out = out, b""
            else:
                self._ready = b""
        if kick and self.on_new is not None:
            self.on_new()
        return out

    def due_now(self):
        """Loader thread, after a cue came: the packets of the cues due now,
        to go into the stream at once rather than with the next piece of
        video (which comes a whole segment later: the demuxer hands on a
        segment at a time); b"" when the stream is not on a packet
        boundary."""
        with self._lock:
            if self._ready or not self._aligned:
                return b""
            out = self._due(self.clock)
            self.between += bool(out)
            return out

    def _new_segment_time(self, clock, now=None):
        """True when the loaders are to look at their playlists: the newest
        video PES of the segments went KICK_STEP on (or back: a new start)."""
        import time
        from .tsclock import JUMP_BACK, PTS_HZ, PTS_WRAP
        cur = clock._last_in
        if cur is None:
            return False
        if self._kick_at is not None:
            d = (cur - self._kick_at) % PTS_WRAP
            if d >= PTS_WRAP // 2:
                d -= PTS_WRAP
            if -JUMP_BACK <= d < KICK_STEP * PTS_HZ:
                return False
        now = time.monotonic() if now is None else now
        if now - self._kick_time < KICK_GAP:
            return False
        self._kick_at, self._kick_time = cur, now
        self.kicks += 1
        return True

    def _due(self, clock):
        if self.last is None or clock.offset is None:
            return b""
        out = []
        while self._held:
            start, end, name, text = self._held[0]
            t0 = clock.stream_time(start, self.last)
            if t0 > self.last + self.lead:
                break
            del self._held[0]
            t1 = clock.stream_time(end, t0)
            if t1 < self.last - CUE_STALE:
                self.stale += 1                   # long over: nobody plays that far behind
                continue
            if t0 < self.last:
                # after its picture (the loader got it later than the
                # demuxer the video): the player still shows what is left
                self.late += 1
                self.behind = max(self.behind, self._after_picture(t0))
            pid = self.pids[name]
            packets, self._cc[pid] = cue_packets(pid, text, t0, t1, self._cc.get(pid, 0))
            out.append(packets)
            self.written += 1
        return b"".join(out)

    def _after_picture(self, t0):
        """s since the first picture at or after *t0* was handed on: how long
        playing at the live end has to wait for the cue to be in time."""
        import time
        came = next((wall for pts, wall in self._came if pts >= t0), None)
        return time.monotonic() - came if came is not None else 0.0

    def report(self):
        with self._lock:
            names = ", ".join(f"{n} on 0x{p:04X}" for n, p in self.pids.items()) or "none"
            return (f"subtitle cues in the buffer ({names}): {self.came} came, "
                    f"{self.written} written ({self.late} after their picture, at most "
                    f"{self.behind:.2f} s after it came), {self.stale} long over, {len(self._held)} held, "
                    f"{self.dropped} thrown away, loaders woken {self.kicks} times, "
                    f"{self.between} times written between two pieces of video; "
                    f"video: {self.clock.summary()}")
