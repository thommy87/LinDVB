"""The channel list's widgets: the filter steps and the cost of a click or a
scroll with a long list (GTK, skipped without it)."""

import time
import types

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gio, GLib, Gtk
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
