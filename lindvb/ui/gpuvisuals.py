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

SHAPES_VERTEX = """
in vec2 a_corner;
in vec4 a_p;          // x0 y0 x1 y1 in pixels (top left origin)
in vec4 a_s;          // radius / half width, kind, middle stop, -
in vec4 a_c0;
in vec4 a_c1;
in vec4 a_c2;
uniform vec2 u_res;
out vec2 v_px;
flat out vec4 v_p;
flat out vec4 v_s;
flat out vec4 v_c0;
flat out vec4 v_c1;
flat out vec4 v_c2;
void main() {
    int kind = int(a_s.y + 0.5);
    float r = a_s.x;
    vec2 pos;
    if (kind == 0) {
        pos = mix(a_p.xy, a_p.zw, a_corner);
    } else if (kind == 1) {
        vec2 d = a_p.zw - a_p.xy;
        float len = length(d);
        vec2 dir = len > 1e-4 ? d / len : vec2(1.0, 0.0);
        vec2 nrm = vec2(-dir.y, dir.x);
        float e = r + 1.0;
        pos = a_p.xy - dir * e + dir * (len + 2.0 * e) * a_corner.x + nrm * e * (a_corner.y * 2.0 - 1.0);
    } else {
        pos = a_p.xy + (a_corner * 2.0 - 1.0) * (r + 1.0);
    }
    v_px = pos; v_p = a_p; v_s = a_s; v_c0 = a_c0; v_c1 = a_c1; v_c2 = a_c2;
    vec2 ndc = pos / u_res * 2.0 - 1.0;
    gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);
}
"""

