# SPDX-License-Identifier: GPL-2.0-or-later
"""The programme details of the EPG show what the programme offers (black
and white, 5.1, original language, clear speech ...) with its symbol."""

import time
import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from windowsmoke import headless_app  # noqa: E402

from lindvb import progfeatures as pf  # noqa: E402
from lindvb.channels import Channel  # noqa: E402
from lindvb.contentguide import SIGN_LANGUAGE  # noqa: E402
from lindvb.epg import Event  # noqa: E402
from lindvb.i18n import _  # noqa: E402


def _chips(box):
    out = []
    child = box.get_first_child()
    while child is not None:
        chip = child.get_child()
        if isinstance(chip, Gtk.Label):
            out.append((None, chip.get_label()))
        else:
            icon = chip.get_first_child()
            name = icon.get_icon_name() if isinstance(icon, Gtk.Image) else None
            out.append((name, chip.get_last_child().get_label()))
        child = child.get_next_sibling()
    return out


def test_details_show_the_features_with_their_symbols():
    from lindvb.ui.epg import EPGWindow
    app = headless_app()
    app.reminders = types.SimpleNamespace(has=lambda *a: False)
    win = EPGWindow(app, None)
    try:
        ch = Channel(name="Das Erste HD", delsys="dvb-s2", frequency=11494000, polarity="h", sid=1)
        ev = Event(int(time.time()), 5400, "Casablanca", genre="Spielfilm", bw=True,
                   access=[SIGN_LANGUAGE], features=[pf.HD, pf.STEREO, pf.SURROUND, pf.ORIGINAL,
                                                     pf.CLEAR_SPEECH])
        win._select((ch, ev))
        chips = _chips(win.chips)
        assert (None, "Das Erste HD") in chips and (None, "Spielfilm") in chips
        features = chips[-6:]
        assert features == [(pf.ICONS.get(f), _(pf.LABELS[f]))
                            for f in (pf.BW, pf.HD, pf.SURROUND, pf.ORIGINAL, pf.CLEAR_SPEECH, pf.SIGN)]
        assert features[3][0] == "lindvb-original-language-symbolic"
        assert features[4][0] == "lindvb-clear-speech-symbolic"
        # the sign language of the DVB-I guide is no second chip
        sign = _(pf.LABELS[pf.SIGN])
        assert [c for c in chips if c[1] == sign] == [(None, sign)]
        # a programme without any: the facts alone
        win._select((ch, Event(int(time.time()), 600, "Nachrichten")))
        assert all(name is None for name, _label in _chips(win.chips))
    finally:
        win.destroy()
