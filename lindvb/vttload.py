# SPDX-License-Identifier: GPL-2.0-or-later
"""WebVTT subtitles of an HLS rendition, loaded by LinDVB itself.

PBS and ARD-alpha over HLS: with the WebVTT rendition picked, hlsdemux2
holds picture and sound back, because the subtitle segments carry no time
stamps and its subtitle track never moves on (debug log of 27.11.2026,
lindvb.flowwatch). If LinDVB loads the subtitles itself, the demuxer never
gets the subtitle track and cannot wait for it; the text cue renderer of
stage F draws them.

First in the shadow (debug mode): while the old path plays the rendition,
:class:`WebVttLoader` loads its media playlist and segments on a thread of
its own and :class:`VttShadow` says what it found, and how its cue times
lie to those of the old path, which is what taking over needs.

Times: a segment's ``X-TIMESTAMP-MAP=MPEGTS:<pts>,LOCAL:<time>`` ties its
cue times to the MPEG-TS time of the picture (90 kHz). Without it, a cue
has no time that can be tied to the picture.
"""

import re
import threading
import urllib.parse
from http.client import HTTPException

from .ipstream import fetch_manifest
from .subrender import cue_text

PTS_WRAP = 1 << 33               # MPEG-TS time stamps (90 kHz) wrap here
SEGMENT_LIMIT = 256 * 1024       # bytes read of a subtitle segment at most
LIVE_START = 4                   # segments of a live playlist loaded at the start
VOD_LIMIT = 300                  # segments of a playlist with an end loaded at most
KEEP = 500                       # cues kept
GIVE_UP = 5                      # errors in a row before the loader stops

_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
_ARROW = re.compile(r"^\s*(\S+)\s+-->\s+(\S+)")


def vtt_time(text):
    """Seconds of a WebVTT time stamp ("01:02:03.456", "02:03.456"), None
    when it is none."""
    m = _TIME.fullmatch(text.strip())
    if not m:
        return None
    h, mi, s, ms = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def _timestamp_map(line):
    """(MPEG-TS time in s, local time in s) of an X-TIMESTAMP-MAP line."""
    mpegts = local = None
    for part in line.split("=", 1)[1].split(","):
        key, _, value = part.strip().partition(":")
        if key == "MPEGTS" and value.strip().isdigit():
            mpegts = int(value.strip()) % PTS_WRAP / 90000
        elif key == "LOCAL":
            local = vtt_time(value)
    return (mpegts, local) if mpegts is not None and local is not None else None


