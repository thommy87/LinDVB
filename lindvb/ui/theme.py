"""LinDVB player theme: a minimal dark look with a blue accent.

Design notes (from the ui-ux-pro-max guidance for media/streaming apps):
"Dark Mode (OLED) + Minimalism": near-black surfaces so the picture stands
out, one vivid accent used sparingly, text at 4.5:1 contrast or better,
visible keyboard focus on every control, tooltips on icon-only buttons,
at least 8 px between controls and short (150-200 ms) hover transitions.
GTK switches transitions off on its own when animations are disabled.
"""

ACCENT = "#119CFF"

CSS = """
@define-color lv_bg #0A0A0C;
@define-color lv_surface #121215;
@define-color lv_surface_2 #1B1B20;
@define-color lv_surface_3 #26262D;
@define-color lv_text #F2F2F5;
@define-color lv_text_dim #A3A3AD;
@define-color lv_rec #FF453A;

/* ---------- window & header ---------- */
window.lindvb { background: @lv_bg; color: @lv_text; }
window.lindvb headerbar {
  background: @lv_surface; color: @lv_text; min-height: 40px; padding: 2px 6px;
  box-shadow: none; border-bottom: 1px solid alpha(white, 0.06);
}
window.lindvb headerbar .title { font-weight: 600; }
window.lindvb headerbar .subtitle { color: @lv_text_dim; font-size: 0.85em; }
window.lindvb headerbar button {
  min-width: 32px; min-height: 32px; border-radius: 8px; color: @lv_text_dim;
  background: none; box-shadow: none; border: none;
  transition: background 150ms ease, color 150ms ease;
}
window.lindvb headerbar button:hover { background: alpha(white, 0.07); color: @lv_text; }
window.lindvb headerbar button:checked { background: alpha(@lv_accent, 0.18); color: @lv_accent; }
window.lindvb headerbar windowcontrols button { min-width: 28px; min-height: 28px; }

/* ---------- focus ring (keyboard only) ---------- */
window.lindvb button:focus-visible, window.lindvb scale:focus-visible,
window.lindvb entry:focus-within, window.lindvb dropdown > button:focus-visible {
  outline: 2px solid alpha(@lv_accent, 0.85); outline-offset: 1px;
}

/* ---------- sidebar / channel list ---------- */
.lv-sidebar { background: @lv_surface; border-right: 1px solid alpha(white, 0.06); }
.lv-sidebar .lv-heading { font-weight: 700; font-size: 1.05em; }
.lv-sidebar .lv-count { color: @lv_text_dim; font-size: 0.85em; font-feature-settings: "tnum"; }
.lv-sidebar entry, .lv-sidebar dropdown > button {
  background: @lv_surface_2; color: @lv_text; border-radius: 8px;
  border: 1px solid alpha(white, 0.06); box-shadow: none; min-height: 32px;
}
.lv-sidebar entry:focus-within { border-color: alpha(@lv_accent, 0.6); }
.lv-sidebar scrolledwindow, .lv-sidebar listview { background: transparent; }
.lv-sidebar listview > row {
  border-radius: 8px; margin: 1px 6px; padding: 6px 8px; color: @lv_text;
  transition: background 150ms ease;
}
.lv-sidebar listview > row:hover { background: alpha(white, 0.05); }
.lv-sidebar listview > row:selected { background: alpha(@lv_accent, 0.20); color: white; }
.lv-sidebar listview > row:selected .lv-num { color: @lv_accent; }
.lv-num { color: @lv_text_dim; font-feature-settings: "tnum"; font-size: 0.9em; }
.lv-chan-icon { color: @lv_text_dim; }
.lv-badge { color: @lv_text_dim; }
.lv-fav { color: #FFC83D; }
paned.lv-paned > separator { background: transparent; min-width: 1px; }

/* ---------- video & overlays ---------- */
.video { background: black; }
.osd {
  background: alpha(@lv_surface, 0.86); color: @lv_text; border-radius: 14px;
  padding: 14px 18px; border: 1px solid alpha(white, 0.08);
}
.osd .lv-osd-num {
  background: @lv_accent; color: @lv_on_accent; font-weight: 800; border-radius: 6px;
  padding: 1px 8px; font-feature-settings: "tnum";
}
.osd .title { font-size: 1.35em; font-weight: 700; }
.osd .lv-osd-now { font-size: 1.05em; }
.osd .lv-osd-time { color: @lv_text_dim; font-feature-settings: "tnum"; }
.osd .lv-osd-next { color: @lv_text_dim; }
.osd progressbar trough { min-height: 4px; border-radius: 2px; background: alpha(white, 0.12); }
.osd progressbar progress { min-height: 4px; border-radius: 2px; background: @lv_accent; }
.osd-num {
  background: alpha(@lv_surface, 0.86); color: @lv_text; font-size: 2.2em; font-weight: 800;
  border-radius: 12px; padding: 6px 18px; border: 1px solid alpha(white, 0.08);
  font-feature-settings: "tnum";
}
.ttxsub { background: rgba(0,0,0,0.75); color: white; font-size: 1.5em; font-weight: bold;
          border-radius: 4px; padding: 2px 10px; }

/* ---------- bottom control bar ---------- */
.lv-controls {
  background: @lv_surface; border-top: 1px solid alpha(white, 0.06);
  padding: 8px 12px;
}
.lv-floating .lv-controls {
  background: alpha(@lv_surface, 0.90); border: 1px solid alpha(white, 0.08);
  border-radius: 14px; margin: 0 24px 20px 24px;
}
.lv-controls button, .lv-controls menubutton > button {
  min-width: 34px; min-height: 34px; padding: 0; border-radius: 8px;
  background: none; box-shadow: none; border: none; color: @lv_text_dim;
  transition: background 150ms ease, color 150ms ease;
}
.lv-controls button:hover, .lv-controls menubutton > button:hover {
  background: alpha(white, 0.07); color: @lv_text;
}
.lv-controls button:active, .lv-controls menubutton > button:active { background: alpha(white, 0.12); }
.lv-controls button:checked, .lv-controls menubutton > button:checked {
  background: alpha(@lv_accent, 0.18); color: @lv_accent;
}
.lv-controls button:disabled { color: alpha(@lv_text_dim, 0.4); }
.lv-controls button.lv-play {
  min-width: 52px; min-height: 34px; border-radius: 10px;
  background: @lv_accent; color: @lv_on_accent;
}
.lv-controls button.lv-play:hover { background: @lv_accent_hover; color: @lv_on_accent; }
.lv-controls button.lv-play:active { background: @lv_accent_active; }
.lv-controls button.lv-rec:hover { color: @lv_rec; }
.lv-controls button.lv-rec:checked { background: alpha(@lv_rec, 0.18); color: @lv_rec; }
.lv-time { color: @lv_text_dim; font-size: 0.9em; font-feature-settings: "tnum"; min-width: 52px; }
.lv-controls button.lv-live {
  min-width: 0; padding: 0 10px; min-height: 24px; border-radius: 12px;
  font-size: 0.78em; font-weight: 800; letter-spacing: 1px;
  background: alpha(white, 0.08); color: @lv_text_dim;
}
.lv-controls button.lv-live.lv-at-live { background: alpha(@lv_accent, 0.18); color: @lv_accent; }
.lv-controls button.lv-live:hover { background: alpha(@lv_accent, 0.28); color: white; }
.lv-sep { background: alpha(white, 0.08); min-width: 1px; margin: 8px 6px; }

/* sliders: slim track, accent fill, pill knob */
.lv-controls scale { padding: 10px 0; }
.lv-controls scale trough {
  min-height: 6px; border-radius: 3px; background: alpha(white, 0.10); border: none;
}
.lv-controls scale highlight {
  background: @lv_accent; border-radius: 3px; border: none; min-height: 6px; margin: 0;
}
.lv-controls scale slider {
  min-width: 6px; min-height: 16px; margin: -5px 0; border-radius: 3px;
  background: @lv_accent; border: none; box-shadow: 0 0 0 2px alpha(@lv_bg, 0.6);
  transition: min-width 150ms ease;
}
.lv-controls scale:hover slider { background: white; }
.lv-controls scale.lv-volume highlight { background: @lv_text; }
.lv-controls scale.lv-volume slider { background: @lv_text; }
.lv-controls scale:disabled trough { background: alpha(white, 0.05); }
.lv-controls scale:disabled slider { background: transparent; box-shadow: none; }

/* ---------- status strip ---------- */
.statusbar {
  background: @lv_bg; color: @lv_text_dim; padding: 3px 14px; font-size: 0.82em;
  font-feature-settings: "tnum";
}
.rec { color: @lv_rec; font-weight: bold; }
.lv-signal { color: @lv_text_dim; }

/* ---------- menus ---------- */
window.lindvb popover > contents { background: @lv_surface_2; border-radius: 12px; }
window.lindvb popover modelbutton:hover, window.lindvb popover modelbutton:selected {
  background: alpha(@lv_accent, 0.18);
}
"""

