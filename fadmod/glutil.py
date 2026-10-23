"""OpenGL helpers, atmosphere shaders (FX), textures, sky sphere."""
import math
import time
import numpy as np
from OpenGL import GL as gl
from OpenGL import GLU as glu

def make_texture(rgba, w, h, mip=True):
    tid = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    data = np.ascontiguousarray(rgba, dtype=np.uint8).tobytes()
    if mip:
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
        glu.gluBuild2DMipmaps(gl.GL_TEXTURE_2D, gl.GL_RGBA, w, h, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
    else:
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
    return tid


# --------------------------------------------------------------------------
# night light
# --------------------------------------------------------------------------
FOG_COLOR = (0.17, 0.175, 0.17)   # ashen grey (also the "sky" colour)
FOG_DENSITY = 0.0011              # exp2 fog; lower = thinner, see further
DAY_COLOR = (0.55, 0.74, 0.93)    # day mode: blue sky / haze colour
DAY_DENSITY = 0.00028             # day mode: thin haze, you see far

_FX_VS = """
#version 120
varying vec3 vPos; varying vec4 vCol; varying vec2 vUV;
void main(){
    vec4 p = gl_ModelViewMatrix * gl_Vertex;
    vPos = p.xyz; vCol = gl_Color; vUV = gl_MultiTexCoord0.xy;
    gl_Position = gl_ProjectionMatrix * p;
}
"""
_FX_FS = """
#version 120
varying vec3 vPos; varying vec4 vCol; varying vec2 vUV;
uniform sampler2D uSampler;
uniform float uTex, uMode, uFlick, uFlash, uDensity, uDay;
uniform vec3 uFog;
void main(){
    vec4 base = (uTex > 0.5) ? texture2D(uSampler, vUV) * vCol : vCol;
    float dist = max(length(vPos), 0.001);
    float ca = dot(vPos / dist, vec3(0.0, 0.0, -1.0));      // angle off the view axis
    float spot = smoothstep(0.80, 0.975, ca);                // flashlight cone
    float wide = smoothstep(0.30, 1.0, ca);                  // soft spill around it
    float att = 1.0 / (1.0 + pow(dist / 650.0, 2.0));        // light dies with distance
    float on = uFlash * uFlick;
    vec3 beam = vec3(1.0, 0.97, 0.92) * (spot * 1.35 + wide * 0.10) * att * on * (1.0 - uDay);
    vec3 amb = (uMode > 0.5) ? vec3(0.50, 0.50, 0.52) : vec3(0.15, 0.16, 0.17);
    amb = mix(amb, vec3(1.0, 0.98, 0.93), uDay);             // day: full sunlight
    vec3 lit = base.rgb * (amb + beam);
    float l = dot(lit, vec3(0.299, 0.587, 0.114));
    vec3 night = mix(vec3(l), lit, 0.72) * vec3(0.98, 1.0, 1.0);   // drained, neutral tint
    lit = mix(night, lit, uDay);
    vec3 fogc = uFog + vec3(0.95, 0.94, 0.90) * wide * 0.07 * on * (1.0 - att * 0.5) * (1.0 - uDay);
    float f = (uMode > 0.5) ? 1.0 : exp(-pow(dist * uDensity, 2.0));
    gl_FragColor = vec4(mix(fogc, lit, f), base.a);
}
"""
_PP_VS = """
#version 120
varying vec2 vUV;
void main(){ vUV = gl_MultiTexCoord0.xy; gl_Position = gl_Vertex; }
"""
_PP_FS = """
#version 120
varying vec2 vUV;
uniform float uDay;
void main(){
    float vig = smoothstep(0.30, 1.0, length(vUV - 0.5) * 1.55) * mix(0.78, 0.10, uDay);
    gl_FragColor = vec4(0.0, 0.0, 0.0, vig);                 // vignette only
}
"""


class FX:
    ok = False
    tried = False
    prog = pp = 0
    active = False
    flash = True          # F toggles the flashlight
    day = False           # map time of day: False = night (Silent Hill look), True = day
    u = {}
    pu = {}

    @classmethod
    def _compile(cls, vs, fs):
        def sh(kind, s):
            o = gl.glCreateShader(kind)
            gl.glShaderSource(o, s)
            gl.glCompileShader(o)
            if not gl.glGetShaderiv(o, gl.GL_COMPILE_STATUS):
                raise RuntimeError(gl.glGetShaderInfoLog(o))
            return o
        p = gl.glCreateProgram()
        gl.glAttachShader(p, sh(gl.GL_VERTEX_SHADER, vs))
        gl.glAttachShader(p, sh(gl.GL_FRAGMENT_SHADER, fs))
        gl.glLinkProgram(p)
        if not gl.glGetProgramiv(p, gl.GL_LINK_STATUS):
            raise RuntimeError(gl.glGetProgramInfoLog(p))
        return p

    @classmethod
    def init(cls):
        if cls.tried:
            return cls.ok
        cls.tried = True
        try:
            cls.prog = cls._compile(_FX_VS, _FX_FS)
            cls.pp = cls._compile(_PP_VS, _PP_FS)
            for n in ("uSampler", "uTex", "uMode", "uFlick", "uFlash", "uDensity", "uFog", "uDay"):
                cls.u[n] = gl.glGetUniformLocation(cls.prog, n)
            cls.pu["uDay"] = gl.glGetUniformLocation(cls.pp, "uDay")
            cls.ok = True
        except Exception as ex:
            print("Atmosphere shaders unavailable, using plain fixed-function fog:", ex)
            cls.ok = False
        return cls.ok

    @classmethod
    def fog_color(cls):
        return DAY_COLOR if cls.day else FOG_COLOR

    @classmethod
    def density(cls):
        return DAY_DENSITY if cls.day else FOG_DENSITY

    @classmethod
    def flicker(cls, t):
        # no blinking: just a gentle, constant shimmer in the beam's brightness
        return 0.96 + 0.04 * math.sin(t * 23.0)

    @classmethod
    def begin(cls, mode=0):
        if cls.ok:
            t = time.perf_counter()
            gl.glUseProgram(cls.prog)
            u = cls.u
            gl.glUniform1i(u["uSampler"], 0)
            gl.glUniform1f(u["uTex"], 1.0)
            gl.glUniform1f(u["uMode"], float(mode))
            gl.glUniform1f(u["uFlick"], cls.flicker(t))
            gl.glUniform1f(u["uFlash"], 1.0 if cls.flash else 0.0)
            gl.glUniform1f(u["uDensity"], cls.density())
            gl.glUniform3f(u["uFog"], *cls.fog_color())
            gl.glUniform1f(u["uDay"], 1.0 if cls.day else 0.0)
            cls.active = True
        else:
            gl.glEnable(gl.GL_FOG)
            gl.glFogi(gl.GL_FOG_MODE, gl.GL_EXP2)
            gl.glFogfv(gl.GL_FOG_COLOR, (*cls.fog_color(), 1.0))
            gl.glFogf(gl.GL_FOG_DENSITY, 0.0 if mode else cls.density())

    @classmethod
    def end(cls):
        if cls.ok:
            gl.glUseProgram(0)
            cls.active = False
        else:
            gl.glDisable(gl.GL_FOG)

    @classmethod
    def post(cls):
        """Vignette over the finished 3D scene (before the HUD)."""
        if not cls.ok:
            return
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glUseProgram(cls.pp)
        gl.glUniform1f(cls.pu["uDay"], 1.0 if cls.day else 0.0)
        gl.glBegin(gl.GL_QUADS)
        for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            gl.glTexCoord2f((x + 1) / 2, (y + 1) / 2)
            gl.glVertex4f(x, y, 0, 1)
        gl.glEnd()
        gl.glUseProgram(0)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_BLEND)
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glEnable(gl.GL_DEPTH_TEST)


