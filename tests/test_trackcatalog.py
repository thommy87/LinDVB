"""The track catalog (stage D, shadow mode): sources, roles, labels and the
comparison with the player's menus."""

import types

import streamkit as sk
from lindvb import trackcatalog as tc
from lindvb.ipstream import rendition_for_pid, rendition_pid
from lindvb.media_info import AudioTrack, is_audio_description
from lindvb.streaminfo import parse_hls
from test_recformat import PMT, _lang, _pmt


def test_language_and_roles():
    assert tc.language_and_roles("ger") == ("deu", set())
    assert tc.language_and_roles("de") == ("deu", set())
    assert tc.language_and_roles("und") == ("", set())
    assert tc.language_and_roles("qaa") == ("", {tc.ORIGINAL})
    assert tc.language_and_roles("mis") == ("", {tc.ORIGINAL})
    assert tc.language_and_roles("qad") == ("", {tc.DESCRIPTION})
    # ARD writes LANGUAGE="klare sprache": no language, the role
    assert tc.language_and_roles("klare sprache") == ("", {tc.EASY})
    assert tc.language_and_roles("deu", "Audio-Deskription") == ("deu", {tc.DESCRIPTION})
    assert tc.language_and_roles("deu", "TV Ton",
                                 "public.accessibility.describes-video")[1] == {tc.DESCRIPTION}
    assert tc.language_and_roles("eng", "Originalton")[1] == {tc.ORIGINAL}
    assert tc.language_and_roles(
        "deu", "", "public.accessibility.transcribes-spoken-dialog")[1] == {tc.HARD_OF_HEARING}


def test_from_pmt_sound():
    tracks = tc.from_pmt(PMT)
    audio = [t for t in tracks if t.kind == tc.AUDIO]
    assert [(t.origin, t.format, t.language) for t in audio] == [
        ("pid:0x13EE", "MP2", "deu"), ("pid:0x13EF", "MP2", "deu"),
        ("pid:0x13F2", "AC-3", "deu"), ("pid:0x13F3", "MP2", "eng")]
    assert [t.described for t in audio] == [False, True, False, False]
    assert tc.MAIN in audio[0].roles and tc.MAIN not in audio[1].roles


def test_from_pmt_subtitles_and_captions():
    supplementary = bytes([0x7F, 2, 0x06, (1 << 2) | 0x80])          # audio description
    dvbsub = bytes([0x59, 16]) + b"deu\x10\x00\x01\x00\x01" + b"deu\x20\x00\x02\x00\x02"
    ttx = bytes([0x56, 15]) + b"deu\x09\x00" + b"deu\x11\x50" + b"deu\x29\x49"
    cc = bytes([0x86, 7, 0xC1]) + b"eng" + bytes([0x81, 0x3F, 0xFF])  # CEA-708 service 1
    pmt = _pmt([(0x1B, 5101, cc), (0x06, 5108, bytes([0x6A, 1, 0]) + supplementary),
                (0x06, 5105, dvbsub), (0x06, 5104, ttx), (0x03, 5102, _lang("deu", 2))])
    tracks = {t.id: t for t in tc.from_pmt(pmt)}
    assert tracks["audio/pid:0x13F4"].described
    assert tracks["audio/pid:0x13F4"].format == "AC-3"
    assert tc.HARD_OF_HEARING in tracks["audio/pid:0x13EE"].roles
    assert tc.HARD_OF_HEARING in tracks["subtitle/pid:0x13F1"].roles
    # teletext: the subtitle pages (type 2 and 5), not the index page
    assert tracks["subtitle/ttx:150"].format == "Teletext"
    assert tc.HARD_OF_HEARING in tracks["subtitle/ttx:149"].roles
    assert "subtitle/ttx:100" not in tracks
    assert tracks["subtitle/cc:1"].format == "CEA-708"
    assert tracks["subtitle/cc:1"].language == "eng"


def test_redundant_variants_are_one_track_each():
    """3sat lists every rendition twice (two CDN copies): the catalog has
    each once, so there is no "German twice"."""
    catalog = tc.catalog_for(None, parse_hls(sk.REDUNDANT_MASTER)["media"])
    assert [t.id for t in catalog] == [
        "audio/rendition:TV Ton", "audio/rendition:Klare Sprache",
        "audio/rendition:Audio-Deskription", "subtitle/rendition:Untertitel deutsch"]
    roles = {t.name: t.roles for t in catalog}
    assert roles["Audio-Deskription"] == {tc.DESCRIPTION}
    assert roles["Klare Sprache"] == {tc.EASY}
    assert roles["TV Ton"] == {tc.MAIN}


def test_timeshift_buffer_names_its_pids():
    """In the buffer each audio rendition has a PID of its own: the PID's
    track gets the rendition's name and role. The subtitles are not in it."""
    media = parse_hls(sk.REDUNDANT_MASTER)["media"]
    taken, streams = set(), [(0x1B, 0x100, b"")]
    for name in ("TV Ton", "Klare Sprache", "Audio-Deskription"):
        pid = rendition_pid(name, taken)
        taken.add(pid)
        streams.append((0x0F, pid, _lang("deu") if name != "Klare Sprache" else b""))
    catalog = tc.catalog_for(_pmt(streams), media, rendition_for_pid)
    audio = [t for t in catalog if t.kind == tc.AUDIO]
    assert [t.name for t in audio] == ["TV Ton", "Klare Sprache", "Audio-Deskription"]
    assert all(t.in_buffer and t.origin.startswith("pid:") for t in audio)
    assert [t.described for t in audio] == [False, False, True]
    subs = [t for t in catalog if t.kind == tc.SUBTITLE]
    assert [(t.name, t.in_buffer) for t in subs] == [("Untertitel deutsch", False)]


