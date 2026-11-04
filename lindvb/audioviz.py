# SPDX-License-Identifier: GPL-2.0-or-later
"""Audio analysis for the radio visualisations (no GTK, no GStreamer).

The player taps the decoded audio (see :meth:`lindvb.player.Player`) and feeds
two things into an :class:`AudioAnalysis`:

* raw sample buffers from a pad probe (streaming thread), from which the
  levels (RMS and peak per channel) and a short waveform are taken, and
* the magnitudes posted by GStreamer's ``spectrum`` element (main thread).

:class:`Smoother` turns the latest analysis into a :class:`Frame` for drawing,
with meter-like ballistics: bars jump up and fall back slowly, VU needles
integrate over about 300 ms, peaks hold for a moment.
"""

import math
import re
import threading
import time
from array import array
from dataclasses import dataclass, field

# GStreamer raw audio formats we can read, and their full-scale value.
SAMPLE_FORMATS = {
    "F32LE": ("f", 1.0),
    "F64LE": ("d", 1.0),
    "S16LE": ("h", 32768.0),
    "S32LE": ("i", 2147483648.0),
}

MIN_DB = -90.0
SPECTRUM_THRESHOLD = -80          # dB, the floor of the spectrum element
DISPLAY_BANDS = 48
LOW_HZ, HIGH_HZ = 35.0, 16000.0
MAX_FRAMES_ANALYSED = 1024        # per buffer and channel, enough for the meters
WAVE_POINTS = 512

_MAGNITUDE_RE = re.compile(r"magnitude=\(float\)[{<]([^}>]*)[}>]")


def parse_spectrum(structure_text):
    """Magnitudes (dB) from the text of a ``spectrum`` element message.

    PyGObject cannot read the GstValueList the element posts without the
    optional gst-python overrides, so the serialised structure is parsed.
    """
    m = _MAGNITUDE_RE.search(structure_text)
    if not m:
        return []
    out = []
    for part in m.group(1).split(","):
        part = part.strip()
        if part:
            try:
                out.append(float(part))
            except ValueError:
                return []
    return out


def to_db(value):
    return 20.0 * math.log10(value) if value > 1e-9 else MIN_DB


def log_bands(magnitudes, rate, n=DISPLAY_BANDS, low=LOW_HZ, high=HIGH_HZ):
    """Regroup linear FFT bins (dB) into ``n`` logarithmically spaced bands.

    Each display band takes the loudest bin inside its frequency range, so
    narrow bass bands are not empty when the FFT resolution is coarse.
    """
    count = len(magnitudes)
    if not count or rate <= 0:
        return [float(SPECTRUM_THRESHOLD)] * n
    nyquist = rate / 2.0
    high = min(high, nyquist)
    width = nyquist / count
    out = []
    for i in range(n):
        f0 = low * (high / low) ** (i / n)
        f1 = low * (high / low) ** ((i + 1) / n)
        b0 = min(count - 1, int(f0 / width))
        b1 = min(count - 1, max(b0, int(f1 / width)))
        out.append(max(magnitudes[b0:b1 + 1]))
    return out


