# SPDX-License-Identifier: GPL-2.0-or-later
"""MPRIS: media keys, the GNOME media controls and other players' remotes.

LinDVB appears on the session bus as ``org.mpris.MediaPlayer2.lindvb``. Play,
Pause, Stop, Next and Previous act like the buttons of the control bar
(channel up/down for TV and radio, the next file of a playlist), and the
metadata shows the channel or file with the programme or song on air.
"""

from gi.repository import Gio, GLib

BUS_NAME = "org.mpris.MediaPlayer2.lindvb"
PATH = "/org/mpris/MediaPlayer2"
IFACE_ROOT = "org.mpris.MediaPlayer2"
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"

XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

DESKTOP_ENTRY = "io.github.thommy87.LinDVB"


def build_metadata(state):
    """MPRIS metadata from a plain dict (see MprisServer.state)."""
    md = {"mpris:trackid": GLib.Variant("o", PATH + "/Track/" + str(state.get("track", 0)))}
    if state.get("title"):
        md["xesam:title"] = GLib.Variant("s", state["title"])
    if state.get("artist"):
        md["xesam:artist"] = GLib.Variant("as", [state["artist"]])
    if state.get("album"):
        md["xesam:album"] = GLib.Variant("s", state["album"])
    if state.get("art"):
        md["mpris:artUrl"] = GLib.Variant("s", state["art"])
    if state.get("length"):
        md["mpris:length"] = GLib.Variant("x", int(state["length"] * 1e6))
    return md


class MprisServer:
    """``source`` is the main window; it must offer the small API used below."""

    def __init__(self, app, source):
        self.app = app
        self.win = source
        self.conn = None
        self.state = {}
        self._owner = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME,
                                       Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                                       self._on_bus, None, None)

    def close(self):
        if self._owner:
            Gio.bus_unown_name(self._owner)
            self._owner = 0

    def _on_bus(self, conn, _name):
        self.conn = conn
        info = Gio.DBusNodeInfo.new_for_xml(XML)
        for iface in info.interfaces:
            conn.register_object(PATH, iface, self._on_call, self._on_get, self._on_set)

    # -- calls ----------------------------------------------------------------
    def _on_call(self, _conn, _sender, _path, iface, method, params, invocation):
        w = self.win
        actions = {
            "Raise": lambda: w.present(), "Quit": lambda: self.app.quit(),
            "Next": lambda: w.step_item(1), "Previous": lambda: w.step_item(-1),
            "PlayPause": w.player_pause, "Stop": w.stop_playback,
            "Play": lambda: w.player_pause() if w.mpris_status() != "Playing" else None,
            "Pause": lambda: w.player_pause() if w.mpris_status() == "Playing" else None,
            "Seek": lambda: w.player.jump(params.unpack()[0] / 1e6),
            "SetPosition": lambda: w.player.seek_to(params.unpack()[1] / 1e6),
            "OpenUri": lambda: w.play_files([params.unpack()[0]]),
        }
        fn = actions.get(method)
        if fn is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
            return
        GLib.idle_add(lambda: (fn(), False)[1])
        invocation.return_value(None)

    def _props(self, iface):
        w = self.win
        if iface == IFACE_ROOT:
            return {
                "CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", "LinDVB"),
                "DesktopEntry": GLib.Variant("s", DESKTOP_ENTRY),
                "SupportedUriSchemes": GLib.Variant("as", ["file", "http", "https", "rtsp",
                                                           "rtp", "udp"]),
                "SupportedMimeTypes": GLib.Variant("as", ["video/mp2t", "video/mp4",
                                                          "video/x-matroska", "audio/mpeg"]),
            }
        pos = w.player.position()
        return {
            "PlaybackStatus": GLib.Variant("s", w.mpris_status()),
            "Rate": GLib.Variant("d", float(getattr(w.player, "rate", 1.0) or 1.0)),
            "Metadata": GLib.Variant("a{sv}", build_metadata(self.state)),
            "Volume": GLib.Variant("d", float(w.player.volume)),
            "Position": GLib.Variant("x", int(pos[0] * 1e6) if pos else 0),
            "MinimumRate": GLib.Variant("d", 0.5), "MaximumRate": GLib.Variant("d", 2.0),
            "CanGoNext": GLib.Variant("b", True), "CanGoPrevious": GLib.Variant("b", True),
            "CanPlay": GLib.Variant("b", True), "CanPause": GLib.Variant("b", True),
            "CanSeek": GLib.Variant("b", pos is not None),
            "CanControl": GLib.Variant("b", True),
        }

    def _on_get(self, _conn, _sender, _path, iface, prop):
        return self._props(iface).get(prop)

    def _on_set(self, _conn, _sender, _path, iface, prop, value):
        if prop == "Volume":
            GLib.idle_add(lambda: (self.win.vol.set_value(max(0.0, min(1.0, value.unpack()))),
                                   False)[1])
        elif prop == "Rate":
            GLib.idle_add(lambda: (self.win.player.set_rate(value.unpack()), False)[1])
        return True

    # -- updates from the window ---------------------------------------------
    def update(self, **state):
        """Tell media controls about a new channel / programme / song / state."""
        changed = {}
        if state and state != {k: self.state.get(k) for k in state}:
            if state.get("title") != self.state.get("title"):
                state["track"] = self.state.get("track", 0) + 1
            self.state.update(state)
            changed["Metadata"] = GLib.Variant("a{sv}", build_metadata(self.state))
        status = self.win.mpris_status()
        if status != self.state.get("_status"):
            self.state["_status"] = status
            changed["PlaybackStatus"] = GLib.Variant("s", status)
        if changed and self.conn is not None:
            self.conn.emit_signal(None, PATH, "org.freedesktop.DBus.Properties",
                                  "PropertiesChanged",
                                  GLib.Variant("(sa{sv}as)", (IFACE_PLAYER, changed, [])))
