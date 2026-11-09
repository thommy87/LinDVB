# SPDX-License-Identifier: GPL-2.0-or-later
"""Recording containers, extra streams and file naming (pure logic, no GTK/GStreamer).

A timer records TV channels into one of TV_FORMATS and radio channels into one
of RADIO_FORMATS. Next to video and audio it can keep extra streams. Not every
container can hold every extra stream, so the formats on offer depend on the
extras that are ticked:

  * Teletext and EPG data only exist as MPEG-TS streams: they force TS.
  * Subtitles are kept in TS, or in MKV as a text track converted from the
    teletext subtitle page (DVB bitmap subtitles stay TS-only).

The audio tracks are chosen separately (AUDIO_MODES): all of them, the ones in
the preferred language, or only the first. TS, MPG and MKV keep several tracks
(tracks a container cannot carry, such as AC-3 in MPG, are left out); MP4 and
the native radio audio file keep one, the preferred language when it is on air.

Internet streams (IPTV, HLS, DASH) carry no teletext, subtitle or EPG data
streams, so their extras are greyed out and every format is on offer. HLS and
DASH streams are recorded with one audio track (see choose_streams).
"""

import re
from datetime import datetime

from .i18n import N_, _
from .ca import TS_PACKET, TS_SYNC, parse_pmt, ts_payload_offset, ts_pid

TV_FORMATS = ["TS", "MPG", "MP4", "MKV"]
RADIO_FORMATS = ["Audio", "TS"]          # "Audio": the native MP2 / MP3 / AAC / AC-3 stream

FORMAT_LABELS = {
    "TS": _("MPEG-TS (.ts)"),
    "MPG": _("MPEG-PS (.mpg)"),
    "MP4": "MP4 (.mp4)",
    "MKV": _("Matroska (.mkv)"),
    "Audio": "MP2 / MP3 / AAC / AC3",
}

TS_SYNC_BYTE = bytes([TS_SYNC])

EXTENSIONS = {"TS": ".ts", "MPG": ".mpg", "MP4": ".mp4", "MKV": ".mkv", "Audio": ".mp2"}

# Extra streams, in the order the timer dialog shows them.
EXTRAS = ("teletext", "subtitles", "epg_data", "all_streams")
EXTRA_LABELS = {
    "teletext": _("Teletext (TS)"),
    "subtitles": _("Subtitles (TS / MKV)"),
    "epg_data": _("EPG data (TS)"),
    "all_streams": _("All streams of the channel (TS)"),
}
# extras ticked when nothing says otherwise ("all streams" is opt-in)
EXTRA_DEFAULTS = {"teletext": True, "subtitles": True, "epg_data": True, "all_streams": False}

# Which extra streams each container can hold.
CAN_HOLD = {
    "TS": set(EXTRAS),
    "MPG": set(),
    "MP4": set(),
    "MKV": {"subtitles"},
    "Audio": set(),
}

# Which audio tracks to record.
AUDIO_MODES = ["all", "language", "first"]
AUDIO_MODE_LABELS = {
    "all": _("All audio tracks"),
    "language": _("Preferred language only"),
    "first": _("First audio track only"),
}
# Containers that keep a single audio track.
SINGLE_AUDIO = {"MP4", "Audio"}

# stored in timers.json; translated where they are shown
TIMER_ACTIONS = [N_("Record"), N_("Tune only"), N_("Record and tune")]
AFTER_ACTIONS = [N_("None"), N_("Standby"), N_("Hibernate"), N_("Power off"), N_("Quit LinDVB")]

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


def usable_extras(extras, iptv=False):
    """The ticked extra streams a channel can deliver (none for internet streams)."""
    return [] if iptv else [e for e in extras if e in EXTRAS]


def allowed_formats(extras, radio=False, iptv=False):
    """Formats that can hold every ticked extra stream, in display order."""
    wanted = set(usable_extras(extras, iptv))
    formats = RADIO_FORMATS if radio else TV_FORMATS
    return [f for f in formats if wanted <= CAN_HOLD[f]]


def effective_format(fmt, extras, radio=False, iptv=False):
    """The chosen format, or TS when it cannot hold the ticked extras."""
    allowed = allowed_formats(extras, radio, iptv)
    return fmt if fmt in allowed else "TS"


def unavailable_reason(fmt, extras, radio=False, iptv=False):
    """Why *fmt* is greyed out, or "" when it can be chosen."""
    if fmt in allowed_formats(extras, radio, iptv):
        return ""
    missing = [EXTRA_LABELS[e].split(" (")[0].lower()
               for e in usable_extras(extras, iptv) if e not in CAN_HOLD[fmt]]
    return f"{FORMAT_LABELS[fmt].split(' (')[0]} cannot hold {' and '.join(missing)}."


IPTV_EXTRAS_NOTE = _("Internet streams have no teletext, subtitle or EPG data streams.")


def audio_mode_from_legacy(all_audio):
    """Timers and settings of older versions only had an "all audio tracks" switch."""
    return "all" if all_audio else "first"


