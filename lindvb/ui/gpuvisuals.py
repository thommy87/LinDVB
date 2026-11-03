"""GPU (OpenGL) renderers for the built-in radio visualisations.

Every built-in style in :mod:`lindvb.visuals` has a renderer here that draws
the same picture with shaders at the full resolution of the window, so the
CPU only computes a few numbers per frame. Text and other parts that never
move (station name, VU meter faces) are still drawn with cairo, but only when
they change, and kept as textures. Styles without a renderer (plugins) are
drawn with cairo as before.
"""

import math
from array import array

import cairo

from .. import visuals
from . import gl as G

VERTEX = """
in vec2 a_pos;
out vec2 v_uv;
void main() {
    v_uv = vec2(a_pos.x, 1.0 - a_pos.y);          // 0..1, origin top left
    gl_Position = vec4(a_pos * 2.0 - 1.0, 0.0, 1.0);
}
"""

PRELUDE = """
in vec2 v_uv;
out vec4 fragColor;
uniform vec2 u_res;
// sample a texture we rendered ourselves (GL rows go bottom to top)
vec4 tap(sampler2D t, vec2 uv) { return texture(t, vec2(uv.x, 1.0 - uv.y)); }
float dither() {
    return (fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453) - 0.5) / 255.0;
}
vec3 bg(vec3 inner, vec3 outer) {
    vec2 px = v_uv * u_res;
    float t = clamp(length(px - u_res * 0.5) / (max(u_res.x, u_res.y) * 0.75), 0.0, 1.0);
    return mix(inner, outer, t) + dither();
}
"""

BACKGROUND = PRELUDE + """
uniform vec3 u_inner, u_outer;
void main() { fragColor = vec4(bg(u_inner, u_outer), 1.0); }
"""

OVERLAY = PRELUDE + """
uniform sampler2D u_tex;
void main() { fragColor = texture(u_tex, v_uv).bgra; }   // cairo ARGB32, premultiplied
"""

SHAPE_EMIT = """
uniform vec2 u_res;          // size of the target in units
uniform float u_unit;        // screen pixels per unit
out vec2 v_px;
flat out vec4 v_p;
flat out vec4 v_s;
flat out vec4 v_c0;
flat out vec4 v_c1;
flat out vec4 v_c2;
in vec2 a_corner;
// p: x0 y0 x1 y1 (top left origin), s: radius / half width, kind, middle stop, -
void emit(vec4 p, vec4 s, vec4 c0, vec4 c1, vec4 c2) {
    int kind = int(s.y + 0.5);
    float r = s.x;
    float e = r + 1.0 / u_unit;
    vec2 pos;
    if (kind == 0) {
        pos = mix(p.xy, p.zw, a_corner);
    } else if (kind == 1) {
        vec2 d = p.zw - p.xy;
        float len = length(d);
        vec2 dir = len > 1e-4 ? d / len : vec2(1.0, 0.0);
        vec2 nrm = vec2(-dir.y, dir.x);
        pos = p.xy - dir * e + dir * (len + 2.0 * e) * a_corner.x + nrm * e * (a_corner.y * 2.0 - 1.0);
    } else {
        pos = p.xy + (a_corner * 2.0 - 1.0) * e;
    }
    v_px = pos; v_p = p; v_s = s; v_c0 = c0; v_c1 = c1; v_c2 = c2;
    vec2 ndc = pos / u_res * 2.0 - 1.0;
    gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);
}
void hide() { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); v_s = vec4(0.0); }
"""

SHAPES_VERTEX = SHAPE_EMIT + """
in vec4 a_p;
in vec4 a_s;
in vec4 a_c0;
in vec4 a_c1;
in vec4 a_c2;
void main() { emit(a_p, a_s, a_c0, a_c1, a_c2); }
"""

SHAPES_FRAGMENT = """
in vec2 v_px;
flat in vec4 v_p;
flat in vec4 v_s;
flat in vec4 v_c0;
flat in vec4 v_c1;
flat in vec4 v_c2;
out vec4 fragColor;
uniform float u_unit;
vec4 pm(vec4 c) { return vec4(c.rgb * c.a, c.a); }
void main() {
    int kind = int(v_s.y + 0.5);
    float r = v_s.x;
    if (kind == 0) { fragColor = pm(v_c0); return; }
    if (kind == 1) {                                  // line with round caps
        vec2 pa = v_px - v_p.xy, ba = v_p.zw - v_p.xy;
        float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-6), 0.0, 1.0);
        float d = length(pa - ba * h);
        fragColor = pm(v_c0) * clamp((r - d) * u_unit + 0.5, 0.0, 1.0);
        return;
    }
    float d = length(v_px - v_p.xy);
    float t = clamp(d / max(r, 1e-3), 0.0, 1.0);
    if (kind == 2) {                                  // disc with a 3-stop radial gradient
        float m = clamp(v_s.z, 1e-4, 1.0 - 1e-4);
        vec4 c = t < m ? mix(pm(v_c0), pm(v_c1), t / m) : mix(pm(v_c1), pm(v_c2), (t - m) / (1.0 - m));
        fragColor = c * clamp((r - d) * u_unit + 0.5, 0.0, 1.0);
    } else {                                          // soft glow
        float g = 1.0 - t;
        fragColor = pm(v_c0) * g * g;
    }
}
"""

GLOW = PRELUDE + """
uniform vec3 u_inner, u_outer;
uniform float u_lw;
uniform int u_count;
uniform vec3 u_col[5];
uniform vec4 u_wave[5];      // centre, width, frequency, phase
uniform float u_amp[5];
const float PI = 3.14159265;
void main() {
    vec2 px = v_uv * u_res;
    float w = u_res.x, cy = u_res.y * 0.5;
    vec3 col = bg(u_inner, u_outer);
    float u = px.x / w;
    // centre line, fading towards the ends
    float ga = u < 0.15 ? u / 0.15 * 0.75 : (u > 0.85 ? (1.0 - u) / 0.15 * 0.75 : 0.75);
    float cov = clamp(u_lw * 0.5 - abs(px.y - cy) + 0.5, 0.0, 1.0)
              * step(0.08 * w, px.x) * step(px.x, 0.92 * w);
    col = mix(col, vec3(1.0), ga * cov);
    for (int k = 0; k < 5; k++) {
        if (k >= u_count) break;
        float amp = u_amp[k];
        vec4 p = u_wave[k];
        vec3 wc = u_col[k];
        vec3 mid = mix(wc, vec3(1.0), 0.35);
        float g = clamp((px.y - (cy - amp)) / (2.0 * amp), 0.0, 1.0);
        for (int pass = 0; pass < 2; pass++) {
            float scale = pass == 0 ? 1.3 : 1.0;
            float alpha = pass == 0 ? 0.10 : 0.45;
            float e = (u - p.x) / (p.y * scale);
            float yv = amp * scale * exp(-e * e) * sin(u * p.z * PI + p.w);
            float inside = clamp(abs(yv) - abs(px.y - cy) + 0.5, 0.0, 1.0);
            vec3 c3; float a;
            if (g < 0.5) { c3 = mix(wc, mid, g * 2.0); a = mix(alpha * 0.5, alpha, g * 2.0); }
            else { c3 = mix(mid, wc, g * 2.0 - 1.0); a = mix(alpha, alpha * 0.5, g * 2.0 - 1.0); }
            vec3 s = c3 * a * inside;
            col = s + col - s * col;                  // screen
        }
    }
    fragColor = vec4(col, 1.0);
}
"""

BARS = PRELUDE + """
uniform vec3 u_inner, u_outer;
uniform sampler2D u_data;    // row 0: bands, row 1: peaks
uniform int u_n;
uniform vec4 u_box;          // margin x, top, area width, area height
uniform vec2 u_bar;          // gap, bar width
uniform vec3 u_seg;          // segment height, segments, gap between segments
const vec3 GREEN = vec3(0.239, 0.863, 0.518);
const vec3 YELLOW = vec3(1.0, 0.824, 0.247);
const vec3 RED = vec3(1.0, 0.271, 0.227);
vec3 seg_colour(float pos) { return pos > 0.85 ? RED : (pos > 0.62 ? YELLOW : GREEN); }
void main() {
    vec2 px = v_uv * u_res;
    vec3 col = bg(u_inner, u_outer);
    float gap = u_bar.x, bw = u_bar.y;
    float fx = px.x - u_box.x;
    int i = int(floor(fx / (bw + gap)));
    float lx = fx - float(i) * (bw + gap);
    if (i >= 0 && i < u_n && lx >= gap * 0.5 && lx < gap * 0.5 + bw) {
        float segh = u_seg.x, segs = u_seg.y, sgap = u_seg.z;
        float yb = u_box.y + u_box.w - px.y;
        float s = floor(yb / segh);
        float ly = yb - s * segh;
        if (yb >= 0.0 && ly >= sgap * 0.5 && ly < segh - sgap * 0.5) {
            float v = texelFetch(u_data, ivec2(i, 0), 0).r;
            float lit = floor(v * segs + 0.5);
            if (s < segs) col = mix(col, seg_colour(s / segs), s < lit ? 0.95 : 0.07);
            float pk = floor(texelFetch(u_data, ivec2(i, 1), 0).r * segs);
            if (pk > 0.0 && s == pk) col = mix(seg_colour(pk / segs), vec3(1.0), 0.4);
        }
    }
    fragColor = vec4(col, 1.0);
}
"""

SCOPE = PRELUDE + """
uniform sampler2D u_wave;
uniform int u_n;
uniform float u_amp;
const vec3 PHOSPHOR = vec3(0.224, 1.0, 0.533);
float cover(float d, float width) { return clamp(width * 0.5 - d + 0.5, 0.0, 1.0); }
float wy(int i) {
    return u_res.y * 0.5 - clamp(texelFetch(u_wave, ivec2(i, 0), 0).r, -1.0, 1.0) * u_amp;
}
void main() {
    vec2 px = v_uv * u_res;
    float w = u_res.x, h = u_res.y;
    vec3 col = bg(vec3(0.02, 0.06, 0.04), vec3(0.0, 0.02, 0.01));
    // graticule
    float gx = abs(px.x - floor(px.x / (w / 10.0) + 0.5) * (w / 10.0));
    float gy = abs(px.y - floor(px.y / (h / 8.0) + 0.5) * (h / 8.0));
    float grid = max(cover(gx, 1.0) * step(1.0, floor(px.x / (w / 10.0) + 0.5)),
                     cover(gy, 1.0) * step(1.0, floor(px.y / (h / 8.0) + 0.5)));
    col = mix(col, PHOSPHOR, 0.08 * grid);
    col = mix(col, PHOSPHOR, 0.18 * cover(abs(px.y - h * 0.5), 1.0));
    // waveform: distance to the nearest segment of the polyline
    float dx = w / float(u_n - 1);
    int i0 = int(floor(px.x / dx));
    int k = min(48, int(ceil(6.0 / dx)) + 1);
    float d = 1e6;
    for (int j = -48; j <= 48; j++) {
        if (j < -k || j > k) continue;
        int a = clamp(i0 + j, 0, u_n - 2);
        vec2 p0 = vec2(float(a) * dx, wy(a));
        vec2 p1 = vec2(float(a + 1) * dx, wy(a + 1));
        vec2 pa = px - p0, ba = p1 - p0;
        float t = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-6), 0.0, 1.0);
        d = min(d, length(pa - ba * t));
    }
    col = mix(col, PHOSPHOR, 0.06 * cover(d, 9.0));
    col = mix(col, PHOSPHOR, 0.18 * cover(d, 4.0));
    col = mix(col, PHOSPHOR, 0.95 * cover(d, 1.6));
    fragColor = vec4(col + dither(), 1.0);
}
"""

