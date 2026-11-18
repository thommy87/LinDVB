# SPDX-License-Identifier: GPL-2.0-or-later
"""A numbered step of the channel list filter (lindvb.listfilter): a button
with the step's number and a summary of its choices, opening a popover with
sections of ticks (any number, children indented below their parent) or of
radio entries (one)."""

from dataclasses import dataclass

from gi.repository import GLib, Gtk, Pango

from ..listfilter import tick, tick_state


@dataclass(frozen=True)
class Section:
    sid: str                         # the step's name for it ("sources", "reception" …)
    heading: str                     # "" for none
    items: tuple                     # ((key, label[, parent key]), …)
    radio: bool = False              # one entry (the value is a key), else ticks (a frozenset)


class StepMenu(Gtk.MenuButton):
    """``on_change(section, value)`` is called after the user changed a section:
    *value* is the frozenset of keys ticked, or the key of a radio section."""

    def __init__(self, number, name, on_change):
        super().__init__(hexpand=True, css_classes=["lv-step"], tooltip_text=name)
        self._on_change = on_change
        self._sections = ()
        self._buttons = {}           # sid -> {key: CheckButton}
        self._values = {}            # sid -> the value shown
        self._sync = False
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Label(label=str(number), css_classes=["lv-step-num"],
                             valign=Gtk.Align.CENTER))
        self._summary = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        box.append(self._summary)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [name])
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                                margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                propagate_natural_height=True, propagate_natural_width=True,
                                max_content_height=440, min_content_width=220)
        sw.set_child(self._content)
        self.set_popover(Gtk.Popover(child=sw, css_classes=["lv-step-pop"]))

    def set_summary(self, text):
        self._summary.set_text(text)

    def set_sections(self, sections, values):
        """Show *sections* with the choices *values* ({sid: value}). The
        entries are built again only when they changed, so a step can be
        updated while its popover is open."""
        sections = tuple(sections)
        if sections != self._sections:
            self._build(sections)
        self._values = {s.sid: values.get(s.sid) for s in sections}
        self._sync = True
        for s in sections:
            v = self._values[s.sid]
            for key, b in self._buttons[s.sid].items():
                if s.radio:
                    b.set_active(key == v)
                    continue
                state = tick_state(s.items, v or frozenset(), key)
                b.set_active(state == "on")
                b.set_inconsistent(state == "part")
        self._sync = False

    def _build(self, sections):
        self._sections = sections
        self._buttons = {}
        while (child := self._content.get_first_child()) is not None:
            self._content.remove(child)
        for n, s in enumerate(sections):
            if s.heading:
                self._content.append(Gtk.Label(label=s.heading, xalign=0,
                                               css_classes=["lv-step-head"] +
                                               (["lv-step-first"] if n == 0 else [])))
            group, buttons = None, {}
            for key, label, *parent in s.items:
                b = Gtk.CheckButton(label=label, margin_start=22 if any(parent) else 0)
                if s.radio:
                    if group is None:
                        group = b
                    else:
                        b.set_group(group)
                b.connect("toggled", self._toggled, s, key)
                self._content.append(b)
                buttons[key] = b
            self._buttons[s.sid] = buttons

    def _toggled(self, button, section, key):
        if self._sync or section.radio and not button.get_active():
            return
        if section.radio:
            value = key
        else:
            value = tick(section.items, self._values.get(section.sid) or frozenset(), key,
                         button.get_active())
        self._values[section.sid] = value
        # after the toggle: the change may build this popover's entries again
        GLib.idle_add(lambda: self._on_change(section, value) and False)
