# SPDX-License-Identifier: GPL-2.0-or-later
"""Channel analysis window (right click on a channel ▸ Channel Analysis…).

Seven pages over one AnalysisSession (lindvb.analysis): Overview, Signal,
Transport Stream, Video & Audio, Tables & EPG, Encryption and HbbTV; plus the
dish alignment aid and the comparison of two channels. Every page draws only
while it is shown.
"""

import locale
import math
import time
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, GObject, Gst, Gtk

from .. import sitree
from ..analysis import AnalysisSession, ca_lines, report_json
from ..esparse import AFD_NAMES
from ..i18n import _
from ..tsanalysis import NULL_PID, ca_system_name, pid_owners
from . import charts
from .charts import (BAD, FAIR, GOOD, OTHER, PALETTE, POOR, Bar, Chart, EventStrip,
                     FrameBars, Ring, ShareBar, Sparkline, level_colour)
from .theme import resolve_accent

WINDOWS = [(60, _("1 minute")), (300, _("5 minutes")), (900, _("15 minutes"))]
AIT_CODES = {1: "AUTOSTART", 2: "PRESENT", 3: "DESTROY", 4: "KILL", 5: "PREFETCH",
             6: "REMOTE", 7: "DISABLED", 8: "PLAYBACK_AUTOSTART"}

CSS = """
window.lv-analysis, .lv-analysis .lv-an-body { background: @lv_bg; color: @lv_text; }
.lv-analysis .lv-card listview, .lv-analysis .lv-card textview, .lv-analysis .lv-card text,
.lv-analysis .lv-card scrolledwindow, .lv-analysis .lv-card viewport { background: @lv_surface_2; }
.lv-analysis .lv-an-bar { background: @lv_surface; border-bottom: 1px solid alpha(white, 0.06);
  padding: 8px 14px; }
.lv-analysis .lv-an-foot { background: @lv_surface; border-top: 1px solid alpha(white, 0.06);
  padding: 8px 14px; }
.lv-analysis .lv-card { background: @lv_surface; border: 1px solid alpha(white, 0.06);
  border-radius: 12px; padding: 12px 14px; }
.lv-analysis .lv-card-title { font-size: 0.78em; font-weight: 700; letter-spacing: 1px;
  color: @lv_text_dim; }
.lv-analysis .lv-dim { color: @lv_text_dim; }
.lv-analysis .lv-big { font-size: 1.9em; font-weight: 700; font-feature-settings: "tnum"; }
.lv-analysis .lv-huge { font-size: 9em; font-weight: 800; font-feature-settings: "tnum"; }
.lv-analysis .lv-unit { color: @lv_text_dim; }
.lv-analysis .lv-tnum { font-feature-settings: "tnum"; }
.lv-analysis .lv-mono { font-family: monospace; font-size: 0.9em; }
.lv-analysis .lv-th { color: @lv_text_dim; font-size: 0.8em; font-weight: 600; }
.lv-analysis .lv-chip { font-size: 0.8em; font-weight: 600; padding: 2px 8px; border-radius: 6px;
  background: alpha(white, 0.08); color: @lv_text_dim; }
.lv-analysis .lv-chip.lv-g { background: alpha(#3FBF5F, 0.16); color: #3FBF5F; }
.lv-analysis .lv-chip.lv-a { background: alpha(@lv_accent, 0.18); color: @lv_accent; }
.lv-analysis .lv-chip.lv-y { background: alpha(#F2CC0C, 0.16); color: #F2CC0C; }
.lv-analysis .lv-chip.lv-r { background: alpha(#E5484D, 0.18); color: #E5484D; }
.lv-analysis .lv-live-dot { color: #3FBF5F; }
.lv-analysis .lv-paused-dot { color: #F2CC0C; }
.lv-analysis .lv-banner { background: #2A2614; color: @lv_text; border-radius: 10px; padding: 8px 12px;
  margin: 10px 14px 0 14px; }
.lv-analysis .lv-row-sel { background: alpha(@lv_accent, 0.12); border-radius: 6px; }
.lv-analysis .lv-cell { padding: 4px 6px; }
.lv-analysis .lv-cell-btn { padding: 0; min-height: 0; background: none; box-shadow: none;
  border: none; }
.lv-analysis .lv-cell-btn:hover { background: alpha(white, 0.05); }
"""
_css_done = False


def _install_css():
    global _css_done
    if _css_done:
        return
    prov = Gtk.CssProvider()
    if hasattr(prov, "load_from_string"):
        prov.load_from_string(CSS)
    else:
        prov.load_from_data(CSS.encode())
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_done = True


# --------------------------------------------------------------------------
#  formatting
# --------------------------------------------------------------------------

def rate(bps):
    if bps is None:
        return "–"
    if bps >= 1e6:
        return f"{bps / 1e6:.2f} Mbit/s".replace(".", _dec())
    return f"{bps / 1e3:.0f} kbit/s"


def _dec():
    try:
        return locale.localeconv()["decimal_point"] or "."
    except (ValueError, locale.Error):
        return "."


def num(v, digits=1, unit=""):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    s = f"{v:.{digits}f}".replace(".", _dec())
    return f"{s} {unit}".strip()


def ber_text(v):
    if v is None:
        return "–"
    if v <= 0:
        return "0"
    exp = math.floor(math.log10(v))
    return f"{v / 10 ** exp:.1f} × 10^{exp}".replace(".", _dec())


def duration(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def label(text="", css=(), xalign=0.0, **kw):
    lab = Gtk.Label(label=text, xalign=xalign, css_classes=list(css), **kw)
    return lab


def chip(text, kind=""):
    lab = Gtk.Label(label=text, css_classes=["lv-chip"] + ([f"lv-{kind}"] if kind else []),
                    halign=Gtk.Align.START)
    return lab


def set_chip(lab, text, kind=""):
    lab.set_text(text)
    for k in ("g", "a", "y", "r"):
        lab.remove_css_class(f"lv-{k}")
    if kind:
        lab.add_css_class(f"lv-{kind}")


def card(title, extra=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["lv-card"])
    head = Gtk.Box(spacing=8)
    head.append(label(title.upper(), ["lv-card-title"], hexpand=True))
    if extra is not None:
        head.append(extra)
    box.append(head)
    return box


def subtitle(text):
    return label(text.upper(), ["lv-card-title"], margin_top=6)


class KV(Gtk.Grid):
    """A two-column list of names and values, set by key."""

    def __init__(self):
        super().__init__(column_spacing=14, row_spacing=4)
        self._rows = {}

    def row(self, key, name):
        n = len(self._rows)
        k = label(name, ["lv-dim"])
        v = label("–", ["lv-tnum"], wrap=True, hexpand=True, selectable=False)
        self.attach(k, 0, n, 1, 1)
        self.attach(v, 1, n, 1, 1)
        self._rows[key] = (k, v)
        return v

    def set(self, key, value, visible=True):
        k, v = self._rows[key]
        v.set_text(value if value not in (None, "") else "–")
        k.set_visible(visible)
        v.set_visible(visible)


class Table(Gtk.Grid):
    """Rows of cells updated by key. Column kinds: text, chip, bar, spark, swatch."""

    def __init__(self, columns, on_activate=None):
        super().__init__(column_spacing=4, row_spacing=0)
        self.columns = columns        # [(title, kind, xalign, expand)]
        self.on_activate = on_activate
        self._rows = {}               # key -> [widgets]
        self._order = []
        self._selected = None
        self._heads = []
        for i, (title, _kind, xalign, expand) in enumerate(columns):
            head = label(title, ["lv-th", "lv-cell"], xalign=xalign, hexpand=expand)
            self._heads.append(head)
            self.attach(head, i, 0, 1, 1)

    def _make(self, kind, xalign, expand):
        if kind == "bar":
            box = Gtk.Box(spacing=8, css_classes=["lv-cell"], hexpand=expand)
            bar = Bar(width=90)
            bar.set_valign(Gtk.Align.CENTER)
            box.append(bar)
            box.append(label("", ["lv-tnum"], xalign=1, width_chars=11))
            return box
        if kind == "spark":
            return Sparkline()
        if kind == "chip":
            box = Gtk.Box(css_classes=["lv-cell"])
            box.append(chip(""))
            return box
        if kind == "swatch":
            box = Gtk.Box(spacing=7, css_classes=["lv-cell"], hexpand=expand)
            sw = Gtk.DrawingArea(content_width=9, content_height=9, valign=Gtk.Align.CENTER)
            sw.colour = OTHER

            def draw(_a, cr, w, h, sw=sw):
                cr.set_source_rgb(*charts.rgb(sw.colour))
                cr.rectangle(0, 0, w, h)
                cr.fill()
            sw.set_draw_func(draw)
            box.append(sw)
            box.append(label("", ellipsize=3, hexpand=True))
            return box
        return label("", ["lv-cell", "lv-tnum"], xalign=xalign, hexpand=expand, ellipsize=3)

    def _fill(self, widgets, values):
        for (title, kind, _x, _e), w, v in zip(self.columns, widgets, values):
            if kind == "bar":
                frac, colour, text = v
                w.get_first_child().set_value(frac, colour)
                w.get_last_child().set_text(text)
            elif kind == "spark":
                vals, colour = v
                w.set_values(vals, colour)
            elif kind == "chip":
                text, k = v
                set_chip(w.get_first_child(), text, k)
                w.get_first_child().set_visible(bool(text))
            elif kind == "swatch":
                colour, text = v
                sw = w.get_first_child()
                sw.colour = colour or OTHER
                sw.set_visible(bool(colour))
                sw.queue_draw()
                w.get_last_child().set_text(text)
            else:
                w.set_text(str(v))

    def set_rows(self, rows, selected=None):
        """rows: [(key, [values])] in display order."""
        for head in self._heads:
            head.set_visible(bool(rows))
        keys = [k for k, _v in rows]
        if keys != self._order:
            for k, widgets in list(self._rows.items()):
                for w in widgets:
                    self.remove(w)
                if k not in keys:
                    del self._rows[k]
            for r, (k, _v) in enumerate(rows, 1):
                widgets = self._rows.get(k)
                if widgets is None:
                    widgets = [self._make(kind, x, e) for _t, kind, x, e in self.columns]
                    if self.on_activate:
                        for w in widgets:
                            g = Gtk.GestureClick()
                            g.connect("released", lambda *_a, k=k: self.on_activate(k))
                            w.add_controller(g)
                    self._rows[k] = widgets
                for c, w in enumerate(widgets):
                    self.attach(w, c, r, 1, 1)
            self._order = keys
        for k, values in rows:
            self._fill(self._rows[k], values)
        if selected != self._selected:
            for k, widgets in self._rows.items():
                for w in widgets:
                    (w.add_css_class if k == selected else w.remove_css_class)("lv-row-sel")
            self._selected = selected


def scroll(child):
    sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["lv-an-body"],
                            hscrollbar_policy=Gtk.PolicyType.NEVER)
    sw.set_child(child)
    sw.get_child().add_css_class("lv-an-body")
    return sw


def page_box():
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=14,
                   margin_end=14, margin_top=14, margin_bottom=14)


def layer_colour(i, kind, accent):
    if kind == "video":
        return accent
    if kind == "other":
        return OTHER
    return PALETTE[i % len(PALETTE)]


def stream_label(d, pid):
    lang = d.get("language", "")
    bits = []
    if d["kind"] == "audio":
        bits.append(_("Audio"))
        if d.get("audio_description"):
            bits.append(_("audio description"))
    elif d["kind"] == "video":
        bits.append(_("Video"))
    elif d["kind"] == "teletext":
        bits.append(_("Teletext"))
    elif d["kind"] == "subtitle":
        bits.append(_("DVB subtitles"))
        if d.get("hard_of_hearing"):
            bits.append(_("hard of hearing"))
    else:
        bits.append(d.get("codec", _("Data")))
    if lang:
        bits.append(lang)
    return " · ".join(bits)


# --------------------------------------------------------------------------
#  pages
# --------------------------------------------------------------------------

class Page:
    name = ""
    title = ""

    def __init__(self, win):
        self.win = win
        self.session = win.session
        self.widget = scroll(self.build())

    def build(self):
        return Gtk.Box()

    def update(self, st):
        pass

    @property
    def accent(self):
        return self.win.accent