# --- Goom HD ---------------------------------------------------------------------------
# GOOM's zoom filter (filters.c zoomVector) as a displacement field: each texel holds
# how far (in 0..1 screen units) the picture moves in one GOOM frame at that point.
GOOM_FIELD = PRELUDE + """
uniform vec2 u_vres;         // GOOM's screen (width x 200)
uniform vec2 u_middle;       // 0..1
uniform float u_speed, u_seed;
uniform int u_mode, u_hypercos, u_noisify;
uniform vec2 u_plane;        // hPlaneEffect, vPlaneEffect
uniform float u_scale;       // GOOM's unit (half its width) in screen heights
float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7)) + u_seed * 91.7) * 43758.5453); }
void main() {
    float ratio = 2.0 / (u_scale * u_vres.y);
    vec2 vpx = v_uv * u_vres;
    float X = (vpx.x - u_middle.x * u_vres.x) * ratio;
    float Y = (vpx.y - u_middle.y * u_vres.y) * ratio;
    float sq = X * X + Y * Y;
    float coef = (1.0 + u_speed) / 50.0;
    if (u_mode == 2) coef -= (sq - 0.3) / 15.0;          // crystal ball
    else if (u_mode == 4) coef += sq * 3.5;              // amulette
    else if (u_mode == 1) coef += sin(sq * 20.0) / 100.0; // wave
    else if (u_mode == 3) coef += sq / 10.0;             // scrunch
    else if (u_mode == 9) coef *= 4.0 * Y;               // speedway
    coef = clamp(coef, -2.01, 2.01);
    vec2 v = coef * vec2(X, Y);
    if (u_noisify != 0) {
        vec2 cell = gl_FragCoord.xy;
        v += (vec2(hash(cell), hash(cell + 17.3)) - 0.5) / 50.0;
    }
    if (u_hypercos != 0) v += vec2(sin(Y * 10.0), sin(X * 10.0)) / 120.0;
    v += vec2(Y * 0.0025 * u_plane.x, X * 0.0025 * u_plane.y);
    float lo = ratio / 16.0;
    if (abs(v.x) < lo) v.x = v.x < 0.0 ? -lo : lo;
    if (abs(v.y) < lo) v.y = v.y < 0.0 ? -lo : lo;
    fragColor = vec4(v / ratio / u_vres, 0.0, 1.0);
}
"""

GOOM_FIELD_MIX = PRELUDE + """
uniform sampler2D u_src, u_dst;
uniform float u_blend;
void main() { fragColor = mix(tap(u_src, v_uv), tap(u_dst, v_uv), u_blend); }
"""

# fields as parameters, for GL implementations without float render targets
GOOM_FIELD_FN = """
uniform vec2 u_vres;
uniform float u_scale;
float hash(vec2 p, float seed) { return fract(sin(dot(p, vec2(127.1, 311.7)) + seed * 91.7) * 43758.5453); }
vec2 field(vec2 uv, vec4 a, vec4 b, vec2 plane) {
    // a: middle x, middle y, speed, seed   b: mode, hypercos, noisify, -
    float ratio = 2.0 / (u_scale * u_vres.y);
    vec2 vpx = uv * u_vres;
    float X = (vpx.x - a.x * u_vres.x) * ratio;
    float Y = (vpx.y - a.y * u_vres.y) * ratio;
    float sq = X * X + Y * Y;
    float coef = (1.0 + a.z) / 50.0;
    int mode = int(b.x + 0.5);
    if (mode == 2) coef -= (sq - 0.3) / 15.0;
    else if (mode == 4) coef += sq * 3.5;
    else if (mode == 1) coef += sin(sq * 20.0) / 100.0;
    else if (mode == 3) coef += sq / 10.0;
    else if (mode == 9) coef *= 4.0 * Y;
    coef = clamp(coef, -2.01, 2.01);
    vec2 v = coef * vec2(X, Y);
    if (b.z > 0.5) {
        vec2 cell = floor(uv * 1024.0);
        v += (vec2(hash(cell, a.w), hash(cell + 17.3, a.w)) - 0.5) / 50.0;
    }
    if (b.y > 0.5) v += vec2(sin(Y * 10.0), sin(X * 10.0)) / 120.0;
    v += vec2(Y * 0.0025 * plane.x, X * 0.0025 * plane.y);
    float lo = ratio / 16.0;
    if (abs(v.x) < lo) v.x = v.x < 0.0 ? -lo : lo;
    if (abs(v.y) < lo) v.y = v.y < 0.0 ? -lo : lo;
    return v / ratio / u_vres;
}
"""

# one GOOM frame of the zoom filter: move, blur (bilinear) and fade the picture
GOOM_WARP = PRELUDE + """
uniform sampler2D u_prev, u_src, u_dst;
uniform float u_blend, u_keep;
uniform int u_textures;      // 1: fields in textures, 0: computed here
uniform vec4 u_fa0, u_fb0, u_fa1, u_fb1;
uniform vec4 u_planes;
uniform float u_frozen;      // blend frozen into the parameter source (fallback)
""" + GOOM_FIELD_FN + """
void main() {
    vec2 d;
    if (u_textures == 1) {
        d = mix(tap(u_src, v_uv).xy, tap(u_dst, v_uv).xy, u_blend);
    } else {
        d = mix(field(v_uv, u_fa0, u_fb0, u_planes.xy), field(v_uv, u_fa1, u_fb1, u_planes.zw), u_blend);
    }
    vec2 s = v_uv - d;
    vec3 c = vec3(0.0);
    if (s.x >= 0.0 && s.y >= 0.0 && s.x < 1.0 - 1.0 / u_vres.x && s.y < 1.0 - 1.0 / u_vres.y)
        c = tap(u_prev, s).rgb;
    // GOOM's coefficients add up to ~252/256, it subtracts 5 and truncates to 8 bits
    c = floor(max(c * 255.0 * u_keep - 5.0 / 256.0, 0.0)) / 255.0;
    fragColor = vec4(c, 1.0);
}
"""

GOOM_COPY = PRELUDE + """
uniform sampler2D u_tex;
void main() { fragColor = vec4(tap(u_tex, v_uv).rgb, 1.0); }
"""

# ifs.c: the points of the fractal, one instance per point (levels breadth first)
GOOM_IFS_VERTEX = SHAPE_EMIT + """
uniform int u_nb, u_depth, u_per, u_incr;
uniform vec4 u_sim[5];       // centre x, centre y, r, r2
uniform vec4 u_ang[5];       // cos A, sin A, cos A2, sin A2
uniform vec2 u_l;            // Lx, Ly
uniform vec4 u_col;
uniform float u_rad;
vec2 T(int k, vec2 o) {
    vec4 s = u_sim[k]; vec4 a = u_ang[k];
    vec2 c = s.xy;
    float xo = (o.x - c.x) * s.z;
    float yo = (o.y - c.y) * s.z;
    float xx = (xo - c.x) * s.w;
    float yy = (-yo - c.y) * s.w;
    return vec2(xo * a.x - yo * a.y + xx * a.z - yy * a.w + c.x,
                xo * a.y + yo * a.x + xx * a.w + yy * a.z + c.y);
}
bool moves(vec2 d) {      // ((x - xo) >> 4) && ((y - yo) >> 4) in GOOM's 12-bit fixed point
    float q = 16.0 / 4096.0;
    return (d.x < 0.0 || d.x >= q) && (d.y < 0.0 || d.y >= q);
}
void main() {
    int id = gl_InstanceID * u_incr;
    int start = id / u_per;
    int rem = id - start * u_per;
    int level = 1;
    int count = u_nb;
    for (int k = 0; k < 12; k++) {
        if (rem < count || level > u_depth) break;
        rem -= count; level++; count *= u_nb;
    }
    int i = start / (u_nb - 1);
    int j = start - i * (u_nb - 1);
    if (j >= i) j++;
    vec2 p = T(j, u_sim[i].xy);
    int div = count / u_nb;
    bool ok = true;
    for (int k = 0; k < 12; k++) {
        if (k >= level) break;
        int digit = (rem / div) - (rem / div / u_nb) * u_nb;
        vec2 q = T(digit, p);
        if (k < level - 1 && !moves(q - p)) ok = false;
        p = q;
        div = max(1, div / u_nb);
    }
    vec2 pos = vec2(u_l.x + p.x * u_l.x * 0.5, u_l.y - p.y * u_l.y * 0.5);
    if (!ok || pos.x <= 0.0 || pos.y <= 0.0 || pos.x >= u_res.x || pos.y >= u_res.y) { hide(); return; }
    emit(vec4(pos, pos), vec4(u_rad, 2.0, 0.5, 0.0), u_col, u_col, u_col);
}
"""

# tentacle3d.c / surf3d.c: the six meshes, one instance per segment
GOOM_TENTACLE_VERTEX = SHAPE_EMIT + """
uniform sampler2D u_y;       // vertex heights: x across, rows of all grids down
uniform vec4 u_grid[6];      // sizex, sizez, defz, first row
uniform vec4 u_cam[6];       // camera x, y, z, first instance
uniform vec2 u_rot;          // sin, cos of the rotation
uniform float u_dist, u_width;
uniform vec4 u_col;
const int DEFX = 15;
vec2 project(int g, int x, int z, out bool ok) {
    vec4 gr = u_grid[g];
    int defz = int(gr.z + 0.5);
    float vx = float(x - DEFX / 2) * gr.x / float(DEFX);
    float vz = float(z - defz / 2) * gr.y / float(defz);
    float vy = texelFetch(u_y, ivec2(x, int(gr.w + 0.5) + z), 0).r;
    vec3 c = u_cam[g].xyz;
    float sx = vx * u_rot.x - vz * u_rot.y + c.x;
    float sz = vx * u_rot.y + vz * u_rot.x + c.z;
    float sy = vy + c.y;
    ok = sz > 2.0;
    if (!ok) return vec2(0.0);
    // GOOM rounds the projection to its 320x200 pixels; unrounded the tentacles are smooth
    vec2 v = u_dist * vec2(sx, sy) / sz;
    vec2 p = vec2(v.x + u_res.x * 0.5, -v.y + u_res.y * 0.5);
    ok = p.x >= 0.0 && p.y >= 0.0 && p.x < u_res.x && p.y < u_res.y;
    return p;
}
void main() {
    int id = gl_InstanceID;
    int g = 0;
    for (int k = 1; k < 6; k++) if (id >= int(u_cam[k].w + 0.5)) g = k;
    int local = id - int(u_cam[g].w + 0.5);
    int defz = int(u_grid[g].z + 0.5);
    int x = local / (defz - 1);
    int z = local - x * (defz - 1) + 1;
    bool ok0, ok1;
    vec2 a = project(g, x, z - 1, ok0);
    vec2 b = project(g, x, z, ok1);
    if (!ok0 || !ok1) { hide(); return; }
    emit(vec4(a, b), vec4(u_width * 0.5, 1.0, 0.0, 0.0), u_col, u_col, u_col);
}
"""

# --- Light ribbons ---------------------------------------------------------------------
SPECTRUM_FN = """
uniform float u_spec[48];
float spec(float m) {            // m: 0 (bass) .. 1 (treble), interpolated
    float x = clamp(m, 0.0, 1.0) * 47.0;
    int i = int(floor(x));
    int j = min(i + 1, 47);
    return mix(u_spec[i], u_spec[j], x - float(i));
}
float hash21(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
"""

