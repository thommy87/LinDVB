"""HbbTV terminal logic: keys, key sets and the application lifecycle."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb import ait, hbbtv  # noqa: E402
from lindvb.ait import Ait, AitApp, Transport  # noqa: E402


def _ait(*apps, version=1):
    a = Ait(0x10, version, transports=[Transport(ait.PROTOCOL_HTTP, 1, ["http://tv.example/"])])
    a.apps = list(apps)
    return a


def _app(aid, code=ait.AUTOSTART, bound=True, loc="index.html"):
    return AitApp(1, aid, code, labels=[1], location=loc, service_bound=bound)


class Browser:
    def __init__(self):
        self.calls = []

    def launch(self, url, app):
        self.calls.append(("launch", url, app.app_id if app else None))

    def kill(self):
        self.calls.append(("kill",))


def _life(autostart=True):
    b = Browser()
    return hbbtv.Lifecycle(b.launch, b.kill, autostart=autostart), b


def test_user_agent_names_hbbtv_2_0_4():
    assert "HbbTV/1.7.1 (" in hbbtv.user_agent("0.5.0")


def test_keys():
    assert hbbtv.key_code("F5") == 403 and hbbtv.key_code("XF86Blue") == 406
    assert hbbtv.key_code("KP_7") == 55 and hbbtv.key_code("x") == 0
    assert hbbtv.wanted(403, hbbtv.RED)
    assert not hbbtv.wanted(404, hbbtv.RED)
    assert hbbtv.wanted(37, hbbtv.NAVIGATION) and not hbbtv.wanted(37, hbbtv.VCR)
    assert hbbtv.wanted(459, hbbtv.OTHER, (459,)) and not hbbtv.wanted(459, hbbtv.OTHER)


def test_autostart_and_ait_updates():
    life, b = _life()
    life.on_ait(_ait(_app(1)))
    assert b.calls == [("launch", "http://tv.example/index.html", 1)]
    life.on_ait(_ait(_app(1), version=2))                 # still signalled: keeps running
    assert len(b.calls) == 1
    life.on_ait(_ait(_app(1, ait.KILL), version=3))
    assert b.calls[-1] == ("kill",) and not life.active


def test_no_autostart_waits_for_red():
    life, b = _life(autostart=False)
    life.on_ait(_ait(_app(1)))
    assert b.calls == []
    assert life.red_button()
    assert b.calls == [("launch", "http://tv.example/index.html", 1)]


def test_user_exit_is_remembered_on_the_service():
    life, b = _life()
    a = _ait(_app(1))
    life.on_ait(a)
    life.stop(by_user=True)
    life.on_ait(_ait(_app(1), version=2))
    assert b.calls == [("launch", "http://tv.example/index.html", 1), ("kill",)]
    life.channel_changed()                                # a new service forgets it
    life.on_ait(a)
    assert b.calls[-1][0] == "launch"


def test_channel_change_ends_service_bound_apps_only():
    life, b = _life()
    life.on_ait(_ait(_app(1, bound=False)))
    life.channel_changed()
    assert life.active                                   # waits for the new AIT
    life.on_ait(_ait(_app(1, bound=False)))
    assert life.active and b.calls.count(("kill",)) == 0
    life.channel_changed()
    life.on_ait(_ait(_app(2)))                           # app 1 is not signalled there
    assert b.calls[-2:] == [("kill",), ("launch", "http://tv.example/index.html", 2)]
    life.channel_changed()
    assert b.calls[-1] == ("kill",)                      # service bound


def test_broadcast_independent_app_survives_ait_changes():
    life, b = _life()
    life.on_ait(_ait(_app(1)))
    life.create_application("https://mediathek.example/start")
    assert life.running is None and life.active
    life.on_ait(_ait(_app(1), version=2))
    assert b.calls[-1] == ("launch", "https://mediathek.example/start", None)
    life.no_signalling()
    assert not life.active


def test_capabilities_document():
    xml = hbbtv.capabilities_xml()
    assert xml.startswith("<profilelist>") and "OITF_HD_UIPROF+DVB_S" in xml
    assert 'type="application/dash+xml"' in xml


def test_viewer_language():
    assert hbbtv.viewer_language({"hbbtv_language": "fra"}) == "fra"
    assert hbbtv.viewer_language({"rec_audio_language": "eng"}) == "eng"
    assert hbbtv.viewer_language({"language": "de"}) == "deu"
    assert hbbtv.viewer_language({}, "nl") == "nld"
    assert hbbtv.viewer_language({}) == "deu"


def test_broadcast_components():
    from lindvb.media_info import AudioTrack
    comps = hbbtv.broadcast_components([AudioTrack("deu"), AudioTrack("eng", "Audiodescription")], 1,
                                       [("Teletext 150 (deu)", "deu")], -1)
    assert [(c["type"], c["language"], c["active"]) for c in comps] == \
        [(0, "", True), (1, "deu", False), (1, "eng", True), (2, "deu", False)]
    assert comps[2]["audioDescription"]


def test_sandbox_probe(monkeypatch):
    class R:
        def __init__(self, code):
            self.returncode = code
    for code, ok in ((0, True), (1, False)):
        monkeypatch.setattr(hbbtv, "_SANDBOX", None)
        assert hbbtv.sandbox_ok(run=lambda *a, **k: R(code), which=lambda n: "/usr/bin/bwrap",
                                env={}) is ok
    monkeypatch.setattr(hbbtv, "_SANDBOX", None)
    assert hbbtv.sandbox_ok(run=None, which=lambda n: None, env={})
    monkeypatch.setattr(hbbtv, "_SANDBOX", None)
    assert hbbtv.sandbox_ok(run=None, which=None, env={"FLATPAK_ID": "x"})
    monkeypatch.setattr(hbbtv, "_SANDBOX", None)


def test_parental_ratings():
    assert hbbtv.rating_age(0x09) == 12 and hbbtv.rating_age(0) == 0 and hbbtv.rating_age(0x20) == 0
    assert hbbtv.min_age([("FRA", 0x0F), ("DEU", 0x0D)]) == 16
    assert hbbtv.min_age([("FRA", 0x09)]) == 12
    assert hbbtv.needs_pin([("DEU", 0x0D)], 12) and not hbbtv.needs_pin([("DEU", 0x0D)], 16)
    assert not hbbtv.needs_pin([("DEU", 0x0F)], 0)


def test_pin_guard_locks_after_wrong_tries():
    salt = hbbtv.new_salt()
    now = [0.0]
    g = hbbtv.PinGuard({"hbbtv_pin_salt": salt, "hbbtv_pin": hbbtv.hash_pin("1234", salt)},
                       clock=lambda: now[0])
    assert g.verify("1234") == 0
    assert g.verify("0000") == 1 and g.verify("1111") == 1
    assert g.verify("2222") == 2 and g.verify("1234") == 2      # locked, even the right one
    now[0] += hbbtv.PIN_LOCK_SECONDS + 1
    assert g.verify("1234") == 0
    assert hbbtv.PinGuard({}).verify("") == 1                    # no PIN set


def test_rated_app_needs_the_pin():
    life, b = _life()
    asked = []
    life.allowed = lambda app: not hbbtv.needs_pin(app.parental, 12)
    life.on_blocked = lambda app, start: asked.append((app.app_id, start))
    rated = _app(1)
    rated.parental = [("DEU", 0x0D)]
    life.on_ait(_ait(rated))
    assert b.calls == [] and asked == []                  # never by itself
    assert life.red_button() and asked[0][0] == 1 and b.calls == []
    asked[0][1]()                                         # the right PIN
    assert b.calls == [("launch", "http://tv.example/index.html", 1)]


def test_teletext_key_starts_the_teletext_app():
    life, b = _life(autostart=False)
    tt = _app(7, ait.PRESENT, loc="ttx.html")
    tt.usage = ait.USAGE_TELETEXT
    life.on_ait(_ait(_app(1), tt))
    assert life.teletext_button()
    assert b.calls == [("launch", "http://tv.example/ttx.html", 7)]
