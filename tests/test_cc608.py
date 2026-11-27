# SPDX-License-Identifier: GPL-2.0-or-later
"""The CEA-608 decoder and renderer of stage F (lindvb.cc608, subrender)."""

from lindvb import subrender as sr
from lindvb.cc608 import Cea608Decoder
from lindvb.trackcatalog import SUBTITLE, Track


def _p(b):
    b &= 0x7f
    return b | (0x80 if bin(b).count("1") % 2 == 0 else 0)


def _feed(dec, pairs):
    changed = []
    for b1, b2 in pairs:
        if dec.feed(_p(b1), _p(b2)):
            changed.append(dec.text)
    return changed


def _text(s):
    s += "\0" * (len(s) % 2)
    return [(ord(s[i]), ord(s[i + 1])) for i in range(0, len(s), 2)]


RCL, EOC, EDM, ENM, CR = (0x14, 0x20), (0x14, 0x2f), (0x14, 0x2c), (0x14, 0x2e), (0x14, 0x2d)
RU2, RU3, RDC, BS, DER = (0x14, 0x25), (0x14, 0x26), (0x14, 0x29), (0x14, 0x21), (0x14, 0x24)
ROW15, ROW14, ROW1 = (0x14, 0x60), (0x14, 0x40), (0x11, 0x40)


def _twice(*codes):
    return [c for code in codes for c in (code, code)]


def test_pop_on():
    """Text goes to the memory not shown; End Of Caption shows it."""
    dec = Cea608Decoder(1)
    seen = _feed(dec, _twice(RCL, ENM, ROW14) + _text("HELLO") + _twice(ROW15) + _text("WORLD"))
    assert seen == [] and dec.text == ""
    assert _feed(dec, _twice(EOC)) == ["HELLO\nWORLD"]
    assert _feed(dec, _twice(EDM)) == [""]


def test_roll_up():
    """Roll-up 2: each carriage return moves the rows up, the oldest goes."""
    dec = Cea608Decoder(1)
    _feed(dec, _twice(RU2, CR, ROW15) + _text("ONE"))
    assert dec.text == "ONE"
    _feed(dec, _twice(CR, ROW15) + _text("TWO"))
    assert dec.text == "ONE\nTWO"
    _feed(dec, _twice(CR, ROW15) + _text("THREE"))
    assert dec.text == "TWO\nTHREE"
    _feed(dec, _twice(RU3, CR) + _text("FOUR"))       # three rows now: "TWO" stays
    assert dec.text == "TWO\nTHREE\nFOUR"
    _feed(dec, _twice(CR) + _text("FIVE"))
    assert dec.text == "THREE\nFOUR\nFIVE"


def test_paint_on_and_editing():
    dec = Cea608Decoder(1)
    seen = _feed(dec, _twice(RDC, ROW1) + _text("AB"))
    assert seen == ["AB"]                                   # a pair writes two at once
    _feed(dec, _twice(BS))
    assert dec.text == "A"
    _feed(dec, _text("XYZ") + _twice((0x17, 0x22)))          # tab offset 2
    _feed(dec, _text("!"))
    assert dec.text == "AXYZ  !"
    _feed(dec, _twice((0x11, 0x41)))                        # row 1, column 0
    _feed(dec, _twice((0x14, 0x24)))                        # delete to the end of the row
    assert dec.text == ""


def test_repeated_commands_count_once():
    dec = Cea608Decoder(1)
    _feed(dec, _twice(RU2, CR, ROW15) + _text("A"))
    _feed(dec, [CR, CR, CR, CR])                            # two carriage returns, each sent twice
    _feed(dec, _twice(ROW15) + _text("B"))
    assert dec.text == "B"                                  # "A" rolled out of the 2 rows


def test_characters():
    dec = Cea608Decoder(1)
    _feed(dec, _twice(RDC, ROW1) + [(0x2a, 0x5c)])          # á é: basic characters not in ASCII
    _feed(dec, _twice((0x11, 0x37)))                        # ♪ (special)
    _feed(dec, _text("A") + _twice((0x13, 0x31)))           # A, then extended ä over it
    _feed(dec, _twice((0x11, 0x20)) + _text("X"))           # a mid-row code is a space
    assert dec.text == "áé♪ä X"


