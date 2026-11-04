# SPDX-License-Identifier: GPL-2.0-or-later
"""Built-in radio visualisations, drawn with cairo.

Each style is a small class with an ``id``, a ``label`` and a
``draw(cr, width, height, frame)`` method, where ``frame`` is an
:class:`lindvb.audioviz.Frame`. Styles may keep state between frames (peak
caps, needles, particles); a new instance is made whenever the user picks a
style. Plugins can add their own styles the same way (see docs/PLUGINS.md).
"""

import math
import random

import cairo

TAU = 2 * math.pi


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def background(cr, w, h, inner=(0.07, 0.07, 0.085), outer=(0.02, 0.02, 0.03)):
    g = cairo.RadialGradient(w / 2, h / 2, 0, w / 2, h / 2, max(w, h) * 0.75)
    g.add_color_stop_rgb(0, *inner)
    g.add_color_stop_rgb(1, *outer)
    cr.set_source(g)
    cr.paint()


def caption(cr, w, h, text, size=None, alpha=0.10, y=None):
    """Large faint station name, the backdrop of a visualisation."""
    if not text:
        return
    cr.save()
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    size = size or max(14.0, min(w / max(6, len(text)) * 1.4, h * 0.16))
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    cr.move_to((w - ext.width) / 2 - ext.x_bearing, (y if y is not None else h / 2) - ext.y_bearing / 2)
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.show_text(text)
    cr.restore()


def footer(cr, w, h, text):
    """Small station name centred at the bottom edge."""
    if text:
        caption(cr, w, h, text, size=max(11.0, h * 0.04), alpha=0.55, y=h - max(14.0, h * 0.05))


def _rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -TAU / 4, 0)
    cr.arc(x + w - r, y + h - r, r, 0, TAU / 4)
    cr.arc(x + r, y + h - r, r, TAU / 4, TAU / 2)
    cr.arc(x + r, y + r, r, TAU / 2, 3 * TAU / 4)
    cr.close_path()


class Style:
    id = ""
    label = ""

    def draw(self, cr, w, h, frame):
        raise NotImplementedError


class Blank(Style):
    """No visualisation: a calm background with the station name."""
    id, label = "off", "Off (station name only)"

    def draw(self, cr, w, h, f):
        background(cr, w, h)
        caption(cr, w, h, f.caption, alpha=0.35)


class SpectrumBars(Style):
    """The classic LED spectrum analyser: green, yellow and red segments
    with falling peak caps."""
    id, label = "bars", "Spectrum analyser (classic)"

    GREEN, YELLOW, RED = rgb("#3DDC84"), rgb("#FFD23F"), rgb("#FF453A")

    def draw(self, cr, w, h, f):
        background(cr, w, h)
        footer(cr, w, h, f.caption)
        n = len(f.bands)
        margin_x, top, bottom = w * 0.06, h * 0.08, h * 0.14
        area_w, area_h = w - 2 * margin_x, h - top - bottom
        gap = max(1.0, area_w / n * 0.18)
        bw = area_w / n - gap
        seg_h = max(3.0, area_h / 32)
        segs = max(4, int(area_h / seg_h))
        seg_gap = max(1.0, seg_h * 0.25)
        for i, v in enumerate(f.bands):
            x = margin_x + i * (bw + gap) + gap / 2
            lit = int(v * segs + 0.5)
            for s in range(segs):
                pos = s / segs
                col = self.RED if pos > 0.85 else self.YELLOW if pos > 0.62 else self.GREEN
                y = top + area_h - (s + 1) * seg_h
                cr.set_source_rgba(*col, 0.95 if s < lit else 0.07)
                cr.rectangle(x, y + seg_gap / 2, bw, seg_h - seg_gap)
                cr.fill()
            p = int(f.peaks[i] * segs)
            if p > 0:
                pos = p / segs
                col = self.RED if pos > 0.85 else self.YELLOW if pos > 0.62 else self.GREEN
                cr.set_source_rgba(*_mix(col, (1, 1, 1), 0.4), 1)
                cr.rectangle(x, top + area_h - p * seg_h - seg_h + seg_gap / 2, bw, seg_h - seg_gap)
                cr.fill()


