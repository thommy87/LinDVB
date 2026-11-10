# SPDX-License-Identifier: GPL-2.0-or-later
"""Channel search (dvbv5-scan; single-cable tuners: lindvb.scscan).

The dialog shows what fits the chosen tuner: a satellite tuner gets orbital
positions with a DiSEqC port each and the LNB, a cable tuner country and
provider, a terrestrial tuner country and region. Lists and blind scans come
from lindvb.scanlists.
"""

import tempfile
from pathlib import Path

from gi.repository import Gio, GLib, Gtk

from .. import scanlists as sl
from ..channels import parse_dvbv5
from ..scan import find_adapters, is_virtual_adapter, scan_command, write_scan_file
from .common import dropdown, labeled, scrolled
from ..i18n import _

LNB_TYPES = [(_("Universal (9750/10600, 11700)"), "UNIVERSAL"), (_("Single 10750"), "L10750"),
             (_("Single 10700"), "L10700"), (_("Single 11300"), "L11300"), ("C-Band", "C-BAND"),
             (_("Extended"), "EXTENDED")]
# the oscillators (low, high, switch in kHz) of the LNB types, to preselect the tuner's one
LNB_LOF = {"UNIVERSAL": (9750000, 10600000, 11700000), "L10750": (10750000, 0, 0),
           "L10700": (10700000, 0, 0), "L11300": (11300000, 0, 0), "C-BAND": (5150000, 0, 0),
           "EXTENDED": (9750000, 10600000, 11700000)}
DISEQC = [_("None"), _("Port 1 (A/A)"), _("Port 2 (B/A)"), _("Port 3 (A/B)"), _("Port 4 (B/B)")]
GROUP_BY = [_("Provider"), _("Frequency"), _("Initial letter")]
FAMILY_LABELS = {sl.SAT: _("Satellite"), sl.CABLE: _("Cable"), sl.TERR: _("Terrestrial")}
DEFAULT_POSITION = 192          # 19.2°E
# a new satellite row starts at the next of the common European positions
NEXT_POSITIONS = (192, 130, 282, 235, 50, 160, 70, 90, 360, 420)


def _search_dropdown(strings):
    d = Gtk.DropDown.new_from_strings(strings)
    d.set_enable_search(True)
    d.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
    d.set_hexpand(True)
    return d


class ScanWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(title=_("Channel Search"), transient_for=parent,
                         default_width=720, default_height=600)
        self.app = app
        self.proc = None
        self._cancel = False
        self.jobs = []
        self.lists = []
        self.systems = []
        self.family = sl.TERR
        self.sat_rows = []
        self.region_choices = []
        self.position_choices = []
        self.memory = dict(app.settings.get("scan_choice") or {})

        g = Gtk.Grid(row_spacing=6, column_spacing=8, margin_start=12, margin_end=12, margin_top=12)
        adapters = find_adapters()
        # SAT>IP: one entry per server (the server picks a free tuner)
        from .. import satip, tunerprofiles
        self.satip_profiles = {}
        for p in tunerprofiles.configured_profiles(app.settings):
            if satip.is_satip(p) and p.get("enabled", True) and \
                    p.get("server") not in {q.get("server") for q in self.satip_profiles.values()}:
                self.satip_profiles[p["adapter"]] = p
        net = [(a, 0, f"SAT>IP {p.get('server_label') or p['host']}")
               for a, p in self.satip_profiles.items()]
        local = adapters or ([] if net else [(a, app.settings["frontend"], f"Adapter {a}")
                                             for a in app.settings["adapters"]])
        self.adapters = local + net
        self.adapter = labeled(g, 0, _("Device"), dropdown(
            [n if a in self.satip_profiles else f"{n} (adapter{a}/frontend{f})"
             for a, f, n in self.adapters] or [_("(no tuner)")]))
        self.systems_label = Gtk.Label(xalign=0, css_classes=["dim-label"])
        g.attach(self.systems_label, 1, 1, 1, 1)
        self.recep_label = Gtk.Label(label=_("Reception"), xalign=0)
        self.recep = Gtk.DropDown.new_from_strings([""])
        self.recep.set_hexpand(True)
        g.attach(self.recep_label, 0, 2, 1, 1)
        g.attach(self.recep, 1, 2, 1, 1)
        self.recep_families = []

        # satellite: one row per dish position, each with its DiSEqC port
        self.stack = Gtk.Stack(vhomogeneous=False)
        sat = Gtk.Grid(row_spacing=6, column_spacing=8)
        sat.attach(Gtk.Label(label=_("Satellites"), xalign=0, yalign=0, margin_top=6), 0, 0, 1, 1)
        self.sat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        sat.attach(self.sat_box, 1, 0, 1, 1)
        add_sat = Gtk.Button(label=_("Add satellite"), halign=Gtk.Align.START,
                             tooltip_text=_("Search several dish positions behind a DiSEqC "
                                            "switch in one go"))
        add_sat.connect("clicked", lambda *_a: self._add_sat_row())
        sat.attach(add_sat, 1, 1, 1, 1)
        self.lnb_label = Gtk.Label(label=_("LNB type"), xalign=0)
        self.lnb = dropdown([n for n, _unused in LNB_TYPES])
        self.lnb.set_hexpand(True)
        sat.attach(self.lnb_label, 0, 2, 1, 1)
        sat.attach(self.lnb, 1, 2, 1, 1)
        self.uc_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        sat.attach(self.uc_label, 1, 3, 1, 1)
        self.stack.add_named(sat, sl.SAT)

        # cable and terrestrial: country, then provider or region
        self.country_dd, self.region_dd = {}, {}
        for fam, second in ((sl.CABLE, _("Provider")), (sl.TERR, _("Region"))):
            pg = Gtk.Grid(row_spacing=6, column_spacing=8)
            self.country_dd[fam] = labeled(pg, 0, _("Country"), _search_dropdown([""]))
            self.region_dd[fam] = labeled(pg, 1, second, _search_dropdown([""]))
            self.country_dd[fam].connect("notify::selected",
                                         lambda *_a, f=fam: self._fill_regions(f))
            self.region_dd[fam].connect("notify::selected", lambda *_a: self._show_info())
            self.stack.add_named(pg, fam)
        g.attach(self.stack, 0, 3, 2, 1)
        self.info = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        g.attach(self.info, 1, 4, 1, 1)

        # the rest is rarely changed
        adv = Gtk.Grid(row_spacing=6, column_spacing=8, margin_top=6)
        self.root = labeled(adv, 0, _("Target group"), Gtk.Entry())
        opts = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2)
        self.nit = Gtk.CheckButton(label=_("Search for additional frequencies (NIT)"), active=True)
        self.fta = Gtk.CheckButton(label=_("Only add free-to-air channels"))
        self.upd_only = Gtk.CheckButton(label=_("Only update existing channels"))
        self.upd_names = Gtk.CheckButton(label=_("Update channel names"), active=True)
        for w in (self.nit, self.fta, self.upd_only, self.upd_names):
            opts.append(w)
        adv.attach(opts, 0, 1, 2, 1)
        self.group_by = labeled(adv, 2, _("Group by"), dropdown(GROUP_BY))
        add = Gtk.Button(label=_("Add folder…"), halign=Gtk.Align.START,
                         tooltip_text=_("Folder with transponder lists "
                                        "(dtv-scan-tables or DVBViewer style .ini)"))
        add.connect("clicked", lambda *_a: self.on_add_folder())
        labeled(adv, 3, _("Own lists"), add).set_hexpand(False)
        expander = Gtk.Expander(label=_("More options"), child=adv)
        g.attach(expander, 0, 5, 2, 1)

        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12)
        self.start_btn = Gtk.Button(label=_("Start Search"), css_classes=["suggested-action"])
        self.start_btn.connect("clicked", lambda *_a: self.on_start())
        self.stop_btn = Gtk.Button(label=_("Stop"), sensitive=False)
        self.stop_btn.connect("clicked", lambda *_a: self._stop())
        self.progress = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER, show_text=True)
        for w in (self.start_btn, self.stop_btn, self.progress):
            bar.append(w)

        self.log = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(g)
        box.append(bar)
        sw = scrolled(self.log)
        sw.set_margin_start(12); sw.set_margin_end(12); sw.set_margin_bottom(12)
        sw.set_size_request(-1, 140)
        box.append(sw)
        self.set_child(box)
        # one label column for the grids on top, on the stack pages and under "More options"
        labels = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for grid in (g, sat, adv, *[self.stack.get_child_by_name(x) for x in (sl.CABLE, sl.TERR)]):
            child = grid.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Label) and grid.query_child(child)[0] == 0 \
                        and grid.query_child(child)[2] == 1:
                    labels.add_widget(child)
                child = child.get_next_sibling()

        self.adapter.connect("notify::selected", lambda *_a: self._tuner_changed())
        self.recep.connect("notify::selected", lambda *_a: self._family_changed())
        self._reload_lists()
        self._tuner_changed()

    # -- the tuner and what it receives ------------------------------------------------

    def _current_adapter(self):
        if not self.adapters:
            return (0, 0)
        return self.adapters[self.adapter.get_selected()][:2]

    def _profile(self, adapter, frontend):
        from .. import tunerprofiles
        if adapter in self.satip_profiles:
            return self.satip_profiles[adapter]
        for p in tunerprofiles.configured_profiles(self.app.settings):
            if p["adapter"] == adapter and p.get("frontend", 0) == frontend:
                return p
        return None

    def _tuner_changed(self):
        from .. import tunerprofiles, unicable
        a, f = self._current_adapter()
        prof = self._profile(a, f)
        systems = list((prof or {}).get("systems") or [])
        if not systems:
            systems = tunerprofiles.probe_delivery_systems(a, f)
        self.systems = systems
        self.virtual = is_virtual_adapter(a, f)
        self.systems_label.set_text(_("Receives: {systems}").format(
            systems=tunerprofiles.systems_label(systems)) if systems else
            _("The tuner does not say what it receives, so every list is offered."))
        fams = sl.families_for(systems)
        use = (prof or {}).get("use") or ""
        if use in fams and len(fams) > 1:
            fams = [use] + [x for x in fams if x != use]
        self.recep_families = fams
        self.recep.set_model(Gtk.StringList.new([FAMILY_LABELS[x] for x in fams]))
        self.recep.set_selected(0)
        many = len(fams) > 1
        self.recep.set_visible(many)
        self.recep_label.set_visible(many)
        # satellite: the tuner's own reception path wins over asking again
        self.satip = prof if a in self.satip_profiles else None
        self.unicable = unicable.config(prof) if prof and self.satip is None else None
        own_lnb = (prof or {}).get("lnb")
        lof = tuple(own_lnb) if own_lnb and len(own_lnb) == 3 and all(own_lnb) else \
            tunerprofiles.tuner_lnb(None, self.app.settings)
        for i, (_n, code) in enumerate(LNB_TYPES):
            if LNB_LOF[code] == lof:
                self.lnb.set_selected(i)
                break
        local = self.unicable is None and self.satip is None
        self.lnb.set_visible(local)
        self.lnb_label.set_visible(local)
        self.uc_label.set_visible(not local)
        if self.satip is not None:
            self.uc_label.set_text(_("The SAT>IP server controls the LNB and the DiSEqC switch; "
                                     "the DiSEqC port chooses its source (1 to 4)."))
        elif self.unicable is not None:
            self.uc_label.set_text(_("Single-cable setup of this tuner: {setup}").format(
                setup=unicable.describe(self.unicable)))
        self._family_changed()

    def _family_changed(self):
        i = self.recep.get_selected()
        if not self.recep_families:
            return
        self.family = self.recep_families[min(i, len(self.recep_families) - 1)]
        self.stack.set_visible_child_name(self.family)
        if self.family == sl.SAT:
            self._fill_positions()
        else:
            self._fill_countries(self.family)
        self._show_info()

    def _usable(self):
        """The lists for this tuner; the vidtv test lists only for the virtual tuner."""
        return [x for x in self.lists if self.virtual or x.country != sl.VIRTUAL]

    # -- satellites -------------------------------------------------------------------

    def _fill_positions(self):
        self.position_choices = sl.positions(self._usable(), self.systems)
        labels = [c.label for c in self.position_choices] or [_("(no lists found)")]
        for row in list(self.sat_rows):
            self._remove_sat_row(row, keep_one=False)
        saved = self.memory.get(sl.SAT) or [[-1, DEFAULT_POSITION]]
        if self.virtual:
            saved = [[-1, DEFAULT_POSITION]]
        for port, pos in saved:
            self._add_sat_row(port, pos, labels)

    def _add_sat_row(self, port=None, pos=None, labels=None):
        if labels is None:
            labels = [c.label for c in self.position_choices] or [_("(no lists found)")]
        row = Gtk.Box(spacing=6)
        dd_port = dropdown(DISEQC)
        dd_port.set_tooltip_text(_("DiSEqC port"))
        if port is None:                 # a new row: the next free port
            used = {r[1].get_selected() for r in self.sat_rows}
            port = next((p for p in range(4) if p + 1 not in used), 0)
            if self.sat_rows and self.sat_rows[0][1].get_selected() == 0:
                self.sat_rows[0][1].set_selected(1)
                port = 1
        dd_port.set_selected(port + 1)
        dd_pos = _search_dropdown(labels)
        if pos is None:
            taken = {r[2].get_selected() for r in self.sat_rows}
            pos = next((p for p in NEXT_POSITIONS
                        if sl.find_position(self.position_choices, p) not in taken),
                       DEFAULT_POSITION)
        i = sl.find_position(self.position_choices, pos)
        dd_pos.set_selected(max(i, 0))
        rm = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text=_("Remove"))
        entry = (row, dd_port, dd_pos, rm)
        rm.connect("clicked", lambda *_a: self._remove_sat_row(entry))
        dd_pos.connect("notify::selected", lambda *_a: self._show_info())
        for w in (dd_port, dd_pos, rm):
            row.append(w)
        self.sat_box.append(row)
        self.sat_rows.append(entry)
        self._update_sat_rows()

    def _remove_sat_row(self, entry, keep_one=True):
        if keep_one and len(self.sat_rows) <= 1:
            return
        self.sat_box.remove(entry[0])
        self.sat_rows.remove(entry)
        self._update_sat_rows()

    def _update_sat_rows(self):
        for r in self.sat_rows:
            r[3].set_visible(len(self.sat_rows) > 1)
        self._show_info()

    # -- cable and terrestrial ----------------------------------------------------------

    def _fill_countries(self, fam):
        self.country_codes = sl.countries(self._usable(), fam, self.systems)
        if not self.virtual:
            self.country_codes = [c for c in self.country_codes if c[0] != sl.VIRTUAL]
        dd = self.country_dd[fam]
        dd.set_model(Gtk.StringList.new([n for _c, n in self.country_codes]))
        want = (self.memory.get(fam) or [None])[0]
        if self.virtual:
            want = sl.VIRTUAL
        if want is None:
            want = self._desktop_country()
        codes = [c for c, _n in self.country_codes]
        dd.set_selected(codes.index(want) if want in codes else 0)
        self._fill_regions(fam)

    @staticmethod
    def _desktop_country():
        import os
        for var in ("LC_ALL", "LC_TIME", "LANG"):
            val = os.environ.get(var, "")
            if "_" in val:
                cc = val.split("_", 1)[1][:2].lower()
                return "uk" if cc == "gb" else cc
        return ""

    def _fill_regions(self, fam):
        dd = self.country_dd[fam]
        codes = getattr(self, "country_codes", [])
        i = dd.get_selected()
        if fam != self.family or not codes or i >= len(codes):
            return
        country = codes[i][0]
        self.region_choices = sl.regions(self._usable(), fam, self.systems, country)
        rd = self.region_dd[fam]
        rd.set_model(Gtk.StringList.new([c.label for c in self.region_choices]
                                        or [_("(no lists found)")]))
        mem = self.memory.get(fam) or [None, None]
        labels = [c.label for c in self.region_choices]
        rd.set_selected(labels.index(mem[1]) if mem[0] == country and mem[1] in labels else 0)
        self._show_info()

    # -- what will be searched --------------------------------------------------------

    def _sat_jobs(self):
        jobs = []
        for _row, dd_port, dd_pos, _rm in self.sat_rows:
            i = dd_pos.get_selected()
            if i < len(self.position_choices):
                jobs.append((self.position_choices[i], dd_port.get_selected() - 1))
        return jobs

    def _region_choice(self):
        i = self.region_dd[self.family].get_selected()
        return self.region_choices[i] if i < len(self.region_choices) else None

    def _show_info(self):
        text = ""
        if self.family == sl.SAT:
            jobs = self._sat_jobs()
            ports = [p for _c, p in jobs]
            if len(jobs) > 1 and (-1 in ports or len(set(ports)) < len(ports)):
                text = _("Each satellite needs a DiSEqC port of its own.")
            elif jobs:
                n = sum(len(sl.satellite_list(c, self.systems).transponders) for c, _p in jobs)
                text = _("{n} transponders from the lists of these positions; "
                         "the search adds the ones the network announces (NIT).").format(n=n)
        else:
            c = self._region_choice()
            if c is not None:
                n = len(sl.scan_entries(c, self.systems))
                if c.blind:
                    text = _("Blind scan: tries {n} frequencies, about {minutes} min.").format(
                        n=n, minutes=sl.estimate_minutes(n))
                else:
                    text = _("{n} frequencies from the list.").format(n=n)
                    title = c.lists[0].title if c.lists else ""
                    if title:
                        text += f" {title}"
        self.info.set_text(text)
        self.info.set_visible(bool(text))
        if hasattr(self, "root"):
            self.root.set_placeholder_text(self._default_target() or _("e.g. Astra 19.2°E"))

    def _default_target(self):
        if self.family == sl.SAT:
            jobs = self._sat_jobs()
            return sl.default_target(jobs[0][0], sl.SAT) if len(jobs) == 1 else ""
        c = self._region_choice()
        return sl.default_target(c, self.family) if c else ""

    def _remember(self):
        mem = dict(self.memory)
        if self.virtual:
            return
        if self.family == sl.SAT:
            mem[sl.SAT] = [[p, c.position] for c, p in self._sat_jobs()]
        else:
            codes = getattr(self, "country_codes", [])
            i = self.country_dd[self.family].get_selected()
            c = self._region_choice()
            if i < len(codes) and c is not None:
                mem[self.family] = [codes[i][0], c.label]
        self.memory = mem
        self.app.settings["scan_choice"] = mem
        self.app.settings.save()

    # -- lists ------------------------------------------------------------------------

    def _stop(self):
        self._cancel = True                  # single-cable search (thread)
        self.jobs = []
        if self.proc:
            self.proc.force_exit()

    def _reload_lists(self):
        self.lists = sl.all_lists(self.app.settings.get("transponder_dirs", []))

    def on_add_folder(self):
        dlg = Gtk.FileDialog(title=_("Folder with transponder lists"))
        dlg.select_folder(self, None, self._folder_chosen)

    def _folder_chosen(self, dlg, res):
        try:
            path = dlg.select_folder_finish(res).get_path()
        except GLib.Error:
            return
        dirs = self.app.settings.get("transponder_dirs", [])
        if path not in dirs:
            dirs.append(path)
        self.app.settings["transponder_dirs"] = dirs
        self.app.settings.save()
        self._reload_lists()
        self._family_changed()

    def log_line(self, text):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self.log.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 0)

    # -- searching --------------------------------------------------------------------

    def on_start(self):
        a, f = self._current_adapter()
        tmp = Path(tempfile.mkdtemp(prefix="lindvb-scan-"))
        root = self.root.get_text().strip()
        jobs = []
        if self.family == sl.SAT:
            sat_jobs = self._sat_jobs()
            ports = [p for _c, p in sat_jobs]
            if not sat_jobs:
                self.log_line(_("No transponder list selected. Install dtv-scan-tables or add a folder.\n"))
                return
            if len(sat_jobs) > 1 and (-1 in ports or len(set(ports)) < len(ports)):
                self.log_line(_("Each satellite needs a DiSEqC port of its own.") + "\n")
                return
            for n, (choice, port) in enumerate(sat_jobs):
                tpl = sl.satellite_list(choice, self.systems)
                if not tpl.transponders:
                    continue
                path = tmp / f"initial-{n}.conf"
                path.write_text(write_scan_file(tpl))
                target = root if root and len(sat_jobs) == 1 else \
                    (f"{root} {tpl.name}".strip() if root else tpl.name)
                jobs.append({"file": path, "total": len(tpl.transponders), "diseqc": port,
                             "root": target, "tpl": tpl, "out": tmp / f"channels-{n}.conf"})
        else:
            choice = self._region_choice()
            entries = sl.scan_entries(choice, self.systems) if choice else []
            if not entries:
                self.log_line(_("No transponder list selected. Install dtv-scan-tables or add a folder.\n"))
                return
            path = tmp / "initial.conf"
            path.write_text(sl.write_entries(entries))
            jobs.append({"file": path, "total": len(entries), "diseqc": -1,
                         "root": root or self._default_target(), "tpl": None,
                         "out": tmp / "channels.conf"})
        if not jobs:
            self.log_line(_("This transponder list has no satellite transponders.\n"))
            return
        self._remember()
        # Live TV on the same adapter would block the frontend.
        win = self.app.window
        if win.capture and win.capture.adapter == a:
            win.stop_playback()
        if self.satip is not None:
            self._start_satip(jobs)
            return
        if self.family == sl.SAT and self.unicable is not None:
            self._start_single_cable(jobs, a, f)
            return
        self.jobs = jobs
        self.total = sum(j["total"] for j in jobs)
        self.done = 0
        self.found = []
        self._run_next()

    def _run_next(self):
        if not self.jobs:
            self._all_done()
            return
        job = self.jobs[0]
        a, f = self._current_adapter()
        lnb = LNB_TYPES[self.lnb.get_selected()][1] if self.family == sl.SAT else ""
        try:
            cmd = scan_command(a, f, str(job["file"]), str(job["out"]), lnb, job["diseqc"],
                               self.nit.get_active())
        except FileNotFoundError as e:
            self.log_line(f"{e}\n")
            self.jobs = []
            return
        if job["tpl"] is not None:
            self.log_line(f"\n== {job['root']} ==\n")
        self.log_line("$ " + " ".join(cmd) + "\n")
        self.proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.STDOUT_PIPE |
                                       Gio.SubprocessFlags.STDERR_MERGE)
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _start_single_cable(self, jobs, a, f):
        """Unicable / JESS: LinDVB tunes each transponder itself (lindvb.scscan)."""
        import threading
        from .. import directtune, scscan, tunerprofiles, unicable
        from ..festats import Frontend
        work = [(scscan.transponders(j["tpl"]), j) for j in jobs]
        work = [(tps, j) for tps, j in work if tps]
        if not work:
            self.log_line(_("This transponder list has no satellite transponders.\n"))
            return
        if a in self.app.tuners.captures:
            self.log_line(_("The tuner is busy (recording or channel analysis).\n"))
            return
        uc = self.unicable
        self.total, self.done = sum(len(tps) for tps, _j in work), 0
        self._cancel = False
        self.log_line(unicable.describe(uc) + "\n")
        lnb = tunerprofiles.tuner_lnb(self._profile(a, f), self.app.settings)
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

        def log(text):
            def show():
                self.log_line(text + "\n")
                if text.startswith("Scanning frequency"):
                    self.done += 1
                    self.progress.set_fraction(min(1, self.done / self.total))
                    self.progress.set_text(f"{self.done} / {self.total}")
                return False
            GLib.idle_add(show)

        def run():
            tuner = directtune.DirectTuner(a, f)
            fe = Frontend(a, f)
            chans = []
            try:
                tuner.open()
                for tps, job in work:
                    if self._cancel:
                        break
                    log(f"== {job['root']} ==")
                    chans += scscan.scan(tuner, fe.status, uc, lnb, tps, max(job["diseqc"], 0),
                                         job["root"], log, cancelled=lambda: self._cancel)
            except OSError as e:
                log(f"Could not open the tuner: {e.strerror}")
            finally:
                fe.close()
                tuner.close()
            GLib.idle_add(lambda: (self._single_cable_done(chans), False)[1])

        threading.Thread(target=run, daemon=True, name="single-cable-scan").start()

    def _start_satip(self, jobs):
        """SAT>IP: LinDVB asks the server for each transponder (lindvb.satip)."""
        import threading
        from .. import satip, scscan
        work = []
        for j in jobs:
            if j["tpl"] is not None:
                tps = scscan.transponders(j["tpl"])
            else:
                try:
                    tps = satip.entry_transponders(parse_dvbv5(j["file"].read_text()))
                except OSError:
                    tps = []
            if tps:
                work.append((tps, j))
        if not work:
            self.log_line(_("This transponder list has no transponders for this tuner.\n"))
            return
        prof = self.satip
        self.total, self.done = sum(len(tps) for tps, _j in work), 0
        self._cancel = False
        self.log_line(f"SAT>IP {prof.get('server_label') or prof['host']}\n")
        if self.nit.get_active():
            self.log_line(_("SAT>IP: the search uses the frequencies of the list "
                            "(no NIT search).") + "\n")
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        log = self._thread_log

        def run():
            chans = []
            for tps, job in work:
                if self._cancel:
                    break
                log(f"== {job['root']} ==")
                chans += satip.scan(prof, tps, job["diseqc"], job["root"], log,
                                    cancelled=lambda: self._cancel)
            GLib.idle_add(lambda: (self._single_cable_done(chans), False)[1])

        threading.Thread(target=run, daemon=True, name="satip-scan").start()

    def _thread_log(self, text):
        """A log line from a search thread; counts the frequencies for the progress."""
        def show():
            self.log_line(text + "\n")
            if text.startswith("Scanning frequency"):
                self.done += 1
                self.progress.set_fraction(min(1, self.done / max(1, self.total)))
                self.progress.set_text(f"{self.done} / {self.total}")
            return False
        GLib.idle_add(show)

    def _single_cable_done(self, chans):
        self.start_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        if not chans:
            self.log_line(_("\nNo channels found.\n"))
            return
        self._merge(chans)

    def _on_line(self, stream, res):
        try:
            line, _len = stream.read_line_finish_utf8(res)
        except GLib.Error:
            line = None
        if line is None:
            self.proc.wait_async(None, self._finished)
            return
        self.log_line(line + "\n")
        if line.lstrip().startswith(("Scanning frequency", "Scanning")):
            self.done += 1
            if self.total:
                self.progress.set_fraction(min(1, self.done / self.total))
                self.progress.set_text(f"{min(self.done, self.total)} / {self.total}")
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _finished(self, proc, res):
        self.proc = None
        if self.jobs:
            job = self.jobs.pop(0)
            try:
                text = job["out"].read_text(errors="replace")
            except OSError:
                text = ""
            if text:
                self.found += parse_dvbv5(text, root=job["root"])
        self._run_next()

    def _all_done(self):
        self.start_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        found, self.found = getattr(self, "found", []), []
        if not found:
            self.log_line(_("\nNo channels found.\n"))
            return
        self._merge(found)

    def _merge(self, chans):
        grp = self.group_by.get_selected()
        for c in chans:
            if grp == 1:
                c.category = f"{c.frequency // 1000} MHz {c.polarity.upper()}"
            elif grp == 2:
                c.category = c.name[:1].upper()
        added, updated = self.app.db.merge(chans, update_only=self.upd_only.get_active(),
                                           fta_only=self.fta.get_active(),
                                           update_names=self.upd_names.get_active())
        self.app.db.save()
        self.app.window.reload_channels()
        self.progress.set_fraction(1)
        self.log_line(_("\nDone: {added} new, {updated} updated channels.\n").format(added=added, updated=updated))
