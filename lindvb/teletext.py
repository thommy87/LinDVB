# SPDX-License-Identifier: GPL-2.0-or-later
"""Teletext subtitle pages announced in the PMT (teletext descriptor, EN 300 468)."""

from dataclasses import dataclass

from .ca import parse_pmt

TELETEXT_DESCRIPTOR = 0x56
TYPE_SUBTITLE = 0x02
TYPE_SUBTITLE_HEARING_IMPAIRED = 0x05

# Pages to offer when a stream carries teletext but announces no subtitle page.
DEFAULT_SUBTITLE_PAGES = (150, 777, 888)


@dataclass(frozen=True)
class TeletextPage:
    page: int                # 100..899
    language: str = ""
    hearing_impaired: bool = False

    @property
    def label(self):
        extra = [x for x in (self.language, "hearing impaired" if self.hearing_impaired else "") if x]
        return f"Teletext {self.page}" + (f" ({', '.join(extra)})" if extra else "")


def page_number(magazine, page_byte):
    """Teletext page from the descriptor's 3-bit magazine and BCD page byte."""
    return (magazine or 8) * 100 + (page_byte >> 4) * 10 + (page_byte & 0x0F)


def subtitle_pages_from_descriptors(loop):
    """Subtitle pages in one ES descriptor loop."""
    pages, i = [], 0
    while i + 2 <= len(loop):
        tag, length = loop[i], loop[i + 1]
        body = loop[i + 2:i + 2 + length]
        if tag == TELETEXT_DESCRIPTOR:
            for k in range(0, len(body) - 4, 5):
                lang = body[k:k + 3].decode("latin-1", "replace").strip("\x00 ")
                ttype = body[k + 3] >> 3
                magazine = body[k + 3] & 0x07
                if ttype in (TYPE_SUBTITLE, TYPE_SUBTITLE_HEARING_IMPAIRED):
                    page = page_number(magazine, body[k + 4])
                    if 100 <= page <= 899:
                        pages.append(TeletextPage(page, lang,
                                                  ttype == TYPE_SUBTITLE_HEARING_IMPAIRED))
        i += 2 + length
    return pages


def has_teletext_descriptor(loop):
    """True when an ES descriptor loop announces a teletext stream."""
    i = 0
    while i + 2 <= len(loop):
        if loop[i] == TELETEXT_DESCRIPTOR:
            return True
        i += 2 + loop[i + 1]
    return False


def teletext_in_pmt(section):
    """(program number, True if the service carries a teletext stream)."""
    pmt = parse_pmt(section)
    return pmt.program_number, any(has_teletext_descriptor(info) for *_x, info in pmt.streams)


def subtitle_pages_from_pmt(section):
    """All teletext subtitle pages of the service described by a PMT section."""
    pmt = parse_pmt(section)
    pages = []
    for _stream_type, _pid, _ca, info in pmt.streams:
        for p in subtitle_pages_from_descriptors(info):
            if p not in pages:
                pages.append(p)
    return pmt.program_number, pages
