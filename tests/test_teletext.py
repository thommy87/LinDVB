"""Teletext subtitle pages from the PMT teletext descriptor."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb import teletext  # noqa: E402


def _pmt(es_info):
    """PMT for program 7 with one private stream (PID 0x101) carrying es_info."""
    body = bytes([0x00, 0x07, 0xC1, 0x00, 0x00, 0xE1, 0x00, 0xF0, 0x00])
    body += bytes([0x06, 0xE1, 0x01, 0xF0, len(es_info)]) + es_info
    length = len(body) + 4
    return bytes([0x02, 0xB0, length]) + body + b"\0\0\0\0"


def _entry(lang, ttype, magazine, page_byte):
    return lang.encode() + bytes([(ttype << 3) | magazine, page_byte])


def test_page_number():
    assert teletext.page_number(1, 0x50) == 150
    assert teletext.page_number(7, 0x77) == 777
    assert teletext.page_number(0, 0x88) == 888          # magazine 0 means 8


def test_subtitle_pages():
    entries = (_entry("deu", 0x01, 1, 0x00)               # initial page: not a subtitle
               + _entry("deu", 0x02, 1, 0x50)
               + _entry("eng", 0x05, 7, 0x77))
    program, pages = teletext.subtitle_pages_from_pmt(_pmt(bytes([0x56, len(entries)]) + entries))
    assert program == 7
    assert pages == [teletext.TeletextPage(150, "deu", False),
                     teletext.TeletextPage(777, "eng", True)]
    assert pages[0].label == "Teletext 150 (deu)"
    assert pages[1].label == "Teletext 777 (eng, hearing impaired)"


def test_other_descriptors_ignored():
    dvbsub = bytes([0x59, 0x08]) + b"deu" + bytes([0x10, 0, 1, 0, 1])
    _program, pages = teletext.subtitle_pages_from_pmt(_pmt(dvbsub))
    assert pages == []


def test_duplicates_removed():
    e = _entry("deu", 0x02, 1, 0x50)
    info = bytes([0x56, 5]) + e + bytes([0x56, 5]) + e
    assert len(teletext.subtitle_pages_from_pmt(_pmt(info))[1]) == 1


def test_teletext_in_pmt():
    entries = _entry("deu", 0x01, 1, 0x00)               # initial page only, no subtitles
    program, present = teletext.teletext_in_pmt(_pmt(bytes([0x56, len(entries)]) + entries))
    assert (program, present) == (7, True)
    assert teletext.subtitle_pages_from_pmt(_pmt(bytes([0x56, len(entries)]) + entries))[1] == []


def test_no_teletext_in_pmt():
    assert teletext.teletext_in_pmt(_pmt(bytes([0x0A, 4]) + b"deu\0")) == (7, False)
    assert teletext.teletext_in_pmt(_pmt(b"")) == (7, False)