class SignalCard:
    """The signal column of the overview (DVB) or the stream column (IPTV)."""

    def __init__(self, page):
        self.page = page
        self.box = card(_("Signal"), extra=None)
        self.title = self.box.get_first_child().get_first_child()
        self.lock_chip = chip("LOCK", "g")
        self.box.get_first_child().append(self.lock_chip)
        top = Gtk.Box(spacing=14)
        self.ring = Ring()
        top.append(self.ring)
        meters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9, hexpand=True,
                         valign=Gtk.Align.CENTER)
        self.meters = {}
        for key, name in (("level", _("Level")), ("cnr", "C/N"), ("margin", _("Margin"))):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            head = Gtk.Box()
            nl = label(name, ["lv-dim"], hexpand=True)
            vl = label("–", ["lv-tnum"], xalign=1)
            head.append(nl)
            head.append(vl)
            bar = Bar()
            row.append(head)
            row.append(bar)
            meters.append(row)
            self.meters[key] = (nl, vl, bar)
        top.append(meters)
        self.box.append(top)
        self.locks = Gtk.Box(spacing=6)
        self.lock_chips = {}
        for key, name in (("signal", _("Signal")), ("carrier", _("Carrier")), ("viterbi", "FEC"),
                          ("sync", "Sync"), ("lock", "Lock")):
            c = chip(name)
            self.lock_chips[key] = c
            self.locks.append(c)
        self.box.append(self.locks)
        self.kv = KV()
        self.kv.row("ber", "BER")
        self.kv.row("unc", _("Uncorrectable blocks"))
        self.kv.row("locklost", _("Lock losses"))
        self.box.append(self.kv)
        self.hist_title = subtitle(_("C/N history"))
        self.box.append(self.hist_title)
        self.chart = Chart(height=60, unit="dB", fmt=lambda v: f"{v:.0f}")
        self.box.append(self.chart)
        self.box.append(subtitle(_("Errors")))
        self.strip = EventStrip()
        self.box.append(self.strip)
        self.box.append(subtitle(_("Reception")))
        self.rx = KV()
        for key, name in (("freq", _("Frequency")), ("sr", _("Symbol rate")),
                          ("mod", _("Modulation")), ("roll", _("Roll-off / pilots")),
                          ("diseqc", "DiSEqC"), ("cap", _("Transponder capacity")),
                          ("ids", "ONID · TSID · SID"), ("proto", _("Protocol")),
                          ("server", _("Server")), ("latency", _("Response time")),
                          ("http", "HTTP"), ("rebuf", _("Rebuffering"))):
            self.rx.row(key, name)
        self.box.append(self.rx)

    def update(self, st, session):
        sig = st.get("signal") or {}
        hist = session.window_history()
        now = time.time()
        q = sig.get("quality")
        lv = level_colour(q)
        word = {GOOD: _("Good"), FAIR: _("Fair"), POOR: _("Poor"), BAD: _("Bad")}.get(lv, _("No signal"))
        self.ring.set_value(q or 0, f"{round((q or 0) * 100)}%", word, lv)
        iptv = st.get("kind") == "iptv"
        self.title.set_text((_("Stream") if iptv else _("Signal")).upper())
        self.locks.set_visible(not iptv)
        self.kv.set_visible(not iptv)
        status = sig.get("status") or {}
        set_chip(self.lock_chip, "LOCK" if sig.get("lock") else _("NO LOCK"),
                 "g" if sig.get("lock") else "r")
        self.lock_chip.set_visible(not iptv)
        for key, c in self.lock_chips.items():
            if key in status:
                set_chip(c, c.get_text(), "g" if status[key] else "r")
            c.set_visible(key in status)
        if iptv:
            ip = st.get("iptv") or {}
            buf = ip.get("buffer") or {}
            self._meter("level", _("Buffer"), f"{buf.get('percent', '–')} %",
                        (buf.get("percent") or 0) / 100, self.page.accent)
            dl, total = ip.get("download_bps"), (st.get("sample") or {}).get("total")
            self._meter("cnr", _("Download"), rate(dl), (dl / (total * 4)) if dl and total else 0,
                        GOOD)
            self._meter("margin", _("Stream"), rate(total), 1.0 if total else 0, self.page.accent)
            self.hist_title.set_text(_("Buffer history").upper())
            self.chart.unit = "%"
            self.chart.y_min, self.chart.y_max = 0, 100
            self.chart.seconds = session.window_seconds
            self.chart.set_data([("buf", [(s["t"], s.get("buffer")) for s in hist], self.page.accent)], now)
            probe = ip.get("probe") or {}
            for key in ("freq", "sr", "mod", "roll", "diseqc", "cap", "ids"):
                self.rx.set(key, "", False)
            self.rx.set("proto", probe.get("protocol", ""))
            self.rx.set("server", probe.get("server") or probe.get("error", ""))
            self.rx.set("latency", num((probe.get("latency") or 0) * 1000, 0, "ms")
                        if "latency" in probe else "")
            self.rx.set("http", str(probe.get("status", "")))
            self.rx.set("rebuf", str(ip.get("rebuffers", "–")))
        else:
            dbm, rel = sig.get("level_dbm"), sig.get("level_rel")
            if dbm is not None:
                self._meter("level", _("Level"), num(dbm, 1, "dBm"), (dbm + 90) / 70, self.page.accent)
            else:
                self._meter("level", _("Level"), f"{round(rel * 100)} %" if rel is not None else "–",
                            rel or 0, self.page.accent)
            cnr = sig.get("cnr_db")
            if cnr is not None:
                self._meter("cnr", "C/N", num(cnr, 1, "dB"), cnr / 20, level_colour(min(1, cnr / 15)))
            else:
                r = sig.get("snr_rel")
                self._meter("cnr", "SNR", f"{round(r * 100)} %" if r is not None else "–", r or 0,
                            level_colour(r))
            m = sig.get("margin")
            self._meter("margin", _("Margin"), (num(m, 1, "dB") if m is None else
                                                ("+" if m >= 0 else "") + num(m, 1, "dB")),
                        max(0, (m or 0) / 10), level_colour(0.3 + (m or 0) / 10 if m is not None else None))
            ber = sig.get("ber")
            self.kv.set("ber", ber_text(ber) if ber is not None else
                        (str(sig.get("ber_raw")) + " " + _("(driver value)")
                         if sig.get("ber_raw") is not None else "–"))
            unc_total = sum(s.get("unc", 0) for s in session.history)
            self.kv.set("unc", f"{unc_total} " + _("since start"))
            self.kv.set("locklost", str(sum(1 for e in session.events if e[1] == "lock")))
            self.chart.seconds = session.window_seconds
            key = "cnr_db" if any(s.get("cnr_db") is not None for s in hist) else "quality"
            self.chart.unit = "dB" if key == "cnr_db" else "%"
            self.chart.y_min, self.chart.y_max = (None, None) if key == "cnr_db" else (0, 100)
            pts = [(s["t"], s.get(key) if key == "cnr_db" else
                    (s.get("quality") or 0) * 100) for s in hist]
            need = sig.get("required_cn")
            self.chart.set_data([("cnr", pts, GOOD)], now,
                                lines=[(need, _("required"))] if need and key == "cnr_db" else [])
            self.hist_title.set_text((_("C/N history") if key == "cnr_db" else _("Quality history")).upper())
            c = session.channel
            t = session.tuning or {}
            if c.is_sat:
                self.rx.set("freq", f"{c.frequency // 1000} MHz · " +
                            {"h": _("horizontal"), "v": _("vertical"), "l": _("left"),
                             "r": _("right")}.get(c.polarity.lower(), c.polarity))
            else:
                self.rx.set("freq", num(c.frequency / 1000, 1, "MHz") if c.frequency else "")
            sr = (t.get("symbol_rate") or c.symbol_rate * 1000) // 1000
            self.rx.set("sr", f"{sr} kBd" if sr else "", bool(sr))
            mod = " · ".join(x for x in ((t.get("delsys") or c.delsys).upper(),
                                         (t.get("modulation") or c.modulation).upper(),
                                         "FEC " + (t.get("fec") or t.get("code_rate_hp") or c.fec))
                             if x and x not in ("AUTO", "FEC AUTO"))
            self.rx.set("mod", mod)
            roll = " · ".join(x for x in (t.get("rolloff", ""), t.get("pilot", ""),
                                          t.get("guard", ""), t.get("transmission_mode", "")) if x)
            self.rx.set("roll", roll, bool(roll))
            self.rx.set("diseqc", _("Port {n}").format(n=c.diseqc + 1), c.diseqc >= 0)
            cap = st.get("capacity")
            self.rx.set("cap", rate(cap) + " " + _("(theoretical)") if cap else "", bool(cap))
            snap = st.get("snap") or {}
            self.rx.set("ids", f"{snap.get('onid') or c.onid or '–'} · "
                               f"{snap.get('tsid') or c.tsid or '–'} · {c.sid or '–'}")
            for key in ("proto", "server", "latency", "http", "rebuf"):
                self.rx.set(key, "", False)
        self.strip.seconds = session.window_seconds
        self.strip.set_events(st.get("events", []), now)

    def _meter(self, key, name, text, frac, colour):
        nl, vl, bar = self.meters[key]
        nl.set_text(name)
        vl.set_text(text)
        bar.set_value(frac, colour)


