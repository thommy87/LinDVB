# SPDX-License-Identifier: GPL-2.0-or-later
"""What flows through some pads, for the debug log (lindvb.flowwatch)."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from lindvb import flowwatch  # noqa: E402


def test_clock_text():
    assert flowwatch.clock_text(None) == "-"
    assert flowwatch.clock_text((2 * 3600 + 70) * Gst.SECOND + 250_000_000) == "2:01:10.2"


def test_flow_of_a_pad():
    """Buffers with and without a time, gaps, how long ago the last came."""
    Gst.init(None)
    now = [100.0]
    watch = flowwatch.FlowWatch(clock=lambda: now[0])
    pl = Gst.parse_launch("appsrc name=src format=time caps=text/x-raw,format=utf8 ! "
                          "fakesink name=sink sync=false")
    sink = pl.get_by_name("sink").get_static_pad("sink")
    assert watch.watch(sink, "text") == "text"
    other = Gst.parse_launch("fakesrc ! fakesink name=s2").get_by_name("s2").get_static_pad("sink")
    assert watch.watch(other, "text") == "text'"          # a second pad of that name
    pl.set_state(Gst.State.PLAYING)
    src = pl.get_by_name("src")
    for pts in (Gst.SECOND, 2 * Gst.SECOND, Gst.CLOCK_TIME_NONE):
        buf = Gst.Buffer.new_wrapped(b"x")
        buf.pts = pts
        src.emit("push-buffer", buf)
    src.send_event(Gst.Event.new_gap(5 * Gst.SECOND, Gst.SECOND))     # queued in order
    src.emit("end-of-stream")
    pl.get_bus().timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pl.set_state(Gst.State.NULL)
    now[0] = 102.5
    flow = watch.flows["text"]
    assert (flow.buffers, flow.untimed, flow.gaps) == (3, 1, 1)
    assert flow.last == 5 * Gst.SECOND
    assert watch.summary() == ("text: 3 buffers, 1 without time, 1 gaps, last 0:00:05.0 "
                               "(2.5 s ago); text': nothing")


def test_nothing_watched():
    assert flowwatch.FlowWatch().summary() == "no pads watched"
