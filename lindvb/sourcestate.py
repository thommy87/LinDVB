# SPDX-License-Identifier: GPL-2.0-or-later
"""One state model for all sources (stage E of the concept "Streams,
Timeshift, Untertitel und Zusatzton", section 6).

Every reader of a stream (the HTTP and GStreamer readers of the timeshift
buffer, the player when it plays a stream directly) reports the same
events, and the reaction to them stands in one place, :func:`reaction`:

========================  ==================================================
event                     reaction
========================  ==================================================
stalled, ended, network   connect again with the reconnect delays (1, 2, 4 …
                          30 s); when that gives up: the next DVB-I instance,
                          else a message
refused (401/403/451)     resolve the address once, then the next DVB-I
                          instance, else a clear message; never the direct
                          path, which the server refuses just the same
unsupported               a format the buffer cannot take: play it directly
                          once, "without timeshift"
a renderer stands         switch only that renderer off, with a hint; never
                          restart the stream
========================  ==================================================

Shadow mode: the reaction is worked out next to what the code does today
and written to the debug log where the two differ (lindvb.ui.window). The
code paths themselves do not ask it yet.
"""

import re
from dataclasses import dataclass

# -- events ------------------------------------------------------------------------

DATA = "data"                    # data arrives (again)
STALLED = "stalled"              # no data for a while, and no error either
REFUSED = "refused"              # HTTP 401, 403 or 451: the stream is not for us
UNSUPPORTED = "unsupported"      # a format the reader cannot take
ENDED = "ended"                  # the server ended the stream
NETWORK = "network"              # any other failure of the connection
EVENTS = (DATA, STALLED, REFUSED, UNSUPPORTED, ENDED, NETWORK)

# HTTP answers that mean "not for you": the stream itself is there
REFUSED_CODES = (401, 403, 451)

# -- reactions ---------------------------------------------------------------------

NOTHING = ""
RECONNECT = "reconnect"
RESOLVE = "resolve"              # ask the address behind a gateway's redirect
NEXT_INSTANCE = "next-instance"  # IT-10 failover to another DVB-I instance
MESSAGE = "message"              # say what went wrong, and stop
DIRECT = "direct"                # play the stream without the timeshift buffer
RENDERER_OFF = "renderer-off"

# GStreamer error codes (GstStreamError, GstCoreError) that name a format
# the pipeline cannot take, not a broken connection
_UNSUPPORTED_STREAM_CODES = {3, 4, 5, 6, 9, 10, 11, 12, 13}
_STREAM_DOMAIN = "gst-stream-error-quark"
_CORE_DOMAIN = "gst-core-error-quark"
_MISSING_PLUGIN = 12


def http_code(text):
    """The HTTP status a GStreamer or urllib error text names, or None:
    souphttpsrc writes "Forbidden (403)", urllib "HTTP 403 Forbidden"."""
    m = re.search(r"\((\d{3})\)|\bHTTP (\d{3})\b", text or "")
    return int(m.group(1) or m.group(2)) if m else None


def from_gst_error(err, debug=""):
    """The event of a GStreamer error message (a GLib.Error and its debug text)."""
    code = http_code(f"{getattr(err, 'message', '')} {debug or ''}")
    if code in REFUSED_CODES:
        return REFUSED
    domain = getattr(err, "domain", "") or ""
    if not isinstance(domain, str):          # pragma: no cover (a GQuark number)
        domain = ""
    if domain == _STREAM_DOMAIN and err.code in _UNSUPPORTED_STREAM_CODES:
        return UNSUPPORTED
    if domain == _CORE_DOMAIN and err.code == _MISSING_PLUGIN:
        return UNSUPPORTED
    return NETWORK


def from_exception(exc):
    """The event of an exception of the HTTP readers (lindvb.ipstream)."""
    from .ipstream import NotTransportStream, Refused
    if isinstance(exc, Refused):
        return REFUSED
    if isinstance(exc, NotTransportStream):
        return UNSUPPORTED
    if isinstance(exc, TimeoutError):
        return STALLED
    return NETWORK


# -- the reaction --------------------------------------------------------------------

@dataclass
class Situation:
    """What is known when a source reports *event*.

    *received*: data flowed before; *gave_up*: the reader's reconnect
    delays ran out (lindvb.ipstream.Retry); *buffered*: the stream went
    through the timeshift buffer; *resolved*: the refused address was
    resolved already; *instances_left*: other playable DVB-I instances not
    tried yet; *direct_tried*: the stream was played directly already;
    *renderer*: a renderer (subtitles) stands, not the source."""

    event: str
    received: bool = False
    gave_up: bool = False
    buffered: bool = False
    resolved: bool = False
    instances_left: int = 0
    direct_tried: bool = False
    renderer: str = ""


def _elsewhere(s):
    return NEXT_INSTANCE if s.instances_left > 0 else MESSAGE


def reaction(s):
    """What to do in the Situation *s* (see the table in the module notes)."""
    if s.renderer:
        return RENDERER_OFF
    if s.event == DATA or s.event not in EVENTS:
        return NOTHING
    if s.event == REFUSED:
        return RESOLVE if not s.resolved else _elsewhere(s)
    if s.event == UNSUPPORTED:
        return DIRECT if s.buffered and not s.direct_tried else _elsewhere(s)
    # stalled, ended, network
    if s.received and not s.gave_up:
        return RECONNECT
    return _elsewhere(s)


def shadow_line(name, s, today):
    """The debug log line for the Situation *s* of channel *name*, where the
    code does *today* (a reaction): None when the model agrees."""
    model = reaction(s)
    if model == today:
        return None
    return (f"{name}: {s.event or 'unknown'} (data before: {'yes' if s.received else 'no'}, "
            f"instances left: {s.instances_left}) – the state model would "
            f"{model or 'do nothing'}, the player does {today or 'nothing'}")
