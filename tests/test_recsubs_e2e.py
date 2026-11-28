# SPDX-License-Identifier: GPL-2.0-or-later
"""Recordings keep the subtitles of the timeshift buffer (stage H): an HLS
channel with a WebVTT rendition is read into a capture with its cues in the
buffer (lindvb.cuebuffer); a TS recording of it gets an .srt file beside it,
an MKV recording a text track, and both show each cue at the picture of
its time."""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import streamkit as sk

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

Gst.init(None)

ELEMENTS = ("videotestsrc", "x264enc", "avenc_aac", "mpegtsmux", "hlssink", "urisourcebin",
            "parsebin", "appsink", "h264parse", "hlsdemux2", "tsdemux", "matroskamux",
            "matroskademux", "aacparse")
SECONDS = 12            # recorded
CLOSE = 0.06            # s a cue may be off its picture (a frame and a half)


def _run(seconds, until=None):
    loop = GLib.MainLoop()
    end = time.monotonic() + seconds

    def tick():
        if time.monotonic() > end or (until is not None and until()):
            loop.quit()
            return False
        return True
    GLib.timeout_add(100, tick)
    loop.run()


class _Tuners(GObject.Object):
    pass


@pytest.fixture
def capture(tmp_path):
    """capture(subtitles_in_buffer) -> the live sample (cue n "Live n" due at
    the segments' second n, for 0.8 s) read into a capture."""
    sk.gst(*ELEMENTS)
    from lindvb import ipstream
    from lindvb.channels import Channel
    from lindvb.config import DEFAULTS
    from lindvb.tuner import Capture
    folder = Path(tempfile.mkdtemp(prefix="lindvb-recsubs-"))
    srv = sk.Server(folder, live=sk.live_hls_vtt(folder, 60))
    made = []

    def make(on):
        settings = dict(DEFAULTS, timeshift_dir=str(tmp_path / "buffer"), subtitles_in_buffer=on)
        ch = Channel(name="Test HLS", delsys="iptv", url=srv.url("master.m3u8"))
        cap = Capture(ch, 901, 0, settings, tuner=ipstream.stream_profile(901, ch), live=True)
        cap.users.add("rec")
        cap.start()
        made.append(cap)
        _run(20, lambda: cap.store.bounds()[1] > 200_000 and (not on or cap.cue_pids))
        return ch, cap
    yield make
    for cap in made:
        cap.destroy()
    srv.close()


def _record(ch, cap, folder, formats):
    """Records *formats* from the same place of the buffer for SECONDS:
    {format: Recording}."""
    from lindvb.recorder import Recording
    _, start = cap.store.bounds()
    recs = {fmt: Recording(folder / f"rec-{fmt}", capture=cap, start_offset=start, fmt=fmt,
                           channel=ch, extras=("subtitles",))
            for fmt in formats}
    _run(SECONDS)
    for rec in recs.values():
        rec.stop()
    _run(20, lambda: all(not r.thread.is_alive() and (r.remux is None or r.remux.finished)
                         for r in recs.values()))
    return recs


def _due(cap, n):
    """The buffer's MPEG-TS time (s) of the segments' second n."""
    return cap._net.reader.cues.clock.stream_time(3600 + n)


def _srt(path):
    """[(n, start s, end s)] of the cues "Live n" of an .srt file."""
    def t(s):
        h, m, rest = s.split(":")
        sec, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000
    out = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        _i, times, text = block.split("\n", 2)
        a, b = times.split(" --> ")
        out.append((int(text.split()[1]), t(a), t(b)))
    return out


def _first_key_pts(path):
    """The PTS (s) of the first key frame of a TS file: the video PES whose
    first packet holds an H.264 IDR slice or the parameter sets before one
    (the random access indicator is not always set)."""
    import re
    from lindvb.tsclock import TS_PACKET, pes_pts
    nal = re.compile(b"\x00\x00\x01([\x00-\xff])")
    data = path.read_bytes()
    for i in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        pkt = data[i:i + TS_PACKET]
        p = pes_pts(pkt)
        if p is None:
            continue
        if any(m.group(1)[0] & 0x1f in (5, 7) for m in nal.finditer(pkt, 4)):
            return p / 90000
    return None


