# SPDX-License-Identifier: GPL-2.0-or-later
"""Small Cairo charts for the channel analysis window, in the player's dark look."""

import math

from gi.repository import Gtk, Pango, PangoCairo

TEXT = (0.949, 0.949, 0.961)
TEXT_DIM = (0.639, 0.639, 0.678)
GRID = (1, 1, 1, 0.07)
TRACK = (0.149, 0.149, 0.176)
GOOD, FAIR, POOR, BAD = "#3FBF5F", "#F2CC0C", "#F5891F", "#E5484D"
PALETTE = ["#BF5AF2", "#64D2FF", "#FF6482", "#FFD60A", "#30D158", "#FF9F0A", "#5AC8FA"]
OTHER = "#5E5E6A"
EVENT_COLOURS = {"unc": BAD, "cc": FAIR, "lock": BAD, "rebuffer": POOR, "pcr": POOR,
                 "loudness": "#BF5AF2", "version": "#5AC8FA"}


def rgb(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))


def level_colour(fraction):
    if fraction is None or fraction <= 0:
        return OTHER
    if fraction < 0.3:
        return BAD
    if fraction < 0.5:
        return POOR
    if fraction < 0.7:
        return FAIR
    return GOOD


def _text(cr, widget, x, y, text, size=8.5, colour=TEXT_DIM, align="left", bold=False):
    layout = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(f"{'Bold ' if bold else ''}{size}")
    layout.set_font_description(fd)
    layout.set_text(text, -1)
    w, h = layout.get_pixel_size()
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    cr.set_source_rgb(*colour)
    cr.move_to(x, y - h / 2)
    PangoCairo.show_layout(cr, layout)
    return w


def _nice_max(v):
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


class Chart(Gtk.DrawingArea):
    """A time series chart: lines or stacked areas, y axis on the right."""

    def __init__(self, height=150, stacked=False, unit="", fmt=None, y_min=None, y_max=None,
                 seconds=300):
        super().__init__(hexpand=True, content_height=height)
        self.stacked = stacked
        self.unit = unit
        self.fmt = fmt or (lambda v: f"{v:.0f}")
        self.y_min, self.y_max = y_min, y_max
        self.seconds = seconds
        self.series = []            # [(label, [(t, v)], colour)]
        self.lines = []             # [(value, text)] horizontal marks (average, threshold)
        self.markers = []           # [(t, colour)] vertical marks
        self.now = 0.0
        self.set_draw_func(self._draw)

    def set_data(self, series, now, lines=(), markers=()):
        self.series, self.now = series, now
        self.lines, self.markers = list(lines), list(markers)
        self.queue_draw()

    def _range(self):
        vals = []
        if self.stacked:
            sums = {}
            for _l, pts, _c in self.series:
                for t, v in pts:
                    if v is not None:
                        sums[t] = sums.get(t, 0) + v
            vals = list(sums.values())
        else:
            vals = [v for _l, pts, _c in self.series for _t, v in pts if v is not None]
        vals += [v for v, _t in self.lines]
        lo = self.y_min if self.y_min is not None else (min(vals) if vals else 0)
        hi = self.y_max if self.y_max is not None else (max(vals) if vals else 1)
        if self.y_max is None:
            if self.y_min is not None and self.y_min == 0:
                hi = _nice_max(hi * 1.1)
            else:
                span = (hi - lo) or abs(hi) or 1
                hi += span * 0.15
                lo -= span * 0.15 if self.y_min is None else 0
        if hi <= lo:
            hi = lo + 1
        return lo, hi

    def _draw(self, _area, cr, width, height):
        right, bottom, top = 54, 16, 6
        w, h = max(10, width - right), max(10, height - bottom - top)
        lo, hi = self._range()

        def ty(v):
            return top + h - (v - lo) / (hi - lo) * h

        def tx(t):
            return w - (self.now - t) / self.seconds * w
        cr.set_line_width(1)
        for i in range(5):
            y = top + h * i / 4
            cr.set_source_rgba(*GRID)
            cr.move_to(0, y + 0.5)
            cr.line_to(w, y + 0.5)
            cr.stroke()
            v = hi - (hi - lo) * i / 4
            _text(cr, self, w + 6, y, self.fmt(v) + (f" {self.unit}" if i == 0 and self.unit else ""))
        mins = self.seconds // 60
        _text(cr, self, 0, top + h + 9, f"−{mins} min" if mins else f"−{self.seconds} s")
        _text(cr, self, w, top + h + 9, "▸", align="right")
        cr.save()
        cr.rectangle(0, 0, w, top + h)
        cr.clip()
        base = {}
        for _label, pts, colour in self.series:
            pts = [(t, v) for t, v in pts if v is not None and t >= self.now - self.seconds - 2]
            if len(pts) < 2:
                continue
            r, g, b = rgb(colour)
            if self.stacked:
                upper = [(t, base.get(t, 0) + v) for t, v in pts]
                cr.move_to(tx(upper[0][0]), ty(upper[0][1]))
                for t, v in upper[1:]:
                    cr.line_to(tx(t), ty(v))
                for t, _v in reversed(pts):
                    cr.line_to(tx(t), ty(base.get(t, 0)))
                cr.close_path()
                cr.set_source_rgba(r, g, b, 0.78)
                cr.fill()
                for t, v in upper:
                    base[t] = v
            else:
                cr.move_to(tx(pts[0][0]), ty(pts[0][1]))
                for t, v in pts[1:]:
                    cr.line_to(tx(t), ty(v))
                cr.set_source_rgba(r, g, b, 1)
                cr.set_line_width(1.6)
                cr.stroke_preserve()
                cr.line_to(tx(pts[-1][0]), top + h)
                cr.line_to(tx(pts[0][0]), top + h)
                cr.close_path()
                cr.set_source_rgba(r, g, b, 0.12)
                cr.fill()
        for t, colour in self.markers:
            if t < self.now - self.seconds:
                continue
            cr.set_source_rgba(*rgb(colour), 0.9)
            cr.set_line_width(1.5)
            cr.move_to(tx(t), top)
            cr.line_to(tx(t), top + h)
            cr.stroke()
        cr.restore()
        for v, text in self.lines:
            if not lo <= v <= hi:
                continue
            y = ty(v)
            cr.set_source_rgba(*TEXT, 0.6)
            cr.set_dash([4, 4])
            cr.set_line_width(1)
            cr.move_to(0, y)
            cr.line_to(w, y)
            cr.stroke()
            cr.set_dash([])
            if text:
                _text(cr, self, 6, y - 8, text, colour=TEXT)


