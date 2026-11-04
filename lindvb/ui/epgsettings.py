# SPDX-License-Identifier: GPL-2.0-or-later
"""The "Programme guide" page of the preferences: saving, language, info on
channel change, time zone, XMLTV, the MediaHighway / Freesat transponders
(modelled on DVBViewer's EPG options) and, under "Advanced", the priority of
the EPG sources (lindvb.epgmerge)."""

import copy

from gi.repository import Gtk

from ..epg import apply_timezone
from ..epgmerge import DEFAULT_ORDER, SOURCE_LABELS, normalize_order
from ..epgsources import DEFAULT_PROVIDER_FREQS, LANGUAGES, PROVIDER_TYPES
from .common import dropdown, fill, make_column_view, message, selected
from ..i18n import _

POLS = ["h", "v"]
KINDS = list(PROVIDER_TYPES)


def _fmt_offset(minutes):
    sign = "+" if minutes >= 0 else "-"
    m = abs(int(minutes))
    return f"{sign}{m // 60:02d}:{m % 60:02d}"


class EPGPrefs:
    """Builds the EPG settings into a preferences page and saves them.

    *row(group, label, widget, hint)* and *group(page, heading)* are the
    preferences window's own helpers, so the page looks like the others.
    """

    def __init__(self, app, window, page, group, row):
        self.app = app
        self.window = window
        s = app.settings

        g = group(page)
        self.save_sw = row(g, _("Save EPG data"), Gtk.Switch(active=bool(s.get("epg_save", True))),
                           _("Keeps the guide on disk after LinDVB is closed."))
        self.info_spin = row(g, _("Show EPG info on channel change (s)"),
                             Gtk.SpinButton.new_with_range(0, 60, 1), _("0 = do not show it."))
        self.info_spin.set_value(int(s.get("epg_info_seconds", 4)))
        self.remind_spin = row(g, _("Remind before start (min)"),
                               Gtk.SpinButton.new_with_range(0, 60, 1),
                               _("Reminders set in the programme guide notify you this early."))
        self.remind_spin.set_value(int(s.get("reminder_minutes", 2)))
        codes = [c for c, _unused in LANGUAGES]
        cur = s.get("epg_language", "")
        if cur and cur not in codes:
            codes.append(cur)
        self._lang_codes = codes
        self.lang_dd = row(g, _("Preferred language"),
                           dropdown([dict(LANGUAGES).get(c, c) for c in codes],
                                    codes.index(cur) if cur in codes else 0),
                           _("Used when a programme is broadcast in several languages."))

        g = group(page, _("Time zone"))
        auto = bool(s.get("epg_tz_auto", True))
        self.tz_auto = row(g, _("Set time zone automatically"), Gtk.Switch(active=auto),
                           _("Off: use a fixed offset to GMT for the guide, timers and clock."))
        self.tz_spin = row(g, _("Offset to GMT"), Gtk.SpinButton.new_with_range(-12 * 60, 14 * 60, 15))
        self.tz_spin.set_value(int(s.get("epg_tz_offset", 60)))
        self.tz_spin.set_numeric(False)
        self.tz_spin.connect("output", self._tz_output)
        self.tz_spin.connect("input", self._tz_input)
        self.tz_spin.set_sensitive(not auto)
        self.tz_auto.connect("notify::active",
                             lambda sw, _p: self.tz_spin.set_sensitive(not sw.get_active()))

        g = group(page, _("Internet EPG (IPTV)"))
        self.xmltv = row(g, "XMLTV URL", Gtk.Entry(text=s.get("xmltv_url", "")),
                         _("Internet EPG source, used in addition to the broadcast EPG."))
        match = Gtk.Button(label=_("Match IPTV Channels…"))
        match.connect("clicked", lambda *_: app.show_xmltv_match())
        row(g, "IPTV channels and XMLTV", match,
            _("Check or fix which guide channel belongs to which M3U channel."))

        page.append(Gtk.Label(label=_("MediaHighway / Freesat frequencies"), xalign=0,
                              css_classes=["lv-section-title"], margin_top=4))
        page.append(Gtk.Label(
            label=_("When a tuned channel is on one of these transponders, LinDVB also reads that "
                  "provider's EPG. Changes apply the next time a channel is tuned."),
            xalign=0, wrap=True, css_classes=["lv-hint"]))
        mh = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["lv-group"])
        add_row = Gtk.Box(spacing=6, margin_top=8)
        self.freq = Gtk.SpinButton.new_with_range(0, 13000, 1)
        self.freq.set_hexpand(True)
        self.freq.set_tooltip_text(_("Frequency (MHz)"))
        self.pol = dropdown([p.upper() for p in POLS])
        self.kind = dropdown([PROVIDER_TYPES[k] for k in KINDS])
        add = Gtk.Button(label=_("Add"))
        add.connect("clicked", lambda *_: self.on_add())
        for w in (self.freq, self.pol, self.kind, add):
            add_row.append(w)
        mh.append(add_row)
        cols = [(_("Frequency (MHz)"), lambda e: str(e[0]), True),
                (_("Polarisation"), lambda e: str(e[1]).upper(), False),
                (_("Type"), lambda e: PROVIDER_TYPES.get(e[2], e[2]), True)]
        self.view, self.store, self.sel = make_column_view(cols)
        sw = Gtk.ScrolledWindow(hexpand=True, min_content_height=130)
        sw.set_child(self.view)
        mh.append(sw)
        btns = Gtk.Box(spacing=6, halign=Gtk.Align.END, margin_bottom=8)
        for label, fn in ((_("Delete"), self.on_delete), (_("Defaults"), self.on_defaults)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, fn=fn: fn())
            btns.append(b)
        mh.append(btns)
        page.append(mh)
        self.entries = copy.deepcopy(s.get("epg_provider_freqs", DEFAULT_PROVIDER_FREQS))
        fill(self.store, self.entries)

        self.priority = SourcePriority(app, page, group, row)

    # -- helpers -----------------------------------------------------------------------
    def _tz_output(self, spin):
        spin.set_text(_fmt_offset(spin.get_value()))
        return True

    def _tz_input(self, spin):
        text = spin.get_text().strip()
        try:
            sign = -1 if text.startswith("-") else 1
            h, _, m = text.lstrip("+-").partition(":")
            return True, sign * (int(h or 0) * 60 + int(m or 0))
        except ValueError:
            return False, 0

    def on_add(self):
        freq = int(self.freq.get_value())
        if freq <= 0:
            message(self.window, _("Enter the transponder frequency in MHz."))
            return
        entry = [freq, POLS[self.pol.get_selected()], KINDS[self.kind.get_selected()]]
        if entry not in self.entries:
            self.entries.append(entry)
            fill(self.store, self.entries)

    def on_delete(self):
        e = selected(self.sel)
        if e in self.entries:
            self.entries.remove(e)
            fill(self.store, self.entries)

    def on_defaults(self):
        self.entries = copy.deepcopy(DEFAULT_PROVIDER_FREQS)
        fill(self.store, self.entries)

    def save(self, s):
        """Write the page into settings *s* and apply it (the caller saves)."""
        s["epg_save"] = self.save_sw.get_active()
        s["epg_info_seconds"] = int(self.info_spin.get_value())
        s["reminder_minutes"] = int(self.remind_spin.get_value())
        s["epg_language"] = self._lang_codes[self.lang_dd.get_selected()]
        s["xmltv_url"] = self.xmltv.get_text().strip()
        s["epg_tz_auto"] = self.tz_auto.get_active()
        s["epg_tz_offset"] = int(self.tz_spin.get_value())
        s["epg_provider_freqs"] = self.entries
        self.priority.save(s)

    def apply(self, s):
        apply_timezone(s)
        self.app.apply_epg_priority()
        self.app.epg.persist = s["epg_save"]
        self.app.epg.save()
        self.app.epg.notify()