def analyse_samples(data, fmt, channels):
    """RMS and peak (linear, 0..1) of the first two channels, and a waveform.

    ``data`` is interleaved raw audio in one of :data:`SAMPLE_FORMATS`.
    Returns ``None`` for formats we do not read.
    """
    spec = SAMPLE_FORMATS.get(fmt)
    if spec is None or channels < 1:
        return None
    code, scale = spec
    samples = array(code)
    usable = len(data) - len(data) % (samples.itemsize * channels)
    samples.frombytes(bytes(data[:usable]))
    frames = len(samples) // channels
    if not frames:
        return None
    step = max(1, frames // MAX_FRAMES_ANALYSED)
    rms, peak = [], []
    for ch in range(min(2, channels)):
        sub = samples[ch::channels * step]
        if not sub:
            sub = array(code, [0])
        rms.append(math.sqrt(sum(x * x for x in sub) / len(sub)) / scale)
        peak.append(max(abs(max(sub)), abs(min(sub))) / scale)
    if len(rms) == 1:
        rms.append(rms[0])
        peak.append(peak[0])
    wstep = max(1, frames // WAVE_POINTS)
    wave = [x / scale for x in samples[0::channels * wstep]]
    return rms, peak, wave


def pcm_block(data, fmt, channels, n=512):
    """The first ``n`` frames of the first two channels as floats (-1..1),
    consecutive like a visualiser's input (GOOM takes 512 per channel)."""
    spec = SAMPLE_FORMATS.get(fmt)
    if spec is None or channels < 1:
        return [], []
    code, scale = spec
    samples = array(code)
    size = samples.itemsize * channels
    take = min(len(data) - len(data) % size, n * size)
    samples.frombytes(bytes(data[:take]))
    left = [x / scale for x in samples[0::channels]]
    right = [x / scale for x in samples[1::channels]] if channels > 1 else left
    return left, right


class AudioAnalysis:
    """The latest analysis of the playing audio, shared between threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.rate = 48000
            self.rms = [0.0, 0.0]
            self.peak = [0.0, 0.0]
            self.wave = []
            self.pcm = ([], [])         # consecutive samples per channel
            self.spectrum = []          # dB per display band
            self.stamp = 0.0            # monotonic time of the last update

    def feed_buffer(self, data, fmt, channels, rate):
        """Called from the GStreamer streaming thread for each audio buffer."""
        res = analyse_samples(data, fmt, channels)
        if res is None:
            return
        rms, peak, wave = res
        pcm = pcm_block(data, fmt, channels)
        with self._lock:
            self.rate = rate or self.rate
            self.rms, self.peak, self.wave = rms, peak, wave
            self.pcm = pcm
            self.stamp = time.monotonic()

    def feed_spectrum(self, magnitudes):
        bands = log_bands(magnitudes, self.rate)
        with self._lock:
            self.spectrum = bands
            self.stamp = time.monotonic()

    def snapshot(self):
        with self._lock:
            return (list(self.spectrum), list(self.rms), list(self.peak),
                    list(self.wave), self.stamp)

    def pcm_snapshot(self):
        with self._lock:
            return self.pcm


@dataclass
class Frame:
    """Everything a visualisation style needs to draw one picture."""

    t: float = 0.0                   # seconds since the visualisation started
    dt: float = 0.0                  # seconds since the previous frame
    bands: list = field(default_factory=lambda: [0.0] * DISPLAY_BANDS)  # 0..1
    peaks: list = field(default_factory=lambda: [0.0] * DISPLAY_BANDS)  # held band peaks
    vu: list = field(default_factory=lambda: [-40.0, -40.0])   # VU-ballistic RMS, dBFS
    level: list = field(default_factory=lambda: [0.0, 0.0])    # fast RMS, 0..1
    peak_db: list = field(default_factory=lambda: [MIN_DB, MIN_DB])
    wave: list = field(default_factory=list)                   # -1..1
    pcm: tuple = ([], [])            # 512 consecutive samples per channel, -1..1
    bass: float = 0.0                # 0..1 energy of the low/mid/high bands
    mid: float = 0.0
    treble: float = 0.0
    beat: float = 0.0                # 0..1, jumps on bass hits and decays
    accent: tuple = (0.067, 0.612, 1.0)
    caption: str = ""                # e.g. the station name
    silent: bool = True


def _norm_db(db, floor=-70.0, top=-10.0):
    return min(1.0, max(0.0, (db - floor) / (top - floor)))


class Smoother:
    """Turns raw analyses into frames with meter ballistics."""

    STALE = 0.3          # seconds without new audio before the meters fall
    BAR_FALL = 1.6       # units (0..1) per second
    PEAK_HOLD = 0.7      # seconds
    PEAK_FALL = 0.9
    VU_TIME = 0.3        # VU integration time constant

    def __init__(self, bands=DISPLAY_BANDS):
        self.n = bands
        self.frame = Frame()
        self._t0 = None
        self._last = None
        self._hold = [0.0] * bands
        self._bass_avg = 0.0

    def update(self, analysis, now=None):
        now = time.monotonic() if now is None else now
        if self._t0 is None:
            self._t0 = self._last = now
        dt = min(0.25, max(0.0, now - self._last))
        self._last = now
        f = self.frame
        f.t, f.dt = now - self._t0, dt
        spectrum, rms, peak, wave, stamp = analysis.snapshot()
        live = stamp and now - stamp < self.STALE
        target = [_norm_db(db) for db in spectrum] if live and spectrum else []
        if len(target) != self.n:
            target = [0.0] * self.n if not target else \
                [target[min(len(target) - 1, i * len(target) // self.n)] for i in range(self.n)]
        if live and not spectrum:
            # no spectrum element: let the level drive a flat bar display
            lv = _norm_db(to_db(max(rms)), -60, 0)
            target = [lv * (0.6 + 0.4 * math.sin(i * 0.7)) for i in range(self.n)]
        fall = self.BAR_FALL * dt
        f.bands = [max(t, b - fall) for t, b in zip(target, f.bands)]
        for i, b in enumerate(f.bands):
            if b >= f.peaks[i]:
                f.peaks[i], self._hold[i] = b, self.PEAK_HOLD
            elif self._hold[i] > 0:
                self._hold[i] -= dt
            else:
                f.peaks[i] = max(b, f.peaks[i] - self.PEAK_FALL * dt)
        if not live:
            rms, peak, wave = [0.0, 0.0], [0.0, 0.0], []
        a = 1.0 - math.exp(-dt / self.VU_TIME) if dt else 0.0
        f.vu = [v + (to_db(r) - v) * a for v, r in zip(f.vu, rms)]
        fast = 1.0 - math.exp(-dt / 0.06) if dt else 0.0
        f.level = [lv + (_norm_db(to_db(r), -60, 0) - lv) * fast for lv, r in zip(f.level, rms)]
        f.peak_db = [max(to_db(p), pd - 20 * dt) for p, pd in zip(peak, f.peak_db)]
        f.wave = wave
        f.pcm = analysis.pcm_snapshot() if live and hasattr(analysis, "pcm_snapshot") else ([], [])
        n = self.n
        third = max(1, n // 6)
        f.bass = sum(f.bands[:third]) / third
        f.mid = sum(f.bands[third:n // 2]) / max(1, n // 2 - third)
        f.treble = sum(f.bands[n // 2:]) / max(1, n - n // 2)
        # simple beat detection: bass energy well above its running average
        self._bass_avg += (f.bass - self._bass_avg) * min(1.0, dt * 2.5)
        if f.bass > self._bass_avg * 1.35 + 0.05:
            f.beat = 1.0
        else:
            f.beat = max(0.0, f.beat - dt * 3.0)
        f.silent = not live or max(rms) < 1e-4
        return f
