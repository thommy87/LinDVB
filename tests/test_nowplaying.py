"""Tests for RDS RadioText, song parsing, cover search hits and logo lookup (no GTK)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb.nowplaying import (RadioTextDecoder, ancillary_tail, find_logo,  # noqa: E402
                               pick_itunes_result, rds_to_str, song_from, split_song)


def _uecp_rt(text_bytes, mec=0x0A):
    """A UECP RadioText frame with byte stuffing, as sent by the station."""
    mel = len(text_bytes) + 1
    body = [0x00, 0x00, 0x00, mel + 4, mec, 0x00, 0x00, mel, 0x01] + list(text_bytes) + [0x12, 0x34]
    stuffed = []
    for b in body:
        if b in (0xFD, 0xFE, 0xFF):
            stuffed += [0xFD, b - 0xFD]
        else:
            stuffed.append(b)
    return [0xFE] + stuffed + [0xFF]


def _pes_payloads(frame, chunk=20):
    """Split the frame over several PES packets, reversed, with 0xFD at the end."""
    out = []
    for i in range(0, len(frame), chunk):
        part = frame[i:i + chunk]
        out.append(bytes([0x55] * 100) + bytes(reversed(part)) + bytes([len(part), 0xFD]))
    return out


def test_radiotext_over_several_packets():
    dec = RadioTextDecoder()
    got = [dec.feed_tail(p) for p in _pes_payloads(_uecp_rt(b"Queen - Bohemian Rhapsody   "))]
    assert got[-1] == "Queen - Bohemian Rhapsody"
    assert all(g is None for g in got[:-1])


def test_radiotext_charset_and_stuffing():
    # 0x91 = ä, 0x99 = ü in the RDS character set; 0xFE inside is stuffed
    text = bytes([0x4D, 0x91, 0x64, 0x63, 0x68, 0x65, 0x6E, 0x20, 0x99, 0x62, 0x65, 0x72])
    dec = RadioTextDecoder()
    got = [dec.feed_tail(p) for p in _pes_payloads(_uecp_rt(text), chunk=7)]
    assert got[-1] == "Mädchen über"
    assert rds_to_str(bytes([0xD7, 0x8D])) == "Öß"


def test_radiotext_ignores_other_messages_and_repeats():
    dec = RadioTextDecoder()
    for p in _pes_payloads(_uecp_rt(b"Programme Service", mec=0x02)):
        assert dec.feed_tail(p) is None
    frames = _pes_payloads(_uecp_rt(b"Artist - Song"))
    assert [dec.feed_tail(p) for p in frames][-1] == "Artist - Song"
    assert [dec.feed_tail(p) for p in frames][-1] is None      # same text again
    assert dec.feed_tail(bytes(200)) is None                    # no RDS at all


def test_ancillary_tail_reads_only_the_end():
    payload = bytes(range(50)) + bytes([9, 8, 7, 3, 0xFD])
    calls = []

    def extract(off, n):
        calls.append((off, n))
        return payload[off:off + n]
    assert ancillary_tail(len(payload), extract) == bytes([9, 8, 7, 3, 0xFD])
    assert sum(n for _o, n in calls) < 10
    assert ancillary_tail(10, lambda o, n: bytes(n)) is None


def test_split_song():
    assert split_song("Within Temptation - All I Need") == ("Within Temptation", "All I Need")
    assert split_song("Jetzt läuft: Adele – Hello") == ("Adele", "Hello")
    assert split_song("Hello von Adele") == ("Adele", "Hello")
    assert split_song("Die Morningshow") == ("", "Die Morningshow")
    assert split_song("") == ("", "")


def test_song_from_sources():
    s = song_from({"title": "Muse - Uprising"})                       # ICY
    assert (s["artist"], s["title"]) == ("Muse", "Uprising")
    s = song_from({"radiotext": "Muse - Uprising"})                   # DVB RDS
    assert (s["artist"], s["title"]) == ("Muse", "Uprising")
    s = song_from({"title": "All I Need", "artist": "Within Temptation",   # file tags
                   "album": "All I Need (maxi)", "year": 2007, "track": 1})
    assert s == {"artist": "Within Temptation", "title": "All I Need",
                 "album": "All I Need (maxi)", "year": "2007", "track": 1}


def test_pick_itunes_result():
    results = [
        {"artistName": "Some Cover Band", "trackName": "Uprising", "artworkUrl100": "x"},
        {"artistName": "Muse", "trackName": "Uprising", "collectionName": "The Resistance",
         "releaseDate": "2009-09-07T07:00:00Z", "trackNumber": 1,
         "artworkUrl100": "https://a/100x100bb.jpg"},
    ]
    got = pick_itunes_result(results, "MUSE", "Uprising")
    assert got == {"album": "The Resistance", "year": "2009", "track": 1,
                   "artwork": "https://a/600x600bb.jpg"}
    assert pick_itunes_result(results, "Adele", "Hello") is None


def test_find_logo(tmp_path):
    (tmp_path / "Bayern 3.PNG").write_bytes(b"x")
    (tmp_path / "Das Erste.svg").write_bytes(b"x")
    (tmp_path / "readme.txt").write_bytes(b"x")
    assert find_logo("bayern 3", [str(tmp_path)]).endswith("Bayern 3.PNG")
    assert find_logo("Das Erste HD", [str(tmp_path)]).endswith("Das Erste.svg")
    assert find_logo("readme", [str(tmp_path)]) is None
    assert find_logo("Bayern 3", ["/nonexistent"]) is None
