# SPDX-License-Identifier: GPL-2.0-or-later
"""The CEA-708 decoder and renderer of stage F (lindvb.cc708, subrender)."""

import pytest

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


def test_caption_report_of_the_renderer_that_draws():
    sh = sr.CaptionShadow("service 1", drawn=True)
    sh.saw_new("LinDVB 708 0", 0)
    line = sh.report(sr.State(sr.CEA708, on=True, data=25))
    assert line.startswith("CEA-708 renderer (service 1): 1 captions drawn; data 25, dropped 0")


def test_packets_over_several_frames_are_drawn():
    """Broadcasters spread a DTVCC packet over frames, a few triplets each
    (PBS WJXT); cc708overlay drew nothing of them, the renderer decodes them."""
    got = []
    r = sr.Cea708Renderer(lambda text, t: got.append((text, t)), timer=False)
    r.show(SERVICE1)
    pkt = _packet(0, _df() + _t("WJXT NEWS") + CR + [0x89, 0x01])
    trip = [bytes([0xff if i == 0 else 0xfe, pkt[i], pkt[i + 1]]) for i in range(0, len(pkt), 2)]
    for f, k in enumerate(range(0, len(trip), 2)):
        r.feed(b"".join(trip[k:k + 2]) + bytes([0xfa, 0, 0]) * 18, "cc_data", f * 1000)
    r.decode()
    assert got and got[-1][0] == "WJXT NEWS" and got[-1][1] == 0      # the time of its start


class _Clock:
    def __init__(self):
        self.pos = None

    def __call__(self):
        return self.pos


def test_caption_drawer_shows_each_change_with_its_frame():
    pos, shown = _Clock(), []
    d = sr.CaptionDrawer(pos, shown.append)
    d.add("A", 1000)
    d.add("A B", 2000)
    d.add("", 3000)
    pos.pos = 500
    d.tick()
    assert shown == []                      # its frame is not on screen yet
    pos.pos = 2500
    d.tick()
    assert shown == ["A B"]                 # the latest due; "A" was over before a look
    d.tick()
    assert shown == ["A B"]                 # once
    pos.pos = 3000
    d.tick()
    assert shown == ["A B", ""]


def test_caption_drawer_without_position_or_time_shows_at_once():
    pos, shown = _Clock(), []
    d = sr.CaptionDrawer(pos, shown.append)
    d.add("A", 1000)
    d.tick()                                # no position (yet)
    pos.pos = 0
    d.add("B", None)                        # no time
    d.tick()
    assert shown == ["A", "B"]


def test_caption_drawer_after_a_seek_back():
    """The frames before the seek point bring their captions again: what
    was still to come of the old ones goes."""
    pos, shown = _Clock(), []
    d = sr.CaptionDrawer(pos, shown.append)
    d.add("late 1", 50_000)
    d.add("late 2", 60_000)
    d.add("again", 10_000)                  # seek back
    pos.pos = 70_000
    d.tick()
    assert shown == ["again"]


def test_caption_drawer_keeps_a_bounded_queue():
    pos, shown = _Clock(), []
    d = sr.CaptionDrawer(pos, shown.append)
    for i in range(sr.CC_DRAW_KEEP + 50):
        d.add(f"c{i}", i)
    assert len(d._changes) == sr.CC_DRAW_KEEP
    pos.pos = 10**9
    d.tick()
    assert shown == [f"c{sr.CC_DRAW_KEEP + 49}"]


def test_caption_drawer_stop_takes_the_text_away():
    pos, shown = _Clock(), []
    d = sr.CaptionDrawer(pos, shown.append)
    pos.pos = 0
    d.add("A", 0)
    d.tick()
    d.stop()
    assert shown == ["A", ""]
    d.stop()
    assert shown == ["A", ""]


def test_caption_drawer_counts_for_the_debug_log():
    """How long before its frame each change came, how long after its frame
    it was shown, and what the drawer threw away (WJXT, 27.11.2026: "not in
    sync, much left out": the log is to say whether LinDVB is the cause)."""
    pos, shown = _Clock(), []
    d = sr.CaptionDrawer(pos, shown.append)
    pos.pos = 0
    d.add("A", 400_000_000)                 # 0.4 s before its frame
    d.add("A B", 500_000_000)
    pos.pos = 550_000_000
    d.tick()                                # both due: "A" shown together with "A B"
    d.add("C", 450_000_000)                 # its frame was already shown: late
    pos.pos = 600_000_000
    d.tick()
    d.add("D", None)
    d.tick()
    assert shown == ["A B", "C", "D"]
    assert (d.shown, d.together, d.untimed, d.back, d.overflow) == (3, 1, 1, 0, 0)
    assert d.late.n == 2 and d.late.mean == pytest.approx(0.1) and d.late.high == pytest.approx(0.15)
    assert d.early.low == pytest.approx(-0.1) and d.early.high == pytest.approx(0.5)
    text = d.summary()
    assert text.startswith("drawer: 3 changes shown (1 more came with the next within 0.1 s), "
                           "shown 0.100 s after their frame on average (most 0.150 s); "
                           "the data came 0.267 s before its frame on average "
                           "(least -0.100 s, most 0.500 s)"), text
    assert text.endswith("thrown away: 0 with more than 300 waiting, 0 as a time went back; "
                         "1 shown without a time"), text


def test_caption_drawer_counts_what_it_throws_away():
    pos, shown = _Clock(), []
    d = sr.CaptionDrawer(pos, shown.append)
    for i in range(sr.CC_DRAW_KEEP + 5):
        d.add(f"c{i}", 10_000 + i)
    d.add("back", 0)                        # a seek back: all that waited goes
    assert d.overflow == 5 and d.back == sr.CC_DRAW_KEEP
    assert d.summary() == ("drawer: 0 changes shown (0 more came with the next within 0.1 s); "
                           f"thrown away: 5 with more than {sr.CC_DRAW_KEEP} waiting, "
                           f"{sr.CC_DRAW_KEEP} as a time went back; 0 shown without a time")