SHAPES_FRAGMENT = """
in vec2 v_px;
flat in vec4 v_p;
flat in vec4 v_s;
flat in vec4 v_c0;
flat in vec4 v_c1;
flat in vec4 v_c2;
out vec4 fragColor;
vec4 pm(vec4 c) { return vec4(c.rgb * c.a, c.a); }
void main() {
    int kind = int(v_s.y + 0.5);
    float r = v_s.x;
    if (kind == 0) { fragColor = pm(v_c0); return; }
    if (kind == 1) {                                  // line with round caps
        vec2 pa = v_px - v_p.xy, ba = v_p.zw - v_p.xy;
        float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-6), 0.0, 1.0);
        float d = length(pa - ba * h);
        fragColor = pm(v_c0) * clamp(r - d + 0.5, 0.0, 1.0);
        return;
    }
    float d = length(v_px - v_p.xy);
    float t = clamp(d / max(r, 1e-3), 0.0, 1.0);
    if (kind == 2) {                                  // disc with a 3-stop radial gradient
        float m = clamp(v_s.z, 1e-4, 1.0 - 1e-4);
        vec4 c = t < m ? mix(pm(v_c0), pm(v_c1), t / m) : mix(pm(v_c1), pm(v_c2), (t - m) / (1.0 - m));
        fragColor = c * clamp(r - d + 0.5, 0.0, 1.0);
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

GOOM_WARP = PRELUDE + """
uniform sampler2D u_prev;
uniform vec2 u_mode;         // modes being blended
uniform float u_mix, u_speed, u_amp, u_rot, u_t, u_dt, u_decay;
uniform vec2 u_plane, u_centre;
vec2 disp(int m, vec2 p) {
    float r2 = dot(p, p), r = sqrt(r2), a = u_amp;
    if (m == 1) return p * (1.0 + 0.6 * a * sin(r * 18.0 - u_t * 4.0));          // wave
    if (m == 2) return p * (1.0 - 2.2 * a * r2);                                   // crystal ball
    if (m == 3) return p * (1.0 + 2.5 * a * r2);                                   // scrunch
    if (m == 4) return p * (1.0 + 1.6 * a * r);                                    // amulet
    if (m == 5) return p + 0.35 * a * vec2(sin(p.y * 22.0 + u_t * 2.0), sin(p.x * 22.0 + u_t * 1.7));  // water
    if (m == 6) return p + 0.5 * a * vec2(sin(p.y * 9.0), sin(p.x * 9.0));         // hypercos
    if (m == 7) return vec2(p.x * 0.6, (p.y + 0.5) * 1.8 * a);                      // speedway
    if (m == 8) return vec2(0.15 * sin(p.y * 6.0 + u_t), p.y * 1.8 * a);            // y only
    return p;                                                                       // zoom
}
void main() {
    vec2 asp = vec2(u_res.x / u_res.y, 1.0);
    vec2 p = (v_uv - u_centre) * asp;
    vec2 d = mix(disp(int(u_mode.x + 0.5), p), disp(int(u_mode.y + 0.5), p), u_mix);
    vec2 src = p - d * u_speed * u_dt;
    src -= u_rot * u_dt * vec2(-p.y, p.x);
    src -= u_dt * vec2(p.y * u_plane.x, p.x * u_plane.y);
    vec2 uv = src / asp + u_centre;
    vec4 c = vec4(0.0);
    if (uv.x > 0.0 && uv.y > 0.0 && uv.x < 1.0 && uv.y < 1.0) c = tap(u_prev, uv);
    vec3 rgb = mix(c.rgb, c.gbr, 0.5 * u_dt);         // colours drift slowly in the trails
    fragColor = vec4(max(rgb * u_decay - 0.75 / 255.0, 0.0), 1.0);
}
"""

GOOM_PRESENT = PRELUDE + """
uniform sampler2D u_tex;
uniform float u_flash;
void main() {
    vec3 c = tap(u_tex, v_uv).rgb;
    c += u_flash * 0.12 * (1.0 - c);
    fragColor = vec4(c + dither(), 1.0);
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
        if mode == "add":
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

    def draw_shapes(self, data, mode="over"):
        n = len(data) // FLOATS_PER_SHAPE
        if not n:
            return
        gl = self.gl
        prog = self.shapes_program().use()
        prog.set("u_res", self.width, self.height)
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
    """Goom HD: a full-resolution feedback loop on two textures."""

    def __init__(self, ctx, style):
        super().__init__(ctx, style)
        self.size = None
        self.tex = [0, 0]
        self.fbos = [0, 0]

    def _resize(self, w, h):
        self.free()
        gl = self.gl
        for k in range(2):
            self.tex[k] = gl.texture(G.LINEAR)
            gl.upload_rgba(self.tex[k], w, h, None)
            self.fbos[k] = gl.framebuffer(self.tex[k])
            gl.glClearColor(0, 0, 0, 1)
            gl.glClear(G.COLOR_BUFFER_BIT)
        self.size = (w, h)

    def render(self, f):
        ctx, gl = self.ctx, self.gl
        w, h, screen = ctx.width, ctx.height, ctx.fbo
        if self.size != (w, h):
            self._resize(w, h)
        st = self.style.update(f, w, h)
        src, dst = self.tex[0], self.fbos[1]
        # 1. zoom/bend/fade the previous picture
        gl.glBindFramebuffer(G.FRAMEBUFFER, dst)
        gl.glViewport(0, 0, w, h)
        prog = ctx.program("goom-warp", GOOM_WARP).use()
        gl.bind_texture(0, src)
        prog.set_int("u_prev", 0)
        prog.set("u_mode", st.mode_a, st.mode_b)
        prog.set("u_mix", st.mix * st.mix * (3 - 2 * st.mix))
        prog.set("u_speed", st.speed)
        prog.set("u_amp", st.amp)
        prog.set("u_rot", st.rot)
        prog.set("u_t", st.t)
        prog.set("u_dt", st.dt)
        prog.set("u_decay", st.decay)
        prog.set("u_plane", *st.plane)
        prog.set("u_centre", *st.centre)
        ctx.fullscreen(prog)
        # 2. new elements on top: they are bent with the picture from the next frame on
        sh = Shapes()
        for pts, col, alpha, width in st.lines:
            rgba = (*col, alpha)
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                sh.line(x0, y0, x1, y1, width, rgba)
        for x, y, r, col, alpha in st.stars:
            sh.glow(x, y, r * 3, (*col, alpha * 0.6))
            sh.disc(x, y, r * 0.6, (1, 1, 1, alpha))
        if st.ifs_alpha > 0:
            rgba = (*st.ifs_colour, st.ifs_alpha)
            for x, y in st.ifs:
                sh.disc(x, y, st.ifs_size, rgba)
        ctx.draw_shapes(sh.data, "add")
        # 3. show it
        gl.glBindFramebuffer(G.FRAMEBUFFER, screen)
        gl.glViewport(0, 0, w, h)
        prog = ctx.program("goom-present", GOOM_PRESENT).use()
        gl.bind_texture(0, self.tex[1])
        prog.set_int("u_tex", 0)
        prog.set("u_flash", st.flash)
        ctx.fullscreen(prog)
        ctx.footer(f.caption)
        self.tex.reverse()
        self.fbos.reverse()

    def free(self):
        for k in range(2):
            self.gl.delete_framebuffer(self.fbos[k])
            self.gl.delete_texture(self.tex[k])
            self.fbos[k] = self.tex[k] = 0
        self.size = None


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