class OverviewPage(Page):
    name, title = "overview", _("Overview")

    def build(self):
        root = Gtk.Box(spacing=12, margin_start=14, margin_end=14, margin_top=14, margin_bottom=14)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, width_request=330)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, width_request=320)
        for b in (left, mid, right):
            root.append(b)
        self.sig = SignalCard(self)
        left.append(self.sig.box)
        # bit rate
        rc = card(_("Bit rate of the channel"))
        self.legend = Gtk.Box(spacing=12)
        rc.append(self.legend)
        figs = Gtk.Box(spacing=28)
        self.figs = {}
        for key, name in (("now", _("Now")), ("avg", _("Average")), ("max", _("Peak")),
                          ("share", _("Share of transponder"))):
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            b.append(label(name, ["lv-dim"]))
            row = Gtk.Box(spacing=4)
            v = label("–", ["lv-big"])
            u = label("", ["lv-unit"], valign=Gtk.Align.END, margin_bottom=5)
            row.append(v)
            row.append(u)
            b.append(row)
            figs.append(b)
            self.figs[key] = (v, u)
        rc.append(figs)
        self.rate_chart = Chart(height=180, stacked=True, unit="Mbit/s", y_min=0,
                                fmt=lambda v: f"{v:.0f}" if v >= 10 else f"{v:.1f}")
        rc.append(self.rate_chart)
        mid.append(rc)
        va = Gtk.Box(spacing=18, css_classes=["lv-card"])
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        vhead = Gtk.Box()
        vhead.append(label(_("Video").upper(), ["lv-card-title"], hexpand=True))
        self.hw_chip = chip("")
        vhead.append(self.hw_chip)
        vbox.append(vhead)
        self.vkv = KV()
        for key, name in (("codec", _("Codec")), ("res", _("Resolution")), ("fps", _("Frame rate")),
                          ("aspect", _("Aspect ratio")), ("colour", _("Colour")),
                          ("decoder", _("Decoder")), ("dropped", _("Dropped frames"))):
            self.vkv.row(key, name)
        vbox.append(self.vkv)
        abox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        ahead = Gtk.Box()
        ahead.append(label(_("Audio").upper(), ["lv-card-title"], hexpand=True))
        self.audio_count = label("", ["lv-dim"])
        ahead.append(self.audio_count)
        abox.append(ahead)
        self.atable = Table([(_("Track"), "swatch", 0, True), (_("Format"), "text", 0, False),
                             (_("Channels"), "text", 0, False), ("kbit/s", "text", 1, False)],
                            on_activate=self._select_track)
        abox.append(self.atable)
        self.loud_line = label("", ["lv-dim"], wrap=True)
        abox.append(self.loud_line)
        va.append(vbox)
        va.append(abox)
        mid.append(va)
        # right column
        ic = card(_("Now & next"), chip(_("EIT"), "a"))
        self.now_t = label("", ["lv-tnum"], wrap=True)
        self.now_t.set_markup("")
        self.now_s = label("", ["lv-dim"], wrap=True)
        self.now_bar = Bar()
        self.next_t = label("", wrap=True)
        self.next_s = label("", ["lv-dim"])
        for w in (self.now_t, self.now_s, self.now_bar, self.next_t, self.next_s):
            ic.append(w)
        ic.append(subtitle(_("Encryption")))
        encr = Gtk.Box(spacing=8)
        self.enc_chip = chip("")
        self.enc_text = label("", ["lv-dim"], wrap=True, hexpand=True)
        encr.append(self.enc_chip)
        encr.append(self.enc_text)
        ic.append(encr)
        ic.append(subtitle(_("Interactive")))
        self.ikv = KV()
        for key, name in (("hbbtv", "HbbTV"), ("autostart", _("Autostart")),
                          ("ttx", _("Teletext")), ("subs", _("DVB subtitles"))):
            self.ikv.row(key, name)
        ic.append(self.ikv)
        ic.append(subtitle(_("Stream health")))
        self.hkv = KV()
        for key, name in (("cc", _("Continuity errors")), ("tei", _("Transport errors")),
                          ("pcr", _("PCR interval")), ("jitter", _("PCR arrival jitter")),
                          ("psi", _("PAT / PMT interval")), ("buffer", _("Timeshift buffer"))):
            self.hkv.row(key, name)
        ic.append(self.hkv)
        right.append(ic)
        return root

    def _select_track(self, key):
        player = self.session.player
        if player is not None and self.session.is_on_screen() and isinstance(key, int):
            player.select_audio(key)

    def update(self, st):
        s = self.session
        self.sig.update(st, s)
        snap = st.get("snap") or {}
        now = time.time()
        hist = s.window_history()
        layers = st.get("layers") or []
        pmt = snap.get("pmt") or {}
        streams = dict(pmt.get("streams", []))
        # legend + chart
        colours = {}
        ai = 0
        child = self.legend.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.legend.remove(child)
            child = nxt
        for key, _r, kind in layers:
            colours[key] = layer_colour(ai, kind, self.accent)
            if kind == "audio":
                ai += 1
            d = streams.get(key, {})
            es = (snap.get("es") or {}).get(key, {})
            name = _("Video") if kind == "video" else (
                _("Teletext / subtitles / data") if kind == "other" else
                " ".join(x for x in (es.get("short") or d.get("codec", ""), d.get("language", ""))
                         if x))
            b = Gtk.Box(spacing=6)
            sw = Gtk.DrawingArea(content_width=9, content_height=9, valign=Gtk.Align.CENTER)
            sw.set_draw_func(lambda _a, cr, w, h, c=colours[key]: (
                cr.set_source_rgb(*charts.rgb(c)), cr.rectangle(0, 0, w, h), cr.fill()))
            b.append(sw)
            b.append(label(name, ["lv-dim"]))
            self.legend.append(b)
        series = []
        for key, _r, _kind in layers:
            pts = [(h["t"], (h["layers"].get(key) or 0) / 1e6) for h in hist if "layers" in h]
            series.append((str(key), pts, colours[key]))
        stats = s.stats("service")
        lines = [(stats["avg"] / 1e6, _("Ø {v}").format(v=rate(stats["avg"])))] if stats else []
        self.rate_chart.seconds = s.window_seconds
        self.rate_chart.set_data(series, now, lines=lines)
        if st.get("kind") == "iptv" and not layers:
            stats = s.stats("total")
        for key, field in (("now", "now"), ("avg", "avg"), ("max", "max")):
            v, u = self.figs[key]
            val = stats[field] if stats else None
            v.set_text(num(val / 1e6, 1) if val is not None else "–")
            u.set_text("Mbit/s")
        v, u = self.figs["share"]
        tp = st.get("transponder") or {}
        base = tp.get("used") or st.get("capacity")
        if stats and base:
            v.set_text(str(round(stats["avg"] / base * 100)))
            u.set_text("%")
        else:
            v.set_text("–")
            u.set_text("")
        # video
        es = snap.get("es") or {}
        vid = next((e for e in es.values() if e.get("kind") == "video"), {})
        vi = st.get("video") or {}
        codec = " · ".join(x for x in (vid.get("codec") or vi.get("codec", ""),
                                       "@".join(y for y in (vid.get("profile"), vid.get("level")) if y))
                           if x)
        self.vkv.set("codec", codec)
        w, h = vi.get("width") or vid.get("width"), vi.get("height") or vid.get("height")
        scan = vi.get("interlace-mode", "")
        scan_t = _("interlaced") if scan in ("interleaved", "mixed", "fields") or \
            (not scan and vid.get("interlaced")) else _("progressive")
        self.vkv.set("res", f"{w} × {h} · {scan_t}" if w and h else "")
        fps = vi.get("fps") or vid.get("fps") or (1000 / vid["frame_ms"] if vid.get("frame_ms") else None)
        self.vkv.set("fps", num(fps, 2 if fps and fps % 1 else 0, "fps") if fps else "")
        aspect = vid.get("aspect") or ""
        if vid.get("afd") is not None:
            aspect += f" · AFD {vid['afd']} ({AFD_NAMES.get(vid['afd'], '')})"
        self.vkv.set("aspect", aspect.strip(" ·"))
        colour = " · ".join(x for x in (vid.get("chroma"), f"{vid['bit_depth']} bit" if vid.get("bit_depth") else "",
                                        vid.get("transfer") or vi.get("colorimetry", "")) if x)
        self.vkv.set("colour", colour)
        self.vkv.set("decoder", vi.get("decoder", "") if st.get("on_screen") else _("only for the channel on screen"))
        dropped = vi.get("dropped")
        self.vkv.set("dropped", f"{dropped} / {vi.get('rendered', 0)}" if dropped is not None else "")
        if st.get("on_screen") and vi.get("decoder"):
            set_chip(self.hw_chip, _("Hardware") if vi.get("hardware") else _("Software"),
                     "a" if vi.get("hardware") else "")
            self.hw_chip.set_visible(True)
        else:
            self.hw_chip.set_visible(False)
        # audio
        rows = []
        rates = st.get("rates") or {}
        tracks = (st.get("tracks") or ([], -1))
        cur = tracks[1]
        ai = 0
        audio_pids = [pid for pid, d in pmt.get("streams", []) if d["kind"] == "audio"]
        for i, pid in enumerate(audio_pids):
            d = streams[pid]
            e = es.get(pid, {})
            name = d.get("language") or "–"
            if d.get("audio_description"):
                name += " · " + _("AD")
            rows.append((i, [(PALETTE[i % len(PALETTE)], name),
                             e.get("short") or d.get("codec", ""),
                             e.get("layout", ""),
                             str(round((e.get("nominal_bps") or rates.get(pid) or 0) / 1000)) or "–"]))
        if not rows and tracks[0]:
            for i, t in enumerate(tracks[0]):
                rows.append((i, [(PALETTE[i % len(PALETTE)], t.language_name or "–"), t.codec or "",
                                 t.layout or "", "–"]))
        self.atable.set_rows(rows, selected=cur if st.get("on_screen") else None)
        self.audio_count.set_text(_("{n} tracks").format(n=len(rows)))
        loud = st.get("loudness") or {}
        dial = next((e.get("dialnorm") for e in es.values() if e.get("dialnorm")), None)
        if loud.get("short") is not None and math.isfinite(loud["short"]):
            self.loud_line.set_text(_("Loudness (current track): {lufs} · peak {peak} · dialnorm {dn}").format(
                lufs=num(loud["short"], 1, "LUFS"), peak=num(loud.get("recent_peak"), 1, "dBFS"),
                dn=str(dial) if dial else "–"))
        else:
            self.loud_line.set_text(_("Loudness: only for the channel on screen")
                                    if not st.get("on_screen") else "")
        # now & next
        ev_now, ev_next = s.app.epg.now_next(s.channel.epg_key)
        if ev_now:
            self.now_t.set_markup(f"<b>{GLib.markup_escape_text(ev_now.title)}</b>")
            self.now_s.set_text(time.strftime("%H:%M", time.localtime(ev_now.start)) + " – " +
                                time.strftime("%H:%M", time.localtime(ev_now.end)) +
                                (f" · {ev_now.genre}" if ev_now.genre else ""))
            self.now_bar.set_value((now - ev_now.start) / max(1, ev_now.duration), self.accent)
        else:
            self.now_t.set_text(_("No programme information"))
            self.now_s.set_text("")
            self.now_bar.set_value(0)
        self.next_t.set_text(ev_next.title if ev_next else "")
        self.next_s.set_text(time.strftime("%H:%M", time.localtime(ev_next.start)) if ev_next else "")
        # encryption
        cas = ca_lines(snap)
        scr = sum(snap.get("pids", {}).get(pid, {}).get("scrambled", 0) for pid in streams)
        if cas or scr or s.channel.encrypted:
            names = sorted({n or f"0x{c:04X}" for c, n, _p, _l in cas})
            set_chip(self.enc_chip, _("Encrypted"), "y")
            self.enc_text.set_text(", ".join(names) if names else _("scrambled packets"))
        else:
            set_chip(self.enc_chip, _("Free-to-air"), "g")
            self.enc_text.set_text(_("no CA descriptors in the PMT") if pmt else "")
        # interactive
        ait_pid = next((pid for pid, d in pmt.get("streams", []) if d.get("ait")), None)
        self.ikv.set("hbbtv", f"AIT · PID {ait_pid}" if ait_pid else _("none"))
        ait = getattr(s.capture, "ait", None) if s.capture is not None else None
        auto = ait.autostart() if ait is not None else None
        self.ikv.set("autostart", auto.name() if auto else "–")
        ttx = [(pid, d) for pid, d in pmt.get("streams", []) if d["kind"] == "teletext"]
        self.ikv.set("ttx", _("PID {pid} · {n} pages").format(pid=ttx[0][0], n=len(ttx[0][1].get("pages", [])))
                     if ttx else _("none"))
        subs = [d for _p, d in pmt.get("streams", []) if d["kind"] == "subtitle"]
        self.ikv.set("subs", ", ".join(d.get("language", "?") + (" (" + _("hard of hearing") + ")"
                                                                 if d.get("hard_of_hearing") else "")
                                       for d in subs) if subs else _("none"))
        # stream health
        self.hkv.set("cc", str(snap.get("cc_errors", 0)))
        self.hkv.set("tei", str(snap.get("tei", 0)))
        pcr_pid = pmt.get("pcr_pid")
        pd = (snap.get("pids") or {}).get(pcr_pid, {})
        self.hkv.set("pcr", (num(pd["pcr_avg"] * 1000, 0, "ms") + " · max " + num(pd["pcr_max"] * 1000, 0, "ms")
                             + (" ⚠" if pd["pcr_max"] > 0.04 else "")) if "pcr_avg" in pd else "")
        self.hkv.set("jitter", ("± " + num(pd["pcr_jitter"] * 1000, 1, "ms")) if pd.get("pcr_jitter") is not None else "")
        tables = {(t["tid"]): t for t in snap.get("tables", [])}
        pat, pmt_t = tables.get(0), tables.get(2)
        self.hkv.set("psi", " / ".join(num(t["interval"] * 1000, 0, "ms") if t and t["interval"] else "–"
                                       for t in (pat, pmt_t)))
        cap = s.capture
        if cap is not None:
            start, end = cap.store.bounds()
            self.hkv.set("buffer", f"{(end - start) / 1e9:.1f} GB · " +
                         duration((end - start) / max(1, cap.byte_rate())))
        else:
            self.hkv.set("buffer", "", False)


