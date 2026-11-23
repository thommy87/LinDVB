# SPDX-License-Identifier: GPL-2.0-or-later
"""Builds LinDVB's windows with a headless app and prints OK or the error.

tests/test_windows_open.py calls main() in a subprocess with its own HOME and
XDG folders: the settings, channel list and timers are read at import time,
and nothing of the user's may be read or written. No command line: the
windows to build are named in the call.
"""

import traceback
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gst, Gtk  # noqa: E402

def _window(module, cls):
    def make(app):
        import importlib
        return getattr(importlib.import_module(module), cls)(app, None)
    return make


WINDOWS = {
    "prefs": _window("lindvb.ui.dialogs", "PrefsWindow"),
    "editor": _window("lindvb.ui.dialogs", "EditorWindow"),
    "stats": _window("lindvb.ui.dialogs", "StatsWindow"),
    "plugins": _window("lindvb.ui.dialogs", "PluginsWindow"),
    "scan": _window("lindvb.ui.scan", "ScanWindow"),
    "tune": _window("lindvb.ui.tune", "TuneWindow"),
    "timers": _window("lindvb.ui.timers", "TimersWindow"),
    "epg": _window("lindvb.ui.epg", "EPGWindow"),
    "export": _window("lindvb.ui.export", "ExportWindow"),
    "favourites": _window("lindvb.ui.favourites", "FavouritesWindow"),
    "xmltvmatch": _window("lindvb.ui.xmltvmatch", "XMLTVMatchWindow"),
}


def headless_app():
    """What the windows reach through app (lindvb.app.App,
    do_handle_local_options and do_startup)."""
    from lindvb.channels import ChannelDB
    from lindvb.config import Settings
    from lindvb.epg import EPGStore
    from lindvb.favourites import FavouriteLists
    from lindvb.plugins import PluginManager
    from lindvb.recorder import TimerManager
    from lindvb.tuner import TunerManager
    settings = Settings()
    app = types.SimpleNamespace(settings=settings, window=None, _dvbi_busy=False)
    app.plugins = PluginManager(settings)
    app.db = ChannelDB()
    app.favs = FavouriteLists(app.db)
    app.epg = EPGStore(persist=False)
    app.tuners = TunerManager(settings, ca_factory=app.plugins.make_ca_provider)
    app.timers = TimerManager(app)
    return app


def main(names):
    Gst.init(None)
    if not Gtk.init_check():
        print("SKIP no display")
        return 0
    app = headless_app()
    failed = 0
    for name in names:
        try:
            win = WINDOWS[name](app)
            # every page of a window with pages, as a click through the sidebar does
            stack = getattr(win, "stack", None)
            if isinstance(stack, Gtk.Stack):
                for page in stack.get_pages():
                    stack.set_visible_child_name(page.get_name())
            win.destroy()
            print("OK", name)
        except Exception:   # noqa: BLE001  (any error is the finding)
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    return 1 if failed else 0