RIBBONS = PRELUDE + SPECTRUM_FN + """
uniform float u_t, u_unit, u_horizon, u_mirror, u_glow;
uniform int u_count;
uniform vec4 u_ra[4];        // y0, amplitude, frequency, phase
uniform vec4 u_rb[4];        // width, twist frequency, twist phase, spectrum gain
uniform vec3 u_front[4];
uniform vec3 u_back[4];
const float TAU = 6.2831853;
const int LINES = 24;
void ribbon(int k, float x, out float c, out float wd, out float phi) {
    vec4 a = u_ra[k]; vec4 b = u_rb[k];
    float u = x / u_res.x;
    c = a.x + a.y * (0.7 * sin(TAU * a.z * u + a.w) + 0.3 * sin(TAU * a.z * 2.3 * u - 1.3 * a.w))
        - b.w * spec(abs(2.0 * u - 1.0));
    wd = b.x * (0.6 + 0.4 * sin(TAU * 0.7 * u + 0.5 * b.z));
    phi = TAU * b.y * u + b.z;
}
// light of all ribbons at p; blur widens the threads (the reflection), stride skips some
vec3 ribbons(vec2 p, float blur, int stride) {
    vec3 acc = vec3(0.0);
    float u = p.x / u_res.x;
    float env = 0.35 + 0.65 * smoothstep(0.0, 0.2, u) * smoothstep(1.0, 0.8, u);
    float lw = max(1.0, 1.05 * u_unit);
    for (int k = 0; k < 4; k++) {
        if (k >= u_count) break;
        float c0, w0, f0, c1, w1, f1;
        ribbon(k, p.x, c0, w0, f0);
        ribbon(k, p.x + 1.0, c1, w1, f1);
        float cs0 = cos(f0), sn0 = sin(f0), cs1 = cos(f1), sn1 = sin(f1);
        vec3 col = mix(u_back[k], u_front[k], smoothstep(-0.35, 0.35, cs0));
        float dc = p.y - c0;
        float halo = abs(w0) * 1.6 + u_res.y * 0.02;
        acc += col * (0.07 + 0.10 * u_glow) * exp(-dc * dc / (halo * halo)) * env;
        // the translucent sheet between the outer threads
        float ya = c0 + w0 * (-cs0 + 0.15 * sn0), yb = c0 + w0 * (cs0 + 0.15 * sn0);
        float lo = min(ya, yb), hi = max(ya, yb);
        acc += col * 0.07 * clamp(p.y - lo + 0.5, 0.0, 1.0) * clamp(hi - p.y + 0.5, 0.0, 1.0) * env;
        float reach = abs(w0) * 1.2 + lw + blur * 2.0 + 2.0;
        if (abs(dc) > reach) continue;
        float threads = 0.0;
        for (int j = 0; j < LINES; j += stride) {
            float s = -1.0 + 2.0 * float(j) / float(LINES - 1);
            float y0 = c0 + w0 * (s * cs0 + 0.15 * s * s * sn0);
            float y1 = c1 + w1 * (s * cs1 + 0.15 * s * s * sn1);
            float slope = y1 - y0;
            float d = abs(p.y - y0) * inversesqrt(1.0 + slope * slope);
            threads += clamp((lw * 0.5 - d) / blur + 0.5, 0.0, 1.0);
        }
        acc += col * threads * 0.26 * float(stride) / sqrt(blur) * env;
    }
    return acc;
}
void main() {
    vec2 px = v_uv * u_res;
    float h = u_res.y, yh = h * u_horizon;
    // night sky with two soft spotlights from above
    vec3 col = mix(vec3(0.004, 0.01, 0.05), vec3(0.02, 0.05, 0.17), smoothstep(0.0, yh, px.y));
    vec2 l1 = (px - vec2(0.13 * u_res.x, -0.05 * h)) / h;
    vec2 l2 = (px - vec2(0.86 * u_res.x, -0.05 * h)) / h;
    col += vec3(0.03, 0.08, 0.28) * (exp(-dot(l1, l1) * 6.0) + exp(-dot(l2, l2) * 6.0)) * (0.7 + 0.5 * u_glow);
    if (px.y < yh) {
        col += 1.0 - exp(-ribbons(px, 1.0, 1) * 1.2);
    } else {
        float depth = (px.y - yh) / max(1.0, h - yh);
        col = mix(vec3(0.02, 0.05, 0.17), vec3(0.0, 0.004, 0.02), sqrt(depth));
        col += vec3(0.1, 0.25, 0.8) * exp(-(px.y - yh) / (3.0 * u_unit)) * 0.25;
        // ripples: wavy rows that shift the mirrored picture sideways
        float yy = px.y / u_unit, xx = px.x / u_unit;
        float rip = sin(yy * 0.35 + u_t * 1.7 + 3.0 * sin(xx * 0.004 + u_t * 0.3))
                  + 0.5 * sin(yy * 0.9 - u_t * 2.3 + xx * 0.002);
        // mirrored about the horizon and squashed, as seen at a low angle
        vec2 q = vec2(px.x + rip * (3.0 + 40.0 * depth) * u_unit,
                      yh - (px.y - yh) * u_mirror + rip * 3.0 * u_unit);
        float streak = 0.5 + 0.5 * sin(yy * 0.55 + rip * 2.0 + 2.5 * sin(xx * 0.011 + yy * 0.07 - u_t * 0.6)
                                       + 1.5 * sin(xx * 0.029 - yy * 0.13 + u_t * 0.9));
        vec3 refl = 1.0 - exp(-ribbons(q, 1.0 + 6.0 * depth * u_unit, 2) * 1.1);
        col += refl * (1.2 - 0.75 * depth) * (0.3 + 0.7 * streak);
        col += 1.0 - exp(-ribbons(px, 1.0, 1) * 1.2);       // ribbons dipping into the water
    }
    fragColor = vec4(col + dither(), 1.0);
}
"""

# --- Pulsar ----------------------------------------------------------------------------
PULSAR = PRELUDE + SPECTRUM_FN + """
uniform float u_t, u_unit, u_r0, u_pulse, u_level, u_spin;
uniform int u_layers;
uniform vec4 u_layer[5];     // turn offset, gain (px), opacity, sharpness
uniform vec3 u_lcol[5];
uniform vec4 u_orbit[3];     // centre x, centre y, radius, phase 2
uniform vec4 u_orbit2[3];    // a2, a3, phase 3, -
uniform vec3 u_ocol[3];
uniform vec2 u_aura;         // radius, phase
const float TAU = 6.2831853;
const float SPOKES = 144.0;
float specm(float turns) { return spec(1.0 - abs(2.0 * fract(turns) - 1.0)); }
vec3 screen(vec3 a, vec3 b) { return a + b - a * b; }
vec3 stars(vec2 px, float cell, float density, float t) {
    vec2 c = floor(px / cell);
    float h = hash21(c);
    if (h > density) return vec3(0.0);
    vec2 pos = (c + vec2(hash21(c + 3.1), hash21(c + 7.7))) * cell;
    float d = length(px - pos) / u_unit;
    float tw = 0.55 + 0.45 * sin(t * (1.0 + 3.0 * hash21(c + 1.3)) + h * 40.0);
    vec3 col = mix(vec3(0.6, 0.75, 1.0), vec3(1.0, 0.9, 0.5), step(0.5, hash21(c + 9.4)));
    return col * tw * exp(-d * d * 1.2);
}
void main() {
    vec2 px = v_uv * u_res;
    vec2 q = px - u_res * 0.5;
    float r = length(q);
    float ang = fract(atan(q.x, -q.y) / TAU);          // turns, 0 at the top, clockwise
    float lw = max(1.0, 1.1 * u_unit);
    vec3 col = vec3(0.0, 0.0, 0.012) + stars(px, 26.0 * u_unit, 0.16, u_t);
    // purple waves at the edges
    float ph = u_aura.y;
    float ra = u_aura.x * (1.0 + 0.05 * sin(3.0 * TAU * ang + ph) + 0.035 * sin(5.0 * TAU * ang - 1.3 * ph)
                          + 0.02 * sin(2.0 * TAU * ang + 0.7 * ph));
    vec3 aura = mix(vec3(0.42, 0.18, 1.0), vec3(1.0, 0.15, 0.78), 0.5 + 0.5 * sin(ang * TAU * 2.0 + ph * 0.5));
    float gap = 6.0 * u_unit;
    float t = (r - ra) / gap;
    if (t > -0.5 && t < 11.5) {
        float d = abs(t - floor(t + 0.5)) * gap;
        col += aura * clamp(lw * 0.5 - d + 0.5, 0.0, 1.0) * 0.55 * (1.0 - t / 14.0);
    }
    col += aura * 0.45 * smoothstep(ra + 8.0 * gap, ra + 40.0 * gap, r);
    col += aura * 0.08 * exp(-abs(r - ra) / (30.0 * u_unit));
    // colour flames around the core
    for (int k = 0; k < 5; k++) {
        if (k >= u_layers) break;
        vec4 L = u_layer[k];
        float R = u_r0 + L.y * pow(specm(ang - L.x), L.w) + u_r0 * 0.04 * sin(ang * TAU * 5.0 + u_t + float(k));
        float fill = clamp((R - r) / (1.5 * u_unit) + 0.5, 0.0, 1.0);
        float m = smoothstep(u_r0, u_r0 + max(R - u_r0, 1.0) * 0.12 + 2.0 * u_unit, r);
        vec3 c = mix(vec3(1.0, 0.9, 0.95), u_lcol[k], m) * (1.0 - 0.3 * smoothstep(u_r0, R, r));
        col = mix(col, mix(c, screen(col, c), 0.35), fill * L.z);
        col += u_lcol[k] * 0.18 * L.z * exp(-max(r - R, 0.0) / (10.0 * u_unit)) * step(R, r);
    }
    // light spokes
    float sa = ang - u_spin;
    float idx = floor(sa * SPOKES + 0.5);
    float sl = u_r0 * (1.3 + 1.9 * specm(idx / SPOKES + u_spin));
    float sd = r * abs(sin((sa - idx / SPOKES) * TAU));
    float spoke = clamp(lw * 0.5 - sd + 0.5, 0.0, 1.0) * step(u_r0, r) * (1.0 - smoothstep(sl * 0.6, sl, r));
    col += vec3(0.55, 0.95, 1.0) * spoke * 0.3;
    // the dark core with star dust inside, a cyan rim and a white glow
    if (r < u_r0) {
        col = vec3(0.0, 0.008, 0.03) + stars(px, 9.0 * u_unit, 0.35, u_t * 2.0) * vec3(0.6, 1.0, 0.6);
        col += vec3(0.1, 0.3, 0.9) * 0.15 * smoothstep(u_r0 * 0.5, u_r0, r);
    } else {
        col += vec3(1.0) * exp(-(r - u_r0) / ((5.0 + 6.0 * u_pulse) * u_unit)) * (0.6 + 0.4 * u_pulse);
    }
    float rim = (r - u_r0) / (1.8 * u_unit);
    col += vec3(0.35, 0.9, 1.0) * exp(-rim * rim);
    // orbiting curves, three strands each
    for (int k = 0; k < 3; k++) {
        vec4 o = u_orbit[k]; vec4 o2 = u_orbit2[k];
        vec2 v = px - o.xy;
        float rv = max(length(v), 1.0);
        float a = atan(v.y, v.x);
        vec3 oc = mix(u_ocol[k], u_ocol[k].gbr, 0.25 + 0.25 * sin(a + o.w));
        for (int s = 0; s < 3; s++) {
            float fs = float(s);
            float p2 = o.w + fs * 0.25, p3 = o2.z - fs * 0.18;
            float rad = o.z * (1.0 + 0.015 * fs);
            float R = rad * (1.0 + o2.x * sin(2.0 * a + p2) + o2.y * sin(3.0 * a + p3));
            float dR = rad * (2.0 * o2.x * cos(2.0 * a + p2) + 3.0 * o2.y * cos(3.0 * a + p3));
            float d = abs(rv - R) * inversesqrt(1.0 + dR * dR / (rv * rv));
            float k2 = (s == 0) ? 0.85 : 0.3;
            col += oc * (clamp(lw * 0.6 - d + 0.5, 0.0, 1.0) * k2 + 0.05 * exp(-d * d / (40.0 * u_unit * u_unit)));
        }
    }
    fragColor = vec4(col + dither(), 1.0);
}
"""

