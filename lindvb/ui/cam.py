"""CAM menu: the interactive menus of a CI module (EN 50221 MMI)."""

from gi.repository import GLib, Gtk

from .. import en50221
from .common import scrolled
from ..i18n import _


class _Bridge(en50221.CIListener):
    """Moves CI thread callbacks onto the GTK main loop."""

    def __init__(self, window):
        self.window = window

    def ci_info(self, stack):
        GLib.idle_add(self.window.refresh_info)

    def ci_menu(self, stack, menu):
        GLib.idle_add(self.window.show_menu, menu)

    def ci_enquiry(self, stack, enquiry):
        GLib.idle_add(self.window.show_enquiry, enquiry)

    def ci_close(self, stack):
        GLib.idle_add(self.window.show_closed)

    def ci_removed(self, stack):
        GLib.idle_add(self.window.show_closed, _("The module was removed or reset."))


class CamMenuWindow(Gtk.Window):
    """Shows the CI module's menus, lists and enquiries and sends the answers."""

    def __init__(self, app, parent):
        super().__init__(title=_("CAM Menu"), transient_for=parent, default_width=520,
                         default_height=480)
        self.app = app
        self.device = en50221.shared_device(app.settings)
        self.device.open()
        self._bridge = _Bridge(self)
        self.device.listeners.append(self._bridge)
        self._menu = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                        margin_end=12, margin_top=12, margin_bottom=12)
        self.info = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        outer.append(self.info)

        self.title_l = Gtk.Label(xalign=0, wrap=True, css_classes=["title-3"])
        self.subtitle_l = Gtk.Label(xalign=0, wrap=True)
        outer.append(self.title_l)
        outer.append(self.subtitle_l)

        self.stack = Gtk.Stack(vexpand=True)
        self.items = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.items.add_css_class("boxed-list")
        self.items.connect("row-activated", lambda _l, row: self._choose(row.get_index() + 1))
        self.stack.add_named(scrolled(self.items), "menu")

        enq = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.enq_text = Gtk.Label(xalign=0, wrap=True)
        self.enq_entry = Gtk.PasswordEntry(show_peek_icon=True)
        self.enq_plain = Gtk.Entry()
        for e in (self.enq_entry, self.enq_plain):
            e.connect("activate", lambda *_: self._answer(True))
        bar = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self._answer(False))
        ok = Gtk.Button(label=_("OK"), css_classes=["suggested-action"])
        ok.connect("clicked", lambda *_: self._answer(True))
        bar.append(cancel)
        bar.append(ok)
        for w in (self.enq_text, self.enq_entry, self.enq_plain, bar):
            enq.append(w)
        self.stack.add_named(enq, "enquiry")

        self.idle_l = Gtk.Label(wrap=True, css_classes=["dim-label"])
        self.stack.add_named(self.idle_l, "idle")
        outer.append(self.stack)

        self.bottom_l = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        outer.append(self.bottom_l)

        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self.open_b = Gtk.Button(label=_("Open CAM Menu"), css_classes=["suggested-action"])
        self.open_b.connect("clicked", lambda *_: self._enter())
        self.select_b = Gtk.Button(label=_("Select"))
        self.select_b.connect("clicked", lambda *_: self._choose_selected())
        self.back_b = Gtk.Button(label=_("Back"))
        self.back_b.connect("clicked", lambda *_: self._choose(0))
        self.close_b = Gtk.Button(label=_("Close Menu"))
        self.close_b.connect("clicked", lambda *_: self._close_mmi())
        for b in (self.open_b, self.select_b, self.back_b, self.close_b):
            buttons.append(b)
        outer.append(buttons)
        self.set_child(outer)

        self.connect("close-request", self._on_close)
        self.refresh_info()
        self.show_closed()
        self._timer = GLib.timeout_add_seconds(1, self._tick)

    # -- display -----------------------------------------------------------
    def _tick(self):
        self.refresh_info()
        return True

    def refresh_info(self):
        st = self.device.stack
        parts = [_("Device: {path} – {state}").format(path=self.device.path, state=self.device.state)]
        if st.module_name:
            parts.append(f"Module: {st.module_name}")
        if st.ca_system_ids:
            parts.append("CA systems: " + ", ".join(f"0x{i:04X}" for i in st.ca_system_ids))
        self.info.set_text("\n".join(parts))
        self.open_b.set_sensitive(bool(st.started))
        return False

    def _set_mode(self, mode):
        self.stack.set_visible_child_name(mode)
        in_menu = mode == "menu"
        self.select_b.set_visible(in_menu and self._menu is not None and not self._menu.is_list)
        self.back_b.set_visible(in_menu)
        self.close_b.set_visible(mode != "idle")
        self.open_b.set_visible(mode == "idle")

    def show_menu(self, menu):
        self._menu = menu
        self.title_l.set_text(menu.title)
        self.subtitle_l.set_text(menu.subtitle)
        self.subtitle_l.set_visible(bool(menu.subtitle))
        self.bottom_l.set_text(menu.bottom)
        self.items.remove_all()
        for item in menu.items:
            row = Gtk.ListBoxRow(activatable=not menu.is_list,
                                 selectable=not menu.is_list)
            row.set_child(Gtk.Label(label=item, xalign=0, wrap=True, margin_top=6,
                                    margin_bottom=6, margin_start=8, margin_end=8))
            self.items.append(row)
        self.back_b.set_label(_("OK") if menu.is_list else _("Back"))
        self._set_mode("menu")
        self.present()
        return False

    def show_enquiry(self, enquiry):
        self._menu = None
        self.title_l.set_text("")
        self.subtitle_l.set_visible(False)
        self.bottom_l.set_text("")
        self.enq_text.set_text(enquiry.text)
        self.enq_entry.set_visible(enquiry.blind)
        self.enq_plain.set_visible(not enquiry.blind)
        entry = self.enq_entry if enquiry.blind else self.enq_plain
        entry.set_text("")
        if isinstance(entry, Gtk.Entry) and enquiry.length:
            entry.set_max_length(enquiry.length)
        self._set_mode("enquiry")
        self.present()
        entry.grab_focus()
        return False

    def show_closed(self, note=""):
        self._menu = None
        self.title_l.set_text("")
        self.subtitle_l.set_visible(False)
        self.bottom_l.set_text("")
        self.items.remove_all()
        self.idle_l.set_text(note or _("Open the CAM menu to see the module's settings, "
                             "smart card information and parental control."))
        self._set_mode("idle")
        return False

    # -- actions -----------------------------------------------------------
    def _enter(self):
        if not self.device.enter_menu():
            self.idle_l.set_text(_("The module has not finished starting yet."))

    def _choose_selected(self):
        row = self.items.get_selected_row()
        if row is not None:
            self._choose(row.get_index() + 1)

    def _choose(self, n):
        if self._menu is not None and self._menu.is_list:
            n = 0
        self.device.answer_menu(n)

    def _answer(self, ok):
        entry = self.enq_entry if self.enq_entry.get_visible() else self.enq_plain
        self.device.answer_enquiry(entry.get_text() if ok else None)
        entry.set_text("")

    def _close_mmi(self):
        self.device.close_mmi()
        self.show_closed()

    def _on_close(self, *_):
        GLib.source_remove(self._timer)
        if self._bridge in self.device.listeners:
            self.device.listeners.remove(self._bridge)
        return False
