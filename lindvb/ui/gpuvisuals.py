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
float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7)) + u_seed * 91.7) * 43758.5453); }
void main() {
    float ratio = 2.0 / u_vres.x;
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
float hash(vec2 p, float seed) { return fract(sin(dot(p, vec2(127.1, 311.7)) + seed * 91.7) * 43758.5453); }
vec2 field(vec2 uv, vec4 a, vec4 b, vec2 plane) {
    // a: middle x, middle y, speed, seed   b: mode, hypercos, noisify, -
    float ratio = 2.0 / u_vres.x;
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
    vec2 v = vec2(float(int(u_dist * sx / sz)), float(int(u_dist * sy / sz)));
    vec2 p = vec2(v.x + float(int(u_res.x) / 2), -v.y + float(int(u_res.y) / 2));
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


class GoomRenderer(Renderer):
    """Goom HD: GOOM's frames (lindvb.goom) drawn at the screen's resolution.

    Per GOOM frame, like goom_core.c: the IFS and the dots go into the picture
    (A), the zoom filter moves A into B along a blend of two displacement fields,
    the shown frame is A plus the fireworks and the bright tentacles, and the
    dim tentacles and the scope lines go into B, which becomes the next A.
    """

    FIELD_MAX = (1280, 720)          # the fields are smooth, they need no more
    KEEP = 252.0 / 256.0             # light kept by GOOM's bilinear coefficients

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
        self._bind(self.pic_fbo[0])
        ctx.blend("add")
        prog = ctx.program("goom-copy", GOOM_COPY).use()
        prog.set("u_res", *self.size)
        gl.bind_texture(0, self.ifs_tex)
        prog.set_int("u_tex", 0)
        gl.glDrawArrays(G.TRIANGLE_STRIP, 0, 4)
        ctx.blend(None)

    def _tentacles(self, tent, unit, low):
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
        gl.glBindVertexArray(ctx.quad_vao)
        ctx.blend("add")
        gl.glDrawArraysInstanced(G.TRIANGLE_STRIP, 0, 4, first)
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
            self._tentacles(fr.tentacles, unit, low=False)
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
            self._tentacles(fr.tentacles, unit, low=True)
        if fr.lines:
            sh = Shapes()
            for pts, col in fr.lines:
                rgba = (col[0] / 255, col[1] / 255, col[2] / 255, 1.0)
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                    if 0 <= x0 < vw and 0 <= x1 < vw and 0 <= y0 < vh and 0 <= y1 < vh:
                        sh.line(x0 + 0.5, y0 + 0.5, x1 + 0.5, y1 + 0.5, line_w, rgba)
            ctx.draw_shapes(sh.data, "add", (vw, vh), unit)
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
    visuals.GoomHD: GoomRenderer,
}


def renderer_class(style):
    """The GPU renderer for a style, or ``None`` (then it is drawn with cairo)."""
    return RENDERERS.get(type(style))
