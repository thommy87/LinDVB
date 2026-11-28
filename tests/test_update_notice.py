# SPDX-License-Identifier: GPL-2.0-or-later
"""The notice of a newer release in the status bar and the About dialog's
lines about updates (issue #206)."""

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from lindvb import updatecheck as uc  # noqa: E402
from lindvb.i18n import _  # noqa: E402
from lindvb.ui import window as W  # noqa: E402

MainWindow = W.MainWindow


def _win(**settings):
    defaults = dict(update_check=True, update_checked_at=0, update_latest="", update_url="",
                    update_hidden="")
    me = types.SimpleNamespace(settings={**defaults, **settings}, _update=None)
    me.update_box = Gtk.Box(visible=False)
    me.update_btn = Gtk.Button()
    for name in ("_show_update", "_hide_update", "_update_lines"):
        setattr(me, name, getattr(MainWindow, name).__get__(me))
    return me


def test_notice_until_closed_and_not_again_for_that_version():
    me = _win()
    me._show_update("99.0", uc.RELEASES + "/tag/v99.0")
    assert me.update_box.get_visible()
    assert "99.0" in me.update_btn.get_label()
    me._hide_update()
    assert not me.update_box.get_visible() and me.settings["update_hidden"] == "99.0"
    me._show_update("99.0", uc.RELEASES)
    assert not me.update_box.get_visible()
    # the next version shows again
    me._show_update("99.1", uc.RELEASES)
    assert me.update_box.get_visible()


def test_about_lines():
    release = {"package": "appimage", "channel": "release", "checks": True}
    me = _win()
    assert me._update_lines(release) == []                       # not asked yet
    me.settings["update_checked_at"] = 1_700_000_000
    assert me._update_lines(release)[0].startswith(_("Up to date (checked {date})")[:5])
    me.settings.update(update_latest="99.0", update_url=uc.RELEASES + "/tag/v99.0")
    line = me._update_lines(release)[0]
    assert "99.0" in line and line.endswith(" " + uc.RELEASES + "/tag/v99.0")
    me = _win(update_check=False)
    assert me._update_lines(release) == [_("The check for new versions is off "
                                           "(Preferences > General)")]
    snap = uc.install({"SNAP": "/snap/x", "SNAP_REVISION": "81"}, {})
    assert me._update_lines(snap) == [_("Updated by the package manager")]
    assert me._update_lines(uc.install({}, {})) == []            # a source checkout
