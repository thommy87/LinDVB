"""--va-test: plays a recording in plain GStreamer pipelines and counts the
graphics buffers each round leaves behind (lindvb.vatest). The VA-API variants
need an Intel or AMD graphics card; here the software decoder stands in for
the VA one, so the rounds, the reused LinDVB output and the report are tested."""

import io
import os

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

from lindvb import vatest  # noqa: E402
from lindvb.selfcheck import SAMPLE  # noqa: E402


@pytest.fixture
def software_as_va(monkeypatch):
    monkeypatch.setattr(vatest, "DECODERS", {"video/x-h264": ("avdec_h264", "avdec_h264")})
    monkeypatch.setattr(vatest, "FRAMES", 10)


def test_graphics_buffers_are_counted(tmp_path):
    for i, target in enumerate(["/dmabuf:", "/dmabuf:", "socket:[1]", "/dev/dri/renderD128"]):
        os.symlink(target, tmp_path / str(i))
    assert vatest.dmabufs(str(tmp_path)) == 2
    assert vatest.dmabufs(str(tmp_path / "missing")) == 0


def test_the_sample_is_found_as_h264():
    assert vatest._codec(SAMPLE) == "video/x-h264"


def test_rounds_count_the_buffers_and_pictures(software_as_va):
    res = vatest.run_variant(SAMPLE, "video/x-h264",
                             ("x", "va", ("deinterlace",), "fake", True), rounds=3)
    assert len(res["counts"]) == 3 and all(res["frames"]) and not res["error"]
    assert res["output"] == "fakesink"


def test_one_pipeline_restarted(software_as_va):
    res = vatest.run_variant(SAMPLE, "video/x-h264", ("x", "va", (), "fake", False), rounds=3)
    assert len(res["counts"]) == 3 and all(res["frames"])


def test_lindvb_output_is_reused_across_rounds(software_as_va):
    """LinDVB keeps its video output across channels; so does the test."""
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display")
    res = vatest.run_variant(SAMPLE, "video/x-h264", ("x", "va", (), "copy", True), rounds=3)
    assert res["output"] == "copy" and all(res["frames"]), res


def test_missing_elements_are_skipped():
    res = vatest.run_variant(SAMPLE, "video/x-h264",
                             ("x", "va", ("no-such-element",), "fake", True), rounds=2)
    assert res["skipped"].endswith("missing")
    assert vatest.describe(res) == f"x: skipped ({res['skipped']})"


def test_the_report_line():
    line = vatest.describe({"label": "VA decoder + vadeinterlace", "output": "fakesink",
                            "start": 300, "counts": [321, 338, 355], "frames": [60, 60, 0],
                            "error": "vah264dec0: failed"})
    assert line == ("VA decoder + vadeinterlace [fakesink]: pictures in 2 of 3 rounds; "
                    "graphics buffers 300 -> 355: first round +21, then +17.0 per round; "
                    "error: vah264dec0: failed")


def test_the_whole_test_writes_its_report(software_as_va, monkeypatch, tmp_path):
    monkeypatch.setattr(vatest, "report_path", lambda: tmp_path / "va-test.txt")
    out = io.StringIO()
    assert vatest.run(["--va-test", "--rounds", "2"], out=out) == 0
    report = (tmp_path / "va-test.txt").read_text()
    assert "video: video/x-h264" in report
    for label, *_rest in vatest.VARIANTS:
        assert label in report
    assert "The report is in" in out.getvalue()


def test_a_missing_file_is_named(tmp_path, monkeypatch):
    monkeypatch.setattr(vatest, "report_path", lambda: tmp_path / "va-test.txt")
    monkeypatch.setenv("LINDVB_ORIG_CWD", str(tmp_path))
    out = io.StringIO()
    assert vatest.run(["--va-test", "nothing.ts"], out=out) == 2
    assert "file not found" in out.getvalue()