# --- painted scenes: supernova, sunset, aurora -----------------------------------------
# Everything is computed per pixel from noise; the CPU only passes the music, smoothed.
SCENE_FN = SPECTRUM_FN + """
uniform float u_t, u_clock, u_unit, u_pulse;
uniform vec4 u_energy;       // bass, mid, treble, level (smoothed)
const float TAU = 6.2831853;
float hash12(vec2 p) {       // without sin: stable for large inputs
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}
float vnoise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash12(i), hash12(i + vec2(1.0, 0.0)), u.x),
               mix(hash12(i + vec2(0.0, 1.0)), hash12(i + vec2(1.0, 1.0)), u.x), u.y);
}
const mat2 OCT = mat2(1.6, 1.2, -1.2, 1.6);
float fbm(vec2 p, int octaves) {
    float s = 0.0, a = 0.5;
    for (int i = 0; i < 6; i++) {
        if (i >= octaves) break;
        s += a * vnoise(p);
        p = OCT * p + 17.1;
        a *= 0.5;
    }
    return s;
}
float ridged(vec2 p, int octaves) {   // thin bright creases: filaments
    float s = 0.0, a = 0.55;
    for (int i = 0; i < 6; i++) {
        if (i >= octaves) break;
        float n = 1.0 - abs(2.0 * vnoise(p) - 1.0);
        s += a * n * n;
        p = OCT * p + 9.7;
        a *= 0.5;
    }
    return s;
}
vec3 starfield(vec2 px, float cell, float density, float t, float size) {
    vec2 c = floor(px / cell);
    float h = hash12(c);
    if (h > density) return vec3(0.0);
    vec2 pos = (c + 0.15 + 0.7 * vec2(hash12(c + 3.1), hash12(c + 7.7))) * cell;
    float d = length(px - pos) / (u_unit * size);
    float tw = 0.65 + 0.35 * sin(t * (0.6 + 2.0 * hash12(c + 1.3)) + h * 40.0);
    vec3 col = mix(vec3(0.65, 0.78, 1.0), vec3(1.0, 0.88, 0.7), hash12(c + 9.4));
    return col * tw * exp(-d * d) * (0.4 + 0.6 * hash12(c + 5.5));
}
vec3 tonemap(vec3 c) { return 1.0 - exp(-c); }
"""

SUPERNOVA = PRELUDE + SCENE_FN + """
float specm(float turns) { return spec(1.0 - abs(2.0 * fract(turns) - 1.0)); }
mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, s, -s, c); }
const int VEILS = 9;
uniform float u_flow;        // how far the veils have travelled (one unit: one veil's life)
uniform float u_veil[9];     // the strength each veil got from the music when it was born
const vec3 PAL[6] = vec3[6](vec3(1.0, 0.48, 0.10),     // orange
                            vec3(1.0, 0.82, 0.28),     // yellow
                            vec3(0.62, 0.30, 1.0),     // violet
                            vec3(1.0, 0.32, 0.68),     // pink
                            vec3(0.22, 0.45, 1.0),     // blue
                            vec3(0.18, 0.92, 0.88));   // turquoise
// stars that slowly light up and go out again, each at its own pace
vec3 blinking(vec2 px, float cell, float density, float size) {
    vec2 c = floor(px / cell);
    float h = hash12(c);
    if (h > density) return vec3(0.0);
    vec2 pos = (c + 0.15 + 0.7 * vec2(hash12(c + 3.1), hash12(c + 7.7))) * cell;
    vec2 d = (px - pos) / (u_unit * size);
    float rate = 0.15 + 0.6 * hash12(c + 1.3);
    float on = pow(max(0.0, sin(u_t * rate + h * 97.0)), 5.0);
    float k = exp(-dot(d, d));
    float big = step(0.85, hash12(c + 2.9));                  // a few with light spikes
    k += big * (0.5 * exp(-abs(d.x) / 0.6) * exp(-abs(d.y) / 14.0) + 0.5 * exp(-abs(d.y) / 0.6) * exp(-abs(d.x) / 14.0));
    vec3 col = mix(vec3(0.70, 0.80, 1.0), vec3(1.0, 0.85, 0.60), hash12(c + 9.4));
    return col * k * on * (0.6 + 0.8 * hash12(c + 5.5));
}
void main() {
    vec2 px = v_uv * u_res;
    float S = min(u_res.x, u_res.y);
    vec2 q = (px - 0.5 * u_res) / S;
    float r = length(q);
    vec2 dir = q / max(r, 1e-4);
    float ang = fract(atan(q.x, -q.y) / TAU);
    float c = u_clock;
    float bass = u_energy.x, mid = u_energy.y, treble = u_energy.z, level = u_energy.w;
    vec3 col = vec3(0.0, 0.0, 0.012);
    col += starfield(px, 6.0 * u_unit, 0.10, u_t, 0.8) * 0.6;            // faint, steady
    col += blinking(px, 21.0 * u_unit, 0.45, 1.3) * (1.0 + 0.8 * treble);
    col += blinking(px + 57.0, 70.0 * u_unit, 0.30, 1.8) * 1.3;
    // the spiral galaxy behind, seen at an angle and turning slowly
    vec2 g = rot(0.45) * q;
    g.y *= 1.9;
    g = rot(-c * 0.15) * g;
    float gr = length(g);
    float ga = atan(g.y, g.x);
    float arms = pow(0.5 + 0.5 * cos(2.0 * ga - 5.0 * log(gr + 0.06)), 3.0);
    float gn = fbm(g * 7.0 + 3.0, 4);
    float galaxy = arms * smoothstep(1.25, 0.25, gr) * smoothstep(0.08, 0.35, gr) * (0.35 + gn);
    float dust = smoothstep(0.45, 0.75, fbm(g * 11.0 - 7.0, 3)) * arms;
    vec3 gcol = mix(vec3(0.50, 0.54, 0.68), vec3(0.78, 0.58, 0.72), smoothstep(0.5, 0.8, gn));
    col += gcol * galaxy * 0.45 * (1.0 - 0.7 * dust);
    col += starfield(px + 5.0, 9.0 * u_unit, 0.25, u_t, 1.6) * vec3(1.0, 0.45, 0.75) * galaxy * 1.6;
    col += starfield(px + 71.0, 4.0 * u_unit, 0.35, u_t, 0.7) * galaxy * 1.6;
    // veils: born somewhere in the white core, they drift outwards, grow and fade,
    // and a new one is born as an old one vanishes, so the picture keeps its size
    float flow = u_flow;
    vec3 neb = vec3(0.0);
    for (int k = 0; k < VEILS; k++) {
        float fk = float(k);
        float life = flow + fk / float(VEILS);
        float s = fract(life);                                   // 0: born, 1: gone
        float seed = floor(life) * 7.31 + fk * 3.17;
        float R = 0.03 * pow(14.0, s);                          // its radius grows exponentially
        float strength = u_veil[k];
        float alpha = smoothstep(0.0, 0.22, s) * pow(1.0 - s, 1.15) * strength;
        vec2 u = rot(seed) * q / R;
        float ur = length(u);
        if (ur > 2.6) continue;
        // where on the ring this veil lies (it is born at a random place, not all around)
        vec2 ud = u / max(ur, 1e-4);
        float place = smoothstep(0.30, 0.65, fbm(ud * 1.6 + seed * 1.37, 3));
        float rag = 0.45 * (fbm(ud * 2.5 + seed * 2.1, 3) - 0.5);
        float width = 0.30 + 0.16 * min(strength, 1.5);                // strong veils are thicker
        float ring = exp(-pow((ur - 1.0 - rag) / width, 2.0)) + 0.3 * exp(-ur * 1.6);
        float m = ring * mix(0.35, 1.0, place) * alpha;
        if (m < 0.004) continue;
        // the veil itself: creased, swirled noise with fine fibres along the radius
        vec2 w = u * 1.8 + seed;
        w += 0.9 * vec2(fbm(w * 0.7 + 1.7, 3), fbm(w * 0.7 + 5.2, 3));
        float fil = pow(ridged(w, 5), 3.0) * 4.0 + pow(ridged(w * 2.7 + 9.0, 3), 4.0) * 2.0;
        float fib = 0.7 + 0.6 * vnoise(ud * 30.0 + seed * 11.0 + ur * 1.5);
        float wisp = fbm(w * 0.6 - 3.0, 3);
        // colours: two of orange, yellow, violet, pink, blue, turquoise per veil,
        // white while it is still near the core
        int i0 = int(hash12(vec2(seed, 1.0)) * 6.0);
        int i1 = int(hash12(vec2(seed, 2.0)) * 6.0);
        vec3 vc = mix(PAL[i0], PAL[i1], smoothstep(0.3, 0.7, wisp));
        vc = mix(vec3(1.0, 0.95, 0.88), vc, smoothstep(0.0, 0.18, s));
        float gain = 0.85 + 0.3 * specm(ang + seed * 0.1);
        neb += vc * m * (fil * fib + 0.22 * wisp * wisp) * gain;
    }
    col += neb * 2.6;
    // the white-hot core, glowing a little brighter with the music
    float core = 0.032 + 0.008 * level;
    col += vec3(1.0, 0.97, 0.92) * exp(-r / core) * (1.7 + 0.4 * level);
    col += vec3(1.0, 0.80, 0.55) * exp(-r / (core * 4.0)) * (0.6 + 0.3 * level);
    col += vec3(1.0, 0.70, 0.45) * exp(-r / 0.22) * 0.25;
    float lum = dot(col, vec3(0.3, 0.55, 0.15));
    col = max(mix(vec3(lum), col, 1.3), 0.0);                 // a little more colour
    fragColor = vec4(tonemap(col * 1.1) + dither(), 1.0);
}
"""