def test_channels_and_fields():
    """CC2 data (commands 0x18-0x1F) is not CC1's; CC3 is in field 2 and
    uses 0x15 for its commands."""
    cc1, cc2, cc3 = Cea608Decoder(1), Cea608Decoder(2), Cea608Decoder(3)
    pairs = _twice((0x1c, 0x29), (0x19, 0x40)) + _text("TWO") + _twice(RDC, ROW1) + _text("ONE")
    for b1, b2 in pairs:
        cc1.feed(_p(b1), _p(b2))
        cc2.feed(_p(b1), _p(b2))
    assert cc1.text == "ONE" and cc2.text == "TWO"
    assert cc1.field == 1 and cc3.field == 2
    _feed(cc3, _twice((0x15, 0x29), (0x15, 0x60)) + _text("THREE"))
    assert cc3.text == "THREE"


def test_xds_and_text_mode_are_not_captions():
    dec = Cea608Decoder(3)
    _feed(dec, _twice((0x15, 0x29), (0x15, 0x60)) + _text("CAP"))
    _feed(dec, [(0x01, 0x03)] + _text("NEWS") + [(0x0f, 0x12)])      # XDS programme name
    assert dec.text == "CAP"
    _feed(dec, _twice((0x15, 0x2a)) + _text("TEXT MODE"))
    assert dec.text == "CAP"
    assert _feed(dec, [(0, 0), (0x80, 0x80)]) == []                    # padding


def test_preamble_indent():
    dec = Cea608Decoder(1)
    _feed(dec, _twice(RDC, (0x14, 0x52)) + _text("X"))      # row 14, indent 4
    assert dec.column == 5
    assert dec.text == "X"


# -- the renderer -------------------------------------------------------------------------

def _cc_data(pairs, field=1):
    return b"".join(bytes([0xfc if field == 1 else 0xfd, _p(a), _p(b)]) for a, b in pairs)


CC1 = Track(SUBTITLE, sr.CEA608, origin="cc:CC1")


def test_renderer_decodes_in_its_own_time():
    """The video thread only queues the pairs; the main loop decodes."""
    got = []
    r = sr.Cea608Renderer(lambda text, t: got.append((text, t)), timer=False)
    assert r.can(CC1) and not r.can(Track(SUBTITLE, sr.CEA708, origin="cc:1"))
    r.feed(_cc_data(_twice(RU2)))                           # off: takes nothing in
    assert r.state().data == 0
    r.show(CC1)
    for i, pair in enumerate(_twice(RU2, CR, ROW15) + _text("HI")):
        r.feed(_cc_data([pair]) + bytes([0xfa, 0, 0]), "cc_data", i * 40_000_000)
    r.feed(_cc_data(_text("NO"), field=2))                  # field 2: not CC1's
    assert got == []
    r.decode()
    assert got == [("HI", 6 * 40_000_000)]
    st = r.state()
    assert st.on and st.data == 7 and st.decoded == 1 and st.last == "HI" and st.track == "cc:CC1"
    r.off()
    assert got[-1] == ("", None) and not r.state().on


def test_renderer_queue_drops_the_oldest():
    r = sr.Cea608Renderer(lambda text, t: None, queue_pairs=4, timer=False)
    r.show(CC1)
    r.feed(_cc_data(_text("ABCDEFGH")))
    assert r.state().dropped == 0
    r.feed(_cc_data(_text("IJKL")))
    assert r.state().dropped == 2


def test_caption_pairs_of_the_formats():
    assert sr.cc608_pairs(bytes([0xfc, 1, 2, 0xfd, 3, 4, 0xfa, 0, 0, 0xf8, 5, 6])) == [
        (1, 1, 2), (2, 3, 4)]                               # 708 and not valid: left out
    assert sr.cc608_pairs(bytes([1, 2, 3, 4]), "raw") == [(1, 1, 2), (1, 3, 4)]
    assert sr.cc608_pairs(bytes([0x80, 1, 2, 0x00, 3, 4]), "s334-1a") == [(1, 1, 2), (2, 3, 4)]


def test_caption_shadow_report():
    sh = sr.CaptionShadow("CC1")
    assert sh.report(sr.State(sr.CEA608, on=True)) is None
    st = sr.State(sr.CEA608, on=True, data=120)
    assert sh.report(st) == ("CEA-608 renderer (shadow, CC1): 0 captions decoded; data 120, "
                             "dropped 0; the caption data held no text for this channel")
    for i in range(5):
        sh.saw_new(f"LINE {i}\nMO", i)
        sh.saw_new(f"LINE {i}\nMORE", i + 0.5)              # the line grew: the same caption
    assert sh.captions[0] == ("LINE 0\nMORE", 0)
    assert sh.report(st) == ("CEA-608 renderer (shadow, CC1): 5 captions decoded; data 120, "
                             "dropped 0; first 'LINE 0 / MORE', 'LINE 1 / MORE', 'LINE 2 / MORE', "
                             "last 'LINE 4 / MORE'")