class VUMeters(Style):
    """Two backlit analogue VU meters with needles (left and right)."""
    id, label = "vu", "VU meters (classic)"

    SCALE = [-20, -10, -7, -5, -3, -2, -1, 0, 1, 2, 3]
    REF_DBFS = -18.0          # 0 VU, the usual broadcast alignment

    @staticmethod
    def _pos(vu):
        """VU reading -> 0..1 along the arc (the scale is linear in voltage)."""
        lo, hi = 10 ** (-20 / 20), 10 ** (3 / 20)
        v = 10 ** (max(-30.0, min(4.0, vu)) / 20)
        return max(-0.03, min(1.03, (v - lo) / (hi - lo)))

    def meters(self, w, h):
        """Position of the two meters: [(x, y, width, height, channel, name)]."""
        if w >= h * 1.2:
            mw = min(w * 0.44, h * 0.9 * 1.6)
            mh = mw / 1.6
            xs = [w / 2 - mw - w * 0.02, w / 2 + w * 0.02]
            ys = [(h - mh) / 2] * 2
        else:
            mh = min(h * 0.42, w * 0.9 / 1.6)
            mw = mh * 1.6
            xs = [(w - mw) / 2] * 2
            ys = [h / 2 - mh - h * 0.02, h / 2 + h * 0.02]
        return [(xs[ch], ys[ch], mw, mh, ch, name) for ch, name in enumerate(("L", "R"))]

    def draw(self, cr, w, h, f):
        background(cr, w, h)
        for x, y, mw, mh, ch, name in self.meters(w, h):
            self.face(cr, x, y, mw, mh, name)
            lx, ly, lr, lit = self.led(x, y, mw, mh, f.peak_db[ch] > -1.0)
            cr.arc(lx, ly, lr, 0, TAU)
            cr.set_source_rgb(*lit)
            cr.fill()
            cx, cy, ex, ey, width = self.needle(x, y, mw, mh, f.vu[ch] - self.REF_DBFS)
            cr.save()
            _rounded(cr, x, y, mw, mh, 8)
            cr.clip()
            cr.set_source_rgba(0, 0, 0, 0.18)
            cr.set_line_width(max(1.5, mh / 70))
            cr.move_to(cx + 3, cy + 3)
            cr.line_to(ex + 3, ey + 3)
            cr.stroke()
            cr.set_source_rgb(0.08, 0.06, 0.05)
            cr.set_line_width(width)
            cr.move_to(cx, cy)
            cr.line_to(ex, ey)
            cr.stroke()
            cr.restore()
            self.glass(cr, x, y, mw, mh)
        footer(cr, w, h, f.caption)

    def _geometry(self, x, y, w, h):
        # pivot below the face, arc in the upper part
        return x + w / 2, y + h * 1.05, h * 0.80, math.radians(-138), math.radians(-42)

    def needle(self, x, y, w, h, vu):
        """Needle from the pivot to its tip: (cx, cy, tip_x, tip_y, line width)."""
        cx, cy, r, a0, a1 = self._geometry(x, y, w, h)
        a = a0 + (a1 - a0) * self._pos(vu)
        return cx, cy, cx + math.cos(a) * r * 1.08, cy + math.sin(a) * r * 1.08, max(1.2, h / 90)

    @staticmethod
    def led(x, y, w, h, clip):
        """Peak LED: (x, y, radius, colour)."""
        return (x + w * 0.92, y + h * 0.84, max(3.0, h / 28),
                (1.0, 0.2, 0.15) if clip else (0.45, 0.20, 0.15))

    def face(self, cr, x, y, w, h, name):
        """The case, backlit face, scale and labels (everything that does not move)."""
        cr.set_source_rgb(0.08, 0.08, 0.09)
        _rounded(cr, x - 6, y - 6, w + 12, h + 12, 12)
        cr.fill()
        g = cairo.LinearGradient(x, y, x, y + h)
        g.add_color_stop_rgb(0, 0.99, 0.93, 0.78)
        g.add_color_stop_rgb(1, 0.90, 0.76, 0.50)
        cr.set_source(g)
        _rounded(cr, x, y, w, h, 8)
        cr.fill()
        cx, cy, r, a0, a1 = self._geometry(x, y, w, h)

        def ang(p):
            return a0 + (a1 - a0) * p

        cr.set_line_width(max(1.0, h / 120))
        cr.set_source_rgb(0.12, 0.10, 0.08)
        cr.arc(cx, cy, r, a0, ang(self._pos(0)))
        cr.stroke()
        cr.set_source_rgb(0.78, 0.10, 0.08)
        cr.set_line_width(max(2.0, h / 40))
        cr.arc(cx, cy, r, ang(self._pos(0)), a1)
        cr.stroke()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(max(8.0, h / 14))
        for mark in self.SCALE:
            a = ang(self._pos(mark))
            red = mark > 0
            cr.set_source_rgb(*((0.78, 0.10, 0.08) if red else (0.12, 0.10, 0.08)))
            cr.set_line_width(max(1.0, h / 100))
            cr.move_to(cx + math.cos(a) * r, cy + math.sin(a) * r)
            cr.line_to(cx + math.cos(a) * (r + h * 0.07), cy + math.sin(a) * (r + h * 0.07))
            cr.stroke()
            if mark in (-20, -10, -7, -5, -3, 0, 3):
                label = f"{mark:+d}" if mark > 0 else str(mark)
                ext = cr.text_extents(label)
                tx = cx + math.cos(a) * (r + h * 0.15) - ext.width / 2 - ext.x_bearing
                ty = cy + math.sin(a) * (r + h * 0.15) - ext.y_bearing / 2
                cr.move_to(tx, ty)
                cr.show_text(label)
        cr.set_font_size(max(10.0, h / 7))
        cr.set_source_rgb(0.12, 0.10, 0.08)
        ext = cr.text_extents("VU")
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, y + h * 0.72)
        cr.show_text("VU")
        cr.set_font_size(max(8.0, h / 12))
        cr.move_to(x + w * 0.06, y + h * 0.9)
        cr.show_text(name)

    @staticmethod
    def glass(cr, x, y, w, h):
        g = cairo.LinearGradient(x, y, x, y + h * 0.5)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.25)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.0)
        cr.set_source(g)
        _rounded(cr, x, y, w, h * 0.5, 8)
        cr.fill()


class Oscilloscope(Style):
    """A green phosphor oscilloscope showing the waveform."""
    id, label = "scope", "Oscilloscope (classic)"

    PHOSPHOR = rgb("#39FF88")

    def draw(self, cr, w, h, f):
        background(cr, w, h, inner=(0.02, 0.06, 0.04), outer=(0.0, 0.02, 0.01))
        cr.set_line_width(1)
        cr.set_source_rgba(*self.PHOSPHOR, 0.08)
        for i in range(1, 10):
            cr.move_to(w * i / 10, 0)
            cr.line_to(w * i / 10, h)
        for i in range(1, 8):
            cr.move_to(0, h * i / 8)
            cr.line_to(w, h * i / 8)
        cr.stroke()
        cr.set_source_rgba(*self.PHOSPHOR, 0.18)
        cr.move_to(0, h / 2)
        cr.line_to(w, h / 2)
        cr.stroke()
        footer(cr, w, h, f.caption)
        wave = f.wave or [0.0, 0.0]
        n = len(wave)
        amp = h * 0.38
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for width, alpha in ((9, 0.06), (4, 0.18), (1.6, 0.95)):
            cr.set_line_width(width)
            cr.set_source_rgba(*self.PHOSPHOR, alpha)
            for i, v in enumerate(wave):
                x = w * i / max(1, n - 1)
                y = h / 2 - max(-1.0, min(1.0, v)) * amp
                (cr.move_to if i == 0 else cr.line_to)(x, y)
            cr.stroke()


