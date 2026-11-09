# SPDX-License-Identifier: GPL-2.0-or-later
"""GStreamer visualisations for radio: projectM (MilkDrop presets) and GOOM.

projectM needs the gst-projectm GStreamer plugin (element "projectm", built
on libprojectM 4) and a preset collection, for example from your distribution's
projectm / projectm-presets packages or from
https://github.com/projectM-visualizer. Without it this add-on only offers
GOOM (from gst-plugins-good) and LinDVB's own visualisations stay available.
The preset folder can be set as "projectm_preset_dir" in settings.json.

GStreamer visualisers render at 320x240 unless told otherwise, which looks
blocky in full screen. projectM draws with OpenGL; "auto" renders at the
screen's shape at most 1280x720, which starts fast and keeps up on most
graphics cards ("visualizer_resolution", Preferences › OSD and
Visualisations, sets more). A leaky queue in front of the visualiser drops
audio for the picture when it cannot keep up, so the sound never waits for it. The original GOOM runs on the CPU and slows down a lot at
high resolutions, so it uses "goom_resolution" (default 640x360, 16:9);
LinDVB's own "Goom HD (GPU)" renders goom's effects at full resolution.
"""

import os
import re
import sys
from pathlib import Path

NAME = "GStreamer visualisations (projectM, GOOM)"
VERSION = "1.4"

# the AppImage brings its own collection under its prefix (sys.prefix/share)
PRESET_DIRS = [str(Path(sys.prefix) / "share/projectM/presets"), "/usr/share/projectM/presets", "/usr/local/share/projectM/presets",
               "/usr/share/projectm-presets", "/app/share/projectM/presets",
               "~/.local/share/projectM/presets"]
TEXTURE_DIRS = [str(Path(sys.prefix) / "share/projectM/textures"), "/usr/share/projectM/textures", "/usr/local/share/projectM/textures",
                "~/.local/share/projectM/textures"]
MAX_SIZE = (2560, 1440)           # largest size "WIDTHxHEIGHT" may ask for
AUTO_SIZE = (1280, 720)           # "auto": the screen's shape, at most this
DEFAULT_SIZE = (1280, 720)
GOOM_SIZE = (640, 360)
FRAMERATE = 30


def _first_dir(candidates):
    for d in candidates:
        if d and Path(d).expanduser().is_dir():
            return str(Path(d).expanduser())
    return None


def _set(element, props, value):
    """Set the first of ``props`` this projectM build has (names differ by version)."""
    if value is None:
        return
    for prop in (props,) if isinstance(props, str) else props:
        if element.find_property(prop) is None:
            continue
        try:
            element.set_property(prop, value)
        except (TypeError, ValueError, OverflowError):
            pass
        return


def preset_folders(root, depth=2):
    """Folders below ``root`` (up to ``depth`` levels) that hold .milk presets,
    as sorted relative paths: ["Dancer", "Dancer/Aurora", ...]."""
    if not root:
        return []
    base = os.path.expanduser(root)
    found = set()
    for path, _dirs, files in os.walk(base):
        if not any(f.endswith(".milk") for f in files):
            continue
        parts = Path(os.path.relpath(path, base)).parts
        parts = () if parts == (".",) else parts
        for n in range(1, min(depth, len(parts)) + 1):
            found.add("/".join(parts[:n]))
    return sorted(found, key=lambda f: [p.lstrip("!# ").casefold() for p in f.split("/")])


def parse_size(text, default):
    """'1920x1080' -> (1920, 1080); anything else -> ``default``. Even sizes only."""
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*", str(text or ""))
    if not m:
        return default
    w, h = int(m.group(1)) & ~1, int(m.group(2)) & ~1
    return (w, h) if w >= 16 and h >= 16 else default


def screen_size(limit=MAX_SIZE, default=DEFAULT_SIZE):
    """Pixel size of the largest monitor, scaled down to fit ``limit`` (16:9 kept)."""
    try:
        import gi
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        best = None
        monitors = display.get_monitors() if display is not None else []
        for i in range(monitors.get_n_items() if monitors else 0):
            mon = monitors.get_item(i)
            geo, scale = mon.get_geometry(), max(1, mon.get_scale_factor())
            size = (geo.width * scale, geo.height * scale)
            if best is None or size[0] * size[1] > best[0] * best[1]:
                best = size
    except (ImportError, ValueError, AttributeError):
        best = None
    if not best:
        return default
    w, h = best
    f = min(1.0, limit[0] / w, limit[1] / h)
    return int(w * f) & ~1, int(h * f) & ~1


def fit(size, limit):
    """``size`` scaled down to fit ``limit``, shape kept, even sizes."""
    w, h = size
    f = min(1.0, limit[0] / w, limit[1] / h)
    return int(w * f) & ~1, int(h * f) & ~1


_ISOLATED = {}


