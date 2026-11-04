"""DSM-CC object carousels and stream events (lindvb.dsmcc)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb import dsmcc  # noqa: E402
from lindvb.ait import AitMonitor  # noqa: E402

FILES = {"index.html": b"<html><body>Videotext</body></html>" * 40,
         "js/app.js": b"var page = 100;\n" * 300,
         "img/logo.png": bytes(range(256)) * 20}
EVENTS = {"events/quiz": ({"question": 1, "answer": 2}, 0x0D)}


def _feed(oc, sections, order=None):
    ts, _ = dsmcc.ts_packets(0x200, sections)
    reader = dsmcc.SectionReader()
    for i in range(0, len(ts), 188):
        for s in reader.feed(ts[i:i + 188]):
            oc.feed(s)


def test_crc():
    assert dsmcc.crc32(b"123456789") == 0x0376E6E7


def test_carousel_round_trip():
    for compress in (False, True):
        oc = dsmcc.ObjectCarousel()
        _feed(oc, dsmcc.CarouselWriter(FILES, EVENTS, compress=compress).sections())
        assert oc.ready
        assert oc.files() == ["events/quiz", "img/logo.png", "index.html", "js/app.js"]
        assert oc.lookup("js/app.js").content == FILES["js/app.js"]
        assert oc.lookup("/img/logo.png").content == FILES["img/logo.png"]
        assert oc.lookup("missing.html") is False
        ste = oc.lookup("events/quiz")
        assert ste.kind == "ste" and ste.event_names == ["question", "answer"]
        assert ste.event_ids == [1, 2] and ste.event_tag() == 0x0D


def test_blocks_out_of_order_and_repeated():
    secs = dsmcc.CarouselWriter(FILES, block_size=500).sections()
    dsi, dii, ddbs = secs[0], secs[1], secs[2:]
    oc = dsmcc.ObjectCarousel()
    for s in list(reversed(ddbs)) + [dsi]:          # blocks before the DII: ignored
        oc.feed(s)
    assert not oc.ready and oc.lookup("index.html") is None
    for s in [dii] + ddbs[::2] + ddbs + ddbs:
        oc.feed(s)
    assert oc.lookup("index.html").content == FILES["index.html"]


def test_new_version_replaces_module():
    oc = dsmcc.ObjectCarousel()
    _feed(oc, dsmcc.CarouselWriter(FILES).sections())
    new = dict(FILES, **{"index.html": b"<html>v2</html>"})
    for s in dsmcc.CarouselWriter(new, version=1).sections():
        oc.feed(s)
    assert oc.lookup("index.html").content == b"<html>v2</html>"


def test_corrupt_sections_are_dropped():
    secs = dsmcc.CarouselWriter(FILES).sections()
    bad = bytearray(secs[2])
    bad[40] ^= 0xFF
    oc = dsmcc.ObjectCarousel()
    for s in secs[:2] + [bytes(bad)]:
        oc.feed(s)
    assert oc.lookup("index.html") is None


def test_stream_events_fire_once_per_version():
    flt = dsmcc.StreamEventFilter()
    s1 = dsmcc.stream_event_section(1, b"Frage 1", version=1)
    assert [(e.event_id, e.data) for e in flt.feed(s1)] == [(1, b"Frage 1")]
    assert flt.feed(s1) == []
    assert flt.feed(dsmcc.stream_event_section(1, b"Frage 2", version=2))[0].data == b"Frage 2"


def test_dvb_urls():
    assert dsmcc.parse_dvb_url("dvb://current.c/a/b.html?x=1") == ("current", 12, "a/b.html")
    assert dsmcc.parse_dvb_url("dvb://1.2.3.1f/x%20y.png") == ("1.2.3", 0x1F, "x y.png")
    assert dsmcc.parse_dvb_url("dvb://1.2.3/x") == ("1.2.3", None, "x")
    assert dsmcc.mime_type("index.HTML") == "text/html"


def _pmt(sid=28106):
    from importlib import util
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "hbbtv-test-ts.py")
    spec = util.spec_from_file_location("hbbtv_test_ts", path)
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_service_mounts_carousel_and_delivers_events():
    tool = _pmt()
    ts = tool.build("dvb://current.c/index.html", 28106, files=FILES, events=EVENTS, repeat=3,
                    stream_events=[(1, b"Frage 1", 1)])
    mon = AitMonitor(28106)
    added, got = [], []
    svc = dsmcc.DsmccService(mon, add_pid=added.append)
    ait_seen = []
    mon.on_ait = ait_seen.append
    for i in range(0, 188 * 3, 188):           # PAT, PMT, AIT
        mon.feed_packet(ts[i:i + 188])
    assert ait_seen and 0x0C in mon.components and 0x0D in mon.components
    app = ait_seen[0].apps[0]
    assert ait_seen[0].url(app) == "dvb://current.c/index.html"
    oc = svc.carousel(0x0C)
    svc.add_listener(0x0D, 1, got.append)
    assert set(added) == {mon.components[0x0C][0], mon.components[0x0D][0]}
    mon.feed_buffer(ts)
    assert svc.resolve("dvb://current.c/js/app.js")[1].content == FILES["js/app.js"]
    assert oc.lookup("index.html").content == FILES["index.html"]
    assert [e.data for e in got] == [b"Frage 1"]
    svc.close()
    assert not mon._watched


def test_event_description():
    xml = (b'<?xml version="1.0"?><dsmcc xmlns="urn:dvb:mis:dsmcc:2009"><dsmcc_object component_tag="13">'
           b'<stream_event stream_event_id="2" stream_event_name="answer"/></dsmcc_object></dsmcc>')
    assert dsmcc.event_description(xml, "answer") == (13, 2)
    assert dsmcc.event_description(xml, "other") is None
    assert dsmcc.event_description(b"<broken", "answer") is None
