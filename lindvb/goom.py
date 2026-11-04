# SPDX-License-Identifier: GPL-2.0-or-later
"""The logic of GOOM 2k4, ported to Python for the "Goom HD" visualisation.

This follows the C sources of GOOM as shipped in gst-plugins-good
(``gst/goom``: goom_core.c, sound_tester.c, filters.c, lines.c,
tentacle3d.c, surf3d.c, flying_stars_fx.c, ifs.c), Copyright (C) 2000-2004
Jean-Christophe Hoelt and the GOOM authors, licensed under the GNU LGPL 2.1 or
later. Everything that decides *what* is drawn — sound and "goom" detection,
the eight states, the zoom filter modes and their switching, the scope lines,
3D tentacles, fireworks and the IFS fractal — runs here exactly like the
original, frame by frame at the original 30 frames per second, in GOOM's own
coordinate system (200 lines high, as GOOM was developed on 320x200).

What is drawn *per pixel* — the zoom filter's displacement field, its
transitions and fading, the IFS points and the tentacle meshes — is done by
shaders in :mod:`lindvb.ui.gpuvisuals` at the screen's real resolution, so the
picture has GOOM's patterns without GOOM's 320x200 pixels.
"""

import math
import random
from dataclasses import dataclass, field

VH = 200                       # GOOM's virtual screen height
STEP_RATE = 30.0               # GOOM frames per second

STOP_SPEED = 128
TIME_BTW_CHG = 300

NORMAL_MODE, WAVE_MODE, CRYSTAL_BALL_MODE, SCRUNCH_MODE, AMULETTE_MODE = range(5)
WATER_MODE, HYPERCOS1_MODE, HYPERCOS2_MODE, YONLY_MODE, SPEEDWAY_MODE = range(5, 10)

GML_CIRCLE, GML_HLINE, GML_VLINE = range(3)
GML_BLEUBLANC, GML_RED, GML_ORANGE_V, GML_ORANGE_J, GML_VERT, GML_BLEU, GML_BLACK = range(7)

# (r, g, b) of the scope line colours (lines.c getcouleur)
LINE_COLOURS = {
    GML_RED: (230, 120, 18), GML_ORANGE_J: (252, 120, 18), GML_ORANGE_V: (236, 160, 40),
    GML_BLEUBLANC: (220, 140, 40), GML_VERT: (80, 200, 18), GML_BLEU: (80, 30, 250),
    GML_BLACK: (16, 16, 16),
}

WHITE, YELLOW, ORANGE, VIOLET, BLACK = (255, 255, 255), (255, 255, 51), (255, 204, 5), \
    (85, 5, 255), (0, 0, 0)

# drawIFS, drawPoints, drawTentacle, drawScope, farScope, rangemin, rangemax
STATES = [(1, 0, 0, 1, 4, 0, 100), (1, 0, 0, 0, 1, 101, 140), (1, 0, 0, 1, 2, 141, 200),
          (0, 1, 0, 1, 2, 201, 260), (0, 1, 0, 1, 0, 261, 330), (0, 1, 1, 1, 4, 331, 400),
          (0, 0, 1, 0, 5, 401, 450), (0, 0, 1, 1, 1, 451, 510)]
STATES_RANGE_MAX = 510

# flying stars: colour by age (0xAARRGGBB in the original's little-endian pixels)
STAR_COLOURS = [((c >> 16) & 0xff, (c >> 8) & 0xff, c & 0xff) for c in (
    0x1416181a, 0x1419181a, 0x141f181a, 0x1426181a, 0x142a181a, 0x142f181a, 0x1436181a,
    0x142f1819, 0x14261615, 0x13201411, 0x111a100a, 0x0c180508, 0x08100304, 0x00050101, 0)]
NCOL = 15
SIN256 = [math.sin(2 * math.pi * i / 256) for i in range(256)]
COS256 = [math.cos(2 * math.pi * i / 256) for i in range(256)]


def _lighten(value, power):
    t = value * math.log10(power) / 2.0 if power > 0 else 0.0
    return min(255, max(0, int(t))) if t > 0 else 0


def lighten_colour(rgb, power):
    return tuple(_lighten(c, power) for c in rgb)