class GlowWaves(Style):
    """Modern flowing light waves in the accent colour, cyan and magenta,
    blended additively around a thin centre line."""
    id, label = "glow", "Glow waves (modern)"

    def __init__(self):
        self.waves = [
            # colour, centre (0..1), width, frequency, speed, band group
            (None, 0.44, 0.13, 6.0, 1.3, 0),
            (rgb("#2EE6D6"), 0.56, 0.11, 8.0, -1.7, 1),
            (rgb("#FF2D95"), 0.60, 0.09, 7.0, 2.1, 2),
            (rgb("#7A5CFF"), 0.48, 0.12, 5.0, -1.1, 1),
            ((1.0, 1.0, 1.0), 0.52, 0.07, 9.0, 2.6, 2),
        ]
        self.amps = [0.0] * len(self.waves)

    def draw(self, cr, w, h, f):
        background(cr, w, h, inner=(0.06, 0.08, 0.13), outer=(0.02, 0.025, 0.04))
        footer(cr, w, h, f.caption)
        cy = h / 2
        # centre line, fading towards the ends
        g = cairo.LinearGradient(0, 0, w, 0)
        g.add_color_stop_rgba(0, 1, 1, 1, 0)
        g.add_color_stop_rgba(0.15, 1, 1, 1, 0.75)
        g.add_color_stop_rgba(0.85, 1, 1, 1, 0.75)
        g.add_color_stop_rgba(1, 1, 1, 1, 0)
        cr.set_source(g)
        cr.set_line_width(max(1.5, h / 300))
        cr.move_to(w * 0.08, cy)
        cr.line_to(w * 0.92, cy)
        cr.stroke()
        cr.set_operator(cairo.OPERATOR_SCREEN)
        steps = max(60, int(w / 6))
        for col, c, width, freq, phase, amp in self.params(f, h):
            for scale, alpha in ((1.3, 0.10), (1.0, 0.45)):
                pts = []
                for i in range(steps + 1):
                    u = i / steps
                    env = math.exp(-((u - c) / (width * scale)) ** 2)
                    pts.append((w * u, amp * scale * env * math.sin(u * freq * math.pi + phase)))
                cr.move_to(*((pts[0][0], cy - pts[0][1])))
                for x, y in pts[1:]:
                    cr.line_to(x, cy - y)
                for x, y in reversed(pts):
                    cr.line_to(x, cy + y)
                cr.close_path()
                g = cairo.LinearGradient(0, cy - amp, 0, cy + amp)
                g.add_color_stop_rgba(0, *col, alpha * 0.5)
                g.add_color_stop_rgba(0.5, *_mix(col, (1, 1, 1), 0.35), alpha)
                g.add_color_stop_rgba(1, *col, alpha * 0.5)
                cr.set_source(g)
                cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)

    def params(self, f, h):
        """Advance the wave amplitudes; returns the visible waves as
        [(colour, centre, width, frequency, phase, amplitude in pixels)]."""
        energy = (f.bass, f.mid, f.treble)
        out = []
        for k, (col, centre, width, freq, speed, grp) in enumerate(self.waves):
            col = col or f.accent
            target = min(1.0, energy[grp] * 1.4 + f.beat * 0.25)
            self.amps[k] += (target - self.amps[k]) * min(1.0, f.dt * 8)
            amp = self.amps[k] * h * 0.36
            if amp < 0.5:
                continue
            drift = 0.06 * math.sin(f.t * 0.35 + k * 1.7)
            out.append((col, centre + drift, width, freq, f.t * speed, amp))
        return out


class Nebula(Style):
    """Glowing particles and light trails that burst out on the beat,
    around a pulsing core."""
    id, label = "nebula", "Nebula particles (modern)"

    MAX = 320

    def __init__(self):
        self.parts = []
        self.rng = random.Random(7)
        self.spin = 0.0

    def _spawn(self, f, count):
        pink, warm = rgb("#FF5FA2"), (1.0, 0.92, 0.82)
        for _ in range(count):
            if len(self.parts) >= self.MAX:
                return
            a = self.rng.uniform(0, TAU)
            speed = self.rng.uniform(0.05, 0.25) * (0.6 + f.bass + f.beat)
            colour = self.rng.choice((f.accent, pink, warm, pink))
            life = self.rng.uniform(1.6, 3.6)
            self.parts.append([0.0, 0.0, math.cos(a) * speed, math.sin(a) * speed,
                               life, life, self.rng.uniform(0.6, 2.2), colour,
                               self.rng.uniform(-1.5, 1.5)])

    BG = ((0.16, 0.10, 0.12), (0.03, 0.02, 0.03))

    def step(self, f):
        """Advance the particles; returns [(x, y, nx, ny, size, colour, fade)]
        in units of min(width, height), relative to the centre."""
        dt = f.dt
        if not f.silent:
            self._spawn(f, int(2 + 60 * dt * (1 + 4 * f.mid)) + (int(18 * f.beat) if f.beat > 0.9 else 0))
        self.spin += dt * (0.2 + f.treble)
        alive, out = [], []
        for p in self.parts:
            x, y, vx, vy, life, total, size, col, curl = p
            life -= dt
            if life <= 0:
                continue
            # swirl: rotate the velocity a little each frame
            ang = curl * dt * 0.6
            vx, vy = vx * math.cos(ang) - vy * math.sin(ang), vx * math.sin(ang) + vy * math.cos(ang)
            nx, ny = x + vx * dt, y + vy * dt
            fade = min(1.0, life / total * 1.5) * min(1.0, (total - life) * 4)
            out.append((x, y, nx, ny, size, col, fade))
            p[:] = [nx, ny, vx, vy, life, total, size, col, curl]
            alive.append(p)
        self.parts = alive
        return out

    def orbs(self, f, w, h):
        """A few big soft orbs that drift with the spin: [(x, y, radius)]."""
        cx, cy, scale = w / 2, h / 2, min(w, h)
        out = []
        for k in range(5):
            a = self.spin * (0.5 + k * 0.17) + k * 1.3
            rad = scale * (0.18 + 0.08 * k)
            size = scale * (0.015 + 0.02 * f.treble)
            out.append((cx + math.cos(a) * rad, cy + math.sin(a * 1.3) * rad * 0.7, size * 3))
        return out

    @staticmethod
    def core(f, w, h):
        return min(w, h) * (0.05 + 0.10 * f.bass + 0.06 * f.beat) * 3

    def draw(self, cr, w, h, f):
        background(cr, w, h, *self.BG)
        footer(cr, w, h, f.caption)
        cx, cy, scale = w / 2, h / 2, min(w, h)
        cr.set_operator(cairo.OPERATOR_ADD)
        for x, y, nx, ny, size, col, fade in self.step(f):
            cr.set_source_rgba(*col, 0.35 * fade)
            cr.set_line_width(max(0.6, size * 0.7))
            cr.move_to(cx + x * scale, cy + y * scale)
            cr.line_to(cx + (nx + (nx - x) * 4) * scale, cy + (ny + (ny - y) * 4) * scale)
            cr.stroke()
            cr.set_source_rgba(*col, 0.8 * fade)
            cr.arc(cx + nx * scale, cy + ny * scale, size, 0, TAU)
            cr.fill()
        for ox, oy, r in self.orbs(f, w, h):
            g = cairo.RadialGradient(ox, oy, 0, ox, oy, r)
            g.add_color_stop_rgba(0, 1.0, 0.75, 0.85, 0.35)
            g.add_color_stop_rgba(1, 1.0, 0.75, 0.85, 0)
            cr.set_source(g)
            cr.arc(ox, oy, r, 0, TAU)
            cr.fill()
        # the core
        r = self.core(f, w, h)
        g = cairo.RadialGradient(cx, cy, 0, cx, cy, r)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.95)
        g.add_color_stop_rgba(0.15, 1.0, 0.80, 0.90, 0.55)
        g.add_color_stop_rgba(1, *f.accent, 0)
        cr.set_source(g)
        cr.arc(cx, cy, r, 0, TAU)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)