def _isolated_bin_class(Gst):
    """A bin that keeps its OpenGL elements away from the application's GL context.

    GStreamer GL elements share the GL context of the video sink, which is GTK's.
    On Wayland GTK draws with OpenGL ES, and projectM needs desktop OpenGL: with
    GTK's context it fails ("Subclass failed to initialize"). projectM hands out
    ordinary video frames, so it does not need to share: this bin answers no GL
    context query or request, and projectM creates its own context."""
    cls = _ISOLATED.get("cls")
    if cls is not None:
        return cls

    def is_gl(context_type):
        return (context_type or "").startswith("gst.gl")

    try:
        import gi
        gi.require_version("GstGL", "1.0")
        from gi.repository import GstGL
    except (ImportError, ValueError):
        GstGL = None

    class IsolatedGLBin(Gst.Bin):
        __gtype_name__ = "LinDVBIsolatedGLBin"
        display = None

        def _display_context(self):
            """A GL display of our own that only offers desktop OpenGL (projectM
            needs it; on EGL GStreamer would otherwise pick OpenGL ES)."""
            if GstGL is None:
                return None
            if self.display is None:
                display = GstGL.GLDisplay.new()
                display.filter_gl_api(GstGL.GLAPI.OPENGL3 | GstGL.GLAPI.OPENGL)
                self.display = display
            context = Gst.Context.new("gst.gl.GLDisplay", True)
            GstGL.context_set_gl_display(context, self.display)
            return context

        def do_handle_message(self, message):
            if message.type == Gst.MessageType.NEED_CONTEXT:
                ok, context_type = message.parse_context_type()
                if ok and is_gl(context_type):
                    if context_type == "gst.gl.GLDisplay" and message.src is not None:
                        context = self._display_context()
                        if context is not None:
                            message.src.set_context(context)
                    return          # nothing from outside: no sharing with GTK's context
            elif message.type == Gst.MessageType.HAVE_CONTEXT:
                if is_gl(message.parse_have_context().get_context_type()):
                    return          # and its context stays inside
            Gst.Bin.do_handle_message(self, message)

        def do_set_context(self, context):
            if not is_gl(context.get_context_type()):
                Gst.Bin.do_set_context(self, context)

    _ISOLATED["cls"] = IsolatedGLBin
    return IsolatedGLBin


def _block_gl_queries(Gst, element):
    def probe(_pad, info):
        query = info.get_query()
        if query is not None and query.type == Gst.QueryType.CONTEXT:
            ok, context_type = query.parse_context_type()
            if ok and (context_type or "").startswith("gst.gl"):
                return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK
    for pad in element.pads:
        pad.add_probe(Gst.PadProbeType.QUERY_BOTH, probe)


def sized(Gst, element, size, any_memory=False, isolate_gl=False):
    """Wrap a visualiser so it renders at ``size`` instead of its default 320x240."""
    w, h = size
    if isolate_gl:
        bin_ = _isolated_bin_class(Gst)()
        _block_gl_queries(Gst, element)
    else:
        bin_ = Gst.Bin.new(None)
    caps = Gst.ElementFactory.make("capsfilter")
    feature = "(ANY)" if any_memory else ""
    caps.set_property("caps", Gst.Caps.from_string(
        f"video/x-raw{feature},width={w},height={h},framerate={FRAMERATE}/1"))
    # playbin feeds the visualiser from the audio tee: when it draws slower
    # than real time it would hold the sound back until playback stalls. This
    # queue drops the oldest audio for the picture instead.
    queue = Gst.ElementFactory.make("queue")
    queue.set_property("leaky", 2)                     # downstream: drop old buffers
    queue.set_property("max-size-buffers", 8)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("max-size-time", 0)
    queue.set_property("silent", True)
    for e in (queue, element, caps):
        bin_.add(e)
    queue.link(element)
    element.link(caps)
    bin_.add_pad(Gst.GhostPad.new("sink", queue.get_static_pad("sink")))
    bin_.add_pad(Gst.GhostPad.new("src", caps.get_static_pad("src")))
    return bin_


def register(api):
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        api.log("GStreamer is not available")
        return
    if not Gst.is_initialized():
        Gst.init(None)

    def settings():
        return api.app.settings if api.app is not None else {}

    if Gst.ElementFactory.find("projectm") is not None:
        def preset_root():
            return _first_dir([settings().get("projectm_preset_dir")] + PRESET_DIRS)

        def make_projectm(choice=None):
            el = Gst.ElementFactory.make("projectm")
            s = settings()
            presets = preset_root()
            if presets and choice and (Path(presets) / choice).is_dir():
                presets = str(Path(presets) / choice)   # just this folder, shuffled
            # gst-projectm calls the preset folder "preset" (older builds "preset-path")
            _set(el, ("preset", "preset-path"), presets)
            _set(el, ("texture-dir", "texture-dir-path"), _first_dir(TEXTURE_DIRS))
            _set(el, "preset-duration", float(s.get("projectm_preset_seconds", 20)))
            _set(el, "enable-playlist", True)
            _set(el, "shuffle-presets", True)
            res = s.get("visualizer_resolution", "auto")
            size = screen_size(AUTO_SIZE) if res in (None, "", "auto") else \
                fit(parse_size(res, DEFAULT_SIZE), MAX_SIZE)
            return sized(Gst, el, size, any_memory=True, isolate_gl=True)
        api.add_visualizer("projectM (MilkDrop)", element=make_projectm,
                           choices=lambda: preset_folders(preset_root()))
    else:
        api.log("projectM not found: install gst-projectm and libprojectM to use it")

    if Gst.ElementFactory.find("goom") is not None:
        def make_goom():
            size = parse_size(settings().get("goom_resolution"), GOOM_SIZE)
            return sized(Gst, Gst.ElementFactory.make("goom"), size)
        api.add_visualizer("GOOM", element=make_goom)
