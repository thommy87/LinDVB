# SPDX-License-Identifier: GPL-2.0-or-later
"""The CEA-708 decoder and renderer of stage F (lindvb.cc708, subrender)."""

from lindvb import subrender as sr
from lindvb.cc708 import Cea708Decoder
from lindvb.trackcatalog import SUBTITLE, Track


def _df(wid=0, visible=True, priority=0, rows=2, columns=32):
    """DefineWindow (DF0-7): visibility and priority, anchor, size."""
    return [0x98 + wid, (0x20 if visible else 0) | priority, 0x0a, 0x00,
            rows - 1, columns - 1, 0x09]


def _t(s):
    return list(s.encode("latin-1"))


CR, BS, FF, HCR = [0x0d], [0x08], [0x0c], [0x0e]


def _bitmap(cmd, *wids):
    return [cmd, sum(1 << w for w in wids)]


CLW, DSW, HDW, TGW, DLW = 0x88, 0x89, 0x8a, 0x8b, 0x8c


def _feed(dec, *codes):
    return dec.feed(bytes(c for part in codes for c in part))


def test_window_shows_its_text():
    dec = Cea708Decoder(1)
    assert _feed(dec, _df(0, visible=False), _t("HELLO")) is False      # hidden: nothing shown
    assert dec.text == ""
    assert _feed(dec, _bitmap(DSW, 0)) is True
    assert dec.text == "HELLO"
    _feed(dec, CR, _t("WORLD"))
    assert dec.text == "HELLO\nWORLD"
    _feed(dec, _bitmap(HDW, 0))
    assert dec.text == ""
    _feed(dec, _bitmap(TGW, 0))
    assert dec.text == "HELLO\nWORLD"
    _feed(dec, _bitmap(CLW, 0))
    assert dec.text == ""
    _feed(dec, _t("AGAIN"))
    assert dec.text == "AGAIN"
    _feed(dec, _bitmap(DLW, 0), _t("LOST"))                             # no window: no text
    assert dec.text == "" and dec.windows == {}


def test_carriage_return_scrolls_at_the_bottom():
    dec = Cea708Decoder(1)
    _feed(dec, _df(0, rows=2), _t("ONE"), CR, _t("TWO"))
    assert dec.text == "ONE\nTWO"
    _feed(dec, CR, _t("THREE"))
    assert dec.text == "TWO\nTHREE"


def test_a_full_row_goes_on_in_the_next():
    dec = Cea708Decoder(1)
    _feed(dec, _df(0, rows=2, columns=4), _t("ABCDEF"))
    assert dec.text == "ABCD\nEF"


def test_editing_codes():
    dec = Cea708Decoder(1)
    _feed(dec, _df(0), _t("ABC"), BS)
    assert dec.text == "AB"
    _feed(dec, CR, _t("ROW"), HCR, _t("NEW"))
    assert dec.text == "AB\nNEW"
    _feed(dec, FF)
    assert dec.text == ""


def test_pen_location():
    dec = Cea708Decoder(1)
    _feed(dec, _df(0, rows=3), [0x92, 2, 3], _t("X"), [0x92, 0, 0], _t("Y"))
    assert dec.text == "Y\nX"
    _feed(dec, [0x92, 9, 60], _t("Z"))                                  # past the window: kept in
    assert dec.windows[0].row == 2 and dec.windows[0].column == 32


def test_characters():
    dec = Cea708Decoder(1)
    _feed(dec, _df(0), _t("a"), [0x7f], [0xe4],                       # ♪, then G1 (Latin-1) ä
          [0x10, 0x25], [0x10, 0x39], [0x10, 0xa0],                    # G2 … ™, G3 [CC]
          [0x18, 0x20, 0xac])                                          # P16: €
    assert dec.text == "a♪ä…™[CC]€"


def test_unknown_and_attribute_codes_change_no_text():
    dec = Cea708Decoder(1)
    _feed(dec, _df(0), _t("A"),
          [0x90, 0, 0], [0x91, 0, 0, 0], [0x97, 0, 0, 0, 0],          # SPA SPC SWA
          [0x10, 0x05], [0x10, 0x85, 1, 2, 3, 4], _t("B"))            # C2, C3 skipped
    assert dec.text == "AB"
    _feed(dec, [0x8f])                                                  # RST
    assert dec.text == "" and dec.windows == {}


