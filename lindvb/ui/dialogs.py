"""Preferences, channel editor, recording statistics and teletext windows."""

import time
from pathlib import Path

from gi.repository import GLib, Gst, Gtk

from ..channels import Channel
from ..tuner import SegmentReader
from .common import dropdown, fill, labeled, make_column_view, scrolled, selected

DELSYS = ["dvb-s", "dvb-s2", "dvb-c-a", "dvb-c-b", "dvb-t", "dvb-t2", "atsc", "isdb-t", "dtmb", "iptv"]
MODS = ["auto", "qpsk", "8psk", "16apsk", "32apsk", "qam-16", "qam-32", "qam-64", "qam-128",
        "qam-256", "8vsb"]


class PrefsWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(title="Preferences", transient_for=parent, default_width=560)
        self.app = app
        s = app.settings
        g = Gtk.Grid(row_spacing=6, column_spacing=10, margin_start=12, margin_end=12,
                     margin_top=12, margin_bottom=12)
        self.w = {}

        def entry(key, row, label):
            e = labeled(g, row, label, Gtk.Entry(text=str(s[key])))
            self.w[key] = (e, str)

        def spin(key, row, label, lo, hi):
            sp = labeled(g, row, label, Gtk.SpinButton.new_with_range(lo, hi, 1))
            sp.set_value(s[key])
            self.w[key] = (sp, int)

        def switch(key, row, label):
            sw = labeled(g, row, label, Gtk.Switch(active=bool(s[key]), halign=Gtk.Align.START))
            self.w[key] = (sw, bool)

        entry("adapters", 0, "DVB adapters (comma separated)")
        spin("frontend", 1, "Frontend", 0, 7)
        spin("lnb_lof1", 2, "LNB LOF low (kHz)", 0, 20000000)
        spin("lnb_lof2", 3, "LNB LOF high (kHz)", 0, 20000000)
        spin("lnb_slof", 4, "LNB switch frequency (kHz)", 0, 20000000)
        switch("full_transponder", 5, "Capture whole transponder")
        entry("recording_dir", 6, "Recording folder")
        entry("name_scheme", 7, "File naming scheme")
        g.attach(Gtk.Label(label="Placeholders: %year %date %time %station %event",
                           css_classes=["dim-label"], xalign=0), 1, 8, 1, 1)
        spin("lead_minutes", 9, "Lead time (min)", 0, 60)
        spin("follow_minutes", 10, "Follow-up time (min)", 0, 180)
        entry("timeshift_dir", 11, "Timeshift folder")
        spin("timeshift_max_mb", 12, "Timeshift buffer (MB)", 128, 200000)
        entry("screenshot_dir", 13, "Screenshot folder")
        entry("xmltv_url", 14, "XMLTV URL (internet EPG)")
        switch("deinterlace", 15, "Deinterlace video")
        spin("osd_seconds", 16, "OSD display time (s)", 1, 30)

        g.attach(Gtk.Label(label="Conditional access (your own encrypted channels)",
                           css_classes=["heading"], xalign=0), 0, 17, 2, 1)
        self._ca_names = ["none", "dvbapi", "ci"] + \
            [n for n in app.plugins.ca_providers if n not in ("dvbapi", "ci")]
        ca_labels = {"none": "None (leave scrambled)",
                     "dvbapi": "External softcam (DVBAPI / OSCam)",
                     "ci": "Hardware CI / CI+ module"}
        cur = s.get("ca_provider", "none")
        idx = self._ca_names.index(cur) if cur in self._ca_names else 0
        self._ca_drop = labeled(g, 18, "Descrambling via",
                                dropdown([ca_labels.get(n, n) for n in self._ca_names], idx))
        entry("ca_dvbapi_socket", 19, "Softcam DVBAPI socket (path or host:port)")
        spin("ca_ci_adapter", 20, "CI adapter number", 0, 15)
        spin("ca_ci_device", 21, "CI CA device number", 0, 7)
        switch("plugins_enabled", 22, "Load plugins at startup")

        self.w["adapters"][0].set_text(", ".join(map(str, s["adapters"])))
        save = Gtk.Button(label="Save", css_classes=["suggested-action"], halign=Gtk.Align.END)
        save.connect("clicked", lambda *_: self.on_save())
        g.attach(save, 1, 23, 1, 1)
        self.set_child(scrolled(g))
        self.set_default_size(560, 640)

    def on_save(self):
        s = self.app.settings
        s["ca_provider"] = self._ca_names[self._ca_drop.get_selected()]
        for k, (w, typ) in self.w.items():
            if typ is bool:
                s[k] = w.get_active()
            elif typ is int:
                s[k] = int(w.get_value())
            elif k == "adapters":
                s[k] = [int(x) for x in w.get_text().replace(" ", "").split(",") if x.isdigit()]
            else:
                s[k] = w.get_text()
        s.save()
        self.close()