# --------------------------------------------------------------------------- sound
class Sound:
    """sound_tester.c: volume, acceleration, speed and "goom" detection."""

    BIG_GOOM_DURATION = 100
    ACCEL_MULT, SPEED_MULT = 0.95, 0.99
    BIGGOOM_SPEED_LIMIT, BIGGOOM_FACTOR = 10, 10

    def __init__(self):
        self.speedvar = self.accelvar = 0.0
        self.totalgoom = 0
        self.prov_max = 0.0
        self.goom_limit = 1.0
        self.big_goom_limit = 1.0
        self.all_times_max = 1
        self.time_since_last_goom = 1
        self.time_since_last_big_goom = 1
        self.goom_power = 0.0
        self.volume = 0.0
        self.cycle = 0

    def evaluate(self, data0):
        incvar = 0
        for i in range(0, 512, 2):
            if incvar < data0[i]:
                incvar = data0[i]
        if incvar > self.all_times_max:
            self.all_times_max = incvar
        self.volume = incvar / self.all_times_max
        difaccel = self.accelvar
        self.accelvar = self.volume
        if self.speedvar > 1.0:
            self.speedvar = 1.0
        if self.speedvar < 0.1:
            self.accelvar *= 1.0 - self.speedvar
        elif self.speedvar < 0.3:
            self.accelvar *= 0.9 - (self.speedvar - 0.1) / 2.0
        else:
            self.accelvar *= 0.8 - (self.speedvar - 0.3) / 4.0
        self.accelvar *= self.ACCEL_MULT
        if self.accelvar < 0:
            self.accelvar = 0.0
        difaccel = abs(self.accelvar - difaccel)
        prevspeed = self.speedvar
        self.speedvar = (self.speedvar + difaccel * 0.5) / 2
        self.speedvar *= self.SPEED_MULT
        self.speedvar = (self.speedvar + 3.0 * prevspeed) / 4.0
        self.speedvar = min(1.0, max(0.0, self.speedvar))
        self.time_since_last_goom += 1
        self.time_since_last_big_goom += 1
        self.cycle += 1
        if (self.speedvar > self.BIGGOOM_SPEED_LIMIT / 100.0
                and self.accelvar > self.big_goom_limit
                and self.time_since_last_big_goom > self.BIG_GOOM_DURATION):
            self.time_since_last_big_goom = 0
        if self.accelvar > self.goom_limit:
            self.totalgoom += 1
            self.time_since_last_goom = 0
            self.goom_power = self.accelvar - self.goom_limit
        if self.accelvar > self.prov_max:
            self.prov_max = self.accelvar
        if self.goom_limit > 1:
            self.goom_limit = 1.0
        if self.cycle % 64 == 0:
            if self.speedvar < 0.01:
                self.goom_limit *= 0.91
            if self.totalgoom > 4:
                self.goom_limit += 0.02
            if self.totalgoom > 7:
                self.goom_limit = self.goom_limit * 1.03 + 0.03
            if self.totalgoom > 16:
                self.goom_limit = self.goom_limit * 1.05 + 0.04
            if self.totalgoom == 0:
                self.goom_limit = self.prov_max - 0.02
            if self.totalgoom == 1 and self.goom_limit > 0.02:
                self.goom_limit -= 0.01
            self.totalgoom = 0
            self.big_goom_limit = self.goom_limit * (1.0 + self.BIGGOOM_FACTOR / 500.0)
            self.prov_max = 0.0


# --------------------------------------------------------------------------- zoom filter
@dataclass
class ZoomParams:
    """ZoomFilterData as set by goom_update."""
    vitesse: int = 127
    middle_x: int = 1
    middle_y: int = 1
    reverse: int = 0
    mode: int = NORMAL_MODE
    h_plane: int = 0
    v_plane: int = 0
    wave_effect: int = 0
    hypercos_effect: int = 0
    noisify: int = 0
    pertedec: int = 8


@dataclass
class Field:
    """The parameters one displacement field is computed from (filters.c zoomVector),
    in GOOM's coordinates: the renderer turns them into a texture."""
    speed: float = 0.0             # general_speed
    mode: int = AMULETTE_MODE
    middle: tuple = (0.5, 0.5)     # 0..1 of the screen
    h_plane: int = 0
    v_plane: int = 0
    hypercos: int = 0
    noisify: int = 0
    seed: float = 0.0              # for the noise


class ZoomFilter:
    """The bookkeeping of zoomFilterFastRGB: a new field is built over 16 frames
    (like GOOM's interlaced generation), then the old one is frozen at the current
    blend into the source and the new one becomes the destination."""

    def __init__(self, width):
        self.width, self.height = width, VH
        self.data = Field(middle=(0.5, 0.5), noisify=2)
        self.initial = self.data
        self.interlace = -2
        self.ratio = 0                 # 0..0xffff
        self.pending = None

    def apply(self, zf, switch_incr, switch_mult, rng):
        """One frame. Returns (new field or None, the blend frozen into the source
        before the new field becomes the destination, this frame's blend), the
        blends going from 0 (source) to 1 (destination)."""
        new, freeze = None, 0.0
        if self.interlace != -2:
            zf = None
        if zf is not None:
            speed = (zf.vitesse - 128) / 128.0
            if zf.reverse:
                speed = -speed
            self.pending = Field(speed, zf.mode, (zf.middle_x / self.width, zf.middle_y / self.height),
                                 zf.h_plane, zf.v_plane, zf.hypercos_effect, zf.noisify, rng.random())
            self.interlace = 0
        if self.interlace == -1:
            freeze = self.ratio / 0xffff
            self.ratio = 0
            new = self.pending
            self.interlace = -2
        if self.interlace >= 0:
            self.interlace += self.height // 16
            if self.interlace >= self.height - 1:
                self.interlace = -1
        if switch_incr:
            self.ratio = min(0xffff, self.ratio + switch_incr)
        if switch_mult != 1.0:
            self.ratio = int(0xffff * (1.0 - switch_mult) + self.ratio * switch_mult)
        return new, freeze, self.ratio / 0xffff


