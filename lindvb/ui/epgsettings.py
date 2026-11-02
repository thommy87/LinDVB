"""The "Programme guide" page of the preferences: saving, language, info on
channel change, time zone, XMLTV and the MediaHighway / Freesat transponders
(modelled on DVBViewer's EPG options)."""

import copy

from gi.repository import Gtk

from ..epg import apply_timezone
from ..epgsources import DEFAULT_PROVIDER_FREQS, LANGUAGES, PROVIDER_TYPES
from .common import dropdown, fill, make_column_view, message, selected

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
        self.save_sw = row(g, "Save EPG data", Gtk.Switch(active=bool(s.get("epg_save", True))),
                           "Keeps the guide on disk after LinDVB is closed.")
        self.info_spin = row(g, "Show EPG info on channel change (s)",
                             Gtk.SpinButton.new_with_range(0, 60, 1), "0 = do not show it.")
        self.info_spin.set_value(int(s.get("epg_info_seconds", 4)))
        codes = [c for c, _ in LANGUAGES]
        cur = s.get("epg_language", "")
        if cur and cur not in codes:
            codes.append(cur)
        self._lang_codes = codes
        self.lang_dd = row(g, "Preferred language",
                           dropdown([dict(LANGUAGES).get(c, c) for c in codes],
                                    codes.index(cur) if cur in codes else 0),
                           "Used when a programme is broadcast in several languages.")

        g = group(page, "Time zone")
        auto = bool(s.get("epg_tz_auto", True))
        self.tz_auto = row(g, "Set time zone automatically", Gtk.Switch(active=auto),
                           "Off: use a fixed offset to GMT for the guide, timers and clock.")
        self.tz_spin = row(g, "Offset to GMT", Gtk.SpinButton.new_with_range(-12 * 60, 14 * 60, 15))
        self.tz_spin.set_value(int(s.get("epg_tz_offset", 60)))
        self.tz_spin.set_numeric(False)
        self.tz_spin.connect("output", self._tz_output)
        self.tz_spin.connect("input", self._tz_input)
        self.tz_spin.set_sensitive(not auto)
        self.tz_auto.connect("notify::active",
                             lambda sw, _p: self.tz_spin.set_sensitive(not sw.get_active()))

        g = group(page, "Internet EPG (IPTV)")
        self.xmltv = row(g, "XMLTV URL", Gtk.Entry(text=s.get("xmltv_url", "")),
                         "Internet EPG source, used in addition to the broadcast EPG.")
        match = Gtk.Button(label="Match IPTV Channels…")
        match.connect("clicked", lambda *_: app.show_xmltv_match())
        row(g, "IPTV channels and XMLTV", match,
            "Check or fix which guide channel belongs to which M3U channel.")

        page.append(Gtk.Label(label="MediaHighway / Freesat frequencies", xalign=0,
                              css_classes=["lv-section-title"], margin_top=4))
        page.append(Gtk.Label(
            label="When a tuned channel is on one of these transponders, LinDVB also reads that "
                  "provider's EPG. Changes apply the next time a channel is tuned.",
            xalign=0, wrap=True, css_classes=["lv-hint"]))
        mh = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["lv-group"])
        add_row = Gtk.Box(spacing=6, margin_top=8)
        self.freq = Gtk.SpinButton.new_with_range(0, 13000, 1)
        self.freq.set_hexpand(True)
        self.freq.set_tooltip_text("Frequency (MHz)")
        self.pol = dropdown([p.upper() for p in POLS])
        self.kind = dropdown([PROVIDER_TYPES[k] for k in KINDS])
        add = Gtk.Button(label="Add")
        add.connect("clicked", lambda *_: self.on_add())
        for w in (self.freq, self.pol, self.kind, add):
            add_row.append(w)
        mh.append(add_row)
        cols = [("Frequency (MHz)", lambda e: str(e[0]), True),
                ("Polarisation", lambda e: str(e[1]).upper(), False),
                ("Type", lambda e: PROVIDER_TYPES.get(e[2], e[2]), True)]
        self.view, self.store, self.sel = make_column_view(cols)
        sw = Gtk.ScrolledWindow(hexpand=True, min_content_height=130)
        sw.set_child(self.view)
        mh.append(sw)
        btns = Gtk.Box(spacing=6, halign=Gtk.Align.END, margin_bottom=8)
        for label, fn in (("Delete", self.on_delete), ("Defaults", self.on_defaults)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, fn=fn: fn())
            btns.append(b)
        mh.append(btns)
        page.append(mh)
        self.entries = copy.deepcopy(s.get("epg_provider_freqs", DEFAULT_PROVIDER_FREQS))
        fill(self.store, self.entries)

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
            message(self.window, "Enter the transponder frequency in MHz.")
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
        s["epg_language"] = self._lang_codes[self.lang_dd.get_selected()]
        s["xmltv_url"] = self.xmltv.get_text().strip()
        s["epg_tz_auto"] = self.tz_auto.get_active()
        s["epg_tz_offset"] = int(self.tz_spin.get_value())
        s["epg_provider_freqs"] = self.entries

    def apply(self, s):
        apply_timezone(s)
        self.app.epg.persist = s["epg_save"]
        self.app.epg.save()
        self.app.epg.notify()
