# SPDX-License-Identifier: GPL-2.0-or-later
"""The track catalog: one list of the sound and subtitle tracks of what plays.

Stage D of the stream concept ("2. Spur-Katalog"). Every track has a stable
id and the same fields, whatever the source: kind, format, language (ISO
639-2/T, checked), name, roles (main, audio description, hard of hearing,
easy language, original sound), origin (PID, HLS rendition, teletext page,
caption service) and whether it is in the timeshift buffer.

For now the catalog runs in the shadow of the menus: the player builds it
next to its old track lists and writes the differences to the debug log
(``compare``). Nothing the user sees comes from it yet; the menus move to it
once the logs show no more differences.

Kept free of GTK and GStreamer so it can be tested on its own."""

from dataclasses import dataclass, field

from . import captions
from .ca import parse_pmt
from .epgsources import norm_lang
from .tsanalysis import describe_stream

AUDIO, SUBTITLE = "audio", "subtitle"

# roles
MAIN = "main"
DESCRIPTION = "description"          # audio description for blind and visually impaired viewers
HARD_OF_HEARING = "hard_of_hearing"
EASY = "easy"                        # easy-to-understand language (ARD/ZDF "Klare Sprache")
ORIGINAL = "original"                # the original sound of a film or series
_SPECIAL_ROLES = {DESCRIPTION, HARD_OF_HEARING, EASY}

ROLE_NAMES = {MAIN: "", DESCRIPTION: "Audio description", HARD_OF_HEARING: "Hard of hearing",
              EASY: "Easy language", ORIGINAL: "Original"}

# ISO 639 codes that name no language, and the codes for special tracks
_NO_LANGUAGE = {"", "und", "zxx", "nar", "unk"}
_ORIGINAL_CODES = {"qaa", "qof", "orig", "ov", "mis"}
_DESCRIPTION_CODES = {"qad", "nar_ad"}

# words in track names, without spaces and hyphens, lower case
_DESCRIPTION_WORDS = ("audiodescription", "audiodeskription", "hörfilm", "audiodescripción",
                      "audiodescrizione", "audiodescrição", "audiodescriptie",
                      "audiodeskripsjon", "syntolkning", "synstolkning", "kuvailutulkkaus")
_EASY_WORDS = ("klaresprache", "leichtesprache", "einfachesprache", "easylanguage",
               "easyread", "facilealire")
_ORIGINAL_WORDS = ("originalton", "originalversion", "originalsound", "versionoriginale",
                   "versiónoriginal")

# HLS CHARACTERISTICS (RFC 8216 4.3.4.1, Apple's accessibility UTIs)
_DESCRIBES_VIDEO = "public.accessibility.describes-video"
_HOH_CHARACTERISTICS = ("public.accessibility.transcribes-spoken-dialog",
                        "public.accessibility.describes-music-and-sound")

# formats as tsanalysis.describe_stream names them -> catalog formats
_FORMATS = {"MPEG-1 Audio": "MP2", "MPEG-2 Audio": "MP2", "HE-AAC (LATM)": "AAC"}
DVB_SUB = "DVB-SUB"
# DVB subtitling types (EN 300 468 6.2.41) for the hard of hearing
_HOH_SUBTITLING = range(0x20, 0x26)
# teletext types (EN 300 468 6.2.43): 2 subtitles, 5 subtitles for the hard of hearing
_TTX_SUBTITLES, _TTX_HOH = 2, 5


@dataclass(frozen=True)
class Track:
    kind: str                       # AUDIO or SUBTITLE
    format: str = ""                # "AAC", "AC-3", "DVB-SUB", "Teletext", "WebVTT", "CEA-708", ...
    language: str = ""              # ISO 639-2/T, "" when not known
    name: str = ""                  # the broadcaster's name (HLS NAME), "" when there is none
    roles: frozenset = field(default_factory=frozenset)
    origin: str = ""                # "pid:0x1F5", "rendition:TV Ton", "ttx:150", "cc:1"
    in_buffer: bool = True          # False: only when the stream plays directly

    @property
    def id(self):
        """Stable for the channel: the kind and where the track comes from."""
        return f"{self.kind}/{self.origin}"

    @property
    def key(self):
        """What makes two tracks the same track (redundant variants)."""
        return (self.kind, self.format, self.language, self.name.casefold(), self.roles)

    @property
    def described(self):
        return DESCRIPTION in self.roles


def _squash(text):
    return "".join((text or "").casefold().replace("-", " ").replace("_", " ").split())