class EditorWindow(Gtk.Window):
    """Channel editor: rename, reorder, favourites, tuning data, delete."""

    def __init__(self, app, parent, channel=None):
        super().__init__(title="Channel Editor", transient_for=parent,
                         default_width=980, default_height=620)
        self.app = app
        cols = [("#", lambda c: str(c.number), False), ("Name", lambda c: c.name, True),
                ("Group", lambda c: c.root, False), ("Category", lambda c: c.category, False),
                ("Fav", lambda c: "★" if c.favourite else "", False),
                ("Type", lambda c: c.delsys, False)]
        self.view, self.store, self.sel = make_column_view(cols)
        self.sel.connect("notify::selected-item", lambda *_: self._load(selected(self.sel)))
        self.filter = Gtk.SearchEntry(placeholder_text="Filter")
        self.filter.connect("search-changed", lambda *_: self.refresh())

        g = Gtk.Grid(row_spacing=4, column_spacing=8, margin_start=10, margin_end=10, margin_top=10)
        self.f = {}
        texts = [("name", "Name"), ("root", "Group"), ("category", "Category"), ("url", "URL")]
        nums = [("frequency", "Frequency (kHz)"), ("symbol_rate", "Symbol rate (kBd)"),
                ("bandwidth_hz", "Bandwidth (Hz)"), ("stream_id", "Stream ID"),
                ("diseqc", "DiSEqC port (-1 none)"), ("sid", "Service ID"), ("tsid", "Transport ID"),
                ("pmt_pid", "PMT PID"), ("video_pid", "Video PID"), ("pcr_pid", "PCR PID"),
                ("teletext_pid", "Teletext PID")]
        r = 0
        for k, lbl in texts:
            self.f[k] = labeled(g, r, lbl, Gtk.Entry())
            r += 1
        self.delsys = labeled(g, r, "System", dropdown(DELSYS)); r += 1
        self.pol = labeled(g, r, "Polarity", dropdown(["", "h", "v", "l", "r"])); r += 1
        self.mod = labeled(g, r, "Modulation", dropdown(MODS)); r += 1
        for k, lbl in nums:
            self.f[k] = labeled(g, r, lbl, Gtk.Entry())
            r += 1
        self.f["audio_pids"] = labeled(g, r, "Audio PIDs", Gtk.Entry()); r += 1
        self.fav = labeled(g, r, "Favourite", Gtk.Switch(halign=Gtk.Align.START)); r += 1
        self.enc = labeled(g, r, "Encrypted", Gtk.Switch(halign=Gtk.Align.START)); r += 1
        self.radio = labeled(g, r, "Radio", Gtk.Switch(halign=Gtk.Align.START)); r += 1
        bar = Gtk.Box(spacing=4)
        for label, fn in (("Apply", self.on_apply), ("New", self.on_new), ("Delete", self.on_delete),
                          ("Up", lambda: self.on_move(-1)), ("Down", lambda: self.on_move(1)),
                          ("Sort A–Z", self.on_sort)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, fn=fn: fn())
            bar.append(b)
        g.attach(bar, 0, r, 2, 1)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.append(self.filter)
        left.append(scrolled(self.view))
        paned = Gtk.Paned(position=560)
        paned.set_start_child(left)
        paned.set_end_child(scrolled(g))
        self.set_child(paned)
        self.refresh()
        if channel:
            self._load(channel)
            self.current = channel

    def refresh(self):
        q = self.filter.get_text().lower()
        fill(self.store, [c for c in self.app.db.channels if q in c.name.lower()])

    def _load(self, c):
        self.current = c
        if not c:
            return
        for k, w in self.f.items():
            v = getattr(c, k)
            w.set_text(" ".join(map(str, v)) if isinstance(v, list) else str(v))
        self.delsys.set_selected(DELSYS.index(c.delsys) if c.delsys in DELSYS else 0)
        pols = ["", "h", "v", "l", "r"]
        self.pol.set_selected(pols.index(c.polarity) if c.polarity in pols else 0)
        self.mod.set_selected(MODS.index(c.modulation) if c.modulation in MODS else 0)
        self.fav.set_active(c.favourite)
        self.enc.set_active(c.encrypted)
        self.radio.set_active(c.radio)

    def _save_into(self, c):
        for k, w in self.f.items():
            t = w.get_text().strip()
            cur = getattr(c, k)
            if isinstance(cur, list):
                setattr(c, k, [int(x) for x in t.split() if x.lstrip("-").isdigit()])
            elif isinstance(cur, int):
                try:
                    setattr(c, k, int(t))
                except ValueError:
                    pass
            else:
                setattr(c, k, t)
        c.delsys = DELSYS[self.delsys.get_selected()]
        c.polarity = ["", "h", "v", "l", "r"][self.pol.get_selected()]
        c.modulation = MODS[self.mod.get_selected()]
        c.favourite, c.encrypted, c.radio = self.fav.get_active(), self.enc.get_active(), self.radio.get_active()

    def _commit(self):
        self.app.db.renumber()
        self.app.db.save()
        self.refresh()
        self.app.window.reload_channels()

    def on_apply(self):
        if self.current:
            self._save_into(self.current)
            self._commit()

    def on_new(self):
        c = Channel(name="New channel", delsys="iptv")
        self.app.db.channels.append(c)
        self._load(c)
        self._commit()

    def on_delete(self):
        c = selected(self.sel)
        if c:
            self.app.db.channels.remove(c)
            self._commit()

    def on_move(self, step):
        c = selected(self.sel)
        if not c:
            return
        ch = self.app.db.channels
        i = ch.index(c)
        j = i + step
        if 0 <= j < len(ch):
            ch[i], ch[j] = ch[j], ch[i]
            self._commit()
            self.sel.set_selected(j)

    def on_sort(self):
        self.app.db.channels.sort(key=lambda c: (c.root, c.name.lower()))
        self._commit()


