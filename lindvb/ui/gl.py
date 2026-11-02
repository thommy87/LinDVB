"""Minimal OpenGL access for the GPU-rendered radio visualisations.

The functions come from libepoxy, the GL loader GTK itself uses, so there is no
extra dependency (no PyOpenGL). They are only called while a ``Gtk.GLArea``'s
context is current. Works with desktop GL 3.3+ and OpenGL ES 3.0+.
"""

import ctypes
import ctypes.util
from array import array

# --- constants -------------------------------------------------------------------
NO_ERROR = 0
DEPTH_TEST, BLEND, SCISSOR_TEST = 0x0B71, 0x0BE2, 0x0C11
ZERO, ONE = 0, 1
SRC_COLOR, ONE_MINUS_SRC_COLOR = 0x0300, 0x0301
SRC_ALPHA, ONE_MINUS_SRC_ALPHA = 0x0302, 0x0303
COLOR_BUFFER_BIT = 0x4000
TRIANGLE_STRIP = 0x0005
FLOAT, UNSIGNED_BYTE = 0x1406, 0x1401
FRAGMENT_SHADER, VERTEX_SHADER = 0x8B30, 0x8B31
COMPILE_STATUS, LINK_STATUS, INFO_LOG_LENGTH = 0x8B81, 0x8B82, 0x8B84
ARRAY_BUFFER, STREAM_DRAW, STATIC_DRAW = 0x8892, 0x88E0, 0x88E4
TEXTURE_2D, TEXTURE0 = 0x0DE1, 0x84C0
TEXTURE_MIN_FILTER, TEXTURE_MAG_FILTER = 0x2801, 0x2800
TEXTURE_WRAP_S, TEXTURE_WRAP_T = 0x2802, 0x2803
NEAREST, LINEAR, CLAMP_TO_EDGE = 0x2600, 0x2601, 0x812F
RGBA, RGBA8, RED, R32F = 0x1908, 0x8058, 0x1903, 0x822E
RGBA16F, HALF_FLOAT = 0x881A, 0x140B
FUNC_ADD, MAX = 0x8006, 0x8008
UNPACK_ALIGNMENT, UNPACK_ROW_LENGTH, PACK_ALIGNMENT = 0x0CF5, 0x0CF2, 0x0D05
FRAMEBUFFER, COLOR_ATTACHMENT0 = 0x8D40, 0x8CE0
FRAMEBUFFER_BINDING, FRAMEBUFFER_COMPLETE = 0x8CA6, 0x8CD5
VIEWPORT = 0x0BA2

_u, _i, _f, _p = ctypes.c_uint, ctypes.c_int, ctypes.c_float, ctypes.c_void_p
_FUNCS = {
    "glGetError": (_u, ()),
    "glGetIntegerv": (None, (_u, ctypes.POINTER(_i))),
    "glViewport": (None, (_i, _i, _i, _i)),
    "glScissor": (None, (_i, _i, _i, _i)),
    "glClearColor": (None, (_f, _f, _f, _f)),
    "glClear": (None, (_u,)),
    "glEnable": (None, (_u,)),
    "glDisable": (None, (_u,)),
    "glBlendFunc": (None, (_u, _u)),
    "glBlendEquation": (None, (_u,)),
    "glCreateShader": (_u, (_u,)),
    "glShaderSource": (None, (_u, _i, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(_i))),
    "glCompileShader": (None, (_u,)),
    "glGetShaderiv": (None, (_u, _u, ctypes.POINTER(_i))),
    "glGetShaderInfoLog": (None, (_u, _i, ctypes.POINTER(_i), ctypes.c_char_p)),
    "glDeleteShader": (None, (_u,)),
    "glCreateProgram": (_u, ()),
    "glAttachShader": (None, (_u, _u)),
    "glBindAttribLocation": (None, (_u, _u, ctypes.c_char_p)),
    "glLinkProgram": (None, (_u,)),
    "glGetProgramiv": (None, (_u, _u, ctypes.POINTER(_i))),
    "glGetProgramInfoLog": (None, (_u, _i, ctypes.POINTER(_i), ctypes.c_char_p)),
    "glUseProgram": (None, (_u,)),
    "glDeleteProgram": (None, (_u,)),
    "glGetUniformLocation": (_i, (_u, ctypes.c_char_p)),
    "glUniform1i": (None, (_i, _i)),
    "glUniform1f": (None, (_i, _f)),
    "glUniform2f": (None, (_i, _f, _f)),
    "glUniform3f": (None, (_i, _f, _f, _f)),
    "glUniform4f": (None, (_i, _f, _f, _f, _f)),
    "glUniform1fv": (None, (_i, _i, _p)),
    "glUniform3fv": (None, (_i, _i, _p)),
    "glUniform4fv": (None, (_i, _i, _p)),
    "glGenVertexArrays": (None, (_i, ctypes.POINTER(_u))),
    "glBindVertexArray": (None, (_u,)),
    "glDeleteVertexArrays": (None, (_i, ctypes.POINTER(_u))),
    "glGenBuffers": (None, (_i, ctypes.POINTER(_u))),
    "glBindBuffer": (None, (_u, _u)),
    "glBufferData": (None, (_u, ctypes.c_ssize_t, _p, _u)),
    "glDeleteBuffers": (None, (_i, ctypes.POINTER(_u))),
    "glEnableVertexAttribArray": (None, (_u,)),
    "glVertexAttribPointer": (None, (_u, _i, _u, ctypes.c_ubyte, _i, _p)),
    "glVertexAttribDivisor": (None, (_u, _u)),
    "glDrawArrays": (None, (_u, _i, _i)),
    "glDrawArraysInstanced": (None, (_u, _i, _i, _i)),
    "glGenTextures": (None, (_i, ctypes.POINTER(_u))),
    "glBindTexture": (None, (_u, _u)),
    "glActiveTexture": (None, (_u,)),
    "glTexImage2D": (None, (_u, _i, _i, _i, _i, _i, _u, _u, _p)),
    "glTexParameteri": (None, (_u, _u, _i)),
    "glDeleteTextures": (None, (_i, ctypes.POINTER(_u))),
    "glPixelStorei": (None, (_u, _i)),
    "glGenFramebuffers": (None, (_i, ctypes.POINTER(_u))),
    "glBindFramebuffer": (None, (_u, _u)),
    "glFramebufferTexture2D": (None, (_u, _u, _u, _u, _i)),
    "glCheckFramebufferStatus": (_u, (_u,)),
    "glDeleteFramebuffers": (None, (_i, ctypes.POINTER(_u))),
    "glReadPixels": (None, (_i, _i, _i, _i, _u, _u, _p)),
}


