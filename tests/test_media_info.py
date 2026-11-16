"""Audio track labels, info bar summary and EPG details (no GStreamer needed)."""

from lindvb import media_info
from lindvb.epg import Event, details_line, parse_xmltv, programme_details
from lindvb.media_info import (AudioTrack, audio_summary, audio_track_labels, channel_layout,
                               hls_has_alternate_audio)


def _no_gst(monkeypatch):
    # use the built-in English names so the result does not depend on the locale
    monkeypatch.setattr(media_info, "_gst_lookup", False)


def test_channel_layout():
    assert [channel_layout(n) for n in (1, 2, 6, 8, 0)] == ["Mono", "Stereo", "5.1", "7.1", ""]


def test_labels_with_language_and_layout(monkeypatch):
    _no_gst(monkeypatch)
    tracks = [AudioTrack("deu", codec="MPEG-4 AAC", channels=2),
              AudioTrack("deu", codec="AC-3 (ATSC A/52)", channels=6),
              AudioTrack("eng", channels=2), AudioTrack("qaa", channels=2)]
    assert audio_track_labels(tracks) == ["German, Stereo", "German, 5.1",
                                          "English, Stereo", "Original, Stereo"]


def test_labels_fallback_without_tags(monkeypatch):
    _no_gst(monkeypatch)
    tracks = [AudioTrack(codec="MPEG-4 AAC", channels=2), AudioTrack(codec="MPEG-4 AAC"),
              AudioTrack(codec="MPEG-4 AAC")]
    assert audio_track_labels(tracks) == ["Track 1, Stereo", "Track 2", "Track 3"]


def test_labels_same_language_told_apart(monkeypatch):
    _no_gst(monkeypatch)
    same = [AudioTrack("deu", codec="MPEG-4 AAC", channels=2),
            AudioTrack("deu", codec="AC-3", channels=2)]
    assert audio_track_labels(same) == ["German, Stereo (AAC)", "German, Stereo (AC-3)"]
    twins = [AudioTrack("deu", channels=2), AudioTrack("deu", channels=2)]
    assert audio_track_labels(twins) == ["German, Stereo #1", "German, Stereo #2"]


def test_labels_use_stream_title(monkeypatch):
    _no_gst(monkeypatch)
    tracks = [AudioTrack("deu", title="Audiodeskription", channels=2),
              AudioTrack("deu", title="audio_1", channels=2)]
    assert audio_track_labels(tracks) == ["German – Audiodeskription, Stereo", "German, Stereo"]


def test_audio_summary(monkeypatch):
    _no_gst(monkeypatch)
    tracks = [AudioTrack("deu", channels=2), AudioTrack("deu", channels=6),
              AudioTrack("eng", channels=2)]
    assert audio_summary(tracks, 1) == (["5.1", "Stereo"], ["German", "English"])
    assert audio_summary([], -1) == ([], [])


def test_hls_alternate_audio():
    master = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",LANGUAGE="deu",NAME="Deutsch",'
              'URI="audio/deu.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=1,AUDIO="a"\nv.m3u8\n')
    muxed = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n'
    assert hls_has_alternate_audio(master)
    assert not hls_has_alternate_audio(muxed)
    # an audio entry without URI means the audio is inside the variant
    assert not hls_has_alternate_audio('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="x"\n')


def test_programme_details_from_eit_subtitle():
    ev = Event(0, 60, "Leony", "Show, Deutschland 2026")
    assert details_line(ev) == "Show, Deutschland 2026"
    assert programme_details(ev)[3] is True        # subtitle was just the details
    ev = Event(0, 60, "Film", "", "Spielfilm, USA 1995\nHandlung ...")
    assert details_line(ev) == "Spielfilm, USA 1995"
    assert details_line(Event(0, 60, "News", "Nachrichten", genre="News/Current affairs")) == "News"
    assert details_line(Event(0, 60, "Tagesschau", "Nachrichten")) == ""


def test_xmltv_country_and_year():
    xml = b"""<tv><channel id="3sat.de"><display-name>3sat</display-name></channel>
    <programme start="20261031211500 +0100" stop="20261031221000 +0100" channel="3sat.de">
      <title>Leony</title><category>Show</category><country>Deutschland</country>
      <date>2026</date></programme></tv>"""
    (name, ev), = list(parse_xmltv(xml))
    assert name == "3sat" and ev.country == "Deutschland" and ev.year == 2026
    assert details_line(ev) == "Show, Deutschland 2026"