class SignalPage(Page):
    name, title = "signal", _("Signal")

    def build(self):
        box = page_box()
        top = Gtk.Box(spacing=12)
        self.big = {}
        for key, name in (("quality", _("Quality")), ("cnr", "C/N"), ("margin", _("Margin")),
                          ("level", _("Level")), ("ber", "BER")):
            c = card(name)
            v = label("–", ["lv-big"])
            sub = label("", ["lv-dim"])
            c.append(v)
            c.append(sub)
            c.set_hexpand(True)
            top.append(c)
            self.big[key] = (v, sub, c.get_first_child().get_first_child())
        box.append(top)
        c = card(_("Carrier to noise ratio"))
        self.cn_title = c.get_first_child().get_first_child()
        self.cn_chart = Chart(height=150, unit="dB")
        c.append(self.cn_chart)
        box.append(c)
        row = Gtk.Box(spacing=12, homogeneous=True)
        c = card(_("Signal level"))
        self.lv_title = c.get_first_child().get_first_child()
        self.lv_chart = Chart(height=110)
        c.append(self.lv_chart)
        row.append(c)
        c = card(_("Bit error rate (log₁₀)"))
        self.ber_card = c
        self.ber_chart = Chart(height=110, fmt=lambda v: f"{v:.0f}", y_min=-9, y_max=-1)
        c.append(self.ber_chart)
        row.append(c)
        box.append(row)
        # internet channels: the server and the playlist
        c = card(_("Internet stream"))
        self.iptv_card = c
        self.ikv = KV()
        for key, name in (("url", _("Address")), ("proto", _("Protocol")), ("http", _("HTTP status")),
                          ("server", _("Server")), ("type", _("Content type")),
                          ("latency", _("Response time")), ("download", _("Download speed")),
                          ("playlist", _("Playlist")), ("segments", _("Segments")),
                          ("rebuf", _("Rebuffering")), ("error", _("Error"))):
            self.ikv.row(key, name)
        c.append(self.ikv)
        c.append(subtitle(_("Variants")))
        self.variants = Table([(_("Bit rate"), "text", 0, False), (_("Resolution"), "text", 0, False),
                               (_("Frame rate"), "text", 0, False), (_("Codecs"), "text", 0, True)])
        c.append(self.variants)
        self.media_l = label("", ["lv-dim"], wrap=True)
        c.append(self.media_l)
        box.append(c)
        c = card(_("Error timeline"))
        self.strip = EventStrip(height=26)
        c.append(self.strip)
        legend = Gtk.Box(spacing=14)
        for kind, name in (("unc", _("uncorrectable blocks")), ("cc", _("continuity errors")),
                           ("lock", _("lock lost")), ("rebuffer", _("rebuffering")),
                           ("pcr", _("PCR jump")), ("loudness", _("loudness jump")),
                           ("version", _("table version"))):
            b = Gtk.Box(spacing=5)
            sw = Gtk.DrawingArea(content_width=9, content_height=9, valign=Gtk.Align.CENTER)
            sw.set_draw_func(lambda _a, cr, w, h, k=kind: (
                cr.set_source_rgb(*charts.rgb(charts.EVENT_COLOURS[k])), cr.rectangle(0, 0, w, h),
                cr.fill()))
            b.append(sw)
            b.append(label(name, ["lv-dim"]))
            legend.append(b)
        c.append(legend)
        self.event_list = label("", ["lv-mono"], wrap=True, selectable=True)
        c.append(self.event_list)
        box.append(c)
        row = Gtk.Box(spacing=12, homogeneous=True)
        c = card(_("Tuned parameters (from the frontend)"))
        self.tuned_card = c
        self.tkv = KV()
        for key, name in (("delsys", _("Delivery system")), ("frequency", _("Frequency")),
                          ("modulation", _("Modulation")), ("fec", _("Code rate")),
                          ("symbol_rate", _("Symbol rate")), ("rolloff", _("Roll-off")),
                          ("pilot", _("Pilots")), ("guard", _("Guard interval")),
                          ("transmission_mode", _("Carriers")), ("stream_id", _("Stream / PLP")),
                          ("required", _("C/N needed")), ("source", _("Source of the figures"))):
            self.tkv.row(key, name)
        c.append(self.tkv)
        align = Gtk.Button(label=_("Dish alignment aid…"), halign=Gtk.Align.START, margin_top=6)
        align.connect("clicked", lambda *_: AlignWindow(self.win).present())
        c.append(align)
        row.append(c)
        c = card(_("Logging and warnings"))
        settings = self.session.app.settings
        self.log_sw = Gtk.Switch(active=bool(settings.get("reception_log")), valign=Gtk.Align.CENTER)
        self.log_sw.connect("notify::active", lambda sw, _p: self._set("reception_log", sw.get_active()))
        c.append(self._switch_row(_("Log reception (one line a minute, kept 31 days)"), self.log_sw))
        self.warn_sw = Gtk.Switch(active=bool(settings.get("analysis_warn")), valign=Gtk.Align.CENTER)
        self.warn_sw.connect("notify::active", lambda sw, _p: self._set("analysis_warn", sw.get_active()))
        c.append(self._switch_row(_("Warn on screen when the reception gets weak"), self.warn_sw))
        spin = Gtk.SpinButton.new_with_range(0.5, 10, 0.5)
        spin.set_value(float(settings.get("analysis_warn_margin", 2.0)))
        spin.connect("value-changed", lambda sp: self._set("analysis_warn_margin", sp.get_value()))
        c.append(self._switch_row(_("Warn below this margin (dB)"), spin))
        c.append(subtitle(_("Reception over the last days")))
        self.log_chart = Chart(height=110, seconds=7 * 86400)
        c.append(self.log_chart)
        self.log_info = label("", ["lv-dim"], wrap=True)
        c.append(self.log_info)
        btn = Gtk.Button(label=_("Open log folder"), halign=Gtk.Align.START)
        btn.connect("clicked", lambda *_: self._open_logs())
        c.append(btn)
        row.append(c)
        box.append(row)
        self._log_at = 0
        return box

    def _switch_row(self, text, widget):
        b = Gtk.Box(spacing=12)
        b.append(label(text, wrap=True, hexpand=True))
        b.append(widget)
        return b

    def _set(self, key, value):
        s = self.session.app.settings
        s[key] = value
        s.save()

    def _open_logs(self):
        from ..receptionlog import LOG_DIR
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(LOG_DIR))).launch(self.win, None, None)

    def update(self, st):
        iptv = self.session.kind == "iptv"
        for w in (self.ber_card, self.tuned_card):
            w.set_visible(not iptv)
        self.iptv_card.set_visible(iptv)
        if iptv:
            self._update_iptv(st)
        else:
            self._update_dvb(st)
        self._update_common(st)

    def _update_iptv(self, st):
        s = self.session
        sig = st.get("signal") or {}
        ip = st.get("iptv") or {}
        probe = ip.get("probe") or {}
        now = time.time()
        hist = s.window_history()
        q = s.stats("quality")
        self._big("quality", f"{round((sig.get('quality') or 0) * 100)} %",
                  _("Ø {avg} · min {min}").format(avg=f"{round(q['avg'] * 100)} %", min=f"{round(q['min'] * 100)} %")
                  if q else "")
        tot = s.stats("total")
        self._big("cnr", rate(tot["now"]) if tot else "–",
                  _("Ø {avg} · peak {max}").format(avg=rate(tot["avg"]), max=rate(tot["max"])) if tot else "",
                  _("Stream"))
        dl = ip.get("download_bps") or probe.get("download_bps")
        self._big("margin", rate(dl) if dl else "–",
                  _("{x}× the stream rate").format(x=num(dl / tot["avg"], 1)) if dl and tot and tot["avg"] else "",
                  _("Download"))
        buf = ip.get("buffer") or {}
        self._big("level", f"{buf['percent']} %" if buf.get("percent") is not None else "–",
                  _("only for the channel on screen") if not st.get("on_screen") else "", _("Buffer"))
        self._big("ber", str(ip.get("rebuffers", "–")), _("since start"), _("Rebuffering"))
        self.cn_title.set_text(_("Buffer history").upper())
        self.cn_chart.unit = "%"
        self.cn_chart.y_min, self.cn_chart.y_max = 0, 100
        self.cn_chart.seconds = self.lv_chart.seconds = s.window_seconds
        markers = [(e[0], charts.EVENT_COLOURS["rebuffer"]) for e in s.events if e[1] == "rebuffer"]
        self.cn_chart.set_data([("buf", [(h["t"], h.get("buffer")) for h in hist], GOOD)], now, markers=markers)
        self.lv_title.set_text(_("Download and stream rate").upper())
        self.lv_chart.unit = "Mbit/s"
        self.lv_chart.y_min = 0
        self.lv_chart.set_data([
            ("dl", [(h["t"], h["download"] / 1e6 if h.get("download") else None) for h in hist], GOOD),
            ("st", [(h["t"], h["total"] / 1e6 if h.get("total") else None) for h in hist], self.accent)],
            now, lines=[(tot["avg"] / 1e6, _("stream"))] if tot else [])
        self.ikv.set("url", probe.get("final_url") or s.channel.url)
        self.ikv.set("proto", probe.get("protocol", ""))
        self.ikv.set("http", str(probe.get("status", "")), "status" in probe)
        self.ikv.set("server", probe.get("server", ""), bool(probe.get("server")))
        self.ikv.set("type", probe.get("content_type", ""), bool(probe.get("content_type")))
        self.ikv.set("latency", num(probe["latency"] * 1000, 0, "ms") if probe.get("latency") else "",
                     bool(probe.get("latency")))
        self.ikv.set("download", rate(probe.get("download_bps")) if probe.get("download_bps") else "",
                     bool(probe.get("download_bps")))
        pl = probe.get("playlist") or {}
        kind = pl.get("kind")
        media_pl = pl.get("media_playlist") or (pl if kind == "media" else {})
        self.ikv.set("playlist", {"master": _("HLS with {n} variants").format(n=len(pl.get("variants", []))),
                                  "media": _("HLS media playlist"),
                                  "dash": _("MPEG-DASH")}.get(kind, _("none (a plain stream)"))
                     + ((" · " + (_("live") if pl.get("live") or media_pl.get("live") else _("on demand")))
                        if kind else ""))
        self.ikv.set("segments", _("{n} segments of Ø {s} (target {t}) · {c}").format(
            n=media_pl["segments"], s=num(media_pl["avg_segment"], 1, "s"), t=num(media_pl["target"], 0, "s"),
            c=media_pl.get("container") or "?") if media_pl.get("segments") else "", bool(media_pl.get("segments")))
        self.ikv.set("rebuf", str(ip.get("rebuffers", "–")))
        self.ikv.set("error", probe.get("error") or ip.get("error", ""), bool(probe.get("error") or ip.get("error")))
        rows = []
        for i, v in enumerate(pl.get("variants") or [r for r in pl.get("representations", [])
                                                     if r["type"] in ("video", "")]):
            rows.append((i, [rate(v["bandwidth"]), f"{v['width']} × {v['height']}" if v.get("width") else "–",
                             num(v["fps"], 2, "fps") if v.get("fps") else "–", v.get("codecs", "")]))
        self.variants.set_rows(rows)
        media = pl.get("media") or [r for r in pl.get("representations", []) if r["type"] not in ("video", "")]
        self.media_l.set_text(", ".join(
            " ".join(x for x in (m.get("type", "").lower(), m.get("language", ""), m.get("name", "")) if x)
            for m in media) if media else (_("No variant list: the address is a single stream.") if not rows else ""))

    def _update_common(self, st):
        s = self.session
        now = time.time()
        self.strip.seconds = s.window_seconds
        self.strip.set_events(s.events, now)
        self.event_list.set_text("\n".join(
            time.strftime("%H:%M:%S", time.localtime(t)) + "  " + text
            for t, _k, text in list(s.events)[-12:][::-1]) or _("No events so far."))
        if time.monotonic() - self._log_at > 60:
            self._log_at = time.monotonic()
            from ..receptionlog import ReceptionLog
            rows = ReceptionLog().read(7, s.channel.name)
            self.log_chart.unit = "dB" if any(r.get("cnr_avg") is not None for r in rows) else "%"
            key = "cnr_avg" if self.log_chart.unit == "dB" else "quality_avg"
            mul = 1 if key == "cnr_avg" else 100
            self.log_chart.set_data([("log", [(r["time"], r[key] * mul if r.get(key) is not None else None)
                                              for r in rows], self.accent)], now)
            self.log_info.set_text(_("{n} minutes logged for this channel.").format(n=len(rows))
                                   if rows else _("Nothing logged for this channel yet."))

    def _update_dvb(self, st):
        s = self.session
        sig = st.get("signal") or {}
        now = time.time()
        hist = s.window_history()
        q = s.stats("quality")
        self._big("quality", f"{round((sig.get('quality') or 0) * 100)} %",
                  _("Ø {avg} · min {min}").format(avg=f"{round(q['avg'] * 100)} %", min=f"{round(q['min'] * 100)} %") if q else "")
        c = s.stats("cnr_db")
        self._big("cnr", num(sig.get("cnr_db"), 1, "dB"),
                  _("min {min} · max {max}").format(min=num(c["min"], 1), max=num(c["max"], 1)) if c else "",
                  "C/N")
        m = sig.get("margin")
        self._big("margin", (("+" if m >= 0 else "") + num(m, 1, "dB")) if m is not None else "–",
                  _("needs {db}").format(db=num(sig.get("required_cn"), 1, "dB")) if sig.get("required_cn") else "",
                  _("Margin"))
        if sig.get("level_dbm") is not None:
            self._big("level", num(sig["level_dbm"], 1, "dBm"), "", _("Level"))
        else:
            self._big("level", f"{round((sig.get('level_rel') or 0) * 100)} %" if sig.get("level_rel") is not None else "–",
                      _("relative (driver)"), _("Level"))
        self._big("ber", ber_text(sig.get("ber")), _("post FEC {v}").format(v=ber_text(sig.get("post_ber")))
                  if sig.get("post_ber") is not None else "", "BER")
        self.lv_title.set_text(_("Signal level").upper())
        for ch in (self.cn_chart, self.lv_chart, self.ber_chart):
            ch.seconds = s.window_seconds
        need = sig.get("required_cn")
        markers = [(e[0], charts.EVENT_COLOURS.get(e[1], OTHER)) for e in s.events if e[1] in ("unc", "lock")]
        if any(h.get("cnr_db") is not None for h in hist):
            self.cn_title.set_text(_("Carrier to noise ratio").upper())
            self.cn_chart.unit = "dB"
            self.cn_chart.y_min = self.cn_chart.y_max = None
            self.cn_chart.set_data([("cnr", [(h["t"], h.get("cnr_db")) for h in hist], GOOD)], now,
                                   lines=[(need, _("needed for a clean picture"))] if need else [],
                                   markers=markers)
        else:
            self.cn_title.set_text(_("Signal quality").upper())
            self.cn_chart.unit = "%"
            self.cn_chart.y_min, self.cn_chart.y_max = 0, 100
            self.cn_chart.set_data([("q", [(h["t"], (h.get("quality") or 0) * 100) for h in hist], GOOD)],
                                   now, markers=markers)
        if any(h.get("level_dbm") is not None for h in hist):
            self.lv_chart.unit = "dBm"
            self.lv_chart.y_min = self.lv_chart.y_max = None
            pts = [(h["t"], h.get("level_dbm")) for h in hist]
        else:
            self.lv_chart.unit = "%"
            self.lv_chart.y_min, self.lv_chart.y_max = 0, 100
            pts = [(h["t"], (h["level_rel"] * 100) if h.get("level_rel") is not None else None) for h in hist]
        self.lv_chart.set_data([("lvl", pts, self.accent)], now)
        self.ber_chart.set_data([("ber", [(h["t"], math.log10(h["ber"]) if h.get("ber") else
                                           (-9 if h.get("ber") == 0 else None)) for h in hist], POOR)], now)
        t = s.tuning or {}
        for key in ("delsys", "modulation", "fec", "rolloff", "pilot", "guard", "transmission_mode"):
            v = t.get(key) or t.get("code_rate_hp" if key == "fec" else "") or ""
            self.tkv.set(key, str(v).upper() if key in ("delsys", "modulation") else str(v), bool(v))
        f = t.get("frequency")
        self.tkv.set("frequency", (f"{f / 1000:.0f} MHz" if s.channel.is_sat else f"{f / 1e6:.2f} MHz")
                     if f else "", bool(f))
        self.tkv.set("symbol_rate", f"{t['symbol_rate'] // 1000} kBd" if t.get("symbol_rate") else "",
                     bool(t.get("symbol_rate")))
        sid = t.get("stream_id")
        self.tkv.set("stream_id", str(sid), sid not in (None, 0xFFFFFFFF, -1))
        self.tkv.set("required", num(need, 1, "dB") if need else "")
        fe = st.get("frontend") or {}
        self.tkv.set("source", _("DVB API v5 statistics") if fe.get("cnr_db") is not None or
                     fe.get("signal_dbm") is not None else
                     (_("dvbsrc (driver scale)") if s.kind == "dvb" else _("internet stream")))

    def _big(self, key, value, sub, title=None):
        v, s, t = self.big[key]
        v.set_text(value)
        s.set_text(sub)
        if title is not None:
            t.set_text(title.upper())


