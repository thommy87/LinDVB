# SPDX-License-Identifier: GPL-2.0-or-later
"""A CEA-608 (line 21) caption decoder: the text a caption channel shows.

The renderer of stage F decodes CEA-608 itself: GStreamer's only CEA-608
decoders (cea608tott, cea608overlay) are in gst-plugins-rs, which most
systems do not have, and cc708overlay draws CEA-708 only.

A channel's captions are byte pairs (parity in the top bit) in one field:
CC1 and CC2 in field 1, CC3 and CC4 in field 2. Commands come as pairs
whose first byte is 0x10-0x1F (0x10-0x17 data channel 1, 0x18-0x1F data
channel 2) and are sent twice; the repeat is dropped. The modes (CEA-608-E):

* pop-on: text goes to the memory not shown, End Of Caption swaps the two
* roll-up (2, 3 or 4 rows): text goes on the bottom row shown, Carriage
  Return moves the rows up
* paint-on: text goes straight into the memory shown

Text mode (TR, RTD) and XDS (field 2) are not captions and are skipped.
"""

ROWS, COLUMNS = 15, 32

# the row a preamble address code names: first byte (data channel 1) -> rows
_PAC_ROWS = {0x11: (1, 2), 0x12: (3, 4), 0x15: (5, 6), 0x16: (7, 8), 0x17: (9, 10),
             0x10: (11, 11), 0x13: (12, 13), 0x14: (14, 15)}

# the basic characters that are not ASCII
_BASIC = {0x2a: "á", 0x5c: "é", 0x5e: "í", 0x5f: "ó", 0x60: "ú", 0x7b: "ç", 0x7c: "÷",
          0x7d: "Ñ", 0x7e: "ñ", 0x7f: "█"}
# special characters (0x11 0x30-0x3F)
_SPECIAL = "®°½¿™¢£♪à èâêîôû"
# extended characters (0x12 / 0x13 0x20-0x3F): Spanish, French, Portuguese, German, Danish
_EXT_12 = "ÁÉÓÚÜü‘¡*'—©℠•“”ÀÂÇÈÊËëÎÏïÔÙùÛ«»"
_EXT_13 = "ÃãÍÌìÒòÕõ{}\\^_|~ÄäÖöß¥¤│ÅåØø┌┐└┘"

POP_ON, ROLL_UP, PAINT_ON, TEXT = "pop-on", "roll-up", "paint-on", "text"


def _blank():
    return {}


