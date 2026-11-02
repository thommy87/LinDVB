"""Now-playing overlay over the radio visualisation.

Separately switchable in Preferences › Visualisation: a station logo tile,
the album cover of the song on air, the song details (artist, album and year,
track and title) and a clock in the top right corner. It is a plain drawing
area over the visualisation, so it works with every style, the GPU ones and
the GStreamer visualisers alike, and is only redrawn when something changes.
"""

import threading
import time

import cairo
import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from ..config import CACHE_DIR, DATA_DIR  # noqa: E402
from ..nowplaying import find_logo, lookup_cover, song_from  # noqa: E402
from ..visuals import rgb  # noqa: E402
from .theme import ACCENT  # noqa: E402

COVER_CACHE = CACHE_DIR / "covers"
DEFAULT_LOGO_DIR = DATA_DIR / "logos"


def overlay_enabled(settings):
    return any(settings.get(k) for k in ("vis_show_logo", "vis_show_cover", "vis_show_song",
                                         "vis_show_clock"))


def _pixbuf(data=None, path=None, size=600):
    try:
        if path:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()
    except (GLib.Error, TypeError, ValueError):
        return None


def _rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.14159)
    cr.arc(x + r, y + r, r, 3.14159, 4.71239)
    cr.close_path()


def _esc(text):
    return GLib.markup_escape_text(str(text))


