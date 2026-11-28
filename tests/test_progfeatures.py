"""What a programme offers (lindvb.progfeatures): from the EIT component
descriptors, XMLTV and the DVB-I guide, for the EPG details and the OSD."""

from types import SimpleNamespace as NS

from lindvb import progfeatures as pf
from lindvb.contentguide import AUDIO_DESCRIPTION, SIGN_LANGUAGE, SUBTITLES
from lindvb.epg import Event, read_xmltv
from lindvb.epgmerge import merge
from lindvb.epgsources import parse_eit_raw
from tests.test_epgsources import START, _eit, _short


def _component(content, ctype, lang="deu", text="", ext=0):
    body = bytes([(ext << 4) | content, ctype, 1]) + lang.encode() + text.encode("latin-1")
    return bytes([0x50, len(body)]) + body


def test_sound_of_the_component_types():
    """EN 300 468 table 26: MPEG audio, HE-AAC and AC-3 (annex D)."""
    assert pf.component_features(0x2, 0, 0x01) == {pf.MONO}
    assert pf.component_features(0x2, 0, 0x02) == {pf.ORIGINAL}        # dual mono, Zweikanalton
    assert pf.component_features(0x2, 0, 0x03) == {pf.STEREO}
    assert pf.component_features(0x2, 0, 0x40) == {pf.DESCRIPTION}
    assert pf.component_features(0x2, 0, 0x41) == {pf.CLEAR_SPEECH}    # for the hard of hearing
    assert pf.component_features(0x6, 0, 0x43) == {pf.STEREO}          # HE-AAC v2
    assert pf.component_features(0x6, 0, 0x48) == {pf.DESCRIPTION}
    # AC-3: full service 5.1, dual mono, visually and hearing impaired
    assert pf.component_features(0x4, 0, 0x44) == {pf.SURROUND}
    assert pf.component_features(0x4, 0, 0x41) == {pf.ORIGINAL}
    assert pf.component_features(0x4, 0, 0x52) == {pf.DESCRIPTION, pf.STEREO}
    assert pf.component_features(0x4, 0, 0x5A) == {pf.CLEAR_SPEECH, pf.STEREO}
    # reserved and unknown types say nothing
    assert pf.component_features(0x2, 0, 0x04) == set()
    assert pf.component_features(0x2, 0, 0xB0) == set()


def test_sound_of_the_language_and_text():
    """The same codes and names as for the tracks on air (trackcatalog)."""
    assert pf.component_features(0x2, 0, 0x03, "qaa") == {pf.STEREO, pf.ORIGINAL}
    assert pf.component_features(0x2, 0, 0x03, "mis", "Originalton") == {pf.STEREO, pf.ORIGINAL}
    assert pf.component_features(0x2, 0, 0x03, "qad") == {pf.STEREO, pf.DESCRIPTION}
    assert pf.component_features(0x2, 0, 0x03, "deu", "Klare Sprache") == {pf.STEREO, pf.CLEAR_SPEECH}
    assert pf.component_features(0x2, 0, 0x03, "deu", "Audiodeskription") == \
        {pf.STEREO, pf.DESCRIPTION}
    assert pf.component_features(0x2, 0, 0x03, "eng", "English") == {pf.STEREO}
    # a subtitle or picture component in "qaa" is no original sound
    assert pf.component_features(0x3, 0, 0x10, "qaa") == {pf.SUBTITLES}


def test_picture_and_subtitles():
    assert pf.component_features(0x1, 0, 0x03) == set()                # SD 16:9
    assert pf.component_features(0x1, 0, 0x01) == {pf.NARROW}          # SD 4:3
    assert pf.component_features(0x5, 0, 0x0B) == {pf.HD}             # H.264 HD 16:9
    assert pf.component_features(0x9, 0, 0x00) == {pf.HD}             # HEVC
    assert pf.component_features(0x3, 0, 0x01) == {pf.SUBTITLES}       # teletext subtitles
    assert pf.component_features(0x3, 0, 0x20) == {pf.SUBTITLES}       # for the hard of hearing
    assert pf.component_features(0x3, 0, 0x30) == {pf.SIGN}


