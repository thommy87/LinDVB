# SPDX-License-Identifier: GPL-2.0-or-later
"""Closed captions: offered only when the video carries them, and drawn.

0.21.0 offered Subtitles › Closed Captions (and showed the OSD's subtitle
icon) on every channel once the packages had cc708overlay, and cc708overlay
drew nothing even for real captions: it ignores the caption meta on the
frames and only reads its cc_sink pad. Since stage F, LinDVB's own renderer
draws them; cc708overlay (the old path, ``old=True``) still can."""

import pytest

from lindvb.captions import caption_channels_608, caption_kinds

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)


def _parity(b):
    b &= 0x7f
    return b | (0x80 if bin(b).count("1") % 2 == 0 else 0)


def cc608(text):
    """cc_data triplets (field 1) for a roll-up caption of ``text``."""
    pairs = [(0x14, 0x25), (0x14, 0x25), (0x14, 0x2d), (0x14, 0x2d)]
    text += "\0" * (len(text) % 2)
    pairs += [(ord(text[i]), ord(text[i + 1])) for i in range(0, len(text), 2)]
    return [bytes([0xfc, _parity(a), _parity(b)]) for a, b in pairs]


def cc708(text):
    """One frame of cc_data: a DTVCC packet that defines, fills and shows
    window 0 of service 1."""
    svc = bytes([0x98, 0x38, 0x00, 0x00, 0x1f, 0x11, 0x0b]) + text.encode() + bytes([0x89, 0x01])
    blk = bytes([(1 << 5) | len(svc)]) + svc
    data = bytes([((len(blk) + 2) // 2) & 0x3f]) + blk
    data += b"\0" * (len(data) % 2)
    trip = [bytes([0xff, data[0], data[1]])] + [bytes([0xfe, data[i], data[i + 1]])
                                                for i in range(2, len(data), 2)]
    return b"".join(trip)


PADDING = bytes([0xfa, 0, 0])


def test_kinds_of_cc_data():
    assert caption_kinds(b"") == set()
    assert caption_kinds(PADDING * 20) == set()                      # DTVCC padding, not valid
    assert caption_kinds(bytes([0xfc, 0x80, 0x80]) * 2) == set()     # 608 null padding
    assert caption_kinds(b"".join(cc608("HI"))) == {"608"}
    assert caption_kinds(cc708("HI")) == {"708"}
    assert caption_kinds(cc608("HI")[0] + cc708("HI")) == {"608", "708"}


def _pair(b0, b1, b2):
    return bytes([b0, _parity(b1), _parity(b2)])


def test_608_channels_with_captions():
    """A channel counts once a command starts captions on it (RCL, RU2-4,
    RDC); text mode, XDS and CEA-708 do not."""
    assert caption_channels_608(b"".join(cc608("HI"))) == {1}                 # RU2, CC1
    assert caption_channels_608(_pair(0xfc, 0x1c, 0x20)) == {2}               # RCL, CC2
    assert caption_channels_608(_pair(0xfd, 0x15, 0x29)) == {3}               # RDC, field 2
    assert caption_channels_608(_pair(0xfd, 0x14, 0x26)) == {3}               # 0x14 there too
    assert caption_channels_608(_pair(0xfd, 0x1d, 0x27)) == {4}
    assert caption_channels_608(_pair(0xfc, 0x14, 0x2a)) == set()             # text mode
    assert caption_channels_608(_pair(0xfc, 0x14, 0x2c)) == set()             # erase only
    assert caption_channels_608(_pair(0xfd, 0x01, 0x03)) == set()             # XDS
    assert caption_channels_608(_pair(0xf8, 0x14, 0x25)) == set()             # not valid
    assert caption_channels_608(cc708("HI") + PADDING) == set()


def _split(text, start, per_frame):
    """{frame: cc_data}: the DTVCC packet of :func:`cc708` spread over
    frames, *per_frame* triplets each (padded to 20), as broadcasters send
    them."""
    trip = [cc708(text)[i:i + 3] for i in range(0, len(cc708(text)), 3)]
    return {start + k // per_frame: b"".join(trip[k:k + per_frame]) +
            PADDING * (20 - len(trip[k:k + per_frame]))
            for k in range(0, len(trip), per_frame)}


def _need(*factories):
    missing = [f for f in factories if Gst.ElementFactory.find(f) is None]
    if missing:
        pytest.skip("missing " + ", ".join(missing))


def _play(frames, silent=False, n=60, measure=False, old=True, listen=None):
    """Black video with these cc_data frames through CaptionOverlay at real
    speed (the overlay times captions against the picture); returns the
    overlay and the largest number of bright pixels in a frame."""
    from lindvb.captions import CaptionOverlay
    cc = CaptionOverlay(silent=silent, old=old)
    if listen is not None:
        cc.listen(listen, "test")
    cc.measure(measure)
    p = Gst.parse_launch(
        f"videotestsrc pattern=black num-buffers={n} ! "
        "video/x-raw,format=I420,width=320,height=180,framerate=30/1 ! cccombiner name=c "
        "appsrc name=a format=time caps=closedcaption/x-cea-708,format=cc_data,framerate=30/1 ! c.caption")
    sink = Gst.parse_bin_from_description(
        "videoconvert ! video/x-raw,format=GRAY8 ! appsink name=s sync=true", True)
    p.add(cc.bin)
    p.add(sink)
    p.get_by_name("c").link(cc.bin)
    cc.bin.link(sink)
    a = p.get_by_name("a")
    for i in range(n):
        b = Gst.Buffer.new_wrapped(frames.get(i, PADDING))
        b.pts, b.duration = i * Gst.SECOND // 30, Gst.SECOND // 30
        a.emit("push-buffer", b)
    a.emit("end-of-stream")
    p.set_state(Gst.State.PLAYING)
    s = sink.get_by_name("s")
    bright = count = 0
    while (smp := s.emit("try-pull-sample", 10 * Gst.SECOND)) is not None:
        count += 1
        buf = smp.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        bright = max(bright, sum(1 for x in m.data if x > 100))
        buf.unmap(m)
    msg = p.get_bus().pop_filtered(Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    assert msg is None, msg.parse_error()
    assert count == n                    # the picture never waits for captions
    return cc, bright


@pytest.fixture
def elements():
    _need("cc708overlay", "ccextractor", "ccconverter", "cccombiner", "videotestsrc", "appsrc")


@pytest.fixture
def extraction():
    _need("ccextractor", "ccconverter", "cccombiner", "videotestsrc", "appsrc")


def test_captions_need_no_overlay(extraction, monkeypatch):
    """GStreamer 1.28 of Ubuntu 26.04 and Arch has no cc708overlay: the
    captions are still taken off the frames, offered and handed on; the
    picture goes through untouched."""
    from lindvb import captions
    assert captions.available()
    monkeypatch.delenv("LINDVB_CC708OVERLAY", raising=False)
    assert not captions.old_path()
    got = []
    data = cc708("HELLO")
    cc, bright = _play({10: data, 20: data}, old=False, listen=lambda d, f, t: got.append((d, f, t)))
    assert cc.overlay is None and cc.present and cc.services == [1]
    assert bright == 0                                   # nothing drawn into the picture
    assert {f for _d, f, _t in got} == {"cc_data"}
    t = next(t for d, _f, t in got if data in d)        # in the frame's cc_data, padded
    assert t == 10 * Gst.SECOND // 30                    # the stream time of its frame
    cc.show(1)
    assert cc.service == 1


def test_old_path_only_when_asked(elements, monkeypatch):
    from lindvb import captions
    monkeypatch.setenv("LINDVB_CC708OVERLAY", "1")
    assert captions.old_path()
    assert captions.CaptionOverlay().overlay is not None
    monkeypatch.setenv("LINDVB_CC708OVERLAY", "0")
    assert captions.CaptionOverlay().overlay is None


def test_old_path_draws_nothing_of_a_packet_over_several_frames(elements):
    """Why LinDVB draws the captions itself: broadcasters spread a DTVCC
    packet over several frames (PBS WJXT), and cc708overlay (GStreamer
    1.24.2) then draws nothing; the same packet in one frame it draws."""
    whole = {}
    split = {}
    for start in (10, 30, 50):
        whole[start] = cc708("HELLO")
        split.update(_split("HELLO", start, 3))
    _cc, bright_whole = _play(whole, n=90)
    _cc, bright_split = _play(split, n=90)
    assert bright_whole > 100
    if bright_split:
        pytest.skip("this cc708overlay draws packets over several frames")


def test_no_captions_no_offer(elements):
    cc, bright = _play({})
    assert not cc.present and bright == 0


def test_708_captions_are_found_and_drawn(elements):
    data = cc708("HELLO")
    cc, bright = _play({10: data, 20: data, 30: data})
    assert cc.present
    assert bright > 100


def test_silent_draws_nothing(elements):
    data = cc708("HELLO")
    cc, bright = _play({10: data, 20: data, 30: data}, silent=True)
    assert cc.present and bright == 0


def test_608_only_is_not_offered_by_the_old_path(elements):
    """cc708overlay cannot draw CEA-608: no menu entry that shows nothing."""
    cc, bright = _play(dict(enumerate(cc608("HELLO WORLD"))))
    assert cc.kinds == {"608"}
    assert not cc.present and cc.channels == []


def test_608_only_is_offered(extraction):
    """No CEA-708: the CEA-608 channel that carries captions is offered,
    LinDVB's CEA-608 renderer draws it."""
    cc, bright = _play(dict(enumerate(cc608("HELLO WORLD"))), old=False)
    assert cc.kinds == {"608"} and cc.present
    assert cc.channels == [1] and cc.services == []
    assert bright == 0                                  # nothing blended into the picture


def test_608_beside_708_only_708_is_offered(extraction):
    """By convention service 1 carries what CC1 does: one entry, not two."""
    frames = dict(enumerate(cc608("HELLO WORLD")))
    frames.update({20: cc708("HELLO"), 30: cc708("HELLO")})
    cc, _bright = _play(frames, old=False)
    assert cc.kinds == {"608", "708"} and cc.present
    assert cc.services == [1] and cc.channels == []


def cc708_service(text, service):
    """Like cc708(), for another caption service."""
    svc = bytes([0x98, 0x38, 0x00, 0x00, 0x1f, 0x11, 0x0b]) + text.encode() + bytes([0x89, 0x01])
    blk = bytes([(service << 5) | len(svc)]) + svc
    data = bytes([((len(blk) + 2) // 2) & 0x3f]) + blk
    data += b"\0" * (len(data) % 2)
    return b"".join([bytes([0xff, data[0], data[1]])] + [bytes([0xfe, data[i], data[i + 1]])
                                                       for i in range(2, len(data), 2)])


def test_services_of_dtvcc_packets():
    from lindvb.captions import DtvccScanner
    sc = DtvccScanner()
    assert sc.feed(cc708("HI") + PADDING) == {1}
    assert sc.feed(cc708_service("HOLA", 2) + PADDING) == {2}
    assert sc.feed(cc708("AGAIN") + PADDING) == set()
    assert sc.services == {1, 2}
    assert DtvccScanner().feed(b"".join(cc608("HI"))) == set()


def test_caption_service_descriptor():
    """ATSC A/65 caption_service_descriptor in the PMT: language per service."""
    from lindvb.captions import caption_services_from_descriptors, instream_service
    body = bytes([0xc2]) + b"eng" + bytes([0x80 | 1, 0x3f, 0xff]) + b"spa" + bytes([0x80 | 2, 0x3f, 0xff])
    assert caption_services_from_descriptors(bytes([0x86, len(body)]) + body) == {1: "eng", 2: "spa"}
    line21 = bytes([0xc1]) + b"eng" + bytes([0x3e, 0x3f, 0xff])
    assert caption_services_from_descriptors(bytes([0x86, len(line21)]) + line21) == {-1: "eng"}
    assert instream_service("CC1") == 1 and instream_service("SERVICE3") == 3
    assert instream_service("CC2") is None


def test_caption_label():
    """'English (CC1)' from the HLS playlist or the PMT, a plain name otherwise."""
    from lindvb.player import Player
    p = Player.__new__(Player)
    p.uri, p.capture = "http://x/master.m3u8", None
    p.renditions = {p.uri: [{"type": "CLOSED-CAPTIONS", "name": "English", "language": "en",
                             "instream_id": "CC1"}]}
    p._cc_pmt = {2: "spa"}
    assert p.caption_label(1) == "English (CC1)"
    assert p.caption_label(2).endswith("(service 2)") and p.caption_label(2) != \
        "Closed captions (service 2)"
    p.renditions = {}
    p._cc_pmt = {}
    assert p.caption_label(1) == "Closed captions (CC1)"


def test_overlay_last_in_the_filter(elements):
    """The overlay draws behind the converters it is given (middle)."""
    from lindvb.captions import CaptionOverlay
    mid = [Gst.ElementFactory.make("videoconvert"), Gst.ElementFactory.make("videobalance")]
    cc = CaptionOverlay(silent=True, middle=mid, old=True)
    assert mid[1].get_static_pad("src").get_peer().get_parent() is cc.overlay
    assert cc.service == 0 and cc.overlay.get_property("silent")
    cc.show(2)
    assert cc.overlay.get_property("service-number") == 2 and not cc.overlay.get_property("silent")
    cc.show(0)
    assert cc.overlay.get_property("silent") and cc.overlay.get_property("service-number") == 0


def test_caption_shows_with_its_frame(elements):
    """A caption sent with frame 10 is drawn from about frame 10 on, not later."""
    from lindvb.captions import CaptionOverlay
    cc = CaptionOverlay(silent=False, old=True)
    p = Gst.parse_launch(
        "videotestsrc pattern=black num-buffers=40 ! "
        "video/x-raw,format=I420,width=320,height=180,framerate=30/1 ! cccombiner name=c "
        "appsrc name=a format=time caps=closedcaption/x-cea-708,format=cc_data,framerate=30/1 ! c.caption")
    sink = Gst.parse_bin_from_description(
        "videoconvert ! video/x-raw,format=GRAY8 ! appsink name=s sync=true", True)
    p.add(cc.bin)
    p.add(sink)
    p.get_by_name("c").link(cc.bin)
    cc.bin.link(sink)
    a = p.get_by_name("a")
    for i in range(40):
        b = Gst.Buffer.new_wrapped(cc708("HELLO") if i == 10 else PADDING)
        b.pts, b.duration = i * Gst.SECOND // 30, Gst.SECOND // 30
        a.emit("push-buffer", b)
    a.emit("end-of-stream")
    p.set_state(Gst.State.PLAYING)
    s = sink.get_by_name("s")
    shown = []
    while (smp := s.emit("try-pull-sample", 10 * Gst.SECOND)) is not None:
        buf = smp.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        shown.append(sum(1 for x in m.data if x > 100) > 50)
        buf.unmap(m)
    p.set_state(Gst.State.NULL)
    assert len(shown) == 40
    assert shown.index(True) <= 12


# -- what the captions carry (channel analysis) -------------------------------------

def test_text_of_608_pairs():
    from lindvb.captions import text_608
    assert text_608(_parity(0x48), _parity(0x49)) == 2              # "HI"
    assert text_608(_parity(0x48), 0x80) == 1                       # "H" and a null
    assert text_608(_parity(0x14), _parity(0x2c)) == 0              # EDM: a command
    assert text_608(_parity(0x11), _parity(0x37)) == 1              # special character ♪
    assert text_608(_parity(0x12), _parity(0x25)) == 1              # extended character


def test_text_of_708_blocks():
    """Characters count, commands and their parameters do not."""
    from lindvb.captions import text_708
    define = bytes([0x98, 0x38, 0x00, 0x00, 0x1f, 0x11, 0x0b])    # DefineWindow: 6 parameters
    assert text_708(define + b"HELLO" + bytes([0x89, 0x01])) == 5
    assert text_708(define + bytes([0x89, 0x01, 0x8c, 0xff])) == 0  # commands only
    assert text_708(bytes([0x90, 0x05, 0x00, 0x91, 0x2a, 0x00, 0x00]) + b"OK") == 2
    assert text_708(bytes([0x0d, 0x03]) + b"A" + bytes([0x10, 0x25])) == 2   # CR, ETX, EXT1 G2
    assert text_708(bytes([0xe4, 0xf6])) == 2                       # G1: "äö"


def test_caption_count_per_field_and_service():
    from lindvb.captions import CaptionCount
    c = CaptionCount()
    c.feed(b"".join(cc608("HELLO WORLD")))
    c.feed(cc708("HELLO") + PADDING)
    c.feed(cc708_service("HOLA", 2) + PADDING)
    c.feed(bytes([0xfc, 0x80, 0x80]) * 4)                          # 608 null padding
    c.feed(PADDING * 10)
    out = c.summary()
    assert out["buffers"] == 5
    assert out["608"][1][1] == 11 and 2 not in out["608"]          # "HELLO WORLD"
    assert out["608"][1][0] == 2 * 10                               # 4 commands, 6 text pairs
    assert out["708"][1][1] == 5 and out["708"][2][1] == 4
    assert out["xds"] == 0


def test_caption_count_sends_nothing():
    """A channel that sends only commands (Google DAI's empty 708 service)
    or padding: bytes, but no text."""
    from lindvb.captions import CaptionCount
    c = CaptionCount()
    svc = bytes([0x98, 0x38, 0x00, 0x00, 0x1f, 0x11, 0x0b, 0x89, 0x01])
    blk = bytes([(1 << 5) | len(svc)]) + svc
    data = bytes([((len(blk) + 2) // 2) & 0x3f]) + blk
    data += b"\0" * (len(data) % 2)
    c.feed(b"".join([bytes([0xff, data[0], data[1]])] +
                    [bytes([0xfe, data[i], data[i + 1]]) for i in range(2, len(data), 2)]) + PADDING)
    c.feed(PADDING * 20)
    out = c.summary()
    assert out["708"] == {1: (len(svc), 0)} and out["608"] == {}


def test_caption_count_keeps_xds_apart():
    """XDS programme data in field 2 (class 0x01, type, "NEWS", end 0x0F) is
    not caption text; caption text after it in field 2 is."""
    from lindvb.captions import CaptionCount
    c = CaptionCount()
    pairs = [(0x01, 0x03), (ord("N"), ord("E")), (ord("W"), ord("S")), (0x0f, 0x12),
             (0x15, 0x2c), (ord("O"), ord("K"))]
    c.feed(b"".join(bytes([0xfd, _parity(a), _parity(b)]) for a, b in pairs))
    out = c.summary()
    assert out["xds"] == 8
    assert out["608"] == {2: (4, 2)}


def test_caption_count_of_608_formats():
    from lindvb.captions import CaptionCount, cdp_cc_data
    c = CaptionCount()
    c.feed(bytes([_parity(ord("H")), _parity(ord("I"))]), "raw")
    c.feed(bytes([0x80, _parity(ord("A")), 0x80, 0x00, _parity(ord("B")), _parity(ord("C"))]),
           "s334-1a")
    assert c.summary()["608"] == {1: (4, 3), 2: (2, 2)}
    trip = cc708("HI")
    cdp = bytes([0x96, 0x69, 0, 0x4f, 0x43, 0, 1, 0x72, 0xe0 | (len(trip) // 3)]) + trip
    assert cdp_cc_data(cdp) == trip
    assert cdp_cc_data(b"\x00" * 12) == b""


def test_overlay_counts_while_measuring(elements):
    """The channel analysis: the overlay counts what the captions carried,
    and only while measuring."""
    data = cc708("HELLO")
    cc, _bright = _play({10: data, 20: data, 30: data}, measure=True)
    info = cc.caption_info()
    assert info["seen"] and info["708"][1][1] == 15
    assert info["buffers"] >= 3
    cc.measure(False)
    assert cc.caption_info() is None and cc._count_probe is None
    cc, _bright = _play({})
    assert cc.caption_info() is None
    cc, _bright = _play({}, measure=True)                # caption data, but only padding
    info = cc.caption_info()
    assert info["seen"] and info["buffers"] > 50 and info["608"] == info["708"] == {}