SUNSET = PRELUDE + SCENE_FN + """
uniform vec2 u_sun;          // 0..1 of the width and the height
vec3 sky(float y) {
    vec3 a = vec3(0.34, 0.26, 0.40), b = vec3(0.78, 0.42, 0.36), c = vec3(1.0, 0.48, 0.14), d = vec3(0.92, 0.22, 0.06);
    if (y < 0.40) return mix(a, b, y / 0.40);
    if (y < 0.72) return mix(b, c, (y - 0.40) / 0.32);
    return mix(c, d, (y - 0.72) / 0.28);
}
vec2 cloud_warp(vec2 p, float c) {
    // the music reshapes the clouds: the mids stir them, the bass swells them
    float a = 0.30 + 0.25 * u_energy.y + 0.15 * u_pulse;
    return p + a * vec2(fbm(p * 0.9 + vec2(c * 0.10, 0.0), 3), fbm(p * 0.9 + vec2(4.0, c * 0.08), 3));
}
float clouds(vec2 w, float cover, float soft, int octaves) {
    return smoothstep(cover, cover + soft, fbm(w, octaves));
}
void main() {
    vec2 px = v_uv * u_res;
    vec2 p = v_uv;                                    // 0..1, top left
    float aspect = u_res.x / u_res.y;
    float c = u_clock;
    float bass = u_energy.x, mid = u_energy.y, treble = u_energy.z, level = u_energy.w;
    vec2 d = (p - u_sun) * vec2(aspect, 1.0);          // in screen heights
    float dist = length(d);
    vec3 col = sky(p.y);
    // the glow of the sun, breathing with the bass
    col += vec3(1.0, 0.62, 0.30) * exp(-dist / (0.32 + 0.08 * bass)) * (0.55 + 0.35 * bass);
    col += vec3(1.0, 0.85, 0.60) * exp(-dist / 0.07) * (0.8 + 0.5 * u_pulse);
    // rays fanning out from the sun; the spectrum from left (bass) to right (treble)
    // the fan of rays turns slowly around the sun (sampled on a circle, so it has no seam)
    float th = atan(d.x, -d.y);                        // 0 straight up
    float m = clamp(th / 3.1 + 0.5, 0.0, 1.0);
    float spin = c * 0.12;
    vec2 rd = d / max(dist, 1e-4);
    vec2 r1 = vec2(cos(spin) * rd.x - sin(spin) * rd.y, sin(spin) * rd.x + cos(spin) * rd.y);
    vec2 r2 = vec2(cos(-spin * 0.6) * rd.x - sin(-spin * 0.6) * rd.y, sin(-spin * 0.6) * rd.x + cos(-spin * 0.6) * rd.y);
    float rays = vnoise(r1 * 5.0 + 11.0) * 0.6 + vnoise(r2 * 13.0 + 31.0) * 0.4;
    // the spectrum shapes them, bass on the left, treble on the right: louder bands
    // give wider, brighter and longer rays
    float sp = spec(m);
    rays = smoothstep(0.55 - 0.25 * sp, 0.9, rays) * (0.3 + 1.2 * sp) * (0.6 + 0.6 * mid);
    float raymask = smoothstep(0.03, 0.15, dist) * exp(-dist / (0.55 + 0.45 * sp + 0.2 * bass))
                    * smoothstep(1.0, 0.92, p.y);
    // two cloud layers drifting at their own speed; lit gold from below and from the sun
    vec2 toSun = normalize(-d + vec2(0.0, 0.001));
    col += vec3(1.0, 0.82, 0.55) * rays * raymask;
    for (int j = 0; j < 2; j++) {
        int k = 1 - j;                                   // the far, lower layer first
        float fk = float(k);
        float scale = k == 0 ? 1.0 : 1.9;
        // drifting from left to right, the near layer a little faster
        vec2 cp = vec2(p.x * aspect * 1.1 * scale - c * (0.20 - 0.06 * fk) + fk * 7.0, p.y * (5.0 + 2.0 * fk) * scale);
        float band = k == 0 ? smoothstep(0.80, 0.30, p.y) : smoothstep(0.95, 0.55, p.y) * smoothstep(0.35, 0.6, p.y);
        float cover = mix(0.80, 0.47, band) - 0.06 * spec(clamp(p.x, 0.0, 1.0)) - 0.03 * u_pulse;
        vec2 w = cloud_warp(cp, c);
        float dn = clouds(w, cover, 0.20, 6);
        // light from the sun through the cloud: thin parts and edges facing it glow
        float occ = 0.0;
        vec2 stepv = toSun * vec2(1.1, 5.0) * 0.03 * scale;
        for (int i = 1; i <= 3; i++) occ += clouds(w + stepv * float(i), cover, 0.25, 4);
        float lit = exp(-occ * 1.1) * (1.0 - 0.5 * dn);
        float near = exp(-dist / 0.55);
        vec3 shade = mix(vec3(0.40, 0.31, 0.43), vec3(0.78, 0.40, 0.30), smoothstep(0.05, 0.85, p.y));
        shade *= 0.85 + 0.3 * fbm(cp * 2.5, 3);
        vec3 light = mix(vec3(1.0, 0.66, 0.38), vec3(1.0, 0.88, 0.66), near);
        vec3 cc = mix(shade, light, clamp(lit * (0.8 + 0.8 * near) + 0.1 * near, 0.0, 1.0));
        cc += vec3(1.0, 0.8, 0.5) * lit * near * (0.4 + 0.8 * treble);
        float a = smoothstep(0.0, 0.6, dn) * (k == 0 ? 0.92 : 0.85);
        col = mix(col, cc, a);
    }
    // the sun itself
    float sr = 0.024;
    col = mix(col, vec3(1.0, 0.98, 0.92), smoothstep(sr, sr - 2.0 / u_res.y, dist));
    col += vec3(1.0, 0.9, 0.7) * exp(-max(dist - sr, 0.0) / 0.012) * 0.8;
    // haze at the horizon
    col += vec3(1.0, 0.35, 0.10) * smoothstep(0.80, 1.0, p.y) * 0.22;
    fragColor = vec4(tonemap(col * 1.35) + dither(), 1.0);
}
"""

AURORA = PRELUDE + SCENE_FN + """
const int SAMPLES = 90;
uniform float u_ridge[48];   // mountain heights (0..1 above the bottom), the newest on the right
uniform float u_scroll;      // fraction of a point already scrolled to the left
uniform float u_world;       // points scrolled in total, moves the snow's texture along
const float VISIBLE = 40.0;
float ridge_at(int i) { return u_ridge[clamp(i, 0, 47)]; }
float ridge_y(float x) {      // Catmull-Rom through the points, plus rocky detail
    float fx = x * VISIBLE + u_scroll;
    int i = int(floor(fx));
    float t = fx - float(i);
    float a = ridge_at(i - 1), b = ridge_at(i), c = ridge_at(i + 1), d = ridge_at(i + 2);
    float h = b + 0.5 * t * (c - a + t * (2.0 * a - 5.0 * b + 4.0 * c - d + t * (3.0 * (b - c) + d - a)));
    float wx = (x * VISIBLE + u_world) / VISIBLE;
    h += 0.018 * (fbm(vec2(wx * 9.0, 0.5), 4) - 0.5) + 0.008 * (ridged(vec2(wx * 45.0, 2.0), 3) - 0.4);
    return 1.0 - h;
}
// stars that light up at random places and fade away again; new ones appear elsewhere
vec3 blinking(vec2 px, float cell, float density, float size, float pace) {
    vec2 c = floor(px / cell);
    float h = hash12(c);
    float life = u_t * pace * (0.5 + hash12(c + 4.4)) + h * 13.0;
    float gen = floor(life);
    if (hash12(c + gen * 1.7) > density) return vec3(0.0);       // this cell is dark this time
    vec2 pos = (c + 0.15 + 0.7 * vec2(hash12(c + gen + 3.1), hash12(c + gen + 7.7))) * cell;
    vec2 d = (px - pos) / (u_unit * size);
    float on = sin(3.14159 * fract(life));
    vec3 col = mix(vec3(0.75, 0.85, 1.0), vec3(1.0, 0.95, 0.85), hash12(c + gen + 9.4));
    return col * exp(-dot(d, d)) * on * on * (0.5 + 0.8 * hash12(c + gen + 5.5));
}
// the curtain's lower edge: a free curve across the sky, slowly changing its shape
vec2 path(float t, float c, float aspect, float seed) {
    // the sideways terms are strong enough to fold the band back on itself at times
    float x = mix(0.0, 1.0, t) + 0.20 * sin(TAU * (1.1 * t + c * 0.11) + seed) * (0.6 + 0.4 * sin(c * 0.05 + seed))
              + 0.12 * sin(TAU * (0.45 * t - c * 0.07) + 2.0 * seed) + 0.15 * sin(c * 0.04 + seed * 3.0);
    float y = 0.58 + 0.16 * sin(TAU * (0.7 * t + c * 0.09) + seed)
              + 0.09 * sin(TAU * (1.6 * t - c * 0.13) + 3.0 * seed)
              + 0.05 * sin(TAU * (2.9 * t + c * 0.17) + 5.0 * seed);
    return vec2(x * aspect, y);
}
vec3 curtain(vec2 p, float aspect, float c, float seed, float gain, float height) {
    float bass = u_energy.x;
    // where it glows most: one end, wandering slowly along the band
    float peak = 0.5 + 0.45 * sin(c * 0.13 + seed);
    float second = 0.5 + 0.5 * sin(c * 0.07 + seed * 2.0);       // pink or violet at the far end
    vec3 far = mix(vec3(1.0, 0.25, 0.65), vec3(0.55, 0.25, 1.0), second);
    vec3 acc = vec3(0.0);
    vec2 prev = path(0.0, c, aspect, seed);
    for (int i = 1; i <= SAMPLES; i++) {
        float t = float(i) / float(SAMPLES);
        vec2 q = path(t, c, aspect, seed);
        float seg = max(length(q - prev), 1e-3);
        vec2 a = prev, mid = 0.5 * (prev + q);
        prev = q;
        // each piece of the edge sends a ray straight up
        float dx = abs(p.x * aspect - mid.x);
        vec2 pa = vec2(p.x * aspect, p.y) - a, ba = q - a;
        float dseg = length(pa - ba * clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0));
        float w = seg * 0.9 + 0.004;
        float side = exp(-dx * dx / (w * w));
        float rim = exp(-dseg / 0.008);
        if (side < 0.01 && rim < 0.01) continue;
        float h = mid.y - p.y;                                     // above the edge
        float H = height * (0.75 + 0.5 * vnoise(vec2(t * 9.0, c * 0.4 + seed)) + 0.45 * bass + 0.2 * u_pulse);
        float prof = smoothstep(-0.015, 0.006, h) * exp(-max(h, 0.0) / H) * smoothstep(H * 3.2, H * 1.1, h);
        float along = abs(t - peak);
        float bright = gain * (0.25 + 2.2 * exp(-along * along / 0.05)) * (0.5 + 1.1 * spec(t));
        // colour: yellow-green at the bright end and the lower rim, turquoise, then pink or violet
        float k = clamp(along * 1.4 + max(h, 0.0) / (H * 2.5), 0.0, 1.0);
        vec3 col = mix(vec3(0.75, 1.0, 0.30), vec3(0.25, 1.0, 0.55), smoothstep(0.0, 0.25, k));
        col = mix(col, vec3(0.15, 0.85, 0.85), smoothstep(0.25, 0.55, k));
        col = mix(col, far, smoothstep(0.55, 0.95, k));
        acc += col * bright * (side * prof * (seg / w) + 0.35 * rim);
    }
    // fine rays running upwards
    float rays = 0.65 + 0.55 * vnoise(vec2(p.x * aspect * 120.0 + c * 0.6, seed))
                      * (0.6 + 0.4 * vnoise(vec2(p.x * aspect * 31.0 - c * 0.3, seed + 4.0)));
    return acc * rays * 0.6;
}
void main() {
    vec2 px = v_uv * u_res;
    vec2 p = v_uv;
    float aspect = u_res.x / u_res.y;
    float c = u_clock;
    // deep blue sky, lighter towards the horizon
    vec3 col = mix(vec3(0.01, 0.06, 0.22), vec3(0.05, 0.22, 0.48), smoothstep(0.0, 1.0, p.y));
    col += vec3(0.0, 0.18, 0.22) * 0.25 * fbm(vec2(p.x * aspect * 1.5, p.y * 1.5) + c * 0.03, 3);
    // a realistic number of stars: a few faint steady ones, others come and go
    col += starfield(px, 13.0 * u_unit, 0.06, u_t, 0.7) * 0.5;
    col += blinking(px, 34.0 * u_unit, 0.35, 1.1, 0.05) * 1.2;
    col += blinking(px + 91.0, 90.0 * u_unit, 0.45, 1.6, 0.03) * 1.4;
    // the band of light, and a fainter one further away
    vec3 au = curtain(p, aspect, c, 1.3, 1.0, 0.17);
    au += curtain(p * vec2(1.0, 1.25) - vec2(0.0, 0.32), aspect, c * 0.8, 4.7, 0.35, 0.10);
    col += au;
    col += vec3(0.1, 0.6, 0.45) * 0.10 * smoothstep(0.0, 1.5, dot(au, vec3(0.33))) ;
    // a yellow-green glow behind the mountains on the right
    float ry = ridge_y(p.x);
    float glow = exp(-max(ry - p.y, 0.0) / 0.20) * smoothstep(0.30, 0.95, p.x);
    col += vec3(0.65, 0.90, 0.35) * glow * (0.45 + 0.30 * u_energy.w);
    // the snowy mountains, gently shimmering, with drifting blown snow
    if (p.y > ry - 0.02) {
        float depth = max(p.y - ry, 0.0);
        vec2 sp = vec2((p.x + u_world / VISIBLE) * aspect, p.y);     // the texture moves with the hills
        float slope = fbm(sp * vec2(8.0, 14.0), 5);
        float lightdir = clamp(0.5 + (fbm(sp * vec2(8.0, 14.0) + vec2(0.02, -0.01), 5) - slope) * 18.0, 0.0, 1.0);
        vec3 snow = mix(vec3(0.42, 0.55, 0.70), vec3(0.82, 0.90, 0.98), lightdir);
        snow *= 0.80 + 0.25 * slope;
        float rock = smoothstep(0.60, 0.72, fbm(sp * vec2(60.0, 90.0) + 5.0, 4)) * smoothstep(0.0, 0.10, depth);
        float trees = smoothstep(0.55, 0.68, fbm(sp * vec2(140.0, 200.0) + 9.0, 3)) * smoothstep(0.04, 0.2, depth);
        vec3 m = mix(snow, vec3(0.10, 0.16, 0.22), rock * 0.85);
        m = mix(m, vec3(0.04, 0.08, 0.10), trees * 0.7);
        m *= 1.0 - 0.35 * smoothstep(0.0, 0.25, depth);
        m += vec3(0.2, 0.9, 0.55) * 0.10 * (0.4 + u_energy.w);         // lit by the aurora
        // glitter on the snow and blown snow drifting along the slope
        vec2 g = floor((px + vec2(u_world / VISIBLE * u_res.x, 0.0)) / (3.0 * u_unit));
        float tw = step(0.996, hash12(g)) * pow(max(0.0, sin(u_t * (1.0 + 2.0 * hash12(g + 1.0)) + hash12(g) * 50.0)), 8.0);
        m += vec3(0.9, 0.95, 1.0) * tw * 0.8 * (1.0 - rock);
        float drift = fbm(vec2(sp.x * 4.0 - u_t * 0.05, sp.y * 12.0 + u_t * 0.01), 4);
        m = mix(m, vec3(0.75, 0.85, 0.95), smoothstep(0.55, 0.85, drift) * 0.25 * exp(-depth / 0.06));
        col = mix(col, m, smoothstep(ry, ry + 1.5 / u_res.y, p.y));
    }
    // drifting mist along the ridge
    float mist = fbm(vec2(p.x * aspect * 3.0 - u_t * 0.03, p.y * 9.0), 3);
    col += vec3(0.55, 0.75, 0.85) * 0.10 * mist * exp(-abs(p.y - ry) / 0.035);
    fragColor = vec4(tonemap(col * 1.2) + dither(), 1.0);
}
"""