class TransportPage(Page):
    name, title = "transport", _("Transport Stream")

    def build(self):
        box = page_box()
        c = card(_("Elementary streams and PIDs"))
        head = c.get_first_child()
        self.scope = Gtk.DropDown.new_from_strings([_("This channel only"), _("Whole stream")])
        self.sort = Gtk.DropDown.new_from_strings([_("Sort by bit rate"), _("Sort by PID")])
        head.append(self.scope)
        head.append(self.sort)
        self.table = Table([(_("PID hex"), "text", 0, False), (_("dec"), "text", 0, False),
                            (_("Content"), "swatch", 0, True), (_("Format"), "text", 0, True),
                            (_("Type"), "chip", 0, False), (_("Bit rate"), "bar", 0, False),
                            (_("CC errors"), "text", 1, False), (_("Scrambled"), "text", 1, False),
                            (_("Last 60 s"), "spark", 0, False)])
        c.append(self.table)
        self.total = label("", ["lv-dim"])
        c.append(self.total)
        box.append(c)
        row = Gtk.Box(spacing=12)
        c = card(_("Transponder"))
        c.set_hexpand(True)
        self.tp_info = label("", ["lv-dim"], wrap=True)
        c.append(self.tp_info)
        self.share = ShareBar()
        c.append(self.share)
        self.tp_list = label("", ["lv-tnum"], wrap=True)
        c.append(self.tp_list)
        self.tp_btn = Gtk.Button(label=_("Measure the whole transponder (10 s)"), halign=Gtk.Align.START)
        self.tp_btn.connect("clicked", lambda *_: self.session.measure_transponder())
        c.append(self.tp_btn)
        row.append(c)
        c = card(_("Stream health"))
        c.set_size_request(340, -1)
        self.hkv = KV()
        for key, name in (("packets", _("Packets")), ("sync", _("Sync losses")),
                          ("tei", _("Transport errors")), ("cc", _("Continuity errors")),
                          ("pcr", _("PCR interval")), ("jitter", _("PCR arrival jitter")),
                          ("jumps", _("PCR jumps")), ("null", _("Null packets")),
                          ("filter", _("PID filter"))):
            self.hkv.row(key, name)
        c.append(self.hkv)
        row.append(c)
        box.append(row)
        return box

    def update(self, st):
        s = self.session
        snap = st.get("snap") or {}
        rates = st.get("rates") or {}
        owners = pid_owners(snap) if snap else {}
        pmt = snap.get("pmt") or {}
        streams = dict(pmt.get("streams", []))
        own = set(streams) | {pmt.get("pcr_pid")} | {p for _c, p in pmt.get("ca", [])}
        if pmt:
            own.add(snap.get("pat", {}).get(pmt["sid"]))
            own |= {0, 1, 0x10, 0x11, 0x12, 0x14}
        pids = snap.get("pids") or {}
        keys = list(pids)
        if self.scope.get_selected() == 0 and pmt:
            keys = [p for p in keys if p in own]
        if self.sort.get_selected() == 0:
            keys.sort(key=lambda p: -(rates.get(p) or 0))
        else:
            keys.sort()
        top = max([rates.get(p) or 0 for p in keys] + [1])
        rows = []
        ai = 0
        colours = {}
        for pid, d in pmt.get("streams", []):
            if d["kind"] == "audio":
                colours[pid] = PALETTE[ai % len(PALETTE)]
                ai += 1
            elif d["kind"] == "video":
                colours[pid] = self.accent
            elif d["kind"] in ("teletext", "subtitle"):
                colours[pid] = FAIR
        es = snap.get("es") or {}
        for pid in keys[:200]:
            d = pids[pid]
            sd = streams.get(pid)
            r = rates.get(pid) or 0
            if sd is not None:
                content = stream_label(sd, pid)
                e = es.get(pid, {})
                fmt = " ".join(x for x in (e.get("codec") or sd.get("codec", ""),
                                           e.get("profile", ""), e.get("layout", "")) if x)
                kind = {"video": (_("Video"), "a"), "audio": (_("Audio"), "")}.get(
                    sd["kind"], (_("Data"), "y"))
            else:
                sid, what = owners.get(pid, (None, ""))
                content = what or _("unknown")
                if sid is not None and sid != pmt.get("sid"):
                    name = snap.get("services", {}).get(sid, {}).get("name")
                    content += f" ({name or sid})"
                fmt = "–"
                kind = (_("SI"), "y") if (sid is None or what == "PMT") and pid != NULL_PID else (
                    (_("Null"), "") if pid == NULL_PID else (_("Other"), ""))
            scr = f"{d['scrambled'] * 100 // max(1, d['packets'])} %" if d["scrambled"] else "—"
            rows.append((pid, [f"0x{pid:04X}", str(pid), (colours.get(pid), content), fmt, kind,
                               (math.sqrt(r / top), colours.get(pid, OTHER), rate(r)),
                               str(d["cc_errors"]), scr,
                               (list(s.pid_history.get(pid, [])), colours.get(pid, "#8E8E98"))]))
        self.table.set_rows(rows)
        total = sum(rates.get(p) or 0 for p in keys if p != NULL_PID)
        cc = sum(pids[p]["cc_errors"] for p in keys)
        self.total.set_text(_("Sum ({n} PIDs): {rate} · {cc} continuity errors").format(
            n=len(keys), rate=rate(total), cc=cc))
        # transponder
        tp = st.get("transponder")
        capacity = st.get("capacity")
        busy = st.get("transponder_busy")
        self.tp_btn.set_visible(s.can_measure_transponder() and not st.get("full_ts"))
        self.tp_btn.set_sensitive(not busy)
        self.tp_btn.set_label(_("Measuring…") if busy else _("Measure the whole transponder (10 s)"))
        if tp:
            parts = []
            for i, sv in enumerate(tp["services"][:12]):
                colour = self.accent if sv["sid"] == pmt.get("sid") else \
                    ["#3E7FBF", "#2F5F8F", "#244A70", "#1B3A57", "#30506E"][i % 5]
                parts.append((sv["name"], sv["bps"], rate(sv["bps"]), colour))
            parts.append((_("SI / EPG"), tp["si"], rate(tp["si"]), "#5E5E6A"))
            free = max(0.0, (capacity or tp["total"]) - tp["used"])
            parts.append((_("free / null"), free, rate(free), "#26262D"))
            self.share.set_parts(parts, max(capacity or 0, tp["total"]))
            self.tp_info.set_text(_("{used} used of {cap} · measured {time}").format(
                used=rate(tp["used"]), cap=rate(capacity) if capacity else rate(tp["total"]),
                time=time.strftime("%H:%M:%S", time.localtime(tp["time"]))))
            self.tp_list.set_text("\n".join(
                f"{sv['name']}: {rate(sv['bps'])}" + (" 🔒" if sv["scrambled"] else "")
                for sv in tp["services"]))
        else:
            self.share.set_parts([])
            self.tp_info.set_text(
                _("Only the PIDs of this channel are received. Measure the whole transponder to "
                  "see the other channels on it.") if s.kind == "dvb" else
                _("Internet channels have no transponder."))
            self.tp_list.set_text("")
        self.hkv.set("packets", f"{snap.get('packets', 0):,}".replace(",", "."))
        self.hkv.set("sync", str(snap.get("sync_losses", 0)))
        self.hkv.set("tei", str(snap.get("tei", 0)))
        self.hkv.set("cc", str(snap.get("cc_errors", 0)))
        pd = pids.get(pmt.get("pcr_pid"), {})
        self.hkv.set("pcr", (num(pd["pcr_avg"] * 1000, 1, "ms") + " · max " + num(pd["pcr_max"] * 1000, 1, "ms"))
                     if "pcr_avg" in pd else "")
        self.hkv.set("jitter", ("± " + num(pd["pcr_jitter"] * 1000, 2, "ms")) if pd.get("pcr_jitter") is not None else "")
        self.hkv.set("jumps", str(pd.get("pcr_jumps", 0)))
        self.hkv.set("null", rate(rates.get(NULL_PID)))
        f = st.get("pid_filter")
        self.hkv.set("filter", (_("whole transponder") if f == "8192" else
                                _("{n} PIDs").format(n=len(f.split(":")))) if f else "", bool(f))


class MediaPage(Page):
    name, title = "media", _("Video & Audio")

    def build(self):
        box = page_box()
        row = Gtk.Box(spacing=12)
        c = card(_("Video"))
        c.set_hexpand(True)
        self.vkv = KV()
        for key, name in (("codec", _("Codec")), ("profile", _("Profile / level")),
                          ("res", _("Resolution")), ("scan", _("Scan")), ("fps", _("Frame rate")),
                          ("aspect", _("Aspect ratio")), ("afd", "AFD"), ("bars", _("Black bars")),
                          ("telecine", _("Film (3:2 pulldown)")), ("colour", _("Colour")),
                          ("hdr", "HDR"), ("decoder", _("Decoder")), ("frames", _("Frames shown / dropped")),
                          ("nominal", _("Nominal bit rate")), ("measured", _("Measured bit rate")),
                          ("vbv", _("VBV buffer"))):
            self.vkv.row(key, name)
        c.append(self.vkv)
        row.append(c)
        c = card(_("Frame sizes"))
        c.set_size_request(420, -1)
        self.frames = FrameBars(height=110)
        c.append(self.frames)
        legend = Gtk.Box(spacing=12)
        for kind, name in (("I", _("I frame")), ("P", _("P frame")), ("B", _("B frame"))):
            b = Gtk.Box(spacing=5)
            sw = Gtk.DrawingArea(content_width=9, content_height=9, valign=Gtk.Align.CENTER)
            sw.set_draw_func(lambda _a, cr, w, h, k=kind: (
                cr.set_source_rgb(*charts.rgb(FrameBars.COLOURS[k])), cr.rectangle(0, 0, w, h), cr.fill()))
            b.append(sw)
            b.append(label(name, ["lv-dim"]))
            legend.append(b)
        c.append(legend)
        self.fkv = KV()
        for key, name in (("gop", _("GOP length")), ("iavg", _("I frame average")),
                          ("pavg", _("P/B frame average"))):
            self.fkv.row(key, name)
        c.append(self.fkv)
        row.append(c)
        box.append(row)
        c = card(_("Audio tracks"))
        self.atable = Table([("PID", "text", 0, False), (_("Language"), "swatch", 0, True),
                             (_("Format"), "text", 0, True), (_("Channels"), "text", 0, False),
                             (_("Sample rate"), "text", 1, False), (_("Nominal"), "text", 1, False),
                             (_("Measured"), "text", 1, False), (_("Dialnorm"), "text", 1, False),
                             (_("Notes"), "text", 0, True)], on_activate=self._select)
        c.append(self.atable)
        c.append(label(_("Click a track to listen to it."), ["lv-dim"]))
        box.append(c)
        c = card(_("Loudness (EBU R 128)"))
        figs = Gtk.Box(spacing=28)
        self.lf = {}
        for key, name in (("momentary", _("Momentary")), ("short", _("Short term")),
                          ("integrated", _("Integrated")), ("lra", _("Loudness range")),
                          ("peak", _("Sample peak"))):
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            b.append(label(name, ["lv-dim"]))
            v = label("–", ["lv-big"])
            b.append(v)
            figs.append(b)
            self.lf[key] = v
        c.append(figs)
        self.loud_chart = Chart(height=120, unit="LUFS", y_min=-50, y_max=-5)
        c.append(self.loud_chart)
        self.loud_note = label("", ["lv-dim"], wrap=True)
        c.append(self.loud_note)
        box.append(c)
        c = card(_("Teletext and subtitles"))
        self.tt = label("", wrap=True, selectable=True)
        c.append(self.tt)
        box.append(c)
        return box

    def _select(self, key):
        p = self.session.player
        if p is not None and self.session.is_on_screen():
            p.select_audio(key)

    def update(self, st):
        s = self.session
        snap = st.get("snap") or {}
        pmt = snap.get("pmt") or {}
        es = snap.get("es") or {}
        rates = st.get("rates") or {}
        vi = st.get("video") or {}
        vpid = next((pid for pid, d in pmt.get("streams", []) if d["kind"] == "video"), None)
        vid = es.get(vpid, {}) if vpid is not None else {}
        vd = dict(pmt.get("streams", [])).get(vpid, {})
        on = st.get("on_screen")
        self.vkv.set("codec", vid.get("codec") or vd.get("codec") or vi.get("codec", ""))
        self.vkv.set("profile", " · ".join(x for x in (vid.get("profile"), vid.get("level") and
                                                       _("level {l}").format(l=vid["level"]),
                                                       vid.get("tier") and _("tier {t}").format(t=vid["tier"])) if x))
        w, h = vi.get("width") or vid.get("width"), vi.get("height") or vid.get("height")
        self.vkv.set("res", f"{w} × {h}" if w and h else "")
        im = vi.get("interlace-mode", "")
        scan = {"progressive": _("progressive"), "interleaved": _("interlaced"),
                "mixed": _("mixed"), "fields": _("fields")}.get(im, "")
        if not scan and "interlaced" in vid:
            scan = _("interlaced capable") if vid["interlaced"] else _("progressive")
        if vi.get("field-order"):
            scan += f" · {vi['field-order']}"
        self.vkv.set("scan", scan)
        fps = vi.get("fps") or vid.get("fps") or (1000 / vid["frame_ms"] if vid.get("frame_ms") else None)
        self.vkv.set("fps", num(fps, 3 if fps and fps % 1 else 0, "fps") if fps else "")
        par = vi.get("par") or vid.get("sar")
        self.vkv.set("aspect", " · ".join(x for x in (vid.get("aspect", ""),
                                                      f"{_('pixel')} {par[0]}:{par[1]}" if par else "") if x))
        afd = vid.get("afd")
        self.vkv.set("afd", f"{afd} – {AFD_NAMES.get(afd, '')}" if afd is not None else _("not sent"))
        bars = st.get("bars")
        if bars:
            t, b, lft, r = bars
            if t + b > 0.05:
                txt = _("letterbox {pct} % top and bottom").format(pct=round((t + b) * 50))
            elif lft + r > 0.05:
                txt = _("pillarbox {pct} % at the sides").format(pct=round((lft + r) * 50))
            else:
                txt = _("none")
            if w and h and (t + b or lft + r):
                aw, ah = w * (1 - lft - r), h * (1 - t - b)
                par_v = (par[0] / par[1]) if par and par[1] else 1
                txt += " · " + _("picture {ratio}:1").format(ratio=num(aw * par_v / max(1, ah), 2))
            self.vkv.set("bars", txt)
        else:
            self.vkv.set("bars", _("only for the channel on screen") if not on else "–")
        tc = vid.get("telecine")
        self.vkv.set("telecine", (_("yes, repeated fields") if tc else _("no")) if tc is not None else "", tc is not None)
        colour = " · ".join(x for x in (vid.get("chroma"), f"{vid['bit_depth']} bit" if vid.get("bit_depth") else "",
                                        vid.get("primaries"), vi.get("colorimetry", "")) if x)
        self.vkv.set("colour", colour)
        tr = vid.get("transfer", "")
        cm = vi.get("colorimetry", "")
        hdr = "HDR10 (PQ)" if "PQ" in tr or "smpte2084" in cm or "bt2100-pq" in cm else \
            ("HLG" if "HLG" in tr or "hlg" in cm or "arib-std-b67" in cm else "SDR")
        if vi.get("mastering-display-info"):
            hdr += " · " + _("mastering display data")
        self.vkv.set("hdr", hdr if (tr or cm) else "")
        self.vkv.set("decoder", vi.get("decoder", "") if on else _("only for the channel on screen"))
        self.vkv.set("frames", f"{vi.get('rendered', 0)} / {vi.get('dropped', 0)}" if "dropped" in vi else "")
        nominal = vid.get("nominal_bps") or vd.get("max_bps")
        self.vkv.set("nominal", (rate(nominal) + ("" if vid.get("nominal_bps") else " " + _("(maximum)")))
                     if nominal else _("not signalled (variable)"))
        hist = s.window_history()
        vr = [hh["layers"].get(vpid) for hh in hist if vpid in hh.get("layers", {})]
        self.vkv.set("measured", _("now {now} · Ø {avg} · peak {max}").format(
            now=rate(rates.get(vpid)), avg=rate(sum(vr) / len(vr)), max=rate(max(vr))) if vr else "")
        self.vkv.set("vbv", f"{vid['vbv_bytes'] // 1024} KiB" if vid.get("vbv_bytes") else "", bool(vid.get("vbv_bytes")))
        frames = vid.get("frames") or []
        self.frames.set_frames(frames)
        self.fkv.set("gop", _("{n} frames").format(n=vid["gop"]) + (f" ({num(vid['gop'] / fps, 2, 's')})" if fps else "")
                     if vid.get("gop") else "")
        self.fkv.set("iavg", f"{vid['i_frame_avg'] / 1024:.0f} KiB" if vid.get("i_frame_avg") else "")
        pb = [sz for sz, t in frames if t in ("P", "B")]
        self.fkv.set("pavg", f"{sum(pb) / len(pb) / 1024:.0f} KiB" if pb else "")
        # audio
        rows = []
        tracks, cur = st.get("tracks") or ([], -1)
        ai = 0
        for pid, d in pmt.get("streams", []):
            if d["kind"] != "audio":
                continue
            e = es.get(pid, {})
            notes = []
            if d.get("audio_description"):
                notes.append(_("audio description"))
            if d.get("hard_of_hearing"):
                notes.append(_("clean audio"))
            if e.get("mode") and e["mode"] not in ("stereo",):
                notes.append(e["mode"])
            rows.append((ai, [str(pid), (PALETTE[ai % len(PALETTE)], d.get("language") or "–"),
                              e.get("codec") or d.get("codec", ""), e.get("layout", ""),
                              f"{e['rate'] / 1000:g} kHz" if e.get("rate") else "",
                              rate(e.get("nominal_bps")) if e.get("nominal_bps") else "–",
                              rate(rates.get(pid)), f"{e['dialnorm']} dB" if e.get("dialnorm") else "–",
                              ", ".join(notes)]))
            ai += 1
        if not rows:
            for i, t in enumerate(tracks):
                rows.append((i, ["–", (PALETTE[i % len(PALETTE)], t.language_name or "–"), t.codec or "",
                                 t.layout or "", "", "", "", "", ""]))
        self.atable.set_rows(rows, selected=cur if on else None)
        loud = st.get("loudness") or {}
        for key in ("momentary", "short", "integrated", "lra"):
            v = loud.get(key)
            self.lf[key].set_text(num(v, 1, "LU" if key == "lra" else "LUFS")
                                  if v is not None and math.isfinite(v) else "–")
        pk = loud.get("recent_peak")
        self.lf["peak"].set_text(num(pk, 1, "dBFS") if pk is not None and math.isfinite(pk) else "–")
        now = time.time()
        self.loud_chart.seconds = s.window_seconds
        jumps = [(e[0], "#BF5AF2") for e in s.events if e[1] == "loudness"]
        self.loud_chart.set_data([("lufs", [(hh["t"], hh.get("loudness") if hh.get("loudness") is not None
                                             and math.isfinite(hh["loudness"]) else None) for hh in hist],
                                   "#BF5AF2")], now, lines=[(-23, _("EBU target −23 LUFS"))], markers=jumps)
        jumps_n = len(loud.get("jumps", []))
        self.loud_note.set_text(
            _("Measured on the decoded sound of the channel on screen, before the equaliser.") +
            (" " + _("{n} loudness jumps of 6 LU or more (programme ↔ advertising).").format(n=jumps_n)
             if jumps_n else "") if on else _("Loudness is measured for the channel on screen only."))
        lines = []
        for pid, d in pmt.get("streams", []):
            if d["kind"] == "teletext":
                types = {1: _("index"), 2: _("subtitles"), 3: _("info"), 4: _("schedule"),
                         5: _("subtitles (hard of hearing)")}
                lines.append(_("Teletext PID {pid}:").format(pid=pid))
                for lang, ttype, page in d.get("pages", []):
                    lines.append(f"   {page}  {lang}  {types.get(ttype, ttype)}")
            elif d["kind"] == "subtitle":
                lines.append(_("DVB subtitles PID {pid}:").format(pid=pid) + " " + ", ".join(
                    f"{lang} (0x{t:02X})" for lang, t in d.get("subtitles", [])))
        self.tt.set_text("\n".join(lines) or _("No teletext or subtitles in this channel."))


