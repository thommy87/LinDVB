"""The channel list's widgets: the filter steps and the cost of a click or a
scroll with a long list (GTK, skipped without it)."""

import time
import types

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk
except (ValueError, ImportError):
    pytest.skip("GTK 4 is not available", allow_module_level=True)
if not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from lindvb import listfilter  # noqa: E402
from lindvb.channels import Channel  # noqa: E402
from lindvb.ui.common import Row  # noqa: E402
from lindvb.ui.stepmenu import Section, StepMenu  # noqa: E402


def _idle():
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def test_step_menu_ticks_and_radio_entries():
    calls = []
    step = StepMenu(1, "Source", lambda section, value: calls.append((section.sid, value)))
    ticks = Section("sources", "", (("sat", "Satellite"), ("iptv", "Internet streams")))
    radio = Section("reception", "Reception", (("all", "All"), ("fta", "Free to air")), radio=True)
    step.set_sections([ticks, radio], {"sources": frozenset({"sat"}), "reception": "all"})
    buttons = step._buttons
    assert buttons["sources"]["sat"].get_active() and not buttons["sources"]["iptv"].get_active()
    assert calls == []                       # setting the choices is no change by the user
    buttons["sources"]["iptv"].set_active(True)
    buttons["reception"]["fta"].set_active(True)
    _idle()
    assert calls == [("sources", frozenset({"sat", "iptv"})), ("reception", "fta")]
    # the same sections again keep their widgets (an open popover stays as it is)
    step.set_sections([ticks, radio], {"sources": frozenset(), "reception": "all"})
    assert step._buttons is buttons and not buttons["sources"]["sat"].get_active()
    step.set_sections([ticks], {})
    assert step._buttons is not buttons and list(step._buttons) == ["sources"]


def test_a_click_refilters_twenty_thousand_rows_quickly():
    kinds = [dict(delsys="dvb-s2", root="Astra-19.2E"), dict(delsys="dvb-s2", root="Hotbird-13.0E"),
             dict(delsys="dvb-c-a", root="Kabel"),
             dict(delsys="iptv", url="http://tvh/x", tvh_server="http://tvh", groups=["News"])]
    chans = [Channel(name=f"C {n} {'HD' if n % 3 else ''}", number=n, encrypted=n % 5 == 0,
                     **kinds[n % len(kinds)]) for n in range(20000)]
    infos = {id(c): listfilter.info(c) for c in chans}
    state = {"st": listfilter.State()}
    store = Gio.ListStore(item_type=Row)
    store.splice(0, 0, [Row(c) for c in chans])
    flt = Gtk.CustomFilter.new(lambda r: listfilter.matches(infos[id(r.data)], state["st"]))
    model = Gtk.FilterListModel(model=store, filter=flt)
    win = Gtk.Window(default_width=300, default_height=600)
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _f, li: li.set_child(Gtk.Label()))
    factory.connect("bind", lambda _f, li: li.get_child().set_text(li.get_item().data.name))
    sw = Gtk.ScrolledWindow(child=Gtk.ListView(model=Gtk.NoSelection(model=model),
                                               factory=factory))
    win.set_child(sw)
    win.present()
    _idle()
    worst = 0
    for st in (listfilter.State(sources=frozenset({"sat"}), areas=frozenset({"sat/pos:130"})),
               listfilter.State(reception="fta", qualities=frozenset({"HD"})),
               listfilter.State(tags=frozenset({"News"})), listfilter.State()):
        state["st"] = st
        t = time.perf_counter()
        flt.changed(Gtk.FilterChange.DIFFERENT)
        _idle()
        worst = max(worst, time.perf_counter() - t)
    assert model.get_n_items() == len(chans)
    win.destroy()
    assert worst < 0.1, f"a refilter took {worst * 1000:.0f} ms"


def test_scrolling_reads_each_logo_once(monkeypatch):
    """A row bound again while scrolling takes its logo from the cache: the
    picon folder is not searched again for every row shown."""
    from lindvb import picons
    from lindvb.ui.window import MainWindow
    looked = []
    monkeypatch.setattr(picons, "find", lambda c, folders: looked.append(c.name) or "")
    win = types.SimpleNamespace(settings={"logo_dir": ""}, _logo_paths={})
    win._channel_logo = lambda c: MainWindow._channel_logo(win, c)
    chans = [Channel(name=f"C {n}") for n in range(50)]
    for _scroll in range(5):
        for c in chans:
            MainWindow._list_logo(win, c)
    assert len(looked) == len(chans)


def _big_list(n=12000):
    store = Gio.ListStore(item_type=Row)
    store.splice(0, 0, [Row(Channel(name=f"C {i}", number=i)) for i in range(1, n + 1)])
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _f, li: li.set_child(Gtk.Label()))
    factory.connect("bind", lambda _f, li: li.get_child().set_text(li.get_item().data.name))
    sel = Gtk.SingleSelection(model=store, autoselect=False)
    return store, sel, Gtk.ListView(model=sel, factory=factory)