FLOATS_PER_SHAPE = 20


class Context:
    """GL state shared by all renderers of one GLArea."""

    def __init__(self, es):
        self.gl = gl = G.GL(es)
        self._programs = {}
        # one quad: fullscreen passes and the corners of instanced shapes
        self.quad_vao = gl.vertex_array()
        gl.glBindVertexArray(self.quad_vao)
        self.quad_buf = gl.buffer()
        gl.glBindBuffer(G.ARRAY_BUFFER, self.quad_buf)
        quad = array("f", (0, 0, 1, 0, 0, 1, 1, 1))
        gl.glBufferData(G.ARRAY_BUFFER, len(quad) * 4, quad.buffer_info()[0], G.STATIC_DRAW)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, G.FLOAT, 0, 8, None)
        # instanced shapes
        self.shape_vao = gl.vertex_array()
        gl.glBindVertexArray(self.shape_vao)
        gl.glBindBuffer(G.ARRAY_BUFFER, self.quad_buf)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, G.FLOAT, 0, 8, None)
        self.shape_buf = gl.buffer()
        gl.glBindBuffer(G.ARRAY_BUFFER, self.shape_buf)
        for k in range(5):
            gl.glEnableVertexAttribArray(1 + k)
            gl.glVertexAttribPointer(1 + k, 4, G.FLOAT, 0, FLOATS_PER_SHAPE * 4, k * 16)
            gl.glVertexAttribDivisor(1 + k, 1)
        gl.glBindVertexArray(0)
        self.layers = {}                 # name -> [key, texture]
        self.width = self.height = 1
        self.fbo = 0

    def program(self, name, fragment, vertex=VERTEX,
                attributes=("a_pos",)):
        prog = self._programs.get(name)
        if prog is None:
            prog = self._programs[name] = self.gl.program(vertex, fragment, attributes)
        return prog

    def shapes_program(self):
        return self.program("shapes", SHAPES_FRAGMENT, SHAPES_VERTEX,
                            ("a_corner", "a_p", "a_s", "a_c0", "a_c1", "a_c2"))

    # -- drawing helpers ----------------------------------------------------------------
    def begin(self, width, height, fbo):
        self.width, self.height, self.fbo = width, height, fbo
        gl = self.gl
        for _ in range(16):              # errors left behind by others
            if gl.glGetError() == G.NO_ERROR:
                break
        gl.glViewport(0, 0, width, height)
        gl.glDisable(G.DEPTH_TEST)
        gl.glDisable(G.BLEND)

    def fullscreen(self, prog):
        prog.set("u_res", self.width, self.height)
        self.gl.glBindVertexArray(self.quad_vao)
        self.gl.glDrawArrays(G.TRIANGLE_STRIP, 0, 4)

    def background(self, inner=(0.07, 0.07, 0.085), outer=(0.02, 0.02, 0.03)):
        prog = self.program("background", BACKGROUND).use()
        prog.set("u_inner", *inner)
        prog.set("u_outer", *outer)
        self.gl.glDisable(G.BLEND)
        self.fullscreen(prog)

    def blend(self, mode):
        gl = self.gl
        if mode is None:
            gl.glDisable(G.BLEND)
            return
        gl.glEnable(G.BLEND)
        gl.glBlendEquation(G.MAX if mode == "max" else G.FUNC_ADD)
        if mode in ("add", "max"):
            gl.glBlendFunc(G.ONE, G.ONE)
        elif mode == "screen":
            gl.glBlendFunc(G.ONE, G.ONE_MINUS_SRC_COLOR)
        else:
            gl.glBlendFunc(G.ONE, G.ONE_MINUS_SRC_ALPHA)

    def layer(self, name, key, draw):
        """A texture drawn with cairo (``draw(cr, w, h)``) at the current size,
        redrawn only when ``key`` or the size changes."""
        w, h = self.width, self.height
        full_key = (key, w, h)
        entry = self.layers.get(name)
        if entry is None:
            entry = self.layers[name] = [None, self.gl.texture()]
        if entry[0] != full_key:
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
            cr = cairo.Context(surf)
            draw(cr, w, h)
            surf.flush()
            self.gl.upload_rgba(entry[1], w, h, surf.get_data())
            entry[0] = full_key
        return entry[1]

    def composite(self, tex):
        prog = self.program("overlay", OVERLAY).use()
        self.gl.bind_texture(0, tex)
        prog.set_int("u_tex", 0)
        self.blend("over")
        self.fullscreen(prog)
        self.blend(None)

    def footer(self, caption):
        if caption:
            self.composite(self.layer("footer", caption,
                                      lambda cr, w, h: visuals.footer(cr, w, h, caption)))

    def draw_shapes(self, data, mode="over", res=None, unit=1.0):
        """Draw instanced shapes; ``res`` is the target size in shape units
        (default: pixels), ``unit`` the screen pixels per unit."""
        n = len(data) // FLOATS_PER_SHAPE
        if not n:
            return
        gl = self.gl
        prog = self.shapes_program().use()
        prog.set("u_res", *(res or (self.width, self.height)))
        prog.set("u_unit", unit)
        gl.glBindVertexArray(self.shape_vao)
        gl.glBindBuffer(G.ARRAY_BUFFER, self.shape_buf)
        gl.glBufferData(G.ARRAY_BUFFER, len(data) * 4, data.buffer_info()[0], G.STREAM_DRAW)
        self.blend(mode)
        gl.glDrawArraysInstanced(G.TRIANGLE_STRIP, 0, 4, n)
        self.blend(None)

    def free(self):
        gl = self.gl
        for prog in self._programs.values():
            prog.free()
        for _key, tex in self.layers.values():
            gl.delete_texture(tex)
        gl.delete_buffer(self.quad_buf)
        gl.delete_buffer(self.shape_buf)
        gl.delete_vertex_array(self.quad_vao)
        gl.delete_vertex_array(self.shape_vao)
        self._programs, self.layers = {}, {}


class Shapes:
    """Collects instanced shapes: rectangles, round-capped lines and discs."""

    def __init__(self):
        self.data = array("f")

    def rect(self, x0, y0, x1, y1, rgba):
        self.data.extend((x0, y0, x1, y1, 0, 0, 0, 0, *rgba, 0, 0, 0, 0, 0, 0, 0, 0))

    def line(self, x0, y0, x1, y1, width, rgba):
        self.data.extend((x0, y0, x1, y1, width / 2, 1, 0, 0, *rgba, 0, 0, 0, 0, 0, 0, 0, 0))

    def disc(self, x, y, r, inner, outer=None, middle=None, mid=0.5):
        if outer is None:
            outer = middle = inner
        elif middle is None:
            middle = tuple((a + b) / 2 for a, b in zip(inner, outer))
        self.data.extend((x, y, x, y, r, 2, mid, 0, *inner, *middle, *outer))

    def glow(self, x, y, r, rgba):
        self.data.extend((x, y, x, y, r, 3, 0, 0, *rgba, 0, 0, 0, 0, 0, 0, 0, 0))


class Renderer:
    """Draws one style; ``render`` is called with the GL context current."""

    def __init__(self, ctx, style):
        self.ctx, self.style, self.gl = ctx, style, ctx.gl

    def render(self, f):
        raise NotImplementedError

    def free(self):
        pass


class BlankRenderer(Renderer):
    def render(self, f):
        self.ctx.background()
        if f.caption:
            self.ctx.composite(self.ctx.layer(
                "caption", f.caption,
                lambda cr, w, h: visuals.caption(cr, w, h, f.caption, alpha=0.35)))


class GlowRenderer(Renderer):
    def render(self, f):
        ctx = self.ctx
        h = ctx.height
        waves = self.style.params(f, h)
        prog = ctx.program("glow", GLOW).use()
        prog.set("u_inner", 0.06, 0.08, 0.13)
        prog.set("u_outer", 0.02, 0.025, 0.04)
        prog.set("u_lw", max(1.5, h / 300))
        prog.set_int("u_count", len(waves))
        if waves:
            prog.set_array("u_col", [c for wv in waves for c in wv[0]], 3)
            prog.set_array("u_wave", [v for wv in waves for v in wv[1:5]], 4)
            prog.set_array("u_amp", [wv[5] for wv in waves], 1)
        ctx.fullscreen(prog)
        ctx.footer(f.caption)


class BarsRenderer(Renderer):
    def __init__(self, ctx, style):
        super().__init__(ctx, style)
        self.tex = self.gl.texture(G.NEAREST)

    def render(self, f):
        ctx, gl = self.ctx, self.gl
        w, h = ctx.width, ctx.height
        n = len(f.bands)
        margin_x, top, bottom = w * 0.06, h * 0.08, h * 0.14
        area_w, area_h = w - 2 * margin_x, h - top - bottom
        gap = max(1.0, area_w / n * 0.18)
        bw = area_w / n - gap
        seg_h = max(3.0, area_h / 32)
        segs = max(4, int(area_h / seg_h))
        seg_gap = max(1.0, seg_h * 0.25)
        gl.upload_floats(self.tex, list(f.bands) + list(f.peaks), n, 2)
        prog = ctx.program("bars", BARS).use()
        prog.set("u_inner", 0.07, 0.07, 0.085)
        prog.set("u_outer", 0.02, 0.02, 0.03)
        gl.bind_texture(0, self.tex)
        prog.set_int("u_data", 0)
        prog.set_int("u_n", n)
        prog.set("u_box", margin_x, top, area_w, area_h)
        prog.set("u_bar", gap, bw)
        prog.set("u_seg", seg_h, segs, seg_gap)
        ctx.fullscreen(prog)
        ctx.footer(f.caption)

    def free(self):
        self.gl.delete_texture(self.tex)


