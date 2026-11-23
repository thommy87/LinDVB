# SPDX-License-Identifier: GPL-2.0-or-later
"""Stage C, part 1 (concept section 9): the DVB-I country list with all DVB
countries, the question before a country change removes an installed list,
and the HbbTV country as a setting of its own."""

import json
import re
import types

import pytest

from lindvb import config, dvbi, hbbtv, i18n
from lindvb import dvbi_countries as countries
from lindvb.channels import Channel

from test_dvbi import SCANNED, SERVICE_LIST as LIST


@pytest.fixture(autouse=True)
def _german_languages():
    """Choosing a country sets the preferred languages of lindvb.dvbi; the
    other tests expect the German default."""
    yield
    dvbi.use_country("DEU")


# ---------------------------------------------------------------- the country list

def test_the_list_has_every_dvb_country_once():
    rows = countries.country_choices()
    codes = [c for c, _n, _ok in rows]
    assert len(codes) == len(set(codes)) > 100
    assert all(re.fullmatch(r"[A-Z]{3}", c) for c in codes)
    assert codes[-1] == countries.OTHER.code               # own registry or list, last
    ok = {c for c, _n, avail in rows if avail}
    assert ok == {"DEU", "ITA", "NZL", "ZZZ"}               # the countries with a profile
    for code in ("AUT", "CHE", "FRA", "GBR", "AUS", "ESP", "POL"):
        assert code in codes and code not in ok
    a2 = [a for a, _b, _n in countries.DVB_COUNTRIES]
    assert len(a2) == len(set(a2)) and all(re.fullmatch(r"[A-Z]{2}", a) for a in a2)


def test_country_names_are_sorted_and_translated():
    try:
        i18n.setup("de")
        rows = countries.country_choices()
        names = [n for _c, n, _ok in rows[:-1]]
        assert names == sorted(names, key=countries._sort_key)
        assert countries.country_name("DEU") == "Deutschland"
        assert countries.country_name("AUT") == "Österreich"
        # Österreich sorts with O, not after Z
        assert names.index("Österreich") < names.index("Polen")
    finally:
        i18n.setup("en")
    assert countries.country_name("AUT") == "Austria"


@pytest.mark.parametrize("po", sorted(i18n.PO_DIR.glob("*.po")), ids=lambda p: p.stem)
def test_every_catalogue_names_every_country(po):
    cat = i18n.parse_po(po.read_text(encoding="utf-8"))
    missing = [n for _a, _b, n in countries.DVB_COUNTRIES if n not in cat]
    assert not missing, (po.stem, missing)


@pytest.mark.parametrize("env, code", [
    ({"LANG": "de_AT.UTF-8"}, "AUT"), ({"LANG": "it_IT.UTF-8"}, "ITA"),
    ({"LC_ALL": "fr_CH.UTF-8", "LANG": "de_DE.UTF-8"}, "CHE"),
    ({"LANG": "en_GB.UTF-8"}, "GBR"), ({"LANG": "en_NZ"}, "NZL"),
    ({"LANG": "en_US.UTF-8"}, "DEU"),          # not a DVB country: as before
    ({"LANG": "C.UTF-8"}, "DEU"), ({}, "DEU"), ({"LANG": "de"}, "DEU"),
])
def test_system_country(env, code):
    assert countries.system_country(env) == code


# ---------------------------------------------------------------- HbbTV country

def _settings(tmp_path, monkeypatch, saved):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(saved))
    monkeypatch.setattr(config.Settings, "path", path)
    return config.Settings()


def test_old_hbbtv_country_from_the_dvbi_country_is_dropped(tmp_path, monkeypatch):
    """Before 0.23 the DVB-I country set the HbbTV country (Italy for the
    DVB-I list made German satellite channels Italian); it could not be
    chosen, so the desktop's country takes over."""
    s = _settings(tmp_path, monkeypatch, {"dvbi_country": "ITA", "hbbtv_country": "ITA",
                                          "dvbi_list_url": "https://example.org/it.xml"})
    assert s["hbbtv_country"] == config.DEFAULTS["hbbtv_country"] == countries.system_country()
    assert s["dvbi_country"] == "ITA" and s["dvbi_list_url"]      # DVB-I stays as it was


def test_chosen_hbbtv_country_is_kept(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, {"hbbtv_country": "AUT", "hbbtv_country_set": True})
    assert s["hbbtv_country"] == "AUT"


