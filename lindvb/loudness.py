# SPDX-License-Identifier: GPL-2.0-or-later
"""Loudness after EBU R 128 / ITU-R BS.1770, for the channel analysis.

The player taps the decoded sound before the equaliser (lindvb.player): a
K-weighting filter (GStreamer's audioiirfilter with the coefficients from
:func:`k_weighting`) and a level element give the mean square of each channel
every 100 ms. :class:`LoudnessMeter` turns those blocks into momentary (400 ms),
short-term (3 s) and integrated loudness (gated, as BS.1770-4 says) in LUFS,
the loudness range (LRA) and the jumps between programme and advertising.
The sample peak comes from a second level element in front of the filter.
"""

import math
from collections import deque

ABS_GATE = -70.0
REL_GATE = -10.0
LRA_REL_GATE = -20.0
JUMP_LU = 6.0


def _poly_mul(a, b):
    out = [0.0] * (len(a) + len(b) - 1)
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            out[i + j] += x * y
    return out


def k_weighting(rate):
    """(b, a) coefficients of the K-weighting filter (both stages) at *rate* Hz,
    as in libebur128, with a[0] = 1."""
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh = 10 ** (gain / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    b1 = [(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0]
    a1 = [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    d = 1 + k / q + k * k
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0, 2 * (k * k - 1) / d, (1 - k / q + k * k) / d]
    return _poly_mul(b1, b2), _poly_mul(a1, a2)


def channel_weights(n):
    """BS.1770 weights for GStreamer's default channel order (FL FR FC LFE RL RR ...)."""
    if n <= 2:
        return [1.0] * n
    w = [1.0, 1.0, 1.0, 0.0] + [1.41] * max(0, n - 4)
    return w[:n]


def lufs(mean_square):
    return -0.691 + 10 * math.log10(mean_square) if mean_square > 0 else -math.inf


class LoudnessMeter:
    """Collects 100 ms blocks (weighted sum of the channels' mean squares)."""

    def __init__(self, block_s=0.1):
        self.block_s = block_s
        self.reset()

    def reset(self):
        self.blocks = deque(maxlen=30)          # 3 s of 100 ms blocks
        self.momentary_all = []                 # every 100 ms: 400 ms mean square (gating)
        self.short_all = deque(maxlen=6000)     # short-term loudness every 100 ms (LRA)
        self.peak_db = -math.inf
        self.peak_recent = deque(maxlen=30)
        self.jumps = []                         # (block index, LU)
        self.count = 0
        self._ref = deque(maxlen=300)           # short-term loudness of the last 30 s

    def feed(self, mean_squares, weights=None):
        weights = weights or channel_weights(len(mean_squares))
        z = sum(w * m for w, m in zip(weights, mean_squares))
        self.blocks.append(z)
        self.count += 1
        if len(self.blocks) >= 4:
            m400 = sum(list(self.blocks)[-4:]) / 4
            self.momentary_all.append(m400)
            if len(self.momentary_all) > 36000:          # one hour
                del self.momentary_all[:6000]
        if len(self.blocks) >= 30:
            st = lufs(sum(self.blocks) / len(self.blocks))
            self.short_all.append(st)
            if self.count % 10 == 0 and st > ABS_GATE:
                if len(self._ref) >= 100:
                    ref = sorted(self._ref)[len(self._ref) // 2]
                    if abs(st - ref) >= JUMP_LU and \
                            (not self.jumps or self.count - self.jumps[-1][0] > 100):
                        self.jumps.append((self.count, st - ref))
                self._ref.append(st)

    def feed_peak(self, peak_db):
        self.peak_recent.append(peak_db)
        if peak_db > self.peak_db:
            self.peak_db = peak_db

    @property
    def momentary(self):
        if len(self.blocks) < 4:
            return None
        return lufs(sum(list(self.blocks)[-4:]) / 4)

    @property
    def short_term(self):
        if len(self.blocks) < 30:
            return None
        return lufs(sum(self.blocks) / len(self.blocks))

    @property
    def integrated(self):
        gated = [m for m in self.momentary_all if lufs(m) > ABS_GATE]
        if not gated:
            return None
        rel = lufs(sum(gated) / len(gated)) + REL_GATE
        gated = [m for m in gated if lufs(m) > rel]
        return lufs(sum(gated) / len(gated)) if gated else None

    @property
    def loudness_range(self):
        vals = [v for v in self.short_all if v > ABS_GATE]
        if len(vals) < 20:
            return None
        ms = [10 ** ((v + 0.691) / 10) for v in vals]
        rel = lufs(sum(ms) / len(ms)) + LRA_REL_GATE
        vals = sorted(v for v in vals if v > rel)
        if len(vals) < 2:
            return None
        lo = vals[int(0.10 * (len(vals) - 1))]
        hi = vals[int(0.95 * (len(vals) - 1))]
        return hi - lo

    @property
    def recent_peak(self):
        return max(self.peak_recent) if self.peak_recent else None
