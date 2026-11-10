# SPDX-License-Identifier: GPL-2.0-or-later
"""Add channels: tune one frequency by hand and compare what it carries with
the channel list.

The user picks the tuner and the reception type and enters the transponder;
"Tune channel" searches just that frequency (the engine of the channel search,
lindvb.ui.scan.ScanEngine, with its signal meter). The table below shows the
channels found against the ones the list has for this transponder: unchanged,
changed, new and no longer broadcast. "Save" carries out the ticked lines.
The comparison itself is lindvb.singletune.
"""

import tempfile
from pathlib import Path

from gi.repository import Gio, GObject, Gtk

from .. import satedit, singletune
from .. import scanlists as sl
from ..singletune import CHANGED, GONE, NEW, SAME
from .common import dropdown, scrolled
from .scan import (DEFAULT_POSITION, DISEQC, FAMILY_LABELS, LNB_TYPES, ScanEngine,
                   _search_dropdown)
from .transponders import MOD_LABELS as SAT_MOD_LABELS
from .transponders import POL_LABELS, fec_label
from ..i18n import _

STATUS_LABELS = {SAME: _("Unchanged"), CHANGED: _("Changed"), NEW: _("New"),
                 GONE: _("No longer broadcast")}
FIELD_LABELS = {
    "name": _("Name"), "video_pid": "Video PID", "audio_pids": "Audio PID",
    "tsid": "TSID", "onid": "ONID", "delsys": _("System"), "symbol_rate": _("Symbol rate"),
    "bandwidth_hz": _("Bandwidth"), "pmt_pid": "PMT PID", "teletext_pid": _("Teletext"),
    "subtitle_pids": _("Subtitles"), "encrypted": _("Encrypted"),
}


def mod_label(mod):
    return _("Auto") if mod in ("", "auto") else singletune.MOD_LABELS.get(
        mod, SAT_MOD_LABELS.get(mod, mod))


def bandwidth_label(hz):
    return f"{hz / 1000000:g} MHz"


def value_text(name, value):
    """A field value of a channel as the "Changes" column shows it."""
    if isinstance(value, bool):
        return _("yes") if value else _("no")
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "–"
    if name == "delsys":
        return singletune.SYSTEM_LABELS.get(value, value)
    if name == "bandwidth_hz":
        return bandwidth_label(value)
    if name == "symbol_rate":
        return f"{value} kBd"
    if value in (0, "", None):
        return "–"
    return str(value)


def changes_text(row):
    """What changed on a channel: "Audio PID: 5101 → 5101, 5102; …"."""
    return "; ".join(f"{FIELD_LABELS.get(n, n)}: {value_text(n, a)} → {value_text(n, b)}"
                     for n, a, b in row.changes)


class _Item(GObject.Object):
    """One line of the result with its tick."""

    def __init__(self, row):
        super().__init__()
        self.row, self.on = row, row.status != SAME


def _set_model(dd, labels, index=0):
    dd.set_model(Gtk.StringList.new(labels))
    dd.set_selected(min(max(index, 0), max(len(labels) - 1, 0)))


def _pair(grid, row, col, text, widget, width=1):
    """A label and its widget in the two-pair grid of the form."""
    label = Gtk.Label(label=text, xalign=0)
    grid.attach(label, col * 2, row, 1, 1)
    grid.attach(widget, col * 2 + 1, row, width, 1)
    widget.set_hexpand(True)
    return label


def _spin(lo, hi, value, digits=0, step=1):
    s = Gtk.SpinButton.new_with_range(lo, hi, step)
    s.set_digits(digits)
    s.set_value(value)
    return s


