# SPDX-License-Identifier: GPL-2.0-or-later
"""Readable descriptions of audio tracks and stream properties.

Kept free of GTK and GStreamer imports so the logic can be tested on its own;
the language lookup uses GStreamer's iso-codes tables when they are there.
"""

from dataclasses import dataclass

# ISO 639 codes that do not name a real language
_NO_LANGUAGE = {"", "und", "zxx", "nar", "unk"}
# codes broadcasters use for special tracks
_SPECIAL = {
    "mul": "Multiple languages",
    "qaa": "Original", "qof": "Original", "orig": "Original", "mis": "Original",
    "qad": "Audio description", "nar_ad": "Audio description",
}
# fallback when GStreamer's iso-codes lookup is not available
_LANGUAGES = {
    "de": "German", "deu": "German", "ger": "German",
    "en": "English", "eng": "English",
    "fr": "French", "fra": "French", "fre": "French",
    "it": "Italian", "ita": "Italian",
    "es": "Spanish", "spa": "Spanish",
    "nl": "Dutch", "nld": "Dutch", "dut": "Dutch",
    "pl": "Polish", "pol": "Polish",
    "tr": "Turkish", "tur": "Turkish",
    "ru": "Russian", "rus": "Russian",
    "pt": "Portuguese", "por": "Portuguese",
    "da": "Danish", "dan": "Danish",
    "sv": "Swedish", "swe": "Swedish",
    "no": "Norwegian", "nor": "Norwegian",
    "fi": "Finnish", "fin": "Finnish",
    "cs": "Czech", "ces": "Czech", "cze": "Czech",
    "hu": "Hungarian", "hun": "Hungarian",
    "el": "Greek", "ell": "Greek", "gre": "Greek",
    "ar": "Arabic", "ara": "Arabic",
}

_gst_lookup = None


def _gst_language_name(code):
    """GStreamer's (localised) language name, or None."""
    global _gst_lookup
    if _gst_lookup is None:
        try:
            import gi
            gi.require_version("GstTag", "1.0")
            from gi.repository import GstTag
            _gst_lookup = GstTag.tag_get_language_name
        except (ImportError, ValueError):
            _gst_lookup = False
    if not _gst_lookup:
        return None
    try:
        return _gst_lookup(code)
    except (TypeError, ValueError):
        return None


def language_name(code):
    """'deu' -> 'Deutsch' (or 'German', depending on the locale), None if unknown."""
    code = (code or "").strip().lower()
    if code in _NO_LANGUAGE:
        return None
    if code in _SPECIAL:
        return _SPECIAL[code]
    if code.startswith("q") and len(code) == 3 and "qaa" <= code <= "qtz":
        return "Original"             # ISO 639-2 local-use range, used for the original sound
    name = _gst_language_name(code) or _LANGUAGES.get(code)
    if name:
        return name[:1].upper() + name[1:]
    return code


def channel_layout(channels):
    """Number of audio channels -> 'Mono', 'Stereo', '5.1', ..."""
    return {1: "Mono", 2: "Stereo", 3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1",
            7: "6.1", 8: "7.1"}.get(channels or 0, f"{channels} channels" if channels else "")


@dataclass
class AudioTrack:
    language: str = ""     # ISO 639 code from the stream, if any
    title: str = ""        # track name from the stream (HLS NAME, Matroska title)
    codec: str = ""        # e.g. "MPEG-4 AAC", "AC-3 (ATSC A/52)"
    channels: int = 0      # 0 = not known yet
    # the stream says it describes the picture (HLS CHARACTERISTICS
    # public.accessibility.describes-video), whatever its name
    description: bool = False

    @property
    def language_name(self):
        return language_name(self.language)

    @property
    def layout(self):
        return channel_layout(self.channels)


def short_codec(codec):
    """'AC-3 (ATSC A/52)' -> 'AC-3', 'MPEG-4 AAC' -> 'AAC'."""
    c = (codec or "").strip()
    low = c.lower()
    for key, short in (("e-ac-3", "E-AC-3"), ("eac3", "E-AC-3"), ("ac-3", "AC-3"), ("ac3", "AC-3"),
                       ("a/52", "AC-3"), ("aac", "AAC"), ("dts", "DTS"), ("opus", "Opus"),
                       ("mpeg-1 layer 2", "MP2"), ("mp2", "MP2"), ("layer 3", "MP3"),
                       ("mp3", "MP3"), ("flac", "FLAC"), ("vorbis", "Vorbis")):
        if key in low:
            return short
    return c.split(" (")[0]


# Preferences › Playback › "Preferred audio format"
AUDIO_PREFERENCES = ("off", "ac3", "eac3", "stereo")
_DOLBY = ("AC-3", "E-AC-3")


