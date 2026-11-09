# SPDX-License-Identifier: GPL-2.0-or-later
"""Small channel logos for the channel list, made once and kept on disk.

A logo (picon or DVB-I logo) is scaled down, its plain border is cut away
(``content_box``) and the result is saved as a small PNG in the cache
folder. The file name comes from the source path, its modification time and
size, so a changed logo or a new picon set makes new thumbnails by itself.
From then on only the small file is read, which is cheap.

``Loader`` does that work on one background thread, newest request first, so
the list keeps scrolling while logos come in, and keeps a bounded number of
finished textures in memory.
"""
import collections
import hashlib
import os
import threading

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from .nowplaying import content_box  # noqa: E402

VERSION = 1             # bump when the way thumbnails are made changes
SIZE = 64               # longest side of a thumbnail: 32 px rows on a 2x screen
CROP_W, CROP_H = 320, 180  # the logo is scaled to this before the border is found


def thumb_name(src):
    """Cache file name of the thumbnail of *src*, or None if *src* is missing."""
    try:
        st = os.stat(src)
    except OSError:
        return None
    sig = f"{VERSION}|{SIZE}|{os.path.abspath(src)}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha1(sig.encode("utf-8", "surrogateescape")).hexdigest() + ".png"


def make_thumb(src, folder):
    """Path of the thumbnail of *src* in *folder*, made now if needed; None if
    the logo cannot be read. A logo that cannot be read leaves an empty file
    behind, so it is not tried again until it changes."""
    name = thumb_name(src)
    if name is None:
        return None
    out = os.path.join(folder, name)
    try:
        if os.path.getsize(out) > 0:
            return out
        return None                     # known to be unreadable
    except OSError:
        pass
    os.makedirs(folder, exist_ok=True)
    tmp = f"{out}.{threading.get_ident()}.tmp"
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(src, CROP_W, CROP_H, True)
        x, y, w, h = content_box(pb.get_pixels(), pb.get_width(), pb.get_height(),
                                 pb.get_rowstride(), pb.get_n_channels())
        if (w, h) != (pb.get_width(), pb.get_height()):
            pb = pb.new_subpixbuf(x, y, w, h).copy()
        scale = SIZE / max(pb.get_width(), pb.get_height())
        if scale < 1:
            pb = pb.scale_simple(max(1, round(pb.get_width() * scale)),
                                 max(1, round(pb.get_height() * scale)),
                                 GdkPixbuf.InterpType.BILINEAR)
        pb.savev(tmp, "png", [], [])
        os.replace(tmp, out)
        return out
    except (GLib.Error, OSError, ValueError, ZeroDivisionError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        try:
            open(out, "wb").close()
        except OSError:
            pass
        return None


class Loader:
    """Logo textures for the channel list, made on a background thread.

    ``get(path)`` returns the texture when it is ready, else None and queues
    the logo; *on_ready(path, texture)* is then called on the main loop.
    *load* turns a thumbnail file into a texture (Gdk.Texture.new_from_filename
    in the app)."""

    def __init__(self, folder, load, on_ready, limit=800, queue_limit=400):
        self.folder = str(folder)
        self.load = load
        self.on_ready = on_ready
        self.limit = limit
        self.queue_limit = queue_limit
        self.cache = collections.OrderedDict()   # path -> texture or None
        self._queue = collections.OrderedDict()  # path -> True, newest last
        self._busy = set()                       # being made right now
        self._cond = threading.Condition()
        self._thread = None

    def get(self, path):
        """(known, texture): *known* False means the logo is still on its way."""
        if path in self.cache:
            self.cache.move_to_end(path)
            return True, self.cache[path]
        with self._cond:
            if path in self._busy:
                return False, None
            self._queue[path] = True
            self._queue.move_to_end(path)
            while len(self._queue) > self.queue_limit:  # scrolled far past: forget the oldest
                self._queue.popitem(last=False)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="lindvb-logos",
                                                daemon=True)
                self._thread.start()
            self._cond.notify()
        return False, None

    def clear(self):
        self.cache.clear()

    def _run(self):
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                path, _x = self._queue.popitem(last=True)    # newest first
                self._busy.add(path)
            tex = None
            try:
                thumb = make_thumb(path, self.folder)
                tex = self.load(thumb) if thumb else None
            except Exception:           # a broken logo must never stop the others
                tex = None
            GLib.idle_add(self._deliver, path, tex)

    def _deliver(self, path, tex):
        with self._cond:
            self._busy.discard(path)
        self.cache[path] = tex
        self.cache.move_to_end(path)
        while len(self.cache) > self.limit:
            self.cache.popitem(last=False)
        self.on_ready(path, tex)
        return False