class Sparkline(Gtk.DrawingArea):
    def __init__(self, width=90, height=18, colour="#119CFF"):
        super().__init__(content_width=width, content_height=height)
        self.values = []
        self.colour = colour
        self.set_draw_func(self._draw)

    def set_values(self, values, colour=None):
        self.values = [v for v in values if v is not None]
        if colour:
            self.colour = colour
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        vals = self.values
        if len(vals) < 2:
            return
        hi, lo = max(vals), min(vals)
        if hi - lo < 1e-9:
            hi, lo = hi + 1, lo - 1
        r, g, b = rgb(self.colour)
        cr.move_to(0, h - (vals[0] - lo) / (hi - lo) * (h - 2) - 1)
        for i, v in enumerate(vals[1:], 1):
            cr.line_to(i / (len(vals) - 1) * w, h - (v - lo) / (hi - lo) * (h - 2) - 1)
        cr.set_source_rgba(r, g, b, 1)
        cr.set_line_width(1.4)
        cr.stroke_preserve()
        cr.line_to(w, h)
        cr.line_to(0, h)
        cr.close_path()
        cr.set_source_rgba(r, g, b, 0.14)
        cr.fill()


class Ring(Gtk.DrawingArea):
    """The quality ring: percentage in the middle, level word below."""

    def __init__(self, size=112):
        super().__init__(content_width=size, content_height=size)
        self.fraction, self.text, self.sub, self.colour = 0.0, "–", "", OTHER
        self.set_draw_func(self._draw)

    def set_value(self, fraction, text, sub, colour):
        self.fraction, self.text, self.sub, self.colour = fraction or 0.0, text, sub, colour
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - 8
        cr.set_line_width(10)
        cr.set_source_rgb(*TRACK)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        if self.fraction > 0:
            cr.set_source_rgb(*rgb(self.colour))
            cr.set_line_cap(1)
            cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * min(1, self.fraction))
            cr.stroke()
        _text(cr, self, cx, cy - 4, self.text, size=17, colour=TEXT, align="center", bold=True)
        _text(cr, self, cx, cy + 18, self.sub, size=9, colour=rgb(self.colour), align="center",
              bold=True)