def preferred_track(tracks, prefer, current=-1):
    """Index of the track the "preferred audio format" asks for, or None to
    keep the current one. The language of the current track stays: only its
    tracks are looked at (all tracks when it has none). ``prefer``: "ac3"
    (Dolby Digital, then Dolby Digital Plus), "eac3" (the other way round) or
    "stereo" (the plain MPEG / AAC sound); "off" keeps what plays."""
    if prefer not in ("ac3", "eac3", "stereo") or len(tracks) < 2:
        return None
    lang = tracks[current].language if 0 <= current < len(tracks) else ""
    pool = [i for i, t in enumerate(tracks) if not lang or t.language == lang] or \
        list(range(len(tracks)))
    codecs = {i: short_codec(tracks[i].codec) for i in pool}
    if prefer == "stereo":
        order = [[i for i in pool if codecs[i] and codecs[i] not in _DOLBY]]
    else:
        first, second = ("AC-3", "E-AC-3") if prefer == "ac3" else ("E-AC-3", "AC-3")
        order = [[i for i in pool if codecs[i] == first], [i for i in pool if codecs[i] == second]]
    for group in order:
        if group:
            return None if current in group else group[0]
    return None


def _useless_title(title, track):
    t = (title or "").strip().lower()
    if not t:
        return True
    lang = (track.language_name or "").lower()
    return t in (lang, (track.language or "").lower()) or t.startswith(("audio_", "track ", "audio "))


def audio_track_labels(tracks):
    """Menu labels like 'German, Stereo' / 'German, 5.1' / 'Original, Stereo'.

    Tracks without a language fall back to 'Track N'. Labels that would come
    out the same are told apart by codec, then by number."""
    base = []
    for i, t in enumerate(tracks):
        name = t.language_name
        if not _useless_title(t.title, t):
            name = f"{name} – {t.title.strip()}" if name else t.title.strip()
        if not name:
            name = f"Track {i + 1}"
        base.append(", ".join(p for p in (name, t.layout) if p))
    labels = list(base)
    for i, t in enumerate(tracks):
        if base.count(base[i]) > 1 and t.codec:
            labels[i] = f"{base[i]} ({short_codec(t.codec)})"
    for i in range(len(labels)):
        if labels.count(labels[i]) > 1 and not labels[i].startswith("Track "):
            same = [j for j, lab in enumerate(labels) if lab == labels[i]]
            for n, j in enumerate(same, 1):
                labels[j] = f"{labels[j]} #{n}"
    return labels


def audio_summary(tracks, current=-1):
    """What the info bar shows about the audio: the layouts on offer (current
    first) and the languages on offer, e.g. (['Stereo', '5.1'], ['German', 'English'])."""
    order = ([current] if 0 <= current < len(tracks) else []) + \
        [i for i in range(len(tracks)) if i != current]
    layouts, languages = [], []
    for i in order:
        t = tracks[i]
        if t.layout and t.layout not in layouts:
            layouts.append(t.layout)
        name = t.language_name
        if name and name not in languages:
            languages.append(name)
    return layouts, languages


def hls_has_alternate_audio(playlist_text):
    """True when an HLS master playlist carries audio in separate renditions
    (EXT-X-MEDIA TYPE=AUDIO with its own URI) instead of muxed into the video."""
    for line in (playlist_text or "").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line and "URI=" in line:
            return True
    return False


# -- what the OSD's technical row shows (lindvb.ui.window) ------------------------

_DELSYS_LABELS = {
    "dvb-s": "DVB-S", "dvb-s2": "DVB-S2", "turbo": "DVB-S2", "isdb-s": "ISDB-S",
    "dvb-c": "DVB-C", "dvb-c-a": "DVB-C", "dvb-c-c": "DVB-C", "dvb-c-b": "DVB-C",
    "dvb-c2": "DVB-C2", "dvb-t": "DVB-T", "dvb-t2": "DVB-T2",
    "atsc": "ATSC", "isdb-t": "ISDB-T", "dtmb": "DTMB",
}


def source_label(channel, satip=False):
    """Where a channel comes from, as the OSD shows it: 'DVB-I', 'TVHeadend',
    'SAT>IP', 'IPTV' (any other internet stream or playlist), or the delivery
    system of a tuner channel ('DVB-S2', 'DVB-C', 'DVB-T2', 'ATSC', ...).
    ``satip``: the channel is received through a SAT>IP server."""
    if getattr(channel, "dvbi_id", ""):
        return "DVB-I"
    if getattr(channel, "tvh_server", ""):
        return "TVHeadend"
    if satip:
        return "SAT>IP"
    delsys = (getattr(channel, "delsys", "") or "").lower()
    if delsys == "iptv":
        return "IPTV"
    return _DELSYS_LABELS.get(delsys, delsys.upper())


def system_label(channel):
    """The delivery system alone ('DVB-S2'), e.g. for the SAT>IP tooltip."""
    delsys = (getattr(channel, "delsys", "") or "").lower()
    return _DELSYS_LABELS.get(delsys, delsys.upper()) if delsys != "iptv" else ""


