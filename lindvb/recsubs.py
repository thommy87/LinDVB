# SPDX-License-Identifier: GPL-2.0-or-later
"""The subtitles of the timeshift buffer in recordings (stage H).

With "Subtitles of HLS channels in the buffer" on, the WebVTT cues of an
HLS channel are in the buffer as packets of their own (lindvb.cuebuffer),
outside the PMT. A TS recording keeps them, but other players skip them,
and remuxing drops them. So a recording takes them out as it reads the
buffer:

* TS: an .srt file of the same name beside the recording for each
  rendition (:class:`SrtFile`). The times are those of the file as
  players show it, counted from its first sound or picture
  (:class:`FileTimes`).
* MKV: a text track for each rendition (lindvb.remux), timed by the
  remuxer's own clock of the picture (lindvb.tsclock).
"""

from pathlib import Path

from .cuebuffer import is_cue_pid
from .tsclock import PTS_HZ, PTS_WRAP, TS_PACKET, is_jump, pes_stream_pts

FRAME = 0.04            # s between the last picture of a stretch and the first of the next


def srt_time(t):
    """``hh:mm:ss,mmm`` of *t* seconds."""
    ms = max(0, round(t * 1000))
    s, ms = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ticks_between(a, b):
    """*b* - *a* in 90 kHz ticks, the wrap of the PTS taken into account."""
    d = (b - a) % PTS_WRAP
    return d - PTS_WRAP if d >= PTS_WRAP // 2 else d


class FileTimes:
    """The time in a TS file, as players show it, of the MPEG-TS times in
    its bytes. Players count from the first sound or picture of the file.
    After a jump of the PTS (the reader connected again: a new epoch, see
    lindvb.tsclock) the next stretch follows on from where the one before
    it ended. Epochs are counted from the first video PES, as
    lindvb.cuebuffer.CueFinder counts them."""

    EARLY = 64                   # sound PES kept until the first picture comes

    def __init__(self):
        self._rest = b""
        self.epoch = -1              # none before the first video PES
        self.base = {}               # epoch -> PTS ticks its time 0 is at
        self.start = {}              # epoch -> file time (s) the stretch starts at
        self._last = None            # the last video PES PTS
        self._top = None             # the latest video PES PTS of the epoch
        self._early = []             # sound PES PTS before the first video PES
        self._settle = None          # till here an earlier PTS can still start the stretch

    def feed(self, data):
        """The bytes written to the file, aligned to its packets or not."""
        data = self._rest + data
        n = len(data)
        i = data.find(b"\x47")
        while 0 <= i and i + TS_PACKET <= n:
            if data[i] != 0x47:
                i = data.find(b"\x47", i + 1)
                continue
            if data[i + 1] & 0x40 and not is_cue_pid((data[i + 1] & 0x1F) << 8 | data[i + 2]):
                got = pes_stream_pts(data[i:i + TS_PACKET])
                if got is not None:
                    self._pes(*got)
            i += TS_PACKET
        self._rest = data[i:] if i >= 0 else b""

    def _pes(self, sid, pts):
        if 0xe0 <= sid <= 0xef:
            self._video(pts)
        elif self.epoch < 0:
            self._early.append(pts)
            del self._early[:-self.EARLY]
        else:
            self._earlier(pts)

    def _earlier(self, pts):
        """In its first second a stretch starts at its earliest PTS: the
        sound that goes with the first pictures can come after them, and
        so can pictures shown before the first one sent (B-frames)."""
        if self._settle is None:
            return
        if _ticks_between(pts, self._settle) <= 0:
            self._settle = None
            return
        ahead = _ticks_between(pts, self.base[self.epoch])
        if 0 < ahead <= PTS_HZ:
            self.base[self.epoch] = pts

    def _video(self, pts):
        if self._last is None:
            self.epoch = 0
            near = [p for p in self._early if 0 <= _ticks_between(p, pts) <= PTS_HZ]
            self.base[0] = min(near + [pts], key=lambda p: _ticks_between(pts, p))
            self.start[0] = 0.0
            self._settle = (pts + PTS_HZ) % PTS_WRAP
            self._early = []
            self._top = pts
        elif is_jump(self._last, pts):
            span = _ticks_between(self.base[self.epoch], self._top) / PTS_HZ
            self.start[self.epoch + 1] = self.start[self.epoch] + max(0.0, span) + FRAME
            self.epoch += 1
            self.base[self.epoch] = pts
            self._settle = (pts + PTS_HZ) % PTS_WRAP
            self._top = pts
        else:
            self._earlier(pts)
            if _ticks_between(self._top, pts) > 0:
                self._top = pts
        self._last = pts

    def file_time(self, mpegts, epoch):
        """The file time (s) of MPEG-TS time *mpegts* (s) in *epoch*, or
        None before its first picture."""
        base = self.base.get(epoch)
        if base is None:
            return None
        ticks = round(mpegts * PTS_HZ) % PTS_WRAP
        return self.start[epoch] + _ticks_between(base, ticks) / PTS_HZ