def test_windows_by_priority_and_current():
    dec = Cea708Decoder(1)
    _feed(dec, _df(1, priority=3), _t("LOW"), _df(0, priority=1), _t("HIGH"))
    assert dec.text == "HIGH\nLOW"
    _feed(dec, [0x81], _t("ER"), [0x85], _t("NOT DEFINED"))             # CW1; CW5 is no window
    assert dec.text == "HIGH\nLOWERNOT DEFINED"
    _feed(dec, _df(1, priority=3, rows=1, columns=8))                    # defined again: text kept
    assert dec.text == "HIGH\nLOWERNOT DEFINED"


def test_a_truncated_command_stops_the_block():
    dec = Cea708Decoder(1)
    assert _feed(dec, _df(0), _t("A"), [0x92, 1]) is True                # SPL lacks a byte
    assert dec.text == "A"


# -- the renderer -------------------------------------------------------------------------

def _packet(seq, block, service=1):
    body = bytes([service << 5 | len(block)]) + bytes(block)
    size = len(body) + 1
    size += size % 2
    pkt = bytes([(seq & 3) << 6 | size // 2]) + body
    return pkt + b"\0" * (size - len(pkt))


def _cc_data(pkt, padding=True, start=True):
    trip = b""
    for i in range(0, len(pkt), 2):
        trip += bytes([0xff if i == 0 and start else 0xfe, pkt[i], pkt[i + 1]])
    return trip + (bytes([0xfa, 0, 0]) if padding else b"")


SERVICE1 = Track(SUBTITLE, sr.CEA708, origin="cc:1")


def test_renderer_decodes_in_its_own_time():
    """The video thread only queues the DTVCC data; the main loop puts the
    packets together and decodes the blocks of its service."""
    got = []
    r = sr.Cea708Renderer(lambda text, t: got.append((text, t)), timer=False)
    assert r.can(SERVICE1) and not r.can(Track(SUBTITLE, sr.CEA608, origin="cc:CC1"))
    r.show(SERVICE1)
    block = bytes(_df(0) + _t("HI 708"))
    pkt = _packet(0, block)
    r.feed(_cc_data(pkt[:4], padding=False), "cc_data", 1)              # a packet over two frames
    r.feed(_cc_data(pkt[4:], start=False), "cc_data", 2)
    r.feed(_cc_data(_packet(1, _t("NOT MINE"), service=2)), "cc_data", 3)
    r.feed(bytes([0xfc, 0x94, 0x2c, 0xfa, 0, 0]), "cc_data", 4)        # 608 and padding only
    r.feed(_cc_data(pkt), "s334-1a", 5)                                 # not cc_data: not taken
    assert got == []
    r.decode()
    assert got == [("HI 708", 1)]
    st = r.state()
    assert st.on and st.data == 3 and st.decoded == 1 and st.last == "HI 708"
    assert st.track == "cc:1"
    r.off()
    assert got[-1] == ("", None) and not r.state().on


def test_renderer_ends_a_packet_by_its_size():
    """No padding after the packet: its size says it is complete."""
    got = []
    r = sr.Cea708Renderer(lambda text, t: got.append(text), timer=False)
    r.show(SERVICE1)
    r.feed(_cc_data(_packet(0, _df(0) + _t("DONE")), padding=False))
    r.decode()
    assert got == ["DONE"]


def test_renderer_queue_drops_the_oldest():
    r = sr.Cea708Renderer(lambda text, t: None, queue_frames=2, timer=False)
    r.feed(_cc_data(_packet(0, _t("X"))))                               # off: takes nothing in
    assert r.state().data == 0
    r.show(SERVICE1)
    for i in range(5):
        r.feed(_cc_data(_packet(i, _t("X"))))
    assert r.state().data == 5 and r.state().dropped == 3


def test_caption_shadow_names_the_service():
    sh = sr.CaptionShadow("service 1")
    sh.saw_new("LinDVB 708 0", 0)
    line = sh.report(sr.State(sr.CEA708, on=True, data=25))
    assert line == ("CEA-708 renderer (shadow, service 1): 1 captions decoded; data 25, "
                    "dropped 0; first 'LinDVB 708 0', last 'LinDVB 708 0'")