class _Spectrum:
    """The bands, smoothed in time (fast up, slow down) and across neighbours,
    for shapes that should flow rather than flicker."""

    def __init__(self, rise=14.0, fall=2.5, blur=1):
        self.rise, self.fall, self.blur = rise, fall, blur
        self.values = []

    def update(self, f):
        bands = list(f.bands)
        n = len(bands)
        if len(self.values) != n:
            self.values = [0.0] * n
        for _ in range(self.blur):
            bands = [(bands[max(0, i - 1)] + 2 * bands[i] + bands[min(n - 1, i + 1)]) / 4 for i in range(n)]
        out = self.values
        for i, v in enumerate(bands):
            rate = self.rise if v > out[i] else self.fall
            out[i] += (v - out[i]) * min(1.0, f.dt * rate)
        return out

    def at(self, m):
        """Value at ``m`` in 0..1 (0: lowest band), linearly interpolated."""
        v = self.values
        if not v:
            return 0.0
        x = max(0.0, min(1.0, m)) * (len(v) - 1)
        i = int(x)
        j = min(i + 1, len(v) - 1)
        return v[i] + (v[j] - v[i]) * (x - i)


class LightRibbons(Style):
    """Translucent ribbons of fine light threads, cyan in front and magenta
    behind, that twist and swell with the music above a rippling water
    reflection. The bass lifts the middle of the ribbons, the treble their
    ends."""
    id, label = "ribbons", "Light ribbons (modern)"

    LINES = 24
    HORIZON = 0.70
    MIRROR = 2.2          # the reflection is squashed by this, as seen at a low angle
    # base y (0..1 of h), wave frequency, wave speed, twist frequency, twist speed,
    # width (0..1 of h), band group, colour in front, colour behind
    RIBBONS = (
        (0.52, 1.05, 0.33, 1.35, 0.50, 0.085, 0, rgb("#2EC8FF"), rgb("#B03CFF")),
        (0.55, 1.55, -0.42, 1.90, -0.66, 0.065, 1, rgb("#3A6BFF"), rgb("#FF3FC8")),
        (0.49, 0.80, 0.24, 1.10, 0.38, 0.075, 2, rgb("#7FE8FF"), rgb("#8A4DFF")),
        (0.57, 2.10, 0.52, 2.50, 0.88, 0.045, 1, rgb("#24A0FF"), rgb("#FF5FA8")),
    )

    def __init__(self):
        self.spectrum = _Spectrum(blur=3)
        self.energy = [0.0, 0.0, 0.0]
        self.phase = [0.0] * len(self.RIBBONS)
        self.twist = [k * 1.7 for k in range(len(self.RIBBONS))]
        self.glow = 0.0

    def params(self, f, h):
        """Advance the ribbons; returns [(y0, amplitude, frequency, phase, width,
        twist frequency, twist phase, spectrum gain, front, back)] in pixels."""
        self.spectrum.update(f)
        for g, v in enumerate((f.bass, f.mid, f.treble)):
            self.energy[g] += (v - self.energy[g]) * min(1.0, f.dt * 4)
        level = sum(self.energy) / 3
        self.glow += (min(1.0, level * 1.5 + f.beat * 0.4) - self.glow) * min(1.0, f.dt * 6)
        pace = 0.35 + 1.4 * level
        out = []
        for k, (y0, freq, speed, tfreq, tspeed, width, grp, front, back) in enumerate(self.RIBBONS):
            e = self.energy[grp]
            self.phase[k] += f.dt * speed * pace
            self.twist[k] += f.dt * tspeed * (0.5 + pace)
            out.append((h * (y0 + 0.015 * math.sin(f.t * 0.21 + k)),
                        h * (0.035 + 0.11 * e + 0.035 * f.beat),
                        freq, self.phase[k],
                        h * width * (0.65 + 0.9 * e),
                        tfreq, self.twist[k],
                        h * (0.10 + 0.08 * e),
                        front, back))
        return out

    def spec_at(self, u):
        """Spectrum along the ribbons: bass in the middle, treble at the ends."""
        return self.spectrum.at(abs(2 * u - 1))

    @staticmethod
    def centre(r, u, spec):
        y0, amp, freq, phase = r[:4]
        return (y0 + amp * (0.7 * math.sin(TAU * freq * u + phase)
                            + 0.3 * math.sin(TAU * freq * 2.3 * u - 1.3 * phase)) - r[7] * spec)

    def draw(self, cr, w, h, f):
        ribbons = self.params(f, h)
        yh = h * self.HORIZON
        g = cairo.LinearGradient(0, 0, 0, h)
        g.add_color_stop_rgb(0, 0.01, 0.02, 0.07)
        g.add_color_stop_rgb(self.HORIZON, 0.02, 0.05, 0.16)
        g.add_color_stop_rgb(min(1.0, self.HORIZON + 0.01), 0.01, 0.02, 0.07)
        g.add_color_stop_rgb(1, 0.0, 0.005, 0.02)
        cr.set_source(g)
        cr.paint()
        steps = max(48, int(w / 8))
        lines = self.LINES // 3
        specs = [self.spec_at(i / steps) for i in range(steps + 1)]

        def ribbons_paths(alpha):
            for r in ribbons:
                width, tfreq, tphase, front, back = r[4], r[5], r[6], r[8], r[9]
                for j in range(lines):
                    s = -1 + 2 * j / (lines - 1)
                    pts = []
                    for i in range(steps + 1):
                        u = i / steps
                        phi = TAU * tfreq * u + tphase
                        wd = width * (0.6 + 0.4 * math.sin(TAU * 0.7 * u + 0.5 * tphase))
                        pts.append((w * u, self.centre(r, u, specs[i])
                                    + wd * (s * math.cos(phi) + 0.15 * s * s * math.sin(phi))))
                    mid = math.cos(TAU * tfreq * 0.5 + tphase)
                    cr.set_source_rgba(*_mix(back, front, 0.5 + 0.5 * mid), alpha)
                    cr.move_to(*pts[0])
                    for p in pts[1:]:
                        cr.line_to(*p)
                    cr.stroke()

        cr.set_operator(cairo.OPERATOR_ADD)
        cr.set_line_width(max(1.0, h / 700))
        ribbons_paths(0.5)
        # the reflection: mirrored about the horizon, squashed and dimmer
        cr.save()
        cr.rectangle(0, yh, w, h - yh)
        cr.clip()
        cr.translate(0, yh)
        cr.scale(1, -1 / self.MIRROR)
        cr.translate(0, -yh)
        cr.set_line_width(max(2.0, h / 250) * self.MIRROR)
        ribbons_paths(0.2)
        cr.restore()
        cr.set_operator(cairo.OPERATOR_OVER)
        footer(cr, w, h, f.caption)


