"""Accent colour helpers (no display needed)."""

from lindvb.ui import theme


def test_parse_and_format_hex():
    assert theme.parse_hex("#119CFF") == (0x11, 0x9C, 0xFF)
    assert theme.parse_hex("fff") == (255, 255, 255)
    assert theme.parse_hex("nope") is None
    assert theme.to_hex((17, 156, 255)) == "#119CFF"


def test_yaru_variants():
    assert theme.yaru_accent("Yaru-dark") == "#E95420"
    assert theme.yaru_accent("Yaru") == "#E95420"
    assert theme.yaru_accent("Yaru-blue-dark") == "#0073E5"
    assert theme.yaru_accent("Yaru-prussiangreen") == "#308280"
    assert theme.yaru_accent("Adwaita") is None


def test_text_on_accent_is_readable():
    assert theme.on_accent((0x11, 0x9C, 0xFF)) == "#0A0A0C"
    assert theme.on_accent((0x30, 0x10, 0x60)) == "#FFFFFF"


def test_resolve_accent():
    assert theme.resolve_accent({}) == theme.ACCENT
    assert theme.resolve_accent({"accent_mode": "custom", "accent_custom": "#9b59b6"}) == "#9B59B6"
    # A broken custom value falls back to the LinDVB blue.
    assert theme.resolve_accent({"accent_mode": "custom", "accent_custom": "x"}) == theme.ACCENT


def test_accent_css_overrides_theme_unless_system():
    css = theme.accent_css("#119CFF")
    assert "@define-color lv_accent #119CFF;" in css
    assert "theme_selected_bg_color" in css and "row:selected" in css
    plain = theme.accent_css("#E95420", override_theme=False)
    assert "@define-color lv_accent #E95420;" in plain
    assert "row:selected" not in plain


def test_display_css_and_subtitle_font():
    from lindvb.player import subtitle_font_desc
    from lindvb.ui.theme import display_css
    css = display_css({"osd_scale": 150, "subtitle_scale": 200, "subtitle_font": 'Noto "Sans'})
    assert ".osd, .osd-num { font-size: 1.50em; }" in css
    assert ".ttxsub { font-size: 3.00em; }" in css and 'font-family: "Noto Sans"' in css
    assert "font-family" not in display_css({})
    assert display_css({"osd_scale": "x"}).startswith(".osd, .osd-num { font-size: 1.00em; }")
    # GStreamer keeps its own font until the preferences change it
    assert subtitle_font_desc({}) is None
    assert subtitle_font_desc({"subtitle_scale": 150}) == "Sans 27"
    assert subtitle_font_desc({"subtitle_font": "DejaVu Serif"}) == "DejaVu Serif 18"


def test_light_and_dark_schemes():
    from lindvb.ui.theme import CSS, resolve_scheme, theme_css
    dark, light = theme_css("dark"), theme_css("light")
    assert CSS == dark and "@define-color lv_bg #0A0A0C;" in dark
    assert "@define-color lv_bg #F4F4F7;" in light and "@define-color lv_line black;" in light
    assert "alpha(white," not in dark.split(".osd")[0]       # lines follow the scheme
    assert ".osd, .osd-num { background: alpha(#121215" in light   # the OSD stays dark
    assert resolve_scheme({}) == "dark" and resolve_scheme({"theme": "light"}) == "light"


def test_new_installation_follows_the_desktop(tmp_path, monkeypatch):
    """A new installation starts in the desktop's look; a setting once saved
    stays, and one from before the colour scheme could be chosen stays dark."""
    from lindvb import config
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config.Settings, "path", path)
    assert config.Settings()["theme"] == "system"              # no file yet
    for saved in ("dark", "light", "system"):
        path.write_text(f'{{"theme": "{saved}"}}')
        assert config.Settings()["theme"] == saved
    path.write_text('{"volume": 0.5}')                          # an old installation
    assert config.Settings()["theme"] == "dark"
    s = config.Settings()
    s.save()
    assert config.Settings()["theme"] == "dark"


def test_system_scheme_asks_the_desktop_portal(monkeypatch):
    from lindvb.ui import theme
    assert [theme.portal_value_scheme(v) for v in (1, 2, 0, (1,), ((2,),), None, "x")] == \
        ["dark", "light", None, "dark", "light", None, None]
    monkeypatch.setattr(theme, "portal_scheme", lambda: "light")
    assert theme.system_scheme() == "light"
    assert theme.resolve_scheme({"theme": "system"}) == "light"
    monkeypatch.setattr(theme, "portal_scheme", lambda: "dark")
    assert theme.resolve_scheme({"theme": "system"}) == "dark"
    assert theme.resolve_scheme({"theme": "light"}) == "light"
