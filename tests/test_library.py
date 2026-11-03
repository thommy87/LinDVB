"""Recording library: programme details beside recordings, resume positions, previews."""


import pytest

from lindvb import library
from lindvb.epg import Event
from lindvb.recordings import find_recordings, sort_recordings


def test_sidecar_round_trip(tmp_path):
    rec = tmp_path / "Tatort.ts"
    rec.write_bytes(b"\x47" * 188)
    ev = Event(1_800_000_000, 5400, "Tatort", "Der Fall", "Kommissarin ermittelt.", "Krimi")
    library.write_info(rec, "Das Erste HD", "Tatort", ev, start=1_799_999_700, fmt="TS")
    library.write_info(rec, end=1_800_005_700)
    assert library.sidecar_path(rec).name == "Tatort.lindvb.json"
    (r,) = find_recordings([tmp_path])
    assert (r.title, r.subtitle, r.channel) == ("Tatort", "Der Fall", "Das Erste HD")
    assert r.description == "Kommissarin ermittelt." and r.duration == 6000
    assert "Das Erste HD" in r.details() and "100 min" in r.details()
    assert "der fall" in r.search_text()
    library.delete_info(rec)
    assert not library.sidecar_path(rec).exists()


def test_files_without_details_still_listed(tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "b.mp3").write_bytes(b"xx")
    (tmp_path / "b.lindvb.json").write_text("not json")
    recs = find_recordings([tmp_path])
    assert sorted(r.title for r in recs) == ["a", "b"]
    assert [r.title for r in sort_recordings(recs, "size")] == ["b", "a"]
    assert [r.title for r in sort_recordings(recs, "title")] == ["a", "b"]


def test_positions(tmp_path, monkeypatch):
    monkeypatch.setattr(library.Positions, "path", tmp_path / "positions.json")
    p = library.Positions()
    assert p.resume_at("/x.ts") is None and p.progress("/x.ts") == 0
    p.update("/x.ts", 10, 3600)
    assert p.resume_at("/x.ts") is None             # barely started
    p.update("/x.ts", 1200, 3600)
    assert p.resume_at("/x.ts") == 1200 and not p.watched("/x.ts")
    p.update("/x.ts", 3590, 3600)
    assert p.resume_at("/x.ts") is None and p.watched("/x.ts")
    p.save()
    assert library.Positions().watched("/x.ts")
    p.forget("/x.ts")
    assert p.get("/x.ts") is None


def test_preview_picture(tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gst
    Gst.init(None)
    if not all(Gst.ElementFactory.find(e) for e in ("x264enc", "mpegtsmux", "h264parse",
                                                       "avdec_h264")):
        pytest.skip("GStreamer H.264 elements missing")
    from lindvb.thumbnails import grab_frame
    src = tmp_path / "clip.ts"
    pipe = Gst.parse_launch(f"videotestsrc num-buffers=50 ! video/x-raw,width=320,height=180 ! "
                            f"x264enc ! h264parse ! mpegtsmux ! filesink location={src}")
    pipe.set_state(Gst.State.PLAYING)
    pipe.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    out = tmp_path / "thumb.png"
    assert grab_frame(src, out) and out.stat().st_size > 100