class Pulsar(Style):
    """A dark core whose rim bursts into layered colour flames shaped by the
    spectrum, with light spokes, orbiting curves, star dust and purple
    waves at the edges."""
    id, label = "pulsar", "Pulsar (modern)"

    MAX = 260
    # colour, turn offset, turn speed, gain (core radii), opacity, sharpness;
    # drawn in this order, each over the ones before
    LAYERS = (
        (rgb("#18D8FF"), 0.62, -0.012, 1.50, 0.80, 2.6),
        (rgb("#FF2E4D"), 0.00, 0.010, 1.30, 0.85, 1.3),
        (rgb("#2B5BFF"), 0.80, 0.018, 0.90, 0.70, 1.8),
        (rgb("#FF2FA0"), 0.30, -0.016, 1.00, 0.75, 1.6),
        (rgb("#FF9A2E"), 0.12, 0.022, 0.50, 0.45, 1.8),
    )
    # colour, radius (core radii), centre drift, speed
    ORBITS = (
        (rgb("#FFD23F"), 2.05, 0.10, 0.11),
        (rgb("#3DFF7A"), 2.45, 0.13, -0.08),
        (rgb("#2EE6D6"), 1.55, 0.06, 0.14),
    )
    SPOKES = 144

    def __init__(self):
        self.spectrum = _Spectrum(rise=16.0, fall=3.0, blur=3)
        self.rng = random.Random(11)
        self.parts = []
        self.energy = [0.0, 0.0, 0.0]
        self.turn = [0.0] * len(self.LAYERS)
        self.orbit = [k * 2.1 for k in range(len(self.ORBITS))]
        self.spin = 0.0
        self.aura = 0.0
        self.pulse = 0.0

    def params(self, f, w, h):
        """Advance the state; returns a dict with everything in pixels."""
        self.spectrum.update(f)
        for g, v in enumerate((f.bass, f.mid, f.treble)):
            self.energy[g] += (v - self.energy[g]) * min(1.0, f.dt * 5)
        bass, mid, treble = self.energy
        level = (bass + mid + treble) / 3
        self.pulse += (f.beat - self.pulse) * min(1.0, f.dt * 10)
        scale = min(w, h)
        r0 = scale * 0.16 * (1 + 0.07 * self.pulse)
        self.spin += f.dt * (0.02 + 0.05 * level)
        self.aura += f.dt * (0.15 + 0.6 * level)
        layers = []
        for k, (col, off, speed, gain, alpha, sharp) in enumerate(self.LAYERS):
            self.turn[k] += f.dt * speed * (1 + 3 * level)
            layers.append((off + self.turn[k], r0 * gain * (0.45 + 0.5 * level + 0.25 * self.pulse),
                           alpha, sharp, col))
        orbits = []
        for k, (col, rad, drift, speed) in enumerate(self.ORBITS):
            self.orbit[k] += f.dt * speed * (1 + 2 * mid)
            a = self.orbit[k]
            orbits.append((w / 2 + math.cos(a * 1.3 + k) * drift * scale,
                           h / 2 + math.sin(a + 2 * k) * drift * scale * 0.7,
                           r0 * rad * (1 + 0.18 * bass), a * 2.0,
                           0.10 + 0.16 * mid, 0.05 + 0.12 * treble, -a * 3.1 + k, col))
        return {"r0": r0, "layers": layers, "orbits": orbits, "spin": self.spin,
                "aura": (max(w, h) * 0.47 * (1 - 0.03 * self.pulse), self.aura),
                "pulse": self.pulse, "level": level}

    def spec_at(self, turns):
        """Spectrum around the circle (mirrored: bass at the offset, treble opposite)."""
        return self.spectrum.at(1 - abs(2 * (turns % 1.0) - 1))

    def step(self, f, r0, scale):
        """Advance the star dust; returns [(x, y, size, colour, fade)] in pixels
        relative to the centre."""
        dt = f.dt
        if not f.silent:
            n = int(1 + 40 * dt * (1 + 3 * f.treble)) + (int(14 * f.beat) if f.beat > 0.9 else 0)
            palette = ((1, 1, 1), rgb("#FFD23F"), rgb("#3DFF7A"), rgb("#18D8FF"), rgb("#FF2FA0"))
            for _ in range(n):
                if len(self.parts) >= self.MAX:
                    break
                a = self.rng.uniform(0, TAU)
                speed = self.rng.uniform(0.04, 0.2) * (0.5 + f.bass + f.beat)
                life = self.rng.uniform(1.5, 4.0)
                self.parts.append([a, 1.0 + self.rng.uniform(0, 0.1), speed, life, life,
                                   self.rng.uniform(0.6, 1.8), self.rng.choice(palette)])
        out, alive = [], []
        for p in self.parts:
            a, d, speed, life, total, size, col = p
            life -= dt
            if life <= 0:
                continue
            d += speed * dt * scale / max(1.0, r0)
            a += dt * 0.05
            p[:] = [a, d, speed, life, total, size, col]
            alive.append(p)
            fade = min(1.0, life / total * 1.5) * min(1.0, (total - life) * 3)
            out.append((math.cos(a) * d * r0, math.sin(a) * d * r0, size * scale / 540, col, fade))
        self.parts = alive
        return out

    @staticmethod
    def orbit_radius(o, a):
        _cx, _cy, rad, p2, a2, a3, p3, _col = o
        return rad * (1 + a2 * math.sin(2 * a + p2) + a3 * math.sin(3 * a + p3))

    def draw(self, cr, w, h, f):
        p = self.params(f, w, h)
        cx, cy, r0 = w / 2, h / 2, p["r0"]
        cr.set_source_rgb(0.0, 0.0, 0.01)
        cr.paint()
        steps = 120
        # purple waves at the edges
        base, ph = p["aura"]
        cr.set_operator(cairo.OPERATOR_ADD)
        cr.set_line_width(max(1.0, h / 600))
        for k in range(6):
            cr.set_source_rgba(0.55, 0.2, 1.0, 0.35)
            for i in range(steps + 1):
                a = i / steps
                rr = base * (1 + 0.05 * math.sin(3 * TAU * a + ph) + 0.035 * math.sin(5 * TAU * a - 1.3 * ph)) \
                    + k * h / 140
                x, y = cx + math.sin(a * TAU) * rr, cy - math.cos(a * TAU) * rr
                (cr.move_to if i == 0 else cr.line_to)(x, y)
            cr.stroke()
        # colour flames
        for turn, gain, alpha, sharp, col in p["layers"]:
            for i in range(steps + 1):
                a = i / steps
                rr = r0 + gain * self.spec_at(a - turn) ** sharp + r0 * 0.04 * math.sin(a * TAU * 5 + f.t)
                x, y = cx + math.sin(a * TAU) * rr, cy - math.cos(a * TAU) * rr
                (cr.move_to if i == 0 else cr.line_to)(x, y)
            cr.close_path()
            g = cairo.RadialGradient(cx, cy, r0, cx, cy, r0 + gain)
            g.add_color_stop_rgba(0, 1, 1, 1, alpha)
            g.add_color_stop_rgba(0.12, *col, alpha)
            g.add_color_stop_rgba(1, *col, alpha * 0.6)
            cr.set_source(g)
            cr.fill()
        # spokes
        cr.set_line_width(max(1.0, h / 900))
        cr.set_source_rgba(0.6, 0.95, 1.0, 0.25)
        for i in range(0, self.SPOKES, 2):
            a = i / self.SPOKES + p["spin"]
            length = r0 * (1.3 + 1.8 * self.spec_at(i / self.SPOKES))
            s, c = math.sin(a * TAU), -math.cos(a * TAU)
            cr.move_to(cx + s * r0, cy + c * r0)
            cr.line_to(cx + s * length, cy + c * length)
        cr.stroke()
        cr.set_operator(cairo.OPERATOR_OVER)
        # the core
        cr.set_source_rgb(0.0, 0.01, 0.03)
        cr.arc(cx, cy, r0, 0, TAU)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_ADD)
        cr.set_source_rgba(0.5, 0.95, 1.0, 0.9)
        cr.set_line_width(max(1.5, h / 400))
        cr.arc(cx, cy, r0, 0, TAU)
        cr.stroke()
        # orbits
        cr.set_line_width(max(1.0, h / 600))
        for o in p["orbits"]:
            cr.set_source_rgba(*o[7], 0.8)
            for i in range(steps + 1):
                a = i / steps * TAU
                rr = self.orbit_radius(o, a)
                (cr.move_to if i == 0 else cr.line_to)(o[0] + math.cos(a) * rr, o[1] + math.sin(a) * rr)
            cr.close_path()
            cr.stroke()
        for x, y, size, col, fade in self.step(f, r0, min(w, h)):
            cr.set_source_rgba(*col, 0.9 * fade)
            cr.arc(cx + x, cy + y, size, 0, TAU)
            cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        footer(cr, w, h, f.caption)