class TuneWindow(ScanEngine, Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(title=_("Add Channels"), transient_for=parent,
                         default_width=780, default_height=760)
        self.app = app
        self.proc = None
        self._cancel = False
        self._closed = False
        self.jobs = []
        self.lists = []
        self.systems = []
        self.family = sl.TERR
        self.position_choices = []
        self.tp_choices = []
        self.sys_choices = {}
        self.mod_choices = {}
        self._filling = False
        self._restored = set()
        self._last_log = ""
        self._tuned = None
        self._group = ""
        self._detailed = False
        self._found = []
        self.scan_memory = dict(app.settings.get("scan_choice") or {})
        self.memory = dict(app.settings.get("tune_choice") or {})
        # the engine counts the frequencies on a progress bar; one frequency needs none
        self.progress = Gtk.ProgressBar()

        g = Gtk.Grid(row_spacing=6, column_spacing=8, margin_start=12, margin_end=12,
                     margin_top=12)
        names = self._list_adapters(configured=False)     # only tuners that are there
        self.adapter = dropdown(names or [_("(no tuner)")])
        _pair(g, 0, 0, _("Device"), self.adapter)
        self.systems_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        g.attach(self.systems_label, 1, 1, 1, 1)
        self.recep = Gtk.DropDown.new_from_strings([""])
        self.recep_label = _pair(g, 2, 0, _("Reception"), self.recep)
        self.recep_families = []

        self.stack = Gtk.Stack(vhomogeneous=False)
        self.w = {sl.SAT: self._build_sat(), sl.CABLE: self._build_cable(),
                  sl.TERR: self._build_terr()}
        g.attach(self.stack, 0, 3, 2, 1)
        self.root = Gtk.Entry()
        _pair(g, 4, 0, _("Target group"), self.root)

        bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12)
        self.start_btn = Gtk.Button(label=_("Tune Channel"), css_classes=["suggested-action"],
                                    sensitive=bool(names))
        self.start_btn.connect("clicked", lambda *_a: self.on_tune())
        self.stop_btn = Gtk.Button(label=_("Stop"), sensitive=False)
        self.stop_btn.connect("clicked", lambda *_a: self._stop())
        self.spinner = Gtk.Spinner()
        self.status = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        for w in (self.start_btn, self.stop_btn, self.spinner, self.status):
            bar.append(w)
        if not names:
            self.status.set_text(_("No tuner found."))

        self._build_meter()
        table = self._build_table()
        table.set_margin_start(12)
        table.set_margin_end(12)
        table.set_size_request(-1, 170)

        self.log = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        log_sw = scrolled(self.log)
        log_sw.set_size_request(-1, 110)
        log_sw.set_vexpand(False)
        expander = Gtk.Expander(label=_("Log"), child=log_sw, hexpand=True)
        self.save_btn = Gtk.Button(label=_("Save"), css_classes=["suggested-action"],
                                   sensitive=False, valign=Gtk.Align.START)
        self.save_btn.connect("clicked", lambda *_a: self.on_save())
        close = Gtk.Button(label=_("Close"), valign=Gtk.Align.START)
        close.connect("clicked", lambda *_a: self.close())
        bottom = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_bottom=12)
        for w in (expander, close, self.save_btn):
            bottom.append(w)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for w in (g, bar, self.meter, table, bottom):
            box.append(w)
        self.set_child(box)
        labels = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for grid in (g, *[self.stack.get_child_by_name(x) for x in self.w]):
            child = grid.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Label) and grid.query_child(child)[0] == 0 \
                        and grid.query_child(child)[2] == 1:
                    labels.add_widget(child)
                child = child.get_next_sibling()

        self.connect("close-request", self._on_close)
        self.adapter.connect("notify::selected", lambda *_a: self._tuner_changed())
        self.recep.connect("notify::selected", lambda *_a: self._family_changed())
        self._reload_lists()
        self._tuner_changed()

    # -- the form ---------------------------------------------------------------------

    def _build_sat(self):
        g = Gtk.Grid(row_spacing=6, column_spacing=8)
        w = {}
        w["pos"] = _search_dropdown([""])
        _pair(g, 0, 0, _("Satellite"), w["pos"])
        w["port"] = dropdown(DISEQC)
        _pair(g, 0, 1, _("DiSEqC port"), w["port"])
        w["tp"] = _search_dropdown([""])
        _pair(g, 1, 0, _("Transponder"), w["tp"], 3)
        lo, hi = singletune.FREQ_RANGE[sl.SAT]
        w["freq"] = _spin(lo / 1000, hi / 1000, 11494, 1)
        _pair(g, 2, 0, _("Frequency (MHz)"), w["freq"])
        w["pol"] = dropdown([POL_LABELS[p] for p in satedit.POLARITIES])
        _pair(g, 2, 1, _("Polarisation"), w["pol"])
        lo, hi = singletune.SR_RANGE[sl.SAT]
        w["sr"] = _spin(lo, hi, 22000)
        _pair(g, 3, 0, _("Symbol rate (kBd)"), w["sr"])
        w["sys"] = dropdown([""])
        _pair(g, 3, 1, _("System"), w["sys"])
        w["mod"] = dropdown([""])
        _pair(g, 4, 0, _("Modulation"), w["mod"])
        w["fec"] = dropdown([fec_label(f) for f in satedit.FECS])
        _pair(g, 4, 1, "FEC", w["fec"])
        self.lnb = dropdown([n for n, _unused in LNB_TYPES])
        self.lnb_label = _pair(g, 5, 0, _("LNB type"), self.lnb, 3)
        self.uc_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        g.attach(self.uc_label, 1, 6, 3, 1)
        w["pos"].connect("notify::selected", lambda *_a: self._position_changed())
        w["tp"].connect("notify::selected", lambda *_a: self._tp_picked())
        w["sys"].connect("notify::selected", lambda *_a: self._system_changed(sl.SAT))
        w["port"].connect("notify::selected", lambda *_a: self._update_target())
        self.stack.add_named(g, sl.SAT)
        return w

    def _build_cable(self):
        g = Gtk.Grid(row_spacing=6, column_spacing=8)
        w = {}
        lo, hi = singletune.FREQ_RANGE[sl.CABLE]
        w["freq"] = _spin(lo / 1000, hi / 1000, 114, 3)
        _pair(g, 0, 0, _("Frequency (MHz)"), w["freq"])
        lo, hi = singletune.SR_RANGE[sl.CABLE]
        w["sr"] = _spin(lo, hi, 6900)
        _pair(g, 0, 1, _("Symbol rate (kBd)"), w["sr"])
        w["sys"] = dropdown([""])
        _pair(g, 1, 0, _("System"), w["sys"])
        w["mod"] = dropdown([""])
        _pair(g, 1, 1, _("Modulation"), w["mod"])
        w["sys"].connect("notify::selected", lambda *_a: self._system_changed(sl.CABLE))
        self.stack.add_named(g, sl.CABLE)
        return w

    def _build_terr(self):
        g = Gtk.Grid(row_spacing=6, column_spacing=8)
        w = {}
        lo, hi = singletune.FREQ_RANGE[sl.TERR]
        w["freq"] = _spin(lo / 1000, hi / 1000, 474, 3)
        _pair(g, 0, 0, _("Frequency (MHz)"), w["freq"])
        w["sys"] = dropdown([""])
        _pair(g, 0, 1, _("System"), w["sys"])
        w["bw"] = dropdown([bandwidth_label(b) for b in singletune.BANDWIDTHS])
        w["bw_label"] = _pair(g, 1, 0, _("Bandwidth"), w["bw"])
        w["plp"] = _spin(-1, 255, -1)
        w["plp"].set_tooltip_text(_("The PLP of a DVB-T2 multiplex that sends several; "
                                    "-1 leaves the choice to the tuner"))
        w["plp_label"] = _pair(g, 1, 1, "PLP", w["plp"])
        w["mod"] = dropdown([""])
        w["mod_label"] = _pair(g, 2, 0, _("Modulation"), w["mod"])
        w["sys"].connect("notify::selected", lambda *_a: self._system_changed(sl.TERR))
        self.stack.add_named(g, sl.TERR)
        return w

    def _family_changed(self):
        if not self.recep_families:
            return
        i = self.recep.get_selected()
        fam = self.family = self.recep_families[min(i, len(self.recep_families) - 1)]
        self.stack.set_visible_child_name(fam)
        w = self.w[fam]
        before = self._delsys(fam)
        self.sys_choices[fam] = choices = singletune.systems_for(fam, self.systems)
        self._filling = True
        _set_model(w["sys"], [singletune.SYSTEM_LABELS[s] for s in choices],
                   choices.index(before) if before in choices else max(
                       [choices.index(x) for x in ("dvb-s2", "dvb-t2") if x in choices],
                       default=0))
        self._filling = False
        if fam == sl.SAT:
            self._fill_positions()
        if fam not in self._restored:
            self._restored.add(fam)
            self._restore(fam)
        self._system_changed(fam)
        self._update_target()

    def _delsys(self, fam):
        choices = self.sys_choices.get(fam) or []
        i = self.w[fam]["sys"].get_selected()
        return choices[i] if i < len(choices) else ""

    def _mod(self, fam):
        mods = self.mod_choices.get(fam) or ("auto",)
        i = self.w[fam]["mod"].get_selected()
        return mods[i] if i < len(mods) else mods[0]

    def _system_changed(self, fam, mod=None):
        """The modulations and fields of the system chosen."""
        if self._filling:
            return
        w, delsys = self.w[fam], self._delsys(fam)
        mods = singletune.MODULATIONS.get(delsys) or ("auto",)
        if mod is None:
            mod = self._mod(fam)             # keep it where the new system has it too
        self.mod_choices[fam] = mods
        _set_model(w["mod"], [mod_label(m) for m in mods],
                   mods.index(mod) if mod in mods else 0)
        if fam == sl.TERR:
            has_bw = delsys in singletune.BANDWIDTH_SYSTEMS
            for key, on in (("bw", has_bw), ("plp", delsys == "dvb-t2"),
                            ("mod", len(mods) > 1)):
                w[key].set_visible(on)
                w[key + "_label"].set_visible(on)

    # -- satellites -------------------------------------------------------------------

    def _choice(self):
        i = self.w[sl.SAT]["pos"].get_selected()
        return self.position_choices[i] if i < len(self.position_choices) else None

    def _fill_positions(self):
        before = self._choice()
        want = before.position if before is not None else None
        if want is None:
            want = (self.memory.get(sl.SAT) or {}).get("position")
        if want is None:
            saved = self.scan_memory.get(sl.SAT) or []
            want = saved[0][1] if saved else DEFAULT_POSITION
        self.position_choices = sl.positions(self._usable(), self.systems)
        self._filling = True
        _set_model(self.w[sl.SAT]["pos"],
                   [c.label for c in self.position_choices] or [_("(no lists found)")],
                   sl.find_position(self.position_choices, want))
        self._filling = False
        self._position_changed()

    def _scan_port(self, position):
        """The DiSEqC port the channel search uses for *position*, None if it has none."""
        for item in self.scan_memory.get(sl.SAT) or []:
            if len(item) > 1 and item[1] == position:
                return item[0]
        return None

    def _position_changed(self):
        if self._filling:
            return
        w, choice = self.w[sl.SAT], self._choice()
        if choice is not None:
            port = self._scan_port(choice.position)
            if port is not None:                # as set up in the channel search
                w["port"].set_selected(port + 1)
            self.tp_choices = list(satedit.satellite_list(choice, self.systems).transponders)
        else:
            self.tp_choices = []
        self._filling = True
        _set_model(w["tp"], [_("Enter by hand")] + [
            f"{t.frequency / 1000:.0f} MHz {t.polarity.upper()}  {t.symbol_rate} kBd  "
            f"{singletune.SYSTEM_LABELS.get(t.delsys, t.delsys)}" for t in self.tp_choices])
        self._filling = False
        self._update_target()

    def _tp_picked(self):
        i = self.w[sl.SAT]["tp"].get_selected()
        if self._filling or not 0 < i <= len(self.tp_choices):
            return
        self._set_sat(self.tp_choices[i - 1])

    def _set_sat(self, t):
        """Show the tuning data *t* (a Transponder or a Tuning) in the satellite form."""
        w = self.w[sl.SAT]
        w["freq"].set_value(t.frequency / 1000)
        if t.polarity in satedit.POLARITIES:
            w["pol"].set_selected(satedit.POLARITIES.index(t.polarity))
        if t.symbol_rate:
            w["sr"].set_value(t.symbol_rate)
        choices = self.sys_choices.get(sl.SAT) or []
        if t.delsys in choices:
            self._filling = True
            w["sys"].set_selected(choices.index(t.delsys))
            self._filling = False
        self._system_changed(sl.SAT, t.modulation)
        if t.fec in satedit.FECS:
            w["fec"].set_selected(satedit.FECS.index(t.fec))

    # -- what is entered ----------------------------------------------------------------

    def _tuning(self):
        fam, w = self.family, self.w[self.family]
        freq = int(round(w["freq"].get_value() * 1000))
        delsys = self._delsys(fam)
        if fam == sl.SAT:
            return singletune.Tuning(
                delsys, freq, satedit.POLARITIES[w["pol"].get_selected()],
                int(w["sr"].get_value()), self._mod(fam),
                satedit.FECS[w["fec"].get_selected()], diseqc=w["port"].get_selected() - 1)
        if fam == sl.CABLE:
            return singletune.Tuning(delsys, freq, symbol_rate=int(w["sr"].get_value()),
                                     modulation=self._mod(fam))
        t = singletune.Tuning(delsys, freq, modulation=self._mod(fam))
        if delsys in singletune.BANDWIDTH_SYSTEMS:
            t.bandwidth_hz = singletune.BANDWIDTHS[w["bw"].get_selected()]
        if delsys == "dvb-t2":
            t.stream_id = int(w["plp"].get_value())
        return t

    def _default_target(self, t=None):
        t = t or self._tuning()
        if t.family == sl.SAT:
            choice = self._choice()
            fallback = sl.default_target(choice, sl.SAT) if choice else _("Satellite")
        else:
            fallback = FAMILY_LABELS[t.family]
        return singletune.target_group(self.app.db.channels, t, fallback)

    def _update_target(self):
        if self._filling or not hasattr(self, "root"):
            return
        self.root.set_placeholder_text(self._default_target())

    def _restore(self, fam):
        """The values entered the last time."""
        m = self.memory.get(fam) or {}
        w = self.w[fam]
        try:
            t = singletune.Tuning(**{k: v for k, v in (m.get("tuning") or {}).items()
                                     if k in singletune.Tuning.__dataclass_fields__})
        except TypeError:
            return
        if singletune.check(t):
            return
        choices = self.sys_choices.get(fam) or []
        if fam == sl.SAT:
            choice = self._choice()
            if choice is None or self._scan_port(choice.position) is None:
                w["port"].set_selected(min(max(t.diseqc, -1), 3) + 1)
            self._set_sat(t)
            return
        w["freq"].set_value(t.frequency / 1000)
        if t.delsys in choices:
            self._filling = True
            w["sys"].set_selected(choices.index(t.delsys))
            self._filling = False
        self._system_changed(fam, t.modulation)
        if fam == sl.CABLE:
            w["sr"].set_value(t.symbol_rate)
        else:
            if t.bandwidth_hz in singletune.BANDWIDTHS:
                w["bw"].set_selected(singletune.BANDWIDTHS.index(t.bandwidth_hz))
            w["plp"].set_value(t.stream_id)

    def _remember(self, t):
        from dataclasses import asdict
        if getattr(self, "virtual", False):
            return
        entry = {"tuning": asdict(t)}
        choice = self._choice() if t.family == sl.SAT else None
        if choice is not None:
            entry["position"] = choice.position
        self.memory[t.family] = entry
        self.app.settings["tune_choice"] = self.memory
        self.app.settings.save()

    # -- tuning -------------------------------------------------------------------------

    def log_line(self, text):
        super().log_line(text)
        if text.strip():
            self._last_log = text.strip().splitlines()[-1]

    def on_tune(self):
        if not self.adapters:
            return
        t = self._tuning()
        bad = singletune.check(t)
        if bad:
            self._show_status(
                _("DVB-S only uses QPSK; choose DVB-S2 for 8PSK, 16APSK and 32APSK.")
                if bad == "system" and t.delsys == "dvb-s" else
                _("This transponder is not valid."), error=True)
            return
        a, f = self._current_adapter()
        tmp = Path(tempfile.mkdtemp(prefix="lindvb-tune-"))
        path = tmp / "initial.conf"
        path.write_text(singletune.scan_file(t))
        root = self.root.get_text().strip() or self._default_target(t)
        sat = t.family == sl.SAT
        job = {"file": path, "total": 1, "diseqc": t.diseqc, "root": root,
               "tpl": singletune.transponder_list(t, root) if sat else None,
               "out": tmp / "channels.conf", "nit": False, "file_only": True}
        self._tuned, self._group = t, root
        # single-cable and SAT>IP tuners read the stream themselves: they know more PIDs
        self._detailed = self.satip is not None or (sat and self.unicable is not None)
        self._remember(t)
        self._found = []
        self.store.remove_all()
        self.save_btn.set_sensitive(False)
        self.log.get_buffer().set_text("")
        self._last_log = ""
        self._show_status(_("Tuning…"), busy=True)
        self._begin([job], a, f)
        if self.start_btn.get_sensitive():       # nothing started: the log says why
            self._meter_stop()
            self._show_status(self._last_log or _("The tuner could not be used."), error=True)

    def _show_status(self, text, busy=False, error=False):
        self.status.set_text(text)
        if error:
            self.status.add_css_class("error")
        else:
            self.status.remove_css_class("error")
        self.spinner.set_spinning(busy)

    def _search_done(self, chans):
        if self._closed or self._tuned is None:
            return
        t = self._tuned
        found = singletune.on_mux(chans or [], t)
        for c in found:
            c.root = self._group
        self._found = found
        if not found:
            # nothing received says nothing about the channels in the list: leave them
            self.store.remove_all()
            self.save_btn.set_sensitive(False)
            self._show_status(_("No channels found on this frequency."), error=True)
            return
        self._show_rows()

    def _show_rows(self, prefix=""):
        rows = singletune.compare(self._found, self.app.db.channels, self._tuned,
                                  self._detailed)
        self.store.remove_all()
        self.store.splice(0, 0, [_Item(r) for r in rows])
        n = singletune.counts(rows)
        self._show_status((prefix + " " if prefix else "") + _(
            "{same} unchanged, {changed} changed, {new} new, {gone} no longer "
            "broadcast.").format(same=n[SAME], changed=n[CHANGED], new=n[NEW], gone=n[GONE]))
        self._update_save()

    def _items(self):
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    def _update_save(self):
        self.save_btn.set_sensitive(any(it.on and it.row.status != SAME
                                        for it in self._items()))

    def on_save(self):
        rows = [it.row for it in self._items() if it.on and it.row.status != SAME]
        if not rows:
            return
        app = self.app
        added, updated, removed = singletune.apply(app.db, rows, app.favs, self._group)
        app.db.save()
        if updated:
            app.favourites_changed()
        app.window.reload_channels()
        self._show_rows(_("Saved: {added} added, {updated} updated, {removed} "
                          "removed.").format(added=added, updated=updated, removed=removed))

    def _on_close(self, *_a):
        self._closed = True
        self._stop()
        self._meter_stop()
        return False

    # -- the table ------------------------------------------------------------------------

    def _build_table(self):
        self.store = Gio.ListStore(item_type=_Item)
        self._handlers = {}
        view = Gtk.ColumnView(model=Gtk.NoSelection(model=self.store),
                              show_column_separators=True, vexpand=True,
                              css_classes=["tune-result"])
        tick = Gtk.SignalListItemFactory()
        tick.connect("setup", lambda _f, li: li.set_child(Gtk.CheckButton()))
        tick.connect("bind", self._bind_tick)
        tick.connect("unbind", self._unbind_tick)
        view.append_column(Gtk.ColumnViewColumn(title="", factory=tick))
        for title, fn, expand in (
                (_("Status"), lambda r: STATUS_LABELS[r.status], False),
                (_("Name"), lambda r: r.channel.name, True),
                (_("Type"), lambda r: _("Radio") if r.channel.radio else "TV", False),
                ("SID", lambda r: str(r.channel.sid), False),
                (_("Changes"), changes_text, True)):
            fac = Gtk.SignalListItemFactory()
            fac.connect("setup", lambda _f, li: li.set_child(
                Gtk.Label(xalign=0, ellipsize=3)))
            fac.connect("bind", lambda _f, li, fn=fn: self._bind_text(li, fn))
            col = Gtk.ColumnViewColumn(title=title, factory=fac)
            col.set_expand(expand)
            col.set_resizable(True)
            view.append_column(col)
        return Gtk.ScrolledWindow(vexpand=True, hexpand=True, child=view, has_frame=True)

    @staticmethod
    def _colour(li):
        """The colour of the line, on each of its cells (the parent of the child)."""
        cell = li.get_child().get_parent()
        if cell is not None:
            for s in singletune.ORDER:
                cell.remove_css_class("tune-" + s)
            cell.add_css_class("tune-" + li.get_item().row.status)

    def _bind_text(self, li, fn):
        self._colour(li)
        text = fn(li.get_item().row)
        label = li.get_child()
        label.set_text(text)
        label.set_tooltip_text(text if len(text) > 30 else None)

    def _bind_tick(self, _f, li):
        item, chk = li.get_item(), li.get_child()
        same = item.row.status == SAME
        chk.set_active(item.on and not same)
        chk.set_sensitive(not same)          # nothing happens to an unchanged channel
        self._handlers[chk] = chk.connect("toggled",
                                          lambda b: self._toggled(item, b.get_active()))
        self._colour(li)

    def _unbind_tick(self, _f, li):
        chk = li.get_child()
        h = self._handlers.pop(chk, None)
        if h:
            chk.disconnect(h)

    def _toggled(self, item, on):
        item.on = on
        self._update_save()

