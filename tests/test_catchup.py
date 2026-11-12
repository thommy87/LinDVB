"""Catch-up: programmes from the provider's archive (lindvb.catchup)."""

import calendar
import json

from lindvb import catchup
from lindvb.channels import Channel, parse_m3u, write_m3u

# 12 Nov 2026 20:15 UTC, 45 minutes; now is 22:00
START = calendar.timegm((2026, 11, 12, 20, 15, 0))
END = START + 45 * 60
NOW = START + 105 * 60


def chan(url, kind, days=7, source=""):
    return Channel(name="Test", delsys="iptv", url=url, catchup=kind, catchup_days=days,
                   catchup_source=source)


M3U = """#EXTM3U
#EXTINF:-1 tvg-id="a" catchup="default" catchup-days="3" catchup-source="http://arch/a?start={utc}&end={utcend}",A
http://live/a.ts
#EXTINF:-1 tvg-id="b" catchup="append" catchup-source="?utc={utc}&lutc={lutc}",B
http://live/b.m3u8?token=1
#EXTINF:-1 tvg-id="c" tvg-rec="5" catchup="shift",C
http://live/c.ts
#EXTINF:-1 tvg-id="d" timeshift="2",D
http://live/d.ts
#EXTINF:-1 tvg-id="e" catchup-source="?s={start}",E
http://live/e.ts
"""


def test_m3u_attributes():
    a, b, c, d, e = parse_m3u(M3U)
    assert (a.catchup, a.catchup_days) == ("default", 3) and "{utc}" in a.catchup_source
    assert (b.catchup, b.catchup_days) == ("append", catchup.DEFAULT_DAYS)
    assert (c.catchup, c.catchup_days) == ("shift", 5)
    assert d.catchup == ""                         # days alone: no kind, no archive
    assert (e.catchup, e.catchup_days) == ("append", catchup.DEFAULT_DAYS)
    again = parse_m3u(write_m3u([a, b, c]))
    assert [(x.catchup, x.catchup_days, x.catchup_source) for x in again] == \
        [(x.catchup, x.catchup_days, x.catchup_source) for x in (a, b, c)]


def test_addresses():
    assert catchup.url(chan("http://live/a.ts", "default", source="http://arch/a?start={utc}"
                            "&end={utcend}&d={duration:60}"), START, END, NOW) == \
        f"http://arch/a?start={START}&end={END}&d=45"
    assert catchup.url(chan("http://live/b.m3u8?token=1", "append",
                            source="?utc={utc}&lutc={lutc}"), START, END, NOW) == \
        f"http://live/b.m3u8?token=1&utc={START}&lutc={NOW}"
    assert catchup.url(chan("http://live/c.ts", "shift"), START, END, NOW) == \
        f"http://live/c.ts?utc={START}&lutc={NOW}"
    assert catchup.url(chan("http://fl/ch1/index.m3u8", "flussonic"), START, END, NOW) == \
        f"http://fl/ch1/index-{START}-2700.m3u8"
    assert catchup.url(chan("http://fl/ch1/mpegts", "flussonic"), START, END, NOW) == \
        f"http://fl/ch1/timeshift_abs-{START}.ts"
    assert catchup.url(chan("http://x/a.ts", "default",
                            source="http://x/{Y}/{m}/{d}/{H}-{M}.ts?t={utc:YmdHMS}"),
                       START, END, NOW) == "http://x/2026/11/12/20-15.ts?t=20261112201500"
    assert catchup.url(chan("http://x/a.ts", ""), START, END, NOW) == ""


def test_running_programme_ends_now():
    c = chan("http://fl/ch1/index.m3u8", "flussonic")
    assert catchup.url(c, START, END, START + 600) == f"http://fl/ch1/index-{START}-600.m3u8"


def test_xtream():
    url = "http://srv.example:8080/live/alex/se%2Fcret/1234.ts"
    assert catchup.xtream_parts(url) == ("http://srv.example:8080", "alex", "se/cret",
                                         "1234", "ts")
    assert catchup.xtream_parts("http://srv/alex/pw/77") == ("http://srv", "alex", "pw",
                                                             "77", "ts")
    assert catchup.xtream_parts("http://srv/movie/alex/pw/77.mkv") is None
    c = chan(url, "xc")
    assert catchup.url(c, START, END, NOW) == \
        "http://srv.example:8080/timeshift/alex/se%2Fcret/45/2026-11-12:20-15/1234.ts"
    m3u = "http://srv.example:8080/get.php?username=alex&password=pw&type=m3u_plus&output=ts"
    assert catchup.xtream_api_url(m3u) == \
        "http://srv.example:8080/player_api.php?username=alex&password=pw&action=get_live_streams"
    api = json.dumps([{"stream_id": 1234, "tv_archive": 1, "tv_archive_duration": "3"},
                      {"stream_id": "55", "tv_archive": 0, "tv_archive_duration": 7},
                      {"stream_id": None}])
    archive = catchup.parse_xtream_archive(api)
    assert archive == {"1234": 3}
    chans = [Channel(name="A", delsys="iptv", url=url),
             Channel(name="B", delsys="iptv", url="http://srv.example:8080/alex/pw/55.ts",
                     catchup="xc", catchup_days=7)]
    assert catchup.apply_xtream_archive(chans, archive) == 1
    assert (chans[0].catchup, chans[0].catchup_days) == ("xc", 3) and chans[1].catchup == ""
    assert catchup.parse_xtream_archive("not json") == {}


def test_available():
    c = chan("http://live/c.ts", "shift", days=1)
    assert catchup.available(c, START, END, NOW)                 # over, in the archive
    assert catchup.available(c, START, END, START + 60)          # running
    assert not catchup.available(c, START, END, START - 60)      # not begun yet
    assert not catchup.available(c, START, END, START + 2 * 86400)   # too old
    assert not catchup.available(chan("http://live/c.ts", ""), START, END, NOW)