class _Scene(Style):
    """Base of the slow painted scenes (supernova, sunset, aurora).

    The picture is drawn entirely by a fragment shader
    (:mod:`lindvb.ui.gpuvisuals`); the CPU only smooths the music into a few
    numbers per frame. Every movement comes from the music: ``speed``
    follows the loudness and jumps on each beat, and both clocks only run
    with it, so the scenes stand still in silence, drift with quiet music
    and surge forward on the beat. ``clock`` drives the large, slow
    movements, ``motion`` (in seconds of music) the small ones such as
    twinkling stars."""

    PACE = 0.1                    # clock units per second at a speed of 1
    EASE = 2.5                    # how quickly the energies follow (1/s)

    def __init__(self):
        self.spectrum = _Spectrum(rise=6.0, fall=1.2, blur=3)
        self.energy = [0.0, 0.0, 0.0]
        self.pulse = 0.0
        self.speed = 0.0
        self.clock = 0.0
        self.motion = 0.0
        self.presence = 0.0       # 1 while music plays, sinks to 0 when it stops

    FADE_IN, FADE_OUT = 2.0, 8.0  # seconds for ``presence`` to come and go

    def params(self, f):
        """Advance the state; returns (clock, bass, mid, treble, pulse, level)."""
        self.spectrum.update(f)
        for g, v in enumerate((f.bass, f.mid, f.treble)):
            self.energy[g] += (v - self.energy[g]) * min(1.0, f.dt * self.EASE)
        bass, mid, treble = self.energy
        level = (bass + mid + treble) / 3
        self.pulse += (f.beat - self.pulse) * min(1.0, f.dt * 4)
        target = min(1.8, 1.8 * level + 0.9 * f.beat)
        self.speed += (target - self.speed) * min(1.0, f.dt * 3.0)
        self.clock += f.dt * self.PACE * self.speed
        self.motion += f.dt * self.speed
        if not f.silent and level > 0.01:
            self.presence = min(1.0, self.presence + f.dt / self.FADE_IN)
        else:
            self.presence = max(0.0, self.presence - f.dt / self.FADE_OUT)
        return self.clock, bass, mid, treble, self.pulse, level

    # -- without OpenGL: a simple painted sketch of the scene -------------------------
    def draw(self, cr, w, h, f):
        p = self.params(f)
        self.sketch(cr, w, h, *p)
        footer(cr, w, h, f.caption)

    def sketch(self, cr, w, h, clock, bass, mid, treble, pulse, level):
        raise NotImplementedError


def _glow(cr, x, y, r, stops):
    g = cairo.RadialGradient(x, y, 0, x, y, max(1.0, r))
    for pos, col, alpha in stops:
        g.add_color_stop_rgba(pos, *col, alpha)
    cr.set_source(g)
    cr.arc(x, y, max(1.0, r), 0, TAU)
    cr.fill()