def _bound_names(lv):
    out, w = [], lv.get_first_child()
    while w is not None:
        lbl = w.get_first_child()
        if w.get_mapped() and isinstance(lbl, Gtk.Label) and lbl.get_text():
            out.append(lbl.get_text())
        w = w.get_next_sibling()
    return out


def test_the_list_scrolls_to_the_channel_once_it_is_on_screen():
    """The channel tuned at the start is asked for before the window is laid
    out: the list waits for its first frame, then shows the rows around it
    (scrolled at once it stayed at the top with only its first row)."""
    from lindvb.ui.window import MainWindow
    store, sel, lv = _big_list()
    win = Gtk.Window(default_width=300, default_height=600)
    win.set_child(Gtk.ScrolledWindow(child=lv))
    fake = types.SimpleNamespace(listview=lv, filtered=store, selection=sel,
                                 _scroll_tick=0, _scroll_map=0)
    for name in ("_scroll_list_mapped", "_scroll_list_tick"):
        setattr(fake, name, getattr(MainWindow, name).__get__(fake))
    sel.set_selected(8361)                          # before the window is shown
    MainWindow._scroll_to_selected(fake)
    win.present()
    end = time.monotonic() + 5
    while "C 8362" not in _bound_names(lv) and time.monotonic() < end:
        GLib.MainContext.default().iteration(True)
    names = _bound_names(lv)
    win.destroy()
    assert "C 8362" in names and len(names) > 5, names[:5]
    assert fake._scroll_tick == 0 and fake._scroll_map == 0


def test_rows_with_and_without_logo_are_equally_high():
    """A row whose logo is missing (a symbol instead) is as high as one with
    a logo: rows changing height while a long list scrolls make it shake."""
    from lindvb.ui.window import MainWindow
    fake = types.SimpleNamespace(_row_tooltip=lambda *a: False,
                                 _row_drag_prepare=lambda *a: None, _row_drop=lambda *a: False)
    heights = []
    for logo in (True, False):
        li = types.SimpleNamespace(set_child=lambda b: setattr(li, "child", b))
        MainWindow._row_setup(fake, None, li)
        icon = li.child.get_first_child().get_next_sibling()
        c = Channel(name="X", delsys="iptv", url="http://x")
        if logo:
            pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 38)
            MainWindow._set_row_icon(fake, icon, c, Gdk.Texture.new_for_pixbuf(pb))
        else:
            MainWindow._set_row_icon(fake, icon, c, None)
        heights.append(li.child.measure(Gtk.Orientation.VERTICAL, 280)[1])
    assert heights[0] == heights[1] >= 32


def test_the_picture_seen_sets_the_quality():
    """A few seconds after a channel starts, its picture size is its quality:
    the list forgets what it had worked out for it and saves the channels
    once, a little later (not for every channel zapped through)."""
    from lindvb.ui.window import MainWindow
    c = Channel(name="Das Erste HD")
    saved = []
    win = types.SimpleNamespace(
        current=c, playlist=[], _channels_due=None,
        player=types.SimpleNamespace(playbin=object(),
                                     video_info=lambda: {"width": 720, "height": 576}),
        _infos={id(c): listfilter.info(c)}, _catalogues={"tv": ()},
        app=types.SimpleNamespace(db=types.SimpleNamespace(save=lambda: saved.append(1))))
    win._save_channels = lambda: MainWindow._save_channels(win)
    win._channel_learnt = lambda ch: MainWindow._channel_learnt(win, ch)
    assert MainWindow._check_video(win) is False
    assert c.seen_video == "SD" and listfilter.info(c).quality == "SD"
    assert win._infos == {} and win._catalogues == {} and win._channels_due
    due = win._channels_due
    MainWindow._check_video(win)                 # nothing new: no second save planned
    assert win._channels_due == due and saved == []
    win._save_channels()
    assert saved == [1] and win._channels_due is None
    c.radio = True                               # radio has no picture to learn from
    win.player.video_info = lambda: {"width": 1920, "height": 1080}
    MainWindow._check_video(win)
    assert c.seen_video == "SD" and win._channels_due is None


def test_the_sdt_sets_the_service_type():
    """The service type the SDT gives the channel playing reaches the list
    channel, also when a DVB-I instance plays it; another channel's capture
    changes nothing."""
    from lindvb.ui.window import MainWindow
    c, instance, other = Channel(name="A"), Channel(name="A (DVB-T)"), Channel(name="B")
    learnt = []
    win = types.SimpleNamespace(current=c, _tuned=instance,
                                _channel_learnt=learnt.append)
    MainWindow._on_service_type(win, types.SimpleNamespace(channel=other), 0x19)
    assert c.service_type == 0 and learnt == []
    MainWindow._on_service_type(win, types.SimpleNamespace(channel=instance), 0x19)
    assert c.service_type == 0x19 and learnt == [c]
