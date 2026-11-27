# SPDX-License-Identifier: GPL-2.0-or-later
"""A CEA-708 (DTVCC) caption decoder: the text a caption service shows.

The renderer of stage F decodes CEA-708 itself and LinDVB draws the text:
GStreamer's cc708overlay drew nothing of packets spread over frames, as
broadcasters send them, and GStreamer 1.28 of some distributions lacks it. The service blocks of a service
(lindvb.captions.dtvcc_blocks) carry commands and characters (CEA-708-E
7.1.4): eight windows, each defined with a size and shown or hidden, a pen
that writes into the current one. What is shown is the text of the
visible windows, by priority.

Not decoded, as they change nothing of the text: pen and window
attributes (SPA, SPC, SWA), Delay (the text comes at once).
"""

MAX_ROWS, MAX_COLUMNS = 15, 42

# C1 commands: the parameter bytes that follow each (0x80-0x9F)
_C1_PARAMS = bytes([0] * 8 + [1, 1, 1, 1, 1, 1, 0, 0] + [2, 3, 2, 0, 0, 0, 0, 4] + [6] * 8)

# G0 is ASCII but for 0x7F; G2 (behind EXT1) as far as it holds text
_G0 = {0x7f: "♪"}
_G2 = {0x20: " ", 0x21: " ", 0x25: "…", 0x2a: "Š", 0x2c: "Œ", 0x30: "█", 0x31: "‘",
       0x32: "’", 0x33: "“", 0x34: "”", 0x35: "•", 0x39: "™", 0x3a: "š", 0x3c: "œ", 0x3d: "℠",
       0x3f: "Ÿ", 0x76: "⅛", 0x77: "⅜", 0x78: "⅝", 0x79: "⅞", 0x7a: "│", 0x7b: "┐", 0x7c: "└",
       0x7d: "─", 0x7e: "┘", 0x7f: "┌"}


class Window:
    def __init__(self, rows, columns, visible, priority):
        self.rows, self.columns = rows, columns
        self.visible, self.priority = visible, priority
        self.cells = {}                 # {row: {column: char}}
        self.row = self.column = 0

    def write(self, ch):
        if self.column >= self.columns:            # past the last column: the next row
            self.carriage_return()
        self.cells.setdefault(self.row, {})[self.column] = ch
        self.column += 1

    def carriage_return(self):
        self.column = 0
        if self.row + 1 < self.rows:
            self.row += 1
            return
        # the bottom row: the rows move up
        self.cells = {r - 1: c for r, c in self.cells.items() if r > 0}

    def backspace(self):
        if self.column > 0:
            self.column -= 1
            self.cells.get(self.row, {}).pop(self.column, None)

    def clear(self):
        self.cells = {}
        self.row = self.column = 0

    def text(self):
        lines = []
        for r in sorted(self.cells):
            cells = self.cells[r]
            if cells:
                line = "".join(cells.get(c, " ") for c in range(max(cells) + 1)).strip()
                if line:
                    lines.append(line)
        return "\n".join(lines)


class Cea708Decoder:
    """Decodes the service blocks of one service; :attr:`text` is what it
    shows now (visible windows, the higher priority first)."""

    def __init__(self, service=1):
        self.service = service
        self.windows = {}               # {id: Window}
        self.current = None

    def reset(self):
        self.windows, self.current = {}, None

    # -- input ----------------------------------------------------------------------

    def feed(self, block):
        """One service block of this service; True when the text shown changed."""
        before = self.text
        i = 0
        while i < len(block):
            n = self._code(block, i)
            if n is None:
                break
            i += n
        return self.text != before

    def _code(self, block, i):
        """Takes the code at *i*; returns its length (None: unknown, stop)."""
        b = block[i]
        if 0x20 <= b < 0x80:
            self._write(_G0.get(b, chr(b)))
            return 1
        if b >= 0xa0:
            self._write(chr(b))                     # G1: Latin-1
            return 1
        if b == 0x10:
            return self._ext1(block, i)
        if b < 0x20:
            return self._c0(block, i)
        n = _C1_PARAMS[b - 0x80]
        if i + n >= len(block):
            return None
        self._c1(b, block[i + 1:i + 1 + n])
        return 1 + n

    def _ext1(self, block, i):
        if i + 1 >= len(block):
            return None
        e = block[i + 1]
        if 0x20 <= e < 0x80:
            ch = _G2.get(e)
            if ch:
                self._write(ch)
            return 2
        if e >= 0xa0:                               # G3: the [CC] symbol and reserved
            if e == 0xa0:
                self._write("[CC]")
            return 2
        if e < 0x20:                                # C2
            return 2 + e // 8
        if e < 0x90:                                # C3
            return 2 + (4 if e < 0x88 else 5)
        return None

    def _c0(self, block, i):
        b = block[i]
        w = self._window()
        if b == 0x08 and w:                         # BS
            w.backspace()
        elif b == 0x0c and w:                       # FF: clear the window
            w.clear()
        elif b == 0x0d and w:                       # CR
            w.carriage_return()
        elif b == 0x0e and w:                       # HCR: clear the row
            w.cells.pop(w.row, None)
            w.column = 0
        elif b == 0x18 and i + 2 < len(block):      # P16: a 16 bit character
            self._write(chr(block[i + 1] << 8 | block[i + 2]))
        return 1 + (b >= 0x10) + (b >= 0x18)

    def _c1(self, b, params):
        if b <= 0x87:                               # CW0-7: the current window
            if b - 0x80 in self.windows:
                self.current = b - 0x80
        elif b <= 0x8c:                             # CLW DSW HDW TGW DLW: windows of a bitmap
            self._windows_command(b, params[0])
        elif b == 0x8f:                             # RST
            self.reset()
        elif b == 0x92:                             # SPL: pen location
            w = self._window()
            if w:
                w.row = min(params[0] & 0x0f, w.rows - 1)
                w.column = min(params[1] & 0x3f, w.columns - 1)
        elif b >= 0x98:                             # DF0-7: define a window
            self._define(b - 0x98, params)

    def _windows_command(self, b, bitmap):
        for wid in [w for w in range(8) if bitmap & (1 << w) and w in self.windows]:
            w = self.windows[wid]
            if b == 0x88:
                w.clear()
            elif b == 0x89:
                w.visible = True
            elif b == 0x8a:
                w.visible = False
            elif b == 0x8b:
                w.visible = not w.visible
            else:
                del self.windows[wid]
                if self.current == wid:
                    self.current = None

    def _define(self, wid, p):
        rows = min((p[3] & 0x0f) + 1, MAX_ROWS)
        columns = min((p[4] & 0x3f) + 1, MAX_COLUMNS)
        visible, priority = bool(p[0] & 0x20), p[0] & 0x07
        w = self.windows.get(wid)
        if w is None:
            self.windows[wid] = Window(rows, columns, visible, priority)
        else:                                       # defined again: keeps its text
            w.rows, w.columns, w.visible, w.priority = rows, columns, visible, priority
            w.row, w.column = min(w.row, rows - 1), min(w.column, columns)
        self.current = wid

    def _window(self):
        return self.windows.get(self.current)

    def _write(self, ch):
        w = self._window()
        if w is not None:
            w.write(ch)

    # -- output ---------------------------------------------------------------------

    @property
    def text(self):
        shown = sorted((w.priority, wid) for wid, w in self.windows.items() if w.visible)
        texts = [self.windows[wid].text() for _p, wid in shown]
        return "\n".join(t for t in texts if t)
