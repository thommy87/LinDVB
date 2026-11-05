"""Hardware video decoding: decoder ranking after the setting, fallback, detection."""

import pytest

from lindvb import hwaccel
from lindvb.hwaccel import RANK_HW_TOP, RANK_NONE, RANK_PRIMARY

DECODERS = [
    ("avdec_h264", "software", RANK_PRIMARY),
    ("mpeg2dec", "software", RANK_PRIMARY - 1),
    ("vah264dec", "vaapi", RANK_NONE),
    ("nvh264sldec", "nvdec", RANK_PRIMARY + 1),
    ("nvh264dec", "nvdec", RANK_PRIMARY),
    ("vulkanh264dec", "vulkan", RANK_NONE),
    ("v4l2slh264dec", "v4l2", RANK_PRIMARY + 1),
    ("vaapih264dec", "legacy", RANK_PRIMARY),
]


def test_klass_and_plugin():
    assert hwaccel.is_video_decoder("Codec/Decoder/Video/Hardware")
    assert hwaccel.is_video_decoder("Codec/Decoder/Video")
    assert not hwaccel.is_video_decoder("Codec/Decoder/Audio")
    assert not hwaccel.is_video_decoder("Filter/Effect/Video/Deinterlace")
    assert hwaccel.backend_of("va") == "vaapi"
    assert hwaccel.backend_of("nvcodec") == "nvdec"
    assert hwaccel.backend_of("libav") == "software"
    assert hwaccel.backend_of(None) == "software"


def test_auto_order_and_fallback():
    assert hwaccel.order("auto") == ("vaapi", "nvdec", "v4l2")
    assert hwaccel.order("bogus") == ("vaapi", "nvdec", "v4l2")
    assert hwaccel.order("auto", {"vaapi"}) == ("nvdec", "v4l2")
    assert hwaccel.order("vulkan") == ("vulkan",)
    assert hwaccel.order("nvdec", {"nvdec"}) == ()
    assert hwaccel.order("software") == ()


def test_auto_puts_gpu_above_software():
    r = hwaccel.ranks(DECODERS, "auto")
    assert r["avdec_h264"] == RANK_PRIMARY and r["mpeg2dec"] == RANK_PRIMARY - 1
    assert r["vah264dec"] > r["nvh264sldec"] > r["nvh264dec"] > r["v4l2slh264dec"] > RANK_PRIMARY
    assert r["vah264dec"] == RANK_HW_TOP
    # Vulkan Video only when chosen; the old gstreamer-vaapi never
    assert r["vulkanh264dec"] == RANK_NONE and r["vaapih264dec"] == RANK_NONE


def test_failed_interface_drops_out():
    r = hwaccel.ranks(DECODERS, "auto", {"vaapi"})
    assert r["vah264dec"] == RANK_NONE
    assert r["nvh264sldec"] > RANK_PRIMARY


def test_manual_choice_and_software():
    r = hwaccel.ranks(DECODERS, "vulkan")
    assert r["vulkanh264dec"] > RANK_PRIMARY
    assert all(r[n] == RANK_NONE for n in ("vah264dec", "nvh264dec", "v4l2slh264dec"))
    r = hwaccel.ranks(DECODERS, "software")
    assert all(r[n] == RANK_NONE for n, b, _r in DECODERS if b != "software")
    assert r["avdec_h264"] == RANK_PRIMARY


def test_describe():
    assert hwaccel.describe(("vah264dec", "vaapi")) == "vah264dec (VA-API)"
    assert hwaccel.describe(None) == ""


class _Factory:
    def __init__(self, plugin, klass="Codec/Decoder/Video/Hardware", name="x"):
        self.plugin, self.klass, self.name = plugin, klass, name

    def get_plugin_name(self):
        return self.plugin

    def get_metadata(self, _key):
        return self.klass

    def get_name(self):
        return self.name


class _Element:
    def __init__(self, factory, parent=None):
        self.factory, self.parent = factory, parent

    def get_factory(self):
        return self.factory

    def get_parent(self):
        return self.parent


def test_failed_backend_walks_up_to_the_gpu_element():
    dec = _Element(_Factory("va", name="vah264dec"))
    child = _Element(None, parent=dec)
    assert hwaccel.failed_backend(child) == "vaapi"
    deint = _Element(_Factory("va", "Filter/Effect/Video/Deinterlace", "vadeinterlace"))
    assert hwaccel.failed_backend(deint) == "vaapi"
    assert hwaccel.failed_backend(_Element(_Factory("libav", "Codec/Decoder/Video"))) is None
    assert hwaccel.failed_backend(_Element(_Factory("vaapi"))) is None    # legacy
    assert hwaccel.failed_backend(None) is None
    assert hwaccel.factory_backend(dec) == ("vah264dec", "vaapi")
    assert hwaccel.factory_backend(deint) is None


def test_apply_on_the_real_registry():
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    soft = Gst.ElementFactory.find("avdec_h264")
    vk = Gst.ElementFactory.find("vulkanh264dec")
    soft_rank = soft.get_rank() if soft else None
    try:
        hwaccel.apply("software")
        if vk is not None:
            assert vk.get_rank() == RANK_NONE
            hwaccel.apply("vulkan")
            assert vk.get_rank() > RANK_PRIMARY
        hwaccel.apply("auto")
        if vk is not None:
            assert vk.get_rank() == RANK_NONE
        if soft is not None:
            assert soft.get_rank() == soft_rank
        assert set(hwaccel.available()) <= {"vaapi", "nvdec", "vulkan", "v4l2"}
        deint = hwaccel.make_deinterlacer(None)
        if Gst.ElementFactory.find("deinterlace"):
            assert deint.get_factory().get_name() == "deinterlace"
    finally:
        hwaccel.apply("auto")
