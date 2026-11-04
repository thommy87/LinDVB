# SPDX-License-Identifier: GPL-2.0-or-later
"""Picture in picture and the channel mosaic (internet channels).

Both play extra channels without sound next to the main picture. Each tile is
its own GStreamer playbin, so they need bandwidth but no tuner: for now they
take internet channels (IPTV, DVB-I streams). DVB channels need a second
tuner and are left out.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst, Gtk, Pango  # noqa: E402

from ..i18n import _  # noqa: E402
from ..player import VideoBin  # noqa: E402

MOSAIC_TILES = 9


def can_show(channel):
    """Picture in picture / mosaic tiles take internet channels only (no tuner)."""
    return bool(channel is not None and channel.is_iptv and channel.url)


class MiniPlayer:
    """A muted playbin that draws into a Gtk.Picture."""

    def __init__(self, picture):
        self.picture = picture
        self.playbin = None
        self.channel = None
        self.on_error = None

    def play(self, channel):
        self.stop()
        self.channel = channel
        pb = Gst.ElementFactory.make("playbin", None)
        if pb is None:
            return False
        pb.set_property("uri", channel.url if "://" in channel.url
                        else Gst.filename_to_uri(channel.url))
        pb.set_property("video-sink", VideoBin(self.picture).sink)
        fake = Gst.ElementFactory.make("fakesink", None)
        if fake is not None:
            fake.set_property("sync", True)
            pb.set_property("audio-sink", fake)       # no sound, keeps the clock running
        pb.set_property("mute", True)
        bus = pb.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self.playbin = pb
        return pb.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE

    def _on_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        if self.on_error:
            self.on_error(err.message)
        self.stop()

    def stop(self):
        if self.playbin is not None:
            self.playbin.set_state(Gst.State.NULL)
            self.playbin.get_bus().remove_signal_watch()
            self.playbin = None


class PipOverlay(Gtk.Box):
    """The small picture in the corner of the video, with swap and close buttons."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END,
                         valign=Gtk.Align.END, margin_end=18, margin_bottom=18,
                         css_classes=["lv-pip"], visible=False)
        self.window = window
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                                   hexpand=True, vexpand=True)
        over = Gtk.Overlay(hexpand=True, vexpand=True)
        over.set_child(self.picture)
        bar = Gtk.Box(spacing=4, halign=Gtk.Align.FILL, valign=Gtk.Align.START,
                      css_classes=["lv-pip-bar"])
        self.label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                               margin_start=6)
        swap = Gtk.Button(icon_name="object-flip-horizontal-symbolic", css_classes=["flat"],
                          tooltip_text=_("Swap with the main picture"))
        swap.connect("clicked", lambda _b: window.pip_swap())
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"],
                           tooltip_text=_("Close picture in picture"))
        close.connect("clicked", lambda _b: window.pip_close())
        for w in (self.label, swap, close):
            bar.append(w)
        over.add_overlay(bar)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, n, _x, _y: n == 2 and window.pip_swap())
        over.add_controller(click)
        self.append(over)
        self.player = MiniPlayer(self.picture)
        self.player.on_error = lambda m: window.set_status(
            _("Picture in picture: {error}").format(error=m))

    @property
    def channel(self):
        return self.player.channel if self.get_visible() else None

    def show_channel(self, channel):
        if not can_show(channel):
            self.window.set_status(_("Picture in picture works with internet channels; "
                                     "DVB channels need a second tuner."))
            return False
        self.label.set_text(channel.name)
        self.set_visible(True)
        return self.player.play(channel)

    def close(self):
        self.player.stop()
        self.player.channel = None
        self.set_visible(False)


class MosaicWindow(Gtk.Window):
    """Up to nine internet channels at once; click one to watch it."""

    def __init__(self, app, parent, channels):
        super().__init__(title=_("Channel Mosaic"), transient_for=parent, modal=False,
                         default_width=1100, default_height=680)
        self.app = app
        self.channels = [c for c in channels if can_show(c)]
        self.page = 0
        self.players = []
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=8,
                      margin_end=8, margin_top=8, margin_bottom=8)
        bar = Gtk.Box(spacing=6)
        self.prev_b = Gtk.Button(icon_name="go-previous-symbolic",
                                 tooltip_text=_("Previous channels"))
        self.next_b = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Next channels"))
        self.prev_b.connect("clicked", lambda _b: self.show_page(self.page - 1))
        self.next_b.connect("clicked", lambda _b: self.show_page(self.page + 1))
        self.info = Gtk.Label(hexpand=True, xalign=0, css_classes=["lv-count"])
        for w in (self.prev_b, self.next_b, self.info):
            bar.append(w)
        box.append(bar)
        self.grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True, row_spacing=6,
                             column_spacing=6, vexpand=True)
        box.append(self.grid)
        self.empty = Gtk.Label(wrap=True, vexpand=True, css_classes=["lv-count"], visible=False,
                               label=_("The mosaic shows internet channels. The list has none "
                                       "in the current view."))
        box.append(self.empty)
        self.set_child(box)
        self.connect("close-request", lambda *_a: (self.stop(), False)[1])
        self.show_page(0)

    def pages(self):
        return max(1, -(-len(self.channels) // MOSAIC_TILES))

    def stop(self):
        for p in self.players:
            p.stop()
        self.players = []

    def show_page(self, page):
        self.stop()
        child = self.grid.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.grid.remove(child)
            child = nxt
        self.page = max(0, min(page, self.pages() - 1))
        chans = self.channels[self.page * MOSAIC_TILES:(self.page + 1) * MOSAIC_TILES]
        self.empty.set_visible(not chans)
        self.grid.set_visible(bool(chans))
        cols = 3 if len(chans) > 4 else 2 if len(chans) > 1 else 1
        for i, c in enumerate(chans):
            self.grid.attach(self._tile(c), i % cols, i // cols, 1, 1)
        self.prev_b.set_sensitive(self.page > 0)
        self.next_b.set_sensitive(self.page < self.pages() - 1)
        self.info.set_text(_("Page {page} of {pages} · {n} internet channels").format(
            page=self.page + 1, pages=self.pages(), n=len(self.channels)))

    def _tile(self, c):
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, css_classes=["video"],
                          hexpand=True, vexpand=True)
        over = Gtk.Overlay(css_classes=["lv-mosaic-tile"])
        over.set_child(pic)
        name = Gtk.Label(label=c.name, halign=Gtk.Align.START, valign=Gtk.Align.END,
                         margin_start=6, margin_bottom=4, css_classes=["lv-mosaic-name"])
        over.add_overlay(name)
        over.set_tooltip_text(_("Click to watch {name}").format(name=c.name))
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: self._watch(c))
        over.add_controller(click)
        p = MiniPlayer(pic)
        p.on_error = lambda m, lbl=name, ch=c: lbl.set_text(f"{ch.name} – {m}")
        GLib.idle_add(lambda: (p.play(c), False)[1])
        self.players.append(p)
        return over

    def _watch(self, c):
        self.stop()
        self.app.window.zap(c)
        self.close()
