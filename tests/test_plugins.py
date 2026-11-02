"""Tests for the plugin loader and dispatch (no GStreamer, no GTK)."""

import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ["LINDVB_NO_BUNDLED_PLUGINS"] = "1"   # only the test plugins

from lindvb.plugins import PluginManager  # noqa: E402

PLUGIN_SRC = textwrap.dedent('''
    NAME = "Test plugin"
    VERSION = "9.9"

    def _load_foo(path):
        return [("channel-from", path)]

    class _Provider:
        name = "testcam"
        def __init__(self, settings):
            self.settings = settings

    def register(api):
        api.add_importer("Foo lists (*.foo)", ".foo", _load_foo)
        api.add_ca_provider("testcam", lambda settings: _Provider(settings))
        api.add_hook("startup", lambda: events.append("startup"))
        api.add_menu_action("Do a thing", lambda app: events.append(("menu", app)))

    events = []
''')


def _manager(tmp_path, settings=None):
    d = tmp_path / "plugins"
    d.mkdir()
    (d / "myplugin.py").write_text(PLUGIN_SRC)
    os.environ["LINDVB_PLUGIN_PATH"] = str(d)
    logs = []
    mgr = PluginManager({} if settings is None else settings, log=logs.append)
    return mgr, logs


def test_discover_and_load(tmp_path):
    mgr, _ = _manager(tmp_path)
    names = [name for name, _p in mgr.discover()]
    assert "myplugin" in names
    mgr.load_all()
    assert any(p.name == "Test plugin" and p.version == "9.9" for p in mgr.plugins)


def test_importer_registration(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.load_all()
    loader = mgr.importer_for("/tmp/list.foo")
    assert loader is not None
    assert loader("/tmp/list.foo") == [("channel-from", "/tmp/list.foo")]
    assert mgr.importer_for("/tmp/list.m3u") is None


def test_ca_provider_from_plugin(tmp_path):
    mgr, _ = _manager(tmp_path, {"ca_provider": "testcam"})
    mgr.load_all()
    provider = mgr.make_ca_provider()
    assert provider is not None and provider.name == "testcam"


def test_disabled_plugin_not_loaded(tmp_path):
    mgr, _ = _manager(tmp_path, {"plugins_disabled": ["myplugin"]})
    mgr.load_all()
    assert mgr.plugins == []


def test_hooks_and_menu(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.load_all()
    plugin = mgr.plugins[0]
    mgr.emit("startup")
    assert "startup" in plugin.module.events
    assert len(mgr.menu_actions) == 1


def test_broken_plugin_does_not_raise(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    (d / "broken.py").write_text("raise RuntimeError('boom')\n")
    os.environ["LINDVB_PLUGIN_PATH"] = str(d)
    logs = []
    mgr = PluginManager({}, log=logs.append)
    mgr.load_all()                             # must not raise
    assert any("broken" in line for line in logs)


def test_none_provider(tmp_path):
    mgr, _ = _manager(tmp_path, {"ca_provider": "none"})
    mgr.load_all()
    assert mgr.make_ca_provider() is None


def test_enable_disable(tmp_path):
    settings = {}
    mgr, _ = _manager(tmp_path, settings)
    assert mgr.is_enabled("myplugin")
    mgr.set_enabled("myplugin", False)
    assert settings["plugins_disabled"] == ["myplugin"] and not mgr.is_enabled("myplugin")
    mgr.set_enabled("myplugin", True)
    assert settings["plugins_disabled"] == [] and mgr.is_enabled("myplugin")


def test_failed_plugin_recorded(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    (d / "broken.py").write_text("import no_such_module_xyz\n")
    os.environ["LINDVB_PLUGIN_PATH"] = str(d)
    mgr = PluginManager({}, log=lambda m: None)
    mgr.load_all()
    assert "no_such_module_xyz" in mgr.failed["broken"]


def test_visualizer_registration(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    (d / "vis.py").write_text(textwrap.dedent('''
        class Style:
            def draw(self, cr, w, h, frame):
                pass

        def register(api):
            api.add_visualizer("Drawn", draw=Style)
            api.add_visualizer("Element", element=lambda: None)
            try:
                api.add_visualizer("Both", draw=Style, element=lambda: None)
            except Exception:
                pass
    '''))
    os.environ["LINDVB_PLUGIN_PATH"] = str(d)
    mgr = PluginManager({}, log=lambda m: None)
    mgr.load_all()
    assert [(n, k) for _p, n, k, _f in mgr.visualizers] == [("Drawn", "draw"), ("Element", "element")]


def test_bundled_addon_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("LINDVB_NO_BUNDLED_PLUGINS", raising=False)
    monkeypatch.setenv("LINDVB_PLUGIN_PATH", str(tmp_path))
    mgr = PluginManager({}, log=lambda m: None)
    names = [n for n, _p in mgr.discover()]
    assert "gst_visualizers" in names
    mgr.load_all()                      # with or without GStreamer, it must not fail
    assert "gst_visualizers" not in mgr.failed