class Supernova(_Scene):
    """An exploding star: veils of glowing gas in orange, yellow, violet,
    pink, blue and turquoise are born at random places in the white-hot core
    and drift outwards, growing more transparent until they vanish while new
    ones follow, so the star keeps spreading without ever getting bigger.
    Behind it a spiral galaxy turns slowly and stars light up and go out.
    When the music stops the veils thin out, a last flash fills the screen,
    then all goes dark and a single star twinkles where the supernova was.
    Veils are born only while music plays; their strength and colours come
    from the music of their birth, the spectrum sets their brightness around
    the star (bass at the top, treble at the bottom), the beat makes the
    core flare."""
    id, label = "supernova", "Supernova (modern)"

    PACE = 0.10
    FADE_OUT = 10.0
    VEILS = 9             # veils alive at once, as in the shader
    FLOW = 1.1            # veil lives per clock unit

    def __init__(self):
        super().__init__()
        self.flow = 0.0
        self.drive = 0.0
        self.veils = [0.5] * self.VEILS
        self.hues = [0.5] * self.VEILS
        self.tone = 0.5
        self.born = [None] * self.VEILS

    def params(self, f):
        """Also decides, when a veil is born, how strong it gets and its
        colours: the music of that moment, smoothed, so a loud passage sends
        out bright, thick veils, a bass-heavy one warm orange and yellow
        veils, a bright one pink, blue and turquoise ones; they then drift
        outwards unchanged. Veils are only born while music plays."""
        p = super().params(f)
        bass, mid, treble, pulse, level = p[1], p[2], p[3], p[4], p[5]
        target = min(1.6, 0.1 + 1.3 * level + 0.6 * pulse)
        self.drive += (target - self.drive) * min(1.0, f.dt * 1.5)
        tone = 0.5 + 1.5 * (treble - bass) / max(0.05, bass + mid + treble)
        self.tone += (min(1.0, max(0.0, tone)) - self.tone) * min(1.0, f.dt * 1.5)
        self.flow = p[0] * self.FLOW
        for k in range(self.VEILS):
            gen = math.floor(self.flow + k / self.VEILS)
            if gen != self.born[k]:
                self.born[k] = gen
                self.veils[k] = self.drive
                self.hues[k] = self.tone
        return p

    def sketch(self, cr, w, h, clock, bass, mid, treble, pulse, level):
        cr.set_source_rgb(0.0, 0.0, 0.01)
        cr.paint()
        cx, cy, s = w / 2, h / 2, min(w, h)
        r0 = s * 0.33 * (1 + 0.06 * bass)
        cr.set_operator(cairo.OPERATOR_ADD)
        _glow(cr, cx, cy, r0 * 1.25, ((0, (0.2, 0.5, 1.0), 0.0), (0.75, (0.15, 0.45, 1.0), 0.35),
                                      (1, (0.1, 0.2, 0.8), 0.0)))
        _glow(cr, cx, cy, r0, ((0, (1.0, 0.95, 0.85), 0.9), (0.4, (1.0, 0.6, 0.2), 0.45),
                               (1, (0.9, 0.2, 0.3), 0.0)))
        _glow(cr, cx, cy, r0 * (0.25 + 0.1 * pulse), ((0, (1, 1, 1), 1.0), (1, (1, 0.9, 0.7), 0.0)))
        cr.set_operator(cairo.OPERATOR_OVER)


class OccasusSolis(_Scene):
    """A sunset (lat. *occasus solis*): a small sun low over the horizon in
    an orange and red sky, a fan of rays turning slowly around it and clouds
    lit gold from below drifting from left to right. The spectrum shapes the
    rays from left (bass) to right (treble) and thickens the clouds above
    it, the mids stir the clouds, the bass sets the glow of the sun. New
    clouds coming in from the left are grown from the music: the louder,
    the cloudier. Clouds and rays only move while music plays, faster when
    it is loud and with a push on every beat; when the music stops no new
    clouds form, the ones there dissolve, the rays fade away and the sun
    slowly sets."""
    id, label = "sunset", "Occasus solis (modern)"

    PACE = 0.40
    COLUMNS = 32          # cloud columns across the screen, as in the shader
    DRIFT = 0.032         # screen widths per second at a speed of 1

    def __init__(self, seed=None):
        super().__init__()
        self.rng = random.Random(seed)
        self.drift = 0.0          # screen widths the clouds have moved to the right
        self.cfrac = 0.0          # fraction of a column
        self.drive = 0.35
        self.clouds = [self._next(0.5) for _ in range(self.COLUMNS + 8)]

    def _next(self, prev):
        """How cloudy (0..1) the next stretch of sky coming in from the left is:
        the music of the moment, smoothed, with some chance in it."""
        target = min(1.0, self.drive * (0.6 + 0.8 * self.rng.random()))
        return prev + (target - prev) * 0.45

    def params(self, f):
        p = super().params(f)
        level, pulse = p[5], p[4]
        self.drive += (min(1.0, 0.15 + 1.1 * level + 0.3 * pulse) - self.drive) * min(1.0, f.dt * 1.0)
        step = f.dt * self.DRIFT * self.speed
        self.drift += step
        self.cfrac += step * self.COLUMNS
        while self.cfrac >= 1.0:
            self.cfrac -= 1.0
            self.clouds.pop()
            self.clouds.insert(0, self._next(self.clouds[0]))
        return p
    SUN = (0.5, 0.86)             # position, 0..1 of the width and the height
    SET = 0.24                    # how far the sun sinks when the music stops

    @property
    def sun(self):
        """Where the sun stands: low over the horizon while music plays; when
        it stops the sun slowly sets below the bottom edge, and rises again
        when the music comes back."""
        pr = self.presence
        return self.SUN[0], self.SUN[1] + self.SET * (1.0 - pr * pr * (3 - 2 * pr))

    def sketch(self, cr, w, h, clock, bass, mid, treble, pulse, level):
        g = cairo.LinearGradient(0, 0, 0, h)
        g.add_color_stop_rgb(0, 0.42, 0.33, 0.45)
        g.add_color_stop_rgb(0.45, 0.93, 0.55, 0.32)
        g.add_color_stop_rgb(0.8, 1.0, 0.45, 0.12)
        g.add_color_stop_rgb(1, 0.85, 0.2, 0.08)
        cr.set_source(g)
        cr.paint()
        sx, sy = w * self.SUN[0], h * self.SUN[1]
        cr.set_operator(cairo.OPERATOR_ADD)
        _glow(cr, sx, sy, h * (0.55 + 0.1 * bass), ((0, (1.0, 0.8, 0.4), 0.55), (1, (1.0, 0.4, 0.1), 0.0)))
        cr.set_operator(cairo.OPERATOR_OVER)
        _glow(cr, sx, sy, h * 0.03, ((0, (1, 1, 0.95), 1.0), (0.8, (1, 0.95, 0.75), 1.0),
                                     (1, (1, 0.85, 0.5), 0.0)))


