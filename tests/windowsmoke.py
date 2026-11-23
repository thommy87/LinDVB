# SPDX-License-Identifier: GPL-2.0-or-later
"""Builds one of LinDVB's windows with a headless app and prints OK or the error.

Run by tests/test_windows_open.py in a subprocess with its own HOME and XDG
folders: the settings, channel list and timers are read at import time, and
nothing of the user's may be read or written.

    python3 tests/windowsmoke.py prefs
"""

import importlib
import sys
import traceback
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gst, Gtk  # noqa: E402

# what the windows reach through app (lindvb.app.App, do_handle_local_options and do_startup)
WINDOWS = {
    "prefs": ("lindvb.ui.dialogs", "PrefsWindow"),
    "editor": ("lindvb.ui.dialogs", "EditorWindow"),
    "stats": ("lindvb.ui.dialogs", "StatsWindow"),
    "plugins": ("lindvb.ui.dialogs", "PluginsWindow"),
    "scan": ("lindvb.ui.scan", "ScanWindow"),
    "tune": ("lindvb.ui.tune", "TuneWindow"),
    "timers": ("lindvb.ui.timers", "TimersWindow"),
    "epg": ("lindvb.ui.epg", "EPGWindow"),
    "export": ("lindvb.ui.export", "ExportWindow"),
    "favourites": ("lindvb.ui.favourites", "FavouritesWindow"),
    "xmltvmatch": ("lindvb.ui.xmltvmatch", "XMLTVMatchWindow"),
}


def headless_app():
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
        module, cls = WINDOWS[name]
        try:
            win = getattr(importlib.import_module(module), cls)(app, None)
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or list(WINDOWS)))