def tex2d(on):
    """glEnable/glDisable(GL_TEXTURE_2D) that also tells the atmosphere shader."""
    (gl.glEnable if on else gl.glDisable)(gl.GL_TEXTURE_2D)
    if FX.active:
        gl.glUniform1f(FX.u["uTex"], 1.0 if on else 0.0)


def checker_tex():
    a = np.zeros((64, 64, 4), np.uint8)
    a[:, :] = (0x66, 0x66, 0x77, 255)
    a[:32, :32] = (0x88, 0x88, 0x99, 255)
    a[32:, 32:] = (0x88, 0x88, 0x99, 255)
    return {"t": make_texture(a, 64, 64), "w": 64, "h": 64, "tr": False}


def sky_sphere_list(R):
    """Display list of a sphere seen from the inside, equirectangular UVs (panorama image)."""
    N, M = 48, 24
    lid = gl.glGenLists(1)
    gl.glNewList(lid, gl.GL_COMPILE)
    gl.glBegin(gl.GL_QUADS)
    for j in range(M):
        for i in range(N):
            for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1)):
                lon = 2 * math.pi * (i + di) / N
                lat = math.pi / 2 - math.pi * (j + dj) / M
                gl.glTexCoord2f(1.0 - (i + di) / N, (j + dj) / M)
                gl.glVertex3f(R * math.cos(lat) * math.cos(lon), R * math.cos(lat) * math.sin(lon), R * math.sin(lat))
    gl.glEnd()
    gl.glEndList()
    return lid

