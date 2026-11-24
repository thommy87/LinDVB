"""The state model of the sources (stage E, shadow mode): events, the one
reaction table, and what the readers report."""

import socket
import types

import pytest

from lindvb import ipstream
from lindvb import sourcestate as st


def test_http_code():
    assert st.http_code("Forbidden (403), URL: https://x/a.mpd") == 403
    assert st.http_code("itv.ard.de: HTTP 451 Unavailable For Legal Reasons") == 451
    assert st.http_code("Could not resolve host") is None
    assert st.http_code("") is None and st.http_code(None) is None


def _gerr(domain, code, message="x"):
    return types.SimpleNamespace(domain=domain, code=code, message=message)


def test_events_of_gstreamer_errors():
    assert st.from_gst_error(_gerr("gst-resource-error-quark", 5, "Forbidden"),
                             "Forbidden (403), URL: https://x") == st.REFUSED
    assert st.from_gst_error(_gerr("gst-resource-error-quark", 5, "Not Found"),
                             "Not Found (404)") == st.NETWORK
    assert st.from_gst_error(_gerr("gst-stream-error-quark", 9, "Could not demultiplex")) \
        == st.UNSUPPORTED
    assert st.from_gst_error(_gerr("gst-stream-error-quark", 6)) == st.UNSUPPORTED
    assert st.from_gst_error(_gerr("gst-core-error-quark", 12)) == st.UNSUPPORTED
    # "Internal data stream error" follows from anything: a broken connection
    assert st.from_gst_error(_gerr("gst-stream-error-quark", 1)) == st.NETWORK
    assert st.from_gst_error(_gerr("gst-resource-error-quark", 3, "Could not read")) \
        == st.NETWORK


def test_events_of_gstreamer_errors_real():
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    err = GLib.Error.new_literal(Gst.StreamError.quark(), "demux", int(Gst.StreamError.DEMUX))
    assert st.from_gst_error(err) == st.UNSUPPORTED
    err = GLib.Error.new_literal(Gst.ResourceError.quark(), "Forbidden",
                                 int(Gst.ResourceError.READ))
    assert st.from_gst_error(err, "Forbidden (403)") == st.REFUSED


def test_events_of_reader_exceptions():
    assert st.from_exception(ipstream.Refused("HTTP 403 Forbidden")) == st.REFUSED
    assert st.from_exception(ipstream.NotTransportStream()) == st.UNSUPPORTED
    assert st.from_exception(socket.timeout("timed out")) == st.STALLED
    assert st.from_exception(OSError("Connection reset")) == st.NETWORK
    assert st.from_exception(ValueError("bad chunk")) == st.NETWORK


S = st.Situation


@pytest.mark.parametrize("situation,expected", [
    # stalled, ended, network: connect again while the reader still tries
    (S(st.STALLED, received=True), st.RECONNECT),
    (S(st.ENDED, received=True), st.RECONNECT),
    (S(st.NETWORK, received=True), st.RECONNECT),
    # ... and when it gave up or never got anything: another instance, else a message
    (S(st.NETWORK, received=True, gave_up=True, instances_left=1), st.NEXT_INSTANCE),
    (S(st.STALLED, received=True, gave_up=True), st.MESSAGE),
    (S(st.NETWORK, buffered=True), st.MESSAGE),
    # refused: resolve once, then another instance or a message; never directly
    (S(st.REFUSED, buffered=True), st.RESOLVE),
    (S(st.REFUSED, buffered=True, resolved=True, instances_left=2), st.NEXT_INSTANCE),
    (S(st.REFUSED, buffered=True, resolved=True), st.MESSAGE),
    # a format the buffer cannot take: directly, once
    (S(st.UNSUPPORTED, buffered=True), st.DIRECT),
    (S(st.UNSUPPORTED, buffered=True, direct_tried=True), st.MESSAGE),
    (S(st.UNSUPPORTED, instances_left=1), st.NEXT_INSTANCE),
    # a renderer that stands: only that renderer, the stream plays on
    (S(st.DATA, received=True, renderer="subtitles"), st.RENDERER_OFF),
    (S(st.DATA, received=True), st.NOTHING),
    (S("", received=True), st.NOTHING),
])
def test_reaction_table(situation, expected):
    assert st.reaction(situation) == expected


def test_refused_never_leads_to_the_direct_path():
    for resolved in (False, True):
        for left in (0, 1):
            for received in (False, True):
                s = S(st.REFUSED, received=received, buffered=True, resolved=resolved,
                      instances_left=left, gave_up=True)
                assert st.reaction(s) != st.DIRECT


def test_shadow_line():
    assert st.shadow_line("ONE HD", S(st.UNSUPPORTED, buffered=True), st.DIRECT) is None
    line = st.shadow_line("Das Erste", S(st.REFUSED, buffered=True, resolved=True,
                                         gave_up=True), st.DIRECT)
    assert line.startswith("Das Erste: refused")
    assert "would message" in line and "does direct" in line


# -- the readers report their events ------------------------------------------------

def test_http_reader_reports_a_refusal():
    """A 403 that resolving does not get around: the reader says "refused"
    and that it resolved the address, not only a text."""
    import http.server
    import threading

    class No(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_error(403)

        do_HEAD = do_GET

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), No)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    errors, done = [], threading.Event()
    r = ipstream.StreamReader(f"http://127.0.0.1:{srv.server_address[1]}/live.ts",
                              lambda b: None, lambda m: (errors.append(m), done.set()))
    r.start()
    try:
        assert done.wait(15)
        assert "403" in errors[0]
        assert (r.event, r.resolved, r.gave_up) == (st.REFUSED, True, True)
    finally:
        r.close()
        srv.shutdown()


def test_stream_source_passes_the_state_on():
    src = ipstream.StreamSource("http://127.0.0.1:9/live.ts", lambda b: None)
    assert (src.event, src.resolved, src.gave_up) == ("", False, False)
    src.reader = types.SimpleNamespace(event=st.STALLED, resolved=False, gave_up=False,
                                       received=10)
    assert (src.event, src.gave_up) == (st.STALLED, False)
    src._event = st.UNSUPPORTED                      # GStreamer could not be loaded
    assert (src.event, src.gave_up) == (st.UNSUPPORTED, True)