def test_saved_dvbi_country_stays_chosen(tmp_path, monkeypatch):
    """Existing settings keep their country; only a new installation starts
    without one."""
    assert config.DEFAULTS["dvbi_country"] == ""
    s = _settings(tmp_path, monkeypatch, {"dvbi_country": "DEU"})
    assert s["dvbi_country"] == "DEU"


def test_switch_country_leaves_the_hbbtv_country_alone():
    s = {"dvbi_country": "DEU", "hbbtv_country": "AUT"}
    countries.switch_country(s, "NZL")
    assert s["dvbi_country"] == "NZL" and s["hbbtv_country"] == "AUT"


def test_country_for_the_channel_on_air():
    s = {"hbbtv_country": "AUT", "dvbi_country": "ITA"}
    sat = Channel(name="ZDF", delsys="dvb-s2")
    assert hbbtv.country_for(s, sat) == "AUT"
    assert hbbtv.country_for(s, None) == "AUT"
    # a DVB-I channel: the country of its list ...
    assert hbbtv.country_for(s, Channel(name="Rai 1", dvbi_id="tag:rai1",
                                        dvbi_country="ITA")) == "ITA"
    assert hbbtv.country_for({"hbbtv_country": "ITA"},
                             Channel(name="Das Erste", dvbi_id="tag:ard",
                                     dvbi_country="DEU")) == "DEU"
    # ... for a list from before 0.23 the DVB-I country (the list was its block)
    assert hbbtv.country_for(s, Channel(name="Rai 2", dvbi_id="tag:rai2")) == "ITA"
    # own list without a country: the HbbTV setting
    assert hbbtv.country_for({"hbbtv_country": "AUT", "dvbi_country": "ZZZ"},
                             Channel(name="X", dvbi_id="tag:x")) == "AUT"
    assert hbbtv.country_for({}, sat) == "DEU"


def test_channels_know_the_country_of_their_list():
    sl = dvbi.parse_service_list(LIST)
    res = dvbi.build_channels(sl, "DE-BY", SCANNED, "Deutschland", country="DEU")
    assert res.channels and all(c.dvbi_country == "DEU" for c in res.channels)
    res = dvbi.build_channels(sl, "DE-BY", SCANNED, "Deutschland")
    assert all(c.dvbi_country == "" for c in res.channels)
    # it survives saving and loading the channel list
    c = res.channels[0]
    c.dvbi_country = "ITA"
    from dataclasses import asdict
    assert Channel.from_dict(json.loads(json.dumps(asdict(c)))).dvbi_country == "ITA"


# ---------------------------------------------------------------- the DVB-I panel

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402


class FakeSettings(dict):
    saved = 0

    def save(self):
        self.saved += 1


class FakeDB:
    def __init__(self, n=0):
        self.channels = [Channel(name=f"S{i}", dvbi_id=f"tag:{i}", dvbi_country="DEU")
                         for i in range(n)]
        self.saved = 0

    def dvbi_channels(self):
        return [c for c in self.channels if c.dvbi_id]

    def replace_dvbi(self, new):
        removed = len(self.dvbi_channels())
        self.channels = [c for c in self.channels if not c.dvbi_id] + list(new)
        return len(new), 0, removed

    def remove_dvbi_block(self, block):
        gone = [c for c in self.channels if c.dvbi_id and c.dvbi_block == block]
        self.channels = [c for c in self.channels if c not in gone]
        return len(gone)

    def save(self):
        self.saved += 1


@pytest.fixture
def panel_for(monkeypatch):
    yield panel_factory(monkeypatch)


def panel_factory(monkeypatch):
    """make(country, n, **settings) -> a DVBIPanel with fake settings and
    channel list; the registry is not asked."""
    if not Gtk.init_check():
        pytest.skip("no display")
    from lindvb.ui import dvbi as dvbi_ui
    loads = []
    monkeypatch.setattr(dvbi_ui.DVBIPanel, "_load_registry", lambda self: loads.append(
        self.country.code))
    made = []

    def make(country="", n=0, **extra):
        s = FakeSettings(config.DEFAULTS)
        s["dvbi_country"] = country
        s.update(extra)
        app = types.SimpleNamespace(settings=s, db=FakeDB(n), window=None, _dvbi_busy=False)
        p = dvbi_ui.DVBIPanel(app)
        p.loads = loads
        made.append(p)
        return p
    return make


