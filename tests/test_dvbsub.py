# SPDX-License-Identifier: GPL-2.0-or-later
"""The DVB subtitle decoder and renderer of stage F (lindvb.dvbsub, subrender)."""

import streamkit as sk

from lindvb import dvbsub
from lindvb import subrender as sr
from lindvb.dvbsub import DvbSubDecoder, Page
from lindvb.trackcatalog import SUBTITLE, Track


def _seg(kind, data, page=1):
    return sk._segment(kind, page, bytes(data))


def _pes(*segs):
    return bytes([0x20, 0x00]) + b"".join(segs) + bytes([0xff])


def _pcs(regions, state=0, timeout=5):
    """regions: [(region id, x, y)]"""
    d = [timeout, state << 2]
    for rid, x, y in regions:
        d += [rid, 0, x >> 8, x & 0xff, y >> 8, y & 0xff]
    return _seg(dvbsub.PCS, d)


def _rcs(rid, w, h, depth=4, objects=(), fill=None, clut=0):
    code = {2: 1, 4: 2, 8: 3}[depth]
    f = fill or 0
    d = [rid, 0x08 if fill is not None else 0, w >> 8, w & 0xff, h >> 8, h & 0xff,
         code << 2, clut, f if depth == 8 else 0,
         (f << 4 if depth == 4 else 0) | (f << 2 if depth == 2 else 0)]
    for oid, x, y in objects:
        d += [oid >> 8, oid & 0xff, x >> 8, x & 0xff, y >> 8, y & 0xff]
    return _seg(dvbsub.RCS, d)


def _ods(oid, top, bottom=b""):
    return _seg(dvbsub.ODS, bytes([oid >> 8, oid & 0xff, 0, len(top) >> 8, len(top) & 0xff,
                                   len(bottom) >> 8, len(bottom) & 0xff]) + top + bottom)


EDS = _seg(dvbsub.EDS, b"")


def test_a_display_set():
    dec = DvbSubDecoder()
    page = dec.feed(sk.dvbsub_display_set((100, 400, 300, 30)))
    assert page == Page(regions=1, pixels=9000, box=(100, 400, 300, 30), timeout=10)
    assert page.describe() == "300x30 at 100,400 (1 region)"
    page = dec.feed(sk.dvbsub_display_set(None, version=1, state=0))
    assert page.pixels == 0 and page.describe() == "empty"


def test_only_the_end_segment_ends_a_set():
    dec = DvbSubDecoder()
    line = sk.dvbsub_line([(1, 10)])
    assert dec.feed(_pes(_pcs([(0, 10, 20)], state=2), _rcs(0, 10, 1, objects=[(1, 0, 0)]))) is None
    page = dec.feed(_pes(_ods(1, line, line), EDS))
    assert page.box == (10, 20, 10, 1) and page.pixels == 10


def test_the_box_of_what_is_drawn():
    """A region larger than its text: the box is the text's, on the display;
    two regions: one box around both."""
    dec = DvbSubDecoder()
    line = sk.dvbsub_line([(0, 5), (3, 20)])
    page = dec.feed(_pes(_pcs([(0, 50, 500), (1, 400, 520)], state=2),
                         _rcs(0, 100, 10, objects=[(1, 2, 4)]), _rcs(1, 40, 4, objects=[(2, 0, 0)]),
                         _ods(1, line, line), _ods(2, line, b""), EDS))
    assert page.regions == 2
    assert page.box == (57, 504, 368, 18)               # x 57..424, y 504..521
    assert page.pixels == 40 + 2 * 20                   # bottom field: the top's lines again
    assert page.describe() == "368x18 at 57,504 (2 regions)"


