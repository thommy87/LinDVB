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


STYLES = [GlowWaves, Nebula, GoomHD, SpectrumBars, VUMeters, Oscilloscope, Blank]
DEFAULT_STYLE = GlowWaves.id


def style_by_id(style_id):
    return next((s for s in STYLES if s.id == style_id), None)
