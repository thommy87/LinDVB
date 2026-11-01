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
