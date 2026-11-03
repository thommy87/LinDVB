"""GStreamer visualisations for radio: projectM (MilkDrop presets) and GOOM.

projectM needs the gst-projectm GStreamer plugin (element "projectm", built
on libprojectM 4) and a preset collection, for example from your distribution's
projectm / projectm-presets packages or from
https://github.com/projectM-visualizer. Without it this add-on only offers
GOOM (from gst-plugins-good) and LinDVB's own visualisations stay available.
The preset folder can be set as "projectm_preset_dir" in settings.json.

GStreamer visualisers render at 320x240 unless told otherwise, which looks
blocky in full screen. projectM draws with OpenGL, so it is asked for the
screen's resolution (at most 2560x1440, or "visualizer_resolution" in
settings.json). The original GOOM runs on the CPU and slows down a lot at
high resolutions, so it uses "goom_resolution" (default 640x360, 16:9);
LinDVB's own "Goom HD (GPU)" renders goom's effects at full resolution.
"""

import re
import sys
from pathlib import Path

NAME = "GStreamer visualisations (projectM, GOOM)"
VERSION = "1.2"

# the AppImage brings its own collection under its prefix (sys.prefix/share)
PRESET_DIRS = [str(Path(sys.prefix) / "share/projectM/presets"), "/usr/share/projectM/presets", "/usr/local/share/projectM/presets",
               "/usr/share/projectm-presets", "/app/share/projectM/presets",
               "~/.local/share/projectM/presets"]
TEXTURE_DIRS = [str(Path(sys.prefix) / "share/projectM/textures"), "/usr/share/projectM/textures", "/usr/local/share/projectM/textures",
                "~/.local/share/projectM/textures"]
MAX_SIZE = (2560, 1440)
DEFAULT_SIZE = (1920, 1080)
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


def sized(Gst, element, size, any_memory=False):
    """Wrap a visualiser so it renders at ``size`` instead of its default 320x240."""
    w, h = size
    bin_ = Gst.Bin.new(None)
    caps = Gst.ElementFactory.make("capsfilter")
    feature = "(ANY)" if any_memory else ""
    caps.set_property("caps", Gst.Caps.from_string(
        f"video/x-raw{feature},width={w},height={h},framerate={FRAMERATE}/1"))
    bin_.add(element)
    bin_.add(caps)
    element.link(caps)
    bin_.add_pad(Gst.GhostPad.new("sink", element.get_static_pad("sink")))
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
        def make_projectm():
            el = Gst.ElementFactory.make("projectm")
            s = settings()
            presets = _first_dir([s.get("projectm_preset_dir")] + PRESET_DIRS)
            # gst-projectm calls the preset folder "preset" (older builds "preset-path")
            _set(el, ("preset", "preset-path"), presets)
            _set(el, ("texture-dir", "texture-dir-path"), _first_dir(TEXTURE_DIRS))
            _set(el, "preset-duration", float(s.get("projectm_preset_seconds", 20)))
            _set(el, "enable-playlist", True)
            _set(el, "shuffle-presets", True)
            res = s.get("visualizer_resolution", "auto")
            size = screen_size() if res in (None, "", "auto") else parse_size(res, DEFAULT_SIZE)
            return sized(Gst, el, size, any_memory=True)
        api.add_visualizer("projectM (MilkDrop)", element=make_projectm)
    else:
        api.log("projectM not found: install gst-projectm and libprojectM to use it")

    if Gst.ElementFactory.find("goom") is not None:
        def make_goom():
            size = parse_size(settings().get("goom_resolution"), GOOM_SIZE)
            return sized(Gst, Gst.ElementFactory.make("goom"), size)
        api.add_visualizer("GOOM", element=make_goom)