def test_eit_component_descriptors_give_the_features():
    sec = _eit(1, 2, [(1, START, 5400, [_short("deu", "Der Film", ""),
                                        _component(0x5, 0x0B), _component(0x2, 0x03),
                                        _component(0x4, 0x44), _component(0x2, 0x03, "qaa"),
                                        _component(0x2, 0x03, "deu", "Klare Sprache"),
                                        _component(0x3, 0x10)]),
                      (2, START + 5400, 900, [_short("deu", "Nachrichten", "")])])
    film, news = parse_eit_raw(sec, "")[1]
    # stereo and 5.1: only the 5.1 symbol
    assert film.features == [pf.HD, pf.SURROUND, pf.ORIGINAL, pf.CLEAR_SPEECH, pf.SUBTITLES]
    assert news.features == []


def test_broken_component_descriptor_is_skipped():
    short = bytes([0x50, 2, 0x02, 0x03])                    # no tag and language
    sec = _eit(1, 2, [(1, START, 600, [_short("deu", "Kurz", ""), short])])
    (ev,) = parse_eit_raw(sec, "")[1]
    assert ev.title == "Kurz" and ev.features == []


def test_xmltv_features():
    xml = b"""<tv><programme channel="a" start="20300101120000 +0000" stop="20300101130000 +0000">
    <title>Casablanca</title><video><aspect>4:3</aspect><colour>no</colour></video>
    <audio><stereo>bilingual</stereo></audio><subtitles type="teletext"/></programme>
    <programme channel="a" start="20300101130000 +0000" stop="20300101140000 +0000">
    <title>Show</title><video><quality>HDTV</quality></video><audio><stereo>surround</stereo></audio>
    <subtitles type="deaf-signed"/></programme>
    <programme channel="a" start="20300101140000 +0000" stop="20300101150000 +0000">
    <title>News</title></programme></tv>"""
    (_a, film), (_b, show), (_c, news) = read_xmltv(xml)[1]
    assert film.bw and film.features == [pf.NARROW, pf.ORIGINAL, pf.SUBTITLES]
    assert show.features == [pf.HD, pf.SURROUND, pf.SIGN]
    assert news.features == []


def test_event_features_take_bw_and_the_dvbi_accessibility():
    ev = Event(START, 600, "x", bw=True, access=[AUDIO_DESCRIPTION, SUBTITLES, SIGN_LANGUAGE],
               features=[pf.STEREO])
    assert pf.event_features(ev) == [pf.BW, pf.STEREO, pf.DESCRIPTION, pf.SUBTITLES, pf.SIGN]
    assert pf.event_features(Event(START, 600, "x")) == []
    # every feature has a label, symbols only where there is one
    assert set(pf.ORDER) == set(pf.LABELS) and set(pf.ICONS) <= set(pf.ORDER)


def test_features_are_borrowed_from_the_same_programme_elsewhere():
    eit = Event(START, 3600, "Der Film", features=[pf.ORIGINAL])
    xmltv = Event(START, 3600, "Der Film", description="Lang")
    (ev,) = merge({"xmltv": [xmltv], "eit": [eit]}, order=["xmltv", "eit"], now=START)
    assert ev.description == "Lang" and ev.features == [pf.ORIGINAL]


def test_osd_trusts_the_guide_over_a_track_on_air_all_day():
    """A channel can keep an original sound track on air all day: when the
    guide says what sound the programme has, it decides."""
    tracks = [NS(language="deu", title=""), NS(language="qaa", title=""),
              NS(language="deu", title="Klare Sprache")]
    assert pf.on_air(None, tracks) == {pf.ORIGINAL, pf.CLEAR_SPEECH}
    assert pf.on_air(Event(START, 60, "x"), tracks) == {pf.ORIGINAL, pf.CLEAR_SPEECH}
    assert pf.on_air(Event(START, 60, "x", features=[pf.STEREO]), tracks) == set()
    assert pf.on_air(Event(START, 60, "x", features=[pf.STEREO, pf.ORIGINAL]), []) == {pf.ORIGINAL}
    # the picture alone says nothing about the sound
    assert pf.on_air(Event(START, 60, "x", features=[pf.HD]), tracks[:2]) == {pf.ORIGINAL}
    assert pf.on_air(None, [NS(language="deu", title="")]) == set()


def test_symbols_are_installed():
    from pathlib import Path
    icons = Path(pf.__file__).parent / "icons" / "hicolor" / "scalable" / "actions"
    for name in pf.ICONS.values():
        assert (icons / f"{name}.svg").is_file(), name
