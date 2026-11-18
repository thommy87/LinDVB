# SPDX-License-Identifier: GPL-2.0-or-later
"""Audio passthrough: Dolby Digital (AC-3), Dolby Digital Plus (E-AC-3) and DTS
go to an AV receiver undecoded, over HDMI or S/PDIF.

The receiver decodes the surround sound itself. LinDVB hands playbin an audio
sink that takes the chosen compressed formats next to plain PCM; playbin then
leaves the decoder out for those tracks and decodes everything else (MPEG, AAC)
as before.

The sink is the sound server (pulsesink, which PipeWire serves too) or an ALSA
device such as ``hdmi:CARD=PCH,DEV=0``. The sound server passes a format on
only when it is switched on for the output (pavucontrol › Output Devices ›
Advanced, or ``pactl set-sink-formats``). LinDVB asks the sink before playing
which of the chosen formats it takes; when it takes none, the sound is decoded
as without passthrough, with volume and equaliser.

This module holds the parts without GStreamer; lindvb.player builds the sink.
"""

# setting value -> GStreamer caps the parsers (ac3parse, dcaparse) put out
CODECS = ("ac3", "eac3", "dts")
CAPS = {
    "ac3": "audio/x-ac3, framed=(boolean)true",
    "eac3": "audio/x-eac3, framed=(boolean)true",
    "dts": "audio/x-dts, framed=(boolean)true",
}
RAW_CAPS = "audio/x-raw"


def codecs(settings):
    """The formats switched on in *settings* ("audio_passthrough"), in the
    order of CODECS; unknown names and doubles are left out."""
    chosen = settings.get("audio_passthrough") or []
    if isinstance(chosen, str):
        chosen = [c.strip() for c in chosen.split(",")]
    chosen = {str(c).lower() for c in chosen}
    return [c for c in CODECS if c in chosen]


def caps_string(formats, raw=True):
    """Caps for the sink's capsfilter: PCM (for the decoded tracks) and the
    compressed *formats*."""
    parts = [RAW_CAPS] if raw else []
    parts += [CAPS[c] for c in formats if c in CAPS]
    return "; ".join(parts)


def sink_spec(device):
    """(GStreamer factory, ALSA device or None) for the output *device*:
    empty is the sound server, anything else an ALSA device name."""
    device = (device or "").strip()
    if not device:
        return "pulsesink", None
    return "alsasink", device


def describe(formats):
    """Short names for the log and the stream info, e.g. "AC-3, DTS"."""
    names = {"ac3": "AC-3", "eac3": "E-AC-3", "dts": "DTS"}
    return ", ".join(names[c] for c in formats if c in names)