# --------------------------------------------------------------------------- scope lines
class GoomLine:
    """lines.c: a scope line (circle, horizontal or vertical) that morphs between shapes."""

    def __init__(self, core, w, h, id_src, param_s, col_s, id_dest, param_d, col_d):
        self.core = core
        self.w, self.h = w, h
        self.points = self._gen(id_src, param_s)
        self.points2 = self._gen(id_dest, param_d)
        self.id_dest, self.param = id_dest, param_d
        self.amplitude = self.amplitude_f = 1.0
        self.colour = LINE_COLOURS[col_s]
        self.colour2 = LINE_COLOURS[col_d]
        self.power = 0.0
        self.powinc = 0.01
        self.switch_to(id_dest, param_d, 1.0, col_d)

    def _gen(self, kind, param):
        w, h = self.w, self.h
        if kind == GML_HLINE:
            return [[i * w / 512.0, param, math.pi / 2] for i in range(512)]
        if kind == GML_VLINE:
            return [[param, i * h / 512.0, 0.0] for i in range(512)]
        out = []
        for i in range(512):
            a = 2.0 * math.pi * i / 512.0
            out.append([w / 2.0 + param * math.cos(a), h / 2.0 + param * math.sin(a), a])
        return out

    def set_res(self, w, h):
        self.w, self.h = w, h
        self.points2 = self._gen(self.id_dest, self.param)

    def switch_to(self, kind, param, amplitude, col):
        self.points2 = self._gen(kind, param)
        self.id_dest, self.param = kind, param
        self.amplitude_f = amplitude
        self.colour2 = LINE_COLOURS[col]

    def draw(self, data):
        """Points of the line for this frame (GOOM px) and its colour; then move it."""
        colour = lighten_colour(self.colour, self.power)
        pts = []
        amp = self.amplitude / 1000.0
        for p, d in zip(self.points, data):
            pts.append((p[0] + math.cos(p[2]) * amp * d, p[1] + math.sin(p[2]) * amp * d))
        self._move()
        return pts, colour

    def _move(self):
        for p, q in zip(self.points, self.points2):
            p[0] = (q[0] + 39.0 * p[0]) / 40.0
            p[1] = (q[1] + 39.0 * p[1]) / 40.0
            p[2] = (q[2] + 39.0 * p[2]) / 40.0
        self.colour = tuple((a * 63 + b) >> 6 for a, b in zip(self.colour, self.colour2))
        self.power += self.powinc
        rng = self.core.rng
        if self.power < 1.1:
            self.power = 1.1
            self.powinc = (rng.randrange(20) + 10) / 300.0
        if self.power > 17.5:
            self.power = 17.5
            self.powinc = -(rng.randrange(20) + 10) / 300.0
        self.amplitude = (99.0 * self.amplitude + self.amplitude_f) / 100.0


# --------------------------------------------------------------------------- tentacles
class Grid:
    """surf3d.c grid3d: one tentacle mesh."""

    def __init__(self, sizex, defx, sizez, defz, centre):
        self.sizex, self.defx, self.sizez, self.defz = sizex, defx, sizez, defz
        self.centre = centre
        self.y = [0.0] * (defx * defz)
        self.cam = centre

    def update(self, angle, vals, dist):
        cx, cy, cz = self.centre
        self.cam = (cx, cy + math.sin(angle / 4.3) * 2.0, cz + dist)
        y, defx = self.y, self.defx
        for i in range(defx):
            y[i] = y[i] * 0.2 + vals[i] * 0.8
        for i in range(defx, len(y)):
            y[i] = y[i] * 0.255 + y[i - defx] * 0.777


def _evolute(src, dest):
    if src < dest:
        return src + 1
    if src > dest:
        return src - 1
    return src


class Tentacles:
    """tentacle3d.c: six 3D meshes moved by the sound."""

    D = 256.0
    NBGRID, DEFX, DEFZ = 6, 15, 45
    COLOURS = [(0x18, 0x4c, 0x2f), (0x48, 0x2c, 0x6f), (0x58, 0x3c, 0x0f), (0x87, 0x55, 0x74)]

    def __init__(self, rng):
        self.rng = rng
        self.cycle = 0.0
        self.col = (0x28, 0x2c, 0x5f)
        self.dstcol = 0
        self.lig, self.ligs = 1.15, 0.1
        self.distt, self.distt2, self.rot = 10.0, 0.0, 0.0
        self.happens = self.rotation = self.lock = 0
        self.grids = []
        cy = -17.0
        for _ in range(self.NBGRID):
            z = 45 + rng.randrange(30)
            x = 85 + rng.randrange(5)
            self.grids.append(Grid(x, self.DEFX, z, self.DEFZ + rng.randrange(10), (0.0, cy, float(z))))
            cy += 8

    def _pretty_move(self, cycle):
        r = self.rng
        if self.happens:
            self.happens -= 1
        elif self.lock == 0:
            self.happens = 0 if r.randrange(200) else 100 + r.randrange(60)
            self.lock = self.happens * 3 // 2
        else:
            self.lock -= 1
        tmp = 8.0 if self.happens else 0.0
        self.distt2 = (tmp + 15.0 * self.distt2) / 16.0
        tmp = 30 + self.D - 90.0 * (1.0 + math.sin(cycle * 19 / 20))
        if self.happens:
            tmp *= 0.6
        self.distt = (tmp + 3.0 * self.distt) / 4.0
        if not self.happens:
            tmp = math.pi * math.sin(cycle) / 32 + 3 * math.pi / 2
        else:
            self.rotation = self.rotation if r.randrange(500) else r.randrange(2)
            cycle *= 2.0 * math.pi if self.rotation else -1.0 * math.pi
            tmp = cycle - (math.pi * 2.0) * math.floor(cycle / (math.pi * 2.0))
        two_pi = 2.0 * math.pi
        if abs(tmp - self.rot) > abs(tmp - (self.rot + two_pi)):
            self.rot = (tmp + 15.0 * (self.rot + two_pi)) / 16.0
            if self.rot > two_pi:
                self.rot -= two_pi
        elif abs(tmp - self.rot) > abs(tmp - (self.rot - two_pi)):
            self.rot = (tmp + 15.0 * (self.rot - two_pi)) / 16.0
            if self.rot < 0.0:
                self.rot += two_pi
        else:
            self.rot = (tmp + 15.0 * self.rot) / 16.0
        return self.distt, self.distt2, self.rot

    def update(self, data0, rapport, drawit):
        """Returns None or (rotation, dist, colour, colour_low) for drawing the grids."""
        r = self.rng
        if not drawit and self.ligs > 0.0:
            self.ligs = -self.ligs
        self.lig += self.ligs
        if self.lig > 1.01:
            if self.lig > 10.0 or self.lig < 1.1:
                self.ligs = -self.ligs
            if self.lig < 6.3 and r.randrange(30) == 0:
                self.dstcol = r.randrange(len(self.COLOURS))
            self.col = tuple(_evolute(a, b) for a, b in zip(self.col, self.COLOURS[self.dstcol]))
            colour = lighten_colour(self.col, self.lig * 2.0 + 2.0)
            colour_low = lighten_colour(self.col, self.lig / 3.0 + 0.67)
            rapport = (1.0 + 2.0 * (rapport - 1.0)) * 1.2
            rapport = min(rapport, 1.12)
            dist, dist2, rot = self._pretty_move(self.cycle)
            for g in self.grids:
                vals = []
                for _ in range(self.DEFX):
                    v = data0[r.randrange(511)]
                    vals.append(float(-((-v) >> 10) if v < 0 else v >> 10) * rapport)
                g.update(rot, vals, dist2)
            self.cycle += 0.01
            return rot, int(dist), colour, colour_low
        self.lig = 1.05
        if self.ligs < 0.0:
            self.ligs = -self.ligs
        self._pretty_move(self.cycle)
        self.cycle += 0.1
        if self.cycle > 1000:
            self.cycle = 0.0
        return None


