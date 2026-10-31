"""Tests for the audio analysis and the drawn radio visualisations (no GTK)."""

import math
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb.audioviz import (AudioAnalysis, Smoother, analyse_samples, log_bands,  # noqa: E402
                             parse_spectrum)


def _sine(freq=1000, amp=0.5, rate=48000, frames=1024, channels=2):
    vals = []
    for i in range(frames):
        v = amp * math.sin(2 * math.pi * freq * i / rate)
        vals += [v] * channels
    return vals


def test_parse_spectrum_message():
    text = ("spectrum, endtime=(guint64)50000000, timestamp=(guint64)0, "
            "magnitude=(float){ -60, -59.5, -12.25 };")
    assert parse_spectrum(text) == [-60.0, -59.5, -12.25]
    assert parse_spectrum("spectrum, timestamp=(guint64)0;") == []


def test_log_bands_take_the_loudest_bin():
    mags = [-80.0] * 256
    mags[10] = -6.0                      # 10 * 93.75 Hz ~ 940 Hz
    bands = log_bands(mags, 48000, n=48)
    assert len(bands) == 48
    assert max(bands) == -6.0
    loud = bands.index(-6.0)
    assert 0 < loud < 47


@pytest.mark.parametrize("fmt,code,scale", [("F32LE", "f", 1), ("S16LE", "h", 32767)])
def test_analyse_samples_levels(fmt, code, scale):
    vals = _sine(amp=0.5)
    if code == "h":
        vals = [int(v * scale) for v in vals]
    data = struct.pack(f"<{len(vals)}{code}", *vals)
    rms, peak, wave = analyse_samples(data, fmt, 2)
    assert rms[0] == pytest.approx(0.5 / math.sqrt(2), rel=0.05)
    assert peak[0] == pytest.approx(0.5, rel=0.05)
    assert wave and max(wave) == pytest.approx(0.5, rel=0.05)


def test_analyse_unknown_format():
    assert analyse_samples(b"\0" * 64, "U8", 2) is None


def test_smoother_rises_and_falls():
    an = AudioAnalysis()
    sm = Smoother()
    data = struct.pack("<2048f", *_sine())
    an.feed_buffer(data, "F32LE", 2, 48000)
    an.feed_spectrum([-10.0] * 256)
    f = sm.update(an, now=an.stamp)
    assert max(f.bands) > 0.8 and not f.silent
    for k in range(1, 30):                       # audio stops: everything falls back
        f = sm.update(an, now=an.stamp + k * 0.1)
    assert max(f.bands) < 0.1 and f.silent
    assert f.vu[0] < -40


def test_styles_draw():
    cairo = pytest.importorskip("cairo")
    from lindvb.visuals import STYLES
    an = AudioAnalysis()
    an.feed_buffer(struct.pack("<2048f", *_sine()), "F32LE", 2, 48000)
    an.feed_spectrum([-20.0 - k * 0.2 for k in range(256)])
    sm = Smoother()
    for style_cls in STYLES:
        style = style_cls()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 180)
        cr = cairo.Context(surf)
        for k in range(5):
            frame = sm.update(an, now=an.stamp + k / 30)
            frame.caption = "Radio Test"
            style.draw(cr, 320, 180, frame)
        assert style.id and style.label