def _mkv_tracks(path):
    """(first key frame time s, {track: [(start s, end s, text)]},
    {track: tags}) of an MKV file, read with matroskademux."""
    pipe = Gst.parse_launch(f'filesrc location="{path}" ! matroskademux name=d')
    demux = pipe.get_by_name("d")
    got = {"key": None, "subs": {}, "tags": {}}

    def on_pad(_d, pad):
        name = pad.get_name()
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", False)
        sink.set_property("async", False)
        pipe.add(sink)
        sink.sync_state_with_parent()
        pad.link(sink.get_static_pad("sink"))

        def probe(_p, info):
            if info.type & Gst.PadProbeType.BUFFER:
                buf = info.get_buffer()
                if name.startswith("video") and got["key"] is None and \
                        not buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
                    got["key"] = buf.pts / Gst.SECOND
                if name.startswith("subtitle"):
                    ok, m = buf.map(Gst.MapFlags.READ)
                    text = bytes(m.data).decode() if ok else ""
                    if ok:
                        buf.unmap(m)
                    got["subs"].setdefault(name, []).append(
                        (buf.pts / Gst.SECOND, (buf.pts + buf.duration) / Gst.SECOND, text))
            else:
                ev = info.get_event()
                if ev.type == Gst.EventType.TAG and name.startswith("subtitle"):
                    tags = ev.parse_tag()
                    for tag in (Gst.TAG_TITLE, Gst.TAG_LANGUAGE_CODE):
                        ok, v = tags.get_string(tag)
                        if ok:
                            got["tags"].setdefault(name, {})[tag] = v
            return Gst.PadProbeReturn.OK
        pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, probe)
    demux.connect("pad-added", on_pad)
    pipe.set_state(Gst.State.PLAYING)
    msg = pipe.get_bus().timed_pop_filtered(20 * Gst.SECOND,
                                            Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    assert msg is not None and msg.type == Gst.MessageType.EOS, msg and msg.parse_error()
    return got["key"], got["subs"], got["tags"]


def _cues_at_their_time(cues, due, origin, what):
    """Each cue n of *cues* [(n, start, end)] starts at *due(n)* minus the
    file's *origin* and lasts 0.8 s; one already running when the file
    starts shows from its start to its end."""
    assert len(cues) >= SECONDS - 4, (what, cues)
    for n, a, b in cues:
        want = due(n) - origin
        if want < 0:
            assert a < CLOSE and abs(b - (want + 0.8)) < CLOSE, (what, n, a, b, want, cues)
            continue
        assert abs(a - want) < CLOSE, (what, n, a, want, cues)
        assert abs((b - a) - 0.8) < 0.01, (what, n, a, b)
    ns = [n for n, _a, _b in cues]
    assert ns == sorted(ns) and len(set(ns)) == len(ns) and ns[-1] - ns[0] == len(ns) - 1, ns


def test_ts_gets_an_srt_and_mkv_a_text_track_at_their_time(capture, tmp_path):
    ch, cap = capture(True)
    tmp_path = tmp_path / "rec"
    recs = _record(ch, cap, tmp_path, ("TS", "MKV"))
    ts, mkv = recs["TS"], recs["MKV"]
    # TS: the .srt beside it, its times from the start of the file
    srt = ts.path.with_suffix(".srt")
    assert srt.exists(), (ts.subs.report(), sorted(p.name for p in tmp_path.iterdir()))
    cues = _srt(srt)
    times = ts.subs._times
    _cues_at_their_time(cues, lambda n: _due(cap, n), times.base[0] / 90000, "srt")
    from lindvb import cuebuffer, library
    assert library.read_info(ts.path).get("subtitle_files") == [srt.name]
    # the cue packets stay out of the file: ffmpeg would take one for its start
    data = ts.path.read_bytes()
    assert cuebuffer.strip_cue_packets(data) == data
    # the file starts where players start it (ffprobe, where it is there)
    if shutil.which("ffprobe"):
        start = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0",
             str(ts.path)], capture_output=True, text=True, check=True).stdout.strip())
        assert abs(start - times.base[0] / 90000) < CLOSE, (start, times.base)
    # MKV: a text track named after the rendition, each cue at its picture:
    # as far after the first key frame as in the TS recording of the same place
    key, subs, tags = _mkv_tracks(mkv.path)
    assert list(subs) == ["subtitle_0"], (subs, mkv.remux.cue_report())
    assert tags["subtitle_0"][Gst.TAG_TITLE] == "English", tags
    assert tags["subtitle_0"].get(Gst.TAG_LANGUAGE_CODE) == "en", tags
    texts = [(int(t.split()[1]), a, b) for a, b, t in subs["subtitle_0"]]
    ts_key = _first_key_pts(ts.path)
    assert key is not None and ts_key is not None
    _cues_at_their_time(texts, lambda n: _due(cap, n) - ts_key + key, 0.0,
                        ("mkv", key, ts_key, mkv.remux.cue_report(), str(mkv.path)))
    assert mkv.remux.cues_dropped == 0 and mkv.remux.cue_clock.missed == 0, \
        mkv.remux.cue_report()
    assert mkv.remux.dropped == [] or all("x-raw" not in d for d in mkv.remux.dropped)


def test_switched_off_nothing_changes(capture, tmp_path):
    ch, cap = capture(False)
    recs = _record(ch, cap, tmp_path / "rec", ("TS", "MKV"))
    assert sorted(p.name for p in (tmp_path / "rec").iterdir()
                  if not p.name.endswith(".lindvb.json")) == ["rec-MKV.mkv", "rec-TS.ts"]
    assert recs["TS"].subs is None and recs["MKV"].remux.cue_clock is None
    _key, subs, _tags = _mkv_tracks(recs["MKV"].path)
    assert subs == {}
