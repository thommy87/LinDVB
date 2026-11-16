"""Playback error texts: an HTTP refusal names its reason."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib  # noqa: E402

import types  # noqa: E402

from lindvb import player as player_mod  # noqa: E402
from lindvb.player import describe_error, http_status, redirect_target  # noqa: E402


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


# -- a refused DVB-I gateway: resolving the redirect ---------------------------

ARD_DEBUG = ("../ext/soup/gstsouphttpsrc.c(1729): gst_soup_http_src_parse_status (): "
             "/GstPlayBin:player/GstURIDecodeBin:uridecodebin0/GstSoupHTTPSrc:source:\n"
             "Forbidden (403), URL: https://itv.ard.de/dvbi/dash/36/dash.mpd, "
             "Redirect to: http://mcdn.br.de/br/fs/ard_alpha/replay/de/manifest.mpd")


def test_http_status_and_redirect_target():
    assert http_status(_err("Forbidden"), ARD_DEBUG) == 403
    assert redirect_target(ARD_DEBUG) == "http://mcdn.br.de/br/fs/ard_alpha/replay/de/manifest.mpd"
    assert http_status(_err("Could not read"), "no status here") is None
    assert redirect_target("Forbidden (403), URL: https://x/a.mpd") is None


def _player_stub(uri="https://itv.ard.de/dvbi/dash/36/dash.mpd"):
    """Just the state the resolving path touches."""
    return types.SimpleNamespace(capture=None, uri=uri, _token=1, _resolve_token=-1,
                                 _played=True, _fallback_done=True, _pb3=False,
                                 started=[], errors=[],
                                 _start_uri=lambda pb3: None, emit=lambda *a: None,
                                 _resolve_worker=lambda *a: None)


def test_refused_stream_is_resolved_once(monkeypatch):
    runs = []
    monkeypatch.setattr(player_mod.threading, "Thread",
                        lambda target, args, daemon: types.SimpleNamespace(
                            start=lambda: runs.append(args)))
    p = _player_stub()
    assert player_mod.Player._resolve_refused(p, _err("Forbidden"), ARD_DEBUG)
    assert runs and runs[0][0] == p.uri
    # one attempt per playback, and only for an HTTP refusal
    assert not player_mod.Player._resolve_refused(p, _err("Forbidden"), ARD_DEBUG)
    p._resolve_token = -1
    assert not player_mod.Player._resolve_refused(p, _err("Not Found"),
                                                  "Not Found (404), URL: https://x/a.mpd")
    p.uri, p._resolve_token = "udp://239.0.0.1:1234", -1
    assert not player_mod.Player._resolve_refused(p, _err("Forbidden"), ARD_DEBUG)


def test_the_resolved_address_is_played(monkeypatch):
    p = _player_stub()
    p._start_uri = lambda pb3: p.started.append(pb3)
    cdn = "https://mcdn.br.de/br/fs/ard_alpha/replay/de/manifest.mpd"
    player_mod.Player._resolved(p, 1, p.uri, cdn, "refused")
    assert p.uri == cdn and p.started == [False] and not p._played

    # nothing else to try: the error reaches the window
    p2 = _player_stub()
    p2.emit = lambda *a: p2.errors.append(a)
    player_mod.Player._resolved(p2, 1, p2.uri, p2.uri, "refused")
    assert p2.errors == [("error", "refused")] and not p2.started

    # a stale answer (the channel changed meanwhile) is dropped
    p3 = _player_stub()
    p3.emit = lambda *a: p3.errors.append(a)
    p3._start_uri = lambda pb3: p3.started.append(pb3)
    player_mod.Player._resolved(p3, 0, p3.uri, cdn, "refused")
    assert not p3.errors and not p3.started