# ---------- dialogs (channel editor, preferences, ...) ----------
# Applies to every window of the app, not just the main one.
CSS += """
.lv-dialog-pane { background: alpha(white, 0.02); }
.lv-section-title { font-weight: 700; }
expander.lv-section > title { padding: 6px 4px; border-radius: 8px; }
expander.lv-section > title:hover { background: alpha(white, 0.05); }
.lv-section-body { margin: 2px 6px 10px 22px; }
.lv-field-label { color: @lv_text_dim; }
.lv-hint { color: @lv_text_dim; font-size: 0.85em; }
.lv-toolstrip {
  background: alpha(white, 0.03); border-left: 1px solid alpha(white, 0.06);
  padding: 8px 6px;
}
.lv-toolstrip button {
  min-width: 34px; min-height: 34px; padding: 0; border-radius: 8px;
  background: none; box-shadow: none; border: none;
}
.lv-toolstrip button:hover { background: alpha(white, 0.08); }
.lv-toolstrip button.lv-primary { background: @lv_accent; color: @lv_on_accent; }
.lv-toolstrip button.lv-primary:hover { background: @lv_accent_hover; }
.lv-toolstrip button.lv-danger:hover { background: alpha(@lv_rec, 0.18); color: @lv_rec; }
.lv-toolstrip separator { margin: 6px 4px; background: alpha(white, 0.08); }
.lv-group {
  background: alpha(white, 0.04); border: 1px solid alpha(white, 0.06);
  border-radius: 12px; padding: 4px 14px;
}
.lv-group > .lv-row { padding: 8px 0; }
.lv-group > .lv-row + .lv-row { border-top: 1px solid alpha(white, 0.06); }
.lv-page-title { font-size: 1.25em; font-weight: 700; }
stacksidebar.lv-nav { background: alpha(white, 0.02); }
stacksidebar.lv-nav row { padding: 8px 12px; border-radius: 8px; margin: 1px 6px; }
"""

