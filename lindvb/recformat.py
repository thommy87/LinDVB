"""Recording containers, extra streams and file naming (pure logic, no GTK/GStreamer).

A timer records TV channels into one of TV_FORMATS and radio channels into one
of RADIO_FORMATS. Next to video and audio it can keep extra streams. Not every
container can hold every extra stream, so the formats on offer depend on the
extras that are ticked:

  * Teletext and EPG data only exist as MPEG-TS streams: they force TS.
  * Subtitles are kept in TS, or in MKV as a text track converted from the
    teletext subtitle page (DVB bitmap subtitles stay TS-only).
  * All audio tracks fit into TS, MPG, MP4 and MKV (tracks a container cannot
    carry, such as AC-3 in MPG, are left out).
"""

import re
from datetime import datetime

from .ca import TS_PACKET, TS_SYNC, ts_payload_offset, ts_pid

TV_FORMATS = ["TS", "MPG", "MP4", "MKV"]
RADIO_FORMATS = ["Audio", "TS"]          # "Audio": the native MP2 / MP3 / AAC / AC-3 stream

FORMAT_LABELS = {
    "TS": "MPEG-TS (.ts)",
    "MPG": "MPEG-PS (.mpg)",
    "MP4": "MP4 (.mp4)",
    "MKV": "Matroska (.mkv)",
    "Audio": "MP2 / MP3 / AAC / AC3",
}

TS_SYNC_BYTE = bytes([TS_SYNC])

EXTENSIONS = {"TS": ".ts", "MPG": ".mpg", "MP4": ".mp4", "MKV": ".mkv", "Audio": ".mp2"}

# Extra streams, in the order the timer dialog shows them.
EXTRAS = ("teletext", "subtitles", "all_audio", "epg_data")
EXTRA_LABELS = {
    "teletext": "Teletext (TS)",
    "subtitles": "Subtitles (TS / MKV)",
    "all_audio": "All audio tracks (TS / MPG / MP4 / MKV)",
    "epg_data": "EPG data (TS)",
}

# Which extra streams each container can hold.
CAN_HOLD = {
    "TS": set(EXTRAS),
    "MPG": {"all_audio"},
    "MP4": {"all_audio"},
    "MKV": {"subtitles", "all_audio"},
    "Audio": set(),
}

TIMER_ACTIONS = ["Record", "Tune only", "Record and tune"]
AFTER_ACTIONS = ["None", "Standby", "Hibernate", "Power off", "Quit LinDVB"]

PLACEHOLDERS = [
    ("%date", "Date (MM-DD)"),
    ("%year", "Year"),
    ("%month", "Month"),
    ("%day", "Day of month"),
    ("%weekday", "Weekday (Mon, Tue, ...)"),
    ("%time", "Start time (HH-MM)"),
    ("%end", "End time (HH-MM)"),
    ("%station", "Channel name"),
    ("%name", "Programme name"),
    ("%event", "Programme name (same as %name)"),
    ("/", "Sub-folder separator"),
]


def allowed_formats(extras, radio=False):
    """Formats that can hold every ticked extra stream, in display order."""
    wanted = {e for e in extras if e in EXTRAS}
    formats = RADIO_FORMATS if radio else TV_FORMATS
    return [f for f in formats if wanted <= CAN_HOLD[f]]


def effective_format(fmt, extras, radio=False):
    """The chosen format, or TS when it cannot hold the ticked extras."""
    allowed = allowed_formats(extras, radio)
    return fmt if fmt in allowed else "TS"


def keep_pids(channel, extras, fmt):
    """PIDs to keep from the captured transport stream, or None for all.

    None means the stream layout is unknown (no PMT PID), so nothing is dropped.
    """
    if not channel.pmt_pid:
        return None
    extras = set(extras)
    keep = {0x00, 0x01, 0x10, 0x11, 0x14, channel.pmt_pid}      # PAT CAT NIT SDT TDT PMT
    if "epg_data" in extras and fmt == "TS":
        keep.add(0x12)                                          # EIT
    for pid in (channel.pcr_pid, channel.video_pid):
        if pid:
            keep.add(pid)
    audio = list(channel.audio_pids)
    keep.update(audio if "all_audio" in extras else audio[:1])
    if channel.teletext_pid and ("teletext" in extras or "subtitles" in extras):
        keep.add(channel.teletext_pid)        # teletext also carries subtitle pages
    if "subtitles" in extras:
        keep.update(channel.subtitle_pids)
    return keep


def _safe(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .") or "_"


def expand_scheme(scheme, station, name, start, end=None):
    """Expand the naming scheme to a relative path without extension.

    "/" in the scheme starts a sub-folder; every part is made file-system safe.
    """
    dt = datetime.fromtimestamp(start)
    de = datetime.fromtimestamp(end if end is not None else start)
    name = name or "Recording"
    rep = {"%year": dt.strftime("%Y"), "%month": dt.strftime("%m"), "%day": dt.strftime("%d"),
           "%weekday": dt.strftime("%a"), "%date": dt.strftime("%m-%d"),
           "%time": dt.strftime("%H-%M"), "%end": de.strftime("%H-%M"),
           "%station": station, "%name": name, "%event": name}
    parts = []
    for part in (scheme or "%station_%date_%time_%name").split("/"):
        for k in sorted(rep, key=len, reverse=True):
            part = part.replace(k, rep[k].replace("/", "_"))
        if part.strip():
            parts.append(_safe(part))
    return "/".join(parts) or "Recording"


class PidFilter:
    """Keeps only the wanted PIDs of a transport stream and finds the PMT on the way.

    Data may arrive in pieces that do not end on a packet boundary; the rest is
    kept for the next call.
    """

    def __init__(self, pids=None, pmt_pid=0):
        self.pids = pids
        self.pmt_pid = pmt_pid
        self.pmt = None              # first complete PMT section seen
        self._rest = b""

    def feed(self, data):
        data = self._rest + data
        if self.pids is None and (self.pmt is not None or not self.pmt_pid):
            n = len(data) - len(data) % TS_PACKET
            self._rest = data[n:]
            return data[:n]
        out = []
        mv = memoryview(data)
        i, size = 0, len(data)
        while i + TS_PACKET <= size:
            if data[i] != TS_SYNC:                     # resync on a lost packet boundary
                j = data.find(TS_SYNC_BYTE, i + 1)
                if j < 0:
                    i = size
                    break
                i = j
                continue
            pkt = mv[i:i + TS_PACKET]
            pid = ts_pid(pkt)
            if self.pmt is None and pid == self.pmt_pid and pkt[1] & 0x40:
                self._grab_pmt(bytes(pkt))
            if self.pids is None or pid in self.pids:
                out.append(pkt)
            i += TS_PACKET
        self._rest = data[i:]
        return b"".join(out)

    def _grab_pmt(self, pkt):
        off = ts_payload_offset(pkt)
        if off is None or off >= TS_PACKET:
            return
        sec = pkt[off + 1 + pkt[off]:]
        if len(sec) >= 3 and sec[0] == 0x02:
            length = ((sec[1] & 0x0F) << 8) | sec[2]
            if 3 + length <= len(sec):
                self.pmt = sec[:3 + length]


def audio_extension(caps_name, mpegversion=0, layer=0):
    """File extension for a native audio stream described by its caps."""
    if caps_name == "audio/x-ac3":
        return ".ac3"
    if caps_name == "audio/x-eac3":
        return ".eac3"
    if caps_name == "audio/mpeg":
        if mpegversion == 1:
            return ".mp3" if layer == 3 else ".mp2"
        return ".aac"
    return ".audio"
