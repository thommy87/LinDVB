# SPDX-License-Identifier: GPL-2.0-or-later
"""Closed captions: offered only when the video carries them, and drawn.

0.21.0 offered Subtitles › Closed Captions (and showed the OSD's subtitle
icon) on every channel once the packages had cc708overlay, and cc708overlay
drew nothing even for real captions: it ignores the caption meta on the
frames and only reads its cc_sink pad."""

import pytest

from lindvb.captions import caption_kinds

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


def _need(*factories):
    missing = [f for f in factories if Gst.ElementFactory.find(f) is None]
    if missing:
        pytest.skip("missing " + ", ".join(missing))


def _play(frames, silent=False, n=60):
    """Black video with these cc_data frames through CaptionOverlay at real
    speed (the overlay times captions against the picture); returns the
    overlay and the largest number of bright pixels in a frame."""
    from lindvb.captions import CaptionOverlay
    cc = CaptionOverlay(silent=silent)
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


def test_608_only_is_not_offered(elements):
    """cc708overlay cannot draw CEA-608: no menu entry that shows nothing."""
    cc, bright = _play(dict(enumerate(cc608("HELLO WORLD"))))
    assert cc.kinds == {"608"}
    assert not cc.present


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
    cc = CaptionOverlay(silent=True, middle=mid)
    assert mid[1].get_static_pad("src").get_peer().get_parent() is cc.overlay
    assert cc.service == 0 and cc.overlay.get_property("silent")
    cc.show(2)
    assert cc.overlay.get_property("service-number") == 2 and not cc.overlay.get_property("silent")
    cc.show(0)
    assert cc.overlay.get_property("silent") and cc.overlay.get_property("service-number") == 0


def test_caption_shows_with_its_frame(elements):
    """A caption sent with frame 10 is drawn from about frame 10 on, not later."""
    from lindvb.captions import CaptionOverlay
    cc = CaptionOverlay(silent=False)
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
