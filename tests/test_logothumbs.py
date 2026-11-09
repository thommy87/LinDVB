# SPDX-License-Identifier: GPL-2.0-or-later
import os
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
except (ValueError, ImportError):
    pytest.skip("GdkPixbuf not available", allow_module_level=True)

from lindvb import logothumbs  # noqa: E402


def _board(path, w=320, h=180):
    """A 16:9 white board with a red logo in the middle."""
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
    pb.fill(0xffffffff)
    pb.new_subpixbuf(w // 2 - 40, h // 2 - 20, 80, 40).fill(0xff0000ff)
    pb.savev(str(path), "png", [], [])


def test_thumb_is_cropped_small_and_kept(tmp_path):
    src, cache = tmp_path / "logo.png", tmp_path / "cache"
    _board(src)
    out = logothumbs.make_thumb(str(src), str(cache))
    assert out and os.path.dirname(out) == str(cache)
    pb = GdkPixbuf.Pixbuf.new_from_file(out)
    assert max(pb.get_width(), pb.get_height()) <= logothumbs.SIZE
    # the white border is gone: the logo is wide, not 16:9 any more
    assert pb.get_width() / pb.get_height() > 1.6
    # a second call reads the file that is there
    mtime = os.stat(out).st_mtime_ns
    assert logothumbs.make_thumb(str(src), str(cache)) == out
    assert os.stat(out).st_mtime_ns == mtime


def test_changed_logo_gets_a_new_thumb(tmp_path):
    src, cache = tmp_path / "logo.png", tmp_path / "cache"
    _board(src)
    first = logothumbs.make_thumb(str(src), str(cache))
    _board(src, 200, 200)
    os.utime(src, ns=(time.time_ns(), time.time_ns() + 10**9))
    second = logothumbs.make_thumb(str(src), str(cache))
    assert second and second != first


def test_unreadable_and_missing_logos(tmp_path):
    bad, cache = tmp_path / "bad.png", tmp_path / "cache"
    bad.write_bytes(b"not an image")
    assert logothumbs.make_thumb(str(bad), str(cache)) is None
    marker = cache / logothumbs.thumb_name(str(bad))
    assert marker.exists() and marker.stat().st_size == 0      # not tried again
    assert logothumbs.make_thumb(str(bad), str(cache)) is None
    assert logothumbs.make_thumb(str(tmp_path / "missing.png"), str(cache)) is None
    assert not [p for p in cache.iterdir() if p.suffix == ".tmp"]


def test_loader_works_in_the_background(tmp_path):
    srcs = []
    for i in range(3):
        p = tmp_path / f"l{i}.png"
        _board(p)
        srcs.append(str(p))
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"x")
    srcs.append(str(bad))
    ready = {}
    loader = logothumbs.Loader(tmp_path / "cache", GdkPixbuf.Pixbuf.new_from_file,
                               lambda path, tex: ready.__setitem__(path, tex), limit=2)
    for s in srcs:
        assert loader.get(s) == (False, None)
    ctx = GLib.MainContext.default()
    end = time.monotonic() + 10
    while len(ready) < len(srcs) and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)
    assert set(ready) == set(srcs)
    assert ready[str(bad)] is None
    assert all(ready[s] is not None for s in srcs[:3])
    assert len(loader.cache) == 2                   # bounded
    # newest request first: the first logo asked for came in last and is kept
    known, tex = loader.get(srcs[0])
    assert known and tex is not None
