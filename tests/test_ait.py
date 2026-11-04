"""HbbTV application signalling: PMT entry and AIT sections (TS 102 809)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb import ait  # noqa: E402
from lindvb.epgsources import mpeg_crc  # noqa: E402


def _desc(tag, body):
    return bytes([tag, len(body)]) + body


def _pmt(program=0x6D66, ait_pid=0x0FA1, app_types=(0x0010,)):
    sig = b"".join(bytes([0x80 | (t >> 8), t & 0xFF, 0xE0 | 1]) for t in app_types)
    body = (program.to_bytes(2, "big") + bytes([0xC1, 0x00, 0x00, 0xE0 | 0x01, 0xFF, 0xF0, 0x00])
            + bytes([0x02, 0xE1, 0xFF, 0xF0, 0x00])                       # video, no descriptors
            + bytes([0x05, 0xE0 | (ait_pid >> 8), ait_pid & 0xFF, 0xF0, len(sig) + 2])
            + _desc(0x6F, sig))
    head = bytes([0x02, 0xB0 | ((len(body) + 4) >> 8), (len(body) + 4) & 0xFF])
    sec = head + body
    return sec + mpeg_crc(sec).to_bytes(4, "big")


def _http(url, label=1):
    sel = bytes([len(url)]) + url.encode() + b"\x00"
    return _desc(0x02, bytes([0x00, 0x03, label]) + sel)


def _app_desc(version=(1, 7, 1), bound=True, priority=1, labels=(1,)):
    prof = bytes([0x00, 0x00, *version])
    flags = (0x80 if bound else 0) | (3 << 5) | 0x1F
    return _desc(0x00, bytes([len(prof)]) + prof + bytes([flags, priority, *labels]))


def _app(org, aid, code, descs):
    return (org.to_bytes(4, "big") + aid.to_bytes(2, "big") + bytes([code])
            + bytes([0xF0 | (len(descs) >> 8), len(descs) & 0xFF]) + descs)


def _ait(apps, common=b"", version=3, number=0, last=0):
    loop = b"".join(apps)
    body = (bytes([0x00, 0x10, 0xC1 | (version << 1), number, last])
            + bytes([0xF0 | (len(common) >> 8), len(common) & 0xFF]) + common
            + bytes([0xF0 | (len(loop) >> 8), len(loop) & 0xFF]) + loop)
    length = len(body) + 4
    sec = bytes([0x74, 0xB0 | (length >> 8), length & 0xFF]) + body
    return sec + mpeg_crc(sec).to_bytes(4, "big")


def _ard_like():
    start = _app(0x13, 0x0001, ait.AUTOSTART,
                 _app_desc(priority=5)
                 + _desc(0x01, b"deu" + bytes([11]) + b"Startleiste")
                 + _http("http://itv.ard.de/hbbtv-ard/")
                 + _desc(0x15, b"startleiste/index.php?sid=28106"))
    text = _app(0x13, 0x0002, ait.PRESENT,
                _app_desc(priority=2)
                + _desc(0x01, b"deu" + bytes([8]) + b"ARD Text")
                + _http("https://ard-text.de/", 1)
                + _desc(0x15, b"/index.html")
                + _desc(0x16, bytes([0x01])))
    return start, text


def _packets(pid, section, cc=0):
    """TS packets carrying one section (pointer field 0)."""
    data = b"\x00" + section
    out = []
    first = True
    while data:
        chunk, data = data[:184], data[184:]
        chunk += b"\xff" * (184 - len(chunk))
        out.append(bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF,
                          0x10 | (cc & 0x0F)]) + chunk)
        cc += 1
        first = False
    return b"".join(out)


def _pat(program, pmt_pid):
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00]) + program.to_bytes(2, "big") + \
        bytes([0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF])
    length = len(body) + 4
    sec = bytes([0x00, 0xB0, length]) + body
    return sec + mpeg_crc(sec).to_bytes(4, "big")


def test_pmt_announces_ait():
    assert ait.ait_pids_from_pmt(_pmt()) == (0x6D66, [0x0FA1])
    # an AIT for another application type (e.g. MHP 0x0001) is not HbbTV
    assert ait.ait_pids_from_pmt(_pmt(app_types=(0x0001,))) == (0x6D66, [])


def test_parse_ait():
    a, number, last = ait.parse_ait_section(_ait(_ard_like()))
    assert (a.app_type, a.version, number, last) == (0x0010, 3, 0, 0)
    start, text = a.apps
    assert start.key == (0x13, 1) and start.control_code == ait.AUTOSTART
    assert start.names == {"deu": "Startleiste"}
    assert start.profiles == [(0, (1, 7, 1))] and start.priority == 5
    assert start.service_bound and start.visibility == 3
    assert a.url(start) == "http://itv.ard.de/hbbtv-ard/startleiste/index.php?sid=28106"
    assert a.url(text) == "https://ard-text.de/index.html"
    assert a.autostart() is start
    assert a.teletext() is text


def test_newer_hbbtv_than_terminal_is_skipped():
    app = _app(1, 1, ait.AUTOSTART, _app_desc(version=(1, 8, 1)) + _http("http://x/"))
    a, *_ = ait.parse_ait_section(_ait([app]))
    assert not a.apps[0].supported()
    assert a.autostart() is None


def test_common_transport_and_carousel():
    oc = _desc(0x02, bytes([0x00, 0x01, 2, 0x00, 0x0B]))       # own carousel, tag 0x0B
    app = _app(7, 9, ait.AUTOSTART, _app_desc(labels=(2,)) + _desc(0x15, b"index.html"))
    a, *_ = ait.parse_ait_section(_ait([app], common=oc))
    assert a.url(a.apps[0]) == "dvb://current.b/index.html"


def test_collector_waits_for_all_sections():
    start, text = _ard_like()
    col = ait.AitCollector()
    assert col.feed(_ait([start], number=0, last=1)) is None
    whole = col.feed(_ait([text], number=1, last=1))
    assert [x.key for x in whole.apps] == [(0x13, 1), (0x13, 2)]
    assert col.feed(_ait([start], number=0, last=1)) is None     # same version again
    assert col.feed(_ait([start], version=4)).version == 4


def test_monitor_finds_ait_through_pat_and_pmt():
    got, pids = [], []
    mon = ait.AitMonitor(0x6D66, on_ait=got.append, on_pids=pids.append)
    ts = _packets(0, _pat(0x6D66, 0x0100)) + _packets(0x0100, _pmt()) + \
        _packets(0x0FA1, _ait(_ard_like()))
    mon.feed_buffer(b"\x00\x12" + ts)                # not aligned on a packet start
    assert pids == [[0x0FA1]]
    assert len(got) == 1 and got[0].autostart().key == (0x13, 1)


def test_dvb_text():
    assert ait.dvb_text(b"\x15Gr\xc3\xbc\xc3\x9fe") == "Grüße"
    assert ait.dvb_text(b"ZDF") == "ZDF"


def test_monitor_reports_video_pts():
    from lindvb.ait import AitMonitor
    from lindvb.ca import SectionAssembler  # noqa: F401
    mon = AitMonitor(0x6DCA, pmt_pid=0x100)
    got = []
    mon.on_pts = lambda pts, at: got.append(pts)
    # PMT with one H.264 stream on PID 0x200
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "hbbtv-test-ts.py")
    spec = importlib.util.spec_from_file_location("t", path)
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    pmt = t.section(0x02, 0x6DCA, bytes([0xE2, 0x00, 0xF0, 0x00]) + t.es(0x1B, 0x200, b""))
    data, _ = t.packets(0x100, pmt, 0)
    mon.feed_buffer(data)
    assert mon.video_pid == 0x200
    pts = 0x1_2345_6789
    b = [0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1, (pts >> 7) & 0xFF,
         ((pts << 1) & 0xFE) | 1]
    pes = bytes([0, 0, 1, 0xE0, 0, 0, 0x80, 0x80, 5]) + bytes(b)
    mon.feed_packet(bytes([0x47, 0x42, 0x00, 0x10]) + pes + b"\xff" * (184 - len(pes)))
    assert got == [pts]
