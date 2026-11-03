"""The visualisation area shown over the video for radio and audio files.

Built-in styles are drawn on the graphics card (a ``Gtk.GLArea`` with the
shaders from :mod:`lindvb.ui.gpuvisuals`) at every frame of the display and
at the full resolution of the window. Plugin styles, and every style when
OpenGL is not available or turned off, are drawn with cairo at ~30 fps.
"""

import os
import time

from gi.repository import Gdk, Gtk

from ..audioviz import Smoother
from ..visuals import Blank, rgb
from .theme import ACCENT
from ..i18n import _

FPS = 30                # cairo drawing
GPU_FPS = 60            # the GPU path follows the display, up to this rate


class VisualizerArea(Gtk.Box):
    """Draws the selected style from the player's audio analysis."""

    def __init__(self, analysis, gpu=True):
        super().__init__(hexpand=True, vexpand=True, can_target=False)
        self.add_css_class("lv-visualizer")
        self.analysis = analysis
        self.smoother = Smoother()
        self.style = Blank()
        self.caption = ""
        self.accent = rgb(ACCENT)
        self._last_draw = 0.0
        self._failed = False
        self.gpu_error = None
        self._gpu_wanted = gpu and not os.environ.get("LINDVB_NO_GPU_VIS")

        self.cairo_area = Gtk.DrawingArea(hexpand=True, vexpand=True, can_target=False)
        self.cairo_area.set_draw_func(self._draw)
        self.append(self.cairo_area)

        self.gl_area = None
        self._ctx = None
        self._renderer = None
        self._renderer_style = None
        self._garbage = []
        if self._gpu_wanted:
            self.gl_area = Gtk.GLArea(hexpand=True, vexpand=True, can_target=False,
                                      has_depth_buffer=False, auto_render=False)
            if hasattr(self.gl_area, "set_allowed_apis") and hasattr(Gdk, "GLAPI"):
                self.gl_area.set_allowed_apis(Gdk.GLAPI.GL | Gdk.GLAPI.GLES)
            self.gl_area.connect("realize", self._gl_realize)
            self.gl_area.connect("unrealize", self._gl_unrealize)
            self.gl_area.connect("render", self._gl_render)
            self.append(self.gl_area)
        self._choose_area()
        self.add_tick_callback(self._tick)

    # -- public API -------------------------------------------------------------------
    def set_style(self, style):
        self.style = style or Blank()
        self._failed = False
        self._choose_area()
        self._queue()

    def set_gpu(self, enabled):
        """Turn drawing on the graphics card on or off (takes effect at once)."""
        self._gpu_wanted = bool(enabled)
        self._choose_area()
        self._queue()

    @property
    def uses_gpu(self):
        return self.gl_area is not None and self.gl_area.get_visible()

    def set_accent(self, hex_colour):
        """Draw with the user's accent colour ('#RRGGBB')."""
        try:
            self.accent = rgb(hex_colour)
        except (ValueError, TypeError):
            self.accent = rgb(ACCENT)

    def set_caption(self, text):
        self.caption = text or ""
        self._queue()

    # -- internals --------------------------------------------------------------------
    def _gpu_possible(self, style):
        if self.gl_area is None or not self._gpu_wanted or self.gpu_error:
            return False
        from .gpuvisuals import renderer_class
        return renderer_class(style) is not None

    def _choose_area(self):
        gpu = self._gpu_possible(self.style)
        if self.gl_area is not None:
            self.gl_area.set_visible(gpu)
        self.cairo_area.set_visible(not gpu)

    def _queue(self):
        if self.uses_gpu:
            self.gl_area.queue_render()
        else:
            self.cairo_area.queue_draw()

    def _frame(self):
        frame = self.smoother.update(self.analysis, time.monotonic())
        frame.accent = self.accent
        frame.caption = self.caption
        return frame

    def _tick(self, _widget, clock):
        if not self.get_mapped():
            return True
        now = clock.get_frame_time() / 1e6
        if self.uses_gpu:
            # every frame of the display, but not more than GPU_FPS (120/144 Hz screens)
            if now - self._last_draw >= 1.0 / GPU_FPS - 0.002:
                self._last_draw = now
                self.gl_area.queue_render()
            return True
        if now - self._last_draw >= 1.0 / FPS - 0.002:
            self._last_draw = now
            self.cairo_area.queue_draw()
        return True

    def _draw(self, _area, cr, width, height):
        frame = self._frame()
        try:
            self.style.draw(cr, width, height, frame)
        except Exception as e:      # a broken plugin style falls back to the plain one
            if not self._failed:
                self._failed = True
                print(_("Visualisation '{v0}' failed: {e}").format(v0=getattr(self.style, 'label', self.style), e=e))
            Blank().draw(cr, width, height, frame)

    def _gpu_failed(self, reason):
        self.gpu_error = str(reason)
        print(f"GPU visualisation not available, drawing with cairo: {reason}")
        self._choose_area()
        self.cairo_area.queue_draw()

    def _gl_realize(self, area):
        area.make_current()
        err = area.get_error()
        if err is not None:
            self._gpu_failed(err.message)
            return
        ctx = area.get_context()
        es = ctx.get_use_es()
        major, minor = ctx.get_version()
        if (major, minor) < ((3, 0) if es else (3, 3)):
            self._gpu_failed(_("OpenGL{v0} {major}.{minor} is too old").format(v0=' ES' if es else '', major=major, minor=minor))
            return
        try:
            from .gpuvisuals import Context
            self._ctx = Context(es)
        except Exception as e:
            self._gpu_failed(e)

    def _gl_unrealize(self, area):
        area.make_current()
        if self._ctx is not None:
            try:
                for r in self._garbage + ([self._renderer] if self._renderer else []):
                    r.free()
                self._ctx.free()
            except Exception:
                pass
        self._ctx = self._renderer = self._renderer_style = None
        self._garbage = []

    def _gl_render(self, area, _gl_context):
        if self._ctx is None:
            return True
        from .gpuvisuals import renderer_class
        ctx = self._ctx
        try:
            if self._renderer_style is not self.style:
                if self._renderer is not None:
                    self._garbage.append(self._renderer)
                self._renderer = None
                cls = renderer_class(self.style)
                if cls is None:
                    return True
                self._renderer = cls(ctx, self.style)
                self._renderer_style = self.style
            for r in self._garbage:
                r.free()
            self._garbage = []
            scale = area.get_scale_factor()
            width = max(1, area.get_width() * scale)
            height = max(1, area.get_height() * scale)
            ctx.begin(width, height, ctx.gl.current_framebuffer())
            self._renderer.render(self._frame())
            ctx.gl.check("after rendering")
        except Exception as e:
            self._renderer = None
            self._gpu_failed(e)
        return True