# --------------------------------------------------------------------------- flying stars
class Stars:
    """flying_stars_fx.c: fireworks on every goom."""

    MAX = 512
    MIN_AGE, MAX_AGE = 1.0 - 99 / 100.0, 1.0 - 80 / 100.0

    def __init__(self, rng):
        self.rng = rng
        self.stars = []                 # [x, y, vx, vy, ax, ay, age, vage]
        self.fx_mode = 0

    def _bomb(self, mx, my, radius, vage, gravity):
        if len(self.stars) >= self.MAX:
            return
        r = self.rng
        ro = radius * r.randrange(100) / 100.0
        ro *= r.randrange(100) / 100.0 + 1.0
        theta = r.randrange(256)
        self.stars.append([float(mx), float(my), ro * COS256[theta], -0.2 + ro * SIN256[theta],
                           0.0, gravity, 0.0, max(vage, self.MIN_AGE)])

    def _event(self, sound, w, h):
        r = self.rng
        maxn = int((1.0 + sound.goom_power) * r.randrange(150)) + 100
        radius = (1.0 + sound.goom_power) * (r.randrange(150) + 50) / 300
        gravity = 0.02
        if self.fx_mode == 0:                      # fireworks
            while True:
                mx, my = r.randrange(w), r.randrange(h)
                dx, dy = mx - w // 2, my - h // 2
                if dx * dx + dy * dy >= (h // 2) * (h // 2):
                    break
            vage = self.MAX_AGE * (1.0 - sound.goom_power)
        elif self.fx_mode == 1:                    # rain
            mx = w if r.randrange(w) > w // 2 else 0
            my = -(h // 3) - r.randrange(max(1, w // 3))
            radius *= 1.5
            vage = 0.002
        elif self.fx_mode == 2:                    # fountain
            my, mx = h + 2, w // 2
            vage, radius, gravity = 0.001, radius + 1.0, 0.04
        else:
            return
        radius *= h / 200.0
        maxn = int(maxn * h / 200.0)
        if sound.time_since_last_big_goom < 1:
            radius *= 1.5
            maxn *= 2
        for _ in range(maxn):
            self._bomb(mx, my, radius, vage, gravity)

    def update(self, sound, w, h):
        """Returns line segments [(x0, y0, x1, y1, (r, g, b))] (each star twice, like GOOM)."""
        if sound.time_since_last_goom < 1:
            self._event(sound, w, h)
            if self.rng.randrange(20) == 1:
                self.fx_mode = self.rng.randrange(9)
        out, alive = [], []
        for s in self.stars:
            s[0] += s[2]
            s[1] += s[3]
            s[2] += s[4]
            s[3] += s[5]
            s[6] += s[7]
            if s[6] >= NCOL:
                continue
            col = STAR_COLOURS[min(int(s[6]), len(STAR_COLOURS) - 1)]
            # GOOM rounds to its pixels; kept as floats, the streaks stay smooth
            x, y = s[0], s[1]
            out.append((x, y, s[0] - s[2] * 6, s[1] - s[3] * 6, col))
            out.append((x, y, s[0] - s[2] * 2, s[1] - s[3] * 2, col))
        for s in self.stars:
            if (s[0] > w + 64 or (s[3] >= 0 and s[1] - 16 * s[3] > h)
                    or s[0] < -64 or s[6] >= NCOL):
                continue
            alive.append(s)
        self.stars = alive
        return out


# --------------------------------------------------------------------------- IFS
class IFS:
    """ifs.c: a morphing fractal of 2 to 5 similitudes (points computed on the GPU)."""

    MAX_SIMI = 6

    def __init__(self, rng):
        self.rng = rng
        self.couleur = [0xc0, 0xc0, 0xc0, 0xc0]     # r, g, b, a as bytes
        self.v = {"b": 2, "g": 4, "r": 3, "a": 2}
        self.col = {"b": 2, "g": 4, "r": 3, "a": 2}
        self.mode = 2                                # MOD_MERVER
        self.just_changed = 0
        self.cycle = 0
        self.speed = 6
        self.count = 0
        self._init()

    def _gauss(self, c, a, s):
        y = self.rng.random() * 127.0
        y = a * (1.0 - math.exp(-y * y * s)) / (1.0 - math.exp(-s))
        return c + y if self.rng.randrange(2) else c - y

    def _half_gauss(self, c, a, s):
        y = self.rng.random() * 127.0
        return c + a * (1.0 - math.exp(-y * y * s)) / (1.0 - math.exp(-s))

    def _random_simis(self, start, n):
        for k in range(start, start + n):
            self.comp[k] = [self._gauss(0.0, 0.8, 4.0), self._gauss(0.0, 0.8, 4.0),
                            self._gauss(self.r_mean, self.dr_mean, 3.0),
                            self._half_gauss(0.0, self.dr2_mean, 2.0),
                            self._gauss(0.0, 360.0, 4.0) * math.pi / 180.0,
                            self._gauss(0.0, 360.0, 4.0) * math.pi / 180.0]

    def _init(self):
        n = self.rng.randrange(4) + 2
        self.nb = n
        self.depth, self.r_mean, self.dr_mean, self.dr2_mean = {
            3: (6, 0.6, 0.4, 0.3), 4: (4, 0.5, 0.4, 0.3), 5: (3, 0.5, 0.4, 0.3)}.get(
            n, (10, 0.7, 0.3, 0.4))
        self.comp = [[0.0] * 6 for _ in range(5 * self.MAX_SIMI)]
        self._random_simis(0, 5 * self.MAX_SIMI)

    def simis(self):
        """Interpolate the similitudes (cubic Bezier between key sets) and step."""
        nb, comp = self.nb, self.comp
        u = self.count * self.speed / 1000.0
        uu, v = u * u, 1.0 - u
        vv = v * v
        u0, u1, u2, u3 = vv * v, 3.0 * vv * u, 3.0 * v * uu, u * uu
        for i in range(nb):
            s1, s2, s3, s4 = comp[nb + i], comp[2 * nb + i], comp[3 * nb + i], comp[4 * nb + i]
            comp[i] = [u0 * a + u1 * b + u2 * c + u3 * d for a, b, c, d in zip(s1, s2, s3, s4)]
        out = [list(comp[i]) for i in range(nb)]
        if self.count >= 1000 // self.speed:
            for i in range(nb):
                comp[2 * nb + i] = [2.0 * d - c for c, d in zip(comp[3 * nb + i], comp[4 * nb + i])]
            self._random_simis(3 * nb, nb)
            self._random_simis(4 * nb, nb)
            self.count = 0
        else:
            self.count += 1
        return out

    def update(self):
        """One frame: (similitudes, depth, colour (r, g, b))."""
        self.cycle = (self.cycle + 1) % 80
        cycle10 = self.cycle // 10 if self.cycle < 40 else 7 - self.cycle // 10
        colour = tuple(c >> cycle10 for c in self.couleur[:3])
        sims = self.simis()
        self._evolve_colour()
        return sims, self.depth, colour

    def _evolve_colour(self):
        r = self.rng
        col, v = self.col, self.v
        self.just_changed -= 1
        col["a"], col["b"], col["g"], col["r"] = self.couleur[3], self.couleur[2], \
            self.couleur[1], self.couleur[0]

        def walk(ch, hi, lo, up, down):
            col[ch] += v[ch]
            if col[ch] > hi:
                col[ch] = hi
                v[ch] = -(r.randrange(down[0])) - down[1]
            if col[ch] < lo:
                col[ch] = lo
                v[ch] = r.randrange(up[0]) + up[1]

        if self.mode == 0:                           # MOD_MER
            walk("b", 255, 32, (4, 1), (4, 1))
            col["g"] += v["g"]
            if col["g"] > 200:
                col["g"], v["g"] = 200, -(r.randrange(3)) - 2
            if col["g"] > col["b"]:
                col["g"], v["g"] = col["b"], v["b"]
            if col["g"] < 32:
                col["g"], v["g"] = 32, r.randrange(3) + 2
            walk("r", 64, 0, (4, 1), (4, 1))
            walk("a", 0, 0, (4, 1), (4, 1))
            if (col["g"] > 32 and col["r"] < col["g"] + 40 and col["g"] < col["r"] + 20
                    and col["b"] < 64 and r.randrange(20) == 0 and self.just_changed < 0):
                self.mode = 1 if r.randrange(3) else 2
                self.just_changed = 250
        elif self.mode == 2:                         # MOD_MERVER
            walk("b", 128, 16, (4, 1), (4, 1))
            col["g"] += v["g"]
            if col["g"] > 200:
                col["g"], v["g"] = 200, -(r.randrange(3)) - 2
            if col["g"] > col["a"]:
                col["g"], v["g"] = col["a"], v["a"]
            if col["g"] < 32:
                col["g"], v["g"] = 32, r.randrange(3) + 2
            walk("r", 128, 0, (4, 1), (4, 1))
            walk("a", 255, 0, (4, 1), (4, 1))
            if (col["g"] > 32 and col["r"] < col["g"] + 40 and col["g"] < col["r"] + 20
                    and col["b"] < 64 and r.randrange(20) == 0 and self.just_changed < 0):
                self.mode = 1 if r.randrange(3) else 0
                self.just_changed = 250
        else:                                        # MOD_FEU
            walk("b", 64, 0, (4, 1), (4, 1))
            col["g"] += v["g"]
            if col["g"] > 200:
                col["g"], v["g"] = 200, -(r.randrange(3)) - 2
            if col["g"] > col["r"] + 20:
                col["g"] = col["r"] + 20
                v["g"] = -(r.randrange(3)) - 2
                v["r"] = r.randrange(4) + 1
                v["b"] = r.randrange(4) + 1
            if col["g"] < 0:
                col["g"], v["g"] = 0, r.randrange(3) + 2
            col["r"] += v["r"]
            if col["r"] > 255:
                col["r"], v["r"] = 255, -(r.randrange(4)) - 1
            if col["r"] > col["g"] + 40:
                col["r"], v["r"] = col["g"] + 40, -(r.randrange(4)) - 1
            if col["r"] < 0:
                col["r"], v["r"] = 0, r.randrange(4) + 1
            walk("a", 0, 0, (4, 1), (4, 1))
            if (col["r"] < 64 and col["g"] > 32 and col["g"] < col["b"] and col["b"] > 32
                    and r.randrange(20) == 0 and self.just_changed < 0):
                self.mode = 0 if r.randrange(2) else 2
                self.just_changed = 250
        self.couleur = [max(0, min(255, col["r"])), max(0, min(255, col["g"])),
                        max(0, min(255, col["b"])), max(0, min(255, col["a"]))]


# --------------------------------------------------------------------------- one frame
@dataclass
class GoomFrame:
    """What to draw for one GOOM frame, in GOOM pixels (width x 200)."""
    width: int = 320
    height: int = VH
    new_field: Field = None          # build this field now...
    freeze_blend: float = 0.0        # ...after freezing source = mix(source, dest, this)
    blend: float = 0.0               # mix(source, dest) used by this frame's zoom
    ifs: tuple = None                # (similitudes, depth, colour, increment)
    points: list = field(default_factory=list)       # [(x, y, colour)] -> picture before zoom
    lines: list = field(default_factory=list)        # [(points, colour)] -> after zoom
    tentacles: tuple = None          # (grids, rotation, dist, colour, colour_low)
    stars: list = field(default_factory=list)        # display only
    goom: bool = False


class GoomCore:
    """goom_core.c goom_update(), one call per GOOM frame."""

    def __init__(self, width=320, seed=None):
        self.rng = random.Random(seed)
        self.w, self.h = int(width), VH
        self.sound = Sound()
        self.cycle = 0
        self.state = STATES[6]
        self.lockvar = self.goomvar = self.loopvar = 0
        self.stop_lines = 0
        self.ifs_incr, self.decay_ifs, self.recay_ifs = 1, 0, 0
        self.cycles_since_last_change = 0
        self.draw_lines_duration = 80
        self.line_mode = self.draw_lines_duration
        self.switch_mult_amount = 29.0 / 30.0
        self.switch_incr_amount = 0x7f
        self.switch_mult = 1.0
        self.switch_incr = self.switch_incr_amount
        self.state_selection_rnd = 0
        self.state_selection_blocker = 0
        self.previous_zoom_speed = 128
        self.zfd = ZoomParams()
        self.zoom = ZoomFilter(self.w)
        self.tentacles = Tentacles(self.rng)
        self.stars = Stars(self.rng)
        self.ifs = IFS(self.rng)
        self.line1 = GoomLine(self, self.w, self.h, GML_HLINE, self.h, GML_BLACK,
                              GML_CIRCLE, 0.4 * self.h, GML_VERT)
        self.line2 = GoomLine(self, self.w, self.h, GML_HLINE, 0, GML_BLACK,
                              GML_CIRCLE, 0.2 * self.h, GML_RED)

    def set_width(self, width):
        width = max(16, int(width))
        if width != self.w:
            self.w = width
            self.zoom.width = width
            self.line1.set_res(width, self.h)
            self.line2.set_res(width, self.h)

    def irand(self, n):
        return self.rng.randrange(n)

    def _choose_line(self, far):
        mode = self.irand(3)
        amplitude, p1, p2 = 1.0, 0.0, 0.0
        h, w = self.h, self.w
        if mode == GML_CIRCLE:
            if far:
                p1 = p2 = 0.47
                amplitude = 0.8
            elif self.irand(3) == 0:
                p1 = p2 = 0.0
                amplitude = 3.0
            elif self.irand(2):
                p1, p2 = 0.40 * h, 0.22 * h
            else:
                p1 = p2 = h * 0.35
        elif mode == GML_HLINE:
            if self.irand(4) or far:
                p1, p2 = h // 7, 6.0 * h / 7.0
            else:
                p1 = p2 = h / 2.0
                amplitude = 2.0
        else:
            if self.irand(3) or far:
                p1, p2 = w / 7.0, 6.0 * w / 7.0
            else:
                p1 = p2 = w / 2.0
                amplitude = 1.5
        return p1, p2, self.irand(6), mode, amplitude

    def _switch_lines(self):
        p1, p2, c1, mode, amp = self._choose_line(self.stop_lines)
        c2 = 5 - c1
        if self.stop_lines:
            self.stop_lines -= 1
            if self.irand(2):
                c1 = c2 = GML_BLACK
        self.line1.switch_to(mode, p1, amp, c1)
        self.line2.switch_to(mode, p2, amp, c2)

    def update(self, data0, data1):
        """One GOOM frame from 512 samples per channel (ints, -32768..32767)."""
        out = GoomFrame(self.w, self.h)
        snd, w, h, zf = self.sound, self.w, self.h, self.zfd
        point_w, point_h = (w * 2) // 5, (h * 2) // 5
        snd.evaluate(data0)
        out.goom = snd.time_since_last_goom == 0
        largfactor = min(1.5, snd.speedvar / 150.0 + snd.volume / 1.5)

        self.decay_ifs -= 1
        if self.decay_ifs > 0:
            self.ifs_incr += 2
        if self.decay_ifs == 0:
            self.ifs_incr = 0
        if self.recay_ifs:
            self.ifs_incr -= 2
            self.recay_ifs -= 1
            if self.recay_ifs == 0 and self.ifs_incr <= 0:
                self.ifs_incr = 1
        if self.ifs_incr > 0:
            sims, depth, colour = self.ifs.update()
            out.ifs = (sims, depth, colour, self.ifs_incr)

        if self.state[1]:                                       # drawPoints
            i = 1
            while i * 15 <= snd.speedvar * 80.0 + 15:
                self.loopvar = int(self.loopvar + snd.speedvar * 50 + 1)
                lv = self.loopvar
                for colour, t1, t2, t3, t4, cyc in (
                        (YELLOW, (point_w - 6.0) * largfactor + 5.0, (point_h - 6.0) * largfactor + 5.0,
                         i * 152.0, 128.0, lv + i * 2032),
                        (ORANGE, ((point_w // 2) * largfactor) / i + 10.0 * i,
                         ((point_h // 2) * largfactor) / i + 10.0 * i, 96.0, i * 80.0, lv // i),
                        (VIOLET, ((point_h // 3 + 5.0) * largfactor) / i + 10.0 * i,
                         ((point_h // 3 + 5.0) * largfactor) / i + 10.0 * i, i + 122.0, 134.0, lv // i),
                        (BLACK, (point_h // 3) * largfactor + 20.0, (point_h // 3) * largfactor + 20.0,
                         58.0, i * 66.0, lv // i),
                        (WHITE, (point_h * largfactor + 10.0 * i) / i, (point_h * largfactor + 10.0 * i) / i,
                         66.0, 74.0, lv + i * 500)):
                    x = w // 2 + int(t1 * math.cos(cyc / t3))
                    y = h // 2 + int(t2 * math.sin(cyc / t4))
                    if 1 < x < w - 2 and 1 < y < h - 2:
                        out.points.append((x, y, colour))
                i += 1

        pzfd = None
        self.lockvar = max(0, self.lockvar - 1)
        if snd.time_since_last_goom == 0 or self.cycles_since_last_change > TIME_BTW_CHG:
            if self.irand(16) == 0:
                self._random_mode()

        if self.lockvar == 0:
            if snd.time_since_last_goom == 0:
                self.goomvar += 1
                if not self.state_selection_blocker and self.irand(3):
                    self.state_selection_rnd = self.irand(STATES_RANGE_MAX)
                    self.state_selection_blocker = 3
                elif self.state_selection_blocker:
                    self.state_selection_blocker -= 1
                for st in STATES:
                    if st[5] <= self.state_selection_rnd <= st[6]:
                        self.state = st
                if self.state[0] and self.ifs_incr <= 0:
                    self.recay_ifs = 5
                    self.ifs_incr = 11
                if not self.state[0] and self.ifs_incr > 0 and self.decay_ifs <= 0:
                    self.decay_ifs = 100
                if not self.state[3]:
                    self.stop_lines = 0
                    self.line_mode = self.draw_lines_duration
                pzfd = self._goom_event(pzfd)
                if self.lockvar > 150:
                    self.switch_incr = self.switch_incr_amount
                    self.switch_mult = 1.0
            if self.irand(700) == 0:                            # mega-slow mode
                pzfd = zf
                zf.vitesse = STOP_SPEED - 1
                zf.pertedec = 8
                self.goomvar = 1
                self.lockvar += 50
                self.switch_incr = self.switch_incr_amount
                self.switch_mult = 1.0

        if snd.speedvar < 0.01 and zf.vitesse < STOP_SPEED - 4 and self.cycle % 16 == 0:
            pzfd = zf
            zf.vitesse += 3
            zf.pertedec = 8
            self.goomvar = 0
        if self.cycle % 73 == 0 and zf.vitesse < STOP_SPEED - 5:
            pzfd = zf
            zf.vitesse += 1
        if self.cycle % 101 == 0 and zf.pertedec == 7:
            pzfd = zf
            zf.pertedec = 8

        if pzfd is not None:
            self.cycles_since_last_change = 0
            self.switch_incr = self.switch_incr_amount
            dif = abs(zf.vitesse - self.previous_zoom_speed)
            if dif > 2:
                self.switch_incr *= (dif + 2) // 2
            self.previous_zoom_speed = zf.vitesse
            self.switch_mult = 1.0
            if snd.time_since_last_goom == 0 and snd.totalgoom < 2:
                self.switch_incr = 0
                self.switch_mult = self.switch_mult_amount
        elif self.cycles_since_last_change > TIME_BTW_CHG:
            pzfd = zf
            self.cycles_since_last_change = 0
        else:
            self.cycles_since_last_change += 1

        zp = None
        if pzfd is not None:
            zp = ZoomParams(**vars(zf))
        out.new_field, out.freeze_blend, out.blend = self.zoom.apply(
            zp, self.switch_incr, self.switch_mult, self.rng)

        tent = self.tentacles.update(data0, snd.accelvar, self.state[2])
        if tent is not None:
            rot, dist, colour, colour_low = tent
            out.tentacles = (self.tentacles.grids, rot, dist, colour, colour_low)
        out.stars = self.stars.update(snd, w, h)

        if (self.stop_lines & 0xf000) or not self.state[3]:
            p1, p2, _c, mode, amp = self._choose_line(True)
            self.line1.switch_to(mode, p1, amp, GML_BLACK)
            self.line2.switch_to(mode, p2, amp, GML_BLACK)
            self.stop_lines &= 0x0fff

        if self.line_mode != self.draw_lines_duration:
            self.line_mode = max(0, self.line_mode - 1)
        elif self.cycle % 80 == 0 and self.irand(5) == 0 and self.line_mode:
            self.line_mode -= 1

        if self.cycle % 120 == 0 and self.irand(4) == 0 and self.state[3]:
            if self.line_mode == 0:
                self.line_mode = self.draw_lines_duration
            elif self.line_mode == self.draw_lines_duration:
                self.line_mode -= 1
                self._switch_lines()

        if self.line_mode != 0 or snd.time_since_last_goom < 5:
            self.line2.power = self.line1.power
            out.lines.append(self.line1.draw(data0))
            out.lines.append(self.line2.draw(data1))
            if (self.cycle % 121 == 9 and self.irand(3) == 1
                    and (self.line_mode == 0 or self.line_mode == self.draw_lines_duration)):
                self._switch_lines()

        self.cycle += 1
        return out

    def _random_mode(self):
        zf = self.zfd
        k = self.irand(34)
        if k in (0, 10):
            zf.hypercos_effect = self.irand(2)
        if k in (0, 10, 13, 20, 21):
            zf.mode = WAVE_MODE
            zf.reverse = 0
            zf.wave_effect = int(self.irand(3) == 0)
            if self.irand(2):
                zf.vitesse = (zf.vitesse + 127) >> 1
        elif k in (1, 11):
            zf.mode, zf.wave_effect, zf.hypercos_effect = CRYSTAL_BALL_MODE, 0, 0
        elif k in (2, 12):
            zf.mode, zf.wave_effect, zf.hypercos_effect = AMULETTE_MODE, 0, 0
        elif k == 3:
            zf.mode, zf.wave_effect, zf.hypercos_effect = WATER_MODE, 0, 0
        elif k in (4, 14):
            zf.mode, zf.wave_effect, zf.hypercos_effect = SCRUNCH_MODE, 0, 0
        elif k in (5, 15, 22):
            zf.mode, zf.wave_effect = HYPERCOS1_MODE, 0
            zf.hypercos_effect = int(self.irand(3) == 0)
        elif k in (6, 16):
            zf.mode, zf.wave_effect, zf.hypercos_effect = HYPERCOS2_MODE, 0, 0
        elif k in (7, 17):
            zf.mode = CRYSTAL_BALL_MODE
            zf.wave_effect = int(self.irand(4) == 0)
            zf.hypercos_effect = self.irand(2)
        elif k in (8, 18, 19):
            zf.mode, zf.wave_effect, zf.hypercos_effect = SCRUNCH_MODE, 1, 1
        elif k in (29, 30):
            zf.mode = YONLY_MODE
        elif k in (31, 32, 33):
            zf.mode = SPEEDWAY_MODE
        else:
            zf.mode, zf.wave_effect, zf.hypercos_effect = NORMAL_MODE, 0, 0

    def _goom_event(self, pzfd):
        """The zoom changes on a goom (speed, direction, middle, plane effects, noise)."""
        zf, snd, w, h = self.zfd, self.sound, self.w, self.h
        self.lockvar = 50
        newvit = int(STOP_SPEED + 1 - (3.5 * math.log10(snd.speedvar * 60 + 1)))
        if zf.reverse and not self.cycle % 13 and self.rng.randrange(5) == 0:
            zf.reverse = 0
            zf.vitesse = STOP_SPEED - 2
            self.lockvar = 75
        if self.irand(10) == 0:
            zf.reverse = 1
            self.lockvar = 100
        if self.irand(10) == 0:
            zf.vitesse = STOP_SPEED - 1
        if self.irand(12) == 0:
            zf.vitesse = STOP_SPEED + 1
        k = self.irand(25)
        if k in (0, 3, 6):
            zf.middle_y, zf.middle_x = h - 1, w // 2
        elif k in (1, 4):
            zf.middle_x = w - 1
        elif k in (2, 5):
            zf.middle_x = 1
        else:
            zf.middle_y, zf.middle_x = h // 2, w // 2
        if zf.mode in (WATER_MODE, YONLY_MODE, AMULETTE_MODE):
            zf.middle_x, zf.middle_y = w // 2, h // 2
        k = self.irand(15)
        ir = self.irand
        if k == 0:
            zf.v_plane = ir(3) - ir(3)
            zf.h_plane = ir(3) - ir(3)
        elif k == 3:
            zf.v_plane = 0
            zf.h_plane = ir(8) - ir(8)
        elif k in (4, 5, 6, 7):
            zf.v_plane = ir(5) - ir(5)
            zf.h_plane = -zf.v_plane
        elif k == 8:
            zf.h_plane = 5 + ir(8)
            zf.v_plane = -zf.h_plane
        elif k == 9:
            zf.v_plane = 5 + ir(8)
            zf.h_plane = -zf.h_plane
        elif k == 13:
            zf.h_plane = 0
            zf.v_plane = ir(10) - ir(10)
        elif k == 14:
            zf.h_plane = ir(10) - ir(10)
            zf.v_plane = ir(10) - ir(10)
        elif k < 10:
            zf.v_plane = zf.h_plane = 0
        if self.irand(5) != 0:
            zf.noisify = 0
        else:
            zf.noisify = self.irand(2) + 1
            self.lockvar *= 2
        if zf.mode == AMULETTE_MODE:
            zf.v_plane = zf.h_plane = zf.noisify = 0
        if zf.middle_x == 1 or zf.middle_x == w - 1:
            zf.v_plane = 0
            if self.irand(2):
                zf.h_plane = 0
        if newvit < zf.vitesse:                     # speed up
            pzfd = zf
            if ((newvit < STOP_SPEED - 7 and zf.vitesse < STOP_SPEED - 6 and self.cycle % 3 == 0)
                    or self.irand(40) == 0):
                zf.vitesse = STOP_SPEED - self.irand(2) + self.irand(2)
                zf.reverse = int(not zf.reverse)
            else:
                zf.vitesse = int((newvit + zf.vitesse * 7) / 8)
            self.lockvar += 50
        return pzfd


def pcm_to_goom(samples, n=512):
    """Floats -1..1 -> ``n`` ints like GOOM's 16-bit input (padded with silence)."""
    out = [max(-32768, min(32767, int(v * 32767))) for v in samples[:n]]
    if len(out) < n:
        out.extend([0] * (n - len(out)))
    return out