class StatsWindow(Gtk.Window):
    """Recording and device statistics."""

    def __init__(self, app, parent):
        super().__init__(title="Recording and Device Statistics", transient_for=parent,
                         default_width=620, default_height=360)
        self.app = app
        self.label = Gtk.Label(xalign=0, yalign=0, selectable=True, margin_start=12, margin_top=12)
        self.label.add_css_class("monospace")
        self.set_child(scrolled(self.label))
        self._src = GLib.timeout_add_seconds(1, self.update)
        self.connect("close-request", lambda *_: (GLib.source_remove(self._src), False)[1])
        self.update()

    def update(self):
        lines = ["Tuners:"]
        for a, cap in sorted(self.app.tuners.captures.items()):
            st = cap.stats
            start, end = cap.store.bounds()
            lines.append(f"  adapter{a}: {cap.channel.name}  users={','.join(sorted(cap.users))}")
            lines.append(f"    lock={st.get('lock', '?')} signal={st.get('signal', '?')} "
                         f"snr={st.get('snr', '?')} ber={st.get('ber', '?')} unc={st.get('unc', '?')}")
            lines.append(f"    rate={cap.byte_rate() * 8 / 1e6:.2f} Mbit/s  "
                         f"buffer={(end - start) / 1e6:.0f} MB")
        if len(lines) == 1:
            lines.append("  (idle)")
        lines.append("")
        lines.append("Active recordings:")
        for key, (rec, _cap) in self.app.timers.active.items():
            size = rec.bytes if rec.capture else (rec.path.stat().st_size if rec.path.exists() else 0)
            lines.append(f"  {rec.path.name}: {size / 1e6:.1f} MB, "
                         f"{int(time.time() - rec.started) // 60} min")
        if not self.app.timers.active:
            lines.append("  (none)")
        nxt = self.app.timers.next_wakeup()
        if nxt:
            lines.append(f"\nNext timer: {time.strftime('%a %d.%m. %H:%M', time.localtime(nxt))}")
        self.label.set_text("\n".join(lines))
        return True


