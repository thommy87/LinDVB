"""Reception settings and channel search (runs dvbv5-scan)."""

import tempfile
from pathlib import Path

from gi.repository import Gio, GLib, Gtk

from ..channels import parse_dvbv5
from ..scan import (find_adapters, find_transponder_lists, load_transponder_list,
                    scan_command, write_scan_file)
from .common import dropdown, labeled, scrolled
from ..i18n import _

LNB_TYPES = [(_("Universal (9750/10600, 11700)"), "UNIVERSAL"), (_("Single 10750"), "L10750"),
             (_("Single 10700"), "L10700"), (_("Single 11300"), "L11300"), ("C-Band", "C-BAND"),
             (_("Extended"), "EXTENDED"), (_("None / not satellite"), "")]
DISEQC = [_("None"), _("Port 1 (A/A)"), _("Port 2 (B/A)"), _("Port 3 (A/B)"), _("Port 4 (B/B)")]
GROUP_BY = [_("Provider"), _("Frequency"), _("Initial letter")]


class ScanWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(title=_("Channel Search"), transient_for=parent,
                         default_width=820, default_height=620)
        self.app = app
        self.proc = None
        self.lists = []

        g = Gtk.Grid(row_spacing=6, column_spacing=8, margin_start=12, margin_end=12, margin_top=12)
        adapters = find_adapters()
        self.adapters = adapters or [(a, app.settings["frontend"], f"Adapter {a}")
                                     for a in app.settings["adapters"]]
        self.adapter = labeled(g, 0, _("Device"), dropdown([f"{n} (adapter{a}/frontend{f})"
                                                         for a, f, n in self.adapters]))
        lbox = Gtk.Box(spacing=6)
        self.list_dd = Gtk.DropDown.new_from_strings([_("(choose…)")])
        self.list_dd.set_hexpand(True)
        self.list_dd.set_enable_search(True)
        self.list_dd.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        add = Gtk.Button(label=_("Add folder…"), tooltip_text=_("Folder with transponder lists "
                         "(dtv-scan-tables or DVBViewer style .ini)"))
        add.connect("clicked", lambda *_: self.on_add_folder())
        lbox.append(self.list_dd)
        lbox.append(add)
        labeled(g, 1, _("Transponder list"), lbox)
        self.lnb = labeled(g, 2, _("LNB type"), dropdown([n for n, _unused in LNB_TYPES]))
        self.diseqc = labeled(g, 3, _("DiSEqC"), dropdown(DISEQC))
        self.root = labeled(g, 4, _("Target group"), Gtk.Entry(placeholder_text=_("e.g. Astra 19.2°E")))
        opts = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2)
        self.nit = Gtk.CheckButton(label=_("Search for additional frequencies (NIT)"))
        self.fta = Gtk.CheckButton(label=_("Only add free-to-air channels"))
        self.upd_only = Gtk.CheckButton(label=_("Only update existing channels"))
        self.upd_names = Gtk.CheckButton(label=_("Update channel names"), active=True)
        for w in (self.nit, self.fta, self.upd_only, self.upd_names):
            opts.append(w)
        g.attach(opts, 0, 5, 2, 1)
        self.group_by = labeled(g, 6, _("Group by"), dropdown(GROUP_BY))

        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12)
        self.start_btn = Gtk.Button(label=_("Start Search"), css_classes=["suggested-action"])
        self.start_btn.connect("clicked", lambda *_: self.on_start())
        self.stop_btn = Gtk.Button(label=_("Stop"), sensitive=False)
        self.stop_btn.connect("clicked", lambda *_: self.proc and self.proc.force_exit())
        self.progress = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER, show_text=True)
        for w in (self.start_btn, self.stop_btn, self.progress):
            bar.append(w)

        self.log = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(g)
        box.append(bar)
        sw = scrolled(self.log)
        sw.set_margin_start(12); sw.set_margin_end(12); sw.set_margin_bottom(12)
        box.append(sw)
        self.set_child(box)
        self._reload_lists()

    def _reload_lists(self, extra=()):
        dirs = list(extra) + self.app.settings.get("transponder_dirs", [])
        self.lists = find_transponder_lists(dirs)
        self.list_dd.set_model(Gtk.StringList.new([n for n, _ in self.lists] or ["(no lists found)"]))

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

    def log_line(self, text):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self.log.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 0)

    def on_start(self):
        if not self.lists:
            self.log_line(_("No transponder list selected. Install dtv-scan-tables or add a folder.\n"))
            return
        name, path = self.lists[self.list_dd.get_selected()]
        tpl = load_transponder_list(path)
        diseqc = self.diseqc.get_selected() - 1
        tmp = Path(tempfile.mkdtemp(prefix="lindvb-scan-"))
        if tpl.transponders:
            scan_file = tmp / "initial.conf"
            scan_file.write_text(write_scan_file(tpl, diseqc))
            self.total = len(tpl.transponders)
        else:
            scan_file, self.total = Path(path), 0
        self.out_file = tmp / "channels.conf"
        if not self.root.get_text().strip():
            self.root.set_text(tpl.name)
        a, f, _unused = self.adapters[self.adapter.get_selected()]
        lnb = LNB_TYPES[self.lnb.get_selected()][1] if tpl.kind == "sat" else ""
        try:
            cmd = scan_command(a, f, str(scan_file), str(self.out_file), lnb, diseqc,
                               self.nit.get_active())
        except FileNotFoundError as e:
            self.log_line(f"{e}\n")
            return
        # Live TV on the same adapter would block the frontend.
        win = self.app.window
        if win.capture and win.capture.adapter == a:
            win.stop_playback()
        self.log_line("$ " + " ".join(cmd) + "\n")
        self.done = 0
        self.proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.STDOUT_PIPE |
                                       Gio.SubprocessFlags.STDERR_MERGE)
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _on_line(self, stream, res):
        try:
            line, _ = stream.read_line_finish_utf8(res)
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
                self.progress.set_text(f"{self.done} / {self.total}")
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _finished(self, proc, res):
        self.start_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        try:
            text = self.out_file.read_text(errors="replace")
        except OSError:
            self.log_line(_("\nNo channels found.\n"))
            return
        chans = parse_dvbv5(text, root=self.root.get_text().strip())
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
