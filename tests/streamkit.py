# SPDX-License-Identifier: GPL-2.0-or-later
"""Stream samples for the regression net (tests/test_regression_net.py).

Each sample stands for one shape of stream seen on air; the channel it was
seen on is named, so a failing test says which broadcast it is about. They
are made on the fly with GStreamer (a few seconds of test picture and tone)
and served by a local HTTP server, so the tests need no network and no
files of their own in the repository:

* ``hls_redundant``: 3sat-like HLS: the same variant twice (two CDN copies),
  each with its own rendition groups, so every audio and subtitle rendition
  is listed twice in the master playlist; the variant carries German sound
  itself, "Klare Sprache" and "Audio-Deskription" are renditions of their own.
* ``hls_vtt_without_timestamps``: PBS-like HLS: a WebVTT rendition whose
  segments carry no X-TIMESTAMP-MAP and no cues.
* ``live_hls``: a live HLS stream (sliding playlist, no ENDLIST) whose server
  can be told to stop answering after a while, as zdf-hls-18 did for 3sat.
* ``make_captioned_segments``: H.264 with CEA-708 captions (service 1) in
  the picture's user data, as US stations send them (WJXT, PBS on air).
* ``dash_with_roles``: the DASH sample of tests/data/dash with Role and
  Accessibility on its audio, as ARD's DVB-I streams have them (ONE HD).
* ``make_teletext_ts``: H.264 with teletext subtitles on page 150 and the
  teletext descriptor in the PMT, as German broadcasters send them (Das
  Erste, ZDF on satellite, cable and antenna).
* ``make_dvbsub_ts``: H.264 with DVB subtitles (bitmaps, one PID per
  language) and the subtitling descriptor in the PMT, as broadcasters
  across Europe send them.

``Server`` serves a folder; its ``stall_after`` makes every request after
that many seconds hang until the server shuts down, its ``refuse`` answers
every request with that HTTP status (403: a CDN that does not serve us).
``redirect`` maps paths to the address they redirect to, and
``refuse_first`` maps paths to how many of their first requests are
answered with 403 (ARD's DVB-I gateway and CDN).
"""

import http.server
import shutil
import threading
import time
from pathlib import Path

import pytest

DATA = Path(__file__).with_name("data")


def gst(*elements):
    """(GLib, Gst), or skip the test when GStreamer or an element is missing."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    missing = [e for e in elements if Gst.ElementFactory.find(e) is None]
    if missing:
        pytest.skip("GStreamer elements missing: " + ", ".join(missing))
    return GLib, Gst


def _pipeline(desc, timeout=120, feed=None):
    """Run a gst-launch style pipeline to its end, in this process (CI has
    no gst-launch-1.0). *feed(pipeline)* pushes data into appsrcs first."""
    _GLib, Gst = gst()
    pl = Gst.parse_launch(desc)
    if feed is not None:
        feed(pl)
    pl.set_state(Gst.State.PLAYING)
    msg = pl.get_bus().timed_pop_filtered(timeout * Gst.SECOND,
                                          Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pl.set_state(Gst.State.NULL)
    if msg is None or msg.type != Gst.MessageType.EOS:
        raise RuntimeError(f"stream sample not made: {msg and msg.parse_error()}")


def _hls(folder):
    return (f"hlssink location={folder}/seg%05d.ts playlist-location={folder}/index.m3u8 "
            "target-duration=1 max-files=0 playlist-length=0")


def _video(seconds, width, height, pattern):
    return (f"videotestsrc num-buffers={seconds * 25} pattern={pattern} ! "
            f"video/x-raw,width={width},height={height},framerate=25/1,format=I420")


def _audio(seconds, freq=440):
    return (f"audiotestsrc freq={freq} num-buffers={seconds * 47} ! "
            "audio/x-raw,rate=48000,channels=2 ! avenc_aac ! aacparse")


def make_av_segments(folder, seconds=6, width=320, height=180, pattern="ball"):
    """H.264 + AAC in TS segments of 1 s and a VOD playlist (index.m3u8)."""
    gst("videotestsrc", "x264enc", "audiotestsrc", "avenc_aac", "mpegtsmux", "hlssink")
    folder.mkdir(parents=True, exist_ok=True)
    _pipeline(f"{_video(seconds, width, height, pattern)} ! "
              "x264enc key-int-max=25 speed-preset=ultrafast ! h264parse ! queue ! mux. "
              f"{_audio(seconds)} ! queue ! mux. mpegtsmux name=mux ! {_hls(folder)}")
    return folder


def make_audio_segments(folder, seconds=6, freq=440):
    """An audio-only HLS rendition: AAC in TS segments of 1 s."""
    gst("audiotestsrc", "avenc_aac", "mpegtsmux", "hlssink")
    folder.mkdir(parents=True, exist_ok=True)
    _pipeline(f"{_audio(seconds, freq)} ! mpegtsmux ! {_hls(folder)}")
    return folder


def make_vtt(folder, seconds=6, timestamps=True, text="Untertitel"):
    """WebVTT segments of 1 s. With *timestamps* they carry X-TIMESTAMP-MAP
    for mpegtsmux's start (3600 s) and one cue each, as ARD's do; without,
    they are empty, as PBS's are."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(seconds):
        body = "WEBVTT\n"
        if timestamps:
            body += ("X-TIMESTAMP-MAP=MPEGTS:324000000,LOCAL:00:00:00.000\n\n"
                     f"00:00:{i:02d}.000 --> 00:00:{i:02d}.800\n{text} {i}\n")
        (folder / f"s{i:05d}.vtt").write_text(body + "\n")
        lines += ["#EXTINF:1.0,", f"s{i:05d}.vtt"]
    (folder / "index.m3u8").write_text("\n".join(lines + ["#EXT-X-ENDLIST", ""]))
    return folder