class SrtFile:
    """One .srt file. Each cue is written as it comes, so a recording that
    is cut off keeps its subtitles. The file is made with its first cue."""

    def __init__(self, path):
        self.path = Path(path)
        self.count = 0
        self._f = None

    def write(self, start, end, text):
        """A cue from *start* to *end* (file time, s); False when it is
        over before the file starts or has no text."""
        text = "\n".join(ln for ln in (text or "").splitlines() if ln.strip())
        if end <= 0 or end <= start or not text:
            return False
        if self._f is None:
            self._f = open(self.path, "w", encoding="utf-8")
        self.count += 1
        self._f.write(f"{self.count}\n{srt_time(max(0.0, start))} --> {srt_time(end)}\n"
                      f"{text}\n\n")
        self._f.flush()
        return True

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None


def srt_paths(path, names):
    """{rendition: .srt path} beside the recording *path*: the first
    rendition gets the recording's own name, as players look for it, and
    each further one its name after it (``Film.Deutsch.srt``)."""
    path = Path(path)
    out = {}
    for k, name in enumerate(names):
        if k == 0:
            out[name] = path.with_suffix(".srt")
            continue
        label = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip() or str(k)
        p = path.with_name(f"{path.stem}.{label}.srt")
        while p in out.values():
            p = p.with_name(f"{p.stem}_{k}.srt")
        out[name] = p
    return out


class TsSubtitles:
    """The cues of a TS recording into its .srt files. :meth:`feed` takes
    the bytes as read from the buffer and as written to the file."""

    def __init__(self, path, renditions):
        from .cuebuffer import CueFinder
        self.path = Path(path)
        self._renditions = renditions        # () -> {rendition: PID}, as the reader knows them
        self._finder = CueFinder()
        self._times = FileTimes()
        self.files = {}                      # PID -> SrtFile
        self.written = self.dropped = 0

    def feed(self, read, written):
        cues = self._finder.feed(read)
        self._times.feed(written)
        for pid, start, end, text, epoch in cues:
            self._cue(pid, start, end, text, epoch)

    def _cue(self, pid, start, end, text, epoch):
        f = self.files.get(pid) or self._file(pid)
        t0 = self._times.file_time(start, epoch)
        if f is None or t0 is None:
            self.dropped += 1
            return
        if f.write(t0, t0 + (end - start), text):
            self.written += 1
        else:
            self.dropped += 1

    def _file(self, pid):
        pids = self._renditions() or {}
        paths = srt_paths(self.path, list(pids))
        for name, p in pids.items():
            if p == pid:
                self.files[pid] = SrtFile(paths[name])
                return self.files[pid]
        return None

    def close(self):
        for f in self.files.values():
            f.close()
        return [f.path for f in self.files.values() if f.count]

    def report(self):
        return f"{self.written} cues into {sum(1 for f in self.files.values() if f.count)} " \
               f".srt files, {self.dropped} not (over before the file starts or no rendition)"
