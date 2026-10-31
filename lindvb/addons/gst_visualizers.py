"""GStreamer visualisations for radio: projectM (MilkDrop presets) and GOOM.

projectM needs the gst-projectm GStreamer plugin (element "projectm", built
on libprojectM 4) and a preset collection, for example from your distribution's
projectm / projectm-presets packages or from
https://github.com/projectM-visualizer. Without it this add-on only offers
GOOM (from gst-plugins-good) and LinDVB's own visualisations stay available.
The preset folder can be set as "projectm_preset_dir" in settings.json.
"""

from pathlib import Path

NAME = "GStreamer visualisations (projectM, GOOM)"
VERSION = "1.0"

PRESET_DIRS = ["/usr/share/projectM/presets", "/usr/local/share/projectM/presets",
               "/usr/share/projectm-presets", "/app/share/projectM/presets",
               "~/.local/share/projectM/presets"]
TEXTURE_DIRS = ["/usr/share/projectM/textures", "/usr/local/share/projectM/textures",
                "~/.local/share/projectM/textures"]


def _first_dir(candidates):
    for d in candidates:
        if d and Path(d).expanduser().is_dir():
            return str(Path(d).expanduser())
    return None


def _set(element, prop, value):
    """Set a property if this projectM build has it (they differ by version)."""
    if value is None or element.find_property(prop) is None:
        return
    try:
        element.set_property(prop, value)
    except (TypeError, ValueError, OverflowError):
        pass


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

    if Gst.ElementFactory.find("projectm") is not None:
        def make_projectm():
            el = Gst.ElementFactory.make("projectm")
            settings = api.app.settings if api.app is not None else {}
            presets = _first_dir([settings.get("projectm_preset_dir")] + PRESET_DIRS)
            _set(el, "preset-path", presets)
            _set(el, "texture-dir-path", _first_dir(TEXTURE_DIRS))
            _set(el, "preset-duration", float(settings.get("projectm_preset_seconds", 20)))
            _set(el, "shuffle-presets", True)
            return el
        api.add_visualizer("projectM (MilkDrop)", element=make_projectm)
    else:
        api.log("projectM not found: install gst-projectm and libprojectM to use it")

    if Gst.ElementFactory.find("goom") is not None:
        api.add_visualizer("GOOM", element=lambda: Gst.ElementFactory.make("goom"))
