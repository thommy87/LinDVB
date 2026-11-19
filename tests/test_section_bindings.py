"""EIT / PAT sections from GStreamer and dvbsrc's signal figures (the DVB-C crash).

A Digital Devices DVB-C card (Sony CXD2837ER) made LinDVB abort right after
tuning with "free(): double free detected in tcache 2": reading the genre of
an EIT event with GstMpegtsDescriptor.parse_dvb_content() from Python frees
its items twice. The sections are now read from their raw bytes.
"""

import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from lindvb import directtune, festats
from lindvb.epg import parse_eit_section
from lindvb.epgsources import mpeg_crc
from tests.test_epgsources import START, _eit, _short

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
    gi.require_version("GstMpegts", "1.0")
    from gi.repository import Gst, GstMpegts
    Gst.init(None)
    GstMpegts.initialize()
except (ImportError, ValueError):
    pytest.skip("GStreamer's MPEG-TS library (GstMpegts) is missing", allow_module_level=True)

from lindvb import tuner  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONTENT = bytes([0x54, 6, 0x10, 0x00, 0x23, 0x00, 0xB1, 0x00])   # movie, show, b/w
EXTENDED = bytes([0x4E, 15, 0x00]) + b"deu" + bytes([0, 9]) + b"Ein Krimi"


def _section(raw, pid=0x12):
    return GstMpegts.Section.new(pid, raw)


def _das_erste_eit():
    return _eit(28106, 1101, [(7, START, 5400, [_short("deu", "Tatort", "Der Fall"),
                                                EXTENDED, CONTENT])], table_id=0x4E)


def test_eit_section_with_genre():
    key, events = parse_eit_section(_section(_das_erste_eit()), GstMpegts, "deu")
    assert key == "1101:28106"
    ev, = events
    assert (ev.title, ev.subtitle, ev.start, ev.duration) == ("Tatort", "Der Fall", START, 5400)
    assert ev.description == "Ein Krimi"
    assert ev.genre and ev.bw


def test_other_sections_are_not_eit():
    pat = _pat(1101, [(0, 0x10), (28106, 0x100)])
    assert parse_eit_section(_section(pat, 0), GstMpegts) is None


def test_eit_sections_do_not_corrupt_the_heap():
    """Many EIT sections with genres, in a process of its own: before the fix
    glibc aborted it (double free) within the first few sections."""
    script = textwrap.dedent(f"""
        import sys
        sys.path[:0] = [{str(ROOT)!r}]
        import gi
        gi.require_version("Gst", "1.0"); gi.require_version("GstMpegts", "1.0")
        from gi.repository import Gst, GstMpegts
        Gst.init(None); GstMpegts.initialize()
        from lindvb.epg import parse_eit_section
        from tests.test_section_bindings import _das_erste_eit
        raw = _das_erste_eit()
        for _ in range(500):
            key, events = parse_eit_section(GstMpegts.Section.new(0x12, raw), GstMpegts, "")
            assert events[0].genre
        print("ok")
    """)
    env = dict(os.environ, PYTHONMALLOC="malloc", MALLOC_CHECK_="3", MALLOC_PERTURB_="165")
    res = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0 and "ok" in res.stdout, res.stderr[-2000:]


def _pat(tsid, programs):
    loop = b"".join(bytes([n >> 8, n & 0xFF, 0xE0 | (pid >> 8), pid & 0xFF])
                    for n, pid in programs)
    length = 5 + len(loop) + 4
    sec = bytes([0x00, 0xB0 | (length >> 8), length & 0xFF, tsid >> 8, tsid & 0xFF,
                 0xC1, 0, 0]) + loop
    return sec + mpeg_crc(sec).to_bytes(4, "big")


def test_pat_programs():
    raw = _pat(1101, [(0, 0x10), (28106, 0x100), (28107, 0x1FFF)])
    assert tuner.pat_programs(raw) == [(0, 0x10), (28106, 0x100), (28107, 0x1FFF)]
    assert tuner.pat_programs(_section(raw, 0).get_data().get_data()) == \
        tuner.pat_programs(raw)
    assert tuner.pat_programs(b"\x42" + raw[1:]) == []
    assert tuner.pat_programs(raw[:6]) == []


# -- dvbsrc's dvb-frontend-stats messages ---------------------------------------

def _stats(lock, signal=0x7FFF0000, snr=-5):
    """A message as dvbsrc posts it; without v3 statistics the figures are garbage."""
    return Gst.Structure.new_from_string(
        f"dvb-frontend-stats, status=(int){0x1F if lock else 0x03}, signal=(int){signal}, "
        f"snr=(int){snr}, ber=(int)-1, unc=(int)987654, lock=(boolean){str(lock).lower()}")