class TeletextWindow(Gtk.Window):
    """Teletext pages decoded with GStreamer's teletextdec (libzvbi)."""

    def __init__(self, app, parent, capture, channel):
        super().__init__(title=f"Teletext – {channel.name}", transient_for=parent,
                         default_width=560, default_height=620)
        self.capture = capture
        self.channel = channel
        self.page = 100
        self.history = []

        top = Gtk.Box(spacing=4, margin_start=6, margin_end=6, margin_top=6)
        self.page_e = Gtk.SpinButton.new_with_range(100, 899, 1)
        self.page_e.set_value(100)
        self.page_e.connect("activate", lambda *_: self.goto(int(self.page_e.get_value())))
        prev = Gtk.Button(icon_name="go-previous-symbolic")
        prev.connect("clicked", lambda *_: self.goto(self.page - 1))
        nxt = Gtk.Button(icon_name="go-next-symbolic")
        nxt.connect("clicked", lambda *_: self.goto(self.page + 1))
        back = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Back")
        back.connect("clicked", lambda *_: self.history and self.goto(self.history.pop(), False))
        self.find_e = Gtk.SearchEntry(placeholder_text="Find on page", hexpand=True)
        self.find_e.connect("activate", lambda *_: self.find())
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy page")
        copy.connect("clicked", lambda *_: self.get_clipboard().set(self.text.get_buffer().props.text))
        save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Save page")
        save.connect("clicked", lambda *_: self.save())
        for w in (prev, self.page_e, nxt, back, self.find_e, copy, save):
            top.append(w)
        self.text = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        self.text.get_buffer().set_text("Waiting for teletext…")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(top)
        box.append(scrolled(self.text))
        self.set_child(box)
        self.pipeline = None
        if Gst.ElementFactory.find("teletextdec") is None:
            self.text.get_buffer().set_text("teletextdec is missing (gst-plugins-bad with libzvbi).")
        else:
            self._build()
        self.connect("close-request", lambda *_: (self._teardown(), False)[1])

    def _build(self):
        self._teardown()
        p = Gst.Pipeline.new("teletext")
        src = Gst.ElementFactory.make("appsrc")
        src.set_property("caps", Gst.Caps.from_string("video/mpegts,systemstream=(boolean)true,packetsize=(int)188"))
        src.set_property("format", Gst.Format.BYTES)
        src.set_property("block", True)
        demux = Gst.ElementFactory.make("tsdemux")
        demux.set_property("program-number", self.channel.sid)
        self.dec = Gst.ElementFactory.make("teletextdec")
        self.dec.set_property("page", self.page)
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("caps", Gst.Caps.from_string("text/x-raw,format=utf-8"))
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.connect("new-sample", self._on_sample)
        for e in (src, demux, self.dec, sink):
            p.add(e)
        src.link(demux)
        self.dec.link(sink)
        demux.connect("pad-added", self._pad_added)
        self.pipeline = p
        _, end = self.capture.store.bounds()
        start = max(0, end - int(self.capture.byte_rate() * 20))
        from ..player import Feeder
        self.feeder = Feeder(SegmentReader(self.capture.store, start), src)
        p.set_state(Gst.State.PLAYING)
        self.feeder.start()

    def _pad_added(self, _d, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps and caps.get_structure(0).get_name() == "application/x-teletext":
            pad.link(self.dec.get_static_pad("sink"))

    def _on_sample(self, sink):
        s = sink.emit("pull-sample")
        buf = s.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if ok:
            text = bytes(m.data).decode("utf-8", "replace")
            buf.unmap(m)
            GLib.idle_add(self.text.get_buffer().set_text, text)
        return Gst.FlowReturn.OK

    def goto(self, page, remember=True):
        page = min(899, max(100, page))
        if remember:
            self.history.append(self.page)
        self.page = page
        self.page_e.set_value(page)
        self.text.get_buffer().set_text(f"Searching page {page}…")
        if self.pipeline:
            self.dec.set_property("page", page)

    def find(self):
        buf = self.text.get_buffer()
        q = self.find_e.get_text()
        start = buf.get_insert()
        it = buf.get_iter_at_mark(start)
        res = it.forward_search(q, Gtk.TextSearchFlags.CASE_INSENSITIVE, None) or \
            buf.get_start_iter().forward_search(q, Gtk.TextSearchFlags.CASE_INSENSITIVE, None)
        if res:
            buf.select_range(res[1], res[0])
            self.text.scroll_to_iter(res[0], 0, False, 0, 0)

    def save(self):
        d = Path.home() / "Documents"
        d.mkdir(exist_ok=True)
        p = d / f"teletext_{self.channel.name}_{self.page}_{time.strftime('%Y%m%d_%H%M')}.txt"
        p.write_text(self.text.get_buffer().props.text)

    def _teardown(self):
        if self.pipeline:
            self.feeder.stop()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

class PluginsWindow(Gtk.Window):
    """Lists the loaded plugins and the conditional-access status."""

    def __init__(self, app, parent):
        super().__init__(title="Plugins", transient_for=parent, default_width=560,
                         default_height=440)
        self.app = app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                      margin_end=12, margin_top=12, margin_bottom=12)

        pm = app.plugins
        box.append(Gtk.Label(label="Conditional access", css_classes=["heading"], xalign=0))
        provider = app.tuners.ca_factory() if app.tuners.ca_factory else None
        if app.settings.get("ca_provider", "none") == "none":
            ca_text = ("No descrambling configured. Encrypted channels you are entitled to "
                       "can be descrambled by pointing LinDVB at your own softcam or CI module "
                       "in Preferences.")
        elif provider is None:
            ca_text = f"Provider '{app.settings['ca_provider']}' is not available."
        else:
            ca_text = (f"Provider: {provider.name}\n"
                       "LinDVB forwards ECM sections to your external card client and applies "
                       "the control words it returns; it holds no keys itself.")
        lab = Gtk.Label(label=ca_text, xalign=0, wrap=True, css_classes=["dim-label"])
        box.append(lab)

        box.append(Gtk.Label(label="Loaded plugins", css_classes=["heading"], xalign=0))
        if pm.plugins:
            for p in pm.plugins:
                row = f"• {p.name} {p.version}".rstrip()
                if p.description:
                    row += f"\n    {p.description.splitlines()[0]}"
                box.append(Gtk.Label(label=row, xalign=0, wrap=True))
        else:
            box.append(Gtk.Label(label="No plugins loaded.", xalign=0,
                                  css_classes=["dim-label"]))

        dirs = ", ".join(str(d) for d in pm.plugin_dirs())
        box.append(Gtk.Label(label=f"Plugin folders: {dirs}", xalign=0, wrap=True,
                             css_classes=["dim-label"]))
        self.set_child(scrolled(box))
