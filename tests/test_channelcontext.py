"""The channel context (stage E): where the HbbTV applications come from,
the HbbTV country, and the other instances of a DVB-I channel."""

import dataclasses
import types

from lindvb import channelcontext as cc
from lindvb import dvbi
from lindvb import hbbtv as hb
from lindvb.channels import Channel
from test_dvbi import SCANNED, SERVICE_LIST


def _old_rule(channel, capture):
    """ui.hbbtv.channel_changed before stage E, for the comparison."""
    if channel is not None and channel.dvbi_apps and \
            (capture is None or getattr(channel, "is_iptv", False)):
        return cc.DVBI_LIST
    return cc.AIT if capture is not None else cc.NONE


def test_signalling_is_the_rule_of_before():
    apps = [{"url": "https://x/ait.xml", "role": "parallel", "level": "service"}]
    dash = Channel(name="ONE HD", delsys="iptv", url="https://x/one.mpd", dvbi_id="tag:one",
                   dvbi_apps=apps)
    sat = Channel(name="ONE HD", delsys="dvb-s2", frequency=11604000, sid=28722,
                  dvbi_id="tag:one", dvbi_apps=apps)
    plain = Channel(name="Das Erste HD", delsys="dvb-s2", frequency=11494000, sid=10301)
    m3u = Channel(name="Some IPTV", delsys="iptv", url="http://x/live.ts")
    capture = object()
    for ch in (dash, sat, plain, m3u, None):
        for cap in (None, capture):
            assert cc.signalling(ch, cap) == _old_rule(ch, cap), (ch and ch.name, cap)
    # with and without timeshift alike: the list names the applications
    assert cc.signalling(dash, None) == cc.signalling(dash, capture) == cc.DVBI_LIST
    # the satellite instance of a DVB-I channel: its AIT
    assert cc.signalling(sat, capture) == cc.AIT


def test_country_of_the_channel():
    settings = {"hbbtv_country": "aut", "dvbi_country": "DEU"}
    dvbi_ch = Channel(name="ONE HD", delsys="iptv", url="https://x", dvbi_id="tag:one",
                      dvbi_country="ita")
    sat = Channel(name="ORF", delsys="dvb-s2")
    for ch in (dvbi_ch, sat, None):
        assert cc.country(settings, ch) == hb.country_for(settings, ch)
    assert cc.ChannelContext(dvbi_ch, settings=settings).country == "ITA"
    assert cc.ChannelContext(sat, settings=settings).country == "AUT"


def _ard_internet(scanned):
    sl = dvbi.parse_service_list(SERVICE_LIST)
    ard = next(c for c in dvbi.build_channels(sl, "DE-BY", scanned).channels
               if c.dvbi_id == "tag:ard")
    return dvbi.alternatives(ard, scanned)[0]          # its internet instance


def test_subtitles_via_the_broadcast_instance():
    """Concept, decided on 23.11.2026: a DVB-I channel's broadcast instance
    with subtitles is offered when a tuner can receive it."""
    scanned = [dataclasses.replace(SCANNED[0], teletext_pid=804)] + SCANNED[1:]
    web = _ard_internet(scanned)
    assert web.is_iptv
    t2 = [{"adapter": 0, "systems": ["dvb-t", "dvb-t2"]}]
    sat_only = [{"adapter": 0, "systems": ["dvb-s", "dvb-s2"]}]
    ctx = cc.ChannelContext(web, None, {}, scanned, t2)
    subs = ctx.broadcast_subtitles()
    assert subs is not None and subs.delsys == "dvb-t2" and subs.teletext_pid == 804
    assert subs.dvbi_id == web.dvbi_id and subs.name == web.name
    assert "subtitles via dvb-t2" in ctx.describe()
    # no tuner for it, or no subtitles found by the channel search: nothing
    assert cc.ChannelContext(web, None, {}, scanned, sat_only).broadcast_subtitles() is None
    assert cc.ChannelContext(web, None, {}, SCANNED, t2).broadcast_subtitles() is None
    # the broadcast instance itself needs none
    assert cc.ChannelContext(subs, object(), {}, scanned, t2).broadcast_subtitles() is None


def test_describe_for_the_debug_log():
    web = _ard_internet(SCANNED)
    line = cc.ChannelContext(web, None, {"hbbtv_country": "DEU"}, SCANNED, []).describe()
    assert line.startswith(f"{web.name}: country DEU, applications from ")
    assert "other instances: dvb-t2" in line and "subtitles via" not in line
    assert cc.ChannelContext(None).describe() == "no channel"
    plain = Channel(name="Arte", delsys="dvb-s2")
    assert cc.ChannelContext(plain, object()).instances() == []


def test_window_logs_the_context(monkeypatch):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gst", "1.0")
    from lindvb import debuglog
    from lindvb.ui.window import MainWindow
    lines = []
    monkeypatch.setattr(debuglog, "write", lambda cat, *p: lines.append((cat, " ".join(p))))
    web = _ard_internet(SCANNED)
    app = types.SimpleNamespace(db=types.SimpleNamespace(channels=SCANNED),
                                tuners=types.SimpleNamespace(profiles=lambda: []))
    win = types.SimpleNamespace(capture=None, settings={}, app=app)
    MainWindow._log_channel_context(win, web)
    assert lines[-1][0] == "Channel" and lines[-1][1].startswith("context: ")

    def broken():
        raise OSError("no /dev/dvb")
    app.tuners.profiles = broken                 # never in the way of a channel change
    MainWindow._log_channel_context(win, web)
    assert lines[-1][1] == "context: not available (no /dev/dvb)"
