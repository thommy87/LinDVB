# SPDX-License-Identifier: GPL-2.0-or-later
"""What a programme offers, as the guide signals it: sound (mono, stereo,
5.1, original language, clear speech), audio description, subtitles,
sign language, black and white, HD and 4:3.

The broadcast EIT says it with a component descriptor per stream (EN 300
468 6.2.8, table 26), XMLTV with <audio>, <subtitles> and <video>, the
DVB-I content guide with its accessibility attributes (lindvb.contentguide).
The EPG's programme details and the OSD show them with the same symbols
(:data:`ICONS`); what has no symbol is a short text."""

from .i18n import N_

# feature keys (stored in epg.Event.features)
MONO = "mono"
STEREO = "stereo"
SURROUND = "5.1"
SURROUND_71 = "7.1"
ORIGINAL = "original"            # original language (dual mono "Zweikanalton", ISO 639 qaa)
CLEAR_SPEECH = "clear_speech"    # an extra sound with clear speech ("Klare Sprache")
DESCRIPTION = "description"      # audio description for the blind ("Hörfilm")
SUBTITLES = "subtitles"
SIGN = "sign"
BW = "bw"                        # black and white
HD = "hd"
NARROW = "4:3"

ORDER = (BW, HD, NARROW, MONO, STEREO, SURROUND, SURROUND_71, ORIGINAL, CLEAR_SPEECH,
         DESCRIPTION, SUBTITLES, SIGN)
SOUND = (MONO, STEREO, SURROUND, SURROUND_71)

ICONS = {
    MONO: "lindvb-audio-mono-symbolic",
    STEREO: "lindvb-audio-stereo-symbolic",
    SURROUND: "lindvb-audio-surround-51-symbolic",
    SURROUND_71: "lindvb-audio-surround-71-symbolic",
    ORIGINAL: "lindvb-original-language-symbolic",
    CLEAR_SPEECH: "lindvb-clear-speech-symbolic",
    DESCRIPTION: "lindvb-audio-description-symbolic",
    SUBTITLES: "lindvb-hearing-impaired-symbolic",
    BW: "lindvb-black-white-symbolic",
}

LABELS = {
    MONO: N_("Mono"), STEREO: N_("Stereo"), SURROUND: N_("Surround sound 5.1"),
    SURROUND_71: N_("Surround sound 7.1"), ORIGINAL: N_("Original language"),
    CLEAR_SPEECH: N_("Clear speech"), DESCRIPTION: N_("Audio description"),
    SUBTITLES: N_("Subtitles"), SIGN: N_("Sign language"), BW: N_("Black and white"),
    HD: "HD", NARROW: "4:3",
}

# stream_content 0x2 (MPEG-1 Layer 2) and 0x6 (HE-AAC) component types
_MP2_AUDIO = {0x01: {MONO}, 0x02: {ORIGINAL}, 0x03: {STEREO}, 0x05: {SURROUND},
              0x40: {DESCRIPTION}, 0x41: {CLEAR_SPEECH}, 0x47: {DESCRIPTION},
              0x48: {DESCRIPTION}}
_AAC_AUDIO = {0x01: {MONO}, 0x03: {STEREO}, 0x05: {SURROUND}, 0x40: {DESCRIPTION},
              0x41: {CLEAR_SPEECH}, 0x43: {STEREO}, 0x44: {DESCRIPTION},
              0x45: {CLEAR_SPEECH}, 0x47: {DESCRIPTION}, 0x48: {DESCRIPTION},
              0x49: {DESCRIPTION}, 0x4A: {DESCRIPTION}}
# AC-3 and E-AC-3 (stream_content 0x4, EN 300 468 annex D): the service
# type in bits 5-3 (2 visually impaired, 3 hearing impaired) and the
# channels in bits 2-0 (0 mono, 1 dual mono, 2 and 3 two, 4 and 5 more)
_AC3_SERVICE = {2: {DESCRIPTION}, 3: {CLEAR_SPEECH}}
_AC3_CHANNELS = {0: {MONO}, 1: {ORIGINAL}, 2: {STEREO}, 3: {STEREO}, 4: {SURROUND},
                 5: {SURROUND}}
# MPEG-2 (0x1) and H.264 (0x5) video: 4:3 and HD by component type
_VIDEO_4_3 = {0x01, 0x05, 0x09, 0x0D}
_VIDEO_HD = set(range(0x09, 0x11)) | set(range(0x80, 0x85))
_AUDIO = (0x2, 0x4, 0x6)
# lindvb.trackcatalog's roles of sound tracks
_ROLES = {"original": ORIGINAL, "description": DESCRIPTION, "easy": CLEAR_SPEECH}