def test_pixel_code_strings():
    """2-, 4- and 8-bit strings, the runs of each."""
    w = sk._BitWriter()
    w.put(1, 2)                                         # a pixel of 1
    w.put(0, 2), w.put(1, 1), w.put(2, 3), w.put(3, 2)  # 5 of 3
    w.put(0, 2), w.put(0, 1), w.put(1, 1)               # one of 0
    w.put(0, 2), w.put(0, 1), w.put(0, 1), w.put(1, 2)  # two of 0
    w.put(0, 2), w.put(0, 1), w.put(0, 1), w.put(2, 2), w.put(3, 4), w.put(2, 2)   # 15 of 2
    w.put(0, 2), w.put(0, 1), w.put(0, 1), w.put(3, 2), w.put(1, 8), w.put(1, 2)   # 30 of 1
    w.put(0, 8)                                         # the end
    assert dvbsub._string(dvbsub._Bits(w.data()), 2, dvbsub._escape_2bit) == [
        (1, 1), (3, 5), (0, 1), (0, 2), (2, 15), (1, 30)]
    w = sk._BitWriter()
    w.put(7, 4)
    w.put(0, 4), w.put(0, 1), w.put(3, 3)               # 5 of 0
    w.put(0, 4), w.put(0b10, 2), w.put(2, 2), w.put(9, 4)   # 6 of 9
    w.put(0, 4), w.put(0b11, 2), w.put(1, 2)            # two of 0
    w.put(0, 4), w.put(0b11, 2), w.put(2, 2), w.put(1, 4), w.put(4, 4)   # 10 of 4
    w.put(0, 8)
    assert dvbsub._string(dvbsub._Bits(w.data()), 4, dvbsub._escape_4bit) == [
        (7, 1), (0, 5), (9, 6), (0, 2), (4, 10)]
    w = sk._BitWriter()
    w.put(200, 8)
    w.put(0, 8), w.put(0, 1), w.put(9, 7)               # 9 of 0
    w.put(0, 8), w.put(1, 1), w.put(40, 7), w.put(17, 8)    # 40 of 17
    w.put(0, 16)
    assert dvbsub._string(dvbsub._Bits(w.data()), 8, dvbsub._escape_8bit) == [
        (200, 1), (0, 9), (17, 40)]


def test_two_bit_data_in_a_four_bit_region():
    """The default map, then a map table that sends code 1 to 0 (transparent)."""
    w = sk._BitWriter()
    w.put(1, 2), w.put(1, 2), w.put(0, 8)
    two = bytes([0x10]) + w.data()
    dec = DvbSubDecoder()
    region = _rcs(0, 4, 1, objects=[(1, 0, 0)])
    dec.feed(_pes(_pcs([(0, 0, 0)], state=2), region, _ods(1, two), EDS))
    assert bytes(dec.regions[0].pixels) == bytes([7, 7, 0, 0])
    table = bytes([0x20, 0x00, 0x00])                   # 2_to_4: all to 0
    page = dec.feed(_pes(_pcs([(0, 0, 0)]), _rcs(0, 4, 1, objects=[(1, 0, 0)], fill=0),
                         _ods(1, table + two), EDS))
    assert page.pixels == 0


def test_transparent_entries_of_the_clut():
    dec = DvbSubDecoder()
    line = sk.dvbsub_line([(1, 8), (2, 8)])
    # CLUT 0: entry 1 of the 4-bit CLUT fully transparent (T 0xff), entry 2 Y 0
    clut = _seg(dvbsub.CLUT, [0, 0, 1, 0x41, 0x80, 0x80, 0x80, 0xff,
                              2, 0x41, 0x00, 0x80, 0x80, 0x00])
    page = dec.feed(_pes(_pcs([(0, 0, 0)], state=2), _rcs(0, 16, 1, objects=[(1, 0, 0)]),
                         clut, _ods(1, line), EDS))
    assert page.pixels == 0 and page.box is None and page.regions == 1


def test_a_filled_region_shows():
    """A region filled with an opaque colour (a box behind the text) shows."""
    dec = DvbSubDecoder()
    page = dec.feed(_pes(_pcs([(0, 10, 10)], state=2), _rcs(0, 20, 4, fill=8), EDS))
    assert page.pixels == 80 and page.box == (10, 10, 20, 4)


def test_a_new_epoch_forgets_the_regions():
    dec = DvbSubDecoder()
    dec.feed(sk.dvbsub_display_set((0, 0, 10, 2)))
    page = dec.feed(_pes(_pcs([(0, 0, 0)], state=0), EDS))     # the region is kept
    assert page.pixels == 20
    page = dec.feed(_pes(_pcs([(0, 0, 0)], state=2), EDS))     # mode change: gone
    assert page.regions == 0 and page.pixels == 0


def test_the_first_composition_page_is_taken():
    """Two languages on one PID: the page that composed first is taken,
    the other's compositions are not; object data of other pages (the
    ancillary page) is."""
    dec = DvbSubDecoder()
    other = sk.dvbsub_display_set((300, 300, 50, 2), page=2)
    page = dec.feed(sk.dvbsub_display_set((0, 0, 10, 2), page=1))
    assert dec.page_id == 1
    assert dec.feed(other) is None
    line = sk.dvbsub_line([(1, 10)])
    page = dec.feed(_pes(_pcs([(0, 0, 0)]), _rcs(0, 10, 2, objects=[(5, 0, 0)], fill=0),
                         _seg(dvbsub.ODS, bytes([0, 5, 0, 0, len(line), 0, 0]) + line, page=7),
                         EDS))
    assert page.pixels == 20 and page.box == (0, 0, 10, 2)


