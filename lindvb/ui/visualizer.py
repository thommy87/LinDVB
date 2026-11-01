"""The visualisation area shown over the video for radio and audio files."""

import time

from gi.repository import Gtk

from ..audioviz import Smoother
from ..visuals import Blank, rgb
from .theme import ACCENT

FPS = 30


class VisualizerArea(Gtk.DrawingArea):
    """Draws the selected style from the player's audio analysis at ~30 fps."""

    def __init__(self, analysis):
        super().__init__(hexpand=True, vexpand=True, can_target=False)
        self.add_css_class("lv-visualizer")
        self.analysis = analysis
        self.smoother = Smoother()
        self.style = Blank()
        self.caption = ""
        self.accent = rgb(ACCENT)
        self._last_draw = 0.0
        self._failed = False
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._tick)

    def set_style(self, style):
        self.style = style or Blank()
        self._failed = False
        self.queue_draw()

    def set_accent(self, hex_colour):
        """Draw with the user's accent colour ('#RRGGBB')."""
        try:
            self.accent = rgb(hex_colour)
        except (ValueError, TypeError):
            self.accent = rgb(ACCENT)

    def set_caption(self, text):
        self.caption = text or ""
        self.queue_draw()

    def _tick(self, _widget, clock):
        now = clock.get_frame_time() / 1e6
        if self.get_mapped() and now - self._last_draw >= 1.0 / FPS - 0.002:
            self._last_draw = now
            self.queue_draw()
        return True

    def _draw(self, _area, cr, width, height):
        frame = self.smoother.update(self.analysis, time.monotonic())
        frame.accent = self.accent
        frame.caption = self.caption
        try:
            self.style.draw(cr, width, height, frame)
        except Exception as e:      # a broken plugin style falls back to the plain one
            if not self._failed:
                self._failed = True
                print(f"Visualisation '{getattr(self.style, 'label', self.style)}' failed: {e}")
            Blank().draw(cr, width, height, frame)
