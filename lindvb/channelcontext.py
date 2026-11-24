# SPDX-License-Identifier: GPL-2.0-or-later
"""The channel context (stage E of the concept "Streams, Timeshift,
Untertitel und Zusatzton", sections 1 and 9).

The signalling hangs on the channel, not on whether a timeshift buffer
runs. HbbTV asks the context "where do the applications come from?" and
gets the DVB-I list for a DVB-I internet channel and the AIT of the
broadcast otherwise, with or without timeshift. The country that HbbTV
applications are told and youth protection uses comes from here too: for a
DVB-I channel the country of its list block, for everything else the HbbTV
country of the preferences.

The context also knows the other instances of a DVB-I channel (IT-10
failover), among them a satellite, cable or antenna instance that carries
subtitles the internet stream does not have.
"""

from dataclasses import dataclass, field

# where the HbbTV applications of a channel come from
DVBI_LIST = "dvbi"               # the DVB-I service list (also through the buffer)
AIT = "ait"                      # the AIT in the broadcast (or in the buffered stream)
NONE = ""


def signalling(channel, capture):
    """Where the HbbTV applications of *channel* come from, played with
    *capture* (None: played directly, without LinDVB's buffer)."""
    if channel is not None and getattr(channel, "dvbi_apps", None) and \
            (capture is None or getattr(channel, "is_iptv", False)):
        # an internet channel of a DVB-I list, also through the timeshift
        # buffer (ONE HD): its list names the applications; an AIT in the
        # stream itself still comes first (lindvb.ui.hbbtv)
        return DVBI_LIST
    return AIT if capture is not None else NONE


def country(settings, channel):
    """The country HbbTV applications are told (configuration.countryId)."""
    from .hbbtv import country_for
    return country_for(settings, channel)


def carries_subtitles(channel):
    """A broadcast channel whose channel search found subtitles (DVB
    subtitles or a teletext service)."""
    return bool(getattr(channel, "subtitle_pids", None) or getattr(channel, "teletext_pid", 0))


@dataclass
class ChannelContext:
    """What LinDVB knows about the channel on air, in one place.

    *channels* is the channel list (the channel search's channels find the
    broadcast instances), *profiles* the enabled tuners
    (lindvb.tuner.TunerManager.profiles)."""

    channel: object
    capture: object = None
    settings: dict = field(default_factory=dict)
    channels: list = field(default_factory=list)
    profiles: list = field(default_factory=list)

    @property
    def signalling(self):
        return signalling(self.channel, self.capture)

    @property
    def country(self):
        return country(self.settings, self.channel)

    def instances(self):
        """The other playable instances of a DVB-I channel, in priority order
        (lindvb.dvbi.alternatives); [] for other channels."""
        c = self.channel
        if c is None or not getattr(c, "dvbi_id", ""):
            return []
        from . import dvbi
        s = self.settings
        return dvbi.alternatives(c, self.channels, sat_direct=s.get("dvbi_sat_direct", True),
                                 prefer_broadcast=s.get("dvbi_prefer_broadcast", False))

    def receivable(self, channel):
        """A tuner of the user's can receive the broadcast *channel*."""
        from .tunerprofiles import supports
        return any(supports(p, channel.delsys) for p in self.profiles)

    def broadcast_subtitles(self):
        """The satellite, cable or antenna instance of an internet channel
        that carries subtitles and that a tuner can receive, or None."""
        c = self.channel
        if c is None or not getattr(c, "is_iptv", False):
            return None
        return next((alt for alt in self.instances()
                     if not alt.is_iptv and carries_subtitles(alt) and self.receivable(alt)),
                    None)

    def describe(self):
        """One line for the debug log."""
        c = self.channel
        if c is None:
            return "no channel"
        insts = self.instances()
        kinds = ", ".join(("internet" if a.is_iptv else a.delsys) for a in insts) or "none"
        subs = self.broadcast_subtitles()
        return (f"{c.name}: country {self.country}, applications from "
                f"{self.signalling or 'nowhere'}, other instances: {kinds}"
                + (f", subtitles via {subs.delsys}" if subs is not None else ""))