def _search_dropdown(strings):
    """A drop-down with a search entry, for long lists such as all channels."""
    model = Gtk.StringList.new(strings)
    dd = Gtk.DropDown(model=model)
    dd.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
    dd.set_enable_search(True)
    if hasattr(dd, "set_search_match_mode"):            # GTK 4.12
        dd.set_search_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
    return dd, model


class SourcePriority:
    """"Advanced": which EPG source wins, for all channels and per channel."""

    def __init__(self, app, page, group, row):
        s = app.settings
        self.order = normalize_order(s.get("epg_priority_order"))
        self.channels = {k: normalize_order(v)
                         for k, v in (s.get("epg_priority_channels") or {}).items()}
        self.all = [c for c in app.db.channels if not c.radio] + \
            [c for c in app.db.channels if c.radio]
        self.by_key = {}
        for c in self.all:
            self.by_key.setdefault(c.key, c)

        g = group(page, _("Advanced"))
        self.mode_dd = row(g, _("Priority of the EPG sources"),
                           dropdown([_("Automatic (recommended)"), _("Custom order")],
                                    1 if s.get("epg_priority_mode") == "custom" else 0),
                           _("Automatic: now and next come from the broadcast, further ahead "
                             "the most complete source wins, and gaps are filled from the "
                             "other sources. A programme is never shown twice."))
        self.mode_dd.connect("notify::selected", lambda *_: self._refresh())

        page.append(Gtk.Label(label=_("Order of the EPG sources"), xalign=0,
                              css_classes=["lv-section-title"], margin_top=4))
        page.append(Gtk.Label(
            label=_("The source at the top wins when two sources disagree; the others fill "
                    "the gaps. A channel with an order of its own always uses it, for "
                    "example when its broadcast EPG is poor."),
            xalign=0, wrap=True, css_classes=["lv-hint"]))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["lv-group"])

        top = Gtk.Box(spacing=6, margin_top=8)
        top.append(Gtk.Label(label=_("Applies to"), xalign=0))
        self.scope_dd, self.scope_model = _search_dropdown([])
        self.scope_dd.set_hexpand(True)
        self.scope_dd.connect("notify::selected", lambda *_: self._show_order())
        top.append(self.scope_dd)
        self.remove_btn = Gtk.Button(label=_("Remove Channel"))
        self.remove_btn.connect("clicked", lambda *_: self._remove())
        top.append(self.remove_btn)
        box.append(top)

        mid = Gtk.Box(spacing=6)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, hexpand=True,
                                css_classes=["boxed-list"])
        mid.append(self.list)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.START)
        self.up_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Move Up"))
        self.up_btn.connect("clicked", lambda *_: self._move(-1))
        self.down_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text=_("Move Down"))
        self.down_btn.connect("clicked", lambda *_: self._move(1))
        self.reset_btn = Gtk.Button(label=_("Defaults"))
        self.reset_btn.connect("clicked", lambda *_: self._reset())
        for b in (self.up_btn, self.down_btn, self.reset_btn):
            side.append(b)
        mid.append(side)
        box.append(mid)

        add = Gtk.Box(spacing=6, margin_bottom=8)
        self.chan_dd, _m = _search_dropdown([self._name(c) for c in self.all])
        self.chan_dd.set_hexpand(True)
        self.chan_dd.set_tooltip_text(_("Channel"))
        add_btn = Gtk.Button(label=_("Own Order for Channel"))
        add_btn.connect("clicked", lambda *_: self._add())
        add_btn.set_sensitive(bool(self.all))
        add.append(self.chan_dd)
        add.append(add_btn)
        box.append(add)
        page.append(box)

        self._scopes = []
        self._fill_scopes()
        self._refresh()

    # -- helpers -----------------------------------------------------------------------
    @staticmethod
    def _name(c):
        return f"{c.number}  {c.name}" if getattr(c, "number", 0) else c.name

    def _fill_scopes(self, select=None):
        keys = sorted((k for k in self.channels if k in self.by_key),
                      key=lambda k: self._name(self.by_key[k]).lower())
        # overrides of channels no longer in the list stay, but are not shown
        self._scopes = [None] + keys
        names = [_("All channels")] + [self._name(self.by_key[k]) for k in keys]
        self.scope_model.splice(0, self.scope_model.get_n_items(), names)
        self.scope_dd.set_selected(self._scopes.index(select) if select in self._scopes else 0)
        self._show_order()

    def _scope(self):
        i = self.scope_dd.get_selected()
        return self._scopes[i] if 0 <= i < len(self._scopes) else None

    def _current(self):
        key = self._scope()
        return self.order if key is None else self.channels[key]

    def _show_order(self, select=0):
        while (child := self.list.get_first_child()) is not None:
            self.list.remove(child)
        for i, src in enumerate(self._current()):
            self.list.append(Gtk.Label(label=f"{i + 1}.  {_(SOURCE_LABELS[src])}", xalign=0,
                                       margin_start=8, margin_end=8, margin_top=6,
                                       margin_bottom=6))
        self.list.select_row(self.list.get_row_at_index(select))
        self._refresh()

    def _refresh(self):
        custom = self.mode_dd.get_selected() == 1 or self._scope() is not None
        for w in (self.list, self.up_btn, self.down_btn, self.reset_btn):
            w.set_sensitive(custom)
        self.remove_btn.set_sensitive(self._scope() is not None)

    def _move(self, step):
        row = self.list.get_selected_row()
        if row is None:
            return
        order = self._current()
        i, j = row.get_index(), row.get_index() + step
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            self._show_order(j)

    def _reset(self):
        key = self._scope()
        if key is None:
            self.order = list(DEFAULT_ORDER)
        else:
            self.channels[key] = list(DEFAULT_ORDER)
        self._show_order()

    def _add(self):
        i = self.chan_dd.get_selected()
        if not 0 <= i < len(self.all):
            return
        key = self.all[i].key
        self.channels.setdefault(key, list(self.order))
        self._fill_scopes(key)

    def _remove(self):
        key = self._scope()
        if key is not None:
            self.channels.pop(key, None)
            self._fill_scopes()

    def save(self, s):
        s["epg_priority_mode"] = "custom" if self.mode_dd.get_selected() == 1 else "auto"
        s["epg_priority_order"] = list(self.order)
        s["epg_priority_channels"] = {k: list(v) for k, v in self.channels.items()}