def test_display_size_and_characters():
    dec = DvbSubDecoder()
    dds = _seg(dvbsub.DDS, [0x00, 0x07, 0x7f, 0x04, 0x37])         # 1920x1080
    chars = _seg(dvbsub.ODS, bytes([0, 1, 0x04, 2, 0, ord("H"), 0, ord("i")]))
    page = dec.feed(_pes(dds, _pcs([(0, 0, 0)], state=2), _rcs(0, 100, 20, objects=[(1, 0, 0)]),
                         chars, EDS))
    assert page.display == (1920, 1080) and page.text == "Hi"
    assert page.describe() == "'Hi' (1 region)"


def test_broken_data_is_skipped():
    dec = DvbSubDecoder()
    assert dec.feed(b"") is None and dec.feed(b"\x00\x00\x0f") is None
    assert dec.feed(_pes(_seg(dvbsub.PCS, [5]), _seg(dvbsub.RCS, [0, 0]), EDS)).regions == 0
    page = dec.feed(_pes(_pcs([(0, 0, 0)], state=2), _rcs(0, 8, 1, objects=[(1, 0, 0)]),
                         _ods(1, bytes([0x11, 0x70, 0x00, 0x33])), EDS))   # stops at 0x33
    assert page.pixels == 1


# -- the renderer -------------------------------------------------------------------------

PID = Track(SUBTITLE, sr.DVB_SUB, origin="pid:0x42")


def test_renderer_decodes_in_its_own_time():
    got = []
    r = sr.DvbSubRenderer(lambda page, t: got.append((page, t)), thread=False)
    assert r.can(PID) and not r.can(Track(SUBTITLE, sr.TELETEXT, origin="ttx:150"))
    r.feed(sk.dvbsub_display_set((0, 0, 4, 2)))                 # off: takes nothing in
    assert r.state().data == 0
    r.show(PID)
    r.feed(sk.dvbsub_display_set((0, 0, 4, 2)), 1)
    r.feed(sk.dvbsub_display_set(None, version=1, state=0), 2)
    assert got == []
    r.decode()
    assert [(p.describe(), t) for p, t in got] == [("4x2 at 0,0 (1 region)", 1), ("empty", 2)]
    st = r.state()
    assert st.on and st.data == 2 and st.decoded == 1 and st.last == "empty"
    assert st.track == "pid:0x42"
    r.off()
    assert got[-1] == (None, None) and not r.state().on


def test_renderer_queue_drops_the_oldest():
    r = sr.DvbSubRenderer(lambda page, t: None, queue_buffers=3, thread=False)
    r.show(PID)
    for _ in range(5):
        r.feed(sk.dvbsub_display_set((0, 0, 4, 2)))
    assert r.state().data == 5 and r.state().dropped == 2


def test_dvbsub_shadow_report():
    sh = sr.DvbSubShadow("pid:0x42")
    assert sh.report(sr.State(sr.DVB_SUB, on=True)) is None
    st = sr.State(sr.DVB_SUB, on=True, data=10)
    assert sh.report(st) == ("DVB-SUB renderer (shadow, pid:0x42): 0 subtitle pages shown, "
                             "0 cleared; data 10, dropped 0; the subtitle data showed nothing")
    for i in range(4):
        sh.saw_page(Page(regions=1, pixels=10, box=(0, 0, 10 + i, 1)), i)
        sh.saw_page(Page(), i + 0.5)
    sh.saw_page(None, None)
    assert sh.report(st) == ("DVB-SUB renderer (shadow, pid:0x42): 4 subtitle pages shown, "
                             "4 cleared; data 10, dropped 0; first '10x1 at 0,0 (1 region)', "
                             "'11x1 at 0,0 (1 region)', '12x1 at 0,0 (1 region)', "
                             "last '13x1 at 0,0 (1 region)'")


def test_renderer_decodes_on_its_own_thread():
    """Not the main loop, not the streaming thread that feeds it."""
    import threading
    import time
    got = []
    r = sr.DvbSubRenderer(lambda page, t: got.append((page, threading.current_thread().name)))
    r.show(PID)
    r.feed(sk.dvbsub_display_set((0, 0, 4, 2)), 1)
    end = time.monotonic() + 5
    while not got and time.monotonic() < end:
        time.sleep(0.01)
    assert got and got[0][1] == "lindvb-dvbsub" and r.state().decoded == 1
    r.off()
    end = time.monotonic() + 5
    while any(t.name == "lindvb-dvbsub" for t in threading.enumerate()) and time.monotonic() < end:
        time.sleep(0.01)
    assert not any(t.name == "lindvb-dvbsub" for t in threading.enumerate())     # it ended
