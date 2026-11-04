# SPDX-License-Identifier: GPL-2.0-or-later
"""A small plugin system for LinDVB.

The design follows DVBViewer's idea of optional add-ons that extend the
application without being part of its core: channel-list importers, conditional
access providers for the user's own encrypted channels, and general hooks into
the application life cycle.

A plugin is a Python module placed in the plugins directory (by default
``$XDG_DATA_HOME/lindvb/plugins``; extra directories can be listed in the
``LINDVB_PLUGIN_PATH`` environment variable). The module must expose a
``register(api)`` function that calls back into the :class:`PluginAPI` it is
given to announce what it provides. For example::

    NAME = "My importer"
    VERSION = "1.0"

    def register(api):
        api.add_importer("Foo lists (*.foo)", ".foo", load_foo)

Plugins are ordinary Python code running in the same process, so only install
plugins you trust, exactly as with any other software add-on.
"""

import importlib.util
import os
import sys
import traceback
from pathlib import Path

from .config import PLUGIN_DIR

# Add-ons shipped with LinDVB. They load like user plugins (and can be switched
# off the same way); a user plugin with the same name takes precedence.
BUNDLED_DIR = Path(__file__).resolve().parent / "addons"


class PluginError(Exception):
    pass


class LoadedPlugin:
    """One plugin module and what it registered."""

    def __init__(self, name, module, path):
        self.name = name
        self.module = module
        self.path = path
        self.version = getattr(module, "VERSION", "")
        self.description = (getattr(module, "__doc__", "") or "").strip()
        self.error = None


class PluginAPI:
    """Handed to each plugin's ``register`` function.

    It is the only surface a plugin is expected to use, which keeps plugins
    insulated from the rest of the code base.
    """

    def __init__(self, manager, plugin):
        self._manager = manager
        self._plugin = plugin

    @property
    def app(self):
        """The running :class:`~lindvb.app.LinDVBApp`, or ``None`` at import time."""
        return self._manager.app

    def log(self, message):
        self._manager.log(f"[{self._plugin.name}] {message}")

    # -- extension points -----------------------------------------------------
    def add_importer(self, label, extension, loader):
        """Register a channel-list importer.

        ``label``   file-chooser label, e.g. ``"Foo lists (*.foo)"``
        ``extension`` file suffix including the dot, e.g. ``".foo"``
        ``loader``  ``loader(path) -> list[Channel]``
        """
        self._manager.importers.append((self._plugin.name, label, extension, loader))

    def add_ca_provider(self, name, factory):
        """Register a conditional-access provider.

        ``factory(settings) -> CAProvider`` builds a provider that can supply
        control words for the user's own encrypted channels (see
        :mod:`lindvb.ca`). ``name`` is what the user selects as ``ca_provider``.
        """
        self._manager.ca_providers[name] = (self._plugin.name, factory)

    def add_hook(self, event, callback):
        """Register a life-cycle hook.

        Known events: ``"startup"`` (app built), ``"channel-tuned"``
        (``callback(channel, capture)``), ``"shutdown"``.
        """
        self._manager.hooks.setdefault(event, []).append((self._plugin.name, callback))

    def add_menu_action(self, label, callback):
        """Add an entry to the Plugins menu. ``callback(app)``."""
        self._manager.menu_actions.append((self._plugin.name, label, callback))

    def add_visualizer(self, name, draw=None, element=None, choices=None):
        """Register a visualisation for radio and other audio-only playback.

        Give exactly one of:

        ``draw``    ``draw() -> style``: an object with a
                    ``draw(cr, width, height, frame)`` method that paints one
                    picture with cairo from a :class:`lindvb.audioviz.Frame`
                    (spectrum bands, levels, waveform, beat). Called anew each
                    time the user picks the visualisation.
        ``element`` ``element() -> Gst.Element``: a GStreamer audio
                    visualiser (audio in, video out, e.g. ``projectm`` or
                    ``goom``) that playbin renders into the video area.

        ``name`` is shown in the View → Visualisation menu.

        ``choices`` (element visualisers only), ``choices() -> [str]``: variants
        the user can pick from a submenu, e.g. preset folders. A "/" in a choice
        nests it under its parent ("Dancer", "Dancer/Aurora"). The picked choice
        is passed as ``element(choice)``; "all" of them calls ``element()``.
        """
        if (draw is None) == (element is None):
            raise PluginError("add_visualizer needs exactly one of draw= or element=")
        kind, factory = ("draw", draw) if draw is not None else ("element", element)
        if choices is not None and kind != "element":
            raise PluginError("add_visualizer: choices= only works with element=")
        self._manager.visualizers.append((self._plugin.name, name, kind, factory))
        if choices is not None:
            self._manager.visualizer_choices[name] = choices