class NowPlayingOverlay(Gtk.DrawingArea):
    def __init__(self, settings):
        super().__init__(hexpand=True, vexpand=True, can_target=False)
        self.settings = settings
        self.accent = rgb(ACCENT)
        self.station = ""
        self.channel = True             # a TV/radio channel (not a file): logo tile
        self.programme = ""
        self.meta = {}
        self.song = song_from({})
        self.cover = None               # Pixbuf of the album cover
        self.logo = None                # Pixbuf of the station logo
        self._logo_for = None
        self._cover_key = None          # song the cover (lookup) belongs to
        self._lookup_gen = 0
        self._scaled = {}               # (id(pixbuf), size) -> scaled pixbuf
        self._clock = ""
        self.set_draw_func(self._draw)

    # -- state ---------------------------------------------------------------
    def set_accent(self, hex_colour):
        try:
            self.accent = rgb(hex_colour)
        except (ValueError, TypeError):
            self.accent = rgb(ACCENT)
        self.queue_draw()

    def set_station(self, name, programme=""):
        if name != self.station:
            self.station = name or ""
            self._logo_for = None
        if (programme or "") != self.programme:
            self.programme = programme or ""
            self.queue_draw()
        self._load_logo()

    def set_metadata(self, meta):
        self.meta = dict(meta or {})
        self.song = song_from(self.meta)
        key = (self.song["artist"].casefold(), self.song["title"].casefold())
        if self.meta.get("image"):
            self.cover = _pixbuf(self.meta["image"])
            self._cover_key = key
        elif key != self._cover_key:
            self.cover = None
            self._cover_key = key
            self._lookup(key)
        self.queue_draw()

    def refresh(self):
        """Settings changed: load what is now wanted and redraw."""
        self._logo_for = None
        self._load_logo()
        if self.cover is None and self._cover_key:
            self._lookup(self._cover_key)
        self.queue_draw()

    def tick(self):
        """Called every second: redraw when the clock moves on."""
        now = time.strftime("%H:%M")
        if now != self._clock:
            self._clock = now
            if self.get_visible() and self.settings.get("vis_show_clock"):
                self.queue_draw()

    def _load_logo(self):
        if not (self.settings.get("vis_show_logo") and self.channel) or \
                self._logo_for == self.station:
            return
        self._logo_for = self.station
        folder = self.settings.get("logo_dir") or str(DEFAULT_LOGO_DIR)
        path = find_logo(self.station, [folder, str(DEFAULT_LOGO_DIR)])
        self.logo = _pixbuf(path=path, size=512) if path else None
        self.queue_draw()

    def _lookup(self, key):
        """Find album, year and cover online (in the background)."""
        self._lookup_gen += 1
        artist, title = self.song["artist"], self.song["title"]
        if not (self.settings.get("vis_show_cover") and self.settings.get("vis_cover_online")
                and artist and title):
            return
        gen = self._lookup_gen

        def work():
            try:
                info = lookup_cover(artist, title, COVER_CACHE)
            except Exception as e:      # network down, odd answer: no cover then
                print(f"Cover lookup failed: {e}")
                info = None
            GLib.idle_add(self._lookup_done, gen, key, info)
        threading.Thread(target=work, daemon=True).start()

    def _lookup_done(self, gen, key, info):
        if gen != self._lookup_gen or key != self._cover_key or not info:
            return False
        if info.get("image"):
            self.cover = _pixbuf(info["image"])
        # album details from the lookup, where the station sent none
        for k in ("album", "year", "track"):
            if info.get(k) and not self.song.get(k):
                self.song[k] = info[k]
        self.queue_draw()
        return False

    # -- drawing -------------------------------------------------------------
    def _scaled_pb(self, pb, size):
        size = max(1, int(size))
        k = (id(pb), size)
        got = self._scaled.get(k)
        if got is None:
            w, h = pb.get_width(), pb.get_height()
            f = size / max(w, h)
            got = pb.scale_simple(max(1, int(w * f)), max(1, int(h * f)),
                                  GdkPixbuf.InterpType.BILINEAR)
            if len(self._scaled) > 8:
                self._scaled.clear()
            self._scaled[k] = got
        return got

    def _image(self, cr, pb, x, y, size, fit_bg=None):
        """Draw ``pb`` centred in a size×size square with rounded corners."""
        sp = self._scaled_pb(pb, size)
        cr.save()
        _rounded(cr, x, y, size, size, size * 0.035)
        cr.clip()
        if fit_bg:
            cr.set_source_rgba(*fit_bg)
            cr.paint()
        Gdk.cairo_set_source_pixbuf(cr, sp, x + (size - sp.get_width()) / 2,
                                    y + (size - sp.get_height()) / 2)
        cr.paint()
        cr.restore()

    def _shadow(self, cr, x, y, size, s):
        cr.save()
        for i in range(6, 0, -1):
            _rounded(cr, x - i * s, y - i * s + 4 * s, size + 2 * i * s, size + 2 * i * s,
                     size * 0.05 + i * s)
            cr.set_source_rgba(0, 0, 0, 0.05)
            cr.fill()
        cr.restore()

    def _logo_tile(self, cr, x, y, size, s):
        """Station logo, or a tile with the station name when there is none."""
        self._shadow(cr, x, y, size, s)
        if self.logo is not None:
            _rounded(cr, x, y, size, size, size * 0.06)
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.fill()
            pad = size * 0.1
            self._image(cr, self.logo, x + pad, y + pad, size - 2 * pad)
            return
        a = self.accent
        g = cairo.LinearGradient(x, y, x + size, y + size)
        g.add_color_stop_rgb(0, *a)
        g.add_color_stop_rgb(1, a[0] * 0.45, a[1] * 0.45, a[2] * 0.45)
        _rounded(cr, x, y, size, size, size * 0.06)
        cr.set_source(g)
        cr.fill()
        layout = PangoCairo.create_layout(cr)
        layout.set_width(int((size * 0.84) * Pango.SCALE))
        layout.set_height(int((size * 0.84) * Pango.SCALE))
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_wrap(Pango.WrapMode.WORD)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        name = self.station or "♪"
        fsize = size * (0.34 if len(name) <= 3 else 0.2 if len(name) <= 10 else 0.15)
        # shrink until the longest word fits on one line
        probe = PangoCairo.create_layout(cr)
        longest = max(name.split(), key=len)
        for _ in range(12):
            probe.set_font_description(
                Pango.FontDescription.from_string(f"Sans Bold {fsize:.1f}px"))
            probe.set_text(longest, -1)
            if probe.get_pixel_extents()[1].width <= size * 0.84:
                break
            fsize *= 0.9
        layout.set_font_description(Pango.FontDescription.from_string(f"Sans Bold {fsize:.1f}px"))
        layout.set_text(name, -1)
        _ink, logical = layout.get_pixel_extents()
        cr.move_to(x + size * 0.08, y + (size - logical.height) / 2)
        cr.set_source_rgb(1, 1, 1)
        PangoCairo.show_layout(cr, layout)

    def _text(self, cr, markup, x, y, width, px, weight="Light"):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(f"Sans {weight} {px:.1f}px"))
        layout.set_width(int(width * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_markup(markup, -1)
        _ink, logical = layout.get_pixel_extents()
        return layout, logical.height

    def _show(self, cr, layout, x, y, s):
        cr.move_to(x + 2 * s, y + 2 * s)
        cr.set_source_rgba(0, 0, 0, 0.55)
        PangoCairo.show_layout(cr, layout)
        cr.move_to(x, y)
        cr.set_source_rgba(1, 1, 1, 0.96)
        PangoCairo.show_layout(cr, layout)

    def _song_lines(self, cr, width, s):
        """Layouts of the song lines, like a music player's now-playing screen."""
        acc = "#%02x%02x%02x" % tuple(int(c * 255) for c in self.accent)
        song = self.song
        lines = []
        if song["artist"] or song["title"]:
            if song["artist"]:
                lines.append((_esc(song["artist"]), 60 * s, "Regular"))
            if song["album"]:
                year = f' <span foreground="{acc}">[{_esc(song["year"])}]</span>' \
                    if song["year"] else ""
                lines.append((_esc(song["album"]) + year, 36 * s, "Light"))
            if song["title"]:
                num = f'<span foreground="{acc}">{int(song["track"]):02d}:</span> ' \
                    if song["track"] else ""
                lines.append((num + _esc(song["title"]), 46 * s, "Light"))
        else:
            # no song information: the station and the programme on air
            if self.station:
                lines.append((_esc(self.station), 60 * s, "Regular"))
            if self.programme:
                lines.append((_esc(self.programme), 40 * s, "Light"))
        out = []
        for markup, px, weight in lines:
            out.append(self._text(cr, markup, 0, 0, width, px, weight))
        return out

    def _draw(self, _area, cr, w, h):
        st = self.settings
        s = max(0.35, min(w / 1920, h / 1080))
        margin = 32 * s
        if st.get("vis_show_clock"):
            layout, _lh = self._text(cr, _esc(time.strftime("%H:%M")), 0, 0, w, 76 * s, "Light")
            layout.set_width(-1)
            _ink, logical = layout.get_pixel_extents()
            self._show(cr, layout, w - margin - logical.width, margin * 0.5, s)

        cover = self.cover if st.get("vis_show_cover") else None
        logo = st.get("vis_show_logo") and self.channel
        tile = 470 * s
        lines = self._song_lines(cr, min(w - 2 * margin, max(w * 0.6, 900 * s)), s) \
            if st.get("vis_show_song") else []
        text_h = sum(lh for _l, lh in lines)
        big = cover is not None or (logo and cover is None)
        # keep the tile and the text inside the picture on small windows
        avail = h - 2 * margin - text_h - (20 * s if lines else 0) - (100 * s if cover and logo else 0)
        tile = max(0, min(tile, avail))
        if tile < 48 * s:
            big = False
        block = (tile + 20 * s if big else 0) + text_h
        y = max(margin, h - margin * 2 - block)
        x = margin
        if big:
            if cover is not None:
                self._shadow(cr, x, y, tile, s)
                self._image(cr, cover, x, y, tile, fit_bg=(0, 0, 0, 1))
            else:
                self._logo_tile(cr, x, y, tile, s)
            y += tile + 20 * s
        if cover is not None and logo:
            small = 150 * s
            self._logo_tile(cr, margin, margin, small, s)
        for layout, lh in lines:
            self._show(cr, layout, x, y, s)
            y += lh
