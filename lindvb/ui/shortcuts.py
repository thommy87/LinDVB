# SPDX-License-Identifier: GPL-2.0-or-later
"""Keyboard shortcuts window (Ctrl+?): every key of the main window, by topic."""

from gi.repository import Gtk

from ..i18n import _

# (topic, [(keys, what it does)]); keys as GTK accelerator names, several
# alternatives separated by spaces
SHORTCUTS = [
    (_("Playback"), [
        ("space", _("Pause / play")),
        ("Left Right", _("Back / forward 10 seconds (timeshift, files)")),
        ("l", _("Back to live")),
        ("m", _("Mute")),
        ("plus minus", _("Volume up / down")),
        ("j k", _("Sound earlier / later (audio delay of the channel)")),
        ("v", _("Next radio visualisation")),
        ("n p", _("Next / previous playlist entry")),
    ]),
    (_("Channels"), [
        ("Up Down", _("Previous / next channel")),
        ("Page_Up Page_Down", _("Next / previous channel")),
        ("0 9", _("Channel by number (type the digits)")),
        ("BackSpace <Ctrl>BackSpace", _("Last channel")),
        ("<Ctrl>m", _("Switch TV / radio")),
        ("F9", _("Show / hide the channel list")),
    ]),
    (_("Programme guide and recording"), [
        ("e <Ctrl>e", _("Programme guide (EPG)")),
        ("r <Ctrl>r", _("Record")),
        ("<Ctrl>t", _("Timer recordings")),
        ("i o", _("Programme info (OSD)")),
    ]),
    (_("Teletext and interactive services"), [
        ("t <Ctrl><Shift>t", _("Teletext")),
        ("F5", _("HbbTV (red button)")),
    ]),
    (_("View"), [
        ("f F11", _("Full screen")),
        ("Escape", _("Leave full screen")),
        ("s <Ctrl><Shift>p", _("Screenshot")),
    ]),
    (_("Application"), [
        ("F10", _("Main menu")),
        ("<Ctrl>o", _("Open media file")),
        ("<Ctrl>u", _("Open URL")),
        ("<Ctrl><Shift>s", _("Channel search")),
        ("<Ctrl><Shift>e", _("Channel editor")),
        ("<Ctrl>comma", _("Preferences")),
        ("<Ctrl>question", _("Keyboard shortcuts")),
    ]),
]


def key_label(accel):
    """'<Ctrl><Shift>t' -> 'Ctrl+Shift+T', in the desktop's own key names."""
    ok, key, mods = Gtk.accelerator_parse(accel)
    if not ok:
        return accel
    return Gtk.accelerator_get_label(key, mods)


class ShortcutsWindow(Gtk.Window):
    def __init__(self, parent):
        super().__init__(title=_("Keyboard Shortcuts"), transient_for=parent, modal=False,
                         default_width=760, default_height=620)
        self.add_css_class("lindvb")
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2,
                           min_children_per_line=1, column_spacing=24, row_spacing=12,
                           homogeneous=False, valign=Gtk.Align.START,
                           margin_start=20, margin_end=20, margin_top=16, margin_bottom=20)
        for topic, keys in SHORTCUTS:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.append(Gtk.Label(label=topic, xalign=0, css_classes=["lv-section-title"]))
            g = Gtk.Grid(column_spacing=16, row_spacing=6, css_classes=["lv-group"])
            for row, (accels, text) in enumerate(keys):
                caps = Gtk.Box(spacing=4, margin_start=10, margin_top=4, margin_bottom=4,
                               width_request=200)
                parts = accels.split()
                if parts == ["0", "9"]:
                    parts = ["0", "…", "9"]
                for a in parts:
                    caps.append(Gtk.Label(label=a if a == "…" else key_label(a),
                                          css_classes=[] if a == "…" else ["lv-keycap"]))
                g.attach(caps, 0, row, 1, 1)
                g.attach(Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True,
                                   margin_end=10), 1, row, 1, 1)
            box.append(g)
            grid.append(box)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=grid)
        self.set_child(sw)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, *_: kv == 0xff1b and (self.close() or True))
        self.add_controller(keys)
