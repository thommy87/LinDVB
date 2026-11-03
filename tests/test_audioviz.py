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


def test_every_builtin_style_has_a_gpu_renderer():
    pytest.importorskip("cairo")
    from lindvb.ui.gpuvisuals import renderer_class
    from lindvb.visuals import STYLES
    for style_cls in STYLES:
        assert renderer_class(style_cls()) is not None, style_cls.id

    class PluginStyle(STYLES[0]):          # subclasses (plugins) are drawn with cairo
        pass
    assert renderer_class(PluginStyle()) is None


def _goom_pcm(k, loud):
    amp = 24000 if loud else 3000
    return [int(amp * math.sin(i * 0.07 + k) * math.sin(i * 0.011)) for i in range(512)]


def test_goom_core_follows_the_original():
    from lindvb.goom import STEP_RATE, VH, GoomCore
    core = GoomCore(width=356, seed=3)
    assert (core.w, core.h) == (356, VH) and STEP_RATE == 30
    modes, gooms, fields = set(), 0, 0
    for k in range(int(STEP_RATE) * 60):               # a minute of GOOM frames
        loud = k % 20 < 2                           # a beat every 2/3 s
        fr = core.update(_goom_pcm(k, loud), _goom_pcm(k + 1, loud))
        assert 0.0 <= fr.blend <= 1.0 and 0.0 <= fr.freeze_blend <= 1.0
        gooms += fr.goom
        if fr.new_field is not None:
            fields += 1
            modes.add(fr.new_field.mode)
            mx, my = fr.new_field.middle
            assert 0.0 <= mx <= 1.0 and 0.0 <= my <= 1.0
        for pts, colour in fr.lines:
            assert len(pts) == 512 and all(0 <= c <= 255 for c in colour)
        for x0, y0, x1, y1, colour in fr.stars:
            assert len(colour) == 3
        if fr.ifs is not None:
            sims, depth, colour, incr = fr.ifs
            assert 2 <= len(sims) <= 5 and depth >= 1 and incr >= 1
        if fr.tentacles is not None:
            grids = fr.tentacles[0]
            assert len(grids) == 6
            assert all(len(g.y) == g.defx * g.defz for g in grids)
    assert gooms >= 10 and fields >= 5 and len(modes) >= 3


def test_goom_core_is_deterministic_with_a_seed():
    from lindvb.goom import GoomCore
    a, b = GoomCore(320, seed=7), GoomCore(320, seed=7)
    for k in range(200):
        d = _goom_pcm(k, k % 15 == 0)
        fa, fb = a.update(d, d), b.update(d, d)
        assert fa.blend == fb.blend and fa.stars == fb.stars and fa.lines == fb.lines


def test_goom_hd_steps_at_goom_rate():
    from lindvb.visuals import GoomHD
    an = AudioAnalysis()
    sm = Smoother()
    style = GoomHD(seed=3)
    steps = 0
    for k in range(120):                            # 2 s at 60 fps
        an.feed_buffer(struct.pack("<2048f", *_sine(amp=0.4)), "F32LE", 2, 48000)
        an.stamp = now = 1000.0 + k / 60
        steps += len(style.steps(sm.update(an, now=now), 2560, 1440))
    assert 55 <= steps <= 62                        # 30 GOOM frames a second
    assert style.core.w == round(200 * 2560 / 1440) and style.core.h == 200


def test_pcm_block():
    from lindvb.audioviz import pcm_block
    vals = []
    for i in range(600):
        vals += [i / 1000, -i / 1000]
    left, right = pcm_block(struct.pack(f"<{len(vals)}f", *vals), "F32LE", 2)
    assert len(left) == len(right) == 512
    assert left[10] == pytest.approx(0.01) and right[10] == pytest.approx(-0.01)
    left, right = pcm_block(struct.pack("<4h", 16384, -16384, 0, 0), "S16LE", 2)
    assert left[0] == pytest.approx(0.5) and right[0] == pytest.approx(-0.5)


def test_visualiser_sizes():
    from lindvb.addons.gst_visualizers import parse_size
    assert parse_size("2560x1440", None) == (2560, 1440)
    assert parse_size(" 1279 X 721 ", None) == (1278, 720)
    assert parse_size("auto", (640, 360)) == (640, 360)
    assert parse_size(None, (640, 360)) == (640, 360)


