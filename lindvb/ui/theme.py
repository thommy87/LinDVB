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
@define-color lv_accent #119CFF;
@define-color lv_accent_hover #3BAEFF;
@define-color lv_accent_active #0B86DE;
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
  background: @lv_accent; color: @lv_bg; font-weight: 800; border-radius: 6px;
  padding: 1px 8px; font-feature-settings: "tnum";
}
.osd .title { font-size: 1.35em; font-weight: 700; }
.osd .lv-osd-now { font-size: 1.05em; }
.osd .lv-osd-time { color: @lv_text_dim; font-feature-settings: "tnum"; }
.osd .lv-osd-next { color: @lv_text_dim; }
.osd .lv-osd-meta { color: @lv_text_dim; font-size: 0.85em; }
.osd .lv-osd-tag {
  color: @lv_text_dim; font-size: 0.75em; font-weight: 600; border-radius: 4px;
  padding: 0 5px; border: 1px solid alpha(white, 0.22);
}
.osd .lv-osd-tag.lv-current { color: @lv_text; border-color: @lv_accent; }
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
  background: @lv_accent; color: @lv_bg;
}
.lv-controls button.lv-play:hover { background: @lv_accent_hover; color: @lv_bg; }
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
