# SPDX-License-Identifier: GPL-2.0-or-later
"""A DVB subtitle decoder (EN 300 743): what a subtitle page shows.

The DVB-SUB renderer of stage F decodes the subtitles itself, apart from
the old path (dvbsuboverlay, which blends them into the picture and lets
nothing be read back), to know whether data comes and whether something
is shown, and where.

DVB subtitles are bitmaps. A PES packet (data identifier 0x20) holds
segments; a display set (up to the end of display set segment) says which
regions a page shows and where, the regions hold objects, the objects
pixel data in 2, 4 or 8 bit codes, the CLUTs which pixel codes are
transparent. Regions keep what was drawn into them until they are filled
again or a new epoch (mode change) begins.

What is decoded: the regions shown, which of their pixels are not
transparent (the T of their CLUT entry), the box around those pixels on
the display. Colours are not needed for that and are left out. Of one PID
the first page that composes is taken (the composition page); object data
and CLUTs of other pages (the ancillary page) are taken too.
"""

from dataclasses import dataclass

PCS, RCS, CLUT, ODS, DDS, EDS = 0x10, 0x11, 0x12, 0x13, 0x14, 0x80
DISPLAY = (720, 576)

# the default maps when an object's pixel codes have fewer bits than its region
_MAP_2_TO_4 = bytes([0x0, 0x7, 0x8, 0xf])
_MAP_2_TO_8 = bytes([0x00, 0x77, 0x88, 0xff])
_MAP_4_TO_8 = bytes(range(0, 256, 17))


@dataclass
class Page:
    """One display set: what the page shows from now on."""

    regions: int = 0                # regions shown
    pixels: int = 0                 # pixels that are not transparent
    box: tuple = None               # (x, y, width, height) around them on the display
    timeout: int = 0                # seconds it is shown at most (0: until the next)
    display: tuple = DISPLAY
    text: str = ""                  # objects sent as characters, not pixels

    def describe(self):
        if not self.pixels and not self.text:
            return "empty"
        parts = []
        if self.box:
            x, y, w, h = self.box
            parts.append(f"{w}x{h} at {x},{y}")
        if self.text:
            parts.append(repr(self.text))
        n = self.regions
        return " ".join(parts) + f" ({n} region{'s' if n != 1 else ''})"


class _Region:
    def __init__(self, width, height, depth):
        self.width, self.height, self.depth = width, height, depth
        self.clut = 0
        self.objects = []           # [(object id, x, y)]
        self.pixels = bytearray(width * height)

    def fill(self, code):
        self.pixels[:] = bytes([code]) * len(self.pixels)

    def run(self, x, y, code, n):
        if y >= self.height or x >= self.width or n <= 0:
            return
        n = min(n, self.width - x)
        at = y * self.width + x
        self.pixels[at:at + n] = bytes([code]) * n


def segments(pes):
    """[(type, page id, data)] of one PES packet's data (data identifier 0x20)."""
    if len(pes) < 2 or pes[0] != 0x20:
        return []
    out, i = [], 2
    while i + 6 <= len(pes) and pes[i] == 0x0f:
        length = pes[i + 4] << 8 | pes[i + 5]
        out.append((pes[i + 1], pes[i + 2] << 8 | pes[i + 3], bytes(pes[i + 6:i + 6 + length])))
        i += 6 + length
    return out


class _Bits:
    """Reads bits of *data* from bit *pos* on; the codes on a 4 or 8 bit
    boundary (most of them) are taken whole."""

    def __init__(self, data, i=0):
        self.data, self.pos = data, i * 8
        self.nibbles = [v for b in data for v in (b >> 4, b & 0x0f)]

    def get(self, n):
        """The next *n* bits (0 past the end)."""
        pos = self.pos
        self.pos = pos + n
        if n == 4 and not pos & 3 and pos >> 2 < len(self.nibbles):
            return self.nibbles[pos >> 2]
        if n == 8 and not pos & 7 and pos >> 3 < len(self.data):
            return self.data[pos >> 3]
        first, end = pos >> 3, (pos + n + 7) >> 3
        chunk = int.from_bytes(self.data[first:end].ljust(end - first, b"\0"), "big")
        return (chunk >> ((end << 3) - pos - n)) & ((1 << n) - 1)

    def align(self):
        self.pos = (self.pos + 7) & ~7
        return self.pos >> 3


def _string(bits, size, escape):
    """[(code, run)] of a pixel code string of *size* bits a code; *escape*
    reads what follows a code 0 (EN 300 743 7.2.5.2): (code, run), or None
    at the end of the string."""
    out = []
    while bits.pos < len(bits.data) * 8:
        code = bits.get(size)
        if code:
            out.append((code, 1))
            continue
        run = escape(bits)
        if run is None:
            break
        out.append(run)
    return out