# ---------- accent colour ----------------------------------------------------
# The accent is kept in its own style provider so it can be swapped at run
# time. Besides the LinDVB colour names it overrides the theme's accent, so
# stock widgets (selected rows, switches, focus rings...) in every window use
# it too; without that Ubuntu's Yaru theme shows its orange accent in dialogs.

ACCENT_MODES = ("default", "system", "custom")

# GNOME 47+ "accent-color" names, with libadwaita's colours.
_GNOME_ACCENTS = {
    "blue": "#3584E4", "teal": "#2190A4", "green": "#3A944A", "yellow": "#C88800",
    "orange": "#ED5B00", "red": "#E62D42", "pink": "#D56199", "purple": "#9141AC",
    "slate": "#6F8396",
}
# Ubuntu's Yaru colour variants (theme name "Yaru-<variant>[-dark]").
_YARU_ACCENTS = {
    "": "#E95420", "bark": "#787859", "sage": "#657B69", "olive": "#4B8501",
    "viridian": "#03875B", "prussiangreen": "#308280", "blue": "#0073E5",
    "purple": "#7764D8", "magenta": "#B34CB3", "red": "#DA3450",
}


def parse_hex(color):
    """'#RRGGBB' (or 'RRGGBB', '#RGB') -> (r, g, b) ints, or None."""
    c = str(color or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return None
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def to_hex(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(v))) for v in rgb)


def _mix(rgb, other, t):
    return tuple(a + (b - a) * t for a, b in zip(rgb, other))


def _luminance(rgb):
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def on_accent(rgb):
    """Text colour for labels drawn on the accent: near black or white."""
    lum = _luminance(rgb)
    dark = (lum + 0.05) / (_luminance((10, 10, 12)) + 0.05)
    light = 1.05 / (lum + 0.05)
    return "#0A0A0C" if dark >= light else "#FFFFFF"


def yaru_accent(theme_name):
    """Accent of an Ubuntu Yaru theme name, or None if it isn't Yaru."""
    name = (theme_name or "").lower()
    if not name.startswith("yaru"):
        return None
    parts = [p for p in name.split("-")[1:] if p not in ("dark", "light")]
    return _YARU_ACCENTS.get(parts[0] if parts else "", _YARU_ACCENTS[""])