class Cea608Decoder:
    """Decodes one caption channel (1-4: CC1-CC4). :meth:`feed` takes the
    byte pairs of its field; :attr:`text` is what is shown now."""

    def __init__(self, channel=1):
        self.channel = channel
        self.field = 1 if channel <= 2 else 2
        self._data_channel = 1 if channel in (1, 3) else 2
        self._current = 0               # the data channel the pairs are for (0: none yet)
        self._last = None               # the last command pair, to drop its repeat
        self._xds = False
        self.mode = None
        self.shown = _blank()           # {row: {column: char}}
        self.hidden = _blank()
        self.row, self.column = ROWS, 0
        self.rollup_rows = 2

    # -- input ----------------------------------------------------------------------

    def feed(self, b1, b2):
        """One byte pair of this decoder's field; True when the text shown changed."""
        b1, b2 = b1 & 0x7f, b2 & 0x7f
        if not b1 and not b2:
            return False
        before = self.text
        if 0x10 <= b1 <= 0x1f:
            self._control(b1, b2)
        elif 0x01 <= b1 <= 0x0f:            # XDS, field 2
            self._xds = b1 != 0x0f
            self._last = None
        else:
            self._last = None
            self._characters(b1, b2)
        return self.text != before

    def _control(self, b1, b2):
        self._xds = False
        pair = (b1, b2)
        if pair == self._last:              # the repeat of a command
            self._last = None
            return
        self._last = pair
        self._current = 1 if b1 < 0x18 else 2
        if self._current == self._data_channel:
            self._command(b1 & 0xf7, b2)

    def _characters(self, b1, b2):
        if self._xds or self._current != self._data_channel or self.mode in (None, TEXT):
            return
        for b in (b1, b2):
            if b >= 0x20:
                self._write(_BASIC.get(b, chr(b)))

    # -- commands -------------------------------------------------------------------

    def _command(self, b1, b2):
        if b1 in (0x14, 0x15) and 0x20 <= b2 <= 0x2f:
            self._misc(b2)
        elif b1 == 0x17 and 0x21 <= b2 <= 0x23:          # tab offset
            self.column = min(COLUMNS - 1, self.column + b2 - 0x20)
        elif b1 == 0x11 and 0x30 <= b2 <= 0x3f:          # special character
            self._char(_SPECIAL[b2 - 0x30])
        elif b1 in (0x12, 0x13) and 0x20 <= b2 <= 0x3f:  # extended: replaces the last
            self._backspace()
            self._char((_EXT_12 if b1 == 0x12 else _EXT_13)[b2 - 0x20])
        elif b1 == 0x11 and 0x20 <= b2 <= 0x2f:          # mid-row code: a space
            self._char(" ")
        elif 0x40 <= b2 <= 0x7f and b1 in _PAC_ROWS:
            self._preamble(b1, b2)

    def _misc(self, code):
        handlers = {0x20: self._rcl, 0x21: self._backspace, 0x24: self._delete_to_end,
                    0x25: lambda: self._roll_up(2), 0x26: lambda: self._roll_up(3),
                    0x27: lambda: self._roll_up(4), 0x29: self._rdc, 0x2a: self._text_mode,
                    0x2b: self._text_mode, 0x2c: self._erase_shown, 0x2d: self._carriage_return,
                    0x2e: self._erase_hidden, 0x2f: self._end_of_caption}
        handler = handlers.get(code)
        if handler is not None:
            handler()

    def _rcl(self):
        self.mode = POP_ON

    def _rdc(self):
        self.mode = PAINT_ON

    def _text_mode(self):
        self.mode = TEXT

    def _roll_up(self, rows):
        if self.mode != ROLL_UP:
            self.shown, self.hidden = _blank(), _blank()
            self.row, self.column = ROWS, 0
        self.mode, self.rollup_rows = ROLL_UP, rows

    def _erase_shown(self):
        self.shown = _blank()

    def _erase_hidden(self):
        self.hidden = _blank()

    def _end_of_caption(self):
        self.shown, self.hidden = self.hidden, self.shown
        self.mode = POP_ON

    def _carriage_return(self):
        if self.mode != ROLL_UP:
            return
        top = self.row - self.rollup_rows + 1
        moved = {}
        for r, cells in self.shown.items():
            if top < r <= self.row:
                moved[r - 1] = cells
        self.shown = moved
        self.column = 0

    def _preamble(self, b1, b2):
        rows = _PAC_ROWS[b1]
        row = rows[1] if b2 & 0x20 else rows[0]
        if self.mode == ROLL_UP and row != self.row:
            # the roll-up window moves to the new base row, with its rows
            shift = row - self.row
            self.shown = {r + shift: c for r, c in self.shown.items() if 1 <= r + shift <= ROWS}
        self.row = row
        self.column = ((b2 & 0x1e) - 0x10) * 2 if b2 & 0x10 else 0

    # -- the memories ---------------------------------------------------------------

    def _memory(self):
        return self.hidden if self.mode == POP_ON else self.shown

    def _char(self, ch):
        if self.mode in (None, TEXT):
            return
        self._write(ch)

    def _write(self, ch):
        cells = self._memory().setdefault(self.row, {})
        cells[self.column] = ch
        self.column = min(COLUMNS - 1, self.column + 1)

    def _backspace(self):
        if self.mode in (None, TEXT) or self.column == 0:
            return
        self.column -= 1
        self._memory().get(self.row, {}).pop(self.column, None)

    def _delete_to_end(self):
        cells = self._memory().get(self.row, {})
        for c in [c for c in cells if c >= self.column]:
            del cells[c]

    # -- output ---------------------------------------------------------------------

    @property
    def text(self):
        """The rows shown, top to bottom, without the space around them."""
        lines = []
        for r in sorted(self.shown):
            cells = self.shown[r]
            if not cells:
                continue
            line = "".join(cells.get(c, " ") for c in range(max(cells) + 1)).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)