def test_preferred_audio_track():
    from lindvb.media_info import AudioTrack, preferred_track
    de_mp2 = AudioTrack(language="deu", codec="MPEG-1 Layer 2 (MP2)", channels=2)
    de_ac3 = AudioTrack(language="deu", codec="AC-3 (ATSC A/52)", channels=6)
    de_eac3 = AudioTrack(language="deu", codec="E-AC-3", channels=6)
    en_ac3 = AudioTrack(language="eng", codec="AC-3", channels=6)
    tracks = [de_mp2, en_ac3, de_ac3, de_eac3]
    assert preferred_track(tracks, "ac3", 0) == 2           # same language, AC-3
    assert preferred_track(tracks, "eac3", 0) == 3
    assert preferred_track(tracks, "stereo", 2) == 0
    assert preferred_track(tracks, "ac3", 2) is None        # already playing
    assert preferred_track(tracks, "off", 0) is None
    assert preferred_track([de_mp2, en_ac3], "ac3", 0) is None   # not in this language
    # E-AC-3 wanted but only AC-3 there: the other Dolby format
    assert preferred_track([de_mp2, de_ac3], "eac3", 0) == 1
    assert preferred_track([de_mp2], "ac3", 0) is None


# -- OSD technical row (issue #127) ---------------------------------------------

from types import SimpleNamespace  # noqa: E402

from lindvb.media_info import (aspect_label, audio_badges, is_hdr, language_short,  # noqa: E402
                               layout_icon, source_label, video_class)


def _ch(**kw):
    base = dict(delsys="dvb-s2", dvbi_id="", tvh_server="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_source_label():
    assert source_label(_ch()) == "DVB-S2"
    assert source_label(_ch(delsys="dvb-s")) == "DVB-S"
    assert source_label(_ch(delsys="dvb-c")) == "DVB-C"
    assert source_label(_ch(delsys="dvb-t2")) == "DVB-T2"
    assert source_label(_ch(delsys="atsc")) == "ATSC"
    assert source_label(_ch(delsys="isdb-t")) == "ISDB-T"
    assert source_label(_ch(delsys="dtmb")) == "DTMB"
    assert source_label(_ch(delsys="iptv")) == "IPTV"
    assert source_label(_ch(), satip=True) == "SAT>IP"
    assert source_label(_ch(delsys="iptv", tvh_server="tvh1")) == "TVHeadend"
    assert source_label(_ch(delsys="dvb-t2", dvbi_id="tag:x")) == "DVB-I"


def test_video_class():
    assert video_class(720, 576, "interleaved") == ("SD", "576i")
    assert video_class(720, 480, "mixed") == ("SD", "480i")
    assert video_class(1280, 720, "progressive") == ("HD", "720p")
    assert video_class(1440, 1080, "interleaved") == ("HD", "1080i")
    assert video_class(1920, 1088, "progressive") == ("HD", "1080p")
    assert video_class(3840, 2160, "progressive") == ("UHD", "2160p")
    assert video_class(640, 360) == ("SD", "360")
    assert video_class(0, 0) is None


def test_aspect_label():
    assert aspect_label(720, 576, (64, 45)) == "16:9"
    assert aspect_label(720, 576, (16, 15)) == "4:3"
    assert aspect_label(1920, 1080) == "16:9"
    assert aspect_label(1440, 1080, (4, 3)) == "16:9"
    assert aspect_label(640, 480, (1, 1)) == "4:3"
    assert aspect_label(0, 576) is None


def test_is_hdr():
    assert is_hdr("bt2100-pq") and is_hdr("bt2100-hlg") and is_hdr("1:4:14:7")
    assert not is_hdr("bt709") and not is_hdr("2:4:5:4") and not is_hdr(None)


def test_audio_badges(monkeypatch):
    _no_gst(monkeypatch)
    tracks = [AudioTrack("deu", codec="MPEG-1 Layer 2", channels=2),
              AudioTrack("deu", codec="AC-3 (ATSC A/52)", channels=6),
              AudioTrack("eng", codec="MPEG-1 Layer 2", channels=2),
              AudioTrack("qad", codec="MPEG-1 Layer 2", channels=2)]
    badges, langs = audio_badges(tracks, 1)
    assert badges == [("lindvb-audio-surround-51-symbolic", "5.1", "AC-3", True),
                      ("lindvb-audio-stereo-symbolic", "Stereo", "MP2", False)]
    assert langs == ["DE", "EN"]
    assert layout_icon(8) == "lindvb-audio-surround-71-symbolic" and layout_icon(3) is None
    assert language_short("qaa") == "OV" and language_short("und") is None