def audio_note(fmt, mode, iptv=False):
    """A short note on how many audio tracks the chosen container keeps."""
    if iptv and mode != "first":
        return _(("HLS and DASH internet streams are recorded with one audio track "
                "(the preferred language when it is offered)."))
    if fmt == "MP4" and mode != "first":
        return _("MP4 keeps one audio track (the preferred language when it is on air).")
    if fmt == "Audio" and mode != "first":
        return _("The audio file keeps one track (the preferred language when it is on air).")
    return ""


def keep_pids(channel, extras, fmt, audio_mode="all"):
    """PIDs to keep from the captured transport stream, or None for all.

    None means the stream layout is unknown (no PMT PID), so nothing is dropped.
    The audio PIDs come from the channel list: all of them or the first. The
    preferred language is chosen later from the PMT (see choose_from_pmt).
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
    many = audio_mode != "first" and fmt not in SINGLE_AUDIO
    keep.update(audio if many else audio[:1])
    if channel.teletext_pid and ("teletext" in extras or "subtitles" in extras):
        keep.add(channel.teletext_pid)        # teletext also carries subtitle pages
    if "subtitles" in extras:
        keep.update(channel.subtitle_pids)
    if fmt == "TS" and getattr(channel, "ait_pid", 0):
        keep.add(channel.ait_pid)             # HbbTV signalling, for playback and tests
    return keep


# -- audio tracks from the PMT ----------------------------------------------------

ISO_639_DESCRIPTOR = 0x0A
# stream_type values that are always audio (ISO 13818-1, ATSC A/53).
AUDIO_STREAM_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x87}
# Descriptors that make a private stream (stream_type 0x06) audio: AC-3, DTS, AAC, E-AC-3.
AUDIO_DESCRIPTORS = {0x6A, 0x7B, 0x7C, 0x7A}
# ISO 639 audio_type: 2 = hearing impaired, 3 = audio description for the blind.
SPECIAL_AUDIO_TYPES = {0x02, 0x03}


def _descriptors(loop):
    i = 0
    while i + 2 <= len(loop):
        tag, length = loop[i], loop[i + 1]
        yield tag, loop[i + 2:i + 2 + length]
        i += 2 + length


def pmt_all_pids(section):
    """Every PID the PMT names: PCR, elementary streams (audio, video,
    subtitles, teletext, HbbTV data and carousels) and their ECMs."""
    pmt = parse_pmt(section)
    pids = {pid for _t, pid, _ca, _info in pmt.streams}
    pids.update(pmt.ca_pids)
    if 0 < pmt.pcr_pid < 0x1FFF:
        pids.add(pmt.pcr_pid)
    return pids


def single_program_pat(tsid, version, program, pmt_pid):
    """A PAT section naming only *program* (a cleaned PAT for one channel)."""
    from .epgsources import mpeg_crc
    body = bytes([(tsid >> 8) & 0xFF, tsid & 0xFF, 0xC1 | ((version & 0x1F) << 1), 0, 0,
                  (program >> 8) & 0xFF, program & 0xFF,
                  0xE0 | ((pmt_pid >> 8) & 0x1F), pmt_pid & 0xFF])
    length = len(body) + 4
    sec = bytes([0x00, 0xB0 | ((length >> 8) & 0x0F), length & 0xFF]) + body
    return sec + mpeg_crc(sec).to_bytes(4, "big")


class PatCleaner:
    """Rewrites the PAT (PID 0) of a transport stream so it names one
    program only. Packets of other PIDs pass unchanged."""

    def __init__(self, program, pmt_pid):
        self.program, self.pmt_pid = program, pmt_pid
        self._cc = 0

    def packet(self, pkt):
        """The cleaned PAT packet for PAT packet *pkt*, or None to drop it
        (a continuation of a PAT section)."""
        if not pkt[1] & 0x40:
            return None
        off = ts_payload_offset(pkt)
        if off is None or off >= TS_PACKET:
            return None
        sec = bytes(pkt[off + 1 + pkt[off]:])
        if len(sec) < 8 or sec[0] != 0x00:
            return None
        tsid, version = (sec[3] << 8) | sec[4], (sec[5] >> 1) & 0x1F
        payload = b"\x00" + single_program_pat(tsid, version, self.program, self.pmt_pid)
        head = bytes([TS_SYNC, 0x40, 0x00, 0x10 | self._cc])
        self._cc = (self._cc + 1) & 0x0F
        return head + payload + b"\xff" * (TS_PACKET - 4 - len(payload))


def pmt_audio_tracks(section):
    """Audio tracks of a PMT section as (pid, language, special) in PMT order.

    *language* is the ISO 639 code ("" when not announced), *special* is True for
    audio description and hearing-impaired tracks.
    """
    tracks = []
    for stream_type, pid, _ca, info in parse_pmt(section).streams:
        tags = dict(_descriptors(info))
        if stream_type not in AUDIO_STREAM_TYPES and not (
                stream_type == 0x06 and AUDIO_DESCRIPTORS & tags.keys()):
            continue
        lang, special = "", False
        body = tags.get(ISO_639_DESCRIPTOR, b"")
        if len(body) >= 4:
            lang = bytes(body[:3]).decode("latin-1", "replace").strip("\x00 ").lower()
            special = body[3] in SPECIAL_AUDIO_TYPES
        tracks.append((pid, lang, special))
    return tracks


def choose_audio(tracks, mode, language="", single=False):
    """The audio PIDs to record from *tracks* (see pmt_audio_tracks).

    "all" keeps every track, "language" the tracks in the preferred language
    (without audio description), "first" only the first. A single-track
    container gets the first preferred-language track. Without a match the
    first track is used.
    """
    from .epgsources import norm_lang
    if not tracks:
        return []
    if mode == "all" and not single:
        return [pid for pid, _lang, _special in tracks]
    want = norm_lang(language)
    if mode != "first" and want:
        match = [pid for pid, lang, special in tracks if norm_lang(lang) == want and not special]
        if match:
            return match[:1] if single else match
    return [tracks[0][0]]


def choose_from_pmt(section, pids, channel_audio, mode, language="", single=False,
                    captured=None):
    """*pids* with the channel list's audio PIDs replaced by the ones chosen
    from the PMT. Returns *pids* unchanged when the PMT names no audio track.

    *captured* are the audio PIDs the tuner delivers (None: all of them, when the
    whole transponder is captured); other tracks of the PMT are not offered.
    """
    try:
        tracks = pmt_audio_tracks(section)
    except ValueError:
        return pids
    if captured is not None:
        tracks = [t for t in tracks if t[0] in set(captured)]
    audio = choose_audio(tracks, mode, language, single)
    if not audio:
        return pids
    return (set(pids) - set(channel_audio)) | set(audio)


def choose_streams(streams, mode, language="", single=False):
    """Streams to record from an internet stream's collection (HLS / DASH).

    *streams* are (stream_id, kind, language, default) with kind "video",
    "audio" or anything else (subtitles and data are not recorded). One video
    stream is kept (the default one, else the first); the audio streams are
    chosen like DVB audio tracks (see choose_audio), the default one counting
    as the first track.
    """
    video = [s for s in streams if s[1] == "video"]
    video.sort(key=lambda s: not s[3])
    audio = [s for s in streams if s[1] == "audio"]
    audio.sort(key=lambda s: not s[3])           # stable: the default track first
    picked = choose_audio([(sid, lang, False) for sid, _k, lang, _d in audio],
                          mode, language, single)
    return [s[0] for s in video[:1]] + picked


def needs_pmt(audio_mode, fmt, language):
    """True when the audio tracks can only be chosen once the PMT is known."""
    return bool(language) and (audio_mode == "language" or
                               (audio_mode == "all" and fmt in SINGLE_AUDIO))


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

    With *choose*, the PIDs are only settled once the PMT is seen:
    ``choose(pmt_section)`` returns the PID set to keep. Until then the packets
    are held (at most *hold_limit* bytes, then the given *pids* are used).
    """

    def __init__(self, pids=None, pmt_pid=0, choose=None, hold_limit=4 << 20, pat=None):
        self.pat = pat               # a PatCleaner, or None to keep the PAT as it is
        self.pids = pids
        self.pmt_pid = pmt_pid
        self.pmt = None              # first complete PMT section seen
        self.choose = choose if pmt_pid else None
        self.hold_limit = hold_limit
        self._held = []
        self._held_bytes = 0
        self._rest = b""

    def _settle(self):
        """Fix the PID set (from the PMT, if it came) and release held packets."""
        if self.pmt is not None:
            try:
                self.pids = self.choose(self.pmt)
            except Exception:          # noqa: BLE001 - a broken PMT keeps the given PIDs
                pass
        self.choose = None
        held, self._held, self._held_bytes = self._held, [], 0
        return [p for p in held if self.pids is None or ts_pid(p) in self.pids]

    def feed(self, data):
        data = self._rest + data
        if self.pat is None and self.choose is None and self.pids is None and \
                (self.pmt is not None or not self.pmt_pid):
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
            i += TS_PACKET
            if self.pmt is None and pid == self.pmt_pid and pkt[1] & 0x40:
                self._grab_pmt(bytes(pkt))
            if pid == 0 and self.pat is not None:
                pkt = self.pat.packet(pkt)
                if pkt is None:
                    continue
            if self.choose is not None:
                self._held.append(bytes(pkt))
                self._held_bytes += TS_PACKET
                if self.pmt is not None or self._held_bytes >= self.hold_limit:
                    out.extend(self._settle())
                continue
            if self.pids is None or pid in self.pids:
                out.append(pkt)
        self._rest = data[i:]
        return b"".join(out)

    def flush(self):
        """Packets still held when the stream ends before the PMT came."""
        return b"".join(self._settle()) if self.choose is not None else b""

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