def _escape_2bit(bits):
    if bits.get(1):                                 # a run of 3-10
        n = 3 + bits.get(3)
        return bits.get(2), n
    if bits.get(1):                                 # one pixel of 0
        return 0, 1
    sw = bits.get(2)
    if sw < 2:
        return None if sw == 0 else (0, 2)
    n = 12 + bits.get(4) if sw == 2 else 29 + bits.get(8)
    return bits.get(2), n


def _escape_4bit(bits):
    if not bits.get(1):
        n = bits.get(3)
        return (0, n + 2) if n else None            # a run of 0 (3-9), or the end
    if not bits.get(1):                             # a run of 4-7
        n = 4 + bits.get(2)
        return bits.get(4), n
    sw = bits.get(2)
    if sw < 2:
        return 0, sw + 1
    n = 9 + bits.get(4) if sw == 2 else 25 + bits.get(8)
    return bits.get(4), n


def _escape_8bit(bits):
    if not bits.get(1):
        n = bits.get(7)
        return (0, n) if n else None                # a run of 0 (1-127), or the end
    n = bits.get(7)
    return bits.get(8), n


_STRINGS = {0x10: (_escape_2bit, 2), 0x11: (_escape_4bit, 4), 0x12: (_escape_8bit, 8)}


class DvbSubDecoder:
    """Decodes the PES packets of one DVB subtitle PID; :meth:`feed`
    returns a :class:`Page` at the end of each display set of the
    composition page."""

    def __init__(self, page_id=None):
        self.page_id = page_id
        self.display = DISPLAY
        self.reset()

    def reset(self):
        self.regions = {}               # {id: _Region}
        self.shown = []                 # [(region id, x, y)] of the page now
        self.cluts = {}                 # {id: {(depth, entry): transparent}}
        self.timeout = 0
        self.text = {}                  # {object id: text}
        self._maps = {}
        self._composed = False

    # -- input ----------------------------------------------------------------------

    def feed(self, pes):
        """The data of one PES packet; a Page when a display set of the
        composition page ended in it (at its end segment, as dvbsuboverlay
        shows it)."""
        page = None
        for kind, page_id, data in segments(pes):
            if kind in (PCS, DDS) and self.page_id is None:
                self.page_id = page_id
            if kind in (PCS, RCS, DDS, EDS) and page_id != self.page_id:
                continue                # another page's composition (another language)
            if kind == EDS:
                if self._composed:
                    page = self._page()
                continue
            handler = {PCS: self._page_composition, RCS: self._region_composition,
                       CLUT: self._clut_definition, ODS: self._object_data,
                       DDS: self._display_definition}.get(kind)
            if handler is not None and data:
                handler(data)
                self._composed |= kind == PCS
        return page

    def _page_composition(self, d):
        if len(d) < 2:
            return
        state = (d[1] >> 2) & 0x03
        if state == 2 or (state == 1 and not self._composed):
            self.reset()                # a new epoch (mode change, acquisition point)
        self.timeout = d[0]
        self.shown = []
        for i in range(2, len(d) - 5, 6):
            self.shown.append((d[i], d[i + 2] << 8 | d[i + 3], d[i + 4] << 8 | d[i + 5]))

    def _region_composition(self, d):
        if len(d) < 10:
            return
        rid, fill = d[0], d[1] & 0x08
        width, height = d[2] << 8 | d[3], d[4] << 8 | d[5]
        depth = {1: 2, 2: 4, 3: 8}.get((d[6] >> 2) & 0x07, 4)
        r = self.regions.get(rid)
        if r is None or (r.width, r.height, r.depth) != (width, height, depth):
            r = self.regions[rid] = _Region(width, height, depth)
        r.clut = d[7]
        if fill:
            code = {2: (d[9] >> 2) & 0x03, 4: d[9] >> 4, 8: d[8]}[depth]
            r.fill(code)
        r.objects = []
        i = 10
        while i + 6 <= len(d):
            oid, otype = d[i] << 8 | d[i + 1], d[i + 2] >> 6
            x, y = (d[i + 2] & 0x0f) << 8 | d[i + 3], (d[i + 4] & 0x0f) << 8 | d[i + 5]
            r.objects.append((oid, x, y))
            i += 8 if otype in (1, 2) else 6

    def _clut_definition(self, d):
        clut = self.cluts.setdefault(d[0], {})
        i = 2
        while i + 2 <= len(d):
            entry, flags = d[i], d[i + 1]
            full = flags & 0x01
            size = 6 if full else 4
            if i + size > len(d):
                break
            # fully transparent: T at its top, or Y 0 (EN 300 743 7.2.4)
            y = d[i + 2] if full else d[i + 2] >> 2
            t = d[i + 5] if full else d[i + 3] & 0x03
            transparent = y == 0 or t == (0xff if full else 0x03)
            for depth, flag in ((2, 0x80), (4, 0x40), (8, 0x20)):
                if flags & flag:
                    clut[(depth, entry)] = transparent
            i += size

    def _object_data(self, d):
        if len(d) < 3:
            return
        oid, coding = d[0] << 8 | d[1], (d[2] >> 2) & 0x03
        if coding == 1:                 # characters
            n = d[3] if len(d) > 3 else 0
            self.text[oid] = "".join(chr(d[4 + 2 * k] << 8 | d[5 + 2 * k])
                                     for k in range(n) if 5 + 2 * k < len(d))
            return
        if coding != 0 or len(d) < 7:
            return
        top, bottom = d[3] << 8 | d[4], d[5] << 8 | d[6]
        top_data = d[7:7 + top]
        bottom_data = d[7 + top:7 + top + bottom] if bottom else top_data
        for r in self.regions.values():
            for o, x, y in r.objects:
                if o == oid:
                    self._draw(r, x, y, top_data, 0)
                    self._draw(r, x, y, bottom_data, 1)

    def _draw(self, region, x0, y0, data, field):
        """The pixel data of one field into *region* at (x0, y0)."""
        y, x, i = y0 + field, x0, 0
        self._maps = {}                             # each object starts with the default maps
        b = _Bits(data)
        while i < len(data):
            kind = data[i]
            i += 1
            if kind == 0xf0:                        # end of object line
                y, x = y + 2, x0
            elif kind in (0x20, 0x21, 0x22):        # map tables
                size = {0x20: 2, 0x21: 4, 0x22: 16}[kind]
                self._maps[kind] = self._map_table(kind, data[i:i + size])
                i += size
            elif kind in _STRINGS:
                escape, bits = _STRINGS[kind]
                b.pos = i * 8
                for code, n in _string(b, bits, escape):
                    region.run(x, y, self._mapped(code, bits, region.depth), n)
                    x += n
                i = b.align()
            else:
                break                               # unknown: the rest cannot be read

    @staticmethod
    def _map_table(kind, raw):
        """2_to_4: four 4-bit entries in two bytes; 2_to_8 and 4_to_8: a byte each."""
        if kind == 0x20:
            return bytes(v for b in raw for v in (b >> 4, b & 0x0f))
        return bytes(raw)

    def _mapped(self, code, bits, depth):
        if bits == depth:
            return code
        if bits < depth:
            table = {(2, 4): (0x20, _MAP_2_TO_4), (2, 8): (0x21, _MAP_2_TO_8),
                     (4, 8): (0x22, _MAP_4_TO_8)}[(bits, depth)]
            return self._maps.get(table[0], table[1])[code]
        return code >> (bits - depth)               # more bits than the region: the top ones

    def _display_definition(self, d):
        if len(d) >= 5:
            self.display = ((d[1] << 8 | d[2]) + 1, (d[3] << 8 | d[4]) + 1)

    # -- output ---------------------------------------------------------------------

    def _visible(self, region):
        """A translation table: 1 for the pixel codes of *region* that are
        not transparent (by default all but 0)."""
        clut = self.cluts.get(region.clut, {})
        return bytes(0 if clut.get((region.depth, c), c == 0) else 1 for c in range(256))

    def _page(self):
        """What the page shows now."""
        self._composed = False
        page = Page(timeout=self.timeout, display=self.display)
        box = None
        for rid, rx, ry in self.shown:
            r = self.regions.get(rid)
            if r is None:
                continue
            page.regions += 1
            mask = r.pixels.translate(self._visible(r))
            n = mask.count(1)
            if n:
                page.pixels += n
                box = _union(box, _box(mask, r.width, rx, ry))
            texts = [self.text[o] for o, _x, _y in r.objects if self.text.get(o)]
            page.text = " ".join([page.text] + texts).strip() if texts else page.text
        page.box = box
        return page


def _box(mask, width, x0, y0):
    """(x, y, w, h) on the display around the 1s of *mask* (rows of *width*)."""
    first, last = mask.find(1), mask.rfind(1)
    top, bottom = first // width, last // width
    left, right = width, -1
    for row in range(top, bottom + 1):
        line = mask[row * width:(row + 1) * width]
        a = line.find(1)
        if a >= 0:
            left, right = min(left, a), max(right, line.rfind(1))
    return (x0 + left, y0 + top, right - left + 1, bottom - top + 1)


def _union(a, b):
    if a is None:
        return b
    x, y = min(a[0], b[0]), min(a[1], b[1])
    return (x, y, max(a[0] + a[2], b[0] + b[2]) - x, max(a[1] + a[3], b[1] + b[3]) - y)
