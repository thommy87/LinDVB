"""Preview pictures of recordings for the Recordings tab.

A background thread decodes one frame (10 % into the file, at most 5 minutes)
with GStreamer and saves it as PNG in ~/.cache/lindvb/thumbnails. Audio files
and files that cannot be decoded get no picture.
"""

import hashlib
import queue
import threading
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gst  # noqa: E402

from .config import CACHE_DIR  # noqa: E402

THUMB_DIR = CACHE_DIR / "thumbnails"
WIDTH = 160


def thumb_path(path, mtime):
    h = hashlib.sha1(f"{Path(path).resolve()}:{int(mtime)}".encode()).hexdigest()
    return THUMB_DIR / f"{h}.png"


def grab_frame(path, out, width=WIDTH, timeout=8):
    """Decode one frame of ``path`` and save it as PNG at ``out``. True on success."""
    uri = Gst.filename_to_uri(str(Path(path).resolve()))
    desc = (f'uridecodebin uri="{uri}" name=src ! videoconvert ! videoscale ! '
            f'video/x-raw,format=RGB,width={width},pixel-aspect-ratio=1/1 ! '
            'appsink name=sink sync=false max-buffers=1 drop=true')
    try:
        pipe = Gst.parse_launch(desc)
    except GLib.Error:
        return False
    sink = pipe.get_by_name("sink")
    try:
        pipe.set_state(Gst.State.PAUSED)
        ret, _state, _pending = pipe.get_state(timeout * Gst.SECOND)
        if ret == Gst.StateChangeReturn.FAILURE:
            return False
        ok, dur = pipe.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            target = min(dur // 10, 300 * Gst.SECOND)
            pipe.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, target)
            pipe.get_state(timeout * Gst.SECOND)
        sample = sink.emit("try-pull-preroll", timeout * Gst.SECOND)
        if sample is None:
            return False
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        stride = len(data) // h
        pix = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB,
                                              False, 8, w, h, stride)
        out.parent.mkdir(parents=True, exist_ok=True)
        pix.savev(str(out), "png", [], [])
        return True
    except Exception:          # noqa: BLE001 - a missing preview is not an error
        return False
    finally:
        pipe.set_state(Gst.State.NULL)


class Thumbnailer:
    """Makes previews one at a time; ``callback(path, png)`` runs in the main loop."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.failed = set()
        self.thread = None

    def request(self, path, mtime, callback):
        out = thumb_path(path, mtime)
        if out.exists():
            return out
        if str(out) in self.failed:
            return None
        self.jobs.put((path, out, callback))
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return None

    def _run(self):
        while True:
            try:
                path, out, callback = self.jobs.get(timeout=2)
            except queue.Empty:
                return
            if out.exists() or grab_frame(path, out):
                GLib.idle_add(callback, path, out)
            else:
                self.failed.add(str(out))
