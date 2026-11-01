"""Matching M3U channels to XMLTV guide channels."""

from lindvb.channels import ChannelDB, parse_m3u, write_m3u
from lindvb.xmltvmatch import Matcher, auto_match, normalize

GUIDE = {
    "DasErste.de": ["Das Erste"],
    "ZDF.de": ["ZDF"],
    "ProSieben.de": ["ProSieben"],
    "RTL.de": ["RTL Television", "RTL"],
    "Sat1.de": ["SAT.1"],
    "kabel1.de": ["Kabel Eins"],
    "E4.uk": ["E4"],
    "E4+1.uk": ["E4 +1"],
    "Arte.fr": ["ARTE"],
}

M3U = """#EXTM3U
#EXTINF:-1 tvg-id="zdf.de" group-title="DE",ZDF HD
http://x/zdf
#EXTINF:-1 group-title="DE",DE: Das Erste HD (1080p)
http://x/ard
#EXTINF:-1 tvg-name="Pro Sieben FHD",Pro7
http://x/pro7
#EXTINF:-1,|DE| SAT 1 HEVC
http://x/sat1
#EXTINF:-1,RTL
http://x/rtl
#EXTINF:-1,Kabel 1 HD
http://x/k1
#EXTINF:-1,E4 +1 HD
http://x/e41
#EXTINF:-1,Arté
http://x/arte
#EXTINF:-1,Totally Unknown Channel
http://x/unknown
"""


def test_normalize():
    assert normalize("DE: Das Erste HD (1080p)") == normalize("Das Erste") == "daserste"
    assert normalize("|DE| SAT 1 HEVC") == normalize("SAT.1") == "sat1"
    assert normalize("E4 +1") != normalize("E4")
    assert normalize("Arté") == "arte"


def test_matcher_methods():
    m = Matcher(GUIDE)
    assert m.match("ZDF HD", tvg_id="zdf.de") == ("ZDF.de", "tvg-id", 1.0)
    assert m.match("DE: Das Erste HD (1080p)")[:2] == ("DasErste.de", "name")
    assert m.match("RTL")[0] == "RTL.de"
    assert m.match("Pro7", tvg_name="Pro Sieben FHD")[0] == "ProSieben.de"
    assert m.match("E4 +1 HD")[0] == "E4+1.uk"
    assert m.match("E4 HD")[0] == "E4.uk"
    assert m.match("Totally Unknown Channel")[0] is None


def test_auto_match_keeps_manual_choices():
    chans = parse_m3u(M3U)
    assert chans[0].tvg_id == "zdf.de" and chans[2].tvg_name == "Pro Sieben FHD"
    chans[5].xmltv_id, chans[5].xmltv_manual = "kabel1.de", True
    chans[3].xmltv_manual = True                    # "no guide" chosen by hand
    matched, unmatched = auto_match(chans, GUIDE)
    ids = {c.name: c.xmltv_id for c in chans}
    assert ids["ZDF HD"] == "ZDF.de" and ids["DE: Das Erste HD (1080p)"] == "DasErste.de"
    assert ids["Pro7"] == "ProSieben.de" and ids["Arté"] == "Arte.fr"
    assert ids["Kabel 1 HD"] == "kabel1.de" and ids["|DE| SAT 1 HEVC"] == ""
    assert ids["Totally Unknown Channel"] == ""
    assert chans[0].epg_key == "xmltv:ZDF.de"
    assert chans[-1].epg_key == "iptv:totally unknown channel"
    assert (matched, unmatched) == (6, 1)      # the two manual ones are skipped


def test_reimport_keeps_match_and_export_writes_tvg_id(tmp_path, monkeypatch):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    db = ChannelDB()
    db.merge(parse_m3u(M3U))
    auto_match(db.channels, GUIDE)
    db.merge(parse_m3u(M3U))                        # import the playlist again
    assert db.channels[1].xmltv_id == "DasErste.de"
    out = write_m3u(db.channels)
    assert 'tvg-id="DasErste.de"' in out and 'tvg-name="Pro Sieben FHD"' in out