def _dtvcc_packet(seq, data, service=1):
    """One DTVCC packet (CEA-708 5) with one service block."""
    body = bytes([service << 5 | len(data)]) + bytes(data)
    size = len(body) + 1
    size += size % 2
    pkt = bytes([(seq & 3) << 6 | size // 2]) + body
    return pkt + b"\0" * (size - len(pkt))


def _odd_parity(b):
    b &= 0x7f
    return b | (0x80 if bin(b).count("1") % 2 == 0 else 0)


def cc608_pairs(text):
    """CEA-608 byte pairs (CC1, roll-up 2) that put *text* on a new bottom
    row: RU2, CR and the row 15 address, each sent twice, then the text."""
    pairs = [(0x14, 0x25)] * 2 + [(0x14, 0x2d)] * 2 + [(0x14, 0x60)] * 2
    text += "\0" * (len(text) % 2)
    pairs += [(ord(text[i]), ord(text[i + 1])) for i in range(0, len(text), 2)]
    return pairs


def caption_frames(seconds, fps=25, text="LinDVB 708", per_frame=20, cc608=None):
    """cc_data of each frame: one line of text a second on service 1, each
    time with its window (CEA-708 DefineWindow, text, CR, DisplayWindows),
    so that every segment shows captions on its own. A DTVCC packet has to
    come within one frame for cc708overlay. With *cc608*, CC1 carries the
    line "<cc608> <second>" too (one pair a frame, in field 1)."""
    window = [0x98, 0x38, 0x0A, 0x00, 0x01, 0x1F, 0x09]    # DF0: visible, 2 rows, 32 columns
    frames = []
    dtvcc = per_frame - (1 if cc608 else 0)
    for sec in range(seconds):
        pkt = _dtvcc_packet(sec, window +
                            list(f"{text} {sec}".encode()) + [0x0D, 0x89, 0x01])
        trip = []
        for i in range(0, len(pkt), 2):
            trip += [0xFF if i == 0 else 0xFE, pkt[i], pkt[i + 1]]   # packet start, data
        chunks = [trip[i:i + dtvcc * 3] for i in range(0, len(trip), dtvcc * 3)]
        line21 = cc608_pairs(f"{cc608} {sec}") if cc608 else []
        for f in range(fps):
            t = chunks[f] if f < len(chunks) else []
            head = []
            if cc608:
                b1, b2 = line21[f] if f < len(line21) else (0, 0)
                head = [0xFC, _odd_parity(b1), _odd_parity(b2)]
            frames.append(bytes(head + t + [0xFA, 0, 0] * (dtvcc - len(t) // 3)))   # padding
    return frames


def make_captioned_segments(folder, seconds=6, width=320, height=180, pattern="ball",
                            cc608=None):
    """As make_av_segments, with CEA-708 captions in the H.264 stream (and
    CEA-608 on CC1 with *cc608*, see caption_frames)."""
    _GLib, Gst = gst("cccombiner", "x264enc", "avenc_aac", "mpegtsmux", "hlssink")
    folder.mkdir(parents=True, exist_ok=True)

    def feed(pl):
        src = pl.get_by_name("cc")
        for i, data in enumerate(caption_frames(seconds, cc608=cc608)):
            buf = Gst.Buffer.new_wrapped(data)
            buf.pts, buf.duration = i * Gst.SECOND // 25, Gst.SECOND // 25
            src.emit("push-buffer", buf)
        src.emit("end-of-stream")
    _pipeline(f"{_video(seconds, width, height, pattern)} ! cccombiner name=c ! "
              "x264enc key-int-max=25 speed-preset=ultrafast ! h264parse ! queue ! mux. "
              "appsrc name=cc format=time caps=closedcaption/x-cea-708,format=cc_data,framerate=25/1 "
              f"! c.caption {_audio(seconds)} ! queue ! mux. mpegtsmux name=mux ! {_hls(folder)}",
              feed=feed)
    return folder



# -- teletext (EN 300 706, carried as in EN 300 472) ----------------------------------

_HAM84 = (0x15, 0x02, 0x49, 0x5E, 0x64, 0x73, 0x38, 0x2F,
          0xD0, 0xC7, 0x8C, 0x9B, 0xA1, 0xB6, 0xFD, 0xEA)


def _reverse(b):
    return int(f"{b:08b}"[::-1], 2)


def _odd(c):
    c &= 0x7F
    return c | (0x80 if bin(c).count("1") % 2 == 0 else 0)


def _ttx_unit(mag, row, data40):
    """One EBU teletext data unit (46 bytes) of packet *row* of magazine *mag*."""
    addr = [_HAM84[(mag & 7) | ((row & 1) << 3)], _HAM84[row >> 1]]
    return bytes([0x03, 0x2C, 0xE7, 0xE4] + [_reverse(b) for b in addr + list(data40)])


def _ttx_page(text, page):
    """A PES payload with a subtitle page: header (C6 subtitle) and row 22."""
    mag, units = page // 0x100 % 8, page & 0xFF
    control = [_HAM84[units & 0xF], _HAM84[units >> 4], _HAM84[0], _HAM84[0x8],
               _HAM84[0], _HAM84[0x8], _HAM84[0x3], _HAM84[0]]
    header = _ttx_unit(mag, 0, control + [_odd(ord(c)) for c in "LinDVB".ljust(32)])
    chars = ([0x0B, 0x0B] + [ord(c) for c in text] + [0x0A, 0x0A] + [0x20] * 40)[:40]
    data = bytes([0x10]) + header + _ttx_unit(mag, 22, [_odd(c) for c in chars])
    while len(data) < 1 + 46 * 3:                     # stuffing units
        data += bytes([0xFF, 0x2C]) + b"\xff" * 44
    return data


def _crc32(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def _pmt_section(program, streams):
    """A PMT section for *streams* [(stream type, pid, descriptors)], PCR on the first."""
    pcr = streams[0][1]
    body = bytes([program >> 8, program & 0xFF, 0xC1, 0, 0, 0xE0 | (pcr >> 8), pcr & 0xFF,
                  0xF0, 0])
    for stype, pid, desc in streams:
        body += bytes([stype, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, len(desc)]) + desc
    sec = bytes([0x02, 0xB0 | ((len(body) + 4) >> 8), (len(body) + 4) & 0xFF]) + body
    return sec + _crc32(sec).to_bytes(4, "big")


def _payload_start(packet):
    """Where the payload of a TS packet starts, after its adaptation field."""
    return 4 + (1 + packet[4] if packet[3] & 0x20 else 0)


def _set_pmt(data, streams):
    """Write a PMT for *streams* into the TS *data* (a bytearray), in place:
    mpegtsmux names a teletext stream without the teletext descriptor."""
    pmt_pid = program = None
    for i in range(0, len(data), 188):
        p = data[i:i + 188]
        pid, start = ((p[1] & 0x1F) << 8) | p[2], _payload_start(p)
        if not p[1] & 0x40:
            continue
        if pid == 0:
            off = start + 1 + p[start]
            program = (p[off + 8] << 8) | p[off + 9]
            pmt_pid = ((p[off + 10] & 0x1F) << 8) | p[off + 11]
        elif pid == pmt_pid:
            payload = bytes([0]) + _pmt_section(program, streams)
            data[i + start:i + 188] = payload + b"\xff" * (188 - start - len(payload))


def make_teletext_ts(path, seconds=10, page=0x150, text="Untertitel"):
    """A TS file of *seconds*: H.264 on PID 0x41 and teletext on 0x42 whose
    subtitle page *page* shows "<text> 0", "<text> 1" ... one a second."""
    _GLib, Gst = gst("videotestsrc", "x264enc", "mpegtsmux")
    path.parent.mkdir(parents=True, exist_ok=True)

    def feed(pl):
        src = pl.get_by_name("ttx")
        for i in range(seconds):
            buf = Gst.Buffer.new_wrapped(_ttx_page(f"{text} {i}", page))
            buf.pts, buf.duration = i * Gst.SECOND, Gst.SECOND
            src.emit("push-buffer", buf)
        src.emit("end-of-stream")
    _pipeline(f"{_video(seconds, 320, 180, 'ball')} ! "
              "x264enc key-int-max=25 speed-preset=ultrafast ! h264parse ! queue ! mux.sink_65 "
              "appsrc name=ttx format=time caps=application/x-teletext ! queue ! mux.sink_66 "
              f"mpegtsmux name=mux ! filesink location={path}", feed=feed)
    data = bytearray(path.read_bytes())
    mag = page // 0x100 % 8
    teletext = bytes([0x56, 5]) + b"deu" + bytes([(2 << 3) | mag, page & 0xFF])
    _set_pmt(data, [(0x1B, 0x41, b""), (0x06, 0x42, teletext)])
    path.write_bytes(bytes(data))
    return path

# -- DVB subtitles (EN 300 743) -----------------------------------------------------

class _BitWriter:
    def __init__(self):
        self.bits = []

    def put(self, value, n):
        self.bits += [(value >> (n - 1 - k)) & 1 for k in range(n)]

    def data(self):
        bits = self.bits + [0] * (-len(self.bits) % 8)
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


def dvbsub_line(runs):
    """One line of 4-bit pixel data: [(code, run)], each run 1 or 3 and more."""
    w = _BitWriter()
    for code, n in runs:
        while n:
            if n >= 25:
                k = min(n, 280)
                w.put(0, 4), w.put(0b1111, 4), w.put(k - 25, 8), w.put(code, 4)
            elif n >= 9:
                k = n
                w.put(0, 4), w.put(0b1110, 4), w.put(k - 9, 4), w.put(code, 4)
            else:
                k = 1
                if code:
                    w.put(code, 4)
                else:
                    w.put(0, 4), w.put(0b1100, 4)               # one pixel of 0
            n -= k
    w.put(0, 4), w.put(0, 4)                                    # end of the string
    return bytes([0x11]) + w.data() + bytes([0xf0])


def _segment(kind, page, data):
    return bytes([0x0f, kind, page >> 8, page & 0xff, len(data) >> 8, len(data) & 0xff]) + data


def dvbsub_display_set(box=None, page=1, version=0, state=2, timeout=10, code=1):
    """The PES data of one display set of *page*: with *box* (x, y, w, h),
    one region there holding one object, a bar of *code* (CLUT 0 default:
    not transparent); without, a page that shows nothing."""
    pcs = bytes([timeout, (version & 0x0f) << 4 | (state & 3) << 2])
    out = b""
    if box:
        x, y, w, h = box
        pcs += bytes([0, 0, x >> 8, x & 0xff, y >> 8, y & 0xff])
        rcs = bytes([0, (version & 0x0f) << 4 | 0x08, w >> 8, w & 0xff, h >> 8, h & 0xff,
                     2 << 5 | 2 << 2, 0, 0, 0]) + bytes([0, 1, 0, 0, 0, 0])
        line = dvbsub_line([(code, w)])
        top = line * ((h + 1) // 2)
        bottom = line * (h // 2)
        ods = bytes([0, 1, (version & 0x0f) << 4, len(top) >> 8, len(top) & 0xff,
                     len(bottom) >> 8, len(bottom) & 0xff]) + top + bottom
        out = _segment(0x10, page, pcs) + _segment(0x11, page, rcs) + _segment(0x13, page, ods)
    else:
        out = _segment(0x10, page, pcs)
    return bytes([0x20, 0x00]) + out + _segment(0x80, page, b"") + bytes([0xff])


def make_dvbsub_ts(path, seconds=10, page=1, languages=("deu",)):
    """A TS file of *seconds*: H.264 on PID 0x41 and DVB subtitles, page
    *page*, one PID from 0x42 on for each of *languages*: each second a bar
    (the second language's 200 pixels further right), every other one lower
    down, shown half a second, then an empty page."""
    _GLib, Gst = gst("videotestsrc", "x264enc", "mpegtsmux")
    path.parent.mkdir(parents=True, exist_ok=True)

    def feed(pl):
        for n in range(len(languages)):
            src = pl.get_by_name(f"sub{n}")
            for i in range(seconds):
                box = (100 + 200 * n, 400 + 40 * (i % 2), 300 + 10 * i, 30)
                for k, data in enumerate((dvbsub_display_set(box, page, version=2 * i),
                                          dvbsub_display_set(None, page, version=2 * i + 1,
                                                             state=0))):
                    buf = Gst.Buffer.new_wrapped(data)
                    buf.pts = i * Gst.SECOND + k * Gst.SECOND // 2
                    buf.duration = Gst.SECOND // 2
                    src.emit("push-buffer", buf)
            src.emit("end-of-stream")
    subs = " ".join(f"appsrc name=sub{n} format=time caps=subpicture/x-dvb ! queue ! "
                    f"mux.sink_{66 + n}" for n in range(len(languages)))
    _pipeline(f"{_video(seconds, 720, 576, 'ball')} ! "
              "x264enc key-int-max=25 speed-preset=ultrafast ! h264parse ! queue ! mux.sink_65 "
              f"{subs} mpegtsmux name=mux ! filesink location={path}", feed=feed)
    data = bytearray(path.read_bytes())
    streams = [(0x1B, 0x41, b"")]
    for n, lang in enumerate(languages):
        streams.append((0x06, 0x42 + n, bytes([0x59, 8]) + lang.encode() +
                        bytes([0x10, page >> 8, page & 0xff, page >> 8, page & 0xff])))
    _set_pmt(data, streams)
    path.write_bytes(bytes(data))
    return path


# 3sat (zdf-hls-18 .../dach/veryhigh/master.m3u8), shortened: two copies of the
# variant, each with its own groups; the German sound of the variant itself
# is the rendition without URI
REDUNDANT_MASTER = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="TV Ton",LANGUAGE="deu",DEFAULT=YES,AUTOSELECT=YES
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="Klare Sprache",LANGUAGE="mul",DEFAULT=NO,AUTOSELECT=NO,URI="klar/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="Audio-Deskription",LANGUAGE="deu",CHARACTERISTICS="public.accessibility.describes-video",DEFAULT=NO,AUTOSELECT=NO,URI="ad/index.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s1",NAME="Untertitel deutsch",LANGUAGE="deu",DEFAULT=NO,AUTOSELECT=YES,URI="subs/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="TV Ton",LANGUAGE="deu",DEFAULT=YES,AUTOSELECT=YES
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="Klare Sprache",LANGUAGE="mul",DEFAULT=NO,AUTOSELECT=NO,URI="klar/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="Audio-Deskription",LANGUAGE="deu",CHARACTERISTICS="public.accessibility.describes-video",DEFAULT=NO,AUTOSELECT=NO,URI="ad/index.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s2",NAME="Untertitel deutsch",LANGUAGE="deu",DEFAULT=NO,AUTOSELECT=YES,URI="subs/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x180,FRAME-RATE=25.000,CODECS="avc1.42c01e,mp4a.40.2",AUDIO="a1",SUBTITLES="s1"
main/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x180,FRAME-RATE=25.000,CODECS="avc1.42c01e,mp4a.40.2",AUDIO="a2",SUBTITLES="s2"
main/index.m3u8
"""

# 3sat as it is on air now (zdf-hls-18 .../master.m3u8, 24.11.2026), shortened:
# every sound is a rendition of its own, the variant carries a sound too
# (which the player must ignore), a name with a slash, and the audio
# description shares its language with the main sound; the second variant
# and its groups come from a backup folder (2016501-b)
THREESAT_MASTER = """#EXTM3U
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="TV Ton",LANGUAGE="deu",DEFAULT=YES,URI="tv/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="Klare Sprache / Originalton",LANGUAGE="mul",URI="klar/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="Audio-Deskription",LANGUAGE="deu",URI="ad/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="TV Ton",LANGUAGE="deu",DEFAULT=YES,URI="b/tv/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="Klare Sprache / Originalton",LANGUAGE="mul",URI="b/klar/index.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a2",NAME="Audio-Deskription",LANGUAGE="deu",URI="b/ad/index.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s1",NAME="Untertitel deutsch",CODECS="wvtt",LANGUAGE="deu",DEFAULT=YES,AUTOSELECT=YES,URI="subs/index.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s2",NAME="Untertitel deutsch",CODECS="wvtt",LANGUAGE="deu",DEFAULT=YES,AUTOSELECT=YES,URI="b/subs/index.m3u8"
#EXT-X-STREAM-INF:CODECS="avc1.42c01e,mp4a.40.2",BANDWIDTH=900000,AVERAGE-BANDWIDTH=800000,AUDIO="a1",SUBTITLES="s1",FRAME-RATE=25.000,RESOLUTION=320x180
main/index.m3u8
#EXT-X-STREAM-INF:CODECS="avc1.42c01e,mp4a.40.2",BANDWIDTH=900000,AVERAGE-BANDWIDTH=800000,AUDIO="a2",SUBTITLES="s2",FRAME-RATE=25.000,RESOLUTION=320x180
b/main/index.m3u8
"""

# PBS (wedqdt4.lls.pbs.org), shortened: one variant, an English WebVTT rendition
PBS_MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="eng",DEFAULT=NO,AUTOSELECT=YES,URI="subs/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x180,CODECS="avc1.42c01e,mp4a.40.2",SUBTITLES="subs"
main/index.m3u8
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "streamkit"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.requests.append(self.path)
        if srv.refuse:
            self.send_error(srv.refuse)
            return
        path = self.path.lstrip("/").split("?")[0]
        if path in srv.redirect:
            self.send_response(302)
            self.send_header("Location", srv.redirect[path])
            self.end_headers()
            return
        if srv.refuse_first.get(path, 0) > 0:
            srv.refuse_first[path] -= 1
            self.send_error(403)
            return
        if srv.stall_after is not None and time.monotonic() - srv.started > srv.stall_after:
            srv.stalled.wait()              # hangs until the server shuts down
            return
        live = srv.live.get(self.path.lstrip("/").split("?")[0])
        if live is not None:
            body = live.playlist().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class Server:
    """A folder over HTTP on 127.0.0.1; ``url(path)`` gives an address.
    *live* maps paths to LivePlaylists, served in place of files."""

    def __init__(self, folder, stall_after=None, live=None, refuse=None, redirect=None,
                 refuse_first=None):
        self.folder = Path(folder)

        def handler(*a, **kw):
            return _Handler(*a, directory=str(self.folder), **kw)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.httpd.requests = []
        self.httpd.stall_after = stall_after
        self.httpd.refuse = refuse
        self.httpd.redirect = dict(redirect or {})
        self.httpd.refuse_first = dict(refuse_first or {})
        self.httpd.stalled = threading.Event()
        self.httpd.live = dict(live or {})
        self.httpd.started = time.monotonic()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def requests(self):
        return self.httpd.requests

    def url(self, path):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{path}"

    def close(self):
        self.httpd.stalled.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class LivePlaylist:
    """A sliding window over VOD segments, as a live HLS playlist: one more
    segment every second, *window* segments long, no ENDLIST. *prefix* is
    the segments' folder as seen from the playlist."""

    def __init__(self, segments, prefix="", window=4):
        self.segments = segments
        self.prefix = prefix
        self.window = window
        self.t0 = time.monotonic()

    def playlist(self):
        now = int(time.monotonic() - self.t0)
        last = min(now + self.window, len(self.segments))
        first = max(0, last - self.window)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1",
                 f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for s in self.segments[first:last]:
            lines += ["#EXTINF:1.0,", f"{self.prefix}{s}"]
        return "\n".join(lines) + "\n"


# -- the samples ----------------------------------------------------------------

def hls_redundant(folder, seconds=6):
    make_av_segments(folder / "main", seconds)
    make_audio_segments(folder / "klar", seconds, freq=660)
    make_audio_segments(folder / "ad", seconds, freq=880)
    make_vtt(folder / "subs", seconds)
    (folder / "master.m3u8").write_text(REDUNDANT_MASTER)
    return folder / "master.m3u8"


def hls_3sat(folder, seconds=6):
    make_av_segments(folder / "main", seconds)
    make_audio_segments(folder / "tv", seconds, freq=440)
    make_audio_segments(folder / "klar", seconds, freq=660)
    make_audio_segments(folder / "ad", seconds, freq=880)
    make_vtt(folder / "subs", seconds)
    (folder / "b").mkdir()
    for sub in ("main", "tv", "klar", "ad", "subs"):
        (folder / "b" / sub).symlink_to(folder / sub)
    (folder / "master.m3u8").write_text(THREESAT_MASTER)
    return folder / "master.m3u8"


def hls_vtt_without_timestamps(folder, seconds=6):
    make_av_segments(folder / "main", seconds)
    make_vtt(folder / "subs", seconds, timestamps=False)
    (folder / "master.m3u8").write_text(PBS_MASTER)
    return folder / "master.m3u8"


def hls_vtt(folder, seconds=6):
    """The PBS master ("English") with timed WebVTT segments, a cue each."""
    make_av_segments(folder / "main", seconds)
    make_vtt(folder / "subs", seconds, text="Cue")
    (folder / "master.m3u8").write_text(PBS_MASTER)
    return folder / "master.m3u8"


def _live(folder, pattern):
    return LivePlaylist(sorted(p.name for p in folder.glob(pattern)))


def live_hls(folder, seconds=30):
    """A live stream: serve with Server(folder, live=...) and open
    server.url("main/live.m3u8")."""
    seg = make_av_segments(folder / "main", seconds)
    return {"main/live.m3u8": _live(seg, "seg*.ts")}


def live_hls_vtt_without_timestamps(folder, seconds=30):
    """PBS as it is on air: live, with the empty WebVTT rendition. Serve
    with Server(folder, live=...) and open server.url("master.m3u8")."""
    seg = make_av_segments(folder / "main", seconds)
    subs = make_vtt(folder / "subs", seconds, timestamps=False)
    (folder / "master.m3u8").write_text(
        PBS_MASTER.replace("subs/index.m3u8", "subs/live.m3u8").replace(
            "main/index.m3u8", "main/live.m3u8"))
    return {"main/live.m3u8": _live(seg, "seg*.ts"), "subs/live.m3u8": _live(subs, "s*.vtt")}



def live_hls_vtt(folder, seconds=30):
    """Live, with a WebVTT rendition whose segments carry X-TIMESTAMP-MAP
    and a cue each (as ARD-alpha's); the master as PBS's ("English")."""
    seg = make_av_segments(folder / "main", seconds)
    subs = make_vtt(folder / "subs", seconds, text="Live")
    (folder / "master.m3u8").write_text(
        PBS_MASTER.replace("subs/index.m3u8", "subs/live.m3u8").replace(
            "main/index.m3u8", "main/live.m3u8"))
    return {"main/live.m3u8": _live(seg, "seg*.ts"), "subs/live.m3u8": _live(subs, "s*.vtt")}


DASH_ROLES = {
    # adaptation set id -> what ARD's DVB-I manifests put on it
    "1": ('<Role schemeIdUri="urn:mpeg:dash:role:2011" value="main" />',),
    "2": ('<Role schemeIdUri="urn:mpeg:dash:role:2011" value="description" />',
          '<Accessibility schemeIdUri="urn:tva:metadata:cs:AudioPurposeCS:2007" value="1" />'),
}


def dash_with_roles(folder):
    """The DASH sample (video, German and English AAC, TTML subtitles) with
    the English set turned into an audio description by Role/Accessibility."""
    src = DATA / "dash"
    for p in src.iterdir():
        shutil.copy(p, folder / p.name)
    text = (src / "manifest-subtitles.mpd").read_text()
    for set_id, extra in DASH_ROLES.items():
        marker = f'<AdaptationSet id="{set_id}" '
        start = text.index(marker)
        end = text.index(">", start) + 1
        text = text[:end] + "\n\t\t\t" + "\n\t\t\t".join(extra) + text[end:]
    (folder / "manifest-roles.mpd").write_text(text)
    return folder / "manifest-roles.mpd"


TTML_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:tts="http://www.w3.org/ns/ttml#styling" '
             'xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:timeBase="media" xml:lang="de">'
             '<head><styling><style xml:id="s1" tts:color="#ffffff" tts:backgroundColor="#000000"/>'
             '</styling><layout><region xml:id="bottom" tts:origin="10% 80%" tts:extent="80% 15%"/>'
             '</layout></head><body><div>\n')
TTML_TAIL = "</div></body></tt>\n"


def ttml_cue(i, text="Untertitel"):
    """An EBU-TT-D paragraph shown from second *i* for 0.8 s."""
    t = f"{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}"
    return (f'<p begin="{t}.000" end="{t}.800" '
            f'region="bottom" style="s1">{text} {i}</p>\n')


def make_dash_ttml(folder, seconds=20, single_file=False, text="Untertitel"):
    """A DASH VOD stream (H.264 + AAC in TS segments of 1 s) with a German
    TTML subtitle set: segments of 2 s (DVB-I), or with *single_file* one
    TTML document for the whole stream (HbbTV media libraries). Cue i is
    "Untertitel i" from second i for 0.8 s. Returns the manifest."""
    folder.mkdir(parents=True, exist_ok=True)
    make_av_segments(folder / "av", seconds=seconds)
    if single_file:
        (folder / "subtitles.ttml").write_text(
            TTML_HEAD + "".join(ttml_cue(i, text) for i in range(seconds)) + TTML_TAIL)
        rep = '<Representation id="sub" bandwidth="1000"><BaseURL>subtitles.ttml</BaseURL></Representation>'
    else:
        for n in range(seconds // 2):
            (folder / f"sub{n:05d}.ttml").write_text(
                TTML_HEAD + ttml_cue(2 * n, text) + ttml_cue(2 * n + 1, text) + TTML_TAIL)
        rep = ('<Representation id="sub" bandwidth="1000"><SegmentTemplate '
               'media="sub$Number%05d$.ttml" startNumber="0" duration="2"/></Representation>')
    mpd = folder / ("single.mpd" if single_file else "segments.mpd")
    mpd.write_text(
        '<?xml version="1.0"?>\n<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
        'profiles="urn:mpeg:dash:profile:isoff-main:2011" type="static" '
        f'mediaPresentationDuration="PT{seconds}S" minBufferTime="PT2S"><Period id="p0">'
        '<AdaptationSet id="1" contentType="video" mimeType="video/mp2t">'
        '<Representation id="av" bandwidth="200000" width="320" height="180" codecs="avc1,mp4a.40.2">'
        '<SegmentTemplate media="av/seg$Number%05d$.ts" startNumber="0" duration="1"/>'
        '</Representation></AdaptationSet>'
        '<AdaptationSet id="3" contentType="text" mimeType="application/ttml+xml" lang="deu">'
        f'<Role schemeIdUri="urn:mpeg:dash:role:2011" value="subtitle"/>{rep}</AdaptationSet>'
        '</Period></MPD>\n')
    return mpd