class TreeItem(GObject.Object):
    def __init__(self, node):
        super().__init__()
        self.node = node


class TablesPage(Page):
    name, title = "tables", _("Tables & EPG")

    def build(self):
        box = page_box()
        row = Gtk.Box(spacing=12)
        c = card(_("SI tables"))
        c.set_hexpand(True)
        self.table = Table([(_("Table"), "text", 0, True), ("PID", "text", 0, False),
                            (_("Version"), "text", 1, False), (_("Changes"), "text", 1, False),
                            (_("Repetition"), "text", 1, False), (_("Sections"), "text", 1, False),
                            (_("Status"), "chip", 0, False)], on_activate=self._pick)
        c.append(self.table)
        row.append(c)
        c = card(_("Programme guide and clock"))
        c.set_size_request(380, -1)
        self.ekv = KV()
        for key, name in (("now", _("Now")), ("next", _("Next")), ("events", _("Events in the guide")),
                          ("until", _("Guide reaches")), ("pf", _("EIT now/next")),
                          ("sched", _("EIT schedule")), ("clock", _("Clock of the channel")),
                          ("offset", _("Difference to this computer")), ("zone", _("Local time offset")),
                          ("network", _("Network")), ("provider", _("Provider"))):
            self.ekv.row(key, name)
        c.append(self.ekv)
        row.append(c)
        box.append(row)
        c = card(_("Raw view"))
        head = c.get_first_child()
        self.raw_pick = Gtk.DropDown.new_from_strings(["–"])
        self.raw_pick.connect("notify::selected", lambda *_: self._show_raw())
        head.append(self.raw_pick)
        self._raw_names = []
        self.root_store = Gio.ListStore(item_type=TreeItem)
        self.tree_model = Gtk.TreeListModel.new(self.root_store, False, True, self._children)
        fac = Gtk.SignalListItemFactory()
        fac.connect("setup", self._setup_row)
        fac.connect("bind", self._bind_row)
        lv = Gtk.ListView(model=Gtk.NoSelection(model=self.tree_model), factory=fac)
        sw = Gtk.ScrolledWindow(min_content_height=260, hexpand=True)
        sw.set_child(lv)
        c.append(sw)
        self.hex = Gtk.TextView(editable=False, monospace=True, css_classes=["lv-mono"])
        sw2 = Gtk.ScrolledWindow(min_content_height=140, hexpand=True)
        sw2.set_child(self.hex)
        c.append(subtitle(_("Hex")))
        c.append(sw2)
        box.append(c)
        self._raw_shown = None
        return box

    def _children(self, item):
        if not item.node.children:
            return None
        store = Gio.ListStore(item_type=TreeItem)
        for ch in item.node.children:
            store.append(TreeItem(ch))
        return store

    def _setup_row(self, _f, li):
        exp = Gtk.TreeExpander()
        b = Gtk.Box(spacing=10)
        b.append(Gtk.Label(xalign=0))
        b.append(Gtk.Label(xalign=0, css_classes=["lv-dim"], selectable=True, ellipsize=3, hexpand=True))
        exp.set_child(b)
        li.set_child(exp)

    def _bind_row(self, _f, li):
        row = li.get_item()
        exp = li.get_child()
        exp.set_list_row(row)
        node = row.get_item().node
        b = exp.get_child()
        b.get_first_child().set_text(node.label)
        b.get_last_child().set_text(node.value)

    def _pick(self, key):
        tid = key[0]
        name = {0x00: "PAT", 0x01: "CAT", 0x02: "PMT", 0x40: "NIT", 0x42: "SDT", 0x4E: "EIT p/f",
                0x70: "TDT", 0x73: "TOT", 0x74: "AIT"}.get(tid)
        if name in self._raw_names:
            self.raw_pick.set_selected(self._raw_names.index(name))

    def _show_raw(self, force=False):
        snap = (self.session.state or {}).get("snap") or {}
        raw = snap.get("raw") or {}
        i = self.raw_pick.get_selected()
        if not self._raw_names or i >= len(self._raw_names):
            return
        name = self._raw_names[i]
        sec = raw.get(name)
        if sec is None or (sec == self._raw_shown and not force):
            return
        self._raw_shown = sec
        self.root_store.remove_all()
        self.root_store.append(TreeItem(sitree.decode(sec)))
        self.tree_model.get_row(0).set_expanded(True) if self.tree_model.get_n_items() else None
        self.hex.get_buffer().set_text(sitree.hexdump(sec))

    def update(self, st):
        s = self.session
        snap = st.get("snap") or {}
        rows = []
        for t in snap.get("tables", []):
            age = t["age"]
            iv = t["interval"]
            limit = {0x00: 0.5, 0x02: 0.5, 0x42: 2.5, 0x40: 10.5, 0x4E: 2.5, 0x70: 30.5}.get(t["tid"])
            status = (_("missing"), "r") if age > max(30, (iv or 0) * 5) else \
                ((_("slow"), "y") if limit and iv and iv > limit else (_("ok"), "g"))
            name = t["name"]
            if t["tid"] == 0x02:
                name += f" (SID {t['ext']})"
            elif t["ext"] == -2:
                name += " " + _("(all services)")
            elif t["ext"] == -3:
                name += " " + _("(other services)")
            rows.append(((t["tid"], t["ext"]), [name, str(t["pid"]),
                                                str(t["version"]) if t["version"] >= 0 else "–",
                                                str(t["versions"]),
                                                (num(iv, 2 if iv < 1 else 1, "s") if iv else "–"),
                                                str(t["count"]), status]))
        rows.sort(key=lambda r: r[0])
        self.table.set_rows(rows)
        names = sorted((snap.get("raw") or {}).keys())
        if names != self._raw_names:
            cur = self._raw_names[self.raw_pick.get_selected()] if self._raw_names and \
                self.raw_pick.get_selected() < len(self._raw_names) else "PMT"
            self._raw_names = names
            self.raw_pick.set_model(Gtk.StringList.new(names or ["–"]))
            if cur in names:
                self.raw_pick.set_selected(names.index(cur))
        self._show_raw()
        epg = s.app.epg
        key = s.channel.epg_key
        n, nx = epg.now_next(key)
        self.ekv.set("now", f"{time.strftime('%H:%M', time.localtime(n.start))} {n.title}" if n else "")
        self.ekv.set("next", f"{time.strftime('%H:%M', time.localtime(nx.start))} {nx.title}" if nx else "")
        evs = epg.for_key(key)
        self.ekv.set("events", str(len(evs)))
        self.ekv.set("until", time.strftime("%a %d.%m. %H:%M", time.localtime(evs[-1].end)) if evs else "")
        tabs = {(t["tid"], t["ext"]): t for t in snap.get("tables", [])}
        pf = tabs.get((0x4E, s.channel.sid))
        self.ekv.set("pf", _("seen {s} s ago").format(s=round(pf["age"])) if pf else _("not seen"))
        sch = tabs.get((0x50, -2))
        self.ekv.set("sched", _("{n} sections").format(n=sch["count"]) if sch else _("not seen"))
        off = snap.get("utc_offset")
        if off is not None:
            self.ekv.set("clock", time.strftime("%H:%M:%S", time.localtime(time.time() + off)))
            kind = "" if abs(off) < 2 else (" ⚠ " + _("timers may start early or late"))
            self.ekv.set("offset", ("+" if off >= 0 else "") + num(off, 0, "s") + kind)
        else:
            self.ekv.set("clock", _("no TDT/TOT"))
            self.ekv.set("offset", "")
        lo = snap.get("local_offset")
        self.ekv.set("zone", (("UTC+" if lo >= 0 else "UTC−") + f"{abs(lo) // 60}:{abs(lo) % 60:02d}")
                     if lo is not None else "", lo is not None)
        self.ekv.set("network", snap.get("network", ""))
        sv = (snap.get("services") or {}).get(s.channel.sid, {})
        self.ekv.set("provider", sv.get("provider", "") or s.channel.category)