def test_buffered_stream_with_subtitle_renditions_only():
    """PBS (Alex's log of 24.11.): HLS in the buffer whose sound is in the
    variant and whose WebVTT is a rendition. The catalog keeps the buffer's
    sound and lists the subtitles as not in the buffer, so it agrees with
    the menus instead of reporting a missing subtitle track."""
    media = parse_hls(sk.PBS_MASTER)["media"]
    pmt = _pmt([(0x1B, 0x41, b""), (0x0F, 0x42, b"")])
    catalog = tc.catalog_for(pmt, media, rendition_for_pid, buffered=True)
    assert [(t.id, t.in_buffer) for t in catalog] == [
        ("audio/pid:0x42", True), ("subtitle/rendition:English", False)]
    assert tc.label(catalog[0]) == "AAC"
    assert tc.compare(catalog, [AudioTrack(codec="AAC")], [], is_audio_description) == []
    # played directly: the renditions are what plays
    direct = tc.catalog_for(pmt, media, rendition_for_pid, buffered=False)
    assert [(t.id, t.in_buffer) for t in direct] == [("subtitle/rendition:English", True)]
    # the capture writes the cues of the WebVTT into the buffer (stage H)
    held = tc.catalog_for(pmt, media, rendition_for_pid, buffered=True, held_subtitles=["English"])
    assert [(t.id, t.in_buffer) for t in held] == [
        ("audio/pid:0x42", True), ("subtitle/rendition:English", True)]
    assert tc.compare(held, [AudioTrack(codec="AAC")], ["cue"], is_audio_description) == []


def test_label():
    names = {"deu": "German", "eng": "English"}.get
    assert tc.label(tc.Track(tc.AUDIO, "AAC", "deu", roles=frozenset({tc.MAIN})),
                    names) == "German · AAC"
    assert tc.label(tc.Track(tc.AUDIO, "AAC", "deu", roles=frozenset({tc.DESCRIPTION})),
                    names) == "German · Audio description · AAC"
    assert tc.label(tc.Track(tc.AUDIO, "", "deu", "TV Ton", frozenset({tc.MAIN})),
                    names) == "German – TV Ton"
    assert tc.label(tc.Track(tc.AUDIO, "", "", "Klare Sprache", frozenset({tc.EASY})),
                    names) == "Klare Sprache · Easy language"
    assert tc.label(tc.Track(tc.AUDIO, "MP2", "", roles=frozenset({tc.ORIGINAL})),
                    names) == "Original · MP2"
    # the name already says it: no "Audio description" twice
    assert tc.label(tc.Track(tc.AUDIO, "", "", "Audio description",
                             frozenset({tc.DESCRIPTION}))) == "Audio description"


def test_compare_agrees_and_differs():
    catalog = tc.from_pmt(PMT)
    same = [AudioTrack(language="deu"), AudioTrack(language="deu", description=True),
            AudioTrack(language="ger"), AudioTrack(language="eng")]
    assert tc.compare(catalog, same, [], is_audio_description) == []
    # the menu misses the audio description mark and a track
    short = [AudioTrack(language="deu"), AudioTrack(language="deu"), AudioTrack(language="eng")]
    diff = tc.compare(catalog, short, [], is_audio_description)
    assert any(d.startswith("audio: 3 tracks in the menu, 4") for d in diff)
    assert any(d.startswith("audio description: 0 in the menu, 1") for d in diff)
    # nothing to compare yet: no lines
    assert tc.compare(catalog, [], [], is_audio_description) == []
    assert tc.compare([], same, ["text"], is_audio_description) == []


def test_compare_subtitles_without_captions():
    catalog = tc.catalog_for(None, parse_hls(sk.REDUNDANT_MASTER)["media"])
    audio = [AudioTrack(title="TV Ton", language="deu"), AudioTrack(title="Klare Sprache"),
             AudioTrack(title="Audio-Deskription", language="deu", description=True)]
    assert tc.compare(catalog, audio, ["text", "cc"], is_audio_description) == []
    assert tc.compare(catalog, audio, ["text", "text"], is_audio_description) == \
        ["subtitles: 2 in the menu, 1 in the catalog"]


def test_player_writes_the_catalog_to_the_debug_log(monkeypatch):
    """Shadow mode: with the debug log on, the catalog and the differences
    go into it once; nothing is written again while nothing changes."""
    from lindvb import debuglog
    from lindvb.player import Player
    lines = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append((cat, " ".join(p))))
    fake = types.SimpleNamespace(
        _catalog_timer=0, _token=1, playbin=object(), _pmt_raw=PMT, _catalog_logged=None,
        capture=None,
        _media=lambda kind=None: [],
        audio_track_info=lambda: ([AudioTrack(language="deu"), AudioTrack(language="eng")], 0),
        _subtitle_entries=lambda: [], buffer_subtitles=lambda: [])
    assert Player._shadow_catalog(fake, 1) is False
    assert lines[0][0] == "Tracks" and "audio/pid:0x13EF" in lines[0][1]
    assert any("2 tracks in the menu, 4 in the catalog" in text for _c, text in lines)
    n = len(lines)
    Player._shadow_catalog(fake, 1)
    assert len(lines) == n                           # unchanged: not written again
    Player._shadow_catalog(fake, 2)                  # another channel by now: nothing
    assert len(lines) == n


def test_shadow_mode_costs_nothing_without_the_debug_log(monkeypatch):
    from lindvb import debuglog
    from lindvb.player import Player
    monkeypatch.setattr(debuglog, "enabled", lambda: False)
    fake = types.SimpleNamespace(_catalog_timer=0, _token=1)
    Player._shadow_catalog_later(fake)
    assert fake._catalog_timer == 0                  # no timer started