def video_class(width, height, interlace=None):
    """('SD' | 'HD' | 'UHD', lines + scan like '1080i') for a picture size.

    SD goes up to 576 lines (720x576 PAL, 720x480 NTSC and smaller web
    streams); HD from 700 lines or 1200 pixels across (1280x720, 1440x1080,
    1920x1080); UHD from 1800 lines or 3200 pixels across (3840x2160).
    ``interlace``: GStreamer's interlace-mode ('progressive', 'interleaved',
    'mixed', ...); unknown leaves the scan letter out. None without a size."""
    if not width or not height:
        return None
    if height >= 1800 or width >= 3200:
        cls = "UHD"
    elif height >= 700 or width >= 1200:
        cls = "HD"
    else:
        cls = "SD"
    lines = height
    for std in (480, 576, 720, 1080, 2160, 4320):   # 1088 / 544 coded heights
        if std <= height <= std + 8:
            lines = std
            break
    mode = (interlace or "").lower()
    scan = "p" if mode == "progressive" else "i" if mode in (
        "interleaved", "mixed", "fields", "alternate") else ""
    return cls, f"{lines}{scan}"


def aspect_label(width, height, par=None):
    """'16:9' or '4:3' for the picture as it is shown: the width stretched by
    the pixel aspect ratio ``par`` (n, d), so 720x576 at 64/45 is 16:9 and at
    16/15 is 4:3. From 1.55 up the picture counts as wide. None without a size."""
    if not width or not height:
        return None
    n, d = par if par and par[0] and par[1] else (1, 1)
    return "16:9" if width * n / (height * d) >= 1.55 else "4:3"


def is_hdr(colorimetry):
    """True when GStreamer's colorimetry names a HDR transfer (PQ / HLG)."""
    c = (colorimetry or "").lower()
    if not c:
        return False
    if "pq" in c or "hlg" in c or "2084" in c or "b67" in c:
        return True
    parts = c.split(":")
    # range:matrix:transfer:primaries; transfer 14 = SMPTE ST 2084, 15 = ARIB STD-B67
    return len(parts) == 4 and parts[2] in ("14", "15")


def layout_icon(channels):
    """Icon name for a channel count: mono, stereo, 5.1, 7.1; None otherwise."""
    return {1: "lindvb-audio-mono-symbolic", 2: "lindvb-audio-stereo-symbolic",
            6: "lindvb-audio-surround-51-symbolic",
            8: "lindvb-audio-surround-71-symbolic"}.get(channels or 0)


# names of audio description tracks, without spaces and hyphens
# ("Audio-Deskription" at 3sat, "Audiodeskription" at ARD)
_AD_WORDS = ("audiodescription", "audiodeskription", "hörfilm", "audiodescripción",
             "audiodescrizione", "audiodescrição", "audiodescriptie", "audiodeskripsjon",
             "syntolkning", "synstolkning", "kuvailutulkkaus")


def is_audio_description(track):
    """A track for blind and visually impaired viewers (Hörfilm)."""
    if getattr(track, "description", False):
        return True
    if (track.language or "").strip().lower() in ("qad", "nar_ad"):
        return True
    t = "".join((track.title or "").lower().replace("-", " ").replace("_", " ").split())
    return any(k in t for k in _AD_WORDS)


_ISO1 = {"deu": "de", "ger": "de", "eng": "en", "fra": "fr", "fre": "fr", "ita": "it",
         "spa": "es", "nld": "nl", "dut": "nl", "pol": "pl", "tur": "tr", "rus": "ru",
         "por": "pt", "dan": "da", "swe": "sv", "nor": "no", "fin": "fi", "ces": "cs",
         "cze": "cs", "hun": "hu", "ell": "el", "gre": "el", "ara": "ar", "ukr": "uk",
         "ron": "ro", "rum": "ro", "hrv": "hr", "srp": "sr", "slv": "sl", "slk": "sk",
         "slo": "sk", "bul": "bg", "jpn": "ja", "zho": "zh", "chi": "zh", "kor": "ko",
         "fas": "fa", "per": "fa", "heb": "he", "hin": "hi"}


def language_short(code):
    """'deu' -> 'DE', 'qaa' -> 'OV'; None for no language."""
    c = (code or "").strip().lower()
    if c in _NO_LANGUAGE or c in ("qad", "nar_ad", "mul"):
        return None
    if c in ("qaa", "qof", "orig", "mis") or (c.startswith("q") and len(c) == 3 and "qaa" <= c <= "qtz"):
        return "OV"
    if len(c) == 2:
        return c.upper()
    return (_ISO1.get(c) or c).upper()


def audio_badges(tracks, current=-1):
    """What the OSD shows about the sound, current track first:
    [(icon name or None, layout text, short codec, is current)], one per
    layout + codec on offer, and the short language codes on offer."""
    order = ([current] if 0 <= current < len(tracks) else []) + \
        [i for i in range(len(tracks)) if i != current]
    badges, seen, langs = [], set(), []
    for i in order:
        t = tracks[i]
        if is_audio_description(t):
            continue
        key = (t.channels, short_codec(t.codec))
        if key not in seen and (t.channels or t.codec):
            seen.add(key)
            badges.append((layout_icon(t.channels), t.layout, key[1], i == current))
        s = language_short(t.language)
        if s and s not in langs:
            langs.append(s)
    return badges, langs
