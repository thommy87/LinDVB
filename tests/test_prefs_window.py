# SPDX-License-Identifier: GPL-2.0-or-later
"""The preferences window opens with every page (0.22.0 could not open it:
the timeshift rows used a helper they were not given)."""

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from lindvb.channels import ChannelDB  # noqa: E402
from lindvb.config import Settings  # noqa: E402
from lindvb.favourites import FavouriteLists  # noqa: E402
from lindvb.plugins import PluginManager  # noqa: E402
from lindvb.ui.dialogs import PrefsWindow  # noqa: E402


@pytest.fixture
def prefs():
    # what the pages read of the app (lindvb.app.App.do_handle_local_options)
    settings = Settings()
    db = ChannelDB()
    app = types.SimpleNamespace(settings=settings, db=db, favs=FavouriteLists(db),
                                plugins=PluginManager(settings), _dvbi_busy=False)
    win = PrefsWindow(app, None)
    yield win
    win.destroy()


def test_the_preferences_open_with_every_page(prefs):
    pages = [p.get_name() for p in prefs.stack.get_pages()]
    assert len(pages) > 5
    for name in pages:
        prefs.stack.set_visible_child_name(name)


@pytest.mark.parametrize("key", ["recording_dir", "screenshot_dir", "timeshift_dir"])
def test_folder_settings_have_a_folder_chooser(prefs, key):
    ent = prefs.w[key][0]
    assert ent.get_text()
    buttons = []
    child = ent.get_parent().get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            buttons.append(child.get_icon_name())
        child = child.get_next_sibling()
    assert "folder-open-symbolic" in buttons
