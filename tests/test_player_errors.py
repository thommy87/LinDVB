"""Playback error texts: an HTTP refusal names its reason."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib  # noqa: E402

from lindvb.player import describe_error  # noqa: E402


def _err(message):
    return GLib.Error(message)


def test_forbidden_after_redirect():
    dbg = ("gstsouphttpsrc.c(1729): ...:\nForbidden (403), URL: https://itv.ard.de/dvbi/dash/1/dash.mpd, "
           "Redirect to: http://daserste-live.ard-mcdn.de/daserste/replay/dash/de/manifest.mpd")
    text = describe_error(_err("Forbidden"), dbg)
    assert "HTTP 403" in text and "countries" in text


def test_not_found_and_other_errors():
    assert "HTTP 404" in describe_error(_err("Not Found"), "Not Found (404), URL: https://x/a.mpd")
    assert describe_error(_err("Stream doesn't contain enough data."), "Can't typefind stream") \
        == "Stream doesn't contain enough data."