class EncryptionPage(Page):
    name, title = "encryption", _("Encryption")

    def build(self):
        box = page_box()
        c = card(_("Status"))
        b = Gtk.Box(spacing=10)
        self.chip = chip("")
        self.text = label("", wrap=True, hexpand=True)
        b.append(self.chip)
        b.append(self.text)
        c.append(b)
        self.kv = KV()
        for key, name in (("sdt", _("SDT free_CA_mode")), ("provider", _("Descrambling")),
                          ("state", _("Descrambler state")), ("share", _("Scrambled packets"))):
            self.kv.row(key, name)
        c.append(self.kv)
        box.append(c)
        c = card(_("Conditional access systems"))
        self.ca = Table([(_("CA system ID"), "text", 0, False), (_("System"), "text", 0, True),
                         (_("ECM PID"), "text", 0, False), (_("For"), "text", 0, False),
                         (_("ECM rate"), "text", 1, False)])
        c.append(self.ca)
        self.emm = label("", ["lv-dim"], wrap=True)
        c.append(self.emm)
        box.append(c)
        c = card(_("Scrambling per stream"))
        self.es = Table([("PID", "text", 0, False), (_("Content"), "text", 0, True),
                         (_("Scrambled"), "text", 1, False), (_("Key changes"), "text", 1, False),
                         (_("Crypto period"), "text", 1, False)])
        c.append(self.es)
        c.append(label(_("A key change is the switch between the even and the odd control word; "
                         "the crypto period is the time between two of them."), ["lv-dim"], wrap=True))
        box.append(c)
        return box

    def update(self, st):
        s = self.session
        snap = st.get("snap") or {}
        pmt = snap.get("pmt") or {}
        pids = snap.get("pids") or {}
        rates = st.get("rates") or {}
        cas = ca_lines(snap)
        streams = pmt.get("streams", [])
        scr = sum(pids.get(p, {}).get("scrambled", 0) for p, _d in streams)
        tot = sum(pids.get(p, {}).get("packets", 0) for p, _d in streams)
        if cas or scr:
            set_chip(self.chip, _("Encrypted"), "y")
            self.text.set_text(_("This channel is encrypted. LinDVB descrambles it only with your own "
                                 "subscription card (softcam or CI module)."))
        else:
            set_chip(self.chip, _("Free-to-air"), "g")
            self.text.set_text(_("No conditional access in the PMT and no scrambled packets."))
        sv = (snap.get("services") or {}).get(s.channel.sid)
        self.kv.set("sdt", (_("scrambled") if sv.get("scrambled") else _("free")) if sv else "")
        cap = s.capture
        prov = getattr(cap, "ca", None) if cap is not None else None
        self.kv.set("provider", prov.name if prov else s.app.settings.get("ca_provider", "none"))
        self.kv.set("state", prov.status if prov else "–")
        self.kv.set("share", f"{scr * 100 // max(1, tot)} %" if tot else "–")
        self.ca.set_rows([((caid, pid, lvl), [f"0x{caid:04X}", name or _("unknown"), f"0x{pid:04X}",
                                              lvl if lvl != "program" else _("all streams"),
                                              rate(rates.get(pid)) if pid in rates else _("not received")])
                          for caid, name, pid, lvl in cas])
        emm = snap.get("cat") or []
        self.emm.set_text(_("EMM (CAT): {list}").format(list=", ".join(
            f"{ca_system_name(c) or hex(c)} PID 0x{p:04X}" for c, p in emm)) if emm else
            _("No CAT with EMM streams received."))
        rows = []
        for pid, d in streams:
            pd = pids.get(pid, {})
            if not pd:
                continue
            cp = pd.get("crypto_period")
            rows.append((pid, [str(pid), stream_label(d, pid),
                               f"{pd['scrambled'] * 100 // max(1, pd['packets'])} %",
                               str(pd.get("parity_changes", 0)), num(cp, 1, "s") if cp else "–"]))
        self.es.set_rows(rows)


class HbbtvPage(Page):
    name, title = "hbbtv", "HbbTV"

    def build(self):
        box = page_box()
        c = card(_("Applications (AIT)"))
        self.info = label("", ["lv-dim"], wrap=True)
        c.append(self.info)
        self.apps = Table([(_("Name"), "text", 0, True), (_("Organisation / app"), "text", 0, False),
                           (_("Control"), "chip", 0, False), (_("Profile"), "text", 0, False),
                           (_("Address"), "text", 0, True)])
        c.append(self.apps)
        box.append(c)
        c = card(_("Data carousels (DSM-CC)"))
        self.dsm = Table([("PID", "text", 0, False), (_("Component tag"), "text", 0, False),
                          (_("Type"), "text", 0, True), (_("Bit rate"), "text", 1, False)])
        c.append(self.dsm)
        self.dsm_note = label("", ["lv-dim"], wrap=True)
        c.append(self.dsm_note)
        box.append(c)
        return box

    def update(self, st):
        s = self.session
        snap = st.get("snap") or {}
        pmt = snap.get("pmt") or {}
        rates = st.get("rates") or {}
        ait = getattr(s.capture, "ait", None) if s.capture is not None else None
        ait_pid = next((pid for pid, d in pmt.get("streams", []) if d.get("ait")), None)
        rows = []
        if ait is not None:
            self.info.set_text(_("AIT version {v} on PID {pid}").format(v=ait.version, pid=ait_pid or "–") +
                               (" · " + _("test application") if ait.test else ""))
            for a in ait.apps:
                prof = ", ".join(".".join(map(str, v)) for _p, v in a.profiles) or "–"
                code = AIT_CODES.get(a.control_code, str(a.control_code))
                rows.append((a.key, [a.name() or "–", f"0x{a.org_id:X} / 0x{a.app_id:X}",
                                     (code, "a" if code == "AUTOSTART" else ""), prof, ait.url(a) or "–"]))
        else:
            self.info.set_text(_("The channel announces an AIT on PID {pid}; it has not arrived yet.").format(pid=ait_pid)
                               if ait_pid else _("This channel has no HbbTV application."))
        self.apps.set_rows(rows)
        drows = []
        for pid, d in pmt.get("streams", []):
            if d.get("codec") == "DSM-CC carousel" or d["stream_type"] in (0x0B, 0x0C, 0x0D):
                drows.append((pid, [str(pid), str(d.get("component_tag", "–")), d.get("codec", ""),
                                    rate(rates.get(pid))]))
        self.dsm.set_rows(drows)
        self.dsm_note.set_text("" if drows else _("No object carousel in this channel."))


PAGES = (OverviewPage, SignalPage, TransportPage, MediaPage, TablesPage, EncryptionPage, HbbtvPage)


# --------------------------------------------------------------------------
#  the window
# --------------------------------------------------------------------------

class AnalysisWindow(Gtk.Window):
    def __init__(self, app, parent, channel):
        super().__init__(title=_("Channel Analysis – {name}").format(name=channel.name),
                         transient_for=None, default_width=1380, default_height=900)
        _install_css()
        self.app = app
        self.add_css_class("lindvb")
        self.add_css_class("lv-analysis")
        self.accent = resolve_accent(app.settings)
        self.session = AnalysisSession(app, channel)
        if not hasattr(app, "analysis_sessions"):
            app.analysis_sessions = []
        app.analysis_sessions.append(self.session)
        self._tick_count = 0

        hb = Gtk.HeaderBar()
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title_l = label("", ["title"], xalign=0.5)
        self.sub_l = label("", ["subtitle"], xalign=0.5)
        tbox.append(self.title_l)
        tbox.append(self.sub_l)
        hb.set_title_widget(tbox)
        self.pause_btn = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic",
                                          tooltip_text=_("Freeze the measurement"))
        self.pause_btn.connect("toggled", self._on_pause)
        hb.pack_start(self.pause_btn)
        menu = Gio.Menu()
        for label_, act in ((_("Copy report as text"), "an.copy"),
                            (_("Save report as text…"), "an.save-text"),
                            (_("Save report as JSON…"), "an.save-json"),
                            (_("Save history as CSV…"), "an.save-csv"),
                            (_("Save window as picture…"), "an.save-png"),
                            (_("Save 30 s of the stream…"), "an.save-ts")):
            menu.append(label_, act)
        exp = Gtk.MenuButton(icon_name="document-save-symbolic", menu_model=menu,
                             tooltip_text=_("Export"))
        hb.pack_start(exp)
        more = Gio.Menu()
        more.append(_("Compare with another channel…"), "an.compare")
        more.append(_("Dish alignment aid…"), "an.align")
        more.append(_("Analyse another channel…"), "an.other")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=more))
        self.set_titlebar(hb)
        group = Gio.SimpleActionGroup()
        for name, fn in (("copy", self._copy), ("save-text", lambda: self._save("txt")),
                         ("save-json", lambda: self._save("json")), ("save-csv", lambda: self._save("csv")),
                         ("save-png", lambda: self._save("png")), ("save-ts", lambda: self._save("ts")),
                         ("compare", lambda: CompareWindow(self).present()),
                         ("align", lambda: AlignWindow(self).present()),
                         ("other", self._choose_other)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            group.add_action(a)
        self.insert_action_group("an", group)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["lv-an-body"])
        bar = Gtk.Box(spacing=12, css_classes=["lv-an-bar"])
        tabs = Gtk.Box(css_classes=["lv-mode"])
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True,
                               css_classes=["lv-an-body"])
        self.tab_buttons = {}
        self.pages = []
        first = None
        for cls in PAGES:
            p = cls(self)
            self.pages.append(p)
            self.stack.add_named(p.widget, p.name)
            b = Gtk.ToggleButton(label=p.title)
            self.tab_buttons[p.name] = b
            if first is None:
                first = b
                b.set_active(True)
            else:
                b.set_group(first)
            b.connect("toggled", lambda btn, n=p.name: btn.get_active() and self._show(n))
            tabs.append(b)
        bar.append(tabs)
        bar.append(Gtk.Box(hexpand=True))
        self.live = label("●", ["lv-live-dot"])
        bar.append(self.live)
        self.elapsed = label("", ["lv-dim", "lv-tnum"])
        bar.append(self.elapsed)
        self.win_dd = Gtk.DropDown.new_from_strings([_("Window: {w}").format(w=t) for _s, t in WINDOWS])
        self.win_dd.set_selected(1)
        self.win_dd.connect("notify::selected", self._on_window)
        bar.append(self.win_dd)
        self.follow = Gtk.ToggleButton(label=_("Follow the channel on screen"))
        self.follow.connect("toggled", lambda b: setattr(self.session, "follow", b.get_active()))
        bar.append(self.follow)
        outer.append(bar)
        self.banner = Gtk.Box(spacing=10, css_classes=["lv-banner"], visible=False)
        self.banner_text = label("", wrap=True, hexpand=True)
        self.banner.append(self.banner_text)
        self.banner_watch = Gtk.Button(label=_("Watch this channel"))
        self.banner_watch.connect("clicked", lambda *_: self.session.switch_to_channel())
        self.banner_retry = Gtk.Button(label=_("Try again"))
        self.banner_retry.connect("clicked", lambda *_: self.session.retune())
        self.banner.append(self.banner_watch)
        self.banner.append(self.banner_retry)
        outer.append(self.banner)
        outer.append(self.stack)
        foot = Gtk.Box(spacing=10, css_classes=["lv-an-foot"])
        self.source = label("", ["lv-dim"], hexpand=True, ellipsize=3)
        foot.append(self.source)
        for text, fn in ((_("Copy as text"), self._copy), (_("Export report…"), lambda: self._save("txt")),
                         (_("Save 30 s of the stream…"), lambda: self._save("ts"))):
            b = Gtk.Button(label=text)
            b.connect("clicked", lambda *_a, fn=fn: fn())
            foot.append(b)
        close = Gtk.Button(label=_("Close"), css_classes=["suggested-action"])
        close.connect("clicked", lambda *_: self.close())
        foot.append(close)
        outer.append(foot)
        self.set_child(outer)
        self._current = "overview"
        self._ids = [self.session.connect("updated", lambda *_: self._update()),
                     self.session.connect("channel-changed", lambda *_: self._update_title()),
                     self.session.connect("message", lambda _s, m: self._message(m))]
        self.connect("close-request", self._on_close)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)
        self._update_title()

    def _on_key(self, _c, kv, _code, state):
        if kv == Gdk.KEY_Escape:
            self.close()
            return True
        # Alt+1 … Alt+7 pick a page
        if state & Gdk.ModifierType.ALT_MASK and Gdk.KEY_1 <= kv <= Gdk.KEY_7:
            self._show(PAGES[kv - Gdk.KEY_1].name)
            return True
        return False

    # -- header and state ------------------------------------------------------
    def _update_title(self):
        c = self.session.channel
        self.set_title(_("Channel Analysis – {name}").format(name=c.name))
        self.title_l.set_text(_("Channel Analysis · {name}").format(name=c.name))
        if c.is_iptv:
            sub = c.url
        else:
            parts = [c.root, f"{c.frequency // 1000 if c.is_sat else c.frequency / 1000:g} MHz"
                     + (f" {c.polarity.upper()}" if c.polarity else ""), c.delsys.upper()]
            if c.modulation and c.modulation != "auto":
                parts.append(c.modulation.upper())
            if c.symbol_rate:
                parts.append(f"{c.symbol_rate} kBd")
            if c.fec and c.fec != "auto":
                parts.append(f"FEC {c.fec}")
            sub = " · ".join(p for p in parts if p)
        self.sub_l.set_text(sub)

    def _show(self, name):
        if not self.tab_buttons[name].get_active():
            self.tab_buttons[name].set_active(True)
            return
        self._current = name
        self.stack.set_visible_child_name(name)
        self._update()

    def _on_window(self, dd, _p):
        self.session.window_seconds = WINDOWS[dd.get_selected()][0]
        self._update()

    def _on_pause(self, btn):
        self.session.paused = btn.get_active()
        self.live.set_text("❚❚" if btn.get_active() else "●")
        for c in ("lv-live-dot", "lv-paused-dot"):
            self.live.remove_css_class(c)
        self.live.add_css_class("lv-paused-dot" if btn.get_active() else "lv-live-dot")

    def _message(self, text):
        self.banner_text.set_text(text)
        self.banner.set_visible(True)

    def _update(self):
        s = self.session
        st = s.state
        if not st:
            return
        elapsed = time.time() - s.started
        self.elapsed.set_text(_("Live · measuring for {t}").format(t=duration(elapsed))
                              if not s.paused else _("Paused"))
        status = st.get("status")
        msgs = {"no_tuner": _("No tuner is free for this channel. Watch it, or try again when a tuner is free."),
                "preempted": _("The tuner was needed for watching or recording."),
                "lost": _("The tuner of this channel was switched to another channel.")}
        if status in msgs and s.capture is None:
            self.banner_text.set_text(msgs[status] + (f" ({st.get('detail')})" if st.get("detail") else ""))
            self.banner.set_visible(True)
            self.banner_watch.set_visible(True)
            self.banner_retry.set_visible(True)
        elif status == "background" and s.kind == "dvb":
            self.banner_text.set_text(_("Measured in the background on a tuner of its own; picture, sound "
                                        "and loudness are only measured for the channel on screen."))
            self.banner.set_visible(True)
            self.banner_watch.set_visible(True)
            self.banner_retry.set_visible(False)
        elif s.kind == "iptv" and not st.get("on_screen"):
            self.banner_text.set_text(_("This internet channel is fetched in the background without "
                                        "decoding it; watch it for picture, sound and buffer figures."))
            self.banner.set_visible(True)
            self.banner_watch.set_visible(True)
            self.banner_retry.set_visible(False)
        else:
            self.banner.set_visible(False)
        cap = s.capture
        if cap is not None:
            src = _("Source: DVB adapter {a}, frontend {f}").format(a=cap.adapter, f=self.app.settings.get("frontend", 0))
            src += " · " + (_("DVB API v5 statistics") if (st.get("frontend") or {}).get("cnr_db") is not None
                            else _("dvbsrc statistics"))
        elif s.kind == "iptv":
            src = _("Source: the player's stream") if st.get("on_screen") else _("Source: background download")
        else:
            src = _("Source: none")
        self.source.set_text(src + " · " + _("updated every second"))
        for p in self.pages:
            if p.name == self._current:
                try:
                    p.update(st)
                except Exception as e:      # noqa: BLE001 - a page must not stop the window
                    print(f"Channel analysis page {p.name}: {e}")

    # -- export ------------------------------------------------------------------
    def _copy(self):
        self.get_clipboard().set(self.session.report_text())

    def _save(self, kind):
        c = self.session.channel
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in c.name)
        dlg = Gtk.FileDialog(initial_name=f"{safe}-{stamp}.{kind}")
        dlg.save(self, None, lambda d, res: self._save_done(d, res, kind))

    def _save_done(self, dlg, res, kind):
        try:
            f = dlg.save_finish(res)
        except GLib.Error:
            return
        path = f.get_path()
        s = self.session
        try:
            if kind == "txt":
                Path(path).write_text(s.report_text(), encoding="utf-8")
            elif kind == "json":
                Path(path).write_text(report_json(s), encoding="utf-8")
            elif kind == "csv":
                Path(path).write_text(s.history_csv(), encoding="utf-8")
            elif kind == "png":
                self._save_png(path)
            elif kind == "ts":
                if not s.save_ts(path, 30, lambda ok, msg: self._message(
                        _("Saved {path}").format(path=msg) if ok else msg)):
                    self._message(_("Nothing to save: the channel is not received."))
                elif s.kind == "iptv":
                    self._message(_("Recording the next 30 seconds…"))
                return
        except OSError as e:
            self._message(str(e))
            return
        self._message(_("Saved {path}").format(path=path))

    def _save_png(self, path):
        child = self.get_child()
        w, h = child.get_width(), child.get_height()
        paintable = Gtk.WidgetPaintable.new(child)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        renderer = self.get_native().get_renderer()
        tex = renderer.render_texture(node, None)
        tex.save_to_png(path)

    def _choose_other(self):
        ChannelChooser(self, lambda c: AnalysisWindow(self.app, self.get_transient_for(), c).present()).present()

    def _on_close(self, *_):
        for i in self._ids:
            self.session.disconnect(i)
        self.session.stop()
        return False