class PluginManager:
    """Discovers, loads and holds the plugins, and dispatches their hooks."""

    def __init__(self, settings, log=None):
        self.settings = settings
        self.app = None
        self._log = log or (lambda m: print(m, file=sys.stderr))
        self.plugins: list[LoadedPlugin] = []
        self.importers = []                 # (plugin, label, ext, loader)
        self.ca_providers = {}              # name -> (plugin, factory)
        self.hooks = {}                     # event -> [(plugin, callback)]
        self.menu_actions = []              # (plugin, label, callback)
        self.visualizers = []               # (plugin, name, "draw"|"element", factory)
        self.visualizer_choices = {}        # name -> choices() of an element visualiser
        self.failed = {}                    # module name -> error text

    def log(self, message):
        self._log(message)

    # -- discovery ------------------------------------------------------------
    def plugin_dirs(self):
        dirs = [PLUGIN_DIR]
        extra = os.environ.get("LINDVB_PLUGIN_PATH", "")
        dirs += [Path(p) for p in extra.split(os.pathsep) if p]
        if not os.environ.get("LINDVB_NO_BUNDLED_PLUGINS"):
            dirs.append(BUNDLED_DIR)
        seen, out = set(), []
        for d in dirs:
            d = d.expanduser()
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

    def user_dirs(self):
        """The folders the user installs plugins into: the plugins directory and any
        ``LINDVB_PLUGIN_PATH`` entries, without the add-ons built into LinDVB."""
        bundled = BUNDLED_DIR.expanduser()
        return [d for d in self.plugin_dirs() if d != bundled]

    def ensure_plugin_dir(self):
        """Create the user plugins directory if needed; return it."""
        try:
            PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.log(f"Cannot create plugin folder {PLUGIN_DIR}: {e}")
        return PLUGIN_DIR

    def discover(self):
        """Return candidate ``(name, path)`` plugin modules, without loading them.

        A plugin is either ``<dir>/<name>.py`` or ``<dir>/<name>/__init__.py``.
        """
        found = {}
        for d in self.plugin_dirs():
            if not d.is_dir():
                continue
            for entry in sorted(d.iterdir()):
                if entry.name.startswith((".", "_")):
                    continue
                if entry.is_file() and entry.suffix == ".py":
                    found.setdefault(entry.stem, entry)
                elif entry.is_dir() and (entry / "__init__.py").is_file():
                    found.setdefault(entry.name, entry / "__init__.py")
        return list(found.items())

    def load_all(self):
        self.ensure_plugin_dir()
        if not self.settings.get("plugins_enabled", True):
            return
        disabled = set(self.settings.get("plugins_disabled", []))
        for name, path in self.discover():
            if name in disabled:
                continue
            try:
                self.load(name, path)
            except Exception as e:  # a broken plugin must not stop the app
                self.failed[name] = str(e) or type(e).__name__
                self.log(f"Plugin '{name}' failed to load: {e}")
                self.log(traceback.format_exc())

    def load(self, name, path):
        mod_name = f"lindvb_plugin_{name}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"cannot import {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        plugin = LoadedPlugin(getattr(module, "NAME", name), module, path)
        plugin.module_name = name
        register = getattr(module, "register", None)
        if not callable(register):
            raise PluginError("missing register(api) function")
        register(PluginAPI(self, plugin))
        self.plugins.append(plugin)
        self.log(f"Loaded plugin '{plugin.name}' {plugin.version}".rstrip())
        return plugin

    def set_enabled(self, module_name, enabled):
        """Switch a plugin on or off for the next start (stored in the settings)."""
        disabled = [n for n in self.settings.get("plugins_disabled", []) if n != module_name]
        if not enabled:
            disabled.append(module_name)
        self.settings["plugins_disabled"] = disabled

    def is_enabled(self, module_name):
        return module_name not in self.settings.get("plugins_disabled", [])

    # -- dispatch -------------------------------------------------------------
    def bind_app(self, app):
        self.app = app

    def emit(self, event, *args):
        """Call every hook registered for ``event``; never raise to the caller.
        Also starts the user's scripts for the event (see lindvb.hooks)."""
        from . import hooks
        try:
            hooks.run(event, args)
        except Exception as e:      # noqa: BLE001 - a script must not break the app
            self.log(f"Script hook '{event}' failed: {e}")
        for plugin_name, cb in self.hooks.get(event, []):
            try:
                cb(*args)
            except Exception as e:
                self.log(f"Plugin '{plugin_name}' hook '{event}' failed: {e}")

    def importer_for(self, path):
        """Return a plugin loader whose extension matches ``path``, or ``None``."""
        suffix = Path(path).suffix.lower()
        for _plugin, _label, ext, loader in self.importers:
            if ext.lower() == suffix:
                return loader
        return None

    def make_ca_provider(self):
        """Build the configured CA provider, or ``None``.

        Besides the plugin-registered providers this also serves the built-in
        ``"dvbapi"`` and ``"ci"`` providers from :mod:`lindvb.ca`.
        """
        name = self.settings.get("ca_provider", "none")
        if not name or name == "none":
            return None
        if name in self.ca_providers:
            _plugin, factory = self.ca_providers[name]
            return factory(self.settings)
        from . import ca
        return ca.builtin_provider(name, self.settings)