class Bar(Gtk.DrawingArea):
    """A thin meter bar."""

    def __init__(self, height=6, width=-1):
        super().__init__(hexpand=width < 0, content_height=height,
                         content_width=max(width, 20))
        self.fraction, self.colour = 0.0, "#119CFF"
        self.set_draw_func(self._draw)

    def set_value(self, fraction, colour=None):
        self.fraction = max(0.0, min(1.0, fraction or 0.0))
        if colour:
            self.colour = colour
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        rad = h / 2

        def rounded(x, width):
            cr.new_sub_path()
            cr.arc(x + width - rad, rad, rad, -math.pi / 2, math.pi / 2)
            cr.arc(x + rad, rad, rad, math.pi / 2, 3 * math.pi / 2)
            cr.close_path()
        cr.set_source_rgb(*TRACK)
        rounded(0, w)
        cr.fill()
        if self.fraction > 0:
            cr.set_source_rgb(*rgb(self.colour))
            rounded(0, max(h, w * self.fraction))
            cr.fill()


class FrameBars(Gtk.DrawingArea):
    """Size of the last video frames, coloured by picture type."""

    COLOURS = {"I": "#FF9F0A", "P": "#119CFF", "B": "#3E5F80", "": "#5E5E6A"}

    def __init__(self, height=96):
        super().__init__(hexpand=True, content_height=height)
        self.frames = []
        self.set_draw_func(self._draw)

    def set_frames(self, frames):
        self.frames = list(frames)
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        frames = self.frames
        if not frames:
            _text(cr, self, w / 2, h / 2, "–", align="center")
            return
        n = max(1, min(len(frames), int(w // 4)))
        frames = frames[-n:]
        top = max(s for s, _t in frames) or 1
        bw = w / n
        for i, (size, kind) in enumerate(frames):
            bh = max(2, size / top * (h - 2))
            cr.set_source_rgb(*rgb(self.COLOURS.get(kind, OTHER)))
            cr.rectangle(i * bw, h - bh, max(1, bw - 1.2), bh)
            cr.fill()


class ShareBar(Gtk.DrawingArea):
    """One horizontal bar split into labelled parts (transponder occupancy)."""

    def __init__(self, height=36):
        super().__init__(hexpand=True, content_height=height)
        self.parts = []             # [(label, value, sublabel, colour)]
        self.total = 0
        self.set_draw_func(self._draw)

    def set_parts(self, parts, total=None):
        self.parts = list(parts)
        self.total = total or sum(p[1] for p in parts) or 1
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        x = 0.0
        for label, value, sub, colour in self.parts:
            pw = value / self.total * w
            if pw < 1:
                continue
            cr.set_source_rgb(*rgb(colour))
            cr.rectangle(x, 0, max(1, pw - 2), h)
            cr.fill()
            if pw > 48:
                cr.save()
                cr.rectangle(x, 0, pw - 4, h)
                cr.clip()
                _text(cr, self, x + 6, h * 0.32, label, size=8, colour=TEXT, bold=True)
                _text(cr, self, x + 6, h * 0.72, sub, size=7.5, colour=TEXT)
                cr.restore()
            x += pw


class EventStrip(Gtk.DrawingArea):
    """The error timeline: one tick per event on the chart's time axis."""

    ROWS = ("unc", "cc", "lock", "rebuffer", "pcr", "loudness", "version")

    def __init__(self, height=22, seconds=300):
        super().__init__(hexpand=True, content_height=height)
        self.events, self.now, self.seconds = [], 0.0, seconds
        self.set_draw_func(self._draw)

    def set_events(self, events, now):
        self.events, self.now = list(events), now
        self.queue_draw()

    def _draw(self, _a, cr, width, h):
        w = max(10, width - 54)
        cr.set_source_rgb(*TRACK)
        for i in range(0, int(w), 6):
            cr.rectangle(i, h / 2 - 2, 3, 4)
        cr.fill()
        for t, kind, _text_ in self.events:
            if t < self.now - self.seconds:
                continue
            x = w - (self.now - t) / self.seconds * w
            cr.set_source_rgb(*rgb(EVENT_COLOURS.get(kind, OTHER)))
            cr.rectangle(x - 1.5, 1, 3, h - 2)
            cr.fill()