class ScopeRenderer(Renderer):
    def __init__(self, ctx, style):
        super().__init__(ctx, style)
        self.tex = self.gl.texture(G.NEAREST)

    def render(self, f):
        ctx, gl = self.ctx, self.gl
        wave = f.wave if len(f.wave) >= 2 else [0.0, 0.0]
        gl.upload_floats(self.tex, wave, len(wave), 1)
        prog = ctx.program("scope", SCOPE).use()
        gl.bind_texture(0, self.tex)
        prog.set_int("u_wave", 0)
        prog.set_int("u_n", len(wave))
        prog.set("u_amp", ctx.height * 0.38)
        ctx.fullscreen(prog)
        ctx.footer(f.caption)

    def free(self):
        self.gl.delete_texture(self.tex)


class NebulaRenderer(Renderer):
    def render(self, f):
        ctx, st = self.ctx, self.style
        w, h = ctx.width, ctx.height
        ctx.background(*st.BG)
        ctx.footer(f.caption)
        cx, cy, scale = w / 2, h / 2, min(w, h)
        sh = Shapes()
        for x, y, nx, ny, size, col, fade in st.step(f):
            sh.line(cx + x * scale, cy + y * scale,
                    cx + (nx + (nx - x) * 4) * scale, cy + (ny + (ny - y) * 4) * scale,
                    max(0.6, size * 0.7), (*col, 0.35 * fade))
            sh.disc(cx + nx * scale, cy + ny * scale, size, (*col, 0.8 * fade))
        for ox, oy, r in st.orbs(f, w, h):
            sh.disc(ox, oy, r, (1.0, 0.75, 0.85, 0.35), (1.0, 0.75, 0.85, 0.0))
        sh.disc(cx, cy, st.core(f, w, h), (1, 1, 1, 0.95), (*f.accent, 0.0),
                middle=(1.0, 0.80, 0.90, 0.55), mid=0.15)
        ctx.draw_shapes(sh.data, "add")


class VURenderer(Renderer):
    def render(self, f):
        ctx, st, gl = self.ctx, self.style, self.gl
        w, h = ctx.width, ctx.height
        meters = st.meters(w, h)

        def faces(cr, w, h):
            visuals.background(cr, w, h)
            for x, y, mw, mh, _ch, name in meters:
                st.face(cr, x, y, mw, mh, name)

        def glass(cr, w, h):
            for x, y, mw, mh, _ch, _name in meters:
                st.glass(cr, x, y, mw, mh)
            visuals.footer(cr, w, h, f.caption)

        ctx.composite(ctx.layer("vu-faces", "faces", faces))
        for x, y, mw, mh, ch, _name in meters:
            sh = Shapes()
            lx, ly, lr, lit = st.led(x, y, mw, mh, f.peak_db[ch] > -1.0)
            sh.disc(lx, ly, lr, (*lit, 1.0))
            ctx.draw_shapes(sh.data)
            cx, cy, ex, ey, width = st.needle(x, y, mw, mh, f.vu[ch] - st.REF_DBFS)
            sh = Shapes()
            sh.line(cx + 3, cy + 3, ex + 3, ey + 3, max(1.5, mh / 70), (0, 0, 0, 0.18))
            sh.line(cx, cy, ex, ey, width, (0.08, 0.06, 0.05, 1.0))
            gl.glEnable(G.SCISSOR_TEST)
            gl.glScissor(int(x), int(h - (y + mh)), int(math.ceil(mw)), int(math.ceil(mh)))
            ctx.draw_shapes(sh.data)
            gl.glDisable(G.SCISSOR_TEST)
        ctx.composite(ctx.layer("vu-glass", f.caption, glass))


class RibbonsRenderer(Renderer):
    def render(self, f):
        ctx, st = self.ctx, self.style
        h = ctx.height
        ribbons = st.params(f, h)
        prog = ctx.program("ribbons", RIBBONS).use()
        prog.set("u_t", f.t)
        prog.set("u_unit", h / 1080)
        prog.set("u_horizon", st.HORIZON)
        prog.set("u_mirror", st.MIRROR)
        prog.set("u_glow", st.glow)
        prog.set_array("u_spec", st.spectrum.values, 1)
        prog.set_int("u_count", len(ribbons))
        prog.set_array("u_ra", [v for r in ribbons for v in r[0:4]], 4)
        prog.set_array("u_rb", [v for r in ribbons for v in r[4:8]], 4)
        prog.set_array("u_front", [c for r in ribbons for c in r[8]], 3)
        prog.set_array("u_back", [c for r in ribbons for c in r[9]], 3)
        ctx.fullscreen(prog)
        ctx.footer(f.caption)


class PulsarRenderer(Renderer):
    def render(self, f):
        ctx, st = self.ctx, self.style
        w, h = ctx.width, ctx.height
        p = st.params(f, w, h)
        prog = ctx.program("pulsar", PULSAR).use()
        prog.set("u_t", f.t)
        prog.set("u_unit", min(w, h) / 1080)
        prog.set("u_r0", p["r0"])
        prog.set("u_pulse", p["pulse"])
        prog.set("u_level", p["level"])
        prog.set("u_spin", p["spin"])
        prog.set("u_aura", *p["aura"])
        prog.set_array("u_spec", st.spectrum.values, 1)
        layers = p["layers"]
        prog.set_int("u_layers", len(layers))
        prog.set_array("u_layer", [v for ly in layers for v in ly[0:4]], 4)
        prog.set_array("u_lcol", [c for ly in layers for c in ly[4]], 3)
        orbits = p["orbits"]
        prog.set_array("u_orbit", [v for o in orbits for v in o[0:4]], 4)
        prog.set_array("u_orbit2", [v for o in orbits for v in (o[4], o[5], o[6], 0.0)], 4)
        prog.set_array("u_ocol", [c for o in orbits for c in o[7]], 3)
        ctx.fullscreen(prog)
        cx, cy = w / 2, h / 2
        sh = Shapes()
        for x, y, size, col, fade in st.step(f, p["r0"], min(w, h)):
            sh.disc(cx + x, cy + y, size, (*col, 0.9 * fade), (*col, 0.0), mid=0.4)
        ctx.draw_shapes(sh.data, "add")
        ctx.footer(f.caption)


class SceneRenderer(Renderer):
    """The painted scenes: one fullscreen shader, the music as a few uniforms."""
    SHADERS = {}

    def render(self, f):
        ctx, st = self.ctx, self.style
        clock, bass, mid, treble, pulse, level = st.params(f)
        prog = ctx.program(st.id, self.SHADERS[type(st)]).use()
        prog.set("u_t", f.t)
        prog.set("u_clock", clock)
        prog.set("u_unit", min(ctx.width, ctx.height) / 1080)
        prog.set("u_pulse", pulse)
        prog.set("u_energy", bass, mid, treble, level)
        prog.set_array("u_spec", st.spectrum.values or [0.0] * 48, 1)
        if isinstance(st, visuals.OccasusSolis):
            prog.set("u_sun", *st.SUN)
        if isinstance(st, visuals.AuroraBorealis):
            prog.set_array("u_ridge", st.ridge, 1)
            prog.set("u_scroll", st.scroll)
            prog.set("u_world", st.world)
        if isinstance(st, visuals.Supernova):
            prog.set("u_flow", st.flow)
            prog.set_array("u_veil", st.veils, 1)
        ctx.fullscreen(prog)
        ctx.footer(f.caption)


SceneRenderer.SHADERS = {
    visuals.Supernova: SUPERNOVA,
    visuals.OccasusSolis: SUNSET,
    visuals.AuroraBorealis: AURORA,
}