def parse_vtt(text):
    """(timestamp map or None, [(start s, end s, text)]) of one WebVTT
    segment; cue times as written (local)."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or not lines[0].startswith("WEBVTT"):
        return None, []
    tmap, cues, i = None, [], 1
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("X-TIMESTAMP-MAP="):
            tmap = _timestamp_map(ln)
        m = _ARROW.match(ln)
        if m:
            start, end = vtt_time(m.group(1)), vtt_time(m.group(2))
            body = []
            i += 1
            while i < len(lines) and lines[i].strip():
                body.append(lines[i])
                i += 1
            text = cue_text("\n".join(body).encode())
            if start is not None and end is not None and text:
                cues.append((start, end, text))
        i += 1
    return tmap, cues


def media_segments(text, base):
    """(target duration s, live, [(sequence number, duration s, address)])
    of an HLS media playlist."""
    target, seq, live, out, duration = 6.0, 0, True, [], 0.0
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("#EXT-X-TARGETDURATION:"):
            target = _number(ln, target)
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(_number(ln, seq))
        elif ln.startswith("#EXTINF:"):
            duration = _number(ln.split(",")[0], 0.0)
        elif ln.startswith("#EXT-X-ENDLIST"):
            live = False
        elif ln and not ln.startswith("#"):
            out.append((seq + len(out), duration, urllib.parse.urljoin(base, ln)))
            duration = 0.0
    return target, live, out


def _number(line, default):
    try:
        return float(line.split(":", 1)[1])
    except (IndexError, ValueError):
        return default


def _fetch(url, limit=SEGMENT_LIMIT):
    """(text, final address) over HTTP, unpacked when compressed."""
    text, final, _compressed = fetch_manifest(url, limit=limit, timeout=6)
    return text, final


class LoaderState:
    def __init__(self):
        self.playlists = 0              # times the media playlist was read
        self.segments = 0               # segments loaded
        self.empty = 0                  # of them without a cue
        self.mapped = 0                 # of them with X-TIMESTAMP-MAP
        self.cues = 0
        self.errors = 0
        self.last_error = ""
        self.gave_up = False
        self.capped = False             # a playlist with an end: stopped at VOD_LIMIT


class WebVttLoader:
    """Loads the WebVTT segments of one HLS subtitle rendition on a thread
    of its own; *on_cue(text, start, end, mapped)* gets each cue (on that
    thread), its times in s: MPEG-TS time when *mapped*, else as written.

    *uri* is the rendition's URI as the master playlist *master* writes it;
    a relative one is taken from where the master really is (after its
    redirects, a DVB-I gateway's for one), so the master is read first
    then."""

    def __init__(self, master, uri, on_cue, fetch=_fetch, sleep=None):
        self.master = master
        self.uri = uri
        # the media playlist's address, once known
        self.url = uri if urllib.parse.urlsplit(uri).scheme else None
        self.on_cue = on_cue
        self.state = LoaderState()
        self._fetch = fetch
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._next = None               # the sequence number of the next segment to load
        self._loaded = 0                # segments loaded of a playlist with an end
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="lindvb-webvtt", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        errors = 0
        while not self._stop.is_set():
            try:
                wait = self.step()
                errors = 0
            except (OSError, ValueError, HTTPException) as e:
                errors += 1
                self.state.errors += 1
                self.state.last_error = str(e)[:120]
                if errors >= GIVE_UP:
                    self.state.gave_up = True
                    return
                wait = 2.0
            if wait is None:
                return                  # a playlist with an end: all loaded
            self._sleep(wait)

    def step(self):
        """Read the playlist once and load the segments not loaded yet;
        returns how long to wait before the next look (None: done)."""
        if self.url is None:
            _text, base = self._fetch(self.master, limit=512 * 1024)
            self.url = urllib.parse.urljoin(base, self.uri)
        text, final = self._fetch(self.url, limit=1024 * 1024)
        st = self.state
        st.playlists += 1
        target, live, segments = media_segments(text, final)
        if segments and self._next is not None and segments[-1][0] + 1 < self._next:
            self._next = None           # the numbers went back: the stream started anew
        if self._next is None and live:
            new = segments[-LIVE_START:]    # start where the picture starts: near the end
        else:
            new = [s for s in segments if self._next is None or s[0] >= self._next]
        if not live:
            room = VOD_LIMIT - self._loaded
            st.capped = len(new) > room
            new = new[:max(room, 0)]
        for seq, _duration, address in new:
            if self._stop.is_set():
                return None
            self._load(address)
            self._next = seq + 1
            self._loaded += 1
        if not live:
            return None
        return min(max(target / (2 if not new else 1), 1.0), 10.0)

    def _load(self, address):
        text, _final = self._fetch(address)
        tmap, cues = parse_vtt(text)
        st = self.state
        st.segments += 1
        st.mapped += tmap is not None
        if not cues:
            st.empty += 1
        for start, end, cue in cues:
            st.cues += 1
            if tmap is not None:
                mpegts, local = tmap
                self.on_cue(cue, mpegts + start - local, mpegts + end - local, True)
            else:
                self.on_cue(cue, start, end, False)


class VttShadow:
    """What the loader found beside the old path (hlsdemux2's subtitle
    track, subparse); the report line says it, and how the loader's cue
    times lie to the old path's for the same texts: the offset between the
    two timelines, and whether it stays the same (what drawing them at the
    right time needs)."""

    def __init__(self, name):
        self.name = name
        self.new = []                   # [(text, start s, mapped)]
        self.old = []                   # [(text, stream time s)]

    def saw_new(self, text, start, _end, mapped):
        self.new.append((text, start, mapped))
        del self.new[:-KEEP]

    def saw_old(self, text, t_ns):
        if text and t_ns is not None:
            self.old.append((text, t_ns / 1e9))
            del self.old[:-KEEP]

    def offsets(self):
        """The loader's time minus the old path's, for each text both had
        (the nearest of the same text)."""
        out = []
        for text, t in self.old:
            mine = [s for x, s, mapped in self.new if x == text and mapped]
            if mine:
                out.append(min(mine, key=lambda s: abs(s - t)) - t)
        return out

    def report(self, state):
        st = state
        head = (f"WebVTT loader (shadow, {self.name}): playlist read {st.playlists} times, "
                f"{st.segments} segments ({st.empty} without cues, {st.mapped} with "
                f"X-TIMESTAMP-MAP), {st.cues} cues; the old path {len(self.old)} cues")
        tail = []
        if st.errors:
            tail.append(f"{st.errors} errors, last {st.last_error!r}"
                        + (", gave up" if st.gave_up else ""))
        if st.capped:
            tail.append(f"stopped after {VOD_LIMIT} segments")
        offsets = self.offsets()
        if offsets:
            spread = max(offsets) - min(offsets)
            tail.append(f"{len(offsets)} of the old path's cues found, its times "
                        f"{_signed(sum(offsets) / len(offsets))} s off theirs "
                        f"(spread {spread:.3f} s)")
        elif self.old and self.new:
            tail.append("none of the old path's cues found")
        if self.new:
            tail.append(f"first {self.new[0][0][:40]!r}")
        return head + "".join(f"; {t}" for t in tail)


def _signed(x):
    return f"{x:+.3f}"
