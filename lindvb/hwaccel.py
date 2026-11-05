# SPDX-License-Identifier: GPL-2.0-or-later
"""Hardware video decoding: which decoder playbin picks.

playbin takes the video decoder with the highest rank. Left alone, whether
that is a GPU decoder depends on the GStreamer version (the VA-API decoders
had no rank before 1.22, Vulkan ones have none at all). This module ranks the
decoders after the "video_decoder" setting:

  "auto"      VA-API, then NVDEC, then V4L2 (ARM boards), then software
  "vaapi" / "nvdec" / "vulkan" / "v4l2"
              only that interface; a codec it cannot decode is decoded in
              software
  "software"  no GPU decoding

A GPU decoder that fails while playing (a driver bug) is switched off for the
rest of the session with :func:`disable`; the player then plays again with
the next one in the order and finally in software.

The decoders of each interface are told apart by their GStreamer plugin, so
the ranking itself is plain Python and is tested without a graphics card.
"""

BACKENDS = ("auto", "vaapi", "nvdec", "vulkan", "v4l2", "software")
AUTO_ORDER = ("vaapi", "nvdec", "v4l2")

# GStreamer plugin -> interface of its decoders
PLUGIN_BACKEND = {
    "va": "vaapi",               # gst-plugins-bad VA-API (vah264dec, ...)
    "nvcodec": "nvdec",          # gst-plugins-bad NVDEC (nvh264dec, ...)
    "vulkan": "vulkan",          # gst-plugins-bad Vulkan Video (vulkanh264dec, ...)
    "v4l2codecs": "v4l2",        # stateless V4L2 (v4l2slh264dec, ...): Rockchip, Allwinner
    "video4linux2": "v4l2",      # stateful V4L2 (v4l2h264dec, ...): Raspberry Pi
    # the old gstreamer-vaapi (vaapih264dec): deprecated and dropped in
    # GStreamer 1.26; the "va" plugin replaces it, so it is never picked
    "vaapi": "legacy",
}

NAMES = {"vaapi": "VA-API", "nvdec": "NVDEC", "vulkan": "Vulkan Video", "v4l2": "V4L2",
         "software": "Software", "legacy": "gstreamer-vaapi"}

RANK_NONE = 0
RANK_PRIMARY = 256
RANK_HW_TOP = RANK_PRIMARY + 100     # above every software decoder (avdec_* are PRIMARY)

_disabled = set()                    # interfaces that failed in this session
_original = {}                       # factory name -> rank before we touched it


def is_video_decoder(klass):
    """True for the klass string of a video decoder ("Codec/Decoder/Video/Hardware")."""
    parts = set((klass or "").split("/"))
    return "Decoder" in parts and "Video" in parts


def backend_of(plugin_name):
    """The interface of an element from its plugin ("software" for the rest)."""
    return PLUGIN_BACKEND.get(plugin_name or "", "software")


def order(mode, disabled=()):
    """The GPU interfaces to use, best first, for the "video_decoder" setting."""
    if mode == "software":
        return ()
    chosen = AUTO_ORDER if mode not in BACKENDS or mode == "auto" else (mode,)
    return tuple(b for b in chosen if b not in disabled)


def ranks(decoders, mode, disabled=()):
    """New ranks for the video decoders.

    decoders: iterable of (factory name, backend, original rank). Returns
    {factory name: rank}; software decoders keep their own rank, the GPU
    decoders of the chosen interfaces are put above them (the first interface
    highest) and all other GPU decoders get rank none.
    """
    use = order(mode, disabled)
    out = {}
    for name, backend, rank in decoders:
        if backend == "software":
            out[name] = rank
        elif backend in use:
            # keep the plugin's own preference between its decoders (e.g. NVDEC
            # stateless before stateful) inside the interface's band
            out[name] = RANK_HW_TOP - 10 * use.index(backend) + min(max(rank - RANK_PRIMARY, 0), 9)
        else:
            out[name] = RANK_NONE
    return out


# -- GStreamer --------------------------------------------------------------

def _decoder_factories():
    from gi.repository import Gst
    reg = Gst.Registry.get()
    for f in reg.get_feature_list(Gst.ElementFactory):
        klass = f.get_metadata("klass") or ""
        if is_video_decoder(klass):
            yield f, backend_of(f.get_plugin_name())


def apply(mode):
    """Rank the installed video decoders after the setting (see :func:`ranks`).
    Takes effect for pipelines built from now on. Returns the order used."""
    facs = list(_decoder_factories())
    for f, _b in facs:
        _original.setdefault(f.get_name(), f.get_rank())
    new = ranks([(f.get_name(), b, _original[f.get_name()]) for f, b in facs], mode, _disabled)
    for f, _b in facs:
        r = new[f.get_name()]
        if f.get_rank() != r:
            f.set_rank(r)
    return order(mode, _disabled)


_available = None


def available():
    """The GPU interfaces that have at least one decoder here, e.g. ["vaapi"].

    GStreamer registers the VA-API, NVDEC and V4L2 decoders only when a
    device and driver for them is found, so this is what the machine can do."""
    global _available
    if _available is None:
        found = {b for _f, b in _decoder_factories() if b not in ("software", "legacy")}
        _available = [b for b in ("vaapi", "nvdec", "vulkan", "v4l2") if b in found]
    return list(_available)


def disable(backend, mode):
    """Switch an interface off for this session (its decoder failed) and rank again."""
    _disabled.add(backend)
    return apply(mode)


def disabled():
    return set(_disabled)


def factory_backend(element):
    """(factory name, backend) of a GStreamer element that is a video decoder, else None."""
    f = element.get_factory() if hasattr(element, "get_factory") else None
    if f is None or not is_video_decoder(f.get_metadata("klass")):
        return None
    return f.get_name(), backend_of(f.get_plugin_name())


def failed_backend(element):
    """The GPU interface an error came from: the element (a decoder, or e.g.
    vadeinterlace) or one of its parents belongs to a GPU plugin. None when
    no GPU element is involved."""
    while element is not None:
        f = element.get_factory() if hasattr(element, "get_factory") else None
        b = backend_of(f.get_plugin_name()) if f is not None else "software"
        if b not in ("software", "legacy"):
            return b
        element = element.get_parent()
    return None


def preferred(mode, have):
    """The GPU interface that will decode first: the first of :func:`order`
    that this machine has (have: :func:`available`), or None for software."""
    return next((b for b in order(mode, _disabled) if b in have), None)


def make_deinterlacer(backend):
    """The deinterlace element: VA-API's on the GPU (motion adaptive where the
    driver has it) when VA-API decodes, else GStreamer's software one."""
    from gi.repository import Gst
    if backend == "vaapi" and Gst.ElementFactory.find("vadeinterlace"):
        e = Gst.ElementFactory.make("vadeinterlace")
        if e is not None:
            _set_best_method(e)
            return e
    if Gst.ElementFactory.find("deinterlace"):
        return Gst.ElementFactory.make("deinterlace")
    return None


def _set_best_method(element):
    """vadeinterlace lists only the methods the driver supports."""
    pspec = element.find_property("method")
    try:
        nicks = {v.value_nick: v for v in pspec.enum_class.__enum_values__.values()}
    except AttributeError:
        return
    for nick in ("motion-adaptive", "motion-compensated"):
        if nick in nicks:
            element.set_property("method", nicks[nick])
            return


def describe(decoder):
    """"vah264dec (VA-API)" for the decoder in use, as (factory, backend)."""
    if not decoder:
        return ""
    name, backend = decoder
    return f"{name} ({NAMES.get(backend, backend)})"