def _choose(panel, code):
    codes = [c for c, _n, _ok in panel.country_rows]
    panel.country_dd.set_selected(codes.index(code))


def test_nothing_but_the_country_list_before_a_country_is_chosen(panel_for):
    p = panel_for("")
    assert p.country_dd.get_selected() == 0 and p.country_rows[0][0] == ""   # Choose…
    assert p.country_dd.get_model().get_string(0) == "Choose…"
    assert not p.body.get_visible() and p.choose_lbl.get_visible()
    p.start()
    assert p.loads == []                         # no registry is asked


def test_saved_country_shows_its_settings(panel_for):
    p = panel_for("DEU", dvbi_list_url="https://example.org/de.xml")
    codes = [c for c, _n, _ok in p.country_rows]
    assert codes[p.country_dd.get_selected()] == "DEU"
    assert p.body.get_visible() and not p.choose_lbl.get_visible()


def test_a_country_without_profile_cannot_be_chosen(panel_for):
    p = panel_for("DEU")
    _choose(p, "AUT")
    codes = [c for c, _n, _ok in p.country_rows]
    assert codes[p.country_dd.get_selected()] == "DEU"
    assert p.s["dvbi_country"] == "DEU" and p.country_status.get_visible()
    assert "Austria" in p.country_status.get_text()


def test_unavailable_countries_are_greyed_out(panel_for):
    p = panel_for("DEU")
    item = types.SimpleNamespace(label=Gtk.Label())
    for code, name, ok in p.country_rows:
        if code in ("AUT", "ITA"):
            fake = types.SimpleNamespace(
                get_item=lambda name=name: Gtk.StringObject.new(name),
                get_child=lambda: item.label)
            p._bind_country(None, fake)
            assert item.label.has_css_class("dim-label") is (not ok)
            assert ("(not available)" in item.label.get_text()) is (not ok)


def test_first_choice_without_channels_switches_at_once(panel_for):
    p = panel_for("")
    _choose(p, "ITA")
    assert p.s["dvbi_country"] == "ITA" and p.country.code == "ITA"
    assert p.body.get_visible()
    assert p.loads == ["ITA"]                    # Italy asks for no consent first
    # "Choose…" leaves the list once the signal is over
    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass
    assert p.country_rows[0][0] != "" and p.country_dd.get_model().get_n_items() == \
        len(p.country_rows)
    codes = [c for c, _n, _ok in p.country_rows]
    assert codes[p.country_dd.get_selected()] == "ITA" and p.s["dvbi_country"] == "ITA"


def test_country_change_with_an_installed_list_asks_first(panel_for, monkeypatch):
    p = panel_for("DEU", n=51, dvbi_list_url="https://example.org/de.xml",
                  dvbi_privacy_ack=True)
    asked = []
    monkeypatch.setattr(p, "_confirm_switch", lambda code, n: asked.append((code, n)))
    _choose(p, "ITA")
    assert asked == [("ITA", 51)]
    assert p.s["dvbi_country"] == "DEU"          # nothing changed yet
    # Cancel: the list goes back, the channels stay
    p._switch_answer("ITA", False)
    codes = [c for c, _n, _ok in p.country_rows]
    assert codes[p.country_dd.get_selected()] == "DEU"
    assert len(p.app.db.dvbi_channels()) == 51 and p.s["dvbi_country"] == "DEU"
    # Switch: the list is removed, Germany's settings are kept for later
    _choose(p, "ITA")
    p._switch_answer("ITA", True)
    assert p.app.db.dvbi_channels() == [] and p.app.db.saved
    assert p.s["dvbi_country"] == "ITA" and p.s["dvbi_list_url"] == ""
    assert p.s["dvbi_country_state"]["DEU"]["dvbi_list_url"] == "https://example.org/de.xml"
    assert "51" in p.status.get_text()
    # back to Germany: no channels any more, so no question, the list comes back
    asked.clear()
    _choose(p, "DEU")
    assert asked == [] and p.s["dvbi_list_url"] == "https://example.org/de.xml"