class GLError(RuntimeError):
    pass


_epoxy = None


def _library():
    global _epoxy
    if _epoxy is None:
        # GTK has loaded it already: by its soname we get that same copy
        try:
            _epoxy = ctypes.CDLL("libepoxy.so.0")
        except OSError:
            _epoxy = ctypes.CDLL(ctypes.util.find_library("epoxy") or "libepoxy.so")
    return _epoxy


class GL:
    """The GL functions as attributes (``gl.glClear(...)``) plus small helpers."""

    def __init__(self, es=False):
        lib = _library()
        self.es = es
        for name, (res, args) in _FUNCS.items():
            ptr = ctypes.c_void_p.in_dll(lib, "epoxy_" + name).value
            if not ptr:
                raise GLError(f"{name} is not available")
            setattr(self, name, ctypes.CFUNCTYPE(res, *args)(ptr))

    # -- shaders --------------------------------------------------------------
    @property
    def header(self):
        if self.es:
            return "#version 300 es\nprecision highp float;\nprecision highp int;\n" \
                   "precision highp sampler2D;\n"
        return "#version 330 core\n"

    def _shader(self, kind, source):
        sh = self.glCreateShader(kind)
        src = (self.header + source).encode()
        self.glShaderSource(sh, 1, (ctypes.c_char_p * 1)(src), None)
        self.glCompileShader(sh)
        ok = _i()
        self.glGetShaderiv(sh, COMPILE_STATUS, ctypes.byref(ok))
        if not ok.value:
            log = self._log(self.glGetShaderiv, self.glGetShaderInfoLog, sh)
            self.glDeleteShader(sh)
            raise GLError(f"shader does not compile: {log}")
        return sh

    def _log(self, getiv, getlog, obj):
        n = _i()
        getiv(obj, INFO_LOG_LENGTH, ctypes.byref(n))
        buf = ctypes.create_string_buffer(max(1, n.value))
        getlog(obj, len(buf), None, buf)
        return buf.value.decode(errors="replace").strip()

    def program(self, vertex, fragment, attributes=()):
        return Program(self, vertex, fragment, attributes)

    # -- objects --------------------------------------------------------------
    def _gen(self, fn):
        out = _u()
        fn(1, ctypes.byref(out))
        return out.value

    def _delete(self, fn, name):
        if name:
            fn(1, ctypes.byref(_u(name)))

    def buffer(self):
        return self._gen(self.glGenBuffers)

    def vertex_array(self):
        return self._gen(self.glGenVertexArrays)

    def texture(self, filtering=LINEAR):
        tex = self._gen(self.glGenTextures)
        self.glBindTexture(TEXTURE_2D, tex)
        for p, v in ((TEXTURE_MIN_FILTER, filtering), (TEXTURE_MAG_FILTER, filtering),
                     (TEXTURE_WRAP_S, CLAMP_TO_EDGE), (TEXTURE_WRAP_T, CLAMP_TO_EDGE)):
            self.glTexParameteri(TEXTURE_2D, p, v)
        return tex

    def delete_texture(self, tex):
        self._delete(self.glDeleteTextures, tex)

    def delete_buffer(self, buf):
        self._delete(self.glDeleteBuffers, buf)

    def delete_vertex_array(self, vao):
        self._delete(self.glDeleteVertexArrays, vao)

    def upload_rgba(self, tex, width, height, data):
        """Upload 8-bit RGBA pixels (bytes-like, rows top to bottom)."""
        self.glBindTexture(TEXTURE_2D, tex)
        self.glPixelStorei(UNPACK_ALIGNMENT, 4)
        self.glPixelStorei(UNPACK_ROW_LENGTH, 0)
        ptr = None
        if data is not None:
            try:
                ptr = (ctypes.c_char * len(data)).from_buffer(data)
            except TypeError:           # read-only buffer
                ptr = (ctypes.c_char * len(data)).from_buffer_copy(data)
        self.glTexImage2D(TEXTURE_2D, 0, RGBA8, width, height, 0, RGBA, UNSIGNED_BYTE, ptr)

    def upload_floats(self, tex, values, width, height=1):
        """Upload a single-channel float texture (read with texelFetch)."""
        arr = values if isinstance(values, array) else array("f", values)
        if len(arr) < width * height:
            arr.extend([0.0] * (width * height - len(arr)))
        self.glBindTexture(TEXTURE_2D, tex)
        self.glPixelStorei(UNPACK_ALIGNMENT, 4)
        self.glPixelStorei(UNPACK_ROW_LENGTH, 0)
        self.glTexImage2D(TEXTURE_2D, 0, R32F, width, height, 0, RED, FLOAT,
                          arr.buffer_info()[0])

    def bind_texture(self, unit, tex):
        self.glActiveTexture(TEXTURE0 + unit)
        self.glBindTexture(TEXTURE_2D, tex)

    def current_framebuffer(self):
        v = _i()
        self.glGetIntegerv(FRAMEBUFFER_BINDING, ctypes.byref(v))
        return v.value

    def framebuffer(self, tex):
        fbo = self._gen(self.glGenFramebuffers)
        self.glBindFramebuffer(FRAMEBUFFER, fbo)
        self.glFramebufferTexture2D(FRAMEBUFFER, COLOR_ATTACHMENT0, TEXTURE_2D, tex, 0)
        status = self.glCheckFramebufferStatus(FRAMEBUFFER)
        if status != FRAMEBUFFER_COMPLETE:
            self._delete(self.glDeleteFramebuffers, fbo)
            raise GLError(f"framebuffer incomplete (0x{status:x})")
        return fbo

    def delete_framebuffer(self, fbo):
        self._delete(self.glDeleteFramebuffers, fbo)

    def read_rgba(self, width, height):
        """Pixels of the bound framebuffer, bottom row first (for tests)."""
        buf = ctypes.create_string_buffer(width * height * 4)
        self.glPixelStorei(PACK_ALIGNMENT, 4)
        self.glReadPixels(0, 0, width, height, RGBA, UNSIGNED_BYTE, buf)
        return buf.raw

    def check(self, where=""):
        err = self.glGetError()
        if err != NO_ERROR:
            raise GLError(f"GL error 0x{err:x} {where}".strip())