class GoomRenderer(Renderer):
    """Goom HD: GOOM's frames (lindvb.goom) drawn at the screen's resolution.

    Per GOOM frame, like goom_core.c: the IFS and the dots go into the picture
    (A), the zoom filter moves A into B along a blend of two displacement fields,
    the shown frame is A plus the fireworks and the bright tentacles, and the
    dim tentacles and the scope lines go into B, which becomes the next A.
    """

    FIELD_MAX = (1280, 720)          # the fields are smooth, they need no more
    KEEP = 252.0 / 256.0             # light kept by GOOM's bilinear coefficients
    # GOOM measures its effects in half widths, so on a wide screen the "ball" of
    # the amulette and crystal modes is cut off at the top and bottom. Measured in
    # screen heights instead (1.1 = 220 of GOOM's 200 lines, instead of its width) it fits with a margin.
    SCALE = 1.1

    def __init__(self, ctx, style):
        super().__init__(ctx, style)
        self.size = None
        self.pic = [0, 0]            # A, B (feedback)
        self.pic_fbo = [0, 0]
        self.shown = self.shown_fbo = 0
        self.ifs_tex = self.ifs_fbo = 0
        self.fields = []             # [source, dest, temp] float textures
        self.field_fbos = []
        self.fsize = (1, 1)
        self.float_fields = True
        self.params = [None, None]   # source / dest Field (fallback without textures)
        self.frozen = 0.0
        self.ytex = self.gl.texture(G.NEAREST)
        self.vres = (320, 200)

    # -- resources ----------------------------------------------------------------------
    def _target(self, w, h, internal=G.RGBA8, fmt=G.RGBA, kind=G.UNSIGNED_BYTE):
        gl = self.gl
        tex = gl.texture(G.LINEAR)
        gl.glBindTexture(G.TEXTURE_2D, tex)
        gl.glTexImage2D(G.TEXTURE_2D, 0, internal, w, h, 0, fmt, kind, None)
        fbo = gl.framebuffer(tex)
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(G.COLOR_BUFFER_BIT)
        return tex, fbo

    def _resize(self, w, h):
        self.free()
        for k in range(2):
            self.pic[k], self.pic_fbo[k] = self._target(w, h)
        self.shown, self.shown_fbo = self._target(w, h)
        self.ifs_tex, self.ifs_fbo = self._target(w, h)
        f = min(1.0, self.FIELD_MAX[0] / w, self.FIELD_MAX[1] / h)
        self.fsize = (max(16, int(w * f)), max(16, int(h * f)))
        self.fields, self.field_fbos = [], []
        if self.float_fields:
            try:
                for _ in range(3):
                    tex, fbo = self._target(*self.fsize, G.RGBA16F, G.RGBA, G.HALF_FLOAT)
                    self.fields.append(tex)
                    self.field_fbos.append(fbo)
            except G.GLError:
                for tex, fbo in zip(self.fields, self.field_fbos):
                    self.gl.delete_framebuffer(fbo)
                    self.gl.delete_texture(tex)
                self.fields, self.field_fbos = [], []
                self.float_fields = False
        self.size = (w, h)
        core = self.style.core
        if core is not None:
            init = core.zoom.initial
            self.params = [init, init]
            if self.fields:
                self._make_field(init, 0)
                self._make_field(init, 1)

    def free(self):
        gl = self.gl
        for k in range(2):
            gl.delete_framebuffer(self.pic_fbo[k])
            gl.delete_texture(self.pic[k])
            self.pic_fbo[k] = self.pic[k] = 0
        for fbo, tex in ((self.shown_fbo, self.shown), (self.ifs_fbo, self.ifs_tex)):
            gl.delete_framebuffer(fbo)
            gl.delete_texture(tex)
        self.shown = self.shown_fbo = self.ifs_tex = self.ifs_fbo = 0
        for tex, fbo in zip(self.fields, self.field_fbos):
            gl.delete_framebuffer(fbo)
            gl.delete_texture(tex)
        self.fields, self.field_fbos = [], []
        self.size = None

    # -- fields -------------------------------------------------------------------------
    def _make_field(self, fd, slot):
        gl, ctx = self.gl, self.ctx
        gl.glBindFramebuffer(G.FRAMEBUFFER, self.field_fbos[slot])
        gl.glViewport(0, 0, *self.fsize)
        prog = ctx.program("goom-field", GOOM_FIELD).use()
        prog.set("u_res", *self.fsize)
        prog.set("u_vres", *self.vres)
        prog.set("u_scale", self.SCALE)
        prog.set("u_middle", *fd.middle)
        prog.set("u_speed", fd.speed)
        prog.set("u_seed", fd.seed)
        prog.set_int("u_mode", fd.mode)
        prog.set_int("u_hypercos", fd.hypercos)
        prog.set_int("u_noisify", fd.noisify)
        prog.set("u_plane", fd.h_plane, fd.v_plane)
        gl.glBindVertexArray(ctx.quad_vao)
        gl.glDrawArrays(G.TRIANGLE_STRIP, 0, 4)

    def _new_field(self, fd, freeze):
        if not self.fields:
            # without float textures: keep the parameters, snap to the nearer one
            self.params = [self.params[1] if freeze >= 0.5 else self.params[0], fd]
            return
        gl, ctx = self.gl, self.ctx
        # temp = mix(source, dest, freeze); source <- temp; dest <- new field
        gl.glBindFramebuffer(G.FRAMEBUFFER, self.field_fbos[2])
        gl.glViewport(0, 0, *self.fsize)
        prog = ctx.program("goom-field-mix", GOOM_FIELD_MIX).use()
        prog.set("u_res", *self.fsize)
        gl.bind_texture(0, self.fields[0])
        gl.bind_texture(1, self.fields[1])
        prog.set_int("u_src", 0)
        prog.set_int("u_dst", 1)
        prog.set("u_blend", freeze)
        gl.glBindVertexArray(ctx.quad_vao)
        gl.glDrawArrays(G.TRIANGLE_STRIP, 0, 4)
        self.fields[0], self.fields[2] = self.fields[2], self.fields[0]
        self.field_fbos[0], self.field_fbos[2] = self.field_fbos[2], self.field_fbos[0]
        self._make_field(fd, 1)

    # -- drawing ------------------------------------------------------------------------
    def _bind(self, fbo):
        self.gl.glBindFramebuffer(G.FRAMEBUFFER, fbo)
        self.gl.glViewport(0, 0, *self.size)

    def _ifs(self, ifs, unit):
        sims, depth, colour, incr = ifs
        nb = len(sims)
        if nb < 2:
            return
        ctx, gl = self.ctx, self.gl
        per = sum(nb ** lv for lv in range(1, depth + 2))
        total = nb * (nb - 1) * per
        prog = ctx.program("goom-ifs", SHAPES_FRAGMENT, GOOM_IFS_VERTEX, ("a_corner",)).use()
        vw, vh = self.vres
        prog.set("u_res", vw, vh)
        prog.set("u_unit", unit)
        prog.set_int("u_nb", nb)
        prog.set_int("u_depth", depth)
        prog.set_int("u_per", per)
        prog.set_int("u_incr", max(1, incr))
        prog.set_array("u_sim", [v for s in sims for v in (s[0], s[1], s[2], s[3])], 4)
        prog.set_array("u_ang", [v for s in sims for v in (math.cos(s[4]), math.sin(s[4]),
                                                          math.cos(s[5]), math.sin(s[5]))], 4)
        prog.set("u_l", (vw - 1) // 2, (vh - 1) // 2)
        prog.set("u_col", *(c / 255 for c in colour), 1.0)
        prog.set("u_rad", 0.55)
        # GOOM sets each point once (picture + colour): overlapping points must
        # not add up, so take the brightest in a layer and add that layer once
        self._bind(self.ifs_fbo)
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(G.COLOR_BUFFER_BIT)
        gl.glBindVertexArray(ctx.quad_vao)
        ctx.blend("max")
        gl.glDrawArraysInstanced(G.TRIANGLE_STRIP, 0, 4, (total + incr - 1) // max(1, incr))
        self._layer_onto(self.pic_fbo[0])

    def _tentacles(self, tent, unit, low, target):
        grids, rot, dist, colour, colour_low = tent
        ctx, gl = self.ctx, self.gl
        ys, grid_u, cam_u = [], [], []
        row = first = 0
        for g in grids:
            ys.extend(g.y)
            grid_u.extend((g.sizex, g.sizez, g.defz, row))
            cam_u.extend((*g.cam, first))
            row += g.defz
            first += g.defx * (g.defz - 1)
        gl.upload_floats(self.ytex, ys, grids[0].defx, row)
        prog = ctx.program("goom-tentacles", SHAPES_FRAGMENT, GOOM_TENTACLE_VERTEX,
                           ("a_corner",)).use()
        prog.set("u_res", *self.vres)
        prog.set("u_unit", unit)
        gl.bind_texture(0, self.ytex)
        prog.set_int("u_y", 0)
        prog.set_array("u_grid", grid_u, 4)
        prog.set_array("u_cam", cam_u, 4)
        prog.set("u_rot", math.sin(rot), math.cos(rot))
        prog.set("u_dist", dist)
        prog.set("u_width", max(1.0, 1.3 / unit) if low else max(0.9, 1.2 / unit))
        prog.set("u_col", *(c / 255 for c in (colour_low if low else colour)), 1.0)
        # segments overlap at their joints: take the brightest in a layer and add
        # that once, or every joint would show as a brighter bead
        self._bind(self.ifs_fbo)
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(G.COLOR_BUFFER_BIT)
        gl.glBindVertexArray(ctx.quad_vao)
        ctx.blend("max")
        gl.glDrawArraysInstanced(G.TRIANGLE_STRIP, 0, 4, first)
        self._layer_onto(target)

    def _layer_onto(self, target):
        ctx, gl = self.ctx, self.gl
        self._bind(target)
        ctx.blend("add")
        prog = ctx.program("goom-copy", GOOM_COPY).use()
        prog.set("u_res", *self.size)
        gl.bind_texture(0, self.ifs_tex)
        prog.set_int("u_tex", 0)
        gl.glBindVertexArray(ctx.quad_vao)
        gl.glDrawArrays(G.TRIANGLE_STRIP, 0, 4)
        ctx.blend(None)

    def _step(self, fr, unit):
        ctx, gl = self.ctx, self.gl
        vw, vh = self.vres
        a_tex, b_tex = self.pic
        a_fbo, b_fbo = self.pic_fbo
        line_w = max(0.6, 1.4 / unit)
        # 1. IFS and dots into the picture, before the zoom
        self._bind(a_fbo)
        if fr.ifs is not None:
            self._ifs(fr.ifs, unit)
        if fr.points:
            sh = Shapes()
            for x, y, col in fr.points:
                rgba = (col[0] / 255, col[1] / 255, col[2] / 255, 1.0)
                for dx, dy in ((1, 0), (0, 1), (2, 1), (1, 2)):
                    sh.disc(x + dx + 0.5, y + dy + 0.5, 0.6, rgba)
                sh.disc(x + 1.5, y + 1.5, 0.6, (1.0, 1.0, 1.0, 1.0))
            ctx.draw_shapes(sh.data, "over", (vw, vh), unit)
        # 2. zoom filter: A -> B
        if fr.new_field is not None:
            self._new_field(fr.new_field, fr.freeze_blend)
        self._bind(b_fbo)
        prog = ctx.program("goom-warp", GOOM_WARP).use()
        prog.set("u_res", *self.size)
        prog.set("u_vres", vw, vh)
        prog.set("u_scale", self.SCALE)
        gl.bind_texture(0, a_tex)
        prog.set_int("u_prev", 0)
        prog.set("u_blend", fr.blend)
        prog.set("u_keep", self.KEEP)
        if self.fields:
            gl.bind_texture(1, self.fields[0])
            gl.bind_texture(2, self.fields[1])
            prog.set_int("u_src", 1)
            prog.set_int("u_dst", 2)
            prog.set_int("u_textures", 1)
        else:
            prog.set_int("u_textures", 0)
            for k, fd in enumerate(self.params):
                prog.set(f"u_fa{k}", fd.middle[0], fd.middle[1], fd.speed, fd.seed)
                prog.set(f"u_fb{k}", fd.mode, fd.hypercos, fd.noisify, 0)
            p0, p1 = self.params
            prog.set("u_planes", p0.h_plane, p0.v_plane, p1.h_plane, p1.v_plane)
        gl.glBindVertexArray(ctx.quad_vao)
        gl.glDrawArrays(G.TRIANGLE_STRIP, 0, 4)
        # 3. the shown frame: A + fireworks + bright tentacles
        self._bind(self.shown_fbo)
        prog = ctx.program("goom-copy", GOOM_COPY).use()
        prog.set("u_res", *self.size)
        gl.bind_texture(0, a_tex)
        prog.set_int("u_tex", 0)
        gl.glDrawArrays(G.TRIANGLE_STRIP, 0, 4)
        if fr.tentacles is not None:
            self._tentacles(fr.tentacles, unit, low=False, target=self.shown_fbo)
        if fr.stars:
            sh = Shapes()
            for x0, y0, x1, y1, col in fr.stars:
                if 0 <= x0 < vw and 0 <= x1 < vw and 0 <= y0 < vh and 0 <= y1 < vh:
                    sh.line(x0 + 0.5, y0 + 0.5, x1 + 0.5, y1 + 0.5, line_w,
                            (col[0] / 255, col[1] / 255, col[2] / 255, 1.0))
            ctx.draw_shapes(sh.data, "add", (vw, vh), unit)
        # 4. dim tentacles and scope lines into B
        self._bind(b_fbo)
        if fr.tentacles is not None:
            self._tentacles(fr.tentacles, unit, low=True, target=b_fbo)
        # one layer per line, so its joints don't add up into beads (two lines still do)
        for pts, col in fr.lines:
            sh = Shapes()
            rgba = (col[0] / 255, col[1] / 255, col[2] / 255, 1.0)
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                if 0 <= x0 < vw and 0 <= x1 < vw and 0 <= y0 < vh and 0 <= y1 < vh:
                    sh.line(x0 + 0.5, y0 + 0.5, x1 + 0.5, y1 + 0.5, line_w, rgba)
            if not sh.data:
                continue
            self._bind(self.ifs_fbo)
            gl.glClearColor(0, 0, 0, 1)
            gl.glClear(G.COLOR_BUFFER_BIT)
            ctx.draw_shapes(sh.data, "max", (vw, vh), unit)
            self._layer_onto(b_fbo)
        self.pic.reverse()
        self.pic_fbo.reverse()

    def render(self, f):
        ctx, gl = self.ctx, self.gl
        w, h, screen = ctx.width, ctx.height, ctx.fbo
        frames = self.style.steps(f, w, h)
        core = self.style.core
        self.vres = (core.w, core.h)
        if self.size != (w, h):
            self._resize(w, h)
        unit = h / core.h
        for fr in frames:
            self._step(fr, unit)
        gl.glBindFramebuffer(G.FRAMEBUFFER, screen)
        gl.glViewport(0, 0, w, h)
        prog = ctx.program("goom-copy", GOOM_COPY).use()
        gl.bind_texture(0, self.shown)
        prog.set_int("u_tex", 0)
        ctx.fullscreen(prog)
        ctx.footer(f.caption)

    def release(self):
        self.free()
        self.gl.delete_texture(self.ytex)


RENDERERS = {
    visuals.Blank: BlankRenderer,
    visuals.GlowWaves: GlowRenderer,
    visuals.SpectrumBars: BarsRenderer,
    visuals.Oscilloscope: ScopeRenderer,
    visuals.Nebula: NebulaRenderer,
    visuals.VUMeters: VURenderer,
    visuals.LightRibbons: RibbonsRenderer,
    visuals.Pulsar: PulsarRenderer,
    visuals.Supernova: SceneRenderer,
    visuals.OccasusSolis: SceneRenderer,
    visuals.AuroraBorealis: SceneRenderer,
    visuals.GoomHD: GoomRenderer,
}


def renderer_class(style):
    """The GPU renderer for a style, or ``None`` (then it is drawn with cairo)."""
    return RENDERERS.get(type(style))