class FakeFrontend:
    def __init__(self, v3=True, available=True):
        self.v3, self.available, self.closed = v3, available, False

    def v3_stats_supported(self):
        return self.v3

    def read(self):
        return {"signal_dbm": -37.0, "cnr_db": 38.0, "post_errors": 0, "post_total": 10 ** 6,
                "post_ber": 0.0, "block_errors": 0, "status": {"lock": True}}

    def close(self):
        self.closed = True


def _capture(monkeypatch, fe, clock):
    monkeypatch.setattr(festats, "Frontend", lambda _a, _f: fe)
    monkeypatch.setattr(tuner.time, "monotonic", lambda: clock[0])
    cap = types.SimpleNamespace(adapter=0, frontend=0, _stats_at=0.0, _stats_lock=None,
                                _v5_fe=None)
    cap._v5_frontend = types.MethodType(tuner.Capture._v5_frontend, cap)
    return lambda s: tuner.Capture._frontend_stats(cap, s), cap


def test_stats_without_v3_come_from_the_v5_statistics(monkeypatch):
    fe = FakeFrontend(v3=False)
    clock = [100.0]
    stats, cap = _capture(monkeypatch, fe, clock)
    st = stats(_stats(True))
    assert st["lock"] is True and st["status"] == 0x1F
    assert st == dict(st, **directtune.dvb_stats(fe.read()), status=0x1F)
    assert st["unc"] == 0 and 0 < st["signal"] <= 65535 and st["snr"] == 65535
    assert cap._v5_fe is fe


def test_stats_with_v3_stay_as_dvbsrc_reports_them(monkeypatch):
    fe = FakeFrontend(v3=True)
    clock = [100.0]
    stats, cap = _capture(monkeypatch, fe, clock)
    st = stats(_stats(True, signal=40000, snr=30000))
    assert (st["signal"], st["snr"], st["unc"]) == (40000, 30000, 987654)
    assert fe.closed and cap._v5_fe is False


def test_stats_storm_is_throttled(monkeypatch):
    """dvbsrc posts dozens of messages within milliseconds while tuning."""
    clock = [100.0]
    stats, _cap = _capture(monkeypatch, FakeFrontend(v3=False), clock)
    assert stats(_stats(False)) is not None
    passed = 0
    for _ in range(30):
        clock[0] += 0.001
        passed += stats(_stats(False)) is not None
    assert passed == 0
    clock[0] += 0.001
    assert stats(_stats(True))["lock"] is True          # the lock comes: at once
    clock[0] += tuner.STATS_INTERVAL
    assert stats(_stats(True)) is not None


def test_v3_stats_supported(monkeypatch):
    fe = festats.Frontend(0, 0, root="/nonexistent")
    assert fe.v3_stats_supported()                       # no device: nothing to say
    fe.fd = 99

    def refuse(errno):
        def ioctl(_fd, _req, _buf):
            raise OSError(errno, os.strerror(errno))
        return ioctl
    monkeypatch.setattr(festats.fcntl, "ioctl", refuse(524))
    assert not fe.v3_stats_supported()
    monkeypatch.setattr(festats.fcntl, "ioctl", refuse(5))   # EIO: a passing hiccup
    assert fe.v3_stats_supported()
    monkeypatch.setattr(festats.fcntl, "ioctl", lambda *_a: 0)
    assert fe.v3_stats_supported()
    fe.fd = None


def _ext(num, last, lang, text):
    body = bytes([(num << 4) | last]) + lang.encode() + bytes([0, len(text)]) + text
    return bytes([0x4E, len(body)]) + body


def test_description_split_over_extended_descriptors():
    """Broadcasters cut the text anywhere, also right next to a space: the
    pieces are joined as sent (as GStreamer's parser did before)."""
    sec = _eit(1, 2, [(1, START, 600, [_short("deu", "Film", ""),
                                       _ext(1, 2, "deu", b"\x05und zwei \xfc"),
                                       _ext(0, 2, "deu", b"\x05Teil eins "),
                                       _ext(2, 2, "deu", b"nd drei  ")])])
    ev, = parse_eit_section(_section(sec), GstMpegts, "deu")[1]
    assert ev.description == "Teil eins und zwei ünd drei"


def test_undefined_start_time_is_skipped():
    sec = bytearray(_eit(1, 2, [(1, START, 600, [_short("deu", "NVOD", "")])]))
    sec[16:21] = b"\xff" * 5                 # start_time of the first event
    sec[-4:] = mpeg_crc(bytes(sec[:-4])).to_bytes(4, "big")
    assert parse_eit_section(_section(bytes(sec)), GstMpegts)[1] == []