def system_accent():
    """Best guess at the desktop's accent colour as '#RRGGBB', or None."""
    try:
        from gi.repository import Gtk
        theme = Gtk.Settings.get_default().get_property("gtk-theme-name")
    except Exception:  # noqa: BLE001 - no display or no GTK
        theme = None
    yaru = yaru_accent(theme)
    if yaru:
        return yaru
    try:
        from gi.repository import Gio
        src = Gio.SettingsSchemaSource.get_default()
        schema = src.lookup("org.gnome.desktop.interface", True) if src else None
        if schema and schema.has_key("accent-color"):
            name = Gio.Settings.new("org.gnome.desktop.interface").get_string("accent-color")
            if name in _GNOME_ACCENTS:
                return _GNOME_ACCENTS[name]
    except Exception:  # noqa: BLE001
        pass
    try:  # whatever the GTK theme itself calls its accent
        from gi.repository import Gtk
        ctx = Gtk.Label().get_style_context()
        for name in ("accent_bg_color", "theme_selected_bg_color", "selected_bg_color"):
            ok, rgba = ctx.lookup_color(name)
            if ok:
                return to_hex((rgba.red * 255, rgba.green * 255, rgba.blue * 255))
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve_accent(settings):
    """The accent '#RRGGBB' for the user's settings."""
    mode = settings.get("accent_mode", "default")
    if mode == "custom" and parse_hex(settings.get("accent_custom")):
        return to_hex(parse_hex(settings["accent_custom"]))
    if mode == "system":
        return system_accent() or ACCENT
    return ACCENT


def accent_css(color, override_theme=True):
    """CSS defining the LinDVB accent colours (and, by default, the theme's)."""
    rgb = parse_hex(color) or parse_hex(ACCENT)
    acc = to_hex(rgb)
    hover = to_hex(_mix(rgb, (255, 255, 255), 0.18))
    active = to_hex(_mix(rgb, (0, 0, 0), 0.15))
    fg = on_accent(rgb)
    css = f"""
@define-color lv_accent {acc};
@define-color lv_accent_hover {hover};
@define-color lv_accent_active {active};
@define-color lv_on_accent {fg};
"""
    if not override_theme:
        return css
    return css + f"""
@define-color accent_color {acc};
@define-color accent_bg_color {acc};
@define-color accent_fg_color {fg};
@define-color theme_selected_bg_color {acc};
@define-color theme_selected_fg_color {fg};
@define-color selected_bg_color {acc};
@define-color selected_fg_color {fg};

row:selected, columnview > listview > row:selected, listview > row:selected,
gridview > child:selected, treeview.view:selected, calendar > grid > label.day-number:selected {{
  background-color: alpha(@lv_accent, 0.32); color: @lv_text;
}}
row:selected:hover, listview > row:selected:hover {{ background-color: alpha(@lv_accent, 0.40); }}
*:focus-visible {{ outline-color: alpha(@lv_accent, 0.80); }}
entry:focus-within, spinbutton:focus-within, searchbar entry:focus-within,
dropdown > button:focus-visible, textview:focus-within {{
  outline-color: alpha(@lv_accent, 0.80); border-color: alpha(@lv_accent, 0.80);
}}
selection, entry selection, text selection, label selection {{
  background-color: alpha(@lv_accent, 0.45);
}}
switch:checked {{ background-color: @lv_accent; border-color: @lv_accent; }}
switch:checked > slider {{ border-color: @lv_accent; }}
check:checked, check:indeterminate, radio:checked, radio:indeterminate {{
  background-color: @lv_accent; border-color: @lv_accent; color: @lv_on_accent;
}}
button.suggested-action, button.suggested-action:checked {{
  background-color: @lv_accent; background-image: none; color: @lv_on_accent;
  border-color: @lv_accent_active;
}}
button.suggested-action:hover {{ background-color: @lv_accent_hover; }}
button.suggested-action:active {{ background-color: @lv_accent_active; }}
scale > trough > highlight, progressbar > trough > progress,
levelbar > trough > block.filled {{ background-color: @lv_accent; border-color: @lv_accent; }}
notebook > header > tabs > tab:checked {{ box-shadow: inset 0 -3px @lv_accent; }}
link, button.link {{ color: @lv_accent; }}
popover modelbutton:selected, popover modelbutton:hover {{
  background-color: alpha(@lv_accent, 0.18);
}}
"""


_provider = None


def apply_accent(settings_or_color):
    """Install or replace the accent provider for the whole display."""
    global _provider
    from gi.repository import Gdk, Gtk
    if isinstance(settings_or_color, str):
        color, override = settings_or_color, True
    else:
        s = settings_or_color
        override = s.get("accent_mode", "default") != "system"
        if _provider is not None and not override:
            # Look up the theme's own accent without our override in place.
            Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), _provider)
            _provider = None
        color = resolve_accent(s)
    css = accent_css(color, override_theme=override)
    if _provider is None:
        _provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    if hasattr(_provider, "load_from_string"):
        _provider.load_from_string(css)
    else:
        _provider.load_from_data(css.encode())
    return color