def test_old_channels_get_the_country_of_their_list():
    """A list from before 0.23: its channels name no country."""
    old = [Channel(name="Das Erste", dvbi_id="tag:ard"), Channel(name="ZDF", dvbi_id="tag:zdf"),
           Channel(name="Sat", delsys="dvb-s2")]
    s = {"dvbi_country": "DEU", "dvbi_list_url": "https://example.org/de.xml"}
    assert countries.label_old_channels(s, old) == 2
    assert [c.dvbi_country for c in old] == ["DEU", "DEU", ""]
    assert countries.label_old_channels(s, old) == 0              # once
    # 0.22 kept the German list when Italy was chosen and none installed for it
    old = [Channel(name="Das Erste", dvbi_id="tag:ard")]
    s = {"dvbi_country": "ITA", "dvbi_list_url": "",
         "dvbi_country_state": {"DEU": {"dvbi_list_url": "https://example.org/de.xml"}}}
    assert countries.installed_country(s, old) == "DEU"
    countries.label_old_channels(s, old)
    assert hbbtv.country_for({"hbbtv_country": "ITA", "dvbi_country": "ITA"}, old[0]) == "DEU"
    # an own list (no country): nothing to label, the HbbTV setting applies
    old = [Channel(name="X", dvbi_id="tag:x")]
    assert countries.label_old_channels({"dvbi_country": "ZZZ", "dvbi_list_url": "u"}, old) == 0
    assert countries.installed_country({"dvbi_country": "ZZZ"}, old) == ""


def test_no_question_when_the_list_is_already_the_new_countrys(panel_for, monkeypatch):
    p = panel_for("ITA", n=5, dvbi_country_state={"DEU": {"dvbi_list_url": "https://de"}})
    asked = []
    monkeypatch.setattr(p, "_confirm_switch", lambda code, n: asked.append((code, n)))
    _choose(p, "DEU")                    # the channels are German (FakeDB)
    assert asked == [] and p.s["dvbi_country"] == "DEU"
    assert len(p.app.db.dvbi_channels()) == 5


def test_the_question_names_both_countries(panel_for, monkeypatch):
    p = panel_for("DEU", n=3)
    shown = {}

    class Dialog:
        def __init__(self, **kw):
            shown.update(kw)

        def choose(self, *a):
            pass
    monkeypatch.setattr(Gtk, "AlertDialog", Dialog)
    p._confirm_switch("ITA", 3)
    assert shown["message"] == ("The list for Germany is installed (3 channels). Remove it "
                                "and switch to Italy?")
    p.app.db.channels = [Channel(name="X", dvbi_id="tag:x")]
    p.s["dvbi_country"] = "ZZZ"
    p._confirm_switch("ITA", 1)
    assert shown["message"].startswith("Your own DVB-I list is installed (1 channels)")
    assert shown["buttons"] == ["Switch", "Cancel"] and shown["cancel_button"] == 1


# ---------------------------------------------------------------- Preferences › HbbTV

def test_prefs_choose_the_hbbtv_country(tmp_path, monkeypatch):
    if not Gtk.init_check():
        pytest.skip("no display")
    from lindvb.channels import ChannelDB
    from lindvb.favourites import FavouriteLists
    from lindvb.plugins import PluginManager
    from lindvb.ui.dialogs import PrefsWindow
    monkeypatch.setattr(config.Settings, "path", tmp_path / "settings.json")
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    settings = config.Settings()
    settings["hbbtv_country"] = "DEU"
    db = ChannelDB()
    app = types.SimpleNamespace(settings=settings, db=db, favs=FavouriteLists(db),
                                plugins=PluginManager(settings), _dvbi_busy=False, window=None)
    win = PrefsWindow(app, None)
    # the guide settings reach into the running app (EPG store); not part of this
    win._epg = types.SimpleNamespace(save=lambda s: None, apply=lambda s: None)
    try:
        codes = [c for c, _n in win._hbbtv_countries]
        assert "ZZZ" not in codes and codes[win._hbbtv_country_drop.get_selected()] == "DEU"
        # saving without a change leaves it to the desktop's country
        win.on_save()
        assert not settings["hbbtv_country_set"]
        win._hbbtv_country_drop.set_selected(codes.index("AUT"))
        win.on_save()
        assert settings["hbbtv_country"] == "AUT" and settings["hbbtv_country_set"]
        assert json.loads((tmp_path / "settings.json").read_text())["hbbtv_country"] == "AUT"
    finally:
        win.destroy()