class ChannelChooser(Gtk.Window):
    """Pick a channel by name (for "Analyse another channel" and the comparison)."""

    def __init__(self, parent, on_pick):
        super().__init__(title=_("Choose a channel"), transient_for=parent, modal=True,
                         default_width=420, default_height=520)
        self.add_css_class("lindvb")
        self.on_pick = on_pick
        self.channels = list(parent.app.db.channels)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=10,
                      margin_end=10, margin_top=10, margin_bottom=10)
        self.entry = Gtk.SearchEntry(placeholder_text=_("Channel name"))
        self.entry.connect("search-changed", lambda *_: self._fill())
        self.entry.connect("activate", lambda *_: self._activate(0))
        box.append(self.entry)
        self.list = Gtk.ListBox()
        self.list.connect("row-activated", lambda _l, row: self._activate(row.get_index()))
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.list)
        box.append(sw)
        self.set_child(box)
        self._shown = []
        self._fill()

    def _fill(self):
        text = self.entry.get_text().lower()
        self._shown = [c for c in self.channels if text in c.name.lower()][:200]
        self.list.remove_all()
        for c in self._shown:
            self.list.append(label(f"{c.number or ''}  {c.name}".strip(), margin_start=6,
                                   margin_top=4, margin_bottom=4))

    def _activate(self, i):
        if 0 <= i < len(self._shown):
            c = self._shown[i]
            self.close()
            self.on_pick(c)


class CompareWindow(Gtk.Window):
    """Two channels side by side (idea: satellite against internet of the same channel)."""

    ROWS = (("channel", _("Channel")), ("delivery", _("Delivery")), ("resolution", _("Resolution")),
            ("fps", _("Frame rate")), ("video", _("Video")), ("audio", _("Audio")),
            ("avg_rate", _("Average bit rate")), ("peak_rate", _("Peak bit rate")),
            ("quality", _("Signal quality")), ("cnr", "C/N"), ("margin", _("Margin")),
            ("cc_errors", _("Continuity errors")), ("loudness", _("Loudness")))

    def __init__(self, win):
        super().__init__(title=_("Compare channels"), transient_for=win, default_width=760,
                         default_height=520)
        _install_css()
        self.add_css_class("lindvb")
        self.add_css_class("lv-analysis")
        self.win = win
        self.app = win.app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=14,
                      margin_end=14, margin_top=14, margin_bottom=14)
        top = Gtk.Box(spacing=10)
        outer = Gtk.Box(css_classes=["lv-an-body"])
        outer.append(box)
        box.set_hexpand(True)
        top.append(label(_("Compare {name} with").format(name=win.session.channel.name)))
        self.pick = Gtk.DropDown.new_from_strings(["–"])
        top.append(self.pick)
        add = Gtk.Button(label=_("Analyse another channel…"))
        add.connect("clicked", lambda *_: ChannelChooser(self, self._open_other).present())
        top.append(add)
        box.append(top)
        c = card(_("Comparison"))
        self.grid = Gtk.Grid(column_spacing=24, row_spacing=6)
        self.cells = {}
        for r, (key, name) in enumerate(self.ROWS):
            self.grid.attach(label(name, ["lv-dim"]), 0, r, 1, 1)
            a, b = label("–", ["lv-tnum"]), label("–", ["lv-tnum"])
            self.grid.attach(a, 1, r, 1, 1)
            self.grid.attach(b, 2, r, 1, 1)
            self.cells[key] = (a, b)
        c.append(self.grid)
        c.append(label(_("Figures over the time window of each analysis; picture and loudness only "
                         "for the channel on screen."), ["lv-dim"], wrap=True))
        box.append(c)
        self.set_child(outer)
        self._others = []
        self._timer = GLib.timeout_add(1000, self._tick)
        self.connect("close-request", lambda *_: (GLib.source_remove(self._timer), False)[1])
        self._tick()

    def _open_other(self, channel):
        w = AnalysisWindow(self.app, self.win.get_transient_for(), channel)
        w.present()
        GLib.timeout_add(300, lambda: (self._tick(), False)[1])

    def _tick(self):
        mine = self.win.session
        others = [s for s in getattr(self.app, "analysis_sessions", []) if s is not mine and not s.closed]
        if [id(s) for s in others] != [id(s) for s in self._others]:
            cur = self._others[self.pick.get_selected()] if self._others and \
                self.pick.get_selected() < len(self._others) else None
            self._others = others
            self.pick.set_model(Gtk.StringList.new([s.channel.name for s in others] or ["–"]))
            if cur in others:
                self.pick.set_selected(others.index(cur))
            elif others:
                self.pick.set_selected(len(others) - 1)
        a = mine.summary()
        i = self.pick.get_selected()
        b = others[i].summary() if others and i < len(others) else {}
        for key, (la, lb) in self.cells.items():
            la.set_text(str(a.get(key, "–")))
            lb.set_text(str(b.get(key, "–")) if b else "–")
        return True


class AlignWindow(Gtk.Window):
    """Full screen figures for aligning a dish, with a tone that rises with the signal."""

    def __init__(self, win):
        super().__init__(title=_("Dish alignment aid"), transient_for=win, default_width=900,
                         default_height=600)
        _install_css()
        self.add_css_class("lindvb")
        self.add_css_class("lv-analysis")
        self.win = win
        self.session = win.session
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER,
                      margin_start=40, margin_end=40)
        outer = Gtk.Box(css_classes=["lv-an-body"], orientation=Gtk.Orientation.VERTICAL)
        outer.append(box)
        box.set_vexpand(True)
        self.name = label(self.session.channel.name, ["lv-big"], xalign=0.5)
        box.append(self.name)
        self.value = label("–", ["lv-huge"], xalign=0.5)
        box.append(self.value)
        self.sub = label("", ["lv-big", "lv-dim"], xalign=0.5)
        box.append(self.sub)
        self.bar = Bar(height=22)
        box.append(self.bar)
        self.best = label("", ["lv-dim"], xalign=0.5)
        box.append(self.best)
        row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.tone = Gtk.ToggleButton(label=_("Tone"))
        self.tone.connect("toggled", lambda b: self._tone(b.get_active()))
        row.append(self.tone)
        reset = Gtk.Button(label=_("Reset best value"))
        reset.connect("clicked", lambda *_: setattr(self, "_best", None))
        row.append(reset)
        close = Gtk.Button(label=_("Close (Esc)"))
        close.connect("clicked", lambda *_: self.close())
        row.append(close)
        box.append(row)
        box.append(label(_("Turn the dish slowly; the tone gets higher with a better signal."),
                         ["lv-dim"], xalign=0.5, wrap=True))
        self.set_child(outer)
        self._best = None
        self._pipe = None
        self._beep_on = False
        self._id = self.session.connect("updated", lambda *_: self._update())
        self._beep = GLib.timeout_add(120, self._beep_tick)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", lambda _c, kv, _k, _s: kv == Gdk.KEY_Escape and (self.close() or True))
        self.add_controller(key)
        self.connect("close-request", self._on_close)
        self.fullscreen()
        self._value = 0.0
        self._update()

    def _figure(self):
        sig = (self.session.state or {}).get("signal") or {}
        cnr = sig.get("cnr_db")
        if cnr is not None:
            return cnr, num(cnr, 1), "dB C/N" + (f" · {_('margin')} {num(sig['margin'], 1, 'dB')}"
                                                 if sig.get("margin") is not None else ""), min(1, cnr / 20)
        q = sig.get("quality") or 0
        return q * 100, f"{round(q * 100)}", _("% quality"), q

    def _update(self):
        v, text, sub, frac = self._figure()
        self._value = frac
        self.value.set_text(text)
        self.sub.set_text(sub)
        self.bar.set_value(frac, level_colour(frac))
        if self._best is None or v > self._best:
            self._best = v
        self.best.set_text(_("Best so far: {v}").format(v=num(self._best, 1)))

    def _tone(self, on):
        if on and self._pipe is None:
            self._pipe = Gst.parse_launch("audiotestsrc name=src wave=sine volume=0 ! autoaudiosink")
            self._pipe.set_state(Gst.State.PLAYING)
        elif not on and self._pipe is not None:
            self._pipe.set_state(Gst.State.NULL)
            self._pipe = None

    def _beep_tick(self):
        if self._pipe is not None:
            src = self._pipe.get_by_name("src")
            self._beep_on = not self._beep_on
            src.set_property("freq", 300 + 1500 * self._value)
            src.set_property("volume", 0.15 if self._beep_on and self._value > 0 else 0.0)
        return True

    def _on_close(self, *_):
        self.session.disconnect(self._id)
        GLib.source_remove(self._beep)
        self._tone(False)
        return False


def open_analysis(app, channel):
    """Open the window, or bring up the one already showing *channel*."""
    for s in getattr(app, "analysis_sessions", []):
        if s.channel.key == channel.key and not s.closed:
            for w in Gtk.Window.list_toplevels():
                if isinstance(w, AnalysisWindow) and w.session is s:
                    w.present()
                    return w
    w = AnalysisWindow(app, app.window, channel)
    w.present()
    return w


__all__ = ["AnalysisWindow", "AlignWindow", "CompareWindow", "open_analysis"]
