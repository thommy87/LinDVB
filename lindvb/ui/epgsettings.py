"""EPG settings: saving, language, info on channel change, time zone and the
MediaHighway / Freesat transponders (modelled on DVBViewer's EPG options)."""

import copy

from gi.repository import Gtk

from ..epg import apply_timezone
from ..epgsources import DEFAULT_PROVIDER_FREQS, LANGUAGES, PROVIDER_TYPES
from .common import dropdown, fill, make_column_view, message, selected

POLS = ["h", "v"]
KINDS = list(PROVIDER_TYPES)


def _frame(title, child):
    f = Gtk.Frame(label=title)
    child.set_margin_start(10)
    child.set_margin_end(10)
    child.set_margin_top(8)
    child.set_margin_bottom(10)
    f.set_child(child)
    return f


def _fmt_offset(minutes):
    sign = "+" if minutes >= 0 else "-"
    m = abs(int(minutes))
    return f"{sign}{m // 60:02d}:{m % 60:02d}"


class EPGSettingsWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(title="EPG Settings", transient_for=parent, modal=True,
                         default_width=640, default_height=720)
        self.app = app
        s = app.settings
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=14,
                      margin_end=14, margin_top=14, margin_bottom=14)

        # -- general ------------------------------------------------------------
        g = Gtk.Grid(row_spacing=8, column_spacing=12)
        self.save_sw = Gtk.CheckButton(label="Save EPG data (keeps the guide after LinDVB is closed)",
                                       active=bool(s.get("epg_save", True)))
        g.attach(self.save_sw, 0, 0, 2, 1)
        g.attach(Gtk.Label(label="Show EPG info on channel change (seconds, 0 = off)", xalign=0),
                 0, 1, 1, 1)
        self.info_spin = Gtk.SpinButton.new_with_range(0, 60, 1)
        self.info_spin.set_value(int(s.get("epg_info_seconds", 4)))
        g.attach(self.info_spin, 1, 1, 1, 1)
        g.attach(Gtk.Label(label="Preferred language", xalign=0), 0, 2, 1, 1)
        codes = [c for c, _ in LANGUAGES]
        cur = s.get("epg_language", "")
        if cur and cur not in codes:
            codes.append(cur)
        self._lang_codes = codes
        labels = [dict(LANGUAGES).get(c, c) for c in codes]
        self.lang_dd = dropdown(labels, codes.index(cur) if cur in codes else 0)
        self.lang_dd.set_hexpand(True)
        g.attach(self.lang_dd, 1, 2, 1, 1)
        g.attach(Gtk.Label(label="XMLTV URL (internet EPG for IPTV)", xalign=0), 0, 3, 1, 1)
        self.xmltv = Gtk.Entry(text=s.get("xmltv_url", ""), hexpand=True)
        g.attach(self.xmltv, 1, 3, 1, 1)
        box.append(_frame("General", g))

        # -- time zone ------------------------------------------------------------
        tz = Gtk.Grid(row_spacing=6, column_spacing=12)
        self.tz_auto = Gtk.CheckButton(label="Set time zone automatically (system)")
        self.tz_manual = Gtk.CheckButton(label="Set time zone manually")
        self.tz_manual.set_group(self.tz_auto)
        auto = bool(s.get("epg_tz_auto", True))
        (self.tz_auto if auto else self.tz_manual).set_active(True)
        self.tz_spin = Gtk.SpinButton.new_with_range(-12 * 60, 14 * 60, 15)
        self.tz_spin.set_value(int(s.get("epg_tz_offset", 60)))
        self.tz_spin.set_numeric(False)
        self.tz_spin.set_width_chars(6)
        self.tz_spin.connect("output", self._tz_output)
        self.tz_spin.connect("input", self._tz_input)
        self.tz_spin.set_sensitive(not auto)
        self.tz_manual.connect("toggled", lambda b: self.tz_spin.set_sensitive(b.get_active()))
        tz.attach(self.tz_auto, 0, 0, 1, 1)
        tz.attach(self.tz_manual, 0, 1, 1, 1)
        tz.attach(Gtk.Label(label="Offset to GMT", xalign=1, hexpand=True), 1, 1, 1, 1)
        tz.attach(self.tz_spin, 2, 1, 1, 1)
        box.append(_frame("Time zone", tz))

        # -- MediaHighway / Freesat ------------------------------------------------------
        mh = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row = Gtk.Box(spacing=6)
        self.freq = Gtk.SpinButton.new_with_range(0, 13000, 1)
        self.freq.set_hexpand(True)
        self.pol = dropdown([p.upper() for p in POLS])
        self.kind = dropdown([PROVIDER_TYPES[k] for k in KINDS])
        add = Gtk.Button(label="Add")
        add.connect("clicked", lambda *_: self.on_add())
        for w in (self.freq, self.pol, self.kind, add):
            row.append(w)
        mh.append(row)
        cols = [("Frequency (MHz)", lambda e: str(e[0]), True),
                ("Polarisation", lambda e: str(e[1]).upper(), False),
                ("Type", lambda e: PROVIDER_TYPES.get(e[2], e[2]), True)]
        self.view, self.store, self.sel = make_column_view(cols)
        sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True, min_content_height=140)
        sw.set_child(self.view)
        lower = Gtk.Box(spacing=6)
        lower.append(sw)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for label, fn in (("Delete", self.on_delete), ("Defaults", self.on_defaults)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, fn=fn: fn())
            side.append(b)
        lower.append(side)
        mh.append(lower)
        mh.append(Gtk.Label(
            label="When a tuned channel is on one of these transponders, LinDVB also reads "
                  "that provider's EPG. Changes apply the next time a channel is tuned.",
            wrap=True, xalign=0, css_classes=["dim-label"]))
        box.append(_frame("MediaHighway / Freesat frequencies", mh))
        self.entries = copy.deepcopy(s.get("epg_provider_freqs", DEFAULT_PROVIDER_FREQS))
        self._refresh()

        # -- buttons --------------------------------------------------------------------
        btns = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        ok = Gtk.Button(label="Save", css_classes=["suggested-action"])
        ok.connect("clicked", lambda *_: self.on_save())
        btns.append(cancel)
        btns.append(ok)
        box.append(btns)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(box)
        self.set_child(sc)

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

    def _refresh(self):
        fill(self.store, self.entries)

    def on_add(self):
        freq = int(self.freq.get_value())
        if freq <= 0:
            message(self, "Enter the transponder frequency in MHz.")
            return
        entry = [freq, POLS[self.pol.get_selected()], KINDS[self.kind.get_selected()]]
        if entry not in self.entries:
            self.entries.append(entry)
            self._refresh()

    def on_delete(self):
        e = selected(self.sel)
        if e in self.entries:
            self.entries.remove(e)
            self._refresh()

    def on_defaults(self):
        self.entries = copy.deepcopy(DEFAULT_PROVIDER_FREQS)
        self._refresh()

    def on_save(self):
        s = self.app.settings
        s["epg_save"] = self.save_sw.get_active()
        s["epg_info_seconds"] = int(self.info_spin.get_value())
        s["epg_language"] = self._lang_codes[self.lang_dd.get_selected()]
        s["xmltv_url"] = self.xmltv.get_text().strip()
        s["epg_tz_auto"] = self.tz_auto.get_active()
        s["epg_tz_offset"] = int(self.tz_spin.get_value())
        s["epg_provider_freqs"] = self.entries
        s.save()
        apply_timezone(s)
        self.app.epg.persist = s["epg_save"]
        self.app.epg.save()
        self.app.epg.notify()
        self.close()