def language_and_roles(code, name="", characteristics=""):
    """(ISO 639-2/T code or "", roles) from a language code as broadcast, a
    track name and HLS characteristics. Codes that are no language (ARD's
    LANGUAGE="klare sprache") become ""; the special codes become roles."""
    raw = (code or "").strip().casefold()
    roles = set()
    if raw in _ORIGINAL_CODES or ("qaa" <= raw <= "qtz" and len(raw) == 3 and raw != "qad"):
        roles.add(ORIGINAL)
        raw = ""
    elif raw in _DESCRIPTION_CODES:
        roles.add(DESCRIPTION)
        raw = ""
    lang = norm_lang(raw)
    if lang in _NO_LANGUAGE or not (lang.isalpha() and len(lang) == 3):
        lang = ""
    words = _squash(name) + " " + _squash(raw)
    if any(w in words for w in _DESCRIPTION_WORDS):
        roles.add(DESCRIPTION)
    if any(w in words for w in _EASY_WORDS):
        roles.add(EASY)
    if any(w in words for w in _ORIGINAL_WORDS):
        roles.add(ORIGINAL)
    chars = characteristics or ""
    if _DESCRIBES_VIDEO in chars:
        roles.add(DESCRIPTION)
    if any(c in chars for c in _HOH_CHARACTERISTICS):
        roles.add(HARD_OF_HEARING)
    return lang, roles


def _with_main(roles):
    roles = set(roles)
    if not roles & _SPECIAL_ROLES:
        roles.add(MAIN)
    return frozenset(roles)


# -- sources -----------------------------------------------------------------------

def from_pmt(section):
    """The tracks a PMT section names: sound (PID), DVB subtitles (PID),
    teletext subtitle pages and the caption services of the video."""
    pmt = parse_pmt(section)
    tracks = []
    cc = captions.caption_services_from_descriptors(pmt.program_info)
    for stream_type, pid, _ca, info in pmt.streams:
        d = describe_stream(stream_type, bytes(info))
        cc.update(captions.caption_services_from_descriptors(info))
        tracks += _stream_tracks(pid, d)
    for service, lang in sorted(cc.items(), key=lambda kv: (kv[0] < 0, abs(kv[0]))):
        fmt = "CEA-608" if service < 0 else "CEA-708"
        number = f"CC{1 if service == -1 else 3}" if service < 0 else str(service)
        tracks.append(Track(SUBTITLE, fmt, language_and_roles(lang)[0],
                            roles=_with_main(()), origin=f"cc:{number}"))
    return tracks


def _stream_tracks(pid, d):
    origin = f"pid:0x{pid:X}"
    lang, roles = language_and_roles(d.get("language", ""))
    if d["kind"] == "audio":
        roles |= {r for r, on in ((DESCRIPTION, d.get("audio_description")),
                                  (HARD_OF_HEARING, d.get("hard_of_hearing"))) if on}
        return [Track(AUDIO, _FORMATS.get(d["codec"], d["codec"]), lang,
                      roles=_with_main(roles), origin=origin)]
    if d["kind"] == "subtitle":
        if any(t in _HOH_SUBTITLING for _l, t in d.get("subtitles", ())):
            roles.add(HARD_OF_HEARING)
        return [Track(SUBTITLE, DVB_SUB, lang, roles=_with_main(roles), origin=origin)]
    if d["kind"] == "teletext":
        return _teletext_tracks(d.get("pages", ()))
    return []


def _teletext_tracks(pages):
    out = []
    for page_lang, ttype, page in pages:
        if ttype not in (_TTX_SUBTITLES, _TTX_HOH):
            continue
        lang, roles = language_and_roles(page_lang)
        if ttype == _TTX_HOH:
            roles.add(HARD_OF_HEARING)
        out.append(Track(SUBTITLE, "Teletext", lang, roles=_with_main(roles),
                         origin=f"ttx:{page}"))
    return out


def _rendition_language_and_roles(m):
    return language_and_roles(m.get("language", ""), m.get("name", ""),
                              m.get("characteristics", ""))


def from_hls(media, in_buffer=True):
    """The tracks of an HLS master playlist's renditions (EXT-X-MEDIA, as
    streaminfo.parse_hls gives them)."""
    tracks = []
    for m in media:
        kind = {"AUDIO": AUDIO, "SUBTITLES": SUBTITLE, "CLOSED-CAPTIONS": SUBTITLE}.get(
            m.get("type", ""))
        if kind is None:
            continue
        name = m.get("name", "")
        lang, roles = _rendition_language_and_roles(m)
        if m.get("type") == "CLOSED-CAPTIONS":
            iid = (m.get("instream_id") or "").upper()
            fmt = "CEA-608" if iid.startswith("CC") else "CEA-708"
            origin = f"cc:{iid.replace('SERVICE', '') or name}"
        else:
            fmt = "WebVTT" if kind == SUBTITLE else ""
            origin = f"rendition:{name}"
        tracks.append(Track(kind, fmt, lang, name, _with_main(roles), origin,
                            in_buffer=in_buffer))
    return tracks