class Program:
    def __init__(self, gl, vertex, fragment, attributes=()):
        self.gl = gl
        vs = gl._shader(VERTEX_SHADER, vertex)
        try:
            fs = gl._shader(FRAGMENT_SHADER, fragment)
        except GLError:
            gl.glDeleteShader(vs)
            raise
        prog = gl.glCreateProgram()
        gl.glAttachShader(prog, vs)
        gl.glAttachShader(prog, fs)
        for index, name in enumerate(attributes):
            gl.glBindAttribLocation(prog, index, name.encode())
        gl.glLinkProgram(prog)
        gl.glDeleteShader(vs)
        gl.glDeleteShader(fs)
        ok = _i()
        gl.glGetProgramiv(prog, LINK_STATUS, ctypes.byref(ok))
        if not ok.value:
            log = gl._log(gl.glGetProgramiv, gl.glGetProgramInfoLog, prog)
            gl.glDeleteProgram(prog)
            raise GLError(f"shader program does not link: {log}")
        self.id = prog
        self._locs = {}

    def use(self):
        self.gl.glUseProgram(self.id)
        return self

    def loc(self, name):
        loc = self._locs.get(name)
        if loc is None:
            loc = self._locs[name] = self.gl.glGetUniformLocation(self.id, name.encode())
        return loc

    def set(self, name, *values):
        """Set a float / vec2 / vec3 / vec4 uniform."""
        loc = self.loc(name)
        if loc < 0:
            return
        fn = (None, self.gl.glUniform1f, self.gl.glUniform2f,
              self.gl.glUniform3f, self.gl.glUniform4f)[len(values)]
        fn(loc, *(float(v) for v in values))

    def set_int(self, name, value):
        loc = self.loc(name)
        if loc >= 0:
            self.gl.glUniform1i(loc, int(value))

    def set_array(self, name, values, size=1):
        """Set a float/vec3/vec4 array uniform from a flat list."""
        loc = self.loc(name)
        if loc < 0 or not values:
            return
        arr = array("f", values)
        fn = {1: self.gl.glUniform1fv, 3: self.gl.glUniform3fv, 4: self.gl.glUniform4fv}[size]
        fn(loc, len(arr) // size, arr.buffer_info()[0])

    def free(self):
        if self.id:
            self.gl.glDeleteProgram(self.id)
            self.id = 0