class AuroraBorealis(_Scene):
    """Northern lights over snowy mountains: a band of light whose lower edge
    is a free curve that slowly changes its shape and wanders across the sky.
    It glows most at one end, yellow-green, then turns turquoise and finally
    pink or violet, with rays reaching upwards. Behind the mountains there is
    a yellow-green glow; the mountains move slowly from right to left, the
    new hills and peaks coming in grown from the music (higher and steeper
    with the bass, a peak on a strong beat); snow drifts and glitters on the
    slopes, and stars light up at random places and fade away. Each part of
    the band brightens with its part of the spectrum, the bass lengthens its
    rays and the beat makes it flare. Nothing moves in silence; when the
    music stops the aurora fades away, the stars dim and the mountains sink
    until only the deep blue night sky with a few stars is left."""
    id, label = "aurora", "Aurora borealis (modern)"

    PACE = 0.40
    RIDGE = 48            # height points of the mountains, as in the shader
    VISIBLE = 40          # of them across the width of the screen
    SCROLL = 0.85         # points per second at a speed of 1

    def __init__(self, seed=None):
        super().__init__()
        self.rng = random.Random(seed)
        self.scroll = 0.0         # fraction of a point scrolled to the left
        self.world = 0.0          # points scrolled in total
        self.trend = 0.0
        h = 0.10
        self.ridge = []
        for _ in range(self.RIDGE):
            h = self._next(h, 0.0, 0.0)
            self.ridge.append(h)

    def _next(self, h, bass, beat):
        """The height (0..1 of the screen above the bottom edge) of the next
        point coming in from the right: a calm random walk for the hills,
        steeper and higher with the bass, a peak on a strong beat."""
        rng = self.rng
        self.trend += rng.gauss(0.0, 0.012 + 0.03 * bass) - 0.25 * self.trend
        h += self.trend + 0.12 * (0.08 + 0.08 * bass - h)
        if beat > 0.8 and rng.random() < 0.5:
            h += 0.04 + 0.06 * beat * rng.random()
        return max(0.02, min(0.26, h))

    def params(self, f):
        p = super().params(f)
        bass, pulse = p[1], p[4]
        step = f.dt * self.SCROLL * self.speed
        self.scroll += step
        self.world += step
        while self.scroll >= 1.0:
            self.scroll -= 1.0
            self.ridge.pop(0)
            self.ridge.append(self._next(self.ridge[-1], bass, max(pulse, f.beat)))
        return p

    def sketch(self, cr, w, h, clock, bass, mid, treble, pulse, level):
        g = cairo.LinearGradient(0, 0, 0, h)
        g.add_color_stop_rgb(0, 0.02, 0.12, 0.32)
        g.add_color_stop_rgb(1, 0.05, 0.25, 0.55)
        cr.set_source(g)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_ADD)
        for k in range(3):
            x = w * (0.3 + 0.25 * k + 0.05 * math.sin(clock * 3 + k))
            g = cairo.LinearGradient(0, h * 0.1, 0, h * 0.85)
            g.add_color_stop_rgba(0, 0.6, 0.2, 0.8, 0.0)
            g.add_color_stop_rgba(0.5, 0.6, 0.2, 0.8, 0.25 + 0.2 * level)
            g.add_color_stop_rgba(1, 0.5, 1.0, 0.4, 0.55 + 0.3 * level)
            cr.set_source(g)
            cr.rectangle(x - w * 0.06, h * 0.1, w * 0.12, h * 0.75)
            cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgb(0.75, 0.82, 0.9)
        cr.move_to(w * 0.5, h)
        cr.line_to(w * 0.75, h * 0.82)
        cr.line_to(w, h * 0.77)
        cr.line_to(w, h)
        cr.close_path()
        cr.fill()


class GoomHD(Style):
    """GOOM 2k4 at the screen's own resolution.

    The logic is GOOM's own, ported to Python in :mod:`lindvb.goom` and run at
    GOOM's 30 frames per second: sound and goom detection, the eight states,
    the zoom filter modes and transitions, scope lines, 3D tentacles,
    fireworks and the IFS fractal. The GPU renderer
    (``lindvb.ui.gpuvisuals.GoomRenderer``) computes GOOM's displacement field
    and draws everything at full resolution. Without OpenGL a small cairo
    version is drawn instead.
    """
    id, label = "goomhd", "Goom HD (GPU)"

    def __init__(self, seed=None):
        self.seed = seed
        self.core = None
        self._acc = 0.0
        self._buf = None
        self._field = None

    def steps(self, f, w, h, max_steps=2):
        """Advance GOOM by the frames due since the last call (at most ``max_steps``)."""
        from .goom import VH, STEP_RATE, GoomCore, pcm_to_goom
        width = max(16, int(round(VH * w / max(1, h))))
        if self.core is None:
            self.core = GoomCore(width, self.seed)
        else:
            self.core.set_width(width)
        period = 1.0 / STEP_RATE
        self._acc = min(self._acc + f.dt, max_steps * period)
        left, right = f.pcm if f.pcm else ([], [])
        d0, d1 = pcm_to_goom(left), pcm_to_goom(right or left)
        out = []
        while self._acc >= period - 1e-4:
            self._acc -= period
            out.append(self.core.update(d0, d1))
        return out

    # -- without OpenGL: GOOM's frames drawn small with cairo ---------------------------
    def draw(self, cr, w, h, f):
        frames = self.steps(f, w, h)
        if self.core is None:
            return
        gw, gh = self.core.w, self.core.h
        buf = self._buf
        if buf is None or buf.get_width() != gw:
            buf = self._buf = cairo.ImageSurface(cairo.FORMAT_RGB24, gw, gh)
        for fr in frames:
            if fr.new_field is not None:
                self._field = fr.new_field
            fd = self._field
            new = cairo.ImageSurface(cairo.FORMAT_RGB24, gw, gh)
            c = cairo.Context(new)
            if fd is not None:
                mx, my = fd.middle[0] * gw, fd.middle[1] * gh
                zoom = 1.0 / max(0.5, 1.0 - (1.0 + fd.speed) / 50.0)
                c.translate(mx, my)
                c.scale(zoom, zoom)
                c.translate(-mx, -my)
            c.set_source_surface(buf, 0, 0)
            c.paint_with_alpha(0.97)
            c.identity_matrix()
            c.set_operator(cairo.OPERATOR_ADD)
            c.set_line_width(1.0)
            for pts, col in fr.lines:
                c.set_source_rgb(*(v / 255 for v in col))
                c.move_to(*pts[0])
                for p in pts[1:]:
                    c.line_to(*p)
                c.stroke()
            for x, y, col in fr.points:
                c.set_source_rgb(*(v / 255 for v in col))
                c.rectangle(x, y, 2, 2)
                c.fill()
            for x0, y0, x1, y1, col in fr.stars:
                c.set_source_rgb(*(v / 255 for v in col))
                c.move_to(x0, y0)
                c.line_to(x1, y1)
                c.stroke()
            buf = self._buf = new
        cr.save()
        cr.scale(w / gw, h / gh)
        cr.set_source_surface(buf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BILINEAR)
        cr.paint()
        cr.restore()
        footer(cr, w, h, f.caption)


STYLES = [GlowWaves, Nebula, LightRibbons, Pulsar, Supernova, OccasusSolis, AuroraBorealis, GoomHD,
          SpectrumBars, VUMeters, Oscilloscope, Blank]
DEFAULT_STYLE = GlowWaves.id


def style_by_id(style_id):
    return next((s for s in STYLES if s.id == style_id), None)