def with_renditions(pmt_tracks, media, rendition_for_pid):
    """The timeshift buffer of an HLS stream: its PMT names one PID per audio
    rendition (lindvb.ipstream.rendition_pid). The PID's track gets the
    rendition's name and roles; renditions the buffer does not hold are
    added as not in the buffer."""
    out, used = [], set()
    for t in pmt_tracks:
        m = _rendition_of(t, media, rendition_for_pid)
        if m is None:
            out.append(t)
            continue
        used.add(m.get("name", ""))
        lang, roles = _rendition_language_and_roles(m)
        roles |= set(t.roles) - {MAIN}
        out.append(Track(AUDIO, t.format, t.language or lang, m.get("name", ""),
                         _with_main(roles), t.origin))
    out += [t for t in from_hls(media, in_buffer=False)
            if not (t.kind == AUDIO and t.name in used)]
    return out


def merged(tracks):
    """The tracks once: the same track from redundant variants (two CDN
    copies of a playlist) is kept the first time it comes."""
    seen, out = set(), []
    for t in tracks:
        if t.key in seen:
            continue
        seen.add(t.key)
        out.append(t)
    return out


# -- labels ------------------------------------------------------------------------

def label(track, language_name=None, gettext=None):
    """'German · Audio description · AAC': the one way a track is named."""
    tr = gettext or (lambda s: s)
    lang = ""
    if track.language:
        lang = language_name(track.language) if language_name else track.language.upper()
    head = lang
    if track.name and _squash(track.name) not in (_squash(lang), _squash(track.language)):
        head = f"{lang} – {track.name}" if lang else track.name
    parts = [head] if head else []
    roles = [tr(ROLE_NAMES[r]) for r in (DESCRIPTION, HARD_OF_HEARING, EASY, ORIGINAL)
             if r in track.roles]
    parts += [r for r in roles if r.casefold() not in head.casefold()]
    if track.format:
        parts.append(track.format)
    if not parts:
        # nothing to name it by (a sound without language in the buffer):
        # where it comes from, rather than nothing
        parts.append(track.origin)
    return " · ".join(parts)


# -- shadow mode ----------------------------------------------------------------------

def catalog_for(pmt_section=None, media=(), rendition_for_pid=None, buffered=None):
    """The catalog of what plays: from the PMT of a transport stream (DVB,
    the timeshift buffer), with the HLS renditions it holds, or from the
    renditions alone (an HLS stream played directly). [] when neither is
    known (files, DASH played directly). *buffered*: True for the timeshift
    buffer, False played directly, None to tell from the PIDs."""
    tracks = from_pmt(pmt_section) if pmt_section else []
    if media:
        in_buffer = buffered if buffered is not None else \
            any(_rendition_of(t, media, rendition_for_pid) for t in tracks)
        if in_buffer:
            tracks = with_renditions(tracks, media, rendition_for_pid)
        else:
            # played directly: the PMT is the one of the variant's segments
            # and knows nothing of the other renditions
            tracks = from_hls(media)
    return merged(tracks)


def _rendition_of(track, media, rendition_for_pid):
    """The rendition the buffer put on the PID of *track*, or None."""
    if rendition_for_pid is None or track.kind != AUDIO or not track.origin.startswith("pid:0x"):
        return None
    return rendition_for_pid(media, int(track.origin[6:], 16))


def compare(catalog, audio, subtitles, is_described):
    """Differences between the catalog and the player's own lists, as short
    lines for the debug log; [] when they agree.

    *audio* are the player's AudioTracks, *subtitles* the kinds of the
    entries of its subtitle menu ("text", "ttx", "cc"), *is_described* tells
    audio description in an AudioTrack.
    Only what is in the buffer (or plays directly) is compared; captions
    are left out, the player only lists them once it has seen them."""
    out = []
    cat_audio = [t for t in catalog if t.kind == AUDIO and t.in_buffer]
    if audio and cat_audio and len(cat_audio) != len(audio):
        out.append(f"audio: {len(audio)} tracks in the menu, {len(cat_audio)} in the catalog")
    if audio and cat_audio:
        old_ad = sum(1 for t in audio if is_described(t))
        new_ad = sum(1 for t in cat_audio if t.described)
        if old_ad != new_ad:
            out.append(f"audio description: {old_ad} in the menu, {new_ad} in the catalog")
        # the languages on offer ("mul", several languages, says nothing)
        old_langs = sorted({norm_lang(t.language) for t in audio} - _NO_LANGUAGE - {"mul"})
        new_langs = sorted({t.language for t in cat_audio} - {"", "mul"})
        if old_langs and new_langs and old_langs != new_langs:
            out.append(f"audio languages: menu {old_langs}, catalog {new_langs}")
    cat_subs = [t for t in catalog if t.kind == SUBTITLE and t.in_buffer
                and not t.format.startswith("CEA")]
    old_subs = [k for k in subtitles if k != "cc"]
    if cat_subs and len(old_subs) != len(cat_subs):
        out.append(f"subtitles: {len(old_subs)} in the menu, {len(cat_subs)} in the catalog")
    return out