def component_features(stream_content, stream_content_ext, component_type, language="", text=""):
    """The features one component descriptor (0x50) of the EIT names: its
    stream_content, stream_content_ext, component_type, ISO 639 language
    and text."""
    sc, ext, ct = stream_content & 0x0F, stream_content_ext & 0x0F, component_type
    found = set()
    if sc in (0x1, 0x5):
        if ct in _VIDEO_4_3:
            found.add(NARROW)
        if ct in _VIDEO_HD:
            found.add(HD)
    elif sc == 0x9 and ext == 0x0 and ct <= 0x07:      # HEVC, HD and UHD
        found.add(HD)
    elif sc == 0x2:
        found |= _MP2_AUDIO.get(ct, set())
    elif sc == 0x6:
        found |= _AAC_AUDIO.get(ct, set())
    elif sc == 0x4:
        found |= _AC3_SERVICE.get((ct >> 3) & 7, set()) | _AC3_CHANNELS.get(ct & 7, set())
    elif sc == 0x3:
        if ct in (0x01, 0x02) or 0x10 <= ct <= 0x25:
            found.add(SUBTITLES)
        elif ct in (0x30, 0x31):
            found.add(SIGN)
    if sc in _AUDIO:
        # the language codes and names a sound track has for its role
        # (qaa original, qad audio description, "Klare Sprache"), as for
        # the tracks on air (imported here: the guide needs no GStreamer)
        from .trackcatalog import language_and_roles
        _lang, roles = language_and_roles(language, text)
        found |= {_ROLES[r] for r in roles if r in _ROLES}
    return found


def xmltv_features(programme):
    """The features of an XMLTV <programme> element."""
    found = set()
    stereo = (programme.findtext("audio/stereo") or "").strip().casefold()
    found |= {"mono": {MONO}, "stereo": {STEREO}, "dolby": {STEREO},
              "dolby digital": {SURROUND}, "surround": {SURROUND},
              "bilingual": {ORIGINAL}}.get(stereo, set())
    for sub in programme.findall("subtitles"):
        found.add(SIGN if (sub.get("type") or "") == "deaf-signed" else SUBTITLES)
    if (programme.findtext("video/aspect") or "").strip() == "4:3":
        found.add(NARROW)
    quality = (programme.findtext("video/quality") or "").strip().casefold()
    if quality in ("hdtv", "hd", "uhd", "uhdtv") or quality.startswith(("720", "1080", "2160")):
        found.add(HD)
    if (programme.findtext("video/colour") or "").strip().casefold() == "no":
        found.add(BW)
    return found


def ordered(features):
    """*features* in the order the symbols are shown, one sound layout only
    (the most channels)."""
    found = set(features)
    sound = [f for f in SOUND if f in found]
    found -= set(sound[:-1])
    return [f for f in ORDER if f in found]


def event_features(ev):
    """The features of an epg.Event in their order: those of its guide, its
    black and white flag and the accessibility of the DVB-I guide."""
    from .contentguide import AUDIO_DESCRIPTION, SIGN_LANGUAGE
    from .contentguide import SUBTITLES as DVBI_SUBTITLES
    found = set(getattr(ev, "features", None) or ())
    if getattr(ev, "bw", False):
        found.add(BW)
    access = set(getattr(ev, "access", None) or ())
    found |= {f for a, f in ((AUDIO_DESCRIPTION, DESCRIPTION), (DVBI_SUBTITLES, SUBTITLES),
                             (SIGN_LANGUAGE, SIGN)) if a in access}
    return ordered(found)


def has_sound_info(ev):
    """The guide says what sound the programme has (then it, not the
    streams on air, tells whether the original language or clear speech is
    on: a channel can keep such a sound stream on air all day)."""
    return bool(set(getattr(ev, "features", None) or ()) &
                {MONO, STEREO, SURROUND, SURROUND_71, ORIGINAL, CLEAR_SPEECH, DESCRIPTION})


def on_air(ev, tracks):
    """Which of the original language and clear speech the OSD shows: what
    the guide says for the programme on now (*ev*) when it says anything
    about the sound, else what the sound tracks on air (media_info
    AudioTrack: language, title) are for."""
    if ev is not None and has_sound_info(ev):
        return set(ev.features) & {ORIGINAL, CLEAR_SPEECH}
    from .trackcatalog import language_and_roles
    found = set()
    for t in tracks:
        _lang, roles = language_and_roles(getattr(t, "language", ""), getattr(t, "title", ""))
        found |= {_ROLES[r] for r in roles if r in _ROLES} & {ORIGINAL, CLEAR_SPEECH}
    return found
