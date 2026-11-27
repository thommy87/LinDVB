# SPDX-License-Identifier: GPL-2.0-or-later
"""How the window shows closed captions: rows as on a TV, in place (WJXT,
27.11.2026: centred and wrapped, roll-up captions jumped about with every
two letters)."""

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gtk, Pango
except (ValueError, ImportError):
    pytest.skip("GTK 4 is not available", allow_module_level=True)
if not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from lindvb.ui.window import CAPTION_COLUMNS, caption_label, caption_markup  # noqa: E402


def test_markup_puts_each_row_on_its_own_background():
    m = caption_markup("A <B> & C\nNEXT")
    assert m == ('<span background="#000000" bgalpha="75%"> A &lt;B&gt; &amp; C </span>\n'
                 '<span background="#000000" bgalpha="75%"> NEXT </span>')
    assert caption_markup("") == ""
    Pango.parse_markup(m, -1, "\0")                  # valid markup


def _width(label, text):
    label.set_markup(caption_markup(text))
    return label.measure(Gtk.Orientation.HORIZONTAL, -1)[1]


def test_roll_up_rows_grow_in_place():
    """A row that grows two letters at a time keeps the block where it is:
    the label is as wide as a full row from the start, left-aligned, and
    never wraps; a second row moves the first up."""
    label = caption_label()
    label.set_visible(True)
    assert label.get_xalign() == 0 and not label.get_wrap()
    assert label.get_width_chars() == CAPTION_COLUMNS == 32
    widths = {_width(label, "LIVE CAPTIONS"[:n]) for n in range(2, 14, 2)}
    assert len(widths) == 1 and min(widths) > 0, widths   # the same width: nothing moves sideways
    rows = label.measure(Gtk.Orientation.VERTICAL, -1)[1]
    _width(label, "LIVE CAPTIONS\nGROW IN PLACE")
    assert label.measure(Gtk.Orientation.VERTICAL, -1)[1] > rows
    long = "X" * 40                                   # wider than 32 columns: one row, not wrapped
    _width(label, long)
    layout = label.get_layout()
    assert layout.get_line_count() == 1