def test_projectm_preset_folders(tmp_path):
    from lindvb.addons.gst_visualizers import preset_folders
    for rel in ("Dancer/Aurora/a.milk", "Dancer/Aurora/Deep/b.milk", "! Transition/c.milk",
                "Waveform/d.milk", "Empty/readme.txt", "top.milk"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("")
    assert preset_folders(str(tmp_path)) == ["Dancer", "Dancer/Aurora", "! Transition", "Waveform"]
    assert preset_folders(None) == []


def _frames(n, amp, spectrum_db):
    an, sm = AudioAnalysis(), Smoother()
    for k in range(n):
        an.feed_buffer(struct.pack("<2048f", *_sine(freq=80, amp=amp)), "F32LE", 2, 48000)
        an.feed_spectrum([spectrum_db - j * 0.1 for j in range(256)])
        an.stamp = now = 1000.0 + k / 60
        yield sm.update(an, now=now)


def test_light_ribbons_follow_the_music():
    from lindvb.visuals import LightRibbons
    quiet, loud = LightRibbons(), LightRibbons()
    for f in _frames(60, 0.01, -78.0):
        rq = quiet.params(f, 1080)
    for f in _frames(60, 0.6, -15.0):
        rl = loud.params(f, 1080)
    assert len(rl) == len(LightRibbons.RIBBONS)
    for q, lo in zip(rq, rl):
        assert lo[1] > q[1] and lo[4] > q[4]          # louder: higher waves, wider ribbons
    assert max(loud.spectrum.values) > 0.5 > max(quiet.spectrum.values)
    assert all(0.0 <= loud.spec_at(u / 10) <= 1.0 for u in range(11))


def test_pulsar_bursts_with_the_music():
    from lindvb.visuals import Pulsar
    style = Pulsar()
    for f in _frames(120, 0.6, -15.0):
        p = style.params(f, 1920, 1080)
        dust = style.step(f, p["r0"], 1080)
    assert dust and len(style.parts) <= Pulsar.MAX
    assert len(p["layers"]) == 5 and len(p["orbits"]) == 3
    assert 0.1 * 1080 < p["r0"] < 0.25 * 1080
    for turn, gain, alpha, sharp, col in p["layers"]:
        assert gain > 0 and 0 < alpha <= 1 and len(col) == 3
    assert all(0.0 <= style.spec_at(a / 16) <= 1.0 for a in range(-16, 33))


def test_painted_scenes_follow_the_music_slowly():
    from lindvb.visuals import AuroraBorealis, OccasusSolis, Supernova
    for cls in (Supernova, OccasusSolis, AuroraBorealis):
        quiet, loud = cls(), cls()
        for f in _frames(120, 0.01, -78.0):
            pq = quiet.params(f)
        for f in _frames(120, 0.6, -15.0):
            pl = loud.params(f)
        assert pl[5] > pq[5]                       # louder: more energy ...
        assert pl[0] > pq[0]                       # ... and the scene drifts a little faster
        assert pl[0] < 2 * 0.5                     # but slowly: under 1 unit in 2 s
        assert len(loud.spectrum.values) == 48 and max(loud.spectrum.values) <= 1.0


def test_scene_shaders_declare_what_the_renderer_sets():
    pytest.importorskip("cairo")
    from lindvb.ui.gpuvisuals import SceneRenderer
    for shader in SceneRenderer.SHADERS.values():
        for name in ("u_t", "u_clock", "u_unit", "u_pulse", "u_energy", "u_spec"):
            assert f" {name}" in shader, name


def test_supernova_veils_take_their_strength_from_the_music_at_birth():
    from lindvb.visuals import Supernova
    quiet, loud = Supernova(), Supernova()
    for f in _frames(600, 0.01, -78.0):
        quiet.params(f)
    for f in _frames(600, 0.6, -15.0):
        loud.params(f)
    assert len(loud.veils) == Supernova.VEILS
    assert min(loud.veils) > max(quiet.veils)
    before, flow = list(loud.veils), loud.flow
    for f in _frames(1, 0.0, -90.0):            # a single quiet frame changes no living veil
        loud.params(f)
    assert loud.flow >= flow and sum(a != b for a, b in zip(before, loud.veils)) <= 1


def test_aurora_mountains_scroll_left_and_grow_from_the_music():
    from lindvb.visuals import AuroraBorealis
    style = AuroraBorealis(seed=5)
    first = list(style.ridge)
    for f in _frames(600, 0.6, -15.0):              # 10 s
        style.params(f)
    assert len(style.ridge) == AuroraBorealis.RIDGE
    assert 0.0 <= style.scroll < 1.0 and style.world > 5
    shifted = int(style.world)
    assert style.ridge[:AuroraBorealis.RIDGE - shifted] == first[shifted:]   # moved to the left
    assert all(0.02 <= h <= 0.26 for h in style.ridge)
