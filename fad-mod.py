#!/usr/bin/env python3
"""
AKB / ARENA - Browser TDM, rewritten as a native Python game.

Run:       python akb_arena.py   (missing packages install automatically)

Mouse look uses SDL relative mode (raw input, OS pointer acceleration off).
Weapon mods (.mdl/.wav/.json, or CS:GO .vpk packages: models + animations only) put in a "weapons" folder next to this file load
automatically at start. Player models go in a "player" folder (your CT / T
choice is remembered). Custom sounds go in a "sounds" folder (name them
shoot.wav and hit.wav). You can also drag & drop .bsp/.wad/.mdl/.wav/.json files
onto the window, or use the buttons in the Play menu (needs tkinter).
Silent Hill look: fog + dim light + flashlight (F toggles it) and vignette.

Online play (Play tab > Online) needs NO port forwarding: rooms are listed on a free public
MQTT broker, public STUN servers find your address, and players connect with UDP hole punching.
"""
import os
import re
import io
import sys
import json
import math
import random
import struct
import time
import socket
import select
import hashlib
import queue
import threading
from pathlib import Path

# Raw mouse: no OS acceleration / scaling in relative mode.
os.environ.setdefault("SDL_MOUSE_RELATIVE_MODE_WARP", "0")
os.environ.setdefault("SDL_MOUSE_RELATIVE_SYSTEM_SCALE", "0")



def _ensure_deps():
    """Install missing packages automatically (pygame, PyOpenGL, numpy, paho-mqtt)."""
    import importlib.util
    import subprocess

    def pip(pkgs):
        base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
        # Wheels only (never compile from source). pygame-ce is a drop-in
        # replacement for pygame that ships wheels for the newest Python versions.
        base += ["--only-binary=:all:"]
        for extra in ([], ["--user"], ["--break-system-packages"]):
            if subprocess.call(base + extra + pkgs) == 0:
                return True
        return False

    need = {"numpy": "numpy", "pygame": "pygame-ce", "OpenGL": "PyOpenGL"}
    missing = [pkg for mod, pkg in need.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print("Installing missing packages:", ", ".join(missing))
        if not pip(missing):
            print("Could not install:", ", ".join(missing))
            print("Run manually: pip install " + " ".join(missing))
            sys.exit(1)
    # optional: the internet room browser (free public MQTT broker). Not fatal if it fails.
    if importlib.util.find_spec("paho") is None:
        print("Installing paho-mqtt (internet room browser) ...")
        if not pip(["paho-mqtt"]):
            print("Could not install paho-mqtt - only LAN rooms will be found. Run: pip install paho-mqtt")
    importlib.invalidate_caches()


_ensure_deps()

import numpy as np
import pygame
from OpenGL import GL as gl
from OpenGL import GLU as glu

# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------
SETTINGS_FILE = Path(__file__).with_name("akb_arena_settings.json")

DEFAULT_BINDS = {
    "forward": "w", "backward": "s", "left": "a", "right": "d",
    "jump": "space", "slide": "c", "reload": "r",
    "weapon1": "1", "weapon2": "2", "weapon3": "3",
    "swaphand": "v", "crouch": "left ctrl",
}
DEFAULT_XH = {"size": 6, "thick": 2, "gap": 3, "dot": 0, "outline": 1, "r": 0, "g": 255, "b": 0, "a": 1.0}
BIND_META = [
    ("forward", "Move forward"), ("backward", "Move backward"),
    ("left", "Move left"), ("right", "Move right"),
    ("jump", "Jump"), ("slide", "Slide"), ("reload", "Reload"),
    ("weapon1", "Weapon slot 1"), ("weapon2", "Weapon slot 2"),
    ("weapon3", "Weapon slot 3"), ("swaphand", "Switch hand"),
    ("crouch", "Crouch"),
]
BASE_SENS = 0.0022
STEP_H = 18  # max step / slope rise per move
MAXV = 3500.0  # max speed (surfing can get fast)
SURF_NZ = 0.7  # surfaces steeper than this (normal.z below it) cannot be walked on - you slide / surf on them
BOT_NAMES = ["Rick", "Sun", "Moon", "Ace", "Blaze", "Nova", "Jinx", "Echo", "Rex", "Luna",
             "Max", "Zed", "Kai", "Axel", "Milo", "Ziggy", "Storm", "Bolt", "Pixel", "Fang"]
VM_NEAR, VM_FAR = 0.1, 1000.0  # viewmodel clip planes

SKIP_TEX = re.compile(r"^(sky|aaatrigger|clip|null|origin|hint|skip|trigger|bevel|noclip|black_hidden)")

WJR, WJH, WJV, WJMAX = 16, 260, 290, 3
TICK = 1 / 64

# CS 1.6 style player animation tuning --------------------------------------
PITCH_SIGN = -1      # aim blend: GoldSrc pitch is +down. If bots aim up when you are below them, flip to +1
TWIST_SIGN = 1       # torso twist direction. If the upper body twists the wrong way vs the legs, flip to -1
FADE_TIME = 0.2      # sequence cross-fade (seconds), same feel as the GoldSrc 0.2s sequence blend
RUN_SPEED = 135      # above this horizontal speed the "run" gait plays, below it "walk"
MAX_TWIST = 70.0     # max degrees the torso may twist away from the legs while standing still
MAXS, FRIC, STOPS, ACC, AIRACC, AIRCAP, JUMPV, BHCAP = 250, 5.2, 80, 5.5, 12, 30, 270, 1.1


class Wpn:
    """Weapon definition; missing attributes read as None."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, k):
        if k.startswith("__"):
            raise AttributeError(k)
        return None


def default_cfg():
    return {
        "weapons": [
            Wpn(n="AWP", dmg=100, rpm=45, mag=5, spr=.00035, auto=0),
            Wpn(n="AK-47", dmg=28, rpm=600, mag=30, spr=.012, auto=1),
            Wpn(n="Knife", dmg=40, rpm=120, mag=0, spr=0, auto=1, melee=1, range=64),
        ],
        "bots": 2,
        "frags": 30,
    }


CFG = default_cfg()


def key_norm(s):
    s = s.lower()
    s = re.sub(r"\.\w+$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


# --------------------------------------------------------------------------
# GL helpers
# --------------------------------------------------------------------------
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
# Silent Hill atmosphere: dense fog, dim ambient, flashlight, vignette
# --------------------------------------------------------------------------
FOG_COLOR = (0.17, 0.175, 0.17)   # ashen grey (also the "sky" colour)
FOG_DENSITY = 0.0011              # exp2 fog; lower = thinner, see further

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
uniform float uTex, uMode, uFlick, uFlash, uDensity;
uniform vec3 uFog;
void main(){
    vec4 base = (uTex > 0.5) ? texture2D(uSampler, vUV) * vCol : vCol;
    float dist = max(length(vPos), 0.001);
    float ca = dot(vPos / dist, vec3(0.0, 0.0, -1.0));      // angle off the view axis
    float spot = smoothstep(0.80, 0.975, ca);                // flashlight cone
    float wide = smoothstep(0.30, 1.0, ca);                  // soft spill around it
    float att = 1.0 / (1.0 + pow(dist / 650.0, 2.0));        // light dies with distance
    float on = uFlash * uFlick;
    vec3 beam = vec3(1.0, 0.92, 0.74) * (spot * 1.35 + wide * 0.10) * att * on;
    vec3 amb = (uMode > 0.5) ? vec3(0.50, 0.50, 0.52) : vec3(0.15, 0.16, 0.17);
    vec3 lit = base.rgb * (amb + beam);
    float l = dot(lit, vec3(0.299, 0.587, 0.114));
    lit = mix(vec3(l), lit, 0.72) * vec3(1.04, 1.0, 0.90);   // drained, slightly sickly tint
    vec3 fogc = uFog + vec3(0.95, 0.88, 0.7) * wide * 0.07 * on * (1.0 - att * 0.5);
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
void main(){
    float vig = smoothstep(0.30, 1.0, length(vUV - 0.5) * 1.55) * 0.78;
    gl_FragColor = vec4(0.0, 0.0, 0.0, vig);                 // vignette only
}
"""


class FX:
    ok = False
    tried = False
    prog = pp = 0
    active = False
    flash = True          # F toggles the flashlight
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
            for n in ("uSampler", "uTex", "uMode", "uFlick", "uFlash", "uDensity", "uFog"):
                cls.u[n] = gl.glGetUniformLocation(cls.prog, n)
            cls.ok = True
        except Exception as ex:
            print("Atmosphere shaders unavailable, using plain fixed-function fog:", ex)
            cls.ok = False
        return cls.ok

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
            gl.glUniform1f(u["uDensity"], FOG_DENSITY)
            gl.glUniform3f(u["uFog"], *FOG_COLOR)
            cls.active = True
        else:
            gl.glEnable(gl.GL_FOG)
            gl.glFogi(gl.GL_FOG_MODE, gl.GL_EXP2)
            gl.glFogfv(gl.GL_FOG_COLOR, (*FOG_COLOR, 1.0))
            gl.glFogf(gl.GL_FOG_DENSITY, 0.0 if mode else FOG_DENSITY)

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


# --------------------------------------------------------------------------
# WAD / BSP
# --------------------------------------------------------------------------
def parse_mip(buf, o, texdb):
    name = buf[o:o + 16].split(b"\0")[0].decode("latin1").lower()
    w, h, o0 = struct.unpack_from("<III", buf, o + 16)
    if not o0 or not w or not h or w > 1024 or h > 1024:
        return name
    o3 = struct.unpack_from("<I", buf, o + 36)[0]
    p = o + o3 + (w >> 3) * (h >> 3) + 2
    idx = np.frombuffer(buf, np.uint8, w * h, o + o0)
    pal = np.frombuffer(buf, np.uint8, 768, p).reshape(256, 3)
    rgba = np.empty((w * h, 4), np.uint8)
    rgba[:, :3] = pal[idx]
    rgba[:, 3] = 255
    tr = name.startswith("{")
    if tr:
        rgba[idx == 255, 3] = 0
    texdb[name] = {"t": make_texture(rgba.reshape(h, w, 4), w, h), "w": w, "h": h, "tr": tr}
    return name


def parse_wad(buf, texdb):
    if len(buf) < 12 or buf[:4] != b"WAD3":
        return
    n, o = struct.unpack_from("<ii", buf, 4)
    for i in range(n):
        e = o + i * 32
        if buf[e + 12] == 67:
            try:
                parse_mip(buf, struct.unpack_from("<i", buf, e)[0], texdb)
            except Exception:
                pass


def _fib_dirs(n):
    out = []
    ga = math.pi * (3 - math.sqrt(5))
    for i in range(n):
        z = 1 - 2 * (i + .5) / n
        r = math.sqrt(1 - z * z)
        out.append((math.cos(i * ga) * r, math.sin(i * ga) * r, z))
    return out


PROBES = _fib_dirs(48)


class World:
    feet_offset = 36

    def draw(self):
        raise NotImplementedError

    def probe_normal(self, x, y, z, r=1.5):
        """Fallback surface normal: average direction of solid space around the point."""
        sx = sy = sz = 0.0
        for d in PROBES:
            if self.solid(x + d[0] * r, y + d[1] * r, z + d[2] * r):
                sx += d[0]
                sy += d[1]
                sz += d[2]
        l = math.sqrt(sx * sx + sy * sy + sz * sz)
        if l < 1e-6:
            return None
        return (-sx / l, -sy / l, -sz / l)

    def bullet_solid(self, x, y, z):
        return self.solid(x, y, z)

    def normal_at(self, x, y, z, want_up=False, move=None):
        """Outward normal of the surface touching the (empty) point, or None.
        move: direction we are trying to travel - only surfaces facing against it count."""
        return self.probe_normal(x, y, z)


class BspWorld(World):
    def __init__(self, buf, texdb):
        if len(buf) < 124 or struct.unpack_from("<i", buf, 0)[0] != 30:
            raise ValueError("Not a GoldSrc BSP (version 30)")
        L = lambda i: struct.unpack_from("<ii", buf, 4 + i * 8)
        eo, el = L(0)
        po, pl = L(1)
        to, _ = L(2)
        vo, vl = L(3)
        xo, _ = L(6)
        fo, fl = L(7)
        co, cl = L(9)
        edo, edl = L(12)
        so, sl = L(13)
        mo, _ = L(14)
        nodo, nodl = L(5)     # BSP nodes   (hull 0 = the real, un-expanded map shape, used for bullets)
        leaf_o, leaf_l = L(10)  # BSP leaves

        ents_txt = buf[eo:eo + el].decode("latin1")
        nt = struct.unpack_from("<i", buf, to)[0]
        names = []
        for i in range(nt):
            off = struct.unpack_from("<i", buf, to + 4 + i * 4)[0]
            names.append("" if off < 0 else parse_mip(buf, to + off, texdb))

        # collision hull 1 (player hull) - point tests
        planes = np.frombuffer(buf, np.uint8, pl, po)
        pv = [struct.unpack_from("<ffff", buf, po + i * 20) for i in range(pl // 20)]
        pn = [(p[0], p[1], p[2], p[3]) for p in pv]
        cn = [struct.unpack_from("<iHH", buf, co + i * 8) for i in range(cl // 8)]
        # children are int16 - re-read signed
        cn = []
        for i in range(cl // 8):
            pnum, c0, c1 = struct.unpack_from("<ihh", buf, co + i * 8)
            cn.append((pnum, c0, c1))
        hn = struct.unpack_from("<i", buf, mo + 40)[0]
        self._pn, self._cn, self._hn = pn, cn, hn
        # hull 0: exact point collision for bullets (the player hull is padded ~16-36 units, which made
        # shots stop at wall edges and corners that look open)
        try:
            self._nd = [struct.unpack_from("<ihh", buf, nodo + i * 24) for i in range(nodl // 24)]
            self._lf = [struct.unpack_from("<i", buf, leaf_o + i * 28)[0] for i in range(leaf_l // 28)]
            self._h0 = struct.unpack_from("<i", buf, mo + 36)[0]
            if not self._nd or not self._lf:
                raise ValueError("no hull 0")
        except Exception as ex:
            print("Exact bullet collision unavailable:", ex)
            self._nd = None

        verts = np.frombuffer(buf, "<f4", vl // 4, vo).reshape(-1, 3)
        edges = np.frombuffer(buf, "<u2", edl // 2, edo).reshape(-1, 2)
        surf = np.frombuffer(buf, "<i4", sl // 4, so)

        G = {}
        for f in range(fl // 20):
            o = fo + f * 20
            fe = struct.unpack_from("<i", buf, o + 4)[0]
            ne, tinfo = struct.unpack_from("<HH", buf, o + 8)
            ti = xo + tinfo * 40
            miptex = struct.unpack_from("<i", buf, ti + 32)[0]
            nm = names[miptex] if 0 <= miptex < len(names) else ""
            if SKIP_TEX.match(nm) or ne < 3:
                continue
            sv = struct.unpack_from("<4f", buf, ti)
            tv = struct.unpack_from("<4f", buf, ti + 16)
            se = surf[fe:fe + ne]
            vi = edges[np.abs(se), (se < 0).astype(np.int64)]
            vs = verts[vi].astype(np.float64)
            a, b, c = vs[0], vs[1], vs[2]
            n = np.cross(b - a, c - a)
            m = np.linalg.norm(n) or 1.0
            br = .55 + .3 * abs(n[2] / m) + .15 * abs(n[0] / m)
            tx = texdb.get(nm)
            if tx is None:
                tx = texdb[nm] = checker_tex()
            tri = np.empty(((ne - 2) * 3,), np.int64)
            k = np.arange(1, ne - 1)
            tri[0::3] = 0
            tri[1::3] = k
            tri[2::3] = k + 1
            P = vs[tri]
            U = (P @ np.array(sv[:3]) + sv[3]) / tx["w"]
            Vv = (P @ np.array(tv[:3]) + tv[3]) / tx["h"]
            g = G.setdefault(nm, {"p": [], "u": [], "c": []})
            g["p"].append(P.astype(np.float32))
            g["u"].append(np.stack([U, Vv], 1).astype(np.float32))
            g["c"].append(np.full((len(P), 3), br, np.float32))

        self.groups = []
        for nm, g in G.items():
            self.groups.append((
                texdb[nm],
                np.ascontiguousarray(np.concatenate(g["p"])),
                np.ascontiguousarray(np.concatenate(g["u"])),
                np.ascontiguousarray(np.concatenate(g["c"])),
            ))

        E = []
        for blk in ents_txt.split("}"):
            d = dict(re.findall(r'"(\w+)"\s+"([^"]*)"', blk))
            if "classname" in d and "origin" in d:
                E.append(d)

        def sp(*cl):
            out = []
            for e in E:
                if e["classname"] in cl:
                    try:
                        out.append([float(v) for v in e["origin"].split()[:3]])
                    except ValueError:
                        pass
            return out

        ct = sp("info_player_start", "info_player_counterterrorist")
        tt = sp("info_player_deathmatch", "info_player_terrorist")
        allp = ct + tt
        if not ct:
            ct = allp
        if not tt:
            tt = allp
        if not allp:
            ct = tt = [[0, 0, 64]]
        self.spawns = [ct, tt]

    def solid(self, x, y, z):
        pn, cn = self._pn, self._cn
        n = self._hn
        while n >= 0:
            pnum, c0, c1 = cn[n]
            p = pn[pnum]
            d = p[0] * x + p[1] * y + p[2] * z - p[3]
            n = c1 if d < 0 else c0
        return n == -2

    def bullet_solid(self, x, y, z):
        """Exact point test against the real map shape (hull 0): is this point inside a wall / sky?"""
        nd = self._nd
        if nd is None:
            return self.solid(x, y, z)
        pn = self._pn
        n = self._h0
        while n >= 0:
            pnum, c0, c1 = nd[n]
            p = pn[pnum]
            n = c1 if p[0] * x + p[1] * y + p[2] * z - p[3] < 0 else c0
        c = self._lf[-1 - n]
        return c == -2 or c == -6

    def normal_at(self, x, y, z, want_up=False, move=None):
        """Surface normal at an empty point: the nearest hull plane on the way down the
        clipnode tree that really has solid space behind it. want_up only accepts
        up-facing planes (the floor under your feet, ignoring walls next to you);
        move only accepts planes that face against the direction we are moving."""
        pn, cn = self._pn, self._cn
        n = self._hn
        cand = []
        while n >= 0:
            pnum, c0, c1 = cn[n]
            p = pn[pnum]
            d = p[0] * x + p[1] * y + p[2] * z - p[3]
            cand.append((abs(d), p, d))
            n = c1 if d < 0 else c0
        if n == -2:
            return None  # inside solid
        cand.sort(key=lambda c: c[0])
        best = None
        for ad, p, d in cand:
            l = math.sqrt(p[0] * p[0] + p[1] * p[1] + p[2] * p[2]) or 1.0
            sg = 1.0 if d >= 0 else -1.0
            nx, ny, nz = sg * p[0] / l, sg * p[1] / l, sg * p[2] / l
            if want_up and nz < .02:
                continue
            if want_up and best is not None and ad / l > 1.0:
                break
            if move is not None and nx * move[0] + ny * move[1] + nz * move[2] > -1e-4:
                continue
            k = ad / l + .3
            if self.solid(x - nx * k, y - ny * k, z - nz * k):
                if not want_up:
                    return (nx, ny, nz)
                # standing in a crease (floor + steep ramp): the floor is what holds you up,
                # so of the up-facing surfaces within reach take the flattest one
                if best is None or nz > best[2]:
                    best = (nx, ny, nz)
        if best is not None:
            return best
        return None if (want_up or move is not None) else self.probe_normal(x, y, z)

    def draw(self):
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glEnableClientState(gl.GL_COLOR_ARRAY)
        for tx, P, U, C in self.groups:
            gl.glBindTexture(gl.GL_TEXTURE_2D, tx["t"])
            if tx["tr"]:
                gl.glEnable(gl.GL_ALPHA_TEST)
                gl.glAlphaFunc(gl.GL_GREATER, .5)
            gl.glVertexPointer(3, gl.GL_FLOAT, 0, P)
            gl.glTexCoordPointer(2, gl.GL_FLOAT, 0, U)
            gl.glColorPointer(3, gl.GL_FLOAT, 0, C)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, len(P))
            if tx["tr"]:
                gl.glDisable(gl.GL_ALPHA_TEST)
        gl.glDisableClientState(gl.GL_COLOR_ARRAY)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
        gl.glColor4f(1, 1, 1, 1)


class ArenaWorld(World):
    B = [[-1000, -1000, -40, 1000, 1000, 0], [-1032, -1000, 0, -1000, 1000, 400],
         [1000, -1000, 0, 1032, 1000, 400], [-1000, -1032, 0, 1000, -1000, 400],
         [-1000, 1000, 0, 1000, 1032, 400],
         [-100, -400, 0, 100, -370, 120], [-100, 370, 0, 100, 400, 120],
         [-500, -100, 0, -400, 0, 100], [400, 0, 0, 500, 100, 100],
         [-250, 150, 0, -150, 250, 100], [150, -250, 0, 250, -150, 100],
         [-60, -60, 0, 60, 60, 160]]

    def __init__(self):
        B = self.B
        ct = [[-500 + i * 200, -900, 40] for i in range(6)]
        tt = [[-500 + i * 200, 900, 40] for i in range(6)]
        self.spawns = [ct, tt]
        self._tex = None
        self._list = None

    def solid(self, x, y, z):
        for b in self.B:
            if (b[0] - 16 < x < b[3] + 16 and b[1] - 16 < y < b[4] + 16
                    and b[2] - 36 < z < b[5] + 36):
                return True
        return False

    def bullet_solid(self, x, y, z):
        for b in self.B:
            if b[0] < x < b[3] and b[1] < y < b[4] and b[2] < z < b[5]:
                return True
        return False

    _FACES = ((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0),
              (0.0, 1.0, 0.0), (0.0, 0.0, -1.0), (0.0, 0.0, 1.0))

    def normal_at(self, x, y, z, want_up=False, move=None):
        if want_up:
            return (0.0, 0.0, 1.0)  # boxes only have flat tops
        best, bd = None, 1e30
        for b in self.B:
            sv = (b[0] - 16 - x, x - (b[3] + 16), b[1] - 16 - y, y - (b[4] + 16),
                  b[2] - 36 - z, z - (b[5] + 36))
            m = max(sv)
            for i in range(6):
                if sv[i] < m - .5:
                    continue  # not a face we are touching
                n = self._FACES[i]
                if move is not None and n[0] * move[0] + n[1] * move[1] + n[2] * move[2] > -1e-4:
                    continue
                if abs(sv[i]) < bd:
                    bd = abs(sv[i])
                    best = n
        return best

    def _build(self):
        self._tex = checker_tex()
        self._list = gl.glGenLists(1)
        gl.glNewList(self._list, gl.GL_COMPILE)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex["t"])
        for b in self.B:
            col = (0x88 / 255, 0x99 / 255, 0xaa / 255) if b[5] <= 0 else (0xb0 / 255, 0xa0 / 255, 0x90 / 255)
            gl.glColor3f(*col)
            x0, y0, z0, x1, y1, z1 = b
            R = 8
            faces = [
                ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),  # top
                ((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0)),  # bottom
                ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),
                ((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)),
                ((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)),
                ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),
            ]
            gl.glBegin(gl.GL_QUADS)
            for f in faces:
                for (u, v), p in zip(((0, 0), (R, 0), (R, R), (0, R)), f):
                    gl.glTexCoord2f(u, v)
                    gl.glVertex3f(*p)
            gl.glEnd()
        gl.glEndList()

    def draw(self):
        if self._list is None:
            self._build()
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glCallList(self._list)
        gl.glColor4f(1, 1, 1, 1)


# --------------------------------------------------------------------------
# GoldSrc MDL (IDST v10)
# --------------------------------------------------------------------------
MDL_RX = {
    "idle": re.compile(r"^idle", re.I), "draw": re.compile(r"draw|deploy|ready", re.I),
    "shoot": re.compile(r"shoot|fire|attack|shot|stab", re.I), "slash": re.compile(r"slash|stab_?miss", re.I),
    "reload": re.compile(r"reload", re.I), "run": re.compile(r"^run", re.I),
    "walk": re.compile(r"^walk", re.I), "crouch": re.compile(r"crouch", re.I),
    "die": re.compile(r"die|death", re.I), "jump": re.compile(r"jump", re.I),
}
MDL_BAD = re.compile(r"empty|dry", re.I)
DEATH_RX = re.compile(r"^(death|die|dead|crouch_?die|head$|gutshot|forward$|back$|left$|right$)", re.I)


def wrap180(a):
    return (a + 180.0) % 360.0 - 180.0


def fresh_anim():
    return {"seq": -1, "time": 0.0, "loop": True, "kind": "",
            "init": False, "gyaw": None, "twist": 0.0,
            "gs": -1, "gt": 0.0, "gl": True, "gk": "",
            "us": -1, "ut": 0.0, "ul": True, "uk": "", "ukey": None, "pst": 0.0,
            "dead": False, "ds": -1, "dt": 0.0, "air": False}


def weapon_tags(w):
    """CS player models name their aim/shoot/reload sequences per weapon class
    (ref_aim_ak47, ref_shoot_rifle, crouch_aim_knife ...). Best guesses first."""
    if w is not None and w.melee:
        return ["knife", "onehanded"]
    nm = re.sub(r"[^a-z0-9]", "", (getattr(w, "n", "") or "").lower()) if w is not None else ""
    pre = {"awp": ["rifle", "carbine"], "scout": ["rifle", "carbine"], "ak47": ["ak47"],
           "m4a1": ["carbine"], "mp5": ["mp5", "mp5n"], "m249": ["m249", "para"]}.get(nm, [])
    out = ([nm] if nm else []) + pre + ["ak47", "rifle", "carbine", "mp5", "m249", "onehanded"]
    seen, res = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            res.append(t)
    return res


def upper_seq(pm, w, kind, crouch):
    """Upper-body sequence for `kind` in (aim, shoot, reload), weapon + stance aware."""
    prefixes = ["crouch_", "ref_"] if crouch else ["ref_"]
    pats = []
    for pf in prefixes:
        for t in weapon_tags(w):
            pats.append(r"^%s%s_%s" % (pf, kind, t))
        pats.append(r"^%s%s" % (pf, kind))
    s = pm.d.pick(*pats)
    if s < 0 and kind == "aim":
        s = pm.base_seq
    return s


def quat_mat(q):
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def read_mdl_tex(buf, n, o):
    out = []
    for i in range(n):
        p = o + i * 80
        fl, w, h, ix = struct.unpack_from("<iiii", buf, p + 64)
        if not w or not h or w > 2048 or h > 2048:
            out.append(None)
            continue
        idx = np.frombuffer(buf, np.uint8, w * h, ix)
        pal = np.frombuffer(buf, np.uint8, 768, ix + w * h).reshape(256, 3)
        rgba = np.empty((w * h, 4), np.uint8)
        rgba[:, :3] = pal[idx]
        rgba[:, 3] = 255
        if fl & 0x40:
            rgba[idx == 255, 3] = 0
        out.append({"t": make_texture(rgba.reshape(h, w, 4), w, h), "w": w, "h": h, "fl": fl})
    return out


class MdlData:
    """Shared, immutable model data (geometry, textures, bones, sequences)."""

    def __init__(self, buf, pack=None):
        self.buf = buf
        I = lambda o: struct.unpack_from("<i", buf, o)[0]
        F = lambda o: struct.unpack_from("<f", buf, o)[0]
        S = lambda o: struct.unpack_from("<h", buf, o)[0]
        self.bbmin_z = F(120)
        nb, bo, ns, so = I(140), I(144), I(164), I(168)
        nt, to, sko, nbp, bpo = I(180), I(184), I(200), I(204), I(208)
        tex = read_mdl_tex(buf, nt, to)
        if not tex and pack:
            tex = read_mdl_tex(pack, struct.unpack_from("<i", pack, 180)[0],
                               struct.unpack_from("<i", pack, 184)[0])
        self.bones = []
        for i in range(nb):
            p = bo + i * 112
            self.bones.append({
                "name": buf[p:p + 32].split(b"\0")[0].decode("latin1"),
                "par": I(p + 32),
                "val": list(struct.unpack_from("<6f", buf, p + 64)),
                "sc": list(struct.unpack_from("<6f", buf, p + 88)),
                "ctl": list(struct.unpack_from("<6i", buf, p + 40)),
            })
        # bone controllers (torso twist = controllers 0..3, mouth = 4)
        self.ctls = []
        for i in range(max(0, I(148))):
            p = I(152) + i * 24
            self.ctls.append({"type": I(p + 4), "start": F(p + 8), "end": F(p + 12), "idx": I(p + 20)})
        # upper body = the spine bone and everything below it in the hierarchy
        up = [False] * len(self.bones)
        for i, B in enumerate(self.bones):
            par = B["par"]
            up[i] = bool(re.search(r"spine", B["name"], re.I)) or (0 <= par < i and up[par])
        self.upper = up if any(up) else None
        self.seqs = []
        for i in range(ns):
            p = so + i * 176
            lb = buf[p:p + 32].split(b"\0")[0].decode("latin1")
            self.seqs.append({"label": lb, "fps": F(p + 32) or 30.0, "n": max(1, I(p + 56)),
                              "anim": I(p + 124), "grp": I(p + 156),
                              "nbl": max(1, I(p + 120)), "bs": F(p + 136), "be": F(p + 144),
                              "lin": math.sqrt(F(p + 76) ** 2 + F(p + 80) ** 2 + F(p + 84) ** 2)})
        self.parts = []
        for b in range(nbp):
            bp = bpo + b * 76
            nm, mi = I(bp + 64), I(bp + 72)
            mp = -1
            for j in range(nm):
                q = mi + j * 112
                if I(q + 72) > 0 and I(q + 80) > 0:
                    mp = q
                    break
            if mp < 0:
                continue
            nmesh, mo, nv, vio, vo = I(mp + 72), I(mp + 76), I(mp + 80), I(mp + 84), I(mp + 88)
            verts = np.frombuffer(buf, "<f4", nv * 3, vo).reshape(nv, 3).astype(np.float64)
            vb = np.frombuffer(buf, np.uint8, nv, vio).astype(np.int64)
            for k in range(nmesh):
                m = mo + k * 20
                tp = I(m + 4)
                sk = S(sko + I(m + 8) * 2)
                tx = tex[sk] if 0 <= sk < len(tex) else None
                src, uv = [], []
                p = tp
                while True:
                    n = S(p)
                    p += 2
                    if not n:
                        break
                    fan = n < 0
                    n = abs(n)
                    Lst = []
                    for _ in range(n):
                        Lst.append(struct.unpack_from("<4h", buf, p))
                        p += 8
                    for i in range(n - 2):
                        if fan:
                            tri = (Lst[0], Lst[i + 1], Lst[i + 2])
                        elif i & 1:
                            tri = (Lst[i + 1], Lst[i], Lst[i + 2])
                        else:
                            tri = (Lst[i], Lst[i + 1], Lst[i + 2])
                        for a in tri:
                            src.append(a[0])
                            uv.append((a[2] / tx["w"], a[3] / tx["h"]) if tx else (0.0, 0.0))
                if not src:
                    continue
                self.parts.append({
                    "src": np.array(src, np.int64), "verts": verts, "vb": vb,
                    "uv": np.ascontiguousarray(np.array(uv, np.float32)), "tex": tx,
                })

    def pick(self, *pats):
        """First usable sequence matching the regexes, in priority order (-1 = none)."""
        for rx in pats:
            if isinstance(rx, str):
                rx = re.compile(rx, re.I)
            for i, s in enumerate(self.seqs):
                if (s["grp"] == 0 and s["anim"] > 0 and rx.search(s["label"])
                        and not MDL_BAD.search(s["label"])):
                    return i
        return -1

    def pick_all(self, rx):
        return [i for i, s in enumerate(self.seqs)
                if s["grp"] == 0 and s["anim"] > 0 and rx.search(s["label"])]

    def find(self, kind):
        rx = MDL_RX.get(kind)
        if not rx:
            return []
        hit = [i for i, s in enumerate(self.seqs) if rx.search(s["label"]) and not MDL_BAD.search(s["label"])]
        good = [i for i in hit if self.seqs[i]["grp"] == 0 and self.seqs[i]["anim"] > 0]
        return good   # never hand back sequences we cannot play: they would snap to the bind pose


class MdlInst:
    """Per-entity pose state on top of shared MdlData."""

    def __init__(self, data):
        self.d = data
        self.seqs = data.seqs
        self.last = None
        self.cv = {}            # bone controller values (degrees)
        self.snap = self.snap_cur = None
        self.bt = self.bdur = 0.0
        self.pos = [None] * len(data.parts)
        # Upper-body pose: GoldSrc player gait sequences (run/walk/jump) only animate the
        # legs, so the rest of the body must come from an aim/idle sequence or it T-poses.
        ok = [i for i, q in enumerate(data.seqs)
              if q["grp"] == 0 and q["anim"] > 0 and not re.search(r"crouch|die|death|dead", q["label"], re.I)]
        aim = [i for i in ok if re.search(r"ref_aim|^aim", data.seqs[i]["label"], re.I)]
        aim.sort(key=lambda i: 0 if re.search(r"ak|rifle|m4|carbine", data.seqs[i]["label"], re.I) else 1)
        idl = [i for i in ok if re.match(r"idle", data.seqs[i]["label"], re.I)]
        self.base_seq = (aim or idl or ok or [-1])[0]
        idle = data.find("idle")
        self.pose(idle[0] if idle else (0 if data.seqs else -1), 0)

    def find(self, kind):
        return self.d.find(kind)

    def is_mirrored(self):
        """True when the left arm/hand sits on the right side of the screen
        (some models are built mirrored). Camera looks +X, +Y is screen-left."""
        lb = re.compile(r"(^|[\s_.\-])(l|left)($|[\s_.\-])|left", re.I)
        rb = re.compile(r"(^|[\s_.\-])(r|right)($|[\s_.\-])|right", re.I)
        ly, ry = [], []
        for part, pos in zip(self.d.parts, self.pos):
            if pos is None:
                continue
            vb = part["vb"][part["src"]]
            for b in set(vb.tolist()):
                if b >= len(self.d.bones):
                    continue
                nm = self.d.bones[b]["name"]
                m = vb == b
                if lb.search(nm) and not rb.search(nm):
                    ly.append(pos[m][:, 1])
                elif rb.search(nm) and not lb.search(nm):
                    ry.append(pos[m][:, 1])
        if not ly or not ry:
            return False
        return float(np.concatenate(ly).mean()) < float(np.concatenate(ry).mean())

    def _aval(self, p, k):
        buf = self.d.buf
        g = 0
        while buf[p + 1] <= k:
            tt = buf[p + 1]
            if not tt or g > 999:
                return 0
            g += 1
            k -= tt
            p += (buf[p] + 1) * 2
        vl = buf[p]
        return struct.unpack_from("<h", buf, p + (k + 1 if vl > k else vl) * 2)[0]

    def _anim_base(self, si):
        s = self.d.seqs[si] if 0 <= si < len(self.d.seqs) else None
        return s["anim"] if (s and s["grp"] == 0 and s["anim"] > 0) else -1

    def pose(self, si, f, base=-1, bf=0, loop=False):
        """Pose sequence `si` at frame `f`. Bones the sequence does not animate
        (e.g. the arms in a run cycle) take their pose from sequence `base`.
        loop: the last frame blends into the first one (no pop at the loop point)."""
        key = (si, f, base, bf, loop)
        if key == self.last and self.bt <= 0:
            return
        self.last = key
        d = self.d
        buf = d.buf
        # smooth animation: blend between this frame and the next one
        f0 = int(f)
        fr = f - f0
        nfr = d.seqs[si]["n"] if 0 <= si < len(d.seqs) else 1
        f0 = max(0, min(f0, nfr - 1))
        f1 = f0 + 1
        if f1 >= nfr:
            f1 = 0 if loop else nfr - 1
        if f1 == f0:
            fr = 0.0
        f = f0
        ab = self._anim_base(si)
        bab = self._anim_base(base) if base is not None and base != si else -1
        vs = []
        for b, B in enumerate(d.bones):
            v = list(B["val"])
            used = False
            if ab >= 0:
                for j in range(6):
                    off = struct.unpack_from("<H", buf, ab + b * 12 + j * 2)[0]
                    if off:
                        used = True
                        sc = B["sc"][j]
                        a0 = self._aval(ab + b * 12 + off, f) * sc
                        if fr:
                            da = self._aval(ab + b * 12 + off, f1) * sc - a0
                            if j >= 3:  # angles: take the short way round
                                da = (da + math.pi) % (2 * math.pi) - math.pi
                            elif abs(da) > 40:  # huge jump = hide/teleport in the data, don't slide
                                da = 0.0
                            a0 += da * fr
                        v[j] += a0
            if not used and bab >= 0:
                for j in range(6):
                    off = struct.unpack_from("<H", buf, bab + b * 12 + j * 2)[0]
                    if off:
                        v[j] += self._aval(bab + b * 12 + off, bf) * B["sc"][j]
            vs.append(v)
        # short cross-fade when the sequence just changed, so the hands don't snap between poses
        if self.bt > 0 and self.snap and len(self.snap) == len(vs):
            w = 1.0 - self.bt / max(1e-4, self.bdur)
            w = w * w * (3 - 2 * w)
            for v, o in zip(vs, self.snap):
                for j in range(6):
                    dv = v[j] - o[j]
                    if j >= 3:
                        dv = (dv + math.pi) % (2 * math.pi) - math.pi
                    v[j] = o[j] + dv * w
        self.snap_cur = [list(v) for v in vs]
        self._build(vs)

    # ---- CS 1.6 style player posing: gait (legs) + aim (upper body) + pitch blend + torso twist ----
    def _blends(self, s, val):
        """Which blend slots of a multi-blend sequence (aim pitch) to mix, and how much."""
        nbl = s["nbl"]
        span = s["be"] - s["bs"]
        if nbl <= 1 or abs(span) < 1e-3:
            return ((0, 1.0),)
        t = min(1.0, max(0.0, (val - s["bs"]) / span))
        pos = t * (nbl - 1)
        i0 = min(nbl - 2, int(pos))
        w = pos - i0
        return ((i0, 1.0 - w), (i0 + 1, w))

    def _sval(self, s, f, loop, bl, b, B):
        """Absolute local pose (6 values) of bone b for sequence s at frame f, or None
        when that sequence does not animate the bone."""
        buf = self.d.buf
        nb = len(self.d.bones)
        n = s["n"]
        f0 = min(int(f), n - 1)
        fr = f - int(f)
        f1 = f0 + 1
        if f1 >= n:
            f1 = 0 if loop else n - 1
        if f1 == f0:
            fr = 0.0
        outs = []
        for bi, bw in bl:
            base = s["anim"] + (bi * nb + b) * 12
            vals = [0.0] * 6
            used = False
            for j in range(6):
                off = struct.unpack_from("<H", buf, base + j * 2)[0]
                if not off:
                    continue
                used = True
                sc = B["sc"][j]
                a0 = self._aval(base + off, f0) * sc
                if fr:
                    da = self._aval(base + off, f1) * sc - a0
                    if j >= 3:
                        da = (da + math.pi) % (2 * math.pi) - math.pi
                    elif abs(da) > 40:
                        da = 0.0
                    a0 += da * fr
                vals[j] = a0
            if not used:
                return None
            outs.append((vals, bw))
        v = outs[0][0]
        if len(outs) == 2:
            w = outs[1][1]
            v2 = outs[1][0]
            v = [v[j] + ((v2[j] - v[j] + math.pi) % (2 * math.pi) - math.pi if j >= 3 else v2[j] - v[j]) * w
                 for j in range(6)]
        return [B["val"][j] + v[j] for j in range(6)]

    def begin_fade(self, dur=FADE_TIME):
        if self.snap_cur:
            self.snap = self.snap_cur
            self.bt = self.bdur = dur

    def tick(self, dt):
        if self.bt > 0:
            self.bt = max(0.0, self.bt - dt)

    def pose_player(self, up, uf, uloop, gt, gf, gloop, pitch, twist):
        """up/uf: upper-body sequence+frame (aim / shoot / reload / death).
        gt/gf: gait sequence+frame (walk / run / crouchrun / jump) or -1 for none.
        pitch: aim blend value (deg).  twist: torso twist vs the legs (deg)."""
        key = ("P", up, round(uf * 4), uloop, gt, round(gf * 4), gloop, round(pitch), round(twist))
        if key == self.last and self.bt <= 0:
            return
        self.last = key
        d = self.d
        S = d.seqs
        su = S[up] if 0 <= up < len(S) else None
        sg = S[gt] if 0 <= gt < len(S) else None
        blu = self._blends(su, pitch) if su else ((0, 1.0),)
        blg = self._blends(sg, pitch) if sg else ((0, 1.0),)
        t4 = twist * TWIST_SIGN / 4.0
        for i in range(4):
            self.cv[i] = t4
        vs = []
        for b, B in enumerate(d.bones):
            vu = self._sval(su, uf, uloop, blu, b, B) if su else None
            vg = self._sval(sg, gf, gloop, blg, b, B) if sg else None
            if d.upper and d.upper[b]:
                v = vu or vg
            else:
                v = vg or vu
            vs.append(list(v) if v else list(B["val"]))
        if self.bt > 0 and self.snap and len(self.snap) == len(vs):
            w = 1.0 - self.bt / max(1e-4, self.bdur)
            w = w * w * (3 - 2 * w)
            for v, o in zip(vs, self.snap):
                for j in range(6):
                    dv = v[j] - o[j]
                    if j >= 3:
                        dv = (dv + math.pi) % (2 * math.pi) - math.pi
                    v[j] = o[j] + dv * w
        self.snap_cur = [list(v) for v in vs]
        self._build(vs)

    def _build(self, vs):
        d = self.d
        Ms = []
        for b, B in enumerate(d.bones):
            v = vs[b]
            if self.cv:
                v = list(v)
                for j in range(6):
                    c = B["ctl"][j]
                    if 0 <= c < len(d.ctls):
                        ct = d.ctls[c]
                        x = self.cv.get(ct["idx"])
                        if x is not None:
                            lo, hi = min(ct["start"], ct["end"]), max(ct["start"], ct["end"])
                            x = min(hi, max(lo, x))
                            v[j] += math.radians(x) if j >= 3 else x
            sr, cr = math.sin(v[3] / 2), math.cos(v[3] / 2)
            sp, cp = math.sin(v[4] / 2), math.cos(v[4] / 2)
            sy, cy = math.sin(v[5] / 2), math.cos(v[5] / 2)
            q = (sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy,
                 cr * cp * sy - sr * sp * cy, cr * cp * cy + sr * sp * sy)
            loc = np.eye(4)
            loc[:3, :3] = quat_mat(q)
            loc[:3, 3] = v[:3]
            Ms.append(Ms[B["par"]] @ loc if B["par"] >= 0 else loc)
        FM = np.stack(Ms) if Ms else np.eye(4)[None]
        for i, p in enumerate(d.parts):
            Mv = FM[np.minimum(p["vb"], len(FM) - 1)]
            out = np.einsum("nij,nj->ni", Mv[:, :3, :3], p["verts"]) + Mv[:, :3, 3]
            self.pos[i] = np.ascontiguousarray(out[p["src"]], dtype=np.float32)

    def min_z(self):
        m = math.inf
        for a in self.pos:
            if a is not None and len(a):
                m = min(m, float(a[:, 2].min()))
        return self.d.bbmin_z if m == math.inf else m

    def draw(self):
        tex2d(True)
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glColor4f(1, 1, 1, 1)
        # additive parts (muzzle flash) go last and never write depth, so they can't cut holes in the gun
        items = sorted(zip(self.d.parts, self.pos),
                       key=lambda it: bool(it[0]["tex"] and it[0]["tex"]["fl"] & 0x20))
        for p, pos in items:
            if pos is None:
                continue
            tx = p["tex"]
            if tx:
                gl.glBindTexture(gl.GL_TEXTURE_2D, tx["t"])
                tex2d(True)
                if tx["fl"] & 0x40:
                    gl.glEnable(gl.GL_ALPHA_TEST)
                    gl.glAlphaFunc(gl.GL_GREATER, .5)
                if tx["fl"] & 0x20:
                    gl.glEnable(gl.GL_BLEND)
                    gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE)
                    gl.glDepthMask(gl.GL_FALSE)
            else:
                tex2d(False)
                gl.glColor4f(.47, .47, .47, 1)
            gl.glVertexPointer(3, gl.GL_FLOAT, 0, pos)
            gl.glTexCoordPointer(2, gl.GL_FLOAT, 0, p["uv"])
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, len(pos))
            gl.glDisable(gl.GL_ALPHA_TEST)
            if tx and tx["fl"] & 0x20:
                gl.glDepthMask(gl.GL_TRUE)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                gl.glDisable(gl.GL_BLEND)
            gl.glColor4f(1, 1, 1, 1)
        tex2d(True)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)


# --------------------------------------------------------------------------
# Source engine (CS:GO) weapon mods: .vpk packages
#   VPK v1/v2 reader, MDL v44-49 + VVD + VTX model loader (with RLE animation),
#   VTF textures (DXT1/3/5 + common uncompressed formats), KeyValues parser.
# Pure python + numpy. No GL here: SrcModel only produces numpy arrays.
# --------------------------------------------------------------------------
class VpkArchive:
    """Reads a Valve .vpk (single file, or *_dir.vpk with *_000.vpk archives). Lazy: files are
    read from disk only when asked for, so a 165 MB package costs no memory."""

    def __init__(self, path):
        self.path = str(path)
        self.files = {}
        self._fh = {}
        with open(self.path, "rb") as f:
            sig, ver, tree = struct.unpack("<III", f.read(12))
            if sig != 0x55AA1234 or ver not in (1, 2):
                raise ValueError("not a VPK file")
            hdr = 28 if ver == 2 else 12
            f.seek(hdr)
            b = f.read(tree)
        self.base = hdr + tree
        p = 0

        def cs():
            nonlocal p
            e = b.index(b"\0", p)
            s = b[p:e].decode("utf-8", "replace")
            p = e + 1
            return s
        while True:
            ext = cs()
            if not ext:
                break
            while True:
                d = cs()
                if not d:
                    break
                while True:
                    n = cs()
                    if not n:
                        break
                    _crc, pre, arch, off, ln, _t = struct.unpack_from("<IHHIIH", b, p)
                    p += 18
                    preload = b[p:p + pre]
                    p += pre
                    nm = (d + "/" if d != " " else "") + n + ("." + ext if ext != " " else "")
                    self.files[nm.replace("\\", "/").lower()] = (arch, off, ln, preload)

    def _open(self, arch):
        if arch not in self._fh:
            if arch == 0x7FFF:
                self._fh[arch] = open(self.path, "rb")
            else:
                self._fh[arch] = open(re.sub(r"_dir\.vpk$", "_%03d.vpk" % arch, self.path), "rb")
        return self._fh[arch]

    def has(self, name):
        return name.lower().replace("\\", "/") in self.files

    def size(self, name):
        e = self.files[name.lower().replace("\\", "/")]
        return len(e[3]) + e[2]

    def read(self, name, start=0, length=None):
        """Bytes [start, start+length) of the file (whole file by default)."""
        arch, off, ln, pre = self.files[name.lower().replace("\\", "/")]
        total = len(pre) + ln
        if length is None:
            length = total - start
        out = b""
        if start < len(pre):
            out = pre[start:start + length]
            start2, length2 = 0, length - len(out)
        else:
            start2, length2 = start - len(pre), length
        if length2 > 0 and ln:
            fh = self._open(arch)
            fh.seek((self.base if arch == 0x7FFF else 0) + off + start2)
            out += fh.read(min(length2, ln - start2))
        return out

    def close(self):
        for f in self._fh.values():
            f.close()
        self._fh = {}


# ----- KeyValues (VMT, game_sounds, items_game, addon) -------------------------------------
def parse_kv(text):
    """Valve KeyValues -> nested dict with lower-case keys. Repeated keys keep the last value."""
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|([^\s{}"/]+)', text)
    pos = 0
    toks = [(a or c or b, bool(b)) for a, b, c in toks if a or b or c]

    def block():
        nonlocal pos
        d = {}
        while pos < len(toks):
            t, brace = toks[pos]
            pos += 1
            if brace and t == "}":
                return d
            if brace:
                continue
            if pos < len(toks) and toks[pos][1] and toks[pos][0] == "{":
                pos += 1
                d[t.lower()] = block()
            elif pos < len(toks):
                d[t.lower()] = toks[pos][0]
                pos += 1
        return d
    root = {}
    while pos < len(toks):
        t, brace = toks[pos]
        pos += 1
        if brace:
            continue
        if pos < len(toks) and toks[pos][1] and toks[pos][0] == "{":
            pos += 1
            root[t.lower()] = block()
        elif pos < len(toks):
            root[t.lower()] = toks[pos][0]
            pos += 1
    return root


# ----- VTF textures ---------------------------------------------------------------------------
_VTF_BPP = {0: 4, 1: 4, 2: 3, 3: 3, 4: 2, 5: 1, 6: 2, 7: 1, 8: 1, 11: 4, 12: 4, 16: 4, 17: 2,
            18: 2, 19: 2, 21: 2, 22: 2, 23: 4, 26: 4}


def _vtf_size(fmt, w, h):
    if fmt in (13, 20):
        return ((w + 3) // 4) * ((h + 3) // 4) * 8
    if fmt in (14, 15):
        return ((w + 3) // 4) * ((h + 3) // 4) * 16
    return w * h * _VTF_BPP.get(fmt, 4)


def _rgb565(c):
    c = c.astype(np.int32)
    r = (c >> 11) & 31
    g = (c >> 5) & 63
    b = c & 31
    return np.stack([(r * 255 + 15) // 31, (g * 255 + 31) // 63, (b * 255 + 15) // 31], -1)


def dxt_decode(data, w, h, fmt):
    bw, bh = (w + 3) // 4, (h + 3) // 4
    bs = 8 if fmt in (13, 20) else 16
    a = np.frombuffer(data, np.uint8, bw * bh * bs).reshape(bw * bh, bs)
    cb = a[:, bs - 8:].astype(np.uint32)
    c0 = cb[:, 0] | (cb[:, 1] << 8)
    c1 = cb[:, 2] | (cb[:, 3] << 8)
    bits = cb[:, 4] | (cb[:, 5] << 8) | (cb[:, 6] << 16) | (cb[:, 7] << 24)
    p0, p1 = _rgb565(c0), _rgb565(c1)
    four = (c0 > c1)[:, None]
    p2 = np.where(four, (2 * p0 + p1) // 3, (p0 + p1) // 2)
    p3 = np.where(four, (p0 + 2 * p1) // 3, 0)
    pal = np.stack([p0, p1, p2, p3], 1)                                  # (n,4,3)
    sh = (np.arange(16, dtype=np.uint32) * 2)[None, :]
    idx = ((bits[:, None] >> sh) & 3).astype(np.int64)                   # (n,16)
    rgb = np.take_along_axis(pal, idx[:, :, None].repeat(3, 2), 1)       # (n,16,3)
    alpha = np.full((bw * bh, 16), 255, np.int32)
    if fmt in (13, 20):
        if fmt == 20:
            alpha = np.where((~four) & (idx == 3), 0, 255).astype(np.int32)
    elif fmt == 14:
        ab = a[:, :8].astype(np.uint64)
        v = np.zeros(bw * bh, np.uint64)
        for i in range(8):
            v |= ab[:, i] << np.uint64(8 * i)
        al = ((v[:, None] >> (np.arange(16, dtype=np.uint64) * np.uint64(4))[None, :]) & np.uint64(15)).astype(np.int32)
        alpha = al * 17
    else:
        a0 = a[:, 0].astype(np.int32)
        a1 = a[:, 1].astype(np.int32)
        v = np.zeros(bw * bh, np.uint64)
        for i in range(6):
            v |= a[:, 2 + i].astype(np.uint64) << np.uint64(8 * i)
        ai = ((v[:, None] >> (np.arange(16, dtype=np.uint64) * np.uint64(3))[None, :]) & np.uint64(7)).astype(np.int64)
        ap = np.zeros((bw * bh, 8), np.int32)
        ap[:, 0], ap[:, 1] = a0, a1
        big = (a0 > a1)
        for k in range(1, 7):
            ap[:, k + 1] = np.where(big, ((7 - k) * a0 + k * a1) // 7, 0)
        for k in range(1, 5):
            ap[:, k + 1] = np.where(big, ap[:, k + 1], ((5 - k) * a0 + k * a1) // 5)
        ap[:, 6] = np.where(big, ap[:, 6], 0)
        ap[:, 7] = np.where(big, ap[:, 7], 255)
        alpha = np.take_along_axis(ap, ai, 1)
    px = np.concatenate([rgb, alpha[:, :, None]], 2).astype(np.uint8)   # (n,16,4)
    px = px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return px[:h, :w]


def load_vtf(vpk, name, max_dim=1024):
    """Returns (rgba uint8 array (h,w,4), has_real_alpha) for the biggest mip <= max_dim."""
    head = vpk.read(name, 0, 96)
    if head[:4] != b"VTF\0":
        raise ValueError("bad VTF")
    hsize = struct.unpack_from("<I", head, 12)[0]
    w, h = struct.unpack_from("<HH", head, 16)
    flags = struct.unpack_from("<I", head, 20)[0]
    frames = max(1, struct.unpack_from("<H", head, 24)[0])
    fmt = struct.unpack_from("<i", head, 52)[0]
    mips = head[56]
    lfmt = struct.unpack_from("<i", head, 57)[0]
    lw, lh = head[61], head[62]
    off = hsize + (_vtf_size(lfmt, lw, lh) if lfmt != -1 and lw and lh else 0)
    m = 0
    while max(w >> m, h >> m) > max_dim and m < mips - 1:
        m += 1
    sizes = [_vtf_size(fmt, max(1, w >> k), max(1, h >> k)) for k in range(mips)]
    faces = 6 if flags & 0x4000 else 1
    pos = off + sum(sizes[m + 1:]) * frames * faces
    mw, mh = max(1, w >> m), max(1, h >> m)
    raw = vpk.read(name, pos, sizes[m])
    if fmt in (13, 14, 15, 20):
        img = dxt_decode(raw, mw, mh, fmt)
    else:
        bpp = _VTF_BPP.get(fmt, 4)
        a = np.frombuffer(raw, np.uint8, mw * mh * bpp).reshape(mh, mw, bpp)
        if fmt == 0:
            img = a
        elif fmt == 12:
            img = a[:, :, [2, 1, 0, 3]]
        elif fmt == 11:
            img = a[:, :, [1, 2, 3, 0]]
        elif fmt == 1:
            img = a[:, :, [3, 2, 1, 0]]
        elif fmt == 16:
            img = np.concatenate([a[:, :, [2, 1, 0]], np.full((mh, mw, 1), 255, np.uint8)], 2)
        elif fmt == 2:
            img = np.concatenate([a, np.full((mh, mw, 1), 255, np.uint8)], 2)
        elif fmt == 3:
            img = np.concatenate([a[:, :, ::-1], np.full((mh, mw, 1), 255, np.uint8)], 2)
        elif fmt in (5, 8):
            g = a[:, :, 0]
            img = np.stack([g, g, g, np.full_like(g, 255)], 2)
        elif fmt == 6:
            img = np.stack([a[:, :, 0]] * 3 + [a[:, :, 1]], 2)
        else:
            raise ValueError("unsupported VTF format %d" % fmt)
    return np.ascontiguousarray(img, dtype=np.uint8), bool(flags & 0x2000 or flags & 0x1000)


# ----- model ----------------------------------------------------------------------------------
def _quat_mat3(q):
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _angle_quat(ax, ay, az):
    sy, cy = math.sin(az * .5), math.cos(az * .5)
    sp, cp = math.sin(ay * .5), math.cos(ay * .5)
    sr, cr = math.sin(ax * .5), math.cos(ax * .5)
    return (sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy, cr * cp * cy + sr * sp * sy)


def _slerp(a, b, t):
    d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]
    if d < 0:
        b = (-b[0], -b[1], -b[2], -b[3])
        d = -d
    if d > 0.9995:
        r = [a[i] + (b[i] - a[i]) * t for i in range(4)]
    else:
        th = math.acos(min(1.0, d))
        s = math.sin(th)
        w0, w1 = math.sin((1 - t) * th) / s, math.sin(t * th) / s
        r = [a[i] * w0 + b[i] * w1 for i in range(4)]
    n = math.sqrt(sum(v * v for v in r)) or 1.0
    return tuple(v / n for v in r)


class SrcModel:
    """A Source studio model (MDL v44-49 + VVD + VTX): bodygroup 0 geometry, bones, sequences."""

    def __init__(self, mdl, vvd, vtx):
        b = self.b = mdl
        if b[:4] != b"IDST":
            raise ValueError("not a Source MDL")
        self.version = struct.unpack_from("<i", b, 4)[0]
        if not 44 <= self.version <= 49:
            raise ValueError("unsupported Source MDL version %d" % self.version)
        I = lambda o: struct.unpack_from("<i", b, o)[0]
        F = lambda o: struct.unpack_from("<f", b, o)[0]
        S = lambda o: struct.unpack_from("<h", b, o)[0]
        cs = lambda o: b[o:b.index(b"\0", o)].decode("latin1")
        self.name = b[12:76].split(b"\0")[0].decode("latin1")
        # ---- bones
        nb, bo = I(156), I(160)
        self.bones = []
        for i in range(nb):
            p = bo + i * 216
            self.bones.append({
                "name": cs(p + I(p)), "par": I(p + 4),
                "pos": np.array(struct.unpack_from("<3f", b, p + 32)),
                "quat": struct.unpack_from("<4f", b, p + 44),
                "rot": struct.unpack_from("<3f", b, p + 60),
                "ps": struct.unpack_from("<3f", b, p + 72),
                "rs": struct.unpack_from("<3f", b, p + 84),
                "p2b": np.vstack([np.array(struct.unpack_from("<12f", b, p + 96)).reshape(3, 4), [0, 0, 0, 1]]),
            })
        self.nb = nb
        # ---- textures / skins
        self.tex_names = [cs(I(208) + i * 64 + I(I(208) + i * 64)) for i in range(I(204))]
        self.cd = [cs(I(I(216) + i * 4)).replace("\\", "/").lower() for i in range(I(212))]
        nref, nfam, si = I(220), I(224), I(228)
        self.skin = [S(si + j * 2) for j in range(nref)] if nfam else list(range(nref))
        # ---- vertices (VVD)
        if vvd[:4] != b"IDSV":
            raise ValueError("bad VVD")
        nfix, fixo, vdo, tdo = struct.unpack_from("<4i", vvd, 48 + 0) if False else (
            struct.unpack_from("<i", vvd, 48)[0], struct.unpack_from("<i", vvd, 52)[0],
            struct.unpack_from("<i", vvd, 56)[0], struct.unpack_from("<i", vvd, 60)[0])
        nlod0 = struct.unpack_from("<i", vvd, 16)[0]
        raw_n = (tdo - vdo) // 48 if tdo > vdo else (len(vvd) - vdo) // 48
        dt = np.dtype([("w", "<f4", 3), ("bi", "u1", 3), ("nbw", "u1"), ("p", "<f4", 3),
                       ("n", "<f4", 3), ("uv", "<f4", 2)])
        raw = np.frombuffer(vvd, dt, raw_n, vdo)
        if nfix:
            fx = np.frombuffer(vvd, "<i4", nfix * 3, fixo).reshape(nfix, 3)
            raw = np.concatenate([raw[s:s + n] for lod, s, n in fx])
        V = raw[:max(nlod0, 1)] if not nfix else raw
        self.v_pos = np.ascontiguousarray(V["p"], np.float64)
        self.v_uv = np.ascontiguousarray(V["uv"], np.float32)
        self.v_w = np.ascontiguousarray(V["w"], np.float32)
        self.v_bi = np.ascontiguousarray(V["bi"], np.int64)
        self.v_nb = np.ascontiguousarray(V["nbw"], np.int64)
        # ---- meshes: MDL bodyparts (first model of each) + VTX strips
        self.parts = []
        nbody, bpo = I(232), I(236)
        sv = struct.unpack_from
        vh_nbp, vh_bpo = sv("<i", vtx, 28)[0], sv("<i", vtx, 32)[0]
        for bi in range(nbody):
            p = bpo + bi * 16
            nmod, mi = I(p + 4), I(p + 12)
            if nmod <= 0:
                continue
            mp = p + mi                      # model 0 of this body part
            nmesh, mo, nvert, vidx = I(mp + 72), I(mp + 76), I(mp + 80), I(mp + 84)
            if nmesh <= 0 or nvert <= 0:
                continue
            vstart = vidx // 48
            # VTX: same body part / model 0 / LOD 0
            vbp = vh_bpo + bi * 8
            vnm, vmo = sv("<ii", vtx, vbp)
            vm = vbp + vmo
            vnl, vlo = sv("<ii", vtx, vm)
            vl = vm + vlo
            vnmesh, vmeo = sv("<ii", vtx, vl)
            for k in range(min(nmesh, vnmesh)):
                q = mp + mo + k * 116
                mat, voff = I(q), I(q + 12)
                vme = vl + vmeo + k * 9
                nsg, sgo = sv("<ii", vtx, vme)
                tri = []
                for g in range(nsg):
                    sg = vme + sgo + g * 25
                    nv_, vo_, ni_, io_, ns_, so_ = sv("<6i", vtx, sg)
                    vt = np.frombuffer(vtx, np.dtype([("bwi", "u1", 3), ("nb", "u1"), ("orig", "<u2"),
                                                       ("bid", "i1", 3)]), nv_, sg + vo_)
                    ix = np.frombuffer(vtx, "<u2", ni_, sg + io_)
                    glob = (vt["orig"].astype(np.int64) + voff + vstart)
                    for s in range(ns_):
                        st = sg + so_ + s * 27
                        sni, sio, snv, svo, snb, sfl = sv("<iiiihB", vtx, st)
                        sidx = ix[sio:sio + sni].astype(np.int64)
                        if sfl & 2:     # tri strip
                            for t in range(len(sidx) - 2):
                                a, b2, c = sidx[t:t + 3]
                                tri.extend((a, c, b2) if t & 1 else (a, b2, c))
                        else:           # tri list (default)
                            tri.extend(sidx[:len(sidx) // 3 * 3].tolist())
                if not tri:
                    continue
                tri = glob_to = np.array(tri, np.int64)
                # indices above are strip-group local; map them to global vertex ids per group
                self.parts.append({"mat": mat, "tri": tri, "glob": None})
        # the loop above needs the group's glob table per triangle: redo cleanly
        self.parts = self._build_parts(vtx, nbody, bpo)
        # ---- sequences + animation descs
        self.sounds_ev = {}
        na, ao = I(180), I(184)
        self.anims = []
        for i in range(na):
            p = ao + i * 100
            self.anims.append({"name": cs(p + I(p + 4)), "fps": F(p + 8) or 30.0, "flags": I(p + 12),
                               "n": max(1, I(p + 16)), "p": p, "block": I(p + 52), "idx": I(p + 56),
                               "sect": I(p + 80), "sfr": I(p + 84)})
        ns, so = I(188), I(192)
        self.seqs = []
        for i in range(ns):
            p = so + i * 212
            nbl = I(p + 56)
            ai = I(p + 60)
            descs = [S(p + ai + k * 2) for k in range(max(1, nbl))]
            d = self.anims[descs[0]] if 0 <= descs[0] < na else None
            evs = []
            for e in range(I(p + 24)):
                q = p + I(p + 28) + e * 80
                if I(q + 4) in (5004, 5005) and I(q + 8) == 0:
                    evs.append((F(q), b[q + 12:q + 76].split(b"\0")[0].decode("latin1")))
            self.seqs.append({"label": cs(p + I(p + 4)), "flags": I(p + 12),
                              "fps": d["fps"] if d else 30.0, "n": d["n"] if d else 1,
                              "grp": 0, "anim": 1 if d else -1, "desc": d, "events": sorted(evs),
                              "loop": bool(I(p + 12) & 1)})
        self._cache = {}

    def _build_parts(self, vtx, nbody, bpo):
        b = self.b
        I = lambda o: struct.unpack_from("<i", b, o)[0]
        sv = struct.unpack_from
        vh_bpo = sv("<i", vtx, 32)[0]
        parts = {}
        for bi in range(nbody):
            p = bpo + bi * 16
            nmod, mi = I(p + 4), I(p + 12)
            if nmod <= 0:
                continue
            mp = p + mi
            nmesh, mo, nvert, vidx = I(mp + 72), I(mp + 76), I(mp + 80), I(mp + 84)
            if nmesh <= 0 or nvert <= 0:
                continue
            vstart = vidx // 48
            vbp = vh_bpo + bi * 8
            vnm, vmo = sv("<ii", vtx, vbp)
            vm = vbp + vmo
            vnl, vlo = sv("<ii", vtx, vm)
            vl = vm + vlo
            vnmesh, vmeo = sv("<ii", vtx, vl)
            for k in range(min(nmesh, vnmesh)):
                q = mp + mo + k * 116
                mat, voff = I(q), I(q + 12)
                vme = vl + vmeo + k * 9
                nsg, sgo = sv("<ii", vtx, vme)
                tris = []
                for g in range(nsg):
                    sg = vme + sgo + g * 25
                    nv_, vo_, ni_, io_, ns_, so_ = sv("<6i", vtx, sg)
                    vt = np.frombuffer(vtx, np.dtype([("bwi", "u1", 3), ("nb", "u1"), ("orig", "<u2"),
                                                       ("bid", "i1", 3)]), nv_, sg + vo_)
                    ix = np.frombuffer(vtx, "<u2", ni_, sg + io_).astype(np.int64)
                    glob = vt["orig"].astype(np.int64) + voff + vstart
                    for s in range(ns_):
                        st = sg + so_ + s * 27
                        sni, sio, _snv, _svo, _snb, sfl = sv("<iiiihB", vtx, st)
                        sidx = ix[sio:sio + sni]
                        if sfl & 2:
                            for t in range(len(sidx) - 2):
                                a, b2, c = sidx[t:t + 3]
                                tris.append(glob[[a, c, b2]] if t & 1 else glob[[a, b2, c]])
                        else:
                            n3 = len(sidx) // 3 * 3
                            if n3:
                                tris.append(glob[sidx[:n3]].reshape(-1, 3))
                if tris:
                    t = np.concatenate([np.asarray(x).reshape(-1, 3) for x in tris])
                    key = (bi, k)
                    parts[key] = {"mat": mat, "tri": t.reshape(-1)}
        return [parts[k] for k in sorted(parts)]

    # ---- material helper: skin table + cdtextures -> list of candidate material paths
    def material_paths(self, mat):
        ti = self.skin[mat] if 0 <= mat < len(self.skin) else mat
        nm = self.tex_names[ti] if 0 <= ti < len(self.tex_names) else ""
        return [(cd + nm).lower() for cd in (self.cd or [""])] + [nm.lower()]

    # ---- animation ---------------------------------------------------------------------------
    @staticmethod
    def _rle(buf, base, k, scale):
        """mstudioanimvalue_t run-length decode at integer frame k -> value*scale."""
        p = base
        g = 0
        while True:
            valid, total = buf[p], buf[p + 1]
            if total == 0 or g > 4096:
                return 0.0
            if total > k:
                break
            k -= total
            p += (valid + 1) * 2
            g += 1
        if valid > k:
            return struct.unpack_from("<h", buf, p + 2 + k * 2)[0] * scale
        return struct.unpack_from("<h", buf, p + valid * 2)[0] * scale

    def decode_frame(self, d, frame):
        """Local pose of every bone for integer frame of anim desc d: (pos[nb,3], quat[nb,4])."""
        key = (id(d), frame)
        c = self._cache.get(key)
        if c is not None:
            return c
        b = self.b
        nb = self.nb
        pos = np.array([bn["pos"] for bn in self.bones])
        quat = np.array([bn["quat"] for bn in self.bones], np.float64)
        delta = bool(d["flags"] & 0x4)
        if delta:
            pos[:] = 0
            quat[:] = (0, 0, 0, 1)
        if d["block"] != 0 or d["idx"] == 0:
            self._cache[key] = (pos, quat)
            return pos, quat
        base = d["p"] + d["idx"]
        fr = frame
        if d["sect"] and d["sfr"]:
            n, sfr = d["n"], d["sfr"]
            if n > sfr and frame == n - 1:
                si, fr = n // sfr + 1, 0
            else:
                si = frame // sfr
                fr = frame - si * sfr
            sp = d["p"] + d["sect"] + si * 8
            sblock, sidx = struct.unpack_from("<ii", b, sp)
            if sblock != 0:
                self._cache[key] = (pos, quat)
                return pos, quat
            base = d["p"] + sidx
        p = base
        guard = 0
        while guard < 4096:
            guard += 1
            bone, flags, nxt = b[p], b[p + 1], struct.unpack_from("<h", b, p + 2)[0]
            if bone < nb:
                B = self.bones[bone]
                q0 = p + 4
                # rotation
                if flags & 0x2:        # RAWROT: Quaternion48
                    x, y, z = struct.unpack_from("<HHH", b, q0)
                    wneg = z >> 15
                    qx = (x - 32768) / 32768.0
                    qy = (y - 32768) / 32768.0
                    qz = ((z & 0x7FFF) - 16384) / 16384.0
                    qw = math.sqrt(max(0.0, 1 - qx * qx - qy * qy - qz * qz))
                    quat[bone] = (qx, qy, qz, -qw if wneg else qw)
                    q0 += 6
                elif flags & 0x20:     # RAWROT2: Quaternion64
                    v = struct.unpack_from("<Q", b, q0)[0]
                    qx = ((v & 0x1FFFFF) - 1048576) / 1048576.5
                    qy = (((v >> 21) & 0x1FFFFF) - 1048576) / 1048576.5
                    qz = (((v >> 42) & 0x1FFFFF) - 1048576) / 1048576.5
                    qw = math.sqrt(max(0.0, 1 - qx * qx - qy * qy - qz * qz))
                    quat[bone] = (qx, qy, qz, -qw if (v >> 63) else qw)
                    q0 += 8
                rot_ptr = None
                if flags & 0x8:        # ANIMROT: valueptr
                    rot_ptr = q0
                    q0 += 6
                pos_ptr = None
                if flags & 0x4:        # ANIMPOS
                    pos_ptr = q0
                    q0 += 6
                if rot_ptr is not None:
                    offs = struct.unpack_from("<3h", b, rot_ptr)
                    ang = [0.0, 0.0, 0.0]
                    for j in range(3):
                        if offs[j]:
                            ang[j] = self._rle(b, rot_ptr + offs[j], fr, B["rs"][j])
                        if not delta:
                            ang[j] += B["rot"][j]
                    quat[bone] = _angle_quat(*ang)
                if flags & 0x1:        # RAWPOS: Vector48 (float16 x3)
                    pos[bone] = np.frombuffer(b, "<f2", 3, q0).astype(np.float64)
                elif pos_ptr is not None:
                    offs = struct.unpack_from("<3h", b, pos_ptr)
                    for j in range(3):
                        v = self._rle(b, pos_ptr + offs[j], fr, B["ps"][j]) if offs[j] else 0.0
                        pos[bone][j] = v + (0.0 if delta else B["pos"][j])
            if nxt == 0:
                break
            p += nxt
        self._cache[key] = (pos, quat)
        if len(self._cache) > 6000:
            self._cache.clear()
        return pos, quat


def _find_vpk_model(vpk, mdl_name):
    base = mdl_name[:-4]
    vtx = None
    for ext in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx"):
        if vpk.has(base + ext):
            vtx = base + ext
            break
    if not vtx or not vpk.has(base + ".vvd"):
        raise ValueError("missing .vvd/.vtx for " + mdl_name)
    return vpk.read(mdl_name), vpk.read(base + ".vvd"), vpk.read(vtx)


# --------------------------------------------------------------------------
# Source engine (CS:GO) weapon mods: view-model + animation playback
#   SrcSpec  = lazy handle stored in game.asset["mdl"] (nothing is loaded until it is used)
#   SrcData  = shared geometry + textures        SrcInst = per-weapon pose state (same API as MdlInst)
# Only the model, its textures and its animations are used. Weapon stats (damage, rpm, ammo ...)
# always stay the game's own values.
# --------------------------------------------------------------------------
class LooseSrcFiles:
    """Loose .mdl/.vvd/.vtx/.vtf/.vmt files dropped in the weapons folder, found by file name."""

    def __init__(self):
        self.paths = {}

    def add(self, path):
        self.paths[Path(path).name.lower()] = Path(path)

    def _key(self, name):
        return name.lower().replace("\\", "/").rsplit("/", 1)[-1]

    def has(self, name):
        return self._key(name) in self.paths

    def read(self, name, start=0, length=None):
        with open(self.paths[self._key(name)], "rb") as f:
            f.seek(start)
            return f.read() if length is None else f.read(length)

    @property
    def files(self):
        return self.paths


class DirSrcFiles:
    """A folder with CS:GO / CS:S files (models/..., materials/...). Also looks for materials in the
    folders above it, so variant folders like V_Models/CSS/models/... find the mod's materials."""

    def __init__(self, root):
        root = Path(root)
        self.roots = [root]
        a = root
        for _ in range(4):
            if a.parent == a:
                break
            a = a.parent
            try:
                if any(c.is_dir() and c.name.lower() == "materials" for c in a.iterdir()):
                    self.roots.append(a)
            except Exception:
                pass
        self.files = {}
        for r in self.roots:
            try:
                subs = [c for c in r.iterdir() if c.is_dir() and c.name.lower() in ("models", "materials")]
            except Exception:
                subs = []
            for sub in subs:
                for dp, _dn, fs in os.walk(sub):
                    for f in fs:
                        q = Path(dp) / f
                        self.files.setdefault(q.relative_to(r).as_posix().lower(), q)

    def _k(self, name):
        return name.lower().replace("\\", "/")

    def has(self, name):
        return self._k(name) in self.files

    def size(self, name):
        return self.files[self._k(name)].stat().st_size

    def read(self, name, start=0, length=None):
        with open(self.files[self._k(name)], "rb") as f:
            f.seek(start)
            return f.read() if length is None else f.read(length)


def src_root(p):
    """Folder that holds models/ and materials/ for a loose model file."""
    p = Path(p)
    for i, part in enumerate(p.parts[:-1]):
        if part.lower() == "models":
            return Path(*p.parts[:i]) if i else p.parent
    return p.parent


class ZipSrcFiles:
    """A .zip with CS:GO files (models/..., materials/...). Names are matched without case."""

    def __init__(self, path):
        import zipfile
        self.zf = zipfile.ZipFile(str(path))
        self.files, self.base, self._info = {}, {}, {}
        for i in self.zf.infolist():
            if i.is_dir():
                continue
            nm = i.filename.replace("\\", "/").lower()
            m = re.search(r"(^|/)(models|materials)/", nm)
            key = nm[m.start() + (1 if m.group(1) else 0):] if m else nm
            self.files[key] = i.file_size
            self._info[key] = i
            self.base.setdefault(key.rsplit("/", 1)[-1], key)

    def _k(self, name):
        n = name.lower().replace("\\", "/")
        return n if n in self.files else self.base.get(n.rsplit("/", 1)[-1])

    def has(self, name):
        return self._k(name) is not None

    def size(self, name):
        return self.files[self._k(name)]

    def read(self, name, start=0, length=None):
        d = self.zf.read(self._info[self._k(name)])
        return d[start:] if length is None else d[start:start + length]


def src_arms(arch):
    """Arm / hand model (not a v_ weapon) inside a Source archive, e.g. models/weapons/t_arms.mdl"""
    for nm in sorted(arch.files):
        b = nm.rsplit("/", 1)[-1]
        if re.match(r"^[^.]+\.mdl$", b) and re.search(r"arms|hands", b) and not b.startswith("v_"):
            return nm
    return None


class SrcSpec:
    def __init__(self, arch, mdl):
        self.arch, self.mdl, self._data = arch, mdl, None

    def __len__(self):
        try:
            return self.arch.size(self.mdl) if hasattr(self.arch, "size") else 1
        except Exception:
            return 1

    def load(self):
        if self._data is None:
            self._data = SrcData(self.arch, self.mdl)
        return self._data


def _kv_float3(s):
    try:
        v = [float(x) for x in re.findall(r"-?[0-9.]+", str(s))[:3]]
        if len(v) == 3:
            return v
    except Exception:
        pass
    return None


class SrcData:
    """Source model + its textures as GL textures (needs the GL context)."""

    _tcache = {}

    def __init__(self, arch, mdl_name):
        self.arch = arch
        self.model = srcm = SrcModel(*_find_vpk_model(arch, mdl_name))
        self.name = mdl_name
        self.seqs = srcm.seqs
        self.parts = []
        for p in srcm.parts:
            tri = np.ascontiguousarray(p["tri"], np.uint32)
            self.parts.append({"idx": tri, "mat": self._material(p["mat"])})
        # CS:S style view-models have the hands built in: no separate arms model then
        self.has_hands = (any(re.search(r"hand|sleeve|glove|finger|^arms?$|_arms?$", t, re.I) for t in srcm.tex_names)
                          or any(re.search(r"/(hands|arms|sleeves|gloves)/", c) for c in srcm.cd))
        self.uv = np.ascontiguousarray(srcm.v_uv, np.float32)

    def _material(self, mat):
        arch = self.arch
        info = {"t": None, "alphatest": False, "blend": False, "add": False, "tint": None}
        nm0 = (self.model.tex_names[self.model.skin[mat]] if 0 <= mat < len(self.model.skin)
               and self.model.skin[mat] < len(self.model.tex_names) else "")
        if re.search(r"hand|finger|palm", nm0, re.I):
            info["fill"] = (.78, .6, .5)           # skin colour when the hand texture is missing
        elif re.search(r"sleeve|arm|glove", nm0, re.I):
            info["fill"] = (.22, .24, .22)
        for cand in self.model.material_paths(mat):
            vmt = "materials/" + cand + ".vmt"
            if not arch.has(vmt):
                continue
            try:
                kv = parse_kv(arch.read(vmt).decode("utf-8", "replace"))
                root = next((v for v in kv.values() if isinstance(v, dict)), {})
            except Exception:
                root = {}
            bt = str(root.get("$basetexture") or "").replace("\\", "/").lower()
            if str(root.get("$alphatest", "0")) not in ("0", ""):
                info["alphatest"] = True
            if str(root.get("$translucent", "0")) not in ("0", ""):
                info["blend"] = True
            if str(root.get("$additive", "0")) not in ("0", ""):
                info["add"] = True
            for tp in ([bt] if bt else []) + [cand]:
                vtf = "materials/" + tp + ".vtf"
                if not arch.has(vtf):
                    continue
                key = (id(arch), vtf)
                if key not in SrcData._tcache:
                    try:
                        rgba, real = load_vtf(arch, vtf, 1024)
                        rgba = np.array(rgba)
                        if not (info["alphatest"] or info["blend"] or info["add"]):
                            rgba[:, :, 3] = 255     # alpha is usually a shine mask, not see-through
                        SrcData._tcache[key] = make_texture(rgba, rgba.shape[1], rgba.shape[0])
                    except Exception as ex:
                        print("Source texture failed:", vtf, ex)
                        SrcData._tcache[key] = None
                info["t"] = SrcData._tcache[key]
                if info["t"]:
                    return info
        return info


class SrcInst:
    """Per-weapon pose state. Same interface the game uses for GoldSrc models (MdlInst)."""
    is_source = True

    def __init__(self, data, arms=None):
        self.d = data
        self.a = arms           # SrcData of the arms model (bone-merged onto the weapon skeleton), or None
        self.m = data.model
        self.seqs = data.seqs
        self.last = None
        self.bt = self.bdur = 0.0
        self.snap = self.snap_cur = None
        self.vtx = None
        self._pos_src = self._pos_val = None
        mm = self.m
        self._p2b = np.stack([b["p2b"] for b in mm.bones])
        self._P = np.concatenate([mm.v_pos, np.ones((len(mm.v_pos), 1))], 1).astype(np.float32)
        nbn = mm.nb
        self._bi = [np.minimum(mm.v_bi[:, k], nbn - 1) for k in range(3)]
        ws = [np.where(mm.v_nb > k, mm.v_w[:, k], 0.0).astype(np.float32) for k in range(3)]
        tot = np.maximum(ws[0] + ws[1] + ws[2], 1e-6)
        self._wk = [w_ / tot for w_ in ws]
        self.base_seq = 0
        self.want_empty = False
        self.avtx = None
        if arms is not None:
            am = arms.model
            wi = {b["name"].lower(): i for i, b in enumerate(mm.bones)}
            self._amap = [wi.get(b["name"].lower(), -1) for b in am.bones]
            self._ap2b = np.stack([b["p2b"] for b in am.bones])
            self._aP = np.concatenate([am.v_pos, np.ones((len(am.v_pos), 1))], 1).astype(np.float32)
            self._abi = [np.minimum(am.v_bi[:, k], am.nb - 1) for k in range(3)]
            aw = [np.where(am.v_nb > k, am.v_w[:, k], 0.0).astype(np.float32) for k in range(3)]
            at = np.maximum(aw[0] + aw[1] + aw[2], 1e-6)
            self._awk = [x / at for x in aw]
            self._albind = []
            for b in am.bones:
                Lb = np.eye(4)
                Lb[:3, :3] = _quat_mat3(b["quat"])
                Lb[:3, 3] = b["pos"]
                self._albind.append(Lb)
        idle = self.find("idle")
        self.pose(idle[0] if idle else 0, 0)

    # ---- sequence lookup (CS:GO v_ model names) ----
    def find(self, kind):
        S = self.seqs
        lab = [s["label"].lower() for s in S]
        ok = [i for i, s in enumerate(S) if s["desc"] is not None]

        def pick(*rxs, bad=None):
            for rx in rxs:
                hit = [i for i in ok if re.search(rx, lab[i]) and not (bad and re.search(bad, lab[i]))]
                if hit:
                    return hit
            return []
        if kind == "idle":
            return pick(r"^idle$", r"^idle(?!_active)", r"idle") or pick(r".")[:1]
        if kind == "draw":
            return pick(r"^(draw|deploy|pullout|ready)$", r"^raise$", r"^(draw|deploy|pullout|raise)(?!_first)",
                        r"^raise") 
        if kind == "shoot":
            return pick(r"^(fire|shoot|attack|primary)\d*$", r"^(fire|shoot|attack)", r"slash|stab|swing|hit",
                        bad=r"last|empty|dry|miss|alt|secondary|zoom")
        if kind == "slash":
            return pick(r"^midslash", r"slash", r"stab_?miss", r"^stab", r"swing", bad=r"back")
        if kind == "reload":
            if self.want_empty:
                hit = pick(r"^reload_?empty", r"^reload")
            else:
                hit = pick(r"^reload$", r"^reload\d*$", bad=r"empty|dry|start|end|insert|loop")
                hit = hit or pick(r"^reload", bad=r"empty")
            return hit[:1]
        return []    # run / walk / jump / die are player-model things

    def is_mirrored(self):
        return False

    # ---- pose ----
    def _frame(self, si, f, loop):
        m = self.m
        s = self.seqs[si] if 0 <= si < len(self.seqs) else None
        if s is None or s["desc"] is None:
            return (np.array([b["pos"] for b in m.bones]), np.array([b["quat"] for b in m.bones], np.float64))
        n = s["n"]
        f0 = max(0, min(int(f), n - 1))
        fr = f - int(f)
        f1 = f0 + 1
        if f1 >= n:
            f1 = 0 if loop else n - 1
        p0, q0 = m.decode_frame(s["desc"], f0)
        if f1 == f0 or fr <= 1e-4:
            return p0, q0
        p1, q1 = m.decode_frame(s["desc"], f1)
        dot = (q0 * q1).sum(1, keepdims=True)
        q1 = np.where(dot < 0, -q1, q1)
        q = q0 + (q1 - q0) * fr
        q = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-9)
        return p0 + (p1 - p0) * fr, q

    def begin_fade(self, dur=FADE_TIME):
        if self.snap_cur is not None:
            self.snap = self.snap_cur
            self.bt = self.bdur = dur

    def tick(self, dt):
        if self.bt > 0:
            self.bt = max(0.0, self.bt - dt)

    def pose(self, si, f, base=-1, bf=0, loop=False):
        key = (si, round(f * 16), loop)
        if key == self.last and self.bt <= 0:
            return
        self.last = key
        pos, quat = self._frame(si, f, loop)
        if self.bt > 0 and self.snap is not None:
            w = 1.0 - self.bt / max(1e-4, self.bdur)
            w = w * w * (3 - 2 * w)
            sp, sq = self.snap
            dot = (quat * sq).sum(1, keepdims=True)
            q2 = np.where(dot < 0, -quat, quat)
            q = sq + (q2 - sq) * w
            quat = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-9)
            pos = sp + (pos - sp) * w
        self.snap_cur = (np.array(pos), np.array(quat))
        self._build(pos, quat)

    def _build(self, pos, quat):
        m = self.m
        nb = m.nb
        x, y, z, w = quat[:, 0], quat[:, 1], quat[:, 2], quat[:, 3]
        L = np.zeros((nb, 4, 4))
        L[:, 0, 0] = 1 - 2 * (y * y + z * z); L[:, 0, 1] = 2 * (x * y - z * w); L[:, 0, 2] = 2 * (x * z + y * w)
        L[:, 1, 0] = 2 * (x * y + z * w); L[:, 1, 1] = 1 - 2 * (x * x + z * z); L[:, 1, 2] = 2 * (y * z - x * w)
        L[:, 2, 0] = 2 * (x * z - y * w); L[:, 2, 1] = 2 * (y * z + x * w); L[:, 2, 2] = 1 - 2 * (x * x + y * y)
        L[:, :3, 3] = pos
        L[:, 3, 3] = 1
        W = L.copy()
        for b in range(nb):
            par = m.bones[b]["par"]
            if 0 <= par < b:
                W[b] = W[par] @ L[b]
        M = np.matmul(W, self._p2b)[:, :3, :].astype(np.float32)        # (nb,3,4)
        out = np.zeros((len(self._P), 3), np.float32)
        for k in range(3):
            out += self._wk[k][:, None] * np.einsum("nij,nj->ni", M[self._bi[k]], self._P)
        self.vtx = out
        if self.a is not None:
            am = self.a.model
            WA = np.empty((am.nb, 4, 4))
            for b in range(am.nb):
                wb = self._amap[b]
                if wb >= 0:                       # same bone name as the weapon: follow the weapon animation
                    WA[b] = W[wb]
                else:
                    par = am.bones[b]["par"]
                    WA[b] = WA[par] @ self._albind[b] if 0 <= par < b else self._albind[b]
            MA = np.matmul(WA, self._ap2b)[:, :3, :].astype(np.float32)
            o2 = np.zeros((len(self._aP), 3), np.float32)
            for k in range(3):
                o2 += self._awk[k][:, None] * np.einsum("nij,nj->ni", MA[self._abi[k]], self._aP)
            self.avtx = o2

    @property
    def pos(self):
        """Vertices without far-away parked parts (spare magazine ...): used for preview framing."""
        v = self.vtx
        if v is None:
            return [None]
        if self._pos_src is not v:
            med = np.median(v, axis=0)
            keep = (np.abs(v - med) < 30).all(axis=1)
            self._pos_src, self._pos_val = v, [np.ascontiguousarray(v[keep] if keep.sum() > 50 else v)]
        return self._pos_val

    @pos.setter
    def pos(self, _):
        pass

    def min_z(self):
        return float(self.vtx[:, 2].min()) if self.vtx is not None else 0.0

    def draw(self):
        if self.vtx is None:
            return
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        self._draw_set(self.d, self.vtx)
        if self.a is not None and self.avtx is not None:
            self._draw_set(self.a, self.avtx)
        tex2d(True)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)

    @staticmethod
    def _draw_set(data, v):
        gl.glVertexPointer(3, gl.GL_FLOAT, 0, v)
        gl.glTexCoordPointer(2, gl.GL_FLOAT, 0, data.uv)
        gl.glColor4f(1, 1, 1, 1)
        order = sorted(data.parts, key=lambda p: (p["mat"]["add"] or p["mat"]["blend"]))
        for p in order:
            mt = p["mat"]
            if mt["t"]:
                gl.glBindTexture(gl.GL_TEXTURE_2D, mt["t"])
                tex2d(True)
            else:
                tex2d(False)
                gl.glColor4f(*mt.get("fill", (.47, .47, .47)), 1)
            if mt["alphatest"]:
                gl.glEnable(gl.GL_ALPHA_TEST)
                gl.glAlphaFunc(gl.GL_GREATER, .5)
            if mt["add"] or mt["blend"]:
                gl.glEnable(gl.GL_BLEND)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE if mt["add"] else gl.GL_ONE_MINUS_SRC_ALPHA)
                gl.glDepthMask(gl.GL_FALSE)
            gl.glDrawElements(gl.GL_TRIANGLES, len(p["idx"]), gl.GL_UNSIGNED_INT, p["idx"])
            gl.glDisable(gl.GL_ALPHA_TEST)
            if mt["add"] or mt["blend"]:
                gl.glDepthMask(gl.GL_TRUE)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                gl.glDisable(gl.GL_BLEND)
            gl.glColor4f(1, 1, 1, 1)


def make_vm(buf, pack=None, arms=None):
    """View-model instance from whatever is stored in game.asset['mdl'] (GoldSrc bytes or a Source spec).
    arms: optional SrcSpec of an arms model, bone-merged onto Source weapons."""
    if isinstance(buf, SrcSpec):
        a = None
        if arms is not None:
            try:
                a = arms.load()
            except Exception as ex:
                print("Arms model failed:", ex)
        data = buf.load()
        return SrcInst(data, None if data.has_hands else a)
    return MdlInst(MdlData(buf, pack))


def src_scan(arch):
    """Names of the weapon view-models inside a Source archive: models/weapons/v_*.mdl"""
    out = []
    for nm in arch.files:
        if nm.endswith(".mdl") and re.match(r"(.*/)?v_[^/]*\.mdl$", nm) and not re.search(r"display|dropped|arms|sleeve|_zoom|_scope", nm):
            out.append(nm)
    return sorted(out)


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------
class Audio:
    def __init__(self):
        self.ok = False
        self.volume = 0.25
        self.shot = None
        self.stereo = False
        try:
            try:
                pygame.mixer.init(22050, -16, 2, 512)  # stereo, so sounds can be panned left / right
                self.stereo = True
            except Exception:
                pygame.mixer.init(22050, -16, 1, 512)
            pygame.mixer.set_num_channels(24)
            n = 6000
            i = np.arange(n)
            d = (np.random.uniform(-1, 1, n) * np.exp(-i / 900) * 32767).astype(np.int16)
            if self.stereo:
                d = np.repeat(d, 2)  # same noise burst in both ears
            self.shot = pygame.mixer.Sound(buffer=d.tobytes())
            self.ok = True
        except Exception as ex:
            print("Audio disabled:", ex)

    def play(self, gain, snd=None, pan=0.0):
        """pan: -1 = fully left, 0 = centre, +1 = fully right (equal-power)."""
        if not self.ok:
            return
        s = snd or self.shot
        v = max(0.0, min(1.0, gain * self.volume))
        try:
            ch = s.play()
            if ch:
                if self.stereo and pan:
                    a = (max(-1.0, min(1.0, pan)) + 1) * math.pi / 4
                    ch.set_volume(v * min(1.0, math.cos(a) * 1.4142), v * min(1.0, math.sin(a) * 1.4142))
                else:
                    ch.set_volume(v)
        except Exception:
            pass



# --------------------------------------------------------------------------
# Online play: peer-hosted rooms with NO port forwarding.
#
#  * Rooms are listed on a free public MQTT broker (the "data broker"): the host
#    publishes its room there, everybody else sees it in the room browser.
#  * A public STUN server tells each player their public ip:port.
#  * To join, the joiner sends the host its public address through the broker and
#    both sides fire UDP packets at each other ("UDP hole punching"), which opens
#    the NAT on both ends. After that the game talks directly peer to peer.
#  * Rooms on the same network are also found by UDP broadcast (works offline).
# No dedicated server, no bots online. Players behind a "symmetric" NAT (some
# mobile / corporate networks) cannot be punched through - see the status text.
# --------------------------------------------------------------------------
NET_DISC_PORT = 27777          # LAN broadcast
NET_GAME_PORT = 27778          # preferred UDP game port (any free one is used if busy)
NET_MAGIC = "AKBARENA2"
NET_PROTO = 2
NET_MAX_PLAYERS = 6
NET_STATE_HZ = 25
NET_DROPPABLE = ("st", "shot")  # sent without resend, newest state wins

BROKER_ROOT = "akbarena-v2"
BROKER_HOSTS = ("broker.hivemq.com", "broker.emqx.io", "test.mosquitto.org")   # free, no account, tried in order
STUN_HOSTS = (("stun.l.google.com", 19302), ("stun1.l.google.com", 19302),
              ("stun.cloudflare.com", 3478), ("stun.nextcloud.com", 3478))
STUN_COOKIE = 0x2112A442


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # routing lookup only, nothing is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class Broker:
    """Free public MQTT broker used as the phone book (room list) and as the signalling
    channel for NAT hole punching. Runs in a background thread; results arrive in self.q."""

    def __init__(self):
        self.cli = None
        self.ok = False
        self.err = ""
        self.q = queue.Queue()
        self.want = set()
        self.thread = None
        self.stop = False

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            import paho.mqtt.client as mqtt
        except Exception:
            self.err = "paho-mqtt missing (pip install paho-mqtt)"
            return
        cid = "akb-%012x" % random.getrandbits(48)
        i = 0
        while not self.stop:
            host = BROKER_HOSTS[i % len(BROKER_HOSTS)]
            i += 1
            try:
                try:
                    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)   # paho 2.x
                except AttributeError:
                    cli = mqtt.Client(client_id=cid)                                      # paho 1.x
                cli.on_connect = self._on_connect
                cli.on_disconnect = self._on_disc
                cli.on_message = self._on_msg
                cli.connect(host, 1883, keepalive=30)
            except Exception as ex:
                self.err = "%s: %s" % (host, str(ex)[:40])
                time.sleep(2.0)
                continue
            self.cli = cli
            self.err = ""
            cli.loop_start()      # keeps running and reconnects by itself
            return

    def _on_connect(self, c, u, flags, rc, *a):
        self.ok = (rc == 0)
        if self.ok:
            self.err = ""
            for t in list(self.want):
                try:
                    c.subscribe(t, qos=0)
                except Exception:
                    pass
        else:
            self.err = "refused (%s)" % (rc,)

    def _on_disc(self, c, u, *a):
        self.ok = False

    def _on_msg(self, c, u, msg):
        try:
            j = json.loads(msg.payload.decode("utf-8", "ignore")) if msg.payload else None
        except ValueError:
            return
        self.q.put((msg.topic, j))

    def sub(self, topic):
        self.want.add(topic)
        if self.cli and self.ok:
            try:
                self.cli.subscribe(topic, qos=0)
            except Exception:
                pass

    def unsub(self, topic):
        self.want.discard(topic)
        if self.cli and self.ok:
            try:
                self.cli.unsubscribe(topic)
            except Exception:
                pass

    def pub(self, topic, obj, retain=False):
        """obj=None with retain=True deletes a retained message."""
        if not (self.cli and self.ok):
            return False
        try:
            self.cli.publish(topic, b"" if obj is None else json.dumps(obj, separators=(",", ":")),
                             qos=0, retain=retain)
            return True
        except Exception:
            return False

    def close(self):
        self.stop = True
        if self.cli:
            try:
                self.cli.disconnect()
                self.cli.loop_stop()
            except Exception:
                pass
            self.cli = None
        self.ok = False


class UConn:
    """A tiny reliable channel on top of a shared UDP socket (what TCP gave us before).
    Reliable messages carry a sequence number, are resent until acknowledged and are
    delivered in order. Droppable ones (player state, shots) are fire-and-forget.
    Until the first packet arrives it talks to every candidate address at once."""
    RTO = 0.2

    def __init__(self, sock, addrs):
        self.s = sock
        self.addrs = list(addrs)
        self.locked = False       # True once we know which address really answers
        self.dead = False
        self.pid = -1
        self.cid = ""             # host side: which joining attempt this is (dedupes multi-path hellos)
        self.last = time.time()
        self.seq = 0              # last reliable seq sent
        self.unacked = {}         # seq -> [packet, last send time]
        self.rseq = 0             # last reliable seq received in order
        self.ooo = {}             # reliable packets that arrived early
        self.inbox = []
        self.t_ping = 0.0

    def accepts(self, addr):
        return addr in self.addrs

    def _raw(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode()
        for a in self.addrs:
            try:
                self.s.sendto(data, a)
            except OSError:
                pass

    def send(self, msg, droppable=False):
        if self.dead:
            return
        if droppable:
            self._raw({"a": self.rseq, "m": msg})
            return
        self.seq += 1
        pkt = {"q": self.seq, "a": self.rseq, "m": msg}
        self.unacked[self.seq] = [pkt, time.time()]
        if len(self.unacked) > 500:     # peer is not acknowledging anything
            self.dead = True
            return
        self._raw(pkt)

    def flush(self):
        if self.dead:
            return
        now = time.time()
        for ent in self.unacked.values():
            if now - ent[1] >= self.RTO:
                ent[1] = now
                ent[0]["a"] = self.rseq
                self._raw(ent[0])
        if now - self.t_ping >= 1.0:    # keepalive: holds the NAT hole open, carries acks
            self.t_ping = now
            self._raw({"a": self.rseq})

    def feed(self, pkt, addr):
        self.last = time.time()
        if not self.locked:
            self.locked = True
            self.addrs = [addr]
        a = pkt.get("a")
        if isinstance(a, int) and self.unacked:
            for q in [q for q in self.unacked if q <= a]:
                del self.unacked[q]
        m = pkt.get("m")
        if not isinstance(m, dict):
            return
        if m.get("t") == "bye":
            self.dead = True
            return
        q = pkt.get("q")
        if q is None:
            self.inbox.append(m)
            return
        if not isinstance(q, int):
            return
        if q == self.rseq + 1:
            self.rseq = q
            self.inbox.append(m)
            while self.rseq + 1 in self.ooo:
                self.rseq += 1
                self.inbox.append(self.ooo.pop(self.rseq))
        elif q > self.rseq + 1 and len(self.ooo) < 300:
            self.ooo[q] = m
        self._raw({"a": self.rseq})     # acknowledge straight away

    def read(self):
        out, self.inbox = self.inbox, []
        return out

    def bye(self):
        for _ in range(3):
            self._raw({"a": self.rseq, "m": {"t": "bye"}})

    def close(self):
        self.dead = True


class Net:
    def __init__(self):
        self.mode = None          # None | "host" | "joining" | "client"
        self.room = ""
        self.rid = ""
        self.my_id = 0
        self.my_team = 0
        self.players = {}         # everyone else: id -> {"n": name, "tm": team}
        self.peers = {}           # host: id -> UConn
        self.pending = []         # host: connections that have not said hello yet
        self.by_addr = {}         # host: source address -> UConn
        self.sock = None          # the one UDP game socket
        self.port = 0
        self.conn = None          # client: link to the host
        self.pub = None           # our public (ip, port) as seen from the internet, via STUN
        self.punch = []           # host: [addr, until, next_send] hole-punch targets
        self.stun_addrs = []
        self.stun_tids = set()
        self.stun_started = False
        self.t_stun = 0.0
        self.t_ann = 0.0
        self.host_t0 = 0.0
        self.pub_warned = False
        self.join_rid = None
        self.join_t0 = 0.0
        self.t_sig = 0.0
        self.cid = "%08x" % random.getrandbits(32)
        self.lan_rooms = {}       # rooms found by UDP broadcast on this network
        self.net_rooms = {}       # rooms found through the internet broker
        self.broker = Broker()
        self.udp = None           # LAN discovery socket
        self.status = ""
        self.t_bc = 0.0
        self.t_st = 0.0
        self.next_id = 1
        self.map_sig = "arena"
        self.pname = "Player"
        self.score_cb = lambda: [0, 0]

    @property
    def active(self):
        return self.mode in ("host", "client")

    @property
    def rooms(self):
        d = dict(self.net_rooms)
        d.update(self.lan_rooms)      # a room seen on both: the direct LAN route wins
        return d

    def start_broker(self):
        self.broker.sub("%s/rooms/+" % BROKER_ROOT)
        self.broker.start()

    def broker_state(self):
        b = self.broker
        if b.ok:
            return "online"
        if b.err:
            return "offline - " + b.err[:36]
        return "connecting..."

    # ----- LAN discovery (UDP broadcast) -----
    def start_discovery(self):
        if self.udp:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", NET_DISC_PORT))
            s.setblocking(False)
            self.udp = s
        except OSError:
            self.udp = None   # fine: the internet room browser still works

    def _disc(self, now):
        if self.udp:
            for _ in range(32):
                try:
                    data, addr = self.udp.recvfrom(2048)
                except (BlockingIOError, InterruptedError, OSError):
                    break
                try:
                    j = json.loads(data.decode("utf-8", "ignore"))
                except ValueError:
                    continue
                if not isinstance(j, dict) or j.get("g") != NET_MAGIC or j.get("id") == self.rid:
                    continue
                try:
                    self.lan_rooms[j["id"]] = {
                        "id": j["id"], "n": str(j.get("n", ""))[:28], "h": str(j.get("h", ""))[:16],
                        "p": int(j.get("p", 1)), "m": int(j.get("m", NET_MAX_PLAYERS)),
                        "ip": addr[0], "port": int(j.get("port", NET_GAME_PORT)),
                        "map": str(j.get("map", "")), "seen": now, "src": "lan", "lan": None,
                    }
                except (KeyError, ValueError, TypeError):
                    continue
            if self.mode == "host" and now - self.t_bc >= 1.0:
                self.t_bc = now
                pkt = json.dumps({"g": NET_MAGIC, "id": self.rid, "n": self.room, "h": self.pname,
                                  "p": len(self.peers) + 1, "m": NET_MAX_PLAYERS, "port": self.port,
                                  "map": self.map_sig}, separators=(",", ":")).encode()
                targets = ["255.255.255.255"]
                ip = lan_ip()
                if ip.count(".") == 3 and not ip.startswith("127."):
                    targets.append(".".join(ip.split(".")[:3] + ["255"]))
                for t in targets:
                    try:
                        self.udp.sendto(pkt, (t, NET_DISC_PORT))
                    except OSError:
                        pass
        for k in [k for k, r in self.lan_rooms.items() if now - r["seen"] > 4.0]:
            del self.lan_rooms[k]
        for k in [k for k, r in self.net_rooms.items() if now - r["seen"] > 15.0]:
            del self.net_rooms[k]

    # ----- internet broker: room list + hole-punch signalling -----
    def _broker_poll(self, now):
        b = self.broker
        for _ in range(200):
            try:
                topic, j = b.q.get_nowait()
            except queue.Empty:
                break
            parts = topic.split("/")
            if len(parts) != 3:
                continue
            kind, rid = parts[1], parts[2]
            if kind == "rooms":
                if j is None:
                    self.net_rooms.pop(rid, None)
                    continue
                if not isinstance(j, dict) or j.get("g") != NET_MAGIC or rid == self.rid:
                    continue
                try:
                    if abs(time.time() - float(j.get("ts", 0))) > 60:
                        continue      # leftover of a room whose host vanished
                    pub = j["pub"]
                    lan = j.get("lan")
                    self.net_rooms[rid] = {
                        "id": rid, "n": str(j.get("n", ""))[:28], "h": str(j.get("h", ""))[:16],
                        "p": int(j.get("p", 1)), "m": int(j.get("m", NET_MAX_PLAYERS)),
                        "ip": str(pub[0]), "port": int(pub[1]), "map": str(j.get("map", "")),
                        "lan": [str(lan[0]), int(lan[1])] if lan else None,
                        "seen": now, "src": "net",
                    }
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
            elif kind == "sig" and self.mode == "host" and rid == self.rid:
                if isinstance(j, dict) and j.get("t") == "punch":
                    self._punch_to(j, now)

    def _same_lan(self, ip):
        mine = lan_ip()
        return mine.count(".") == 3 and ip.count(".") == 3 and mine.rsplit(".", 1)[0] == ip.rsplit(".", 1)[0]

    def _punch_to(self, j, now):
        """A joiner told us (through the broker) where it is: fire packets at it so our NAT
        lets its packets in."""
        try:
            addrs = []
            if j.get("pub"):
                addrs.append((str(j["pub"][0]), int(j["pub"][1])))
            if j.get("lan") and self._same_lan(str(j["lan"][0])):
                addrs.append((str(j["lan"][0]), int(j["lan"][1])))
        except (KeyError, ValueError, TypeError, IndexError):
            return
        for a in addrs:
            for pu in self.punch:
                if pu[0] == a:
                    pu[1] = now + 5.0
                    break
            else:
                self.punch.append([a, now + 5.0, 0.0])

    def _announce(self, now):
        self.t_ann = now
        self.broker.pub("%s/rooms/%s" % (BROKER_ROOT, self.rid), {
            "g": NET_MAGIC, "id": self.rid, "n": self.room, "h": self.pname,
            "p": len(self.peers) + 1, "m": NET_MAX_PLAYERS, "map": self.map_sig,
            "pub": list(self.pub), "lan": [lan_ip(), self.port], "ts": int(time.time()),
        }, retain=True)

    # ----- STUN: learn our public address (no server of our own needed) -----
    def _stun_start(self):
        if self.stun_started:
            return
        self.stun_started = True

        def run():
            for h, p in STUN_HOSTS:
                try:
                    self.stun_addrs.append((socket.gethostbyname(h), p))
                except OSError:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def _stun_tick(self, now):
        if self.pub is None and self.sock and self.stun_addrs and now >= self.t_stun:
            self.t_stun = now + 0.8
            if len(self.stun_tids) > 60:
                self.stun_tids.clear()
            for a in self.stun_addrs[:3]:
                tid = os.urandom(12)
                self.stun_tids.add(tid)
                try:
                    self.sock.sendto(struct.pack(">HHI", 1, 0, STUN_COOKIE) + tid, a)
                except OSError:
                    pass

    def _stun_reply(self, data):
        mt, ln = struct.unpack_from(">HH", data, 0)
        if mt != 0x0101 or data[8:20] not in self.stun_tids:
            return
        p, end = 20, min(len(data), 20 + ln)
        xor = mapped = None
        while p + 4 <= end:
            at, al = struct.unpack_from(">HH", data, p)
            v = data[p + 4:p + 4 + al]
            if at == 0x0020 and al >= 8 and v[1] == 1:
                port = struct.unpack(">H", v[2:4])[0] ^ (STUN_COOKIE >> 16)
                ip = bytes(a ^ b for a, b in zip(v[4:8], struct.pack(">I", STUN_COOKIE)))
                xor = (".".join(str(x) for x in ip), port)
            elif at == 0x0001 and al >= 8 and v[1] == 1:
                mapped = (".".join(str(x) for x in v[4:8]), struct.unpack(">H", v[2:4])[0])
            p += 4 + al + (-al % 4)
        self.pub = xor or mapped
        if self.pub and self.mode == "host":
            self.t_ann = 0.0
            self.status = "Hosting '%s' - friends can join from the room browser, no port forwarding needed." % self.room

    # ----- sockets -----
    def _open_udp(self, port=0, tries=1):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for p in range(port, port + tries):
            try:
                s.bind(("", p))
                break
            except OSError:
                continue
        else:
            s.close()
            return None
        s.setblocking(False)
        if os.name == "nt":
            try:
                s.ioctl(0x9800000C, False)     # SIO_UDP_CONNRESET: ignore ICMP "port unreachable" errors
            except Exception:
                pass
        return s

    def _pump(self, now):
        """Read every waiting datagram and hand it to the right connection."""
        for _ in range(512):
            try:
                data, addr = self.sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                if self.sock is None or self.sock.fileno() < 0:
                    break
                continue
            if len(data) >= 20 and data[4:8] == struct.pack(">I", STUN_COOKIE):
                self._stun_reply(data)
                continue
            try:
                pkt = json.loads(data)
            except ValueError:
                continue
            if not isinstance(pkt, dict) or "p" in pkt:     # "p" = hole-punch noise
                continue
            c = None
            if self.mode == "host":
                c = self.by_addr.get(addr)
                if c is None:
                    m = pkt.get("m")
                    if (isinstance(m, dict) and m.get("t") == "hello" and pkt.get("q") == 1
                            and len(self.pending) < 16):
                        c = UConn(self.sock, [addr])
                        c.locked = True
                        self.by_addr[addr] = c
                        self.pending.append(c)
            elif self.conn is not None and self.conn.accepts(addr):
                c = self.conn
            if c is not None:
                c.feed(pkt, addr)

    # ----- host / join / leave -----
    def host(self, room, pname, map_sig, team=0):
        self.leave()
        self.start_discovery()
        sock = self._open_udp(NET_GAME_PORT, 20)
        if sock is None:
            self.status = "Could not open a network port for the room."
            return False
        self.sock = sock
        self.port = sock.getsockname()[1]
        self.mode = "host"
        self.room = room
        self.rid = "%08x" % random.getrandbits(32)
        self.my_id = 0
        self.my_team = team
        self.players, self.peers, self.pending, self.by_addr, self.punch = {}, {}, [], {}, []
        self.next_id = 1
        self.map_sig, self.pname = map_sig, pname
        self.pub = None
        self.pub_warned = False
        self.host_t0 = time.time()
        self.t_ann = 0.0
        self.t_stun = 0.0
        self._stun_start()
        self.broker.sub("%s/sig/%s" % (BROKER_ROOT, self.rid))
        self.status = "Hosting '%s' - finding your public address..." % room
        return True

    @staticmethod
    def room_cands(r):
        """Addresses to try for a room-browser entry."""
        out = []
        if r.get("src") == "lan":
            out.append((r["ip"], r["port"]))
        else:
            lan = r.get("lan")
            mine = lan_ip()
            if lan and mine.count(".") == 3 and lan[0].rsplit(".", 1)[0] == mine.rsplit(".", 1)[0]:
                out.append((lan[0], lan[1]))
            out.append((r["ip"], r["port"]))
        return out

    def join_room(self, r, pname, map_sig):
        return self.join(self.room_cands(r), r["id"] if r.get("src") == "net" else None, pname, map_sig)

    def join(self, cands, rid, pname, map_sig):
        self.leave()
        self.start_discovery()
        sock = self._open_udp()
        if sock is None:
            self.status = "Cannot connect: no free network port."
            return False
        self.sock = sock
        self.port = sock.getsockname()[1]
        self.mode = "joining"
        self.pname, self.map_sig = pname, map_sig
        self.pub = None
        self.join_rid = rid
        self.join_t0 = time.time()
        self.t_sig = 0.0
        self.t_stun = 0.0
        self._stun_start()
        self.cid = "%08x" % random.getrandbits(32)      # new for every attempt
        self.conn = UConn(sock, cands)
        self.conn.send({"t": "hello", "g": NET_MAGIC, "v": NET_PROTO, "n": pname, "map": map_sig,
                        "cid": self.cid})
        self.status = "Connecting to the room ..."
        return True

    def leave(self, status=None):
        if self.mode == "host" and self.rid:
            self.broker.pub("%s/rooms/%s" % (BROKER_ROOT, self.rid), None, retain=True)
            self.broker.unsub("%s/sig/%s" % (BROKER_ROOT, self.rid))
        for c in list(self.peers.values()):
            c.bye()
        if self.conn:
            self.conn.bye()
        for c in list(self.peers.values()) + list(self.pending):
            c.close()
        if self.conn:
            self.conn.close()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = self.conn = None
        self.peers, self.pending, self.by_addr, self.punch, self.players = {}, [], {}, [], {}
        self.mode = None
        self.rid = ""
        self.my_id = 0
        self.pub = None
        self.join_rid = None
        if status is not None:
            self.status = status

    def close(self):
        self.leave()
        self.broker.close()
        if self.udp:
            try:
                self.udp.close()
            except OSError:
                pass
            self.udp = None

    # ----- send -----
    def send(self, m, droppable=False):
        """Broadcast to the room, or to one player when m has "to"."""
        if self.mode == "host":
            m["f"] = 0
            to = m.get("to")
            if to is None:
                for c in self.peers.values():
                    c.send(m, droppable)
            else:
                c = self.peers.get(to)
                if c:
                    c.send(m, droppable)
        elif self.mode == "client" and self.conn:
            self.conn.send(m, droppable)

    # ----- receive -----
    def _team_for_new(self):
        cnt = [0, 0]
        cnt[self.my_team] += 1
        for p in self.players.values():
            cnt[p["tm"] if p["tm"] in (0, 1) else 0] += 1
        return 0 if cnt[0] < cnt[1] else 1

    def _forget(self, c):
        if c.addrs:
            self.by_addr.pop(c.addrs[0], None)
        if c in self.pending:
            self.pending.remove(c)

    def _drop(self, c, msgs, why=""):
        c.bye()
        c.close()
        self._forget(c)
        if c.pid in self.peers:
            del self.peers[c.pid]
            self.players.pop(c.pid, None)
            m = {"t": "leave", "id": c.pid, "f": 0}
            for o in self.peers.values():
                o.send(m)
            msgs.append(m)

    def poll(self, now):
        msgs = []
        self._disc(now)
        self._broker_poll(now)
        if self.sock:
            self._pump(now)
            self._stun_tick(now)
        if self.mode == "host":
            self._poll_host(now, msgs)
        elif self.mode == "joining":
            self._poll_joining(now, msgs)
        elif self.mode == "client":
            self._poll_client(now, msgs)
        return msgs

    def _poll_host(self, now, msgs):
        if self.pub and now - self.t_ann >= 5.0:
            self._announce(now)
        if not self.pub and not self.pub_warned and now - self.host_t0 > 6.0:
            self.pub_warned = True
            self.status = ("Hosting '%s' - could not find your public address (is UDP blocked?). "
                           "Only players on your network can join." % self.room)
        for pu in self.punch[:]:
            if now > pu[1]:
                self.punch.remove(pu)
            elif now >= pu[2]:
                pu[2] = now + 0.15
                try:
                    self.sock.sendto(b'{"p":1}', pu[0])
                except OSError:
                    pass
        for c in list(self.pending):
            for m in c.read():
                if m.get("t") != "hello":
                    continue
                name = str(m.get("n", "Player"))[:16] or "Player"
                cid = str(m.get("cid", ""))[:16]
                if cid and any(p.cid == cid for p in self.peers.values()):
                    self._forget(c)     # same joiner reached us over a second route: keep the first
                    c.close()
                    break
                why = None
                if m.get("v") != NET_PROTO or m.get("g") != NET_MAGIC:
                    why = "Game version mismatch."
                elif len(self.peers) + 1 >= NET_MAX_PLAYERS:
                    why = "Room is full."
                elif m.get("map") != self.map_sig:
                    why = f"Different map. The host uses '{self.map_sig}', you have '{m.get('map')}'. Load the same map first."
                if why:
                    c.send({"t": "deny", "why": why})
                    for _ in range(2):
                        c.unacked and c._raw(list(c.unacked.values())[-1][0])
                    self._forget(c)
                    c.close()
                else:
                    pid = self.next_id
                    self.next_id += 1
                    tm = self._team_for_new()
                    roster = [{"id": 0, "n": self.pname, "tm": self.my_team}] + \
                             [{"id": i, "n": p["n"], "tm": p["tm"]} for i, p in self.players.items()]
                    c.pid = pid
                    c.cid = cid
                    c.send({"t": "welcome", "id": pid, "team": tm, "ps": roster, "room": self.room,
                            "sc": self.score_cb(), "frags": CFG["frags"]})
                    j = {"t": "join", "id": pid, "n": name, "tm": tm, "f": 0}
                    for o in self.peers.values():
                        o.send(j)
                    self.pending.remove(c)
                    self.peers[pid] = c
                    self.players[pid] = {"n": name, "tm": tm}
                    msgs.append(j)
                break
            if c in self.pending:
                if c.dead or now - c.last > 6:
                    self._forget(c)
                    c.close()
                else:
                    c.flush()
        for c in list(self.peers.values()):
            for m in c.read():
                t = m.get("t")
                if t in ("hello", "welcome", "join", "leave", "deny"):
                    continue
                m["f"] = c.pid
                to = m.get("to")
                if to is not None and to != 0:
                    o = self.peers.get(to)
                    if o:
                        o.send(m, droppable=(t in NET_DROPPABLE))
                    continue
                if to is None:
                    for o in self.peers.values():
                        if o is not c:
                            o.send(m, droppable=(t in NET_DROPPABLE))
                msgs.append(m)
            c.flush()
            if c.dead or now - c.last > 10:
                self._drop(c, msgs)

    def _poll_joining(self, now, msgs):
        c = self.conn
        if self.join_rid and now - self.t_sig >= 1.5 and (self.pub or now - self.join_t0 > 2.5):
            # tell the host (via the broker) where to punch through to us
            self.t_sig = now
            self.broker.pub("%s/sig/%s" % (BROKER_ROOT, self.join_rid), {
                "t": "punch", "cid": self.cid, "n": self.pname,
                "pub": list(self.pub) if self.pub else None, "lan": [lan_ip(), self.port]})
        c.flush()
        for m in c.read():
            t = m.get("t")
            if t == "welcome":
                self.mode = "client"
                self.my_id = int(m.get("id", 1))
                self.my_team = int(m.get("team", 1)) & 1
                self.room = str(m.get("room", ""))
                self.players = {int(p["id"]): {"n": str(p["n"])[:16], "tm": int(p["tm"]) & 1}
                                for p in m.get("ps", [])}
                CFG["frags"] = int(m.get("frags", CFG["frags"]))
                self.status = f"Joined '{self.room}'."
                msgs.append(m)
            elif t == "deny":
                self.leave("Denied: " + str(m.get("why", "Denied.")))
                msgs.append({"t": "failed"})
                return
        if self.mode == "joining" and (c.dead or now - self.join_t0 > 12.0):
            self.leave("Could not reach the room. The host may have left, or one of your networks "
                       "blocks direct connections (try another network or hotspot).")
            msgs.append({"t": "failed"})

    def _poll_client(self, now, msgs):
        c = self.conn
        for m in c.read():
            t = m.get("t")
            if t == "join":
                self.players[int(m["id"])] = {"n": str(m.get("n", "?"))[:16], "tm": int(m.get("tm", 0)) & 1}
            elif t == "leave":
                self.players.pop(int(m.get("id", -1)), None)
            elif t in ("welcome", "deny"):
                continue
            msgs.append(m)
        c.flush()
        if c.dead or now - c.last > 10:
            self.leave("Lost connection to the host.")
            msgs.append({"t": "closed", "why": "Lost connection to the host."})


NET = Net()


# --------------------------------------------------------------------------
# Game state / logic
# --------------------------------------------------------------------------
class Ent:
    def __init__(self, team, is_p, name):
        self.team = team
        self.isP = is_p
        self.name = name
        self.hp = 100
        self.regen_delay = 5.0  # seconds without taking damage before health regeneration
        self.took_damage_this_frame = False
        self.alive = 1
        self.x = self.y = self.z = 0.0
        self.vx = self.vy = self.vz = 0.0
        self.px = self.py = self.pz = 0.0
        self.yaw = self.pitch = 0.0
        self.w = 0
        self.ammo = [w.mag for w in CFG["weapons"]]
        self.cool = 0.0
        self.resp = 0.0
        self.rt = random.random() * .25   # staggered so bots don't all think on the same frame
        self.aacc = 0.0                   # animation time accumulator (animations are throttled)
        self.pun_p = self.pun_y = 0.0     # recoil punch (radians) added to the view / bullet direction
        self.pend_p = self.pend_y = 0.0   # spray recoil still to be applied to the aim
        self.spray = 0.0                  # bullets fired in the current spray
        self.since = 9.0                  # seconds since the last spray shot
        self.sprayw = False               # recoil currently follows the spray pattern
        self.turn = 0.0
        self.dev = 0.0
        self.str = 1
        self.aim = 0.0
        self.bl = 0
        self.see = 0
        self.tgt = None
        self.zoom = 0
        self.acc = 0.0
        self.jreq = 0
        self.sl = 0.0
        self.scd = 0.0
        self.so = 0.0
        self.cr = 0.0
        self.wj = 0
        self.cHeld = False
        self.aS = 0
        self.sdx = self.sdy = 0.0
        self.sdyaw = None
        self.held = 0
        self.kills = 0
        self.deaths = 0
        self.walk = False  # standing on walkable ground
        self.steep = False  # touching a too-steep slope (you just slide down it)
        self.shootT = 0.0
        self.reloadT = 0.0
        self.remote = False     # online player driven by network state
        self.nid = -1
        self.tx = self.ty = self.tz = None
        self.tyaw = self.tpitch = 0.0
        self.ph = 0.0   # run-cycle phase
        self.bob = 0.0  # run-cycle strength 0..1
        self.air = 0.0  # jump pose strength 0..1
        self.land = 0.0  # landing squash 0..1
        self.pvz = 0.0
        self.was_air = False
        self.pm = None
        self.pm_dz = 0.0
        self.anim = fresh_anim()


class Game:
    def __init__(self, audio):
        self.audio = audio
        self.texdb = {}
        self.files = {"bsp": None, "wads": []}
        self.asset = {"mdl": {}, "pack": {}, "wav": {}, "player": {}}
        self.src_loose = LooseSrcFiles()
        self.src_dirs = {}
        self.src_arms = None      # SrcSpec of the arms model (hands holding the CS:GO weapon)
        self.arms_all = {}        # name -> SrcSpec of every arms model loaded
        self.arms_pick = None     # name of the equipped arms
        self.arms_want = None     # name remembered from settings
        self.arms_off = False     # "No arms" chosen
        self.pm_cfg = {"CT": None, "T": None}
        self.wm_pick = {}  # weapon slot index -> chosen view-model filename (Loadout tab)
        self.pm_saved = {"CT": None, "T": None}  # remembered team models (from settings)
        self.pm_cache = {}
        self.vm_msg = ""
        self.world = None
        self.ents = []
        self.P = None
        self.score = [0, 0]
        self.feed = []
        self.tracers = []
        self.over = 0.0
        self.keys = {}
        self.binds = dict(DEFAULT_BINDS)
        self.hand_swap = False
        self.bots_on = True
        self.net = NET
        self.net.score_cb = lambda: self.score
        self.remotes = {}
        self.my_team = 0
        self.net_notice = ""
        self.net_joined = False
        self._bot_names = []
        self.aspect = 0.0  # 0 = match the window; else forced aspect (stretched to fill)
        self.vm_adj = [0.0, 0.0, 0.0]  # viewmodel x (forward), y (right), z (up)
        self.custom_shot = self.custom_hit = None
        self.custom_shot_path = self.custom_hit_path = ""
        self.sens = BASE_SENS
        self.lock = False
        self.flash = 0.0
        self.fov = 75.0
        self.death_killer = "UNKNOWN"
        self.player_name = "Player"
        self.streak = 0
        self.xh = dict(DEFAULT_XH)   # crosshair settings
        self.vmA = {"vm": None, "w": -1, "s": 0, "t": 0.0, "loop": True}

    # ----- setup -----
    def set_world(self, w):
        self.world = w
        self.reset_game()

    def mk_ent(self, team, is_p, i=0):
        if is_p:
            name = self.player_name or "Player"
        else:
            name = self._bot_names.pop() if self._bot_names else ("T" if team else "CT") + "-" + str(i)
        e = Ent(team, is_p, name)
        if not is_p:
            self.build_entity_model(e)
        return e

    def build_entity_model(self, e):
        e.pm = None
        if e.isP:
            return
        name = self.pm_cfg["CT"] if e.team == 0 else self.pm_cfg["T"]
        if name and name in self.asset["player"]:
            try:
                data = self.pm_cache.get(name)
                if data is None:
                    pack = self.asset["pack"].get(key_norm(re.sub(r"\.mdl$", "", name)))
                    data = self.pm_cache[name] = MdlData(self.asset["player"][name], pack)
                pm = MdlInst(data)
                idle = pm.find("idle")
                if idle:
                    seq = idle[0]
                else:
                    valid = [i for i, s in enumerate(pm.seqs) if not re.search(r"crouch|die|death", s["label"], re.I)]
                    seq = valid[0] if valid else 0
                pm.last = None
                pm.pose(seq, 0, pm.base_seq, 0)
                e.pm_dz = -(self.world.feet_offset + pm.min_z())
                e.pm = pm
                e.anim = fresh_anim()
            except Exception as ex:
                print("Player model error:", ex)
                e.pm = None

    def spawn(self, e):
        lst = self.world.spawns[e.team]
        s = random.choice(lst)
        e.x, e.y, e.z = s[0], s[1], s[2]
        n = 0
        while self.world.solid(e.x, e.y, e.z) and n < 80:
            e.z += 4
            n += 1
        e.vx = e.vy = e.vz = 0.0
        e.px, e.py, e.pz = e.x, e.y, e.z
        e.acc = 0.0
        e.jreq = 0
        e.sl = 0.0
        e.scd = 0.0
        e.so = 0.0
        e.cr = 0.0
        e.hp = 200 if e.isP else 100   # the player secretly has double health (HUD still shows 100)
        e.regen_delay = 5.0
        e.alive = 1
        e.yaw = -math.pi / 2 if e.team else math.pi / 2
        e.pitch = 0.0
        e.ammo = [w.mag for w in CFG["weapons"]]
        e.w = 1
        e.cool = 0.0
        e.zoom = 0
        e.pun_p = e.pun_y = 0.0
        e.spray = 0.0
        e.pend_p = e.pend_y = 0.0
        e.sprayw = False
        if e.isP:
            self.vmA["vm"] = None   # restart the viewmodel (draw animation) on every spawn
        e.anim = fresh_anim()
        e.shootT = 0.0
        e.reloadT = 0.0

    def reset_game(self):
        self.ents = []
        pool = list(BOT_NAMES)
        random.shuffle(pool)
        total = 2 * CFG["bots"] + 1
        pool = [("Bot " + n) for n in pool]
        while len(pool) < total:  # more bots than names: reuse them with a number
            pool += [f"{n} {len(pool) // len(BOT_NAMES) + 1}" for n in pool[:len(BOT_NAMES)]]
        self._bot_names = pool[:total][::-1]
        online = self.net.active
        self.P = self.mk_ent(self.my_team if online else 0, True)
        self.ents.append(self.P)
        self.remotes = {}
        if online:
            for pid, p in self.net.players.items():
                self.mk_remote(pid, p)
        if self.bots_on and not online:   # no bots when playing online
            for i in range(CFG["bots"]):
                self.ents.append(self.mk_ent(0, False, i + 1))
            for i in range(CFG["bots"] + 1):
                self.ents.append(self.mk_ent(1, False, i + 1))
        for e in self.ents:
            self.spawn(e)
        self.score = [0, 0]
        self.feed = []
        self.over = 0.0
        self.streak = 0

    # ----- online -----
    def map_sig(self):
        b = self.files.get("bsp")
        return hashlib.sha1(b).hexdigest()[:10] if b else "arena"

    def mk_remote(self, pid, p):
        e = Ent(p["tm"], False, p["n"])
        e.remote = True
        e.nid = pid
        self.build_entity_model(e)
        self.spawn(e)
        self.ents.append(e)
        self.remotes[pid] = e
        return e

    def net_host(self, room):
        self.my_team = 0
        if not self.net.host(room, self.player_name, self.map_sig(), 0):
            return False
        self.reset_game()
        return True

    def net_join(self, room, rid=None):
        """room: a room-browser entry (dict), or a list of (ip, port) addresses to try."""
        if isinstance(room, dict):
            return self.net.join_room(room, self.player_name, self.map_sig())
        return self.net.join(room, rid, self.player_name, self.map_sig())

    def net_leave(self, status=None):
        self.net.leave(status)
        self.my_team = 0
        self.reset_game()

    def ent_by_id(self, pid):
        return self.P if pid == self.net.my_id else self.remotes.get(pid)

    def net_update(self, dt):
        net = self.net
        for m in net.poll(time.time()):
            try:
                self.net_msg(m)
            except Exception as ex:
                print("net message error:", ex)
        if net.active and self.P:
            net.t_st += dt
            if net.t_st >= 1.0 / NET_STATE_HZ:
                net.t_st = 0.0
                P = self.P
                net.send({"t": "st", "s": [
                    round(P.x, 1), round(P.y, 1), round(P.z, 1),
                    round(P.vx), round(P.vy), round(P.vz),
                    round(P.yaw, 3), round(P.pitch, 3), P.w, round(P.cr, 2),
                    1 if P.sl > 0 else 0, 1 if P.alive else 0, 1 if P.reloadT > 0 else 0]}, droppable=True)

    def net_msg(self, m):
        net = self.net
        t, f = m.get("t"), m.get("f")
        if t == "welcome":
            self.my_team = net.my_team
            self.reset_game()
            self.score = [int(x) for x in m.get("sc", [0, 0])][:2] or [0, 0]
            self.net_joined = True
        elif t in ("failed", "closed"):
            if t == "closed":
                self.reset_game()
            self.net_notice = net.status or m.get("why", "")
        elif t == "join":
            pid = int(m["id"])
            if pid not in self.remotes:
                self.mk_remote(pid, net.players.get(pid, {"n": m.get("n", "?"), "tm": m.get("tm", 0)}))
        elif t == "leave":
            e = self.remotes.pop(int(m.get("id", -1)), None)
            if e in self.ents:
                self.ents.remove(e)
        elif t == "st":
            e = self.remotes.get(f)
            s = m.get("s")
            if not e or not s or len(s) < 13:
                return
            x, y, z, vx, vy, vz, yaw, pt, w, cr, sl, al, rl = s[:13]
            if al and not e.alive:
                e.alive = 1
                e.hp = 100
                e.anim = fresh_anim()
                e.tx = None
            if not e.alive:
                return
            if e.tx is None or abs(x - e.x) > 300 or abs(y - e.y) > 300 or abs(z - e.z) > 300:
                e.x, e.y, e.z = x, y, z
                e.yaw = yaw
                e.pitch = pt
            e.tx, e.ty, e.tz = x, y, z
            e.vx, e.vy, e.vz = vx, vy, vz
            e.tyaw, e.tpitch = yaw, pt
            e.w = max(0, min(len(CFG["weapons"]) - 1, int(w)))
            e.cr = cr
            e.sl = 1.0 if sl else 0.0
            if rl and e.reloadT <= 0:
                e.reloadT = 1.6
        elif t == "shot":
            e = self.remotes.get(f)
            if not e or not e.alive:
                return
            e.w = max(0, min(len(CFG["weapons"]) - 1, int(m.get("w", e.w))))
            w = CFG["weapons"][e.w]
            o, b = m.get("o"), m.get("e")
            if not o or not b:
                return
            e.shootT = .15
            if not w.melee:
                self.tracers.append({"a": (o[0], o[1], o[2] - 2), "b": tuple(b), "al": .5, "t": .022})
            if not w.melee or w.snd:
                g, pan = self.spatial(o[0], o[1], o[2], .55)
                snd = w.snd
                if not w.melee and self.custom_shot:
                    snd = self.custom_shot
                if g > .004:
                    self.audio.play(g, snd, pan)
        elif t == "hit":
            P = self.P
            if P.alive and not self.over:
                damage = max(0.0, float(m.get("d", 0)))
                P.hp -= damage
                if damage > 0:
                    P.regen_delay = 5.0
                    P.took_damage_this_frame = True
                if P.hp <= 0:
                    k = self.remotes.get(f) or P
                    ow = k.w
                    k.w = max(0, min(len(CFG["weapons"]) - 1, int(m.get("w", k.w))))
                    self.kill(P, k)
                    k.w = ow
        elif t == "kill":
            k, v = self.ent_by_id(m.get("k")), self.ent_by_id(m.get("v"))
            if k and v and v.alive:
                ow = k.w
                k.w = max(0, min(len(CFG["weapons"]) - 1, int(m.get("w", k.w))))
                self.kill(v, k)
                k.w = ow

    def airborne(self, e):
        """True when the entity really is in the air. Checks a few units below the feet (a hair above
        the floor still counts as standing) and, for online players, the position the network says
        they are at, not the smoothed one that trails behind it by a fraction of a unit."""
        solid = self.world.solid
        if e.remote and e.tx is not None:
            x, y, z = e.tx, e.ty, e.tz
        else:
            x, y, z = e.x, e.y, e.z
        return not (solid(x, y, z - 1) or solid(x, y, z - 3))

    def remote_tick(self, e, dt):
        if not e.alive or e.tx is None:
            return
        k = min(1.0, dt * 16)
        e.x += (e.tx - e.x) * k
        e.y += (e.ty - e.y) * k
        e.z += (e.tz - e.z) * k
        if abs(e.tz - e.z) < 1.5:
            e.z = e.tz          # snap: never hover a hair above the floor
        e.yaw += math.atan2(math.sin(e.tyaw - e.yaw), math.cos(e.tyaw - e.yaw)) * min(1.0, dt * 20)
        e.pitch += (e.tpitch - e.pitch) * min(1.0, dt * 20)

    # ----- physics -----
    def phys(self, e, dt, vx, vy, jump):
        world = self.world
        gr = world.solid(e.x, e.y, e.z - 1)
        bl = 0
        if jump and gr:
            e.vz = 270
        elif not gr:
            e.vz -= 800 * dt
        elif e.vz < 0:
            e.vz = 0
        stick = gr and e.vz <= 0  # walking on the ground (not leaving it by jumping)
        nx = e.x + vx * dt
        if not world.solid(nx, e.y, e.z):
            e.x = nx
        else:
            h = self.step_h(nx, e.y, e.z) if stick else 0
            if h:
                e.x = nx
                e.z += h
            elif vx:
                bl = 1
        ny = e.y + vy * dt
        if not world.solid(e.x, ny, e.z):
            e.y = ny
        else:
            h = self.step_h(e.x, ny, e.z) if stick else 0
            if h:
                e.y = ny
                e.z += h
            elif vy:
                bl = 1
        if stick and not world.solid(e.x, e.y, e.z - 1):
            # walked off a slope / small drop: stay glued to the ground instead of
            # going airborne (which killed friction and made slopes bounce)
            for d in range(2, STEP_H + 1):
                if world.solid(e.x, e.y, e.z - d):
                    e.z -= d - 1
                    break
        nz = e.z + e.vz * dt
        if world.solid(e.x, e.y, nz):
            if e.vz < 0:
                z = e.z
                while z - 1 > nz and not world.solid(e.x, e.y, z - 1):
                    z -= 1
                e.z = z
            e.vz = 0
        else:
            e.z = nz
        return bl

    def step_h(self, x, y, z, fx=None, fy=None):
        """Smallest height (1..STEP_H) we must rise to stand at (x, y); 0 if blocked.
        Rising only as much as needed makes slopes and stairs smooth. Steep surfaces
        (too steep to walk) are never stepped onto - those you slide / surf on."""
        world = self.world
        solid = world.solid
        for h in range(1, STEP_H + 1):
            if fx is not None and solid(fx, fy, z + h):
                return 0  # ceiling above where we stand
            if not solid(x, y, z + h):
                n = world.normal_at(x, y, z + h, True)
                if n is not None and n[2] < SURF_NZ:
                    return 0
                return h
        return 0

    @staticmethod
    def clip_vel(v, n):
        b = v[0] * n[0] + v[1] * n[1] + v[2] * n[2]
        if b >= 0:
            return v
        o = (v[0] - n[0] * b, v[1] - n[1] * b, v[2] - n[2] * b)
        a = o[0] * n[0] + o[1] * n[1] + o[2] * n[2]
        if a < 0:
            o = (o[0] - n[0] * a, o[1] - n[1] * a, o[2] - n[2] * a)
        return o

    def floor_n(self, e):
        """Normal of the up-facing surface right under the feet, None when airborne."""
        world = self.world
        if not world.solid(e.x, e.y, e.z - 1):
            return None
        return world.normal_at(e.x, e.y, e.z, True) or (0.0, 0.0, 1.0)

    def walkable(self, e):
        """On ground gentle enough to walk on (steeper surfaces count as air: you surf)."""
        if e.vz > 0:
            return False
        n = self.floor_n(e)
        return n is not None and n[2] >= SURF_NZ

    def pm_move(self, e, dt):
        """Player move: gravity, then a swept move that slides along whatever blocks it.
        Gentle slopes / stairs are stepped; slopes too steep to walk just slide you
        down and off them (no sideways momentum kept, no air control)."""
        world = self.world
        solid = world.solid
        walk = e.walk
        e.steep = False
        if walk:
            if e.vz < 0:
                e.vz = 0.0
        else:
            e.vz -= 800 * dt
        ov = (e.vx, e.vy, e.vz)
        sp = math.sqrt(ov[0] * ov[0] + ov[1] * ov[1] + ov[2] * ov[2])
        if sp > MAXV:  # engine speed limit, like sv_maxvelocity
            k = MAXV / sp
            e.vx, e.vy, e.vz = ov = (ov[0] * k, ov[1] * k, ov[2] * k)
        planes = []
        t = dt
        for _ in range(4):
            if t <= 1e-7:
                break
            if solid(e.x, e.y, e.z):  # stuck inside something: pop up out of it
                for h in range(1, 129):
                    if not solid(e.x, e.y, e.z + h):
                        e.z += h
                        break
                else:
                    e.vx = e.vy = e.vz = 0.0
                    break
            dx, dy, dz = e.vx * t, e.vy * t, e.vz * t
            if dx == 0 and dy == 0 and dz == 0:
                break
            nx, ny, nz = e.x + dx, e.y + dy, e.z + dz
            if not solid(nx, ny, nz):
                e.x, e.y, e.z = nx, ny, nz
                break
            lo, hi = 0.0, 1.0
            for _i in range(10):
                mid = (lo + hi) * .5
                if solid(e.x + dx * mid, e.y + dy * mid, e.z + dz * mid):
                    hi = mid
                else:
                    lo = mid
            e.x += dx * lo
            e.y += dy * lo
            e.z += dz * lo
            t *= 1 - lo
            if walk:  # stairs and gentle slopes: rise just enough
                tx, ty = e.x + e.vx * t, e.y + e.vy * t
                h = self.step_h(tx, ty, e.z, e.x, e.y)
                if h:
                    e.x, e.y, e.z = tx, ty, e.z + h
                    break
            n = world.normal_at(e.x, e.y, e.z, False, (dx, dy, dz))
            if n is None:
                e.vx = e.vy = e.vz = 0.0
                break
            if walk and 0 < n[2] < SURF_NZ:
                # on the ground, a steep slope acts like a wall: slide along its base,
                # never up it
                hl = math.hypot(n[0], n[1])
                if hl > 1e-6:
                    n = (n[0] / hl, n[1] / hl, 0.0)
            planes.append(n)
            v = None
            for i, pl in enumerate(planes):
                v2 = self.clip_vel(ov, pl)
                if all(v2[0] * q[0] + v2[1] * q[1] + v2[2] * q[2] >= -.01
                       for j, q in enumerate(planes) if j != i):
                    v = v2
                    break
            if v is None:
                if len(planes) == 2:  # crease between two surfaces: slide along it
                    a, b = planes
                    d = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
                    l = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
                    if l > 1e-6:
                        k = (d[0] * ov[0] + d[1] * ov[1] + d[2] * ov[2]) / (l * l)
                        v = (d[0] * k, d[1] * k, d[2] * k)
                if v is None:
                    v = (0.0, 0.0, 0.0)
                    e.vx = e.vy = e.vz = 0.0
                    break
            if not walk and len(planes) == 1 and .05 < planes[0][2] < SURF_NZ:
                # too steep to stand on: only the downhill part of the motion survives
                e.steep = True
                q = planes[0]
                dd = (q[0] * q[2], q[1] * q[2], q[2] * q[2] - 1.0)
                dl = math.sqrt(dd[0] * dd[0] + dd[1] * dd[1] + dd[2] * dd[2]) or 1.0
                dd = (dd[0] / dl, dd[1] / dl, dd[2] / dl)
                k = max(0.0, v[0] * dd[0] + v[1] * dd[1] + v[2] * dd[2])
                v = (dd[0] * k, dd[1] * k, dd[2] * k)
            if v[0] * ov[0] + v[1] * ov[1] + v[2] * ov[2] <= 0:
                e.vx = e.vy = e.vz = 0.0
                break
            e.vx, e.vy, e.vz = v
        if walk and e.vz <= 0 and not solid(e.x, e.y, e.z - 1):
            # walked off a slope / small drop: stay on the ground instead of going airborne
            for d in range(2, STEP_H + 1):
                if solid(e.x, e.y, e.z - d):
                    zz = e.z - (d - 1)
                    n = world.normal_at(e.x, e.y, zz, True)
                    if n is None or n[2] >= SURF_NZ:
                        e.z = zz
                    break

    @staticmethod
    def accel(e, wx, wy, ws, a, dt, cap):
        if not wx and not wy:
            return
        wsp = min(ws, cap)
        add = wsp - (e.vx * wx + e.vy * wy)
        if add <= 0:
            return
        as_ = a * ws * dt
        if as_ > add:
            as_ = add
        e.vx += as_ * wx
        e.vy += as_ * wy

    def pmove(self, e, dt, wx, wy, jump):
        gr = e.walk
        if jump and gr:
            sp = math.hypot(e.vx, e.vy)
            cap = MAXS * BHCAP
            if BHCAP and sp > cap:
                e.vx *= cap / sp
                e.vy *= cap / sp
            e.vz = JUMPV
            gr = False
            e.walk = False
        if gr:
            sp = math.hypot(e.vx, e.vy)
            if sp > .1:
                ns = max(0.0, sp - max(sp, STOPS) * FRIC * dt) / sp
                e.vx *= ns
                e.vy *= ns
            else:
                e.vx = e.vy = 0.0
            self.accel(e, wx, wy, MAXS, ACC, dt, MAXS)
        elif not e.steep:  # no air control while sliding down a steep slope
            self.accel(e, wx, wy, MAXS, AIRACC, dt, AIRCAP)

    def ptick(self, e, dt, f, s, n, c, sn):
        world = self.world
        e.walk = self.walkable(e)
        jump = e.jreq
        e.jreq = 0
        if e.sl > 0 and jump:
            e.sl = 0
        if e.sl > 0:
            e.sl -= dt
            # steer the slide: turn the slide direction with the view
            dy = e.yaw - (e.sdyaw if e.sdyaw is not None else e.yaw)
            if dy:
                cd, sd = math.cos(dy), math.sin(dy)
                e.sdx, e.sdy = e.sdx * cd - e.sdy * sd, e.sdx * sd + e.sdy * cd
            e.sdyaw = e.yaw
            k = max(0.0, e.sl) / 1.3
            v = 160 + 540 * k * k
            e.vx = e.sdx * v
            e.vy = e.sdy * v
            self.pm_move(e, dt)
            return
        mv = f or s
        wx = (c * f + sn * s) / n if mv else 0
        wy = (sn * f - c * s) / n if mv else 0
        jmp = jump
        if world.solid(e.x, e.y, e.z - 1):
            e.wj = 0
        elif jump and int(e.wj) < WJMAX:
            nx = ny = 0.0
            for a in range(8):
                dx, dy = math.cos(a * math.pi / 4), math.sin(a * math.pi / 4)
                for h in (8, 30):
                    if world.solid(e.x + dx * WJR, e.y + dy * WJR, e.z + h):
                        nx -= dx
                        ny -= dy
            nl = math.hypot(nx, ny)
            if nl > .01:
                nx /= nl
                ny /= nl
                d = e.vx * nx + e.vy * ny
                po = max(WJH, d)
                e.vx = e.vx - d * nx + nx * po
                e.vy = e.vy - d * ny + ny * po
                e.vz = WJV
                e.wj = int(e.wj) + 1
                jmp = 0
        self.pmove(e, dt, wx, wy, jmp)
        if e.cr > .5 and world.solid(e.x, e.y, e.z - 1):  # crouch walk is slow
            sp = math.hypot(e.vx, e.vy)
            cap = MAXS * .4
            if sp > cap:
                e.vx *= cap / sp
                e.vy *= cap / sp
        self.pm_move(e, dt)

    def can_hop(self, e, wx, wy):
        """True when the obstacle in the walking direction is low enough to jump onto / over
        (the jump apex is ~45 units). Tall walls return False."""
        n = math.hypot(wx, wy)
        if n < 1e-3 or not self.world.solid(e.x, e.y, e.z - 1):
            return False
        dx, dy = wx / n, wy / n
        solid = self.world.solid
        for dist in (14, 24):
            px, py = e.x + dx * dist, e.y + dy * dist
            if solid(px, py, e.z + 44) or solid(px, py, e.z + 70):
                return False  # wall: head / apex height is blocked too
            if not solid(px, py, e.z + STEP_H + 2) and not solid(px, py, e.z + 30):
                continue
        return True

    def los(self, a, b):
        dx, dy, dz = b.x - a.x, b.y - a.y, b.z - a.z
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        t = 24
        while t < d:
            if self.world.solid(a.x + dx * t / d, a.y + dy * t / d, a.z + 17 + dz * t / d):
                return 0
            t += 24
        return 1

    # ----- combat -----
    def kill(self, v, k):
        v.alive = 0
        v.resp = 3
        v.deaths += 1
        k.kills += 1
        self.score[k.team] += 1
        w = CFG["weapons"][k.w]
        self.feed.insert(0, {
            "killer": k.name or "UNKNOWN", "victim": v.name or "UNKNOWN",
            "weapon": ("KNIFE" if w.melee else w.n) if w else "RIFLE",
            "kl": bool(k.isP), "vl": bool(v.isP), "kt": k.team, "vt": v.team,
        })
        self.feed = self.feed[:5]
        v.anim = fresh_anim()
        if k.isP and v is not k:
            self.streak += 1
        if v.isP:
            self.streak = 0
            self.death_killer = k.name or "UNKNOWN"
            if self.P.zoom:
                self.P.zoom = 0
        if v.isP and self.net.active:
            self.net.send({"t": "kill", "k": self.net.my_id if k.isP else k.nid,
                           "v": self.net.my_id, "w": k.w})
        if self.score[k.team] >= CFG["frags"]:
            self.over = 6.0

    def fire(self, e, extra=0.0):
        w = CFG["weapons"][e.w]
        if e.cool > 0 or not e.alive:
            return
        if not w.melee and e.ammo[e.w] <= 0:
            e.ammo[e.w] = w.mag
            dur = 1.6
            if e.isP and w.vm and hasattr(w.vm, "want_empty"):
                w.vm.want_empty = not getattr(self, "_manual_reload", False)
            if e.isP and w.vm and w.vm.find("reload"):
                # the reload lasts as long as the weapon's reload animation, so a shot can never
                # cut it off halfway (that made the hands snap / bug out)
                d = self.vm_play(w.vm, "reload")
                if d > 0.3:
                    dur = min(d, 6.0)
            e.cool = dur
            e.reloadT = dur
            return
        if not w.melee:
            e.ammo[e.w] -= 1
        e.cool = 60 / w.rpm
        if e.isP and w.vm:
            self.vm_play(w.vm, "slash" if w.melee else "shoot")
        pp = e.pitch + e.pun_p      # aim + current recoil punch: bullets (and tracers) follow the recoil
        yy = e.yaw + e.pun_y
        cp = math.cos(pp)
        sp = (w.spr or 0) + extra
        sniper = ("awp" in str(w.n or "").lower() or "scout" in str(w.n or "").lower()
                  or (not w.melee and not w.auto and 0 < (w.mag or 0) <= 10))
        if e.isP and sniper:
            # CS:GO style AWP: pin-point when scoped and standing still, wild when unscoped,
            # in the air, and a bit off while moving
            spd = math.hypot(e.vx, e.vy)
            if not e.zoom or not e.walk:
                sp = .11          # unscoped / in the air: shots scatter widely
            elif spd > 40:
                sp = .03 + min(spd, 250.0) / 250.0 * .04
            else:
                sp = 0.0
        e.shootT = .15
        d = [cp * math.cos(yy) + (random.random() - .5) * sp * 2,
             cp * math.sin(yy) + (random.random() - .5) * sp * 2,
             math.sin(pp) + (random.random() - .5) * sp * 2]
        m = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2)
        d = [x / m for x in d]
        if e.isP and not w.melee:
            if w.auto:
                # CS:GO style spray: every bullet moves the aim along a fixed pattern
                p0 = spray_pattern(e.spray)
                e.spray = min(30.0, e.spray + 1.0)
                p1 = spray_pattern(e.spray)
                # the recoil moves your real aim and stays there (no snap back when you stop shooting)
                e.pend_p += p1[0] - p0[0]
                e.pend_y += p1[1] - p0[1]
                e.since = 0.0
                e.sprayw = True
            else:
                kick = w.recoil if w.recoil is not None else 0.05
                e.pun_p = min(0.3, e.pun_p + kick)
                e.pun_y += (random.random() - .5) * kick * .8
                e.sprayw = False
        if e.isP:
            # shoot from the exact eye position you are looking from (the camera is interpolated
            # between physics ticks, so e.x / e.y / e.z alone can be a hair behind it)
            al = min(1.0, e.acc / TICK)
            o = [e.px + (e.x - e.px) * al, e.py + (e.y - e.py) * al,
                 e.pz + (e.z - e.pz) * al + 17 + e.so]
        else:
            o = [e.x, e.y, e.z + 17 + e.so]
        rg = w.range or 3000
        tw = rg
        step = 4.0 if e.isP else 16.0   # bots use a coarser (much cheaper) ray
        t = 1.0
        solid = self.world.bullet_solid
        while t < rg:
            if solid(o[0] + d[0] * t, o[1] + d[1] * t, o[2] + d[2] * t):
                lo2, hi2 = max(0.0, t - step), t      # refine: find the exact wall distance
                for _r in range(5):
                    mid = (lo2 + hi2) * .5
                    if solid(o[0] + d[0] * mid, o[1] + d[1] * mid, o[2] + d[2] * mid):
                        hi2 = mid
                    else:
                        lo2 = mid
                tw = hi2
                break
            t += step
        best, bt = None, tw
        for v in self.ents:
            if not v.alive or v.team == e.team:
                continue
            sl = v.sl > 0 or v.cr > .5
            p = [v.x, v.y, v.z - (16 if sl else 0)]
            h = [24, 24, 26 if sl else 44]
            a, b = 0.0, 1e9
            for k in range(3):
                if abs(d[k]) < 1e-6:
                    if o[k] < p[k] - h[k] or o[k] > p[k] + h[k]:
                        b = -1
                else:
                    t1 = (p[k] - h[k] - o[k]) / d[k]
                    t2 = (p[k] + h[k] - o[k]) / d[k]
                    a = max(a, min(t1, t2))
                    b = min(b, max(t1, t2))
            if a <= b and b > 0 and a < bt:
                best, bt = v, a
        if best:
            if e.isP and self.custom_hit:
                self.audio.play(.6, self.custom_hit)
            if best.remote:
                self.net.send({"t": "hit", "to": best.nid, "d": w.dmg, "w": e.w})  # the victim applies it
            else:
                best.hp -= w.dmg * (1 if e.isP else .7)
                if best.hp <= 0:
                    self.kill(best, e)
        if not w.melee:
            tl = min(bt, 115 if e.isP else 85)
            # your own tracer starts a little ahead of the eye, right on the aim line,
            # so it comes out of the crosshair instead of the bottom of the screen
            ts = min(40.0, tl * .5) if e.isP else 0.0
            # nudged a little to the right and down so the line isn't hidden behind the crosshair
            ox = math.sin(yy) * 4.0 if e.isP else 0.0
            oy = -math.cos(yy) * 4.0 if e.isP else 0.0
            self.tracers.append({
                "a": (o[0] + d[0] * ts + ox, o[1] + d[1] * ts + oy,
                      o[2] + d[2] * ts - (3.0 if e.isP else 2.0)),
                "b": (o[0] + d[0] * tl, o[1] + d[1] * tl, o[2] + d[2] * tl),
                "al": .62 if e.isP else .32, "t": .04 if e.isP else .022,
            })
        if e.isP and self.net.active:
            tl2 = min(bt, 85)
            self.net.send({"t": "shot", "w": e.w,
                           "o": [round(o[0], 1), round(o[1], 1), round(o[2], 1)],
                           "e": [round(o[0] + d[0] * tl2, 1), round(o[1] + d[1] * tl2, 1), round(o[2] + d[2] * tl2, 1)]}, droppable=True)
        if not w.melee or w.snd:
            if e.isP:
                g, pan = .42, 0.0
            else:
                g, pan = self.spatial(e.x, e.y, e.z + 17, .55)
            snd = w.snd
            if not w.melee and self.custom_shot:  # you and the bots
                snd = self.custom_shot
            if g > .004:
                self.audio.play(g, snd, pan)
        if e.isP and e.w == 0 and e.zoom:
            e.zoom = 0  # AWP: scope out after each shot

    def bot(self, e, dt):
        e.rt -= dt
        if e.rt <= 0:
            e.rt = .25
            e.str = 1 if random.random() < .5 else -1
            b, bd = None, 1e9
            for v in self.ents:
                if not v.alive or v.team == e.team:
                    continue
                d = math.sqrt((v.x - e.x) ** 2 + (v.y - e.y) ** 2 + (v.z - e.z) ** 2)
                if d < bd:
                    bd, b = d, v
            e.tgt = b
            e.see = 1 if (b and bd < 2200 and self.los(e, b)) else 0
        t = e.tgt
        sp = 0
        if t:
            dx, dy = t.x - e.x, t.y - e.y
            da = math.atan2(dy, dx) - e.yaw
            da = math.atan2(math.sin(da), math.cos(da))
            e.yaw += da * min(1, dt * (8 if e.see else 3))
            e.pitch = math.atan2(t.z - e.z, math.hypot(dx, dy)) * .9
            if not e.see or math.hypot(dx, dy) > 450:
                sp = 190
        e.turn -= dt
        h = e.yaw + (e.dev if e.turn > 0 else 0)
        st = e.str * 70 if e.see else 0
        x0, y0 = e.x, e.y
        wx = math.cos(h) * sp + math.sin(e.yaw) * st
        wy = math.sin(h) * sp - math.cos(e.yaw) * st
        # Only hop over low obstacles. Against a wall (too tall to clear) bots never jump,
        # they just turn away; bots also never wall-jump.
        hop = bool(e.bl) and self.can_hop(e, wx, wy)
        e.bl = self.phys(e, dt, wx, wy, hop)
        if dt > 0:
            e.vx = (e.x - x0) / dt
            e.vy = (e.y - y0) / dt
        if e.bl and sp:
            e.turn = .9
            e.dev = (1 if random.random() < .5 else -1) * 1.4
        if e.see:
            e.aim += dt
            if e.aim > .45:
                self.fire(e, .05)
        else:
            e.aim = 0

    # ----- viewmodel -----
    def vm_play(self, vm, kind):
        c = vm.find(kind)
        if not c and kind == "slash":
            kind = "shoot"
            c = vm.find("shoot")
        if not c:
            kind = "idle"
            c = vm.find("idle")
        vm.begin_fade(0.07)
        self.vmA["s"] = random.choice(c) if c else 0
        self.vmA["t"] = 0.0
        self.vmA["loop"] = kind == "idle"
        if c and 0 <= self.vmA["s"] < len(vm.seqs):
            sq = vm.seqs[self.vmA["s"]]
            return sq["n"] / max(1.0, sq["fps"])   # how long the chosen animation takes (seconds)
        return 0.0

    def vm_tick(self, vm, dt):
        A = self.vmA
        if A["vm"] is not vm or A["w"] != self.P.w:
            A["vm"] = vm
            A["w"] = self.P.w
            self.vm_play(vm, "draw")
        vm.tick(dt)
        if not (0 <= A["s"] < len(vm.seqs)):
            return
        s = vm.seqs[A["s"]]
        A["t"] += dt * s["fps"]
        f = A["t"]
        if f >= s["n"]:
            if A["loop"]:
                A["t"] = f = f % s["n"]
            else:
                self.vm_play(vm, "idle")
                return
        ff = round(f * 16) / 16
        if ff >= s["n"]:
            ff = 0.0 if A["loop"] else s["n"] - 1.0
        vm.pose(A["s"], ff, loop=A["loop"])

    def update_entity_anim(self, e, dt):
        """CS 1.6 style: legs (gait) follow the movement direction at a speed-matched cadence,
        the torso follows the view (twist), the aim pitch is a blend, shooting / reloading play
        on the upper body only, sequences cross-fade, and dying plays a death sequence."""
        pm = e.pm
        if not pm:
            return
        A = e.anim
        if "gk" not in A:
            A.update({k: v for k, v in fresh_anim().items() if k not in A})
        d = pm.d
        pm.tick(dt)
        wpn = CFG["weapons"][e.w] if 0 <= e.w < len(CFG["weapons"]) else None

        # ---------------- death: full-body sequence, hold the last frame (corpse) ----------------
        if not e.alive:
            if not A["dead"]:
                A["dead"] = True
                ds = d.pick_all(DEATH_RX)
                if e.sl > 0 or e.cr > .5:
                    cd = [i for i in ds if re.search(r"crouch", d.seqs[i]["label"], re.I)]
                    ds = cd or ds
                else:
                    ds = [i for i in ds if not re.search(r"crouch", d.seqs[i]["label"], re.I)] or ds
                A["ds"] = random.choice(ds) if ds else -1
                A["dt"] = 0.0
                if A["gyaw"] is None:
                    A["gyaw"] = math.degrees(e.yaw)
                A["gyaw"] = math.degrees(e.yaw)
                if A["ds"] >= 0:
                    pm.begin_fade(0.08)
            if A["ds"] >= 0:
                s = d.seqs[A["ds"]]
                A["dt"] = min(s["n"] - 1.0, A["dt"] + dt * s["fps"])
                pm.pose_player(A["ds"], A["dt"], False, -1, 0.0, True, 0.0, 0.0)
            return
        A["dead"] = False

        # ---------------- movement state ----------------
        vx, vy = e.vx, e.vy
        spd = math.hypot(vx, vy)
        in_air = self.airborne(e)
        crouch = e.cr > .5 or e.sl > 0
        yaw_deg = math.degrees(e.yaw)

        # ---------------- gait yaw (legs) vs view yaw (torso) ----------------
        gy = A["gyaw"]
        if gy is None:
            gy = yaw_deg
        if spd > 10:
            # legs face the movement direction; moving backwards relative to the view (>120 deg)
            # the legs flip 180 and run "forward" under a backwards-moving body, like CS 1.6
            tgt = math.degrees(math.atan2(vy, vx))
            f0 = wrap180(yaw_deg - tgt)
            if f0 > 120:
                tgt -= 180.0
            elif f0 < -120:
                tgt += 180.0
            gy += wrap180(tgt - gy) * min(1.0, dt * 14)
        else:
            # standing: legs trail behind the view, torso twists, but never past the limit
            gy += wrap180(yaw_deg - gy) * min(1.0, dt * 4)
            fy = wrap180(yaw_deg - gy)
            if fy > MAX_TWIST:
                gy += fy - MAX_TWIST
            elif fy < -MAX_TWIST:
                gy += fy + MAX_TWIST
        gy = wrap180(gy)
        fy = wrap180(yaw_deg - gy)
        A["gyaw"] = gy
        A["twist"] = max(-120.0, min(120.0, fy))

        # ---------------- gait sequence (legs) ----------------
        if in_air:
            gk = "jump"
        elif crouch:
            gk = "crouchrun" if spd > 15 else "idle"
        elif spd > RUN_SPEED:
            gk = "run"
        elif spd > 10:
            gk = "walk"
        else:
            gk = "idle"
        if gk != A["gk"]:
            gs = -1
            if gk == "jump":
                gs = d.pick(r"^longjump", r"^jump", r"^run", r"^walk") if spd > 120 else d.pick(r"^jump", r"^run", r"^walk")
            elif gk == "crouchrun":
                gs = d.pick(r"^crouch_?run", r"^crouch.*(run|walk)", r"^cwalk", r"^walk", r"^run")
            elif gk == "run":
                gs = d.pick(r"^run", r"^walk")
            elif gk == "walk":
                gs = d.pick(r"^walk", r"^run")
            first = A["gs"] == -1 and A["gk"] == ""
            A["gk"] = gk
            if gs != A["gs"]:
                A["gs"] = gs
                A["gt"] = 0.0
                A["gl"] = gk != "jump"
                if not first:
                    pm.begin_fade()
        if A["gs"] >= 0:
            s = d.seqs[A["gs"]]
            n = s["n"]
            if A["gk"] in ("run", "walk", "crouchrun") and s["lin"] > 1.0:
                A["gt"] += spd * dt / s["lin"] * n          # footsteps match the ground speed
            else:
                A["gt"] += dt * s["fps"]
            if A["gl"]:
                A["gt"] %= n
            else:
                A["gt"] = min(n - 1.0, A["gt"])

        # ---------------- upper body: aim / shoot / reload (weapon + stance aware) ----------------
        if e.reloadT > 0:
            e.reloadT -= dt
        if e.shootT > 0:
            e.shootT -= dt
        new_shot = e.shootT > A["pst"] + 1e-6
        A["pst"] = e.shootT
        if e.reloadT > 0 and wpn and not wpn.melee:
            uk = "reload"
        elif e.shootT > 0:
            uk = "shoot"
        else:
            uk = "aim"
        ukey = (uk, crouch, e.w)
        if ukey != A["ukey"] or (uk == "shoot" and new_shot):
            us = upper_seq(pm, wpn, uk, crouch)
            if us < 0:
                us = upper_seq(pm, wpn, "aim", crouch)
            first = A["ukey"] is None
            changed = ukey != A["ukey"]
            A["ukey"] = ukey
            A["uk"] = uk
            if us != A["us"] or new_shot:
                if changed and not first and us != A["us"]:
                    pm.begin_fade()
                A["us"] = us
                A["ut"] = 0.0
                A["ul"] = uk == "aim"
        if A["us"] >= 0:
            s = d.seqs[A["us"]]
            n = s["n"]
            if A["uk"] == "reload":
                A["ut"] += dt * n / 1.6                     # fit the reload into the real reload time
            else:
                A["ut"] += dt * s["fps"]
            if A["ul"]:
                A["ut"] %= n
            else:
                A["ut"] = min(n - 1.0, A["ut"])
        A["init"] = True

        pd = math.degrees(e.pitch) * PITCH_SIGN
        pm.pose_player(A["us"], A["ut"], A["ul"], A["gs"], A["gt"], A["gl"], pd, A["twist"])

    # ----- assets -----
    def bind_assets(self):
        asset = self.asset
        msg = []
        names = [n for n in asset["mdl"] if not re.match(r"^[pw]_", n)]
        for i, w in enumerate(CFG["weapons"]):
            w.vm = None
            pick = self.wm_pick.get(i)
            if pick not in asset["mdl"]:
                pick = None
            n = pick or (str(w.model).lower() if w.model else None)
            if n not in asset["mdl"]:
                wk = key_norm(w.n)
                n = next((m for m in names if key_norm(re.sub(r"^v_", "", m)) == wk), None) or \
                    next((m for m in names if wk in key_norm(m)), None)
            if n and not w.model and not pick and bool(w.melee) != bool(re.search("knife", n)):
                n = None
            w._m = n if (n and n in asset["mdl"]) else None
        for w in CFG["weapons"]:
            if not w._m:
                continue
            try:
                w.vm = make_vm(asset["mdl"][w._m], asset["pack"].get(key_norm(w._m)), self.src_arms)
                o = w.ofs or [0, 0, 0]
                w.vm_ofs = (o[0], o[1], o[2])
                w.vm_flip = bool(w.flip) if w.flip is not None else w.vm.is_mirrored()
                msg.append(f"{w.n} <- {w._m}")
            except Exception as ex:
                msg.append(f"{w._m}: {ex}")
        wn = list(asset["wav"].keys())
        bad = re.compile(r"clip|bolt|draw|reload|slide|deploy|insert|empty|pull|release|cock|rattle")
        for i, w in enumerate(CFG["weapons"]):
            w.snd = None
            n = str(w.sound).lower() if w.sound else None
            if n not in asset["wav"]:
                pk = self.wm_pick.get(i)
                stem = key_norm(re.sub(r"^v_", "", pk)) if pk in asset["mdl"] else None
                n = (stem and next((m for m in wn if not bad.search(m) and key_norm(m).startswith(stem)), None)) or \
                    (w.melee and next((m for m in wn if re.match(r"^knife.*(slash|miss)", m)), None)) or \
                    next((m for m in wn if not bad.search(m) and key_norm(m).startswith(key_norm(w.n))), None)
            if n and n in asset["wav"]:
                w.snd = asset["wav"][n]
                msg.append(f"{w.n} sound: {n}")
        self.vm_msg = (" Weapons: " + ", ".join(msg) + ".") if msg else ""

    def spatial(self, x, y, z, base):
        """3D sound: (gain, pan) for a sound at (x, y, z) as heard by the local player.
        Louder when close, fading to silence at ~2600 units; pan follows the player's view."""
        P = self.P
        dx, dy, dz = x - P.x, y - P.y, z - (P.z + 17)
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        att = (1.0 - min(dist, 2600.0) / 2600.0) ** 2
        hd = math.hypot(dx, dy)
        lat = (dx * math.sin(P.yaw) - dy * math.cos(P.yaw)) / max(hd, 1.0)  # +1 = right of you
        return base * att, lat * min(1.0, hd / 120.0)

    def weapon_mods(self):
        """Loaded weapon view-models (v_*.mdl style), for the Loadout tab."""
        return sorted(n for n in self.asset["mdl"] if not re.match(r"^[pw]_", n))

    def load_src_vpk(self, path):
        """CS:GO weapon mod (.vpk): adds every v_*.mdl inside as a selectable view-model.
        Only models, textures and animations are used - the weapon stats stay the game's own."""
        arch = VpkArchive(path)
        names = src_scan(arch)
        if not names:
            return f"{Path(path).name}: no weapon view-model (v_*.mdl) inside."
        for nm in names:
            self.asset["mdl"][nm.rsplit("/", 1)[-1]] = SrcSpec(arch, nm)
        if src_arms(arch):
            self.add_arms(src_arms(arch).rsplit("/", 1)[-1], SrcSpec(arch, src_arms(arch)))
        return f"{Path(path).name}: {len(names)} CS:GO view-model(s) - pick them in Loadout."

    def add_arms(self, name, spec):
        self.arms_all[name] = spec
        if self.arms_off:
            return
        if (self.arms_want and name == self.arms_want) or (not self.arms_want and self.arms_pick is None):
            self.arms_pick, self.src_arms = name, spec

    def set_arms(self, name):
        """Equip an arms model (name from arms_all) or None = no arms."""
        self.arms_pick = name if name in self.arms_all else None
        self.arms_want = self.arms_pick
        self.arms_off = self.arms_pick is None
        self.src_arms = self.arms_all.get(self.arms_pick)
        self.bind_assets()

    def load_src_archive(self, path):
        """.zip / .7z with CS:GO or CS:S weapon files (models, materials, or a .vpk inside).
        Only the needed parts are unpacked (once) into the temp folder, then loaded like loose files."""
        import tempfile
        path = Path(path)
        out = Path(tempfile.gettempdir()) / "akb_arena_src" / (re.sub(r"[^A-Za-z0-9_.-]", "_", path.stem) + "_%d" % path.stat().st_size)

        def want(nm):
            nm = nm.replace("\\", "/").lower()
            parts = nm.split("/")
            return nm.endswith(".vpk") or (not nm.endswith("/") and ("models" in parts[:-1] or "materials" in parts[:-1]))
        if not (out / ".done").exists():
            out.mkdir(parents=True, exist_ok=True)
            if path.suffix.lower() == ".7z":
                try:
                    import py7zr
                except Exception:
                    import subprocess
                    subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "py7zr"])
                    try:
                        import importlib
                        importlib.invalidate_caches()
                        import py7zr
                    except Exception:
                        return f"{path.name}: .7z needs py7zr. Run: pip install py7zr  (or unzip it yourself)"
                with py7zr.SevenZipFile(str(path)) as z:
                    names = [n for n in z.getnames() if want(n)]
                    z.extract(path=str(out), targets=names)
            else:
                import zipfile
                with zipfile.ZipFile(str(path)) as z:
                    for i in z.infolist():
                        if not i.is_dir() and want(i.filename):
                            z.extract(i, str(out))
            (out / ".done").write_text("ok")
        files = [f for f in out.rglob("*") if f.is_file() and f.suffix.lower() in (".mdl", ".vpk")
                 and f.name.lower().count(".mdl") <= 1]
        if not files:
            return f"{path.name}: no CS:GO / CS:S weapon or arms model inside."
        return self.load_files(files, "weapon") or f"{path.name}: loaded."

    def set_weapon_pick(self, slot, name):
        if name:
            self.wm_pick[slot] = name
        else:
            self.wm_pick.pop(slot, None)
        self.bind_assets()

    def set_custom_sound(self, kind, path):
        """kind is 'shot' or 'hit'. An empty path clears it. Returns a status string."""
        snd = None
        if path:
            try:
                snd = pygame.mixer.Sound(str(path))
            except Exception as ex:
                path = ""
                setattr(self, f"custom_{kind}", None)
                setattr(self, f"custom_{kind}_path", "")
                return f"Cannot load that sound: {ex}"
        setattr(self, f"custom_{kind}", snd)
        setattr(self, f"custom_{kind}_path", str(path) if path else "")
        return f"Loaded {Path(path).name}." if path else "Back to the default sound."

    def load_files(self, paths, mode):
        """Returns a status string."""
        status = None
        map_chg = wpn_chg = cfg_chg = pm_chg = False
        for p in sorted(paths, key=lambda q: len(Path(q).parts)):
            p = Path(p)
            n = p.name.lower()
            if n.endswith(".vpk"):               # CS:GO weapon mod package: models + animations
                try:
                    st = self.load_src_vpk(p)
                    status = st or status
                    wpn_chg = True
                except Exception as ex:
                    status = f"Cannot open {p.name}: {ex}"
                continue
            if n.endswith((".zip", ".7z")):         # archive with CS:GO / CS:S weapons and / or arms
                try:
                    st = self.load_src_archive(p)
                    status = st or status
                    wpn_chg = True
                except Exception as ex:
                    status = f"Cannot open {p.name}: {ex}"
                continue
            if n.endswith((".vvd", ".vtx", ".vtf", ".vmt")):   # loose CS:GO model files (found by their .mdl)
                wpn_chg = True
                continue
            try:
                data = p.read_bytes()
            except Exception as ex:
                return f"Cannot read {p.name}: {ex}"
            if n.endswith((".bsp", ".wad")):
                map_chg = True
            elif n.endswith(".mdl"):
                if mode == "player":
                    pm_chg = True
                else:
                    wpn_chg = True
            elif n.endswith(".json"):
                wpn_chg = cfg_chg = True
            elif n.endswith(".wav"):
                wpn_chg = True
            if n.endswith(".bsp"):
                self.files["bsp"] = data
            elif n.endswith(".wad"):
                self.files["wads"].append(data)
            elif n.endswith(".json"):
                try:
                    j = json.loads(data.decode("utf-8"))
                    for i, wd in enumerate(j.get("weapons", [])):
                        while len(CFG["weapons"]) <= i:
                            CFG["weapons"].append(Wpn(n="Gun", dmg=30, rpm=300, mag=20, spr=.02, auto=1))
                        CFG["weapons"][i].__dict__.update(wd)
                    if j.get("bots") is not None:
                        CFG["bots"] = int(j["bots"])
                    if j.get("frags"):
                        CFG["frags"] = int(j["frags"])
                except Exception as ex:
                    status = "Bad JSON: " + str(ex)
            elif n.endswith(".wav"):
                try:
                    self.asset["wav"][n] = pygame.mixer.Sound(io.BytesIO(data))
                except Exception:
                    pass
            elif n.endswith(".mdl"):
                if (len(data) > 250 and data[:4] == b"IDST" and 44 <= struct.unpack_from("<i", data, 4)[0] <= 49
                        and mode != "player"):
                    # CS:GO / CS:S .mdl (its .vvd/.vtx/.vtf/.vmt are found in the same mod folder)
                    if re.search(r"_zoom|_scope|display|dropped|^w_", n):
                        continue
                    root = src_root(p)
                    arch = self.src_dirs.get(root)
                    if arch is None:
                        arch = self.src_dirs[root] = DirSrcFiles(root)
                    rel = p.resolve().relative_to(root.resolve()).as_posix().lower()
                    spec = SrcSpec(arch, rel)
                    key = n
                    old = self.asset["mdl"].get(key)
                    if old is not None and not (isinstance(old, SrcSpec) and old.arch is arch and old.mdl == rel):
                        key = re.sub(r"\.mdl$", "", n) + " (" + (root.name or "mod")[:14] + ").mdl"
                    if re.search(r"arms|hands", n) and not n.startswith("v_"):
                        self.add_arms(key, spec)
                    else:
                        self.asset["mdl"][key] = spec
                    continue
                if len(data) < 250 or data[:4] != b"IDST" or struct.unpack_from("<i", data, 4)[0] != 10:
                    status = "Not a GoldSrc / CS:GO MDL: " + n
                    continue
                if struct.unpack_from("<i", data, 204)[0] == 0:
                    base = n[:-5] if n.endswith("t.mdl") else n
                    self.asset["pack"][key_norm(base)] = data
                elif mode == "player":
                    self.asset["player"][n] = data
                    self.pm_cache.pop(n, None)
                    for tk, sv in self.pm_saved.items():
                        if sv == n and not self.pm_cfg[tk]:
                            self.pm_cfg[tk] = n
                else:
                    self.asset["mdl"][n] = data
        if pm_chg and not map_chg and not wpn_chg:
            self.reset_game()
            return f"{len(self.asset['player'])} player model(s) loaded. Pick them below."
        if wpn_chg:
            self.bind_assets()
            if not map_chg:
                if cfg_chg or pm_chg:
                    self.reset_game()
                nsrc = sum(isinstance(v, SrcSpec) for v in self.asset["mdl"].values())
                if not self.vm_msg and nsrc:
                    return status or f"{nsrc} CS:GO / CS:S view-model(s) loaded - pick them in Loadout."
                return (self.vm_msg or "No weapon files matched.").strip() if not status else status
        if not map_chg:
            return status or ""
        try:
            if self.net.active or self.net.mode:
                self.net_leave("Map changed - left the online room (everyone must use the same map).")
            self.texdb = {}
            for wb in self.files["wads"]:
                parse_wad(wb, self.texdb)
            if self.files["bsp"]:
                self.set_world(BspWorld(self.files["bsp"], self.texdb))
                s = f"Map loaded ({len(self.texdb)} textures). Missing textures show as a grey checker - add the map's .wad too."
            else:
                self.set_world(ArenaWorld())
                s = "Built-in arena loaded."
            return s + self.vm_msg
        except Exception as ex:
            return "Error: " + str(ex)

    # ----- per-frame -----
    def update(self, dt):
        P = self.P
        P.took_damage_this_frame = False
        self.net_update(dt)
        P = self.P
        K = self.keys
        B = self.binds
        # Regenerate health after 5 seconds without taking damage. A low-health
        # player returns to full in about 4 seconds; near-full health recovers in 2.
        if P.alive and not self.over and P.hp < 200 and not P.took_damage_this_frame:
            if P.regen_delay > 0:
                P.regen_delay = max(0.0, P.regen_delay - dt)
            else:
                hp_ratio = max(0.0, min(1.0, P.hp / 200.0))
                full_heal_time = 4.0 - 2.0 * hp_ratio
                P.hp = min(200.0, P.hp + (200.0 - P.hp) / max(2.0, full_heal_time) * dt)
        if (self.lock or self.net.active) and not self.over:   # online: the world never pauses
            for e in self.ents:
                e.cool -= dt
                if e.remote:
                    self.remote_tick(e, dt)
                    continue
                if not e.alive:
                    e.resp -= dt
                    if e.resp <= 0:
                        self.spawn(e)
                    continue
                if e.isP:
                    f = (1 if K.get(B["forward"]) else 0) - (1 if K.get(B["backward"]) else 0)
                    s = (1 if K.get(B["right"]) else 0) - (1 if K.get(B["left"]) else 0)
                    n = math.hypot(f, s) or 1
                    c, sn = math.cos(e.yaw), math.sin(e.yaw)
                    e.walk = self.walkable(e)
                    gr = e.walk
                    slide = bool(K.get(B["slide"]))
                    if e.sl > 0 and not slide:
                        e.sl = 0          # released the slide key: stop sliding
                        e.aS = 0
                        e.scd = min(e.scd, .3)
                    else:
                        if not gr and slide:
                            e.aS = 1
                        if not slide:
                            e.aS = 0
                        if slide and (not e.cHeld or e.aS) and e.sl <= 0 and e.scd <= 0 and gr and (f or s):
                            e.sl = 1.3
                            e.scd = 1.6
                            e.sdx = c  # always slide the way you look
                            e.sdy = sn
                            e.sdyaw = e.yaw
                            e.aS = 0
                        if gr:
                            e.aS = 0
                    e.cHeld = slide
                    e.scd -= dt
                    e.acc += dt
                    while e.acc >= TICK:
                        e.acc -= TICK
                        e.px, e.py, e.pz = e.x, e.y, e.z
                        self.ptick(e, TICK, f, s, n, c, sn)
                    crouch = bool(K.get(B["crouch"])) and e.sl <= 0
                    e.cr += ((1.0 if crouch else 0.0) - e.cr) * min(1.0, dt * 14)
                    e.so = -11 if e.sl > 0 else -11 * e.cr
                    if K.get("mouse1") and (CFG["weapons"][e.w].auto or not e.held):
                        self.fire(e)
                        e.held = 1
                    if not K.get("mouse1"):
                        e.held = 0
                else:
                    self.bot(e, dt)
        elif self.over:
            self.over -= dt
            if self.over <= 0:
                self.over = 0.0
                self.reset_game()
                P = self.P

        target = 20.0 if (P.zoom and P.w == 0) else 75.0
        if abs(self.fov - target) > .1:
            self.fov += (target - self.fov) * min(1, dt * 20)
        else:
            self.fov = target

        if P.sprayw:
            P.since += dt
            if P.since > 0.12:
                P.spray = max(0.0, P.spray - dt * 18)      # spray pattern resets when you stop shooting
            k = min(1.0, dt * 25)
            dp, dy = P.pend_p * k, P.pend_y * k
            P.pitch = max(-1.5, min(1.5, P.pitch + dp))
            P.yaw += dy
            P.pend_p -= dp
            P.pend_y -= dy
            if P.spray <= 0 and abs(P.pend_p) < 1e-5 and abs(P.pend_y) < 1e-5:
                P.sprayw = False
        else:
            k = math.exp(-dt * 3.5)       # single-shot recoil settles back down
            P.pun_p *= k
            P.pun_y *= k
        if P.reloadT > 0:
            P.reloadT -= dt
        for e in self.ents:
            if e.pm:
                # posing a model is the heaviest per-bot cost: close bots update often, far ones rarely
                e.aacc += dt
                d2 = (e.x - P.x) ** 2 + (e.y - P.y) ** 2
                step = 1 / 50 if d2 < 640000 else (1 / 30 if d2 < 3240000 else 1 / 15)
                if e.aacc >= step:
                    self.update_entity_anim(e, e.aacc)
                    e.aacc = 0.0
            spd = math.hypot(e.vx, e.vy)
            ground = e.walk if e.isP else not self.airborne(e)
            in_air = e.alive and not ground
            run = e.alive and e.sl <= 0 and spd > 10 and ground
            if in_air:
                e.pvz = e.vz
            elif e.was_air and e.alive:
                e.land = min(1.0, abs(e.pvz) / 450)   # landing impact
            e.was_air = in_air
            e.air += ((1.0 if in_air else 0.0) - e.air) * min(1.0, dt * 14)
            e.land = max(0.0, e.land - dt * 4)
            if run:
                e.ph += dt * spd * 0.035
            e.bob += ((min(1.0, spd / MAXS) if run else 0.0) - e.bob) * min(1.0, dt * 10)
        for t in self.tracers[:]:
            t["t"] -= dt
            if t["t"] <= 0:
                self.tracers.remove(t)
        self.flash = max(0.0, self.flash - dt * 2.5)
        cvm = CFG["weapons"][P.w].vm
        if cvm and P.alive and not (P.zoom and P.w == 0):
            self.vm_tick(cvm, dt)

    def select_weapon(self, i):
        P = self.P
        if P and 0 <= i < len(CFG["weapons"]):
            P.w = i
            P.zoom = 0

    def reload_pressed(self):
        P = self.P
        if P and P.zoom and P.w == 0:
            return  # no reload while scoped
        if P and not CFG["weapons"][P.w].melee:
            if P.reloadT > 0 or P.ammo[P.w] >= CFG["weapons"][P.w].mag:
                return  # already reloading / full: pressing R must not restart the animation
            P.ammo[P.w] = 0
            P.cool = 0
            self._manual_reload = True
            try:
                self.fire(P)
            finally:
                self._manual_reload = False


# --------------------------------------------------------------------------
# 2D drawing helpers
# --------------------------------------------------------------------------
class Gfx:
    def __init__(self):
        self.fonts = {}
        self.cache = {}

    def font(self, size, bold):
        k = (size, bold)
        if k not in self.fonts:
            self.fonts[k] = pygame.font.SysFont("segoeui,arial,helvetica,dejavusans,freesans", size, bold=bold)
        return self.fonts[k]

    def _tex(self, s, size, color, bold):
        k = (s, size, color, bold)
        c = self.cache.get(k)
        if c:
            return c
        if len(self.cache) > 400:
            for tid, _, _ in self.cache.values():
                gl.glDeleteTextures([tid])
            self.cache.clear()
        surf = self.font(size, bold).render(s if s else " ", True, color)
        w, h = surf.get_size()
        data = pygame.image.tostring(surf, "RGBA", False)
        tid = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
        self.cache[k] = (tid, w, h)
        return self.cache[k]

    def text_w(self, s, size, bold=False):
        return self.font(size, bold).size(s)[0]

    def text(self, s, x, y, size=14, color=(255, 255, 255), alpha=1.0, bold=False, anchor="l"):
        tid, w, h = self._tex(s, size, tuple(color), bold)
        if anchor == "c":
            x -= w / 2
        elif anchor == "r":
            x -= w
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
        gl.glColor4f(1, 1, 1, alpha)
        gl.glBegin(gl.GL_QUADS)
        gl.glTexCoord2f(0, 0); gl.glVertex2f(x, y)
        gl.glTexCoord2f(1, 0); gl.glVertex2f(x + w, y)
        gl.glTexCoord2f(1, 1); gl.glVertex2f(x + w, y + h)
        gl.glTexCoord2f(0, 1); gl.glVertex2f(x, y + h)
        gl.glEnd()
        return w

    @staticmethod
    def rect(x, y, w, h, rgba):
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor4f(*rgba)
        gl.glBegin(gl.GL_QUADS)
        gl.glVertex2f(x, y); gl.glVertex2f(x + w, y)
        gl.glVertex2f(x + w, y + h); gl.glVertex2f(x, y + h)
        gl.glEnd()

    def box(self, x, y, w, h, fill, border=None):
        self.rect(x, y, w, h, fill)
        if border:
            self.rect(x, y, w, 1, border)
            self.rect(x, y + h - 1, w, 1, border)
            self.rect(x, y, 1, h, border)
            self.rect(x + w - 1, y, 1, h, border)


def c255(r, g, b, a=1.0):
    return (r / 255, g / 255, b / 255, a)


# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------
def key_label(code):
    if code == "Mouse4":
        return "Mouse 4 / Back"
    if code == "Mouse5":
        return "Mouse 5 / Forward"
    return code.title() if len(code) > 1 else code.upper()


class Menu:
    def __init__(self, game, gfx, on_launch, pick_files):
        self.g = game
        self.gfx = gfx
        self.page = "play"
        self.name = ""
        self.focus = False
        self.room = ""        # online: room name field
        self.ip = ""          # online: direct-join field
        self.edit = None      # which online text field has focus
        self.pfp_path = ""    # profile picture file
        self.pfp_tex = None
        self.pfp_status = ""
        self.status = "Built-in arena loaded."
        self.snd_status = ""
        self.vm_prev = 0      # weapon shown in the viewmodel preview
        self.vm_dd = False    # preview dropdown open?
        self.lo_tab = "weapons"   # Loadout tab: "weapons" | "players"
        self.lo_slot = 0          # weapon slot being edited
        self.lo_team = "CT"       # team being edited
        self.lo_page = 0
        self.set_sub = "controls"  # Settings sub-tab: controls | sounds | viewmodel | video
        self.pv = {}              # cached preview model instances
        self.pv_bb = {}           # cached bounding boxes
        self.waiting = None
        self.on_launch = on_launch
        self.pick_files = pick_files
        self.hot = []
        self.drag = None
        self.mp = (-1, -1)
        self.xf = (0, 0, 1)

    # --- input ---
    def to_logical(self, pos):
        ox, oy, s = self.xf
        return ((pos[0] - ox) / s, (pos[1] - oy) / s)

    def mouse_move(self, pos):
        self.mp = self.to_logical(pos)
        if self.drag:
            self.drag(self.mp[0])

    def mouse_down(self, pos, button):
        if button != 1:
            return
        mx, my = self.to_logical(pos)
        self.focus = False
        self.edit = None
        for (x, y, w, h, click, drag) in reversed(self.hot):
            if x <= mx <= x + w and y <= my <= y + h:
                if drag:
                    self.drag = drag
                    drag(mx)
                if click:
                    click()
                break

    def mouse_up(self):
        self.drag = None

    def bind_press(self, code):
        """Assign `code` to the waiting action; returns True if consumed."""
        if not self.waiting:
            return False
        action = self.waiting
        binds = self.g.binds
        clash = next((i for i, _ in BIND_META if i != action and binds[i] == code), None)
        if clash:
            binds[clash] = binds[action]
        binds[action] = code
        self.waiting = None
        return True

    def key_down(self, event):
        if self.waiting:
            if event.key == pygame.K_ESCAPE:
                self.waiting = None
            else:
                self.bind_press(pygame.key.name(event.key))
            return True
        if self.edit:
            lim = {"room": 28, "name": 16}.get(self.edit, 40)
            cur = getattr(self, self.edit)
            if event.key in (pygame.K_RETURN, pygame.K_ESCAPE):
                self.edit = None
                save_settings(self.g, self)
            elif event.key == pygame.K_BACKSPACE:
                setattr(self, self.edit, cur[:-1])
            elif event.unicode and event.unicode.isprintable() and len(cur) < lim:
                setattr(self, self.edit, cur + event.unicode)
            return True
        if self.focus:
            if event.key in (pygame.K_RETURN, pygame.K_ESCAPE):
                self.focus = False
            elif event.key == pygame.K_BACKSPACE:
                self.name = self.name[:-1]
            elif event.unicode and event.unicode.isprintable() and len(self.name) < 16:
                self.name += event.unicode
            return True
        return False

    # --- drawing ---
    def draw(self, W, H):
        g, gfx = self.g, self.gfx
        s = max(.4, min((W - 24) / 1000, (H - 24) / 600, 1.5))
        ox, oy = (W - 1000 * s) / 2, (H - 600 * s) / 2
        self.xf = (ox, oy, s)
        self.hot = []
        mx, my = self.mp

        gl.glDisable(gl.GL_DEPTH_TEST)
        gfx.rect(0, 0, W, H, c255(8, 8, 8))
        gl.glPushMatrix()
        gl.glTranslatef(ox, oy, 0)
        gl.glScalef(s, s, 1)

        gfx.box(0, 0, 1000, 600, c255(12, 12, 12), c255(48, 48, 48))
        gfx.box(0, 0, 240, 600, c255(10, 10, 10))
        gfx.rect(240, 0, 1, 600, c255(41, 41, 41))
        gfx.text("AKB / ARENA", 28, 30, 12, (119, 119, 119), bold=True)
        gfx.text("Browser TDM", 28, 48, 22, (255, 255, 255), bold=True)

        def hover(x, y, w, h):
            return x <= mx <= x + w and y <= my <= y + h

        def nav(label, page, y):
            act = self.page == page
            h = hover(20, y, 200, 40)
            gfx.box(20, y, 200, 40, c255(32, 32, 32) if act else (c255(23, 23, 23) if h else (0, 0, 0, 0)),
                    c255(51, 51, 51) if act else None)
            gfx.text(label, 40, y + 11, 14, (255, 255, 255) if act else (146, 146, 146))
            self.hot.append((20, y, 200, 40, lambda p=page: self.set_page(p), None))

        nav("Play", "play", 110)
        nav("Profile", "profile", 156)
        nav("Loadout", "loadout", 202)
        nav("Settings", "settings", 248)
        gfx.text(("ONLINE: " + g.net.room[:18]) if g.net.active else "PYTHON BUILD", 28, 570, 11, (104, 104, 104))

        def button(x, y, w, h, label, fn, primary=False):
            hv = hover(x, y, w, h)
            if primary:
                fill = c255(212, 212, 212) if hv else c255(241, 241, 241)
                gfx.box(x, y, w, h, fill, c255(255, 255, 255))
                gfx.text(label, x + w / 2, y + h / 2 - 9, 13, (8, 8, 8), bold=True, anchor="c")
            else:
                gfx.box(x, y, w, h, c255(41, 41, 41) if hv else c255(26, 26, 26), c255(85, 85, 85) if hv else c255(58, 58, 58))
                gfx.text(label, x + w / 2, y + h / 2 - 9, 13, (238, 238, 238), bold=True, anchor="c")
            self.hot.append((x, y, w, h, fn, None))

        def panel(x, y, w, h, title, sub=None):
            gfx.box(x, y, w, h, c255(20, 20, 20), c255(43, 43, 43))
            gfx.text(title, x + 20, y + 14, 14, (255, 255, 255), bold=True)
            if sub:
                gfx.text(sub, x + 20, y + 34, 12, (133, 133, 133))

        def slider(x, y, w, val, lo, hi, setter):
            t = (val - lo) / (hi - lo)
            gfx.rect(x, y + 8, w, 4, c255(60, 60, 60))
            gfx.rect(x, y + 8, w * t, 4, c255(240, 240, 240))
            gfx.box(x + w * t - 7, y + 3, 14, 14, c255(255, 255, 255))

            def drag(mxl):
                tt = max(0.0, min(1.0, (mxl - x) / w))
                setter(lo + (hi - lo) * tt)
            self.hot.append((x - 8, y, w + 16, 20, None, drag))

        cx, cw = 280, 680
        if self.page == "settings":
            for i, (sub, lab) in enumerate((("controls", "Controls"), ("sounds", "Sounds"),
                                            ("viewmodel", "Viewmodel"), ("crosshair", "Crosshair"),
                                            ("video", "Video"))):
                button(cx + cw - 5 * 98 + 6 + i * 98, 40, 92, 30, lab,
                       lambda sub=sub: self.set_sub_tab(sub), self.set_sub == sub)
        if self.page == "play":
            net = g.net
            gfx.text("READY WHEN YOU ARE", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Enter the arena.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Play offline against bots, or host / join a room with friends. No port forwarding needed.",
                     cx, 96, 13, (146, 146, 146))

            def field(x, y, w, h, key, ph):
                val = getattr(self, key)
                on = self.edit == key
                gfx.box(x, y, w, h, c255(8, 8, 8), c255(170, 170, 170) if on else c255(54, 54, 54))
                shown = val if (val or on) else ph
                gfx.text(shown + ("|" if on and int(time.time() * 2) % 2 == 0 else ""), x + 12, y + 8, 14,
                         (255, 255, 255) if val else (110, 110, 110))
                self.hot.append((x, y, w, h, lambda k=key: setattr(self, "edit", k), None))

            def pname():
                return self.name.strip() or "Player"

            def toggle_bots():
                g.bots_on = not g.bots_on
                g.reset_game()
                save_settings(g, self)

            def do_host():
                g.player_name = pname()
                g.P.name = g.player_name
                ok = g.net_host(self.room.strip() or (pname() + "'s room"))
                self.status = net.status
                if ok:
                    self.on_launch()

            def do_join(r):
                g.player_name = pname()
                g.P.name = g.player_name
                g.net_join(r)

            def do_leave():
                g.net_leave("Left the room.")
                self.status = "Left the room."

            # --- top panel: host / current room / offline match ---
            if net.mode == "host":
                panel(cx, 128, cw, 112, "Hosting: " + net.room[:40],
                      "Your room is listed in the room browser. Friends join with one click.")
                addr = ("%s:%d" % net.pub) if net.pub else "finding it ..."
                gfx.text("Public address: %s   |   Players: %d / %d" % (addr, len(net.peers) + 1, NET_MAX_PLAYERS),
                         cx + 20, 180, 13, (200, 200, 200))
                button(cx + 20, 202, 150, 32, "Close room", do_leave)
                button(cx + 182, 202, 150, 32, "Back to game", self.on_launch, True)
                ly = 252
            elif net.mode == "client":
                panel(cx, 128, cw, 112, "In room: " + net.room[:40], "Connected to the host. Players: %d" % (len(net.players) + 1))
                button(cx + 20, 202, 150, 32, "Leave room", do_leave)
                button(cx + 182, 202, 150, 32, "Back to game", self.on_launch, True)
                ly = 252
            elif net.mode == "joining":
                panel(cx, 128, cw, 112, "Connecting ...", "Reaching the host. This can take a few seconds.")
                button(cx + 20, 202, 150, 32, "Cancel", do_leave)
                ly = 252
            else:
                panel(cx, 128, cw, 150, "Play", "Host a room or play offline against bots. Change your name in Profile.")
                field(cx + 20, 176, 640, 36, "room", "Room name")
                button(cx + 20, 226, 200, 36, "Host room >", do_host, True)
                button(cx + 232, 226, 200, 36, "Offline match >", self.on_launch, True)
                button(cx + 444, 226, 100, 36, "Bots: ON" if g.bots_on else "Bots: OFF", toggle_bots)
                button(cx + 556, 226, 104, 36, "Load map", lambda: self.pick("map"))
                ly = 290

            # --- roster / room browser ---
            if net.active:
                panel(cx, ly, cw, 578 - ly, "Players in the room")
                rows = [(g.P.name or "You", g.P.team, True)] + [(p["n"], p["tm"], False) for p in net.players.values()]
                for i, (nm, tm, me) in enumerate(rows[:NET_MAX_PLAYERS]):
                    xx = cx + 20 + (i % 2) * 330
                    yy = ly + 40 + (i // 2) * 34
                    col = (80, 140, 255) if tm == 0 else (255, 90, 80)
                    gfx.box(xx, yy, 320, 28, (col[0] / 255, col[1] / 255, col[2] / 255, .18), c255(41, 41, 41))
                    gfx.text(nm[:20] + ("  (you)" if me else ""), xx + 10, yy + 6, 13, (240, 198, 116) if me else (255, 255, 255), bold=me)
                    gfx.text("CT" if tm == 0 else "T", xx + 308, yy + 6, 12, col, bold=True, anchor="r")
            elif not net.mode:
                rooms = sorted(net.rooms.values(), key=lambda r: r["n"].lower())
                panel(cx, ly, cw, 578 - ly, "Room browser", "Rooms on the internet and on your network - refreshes automatically.")
                gfx.text("Room list: " + net.broker_state(), cx + cw - 20, ly + 16, 11, (119, 119, 119), anchor="r")
                if not rooms:
                    gfx.text("No rooms found yet. Host one, or ask a friend to - rooms appear here by themselves.", cx + 20, ly + 56, 13, (130, 130, 130))
                for i, r in enumerate(rooms[:6]):
                    yy = ly + 46 + i * 34
                    full = r["p"] >= r["m"]
                    other = r["map"] != g.map_sig()
                    gfx.box(cx + 20, yy, cw - 40, 30, c255(17, 17, 17), c255(41, 41, 41))
                    gfx.text(r["n"][:26], cx + 32, yy + 7, 14, (255, 255, 255), bold=True)
                    gfx.text("by " + r["h"][:14], cx + 290, yy + 8, 12, (140, 140, 140))
                    gfx.text("%d/%d" % (r["p"], r["m"]), cx + 430, yy + 8, 12, (200, 200, 200))
                    if other:
                        gfx.text("other map", cx + 480, yy + 8, 11, (230, 160, 90))
                    if full:
                        gfx.text("FULL", cx + cw - 36, yy + 8, 12, (150, 150, 150), anchor="r")
                    else:
                        button(cx + cw - 110, yy + 2, 80, 26, "Join", lambda r=r: do_join(r), True)

            # --- status line (map loaded, join errors, connection progress) ---
            msg = net.status if net.mode else self.status
            bad = msg.startswith(("Denied", "Could", "Conn", "Lost", "The host", "Cannot", "Error"))
            col = (230, 160, 90) if bad else (170, 170, 170)
            if net.mode or bad:   # only show room status / errors, not the weapons-loaded text
                gfx.text(msg[:100], cx + 20, 548, 11, col)
                if len(msg) > 100:
                    gfx.text(msg[100:200], cx + 20, 563, 11, col)
        elif self.page == "profile":
            gfx.text("YOUR IDENTITY", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Profile.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Change your username and profile picture.", cx, 96, 13, (146, 146, 146))
            panel(cx, 128, cw, 250, "Your profile", "Your username is shown to other players in online rooms and on the scoreboard.")
            px, py = cx + 20, 176
            gfx.box(px - 2, py - 2, 164, 164, c255(8, 8, 8), c255(58, 58, 58))
            if self.pfp_tex:
                gl.glEnable(gl.GL_TEXTURE_2D)
                gl.glBindTexture(gl.GL_TEXTURE_2D, self.pfp_tex)
                gl.glColor4f(1, 1, 1, 1)
                gl.glBegin(gl.GL_QUADS)
                gl.glTexCoord2f(0, 0); gl.glVertex2f(px, py)
                gl.glTexCoord2f(1, 0); gl.glVertex2f(px + 160, py)
                gl.glTexCoord2f(1, 1); gl.glVertex2f(px + 160, py + 160)
                gl.glTexCoord2f(0, 1); gl.glVertex2f(px, py + 160)
                gl.glEnd()
            else:
                gfx.rect(px, py, 160, 160, c255(30, 30, 30))
                gfx.text((self.name.strip() or "P")[0].upper(), px + 80, py + 44, 64, (150, 150, 150), bold=True, anchor="c")
            gfx.text("USERNAME", cx + 210, 176, 11, (189, 189, 189), bold=True)
            on = self.edit == "name"
            gfx.box(cx + 210, 196, 300, 36, c255(8, 8, 8), c255(170, 170, 170) if on else c255(54, 54, 54))
            shown = self.name if (self.name or on) else "Your name"
            gfx.text(shown + ("|" if on and int(time.time() * 2) % 2 == 0 else ""), cx + 222, 204, 14,
                     (255, 255, 255) if self.name else (110, 110, 110))
            self.hot.append((cx + 210, 196, 300, 36, lambda: setattr(self, "edit", "name"), None))
            gfx.text("PROFILE PICTURE", cx + 210, 252, 11, (189, 189, 189), bold=True)
            button(cx + 210, 272, 150, 32, "Choose picture", self.pick_pfp)
            button(cx + 372, 272, 110, 32, "Remove", self.clear_pfp)
            if self.pfp_path:
                gfx.text("File: " + Path(self.pfp_path).name[:40], cx + 210, 316, 12, (170, 170, 170))
            if self.pfp_status:
                gfx.text(self.pfp_status[:80], cx + 210, 336, 12, (230, 160, 90))
        elif self.page == "loadout":
            gfx.text("YOUR KIT", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Loadout.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Pick weapon mods and player models. Click a card to equip it on the selected slot / team.",
                     cx, 96, 13, (146, 146, 146))
            wp = self.lo_tab == "weapons"
            button(cx, 124, 150, 32, "Weapon mods", lambda: self.lo_set("weapons"), wp)
            button(cx + 160, 124, 150, 32, "Player models", lambda: self.lo_set("players"), self.lo_tab == "players")
            button(cx + 320, 124, 150, 32, "Arms", lambda: self.lo_set("arms"), self.lo_tab == "arms")
            if self.lo_tab == "arms":
                button(cx + cw - 130, 124, 130, 32, "Add files", lambda: self.pick("arms"))
                button(cx, 166, 150, 30, "No arms", lambda: self.lo_pick(None), g.arms_pick is None)
                gfx.text(("Arms: " + g.arms_pick) if g.arms_pick else "Arms: none (CS:GO guns float without hands)",
                         cx, 204, 12, (170, 170, 170))
                names = sorted(g.arms_all)
                if not names:
                    gfx.text("No arms loaded yet.", cx + cw / 2, 330, 15, (200, 200, 200), bold=True, anchor="c")
                    gfx.text("Use Add files with the arms .zip (or .mdl + .vvd/.vtx/.vtf/.vmt), or drop it on the window.",
                             cx + cw / 2, 356, 12, (130, 130, 130), anchor="c")
                for idx, name in enumerate(names[:12]):
                    r, c = divmod(idx, 3)
                    x, y = cx + c * 232, 226 + r * 74
                    here = g.arms_pick == name
                    hv = hover(x, y, 216, 62)
                    gfx.box(x, y, 216, 62, c255(20, 20, 20) if not hv else c255(27, 27, 27),
                            c255(240, 198, 116) if here else (c255(120, 120, 120) if hv else c255(48, 48, 48)))
                    gfx.text(re.sub(r"\.mdl$", "", name)[:24], x + 10, y + 12, 13, (255, 255, 255), bold=True)
                    gfx.text("equipped" if here else "click to equip", x + 10, y + 36, 11,
                             (240, 198, 116) if here else (100, 100, 100))
                    self.hot.append((x, y, 216, 62, lambda n=name: self.lo_pick(n), None))
            else:
                ws = CFG["weapons"][:3]
                items = g.weapon_mods() if wp else sorted(g.asset["player"].keys())
                per = 6
                pages = max(1, (len(items) + per - 1) // per)
                self.lo_page = max(0, min(self.lo_page, pages - 1))
                add_x = cx + cw - 130
                button(add_x, 124, 130, 32, "Add files", lambda: self.pick("weapon" if wp else "player"))
                nxt_x = add_x - 44
                prv_x = nxt_x - 112
                button(prv_x, 124, 36, 32, "<", lambda: setattr(self, "lo_page", max(0, self.lo_page - 1)))
                button(nxt_x, 124, 36, 32, ">", lambda: setattr(self, "lo_page", min(pages - 1, self.lo_page + 1)))
                gfx.text(f"{self.lo_page + 1} / {pages}", prv_x + 36 + 38, 132, 13, (200, 200, 200), bold=True, anchor="c")

                if wp:
                    self.lo_slot = max(0, min(self.lo_slot, len(ws) - 1)) if ws else 0
                    for i, w in enumerate(ws):
                        button(cx + i * 160, 166, 150, 30, f"{i + 1}  {w.n}"[:18],
                               lambda i=i: setattr(self, "lo_slot", i), self.lo_slot == i)
                    button(cx + 490, 166, 150, 30, "Auto-match", lambda: self.lo_pick(None))
                    cur = ws[self.lo_slot] if ws else None
                    info = ("Slot %d (%s): " % (self.lo_slot + 1, cur.n) + (cur._m or "no model - box")
                            + ("" if self.lo_slot in g.wm_pick else "  (auto)")) if cur else ""
                else:
                    for i, tk in enumerate(("CT", "T")):
                        button(cx + i * 160, 166, 150, 30, "CT team" if tk == "CT" else "T team",
                               lambda tk=tk: setattr(self, "lo_team", tk), self.lo_team == tk)
                    button(cx + 490, 166, 150, 30, "Default box", lambda: self.lo_pick(None))
                    info = f"{self.lo_team} team: " + (g.pm_cfg[self.lo_team] or "default box")
                gfx.text(info[:100], cx, 204, 12, (170, 170, 170))

                gy, cw2, ch2 = 226, 216, 170
                if not items:
                    gfx.text("No weapon mods loaded yet." if wp else "No player models loaded yet.",
                             cx + cw / 2, gy + 120, 15, (200, 200, 200), bold=True, anchor="c")
                    gfx.text("Use Add files, drop .mdl files on the window, or put them in the "
                             + ("weapons" if wp else "player") + " folder.",
                             cx + cw / 2, gy + 146, 12, (130, 130, 130), anchor="c")
                for idx, name in enumerate(items[self.lo_page * per:(self.lo_page + 1) * per]):
                    r, c = divmod(idx, 3)
                    x, y = cx + c * (cw2 + 16), gy + r * (ch2 + 14)
                    if wp:
                        here = bool(ws) and ws[self.lo_slot]._m == name
                        inst = self.pv_weapon(name)
                    else:
                        here = g.pm_cfg[self.lo_team] == name
                        inst = self.pv_player(name)
                    hv = hover(x, y, cw2, ch2)
                    gfx.box(x, y, cw2, ch2, c255(20, 20, 20) if not hv else c255(27, 27, 27),
                            c255(240, 198, 116) if here else (c255(120, 120, 120) if hv else c255(48, 48, 48)))
                    self.draw_card_preview(W, H, x + 2, y + 2, cw2 - 4, 116, "weapon" if wp else "player", inst)
                    gfx.rect(x + 2, y + 118, cw2 - 4, 1, c255(41, 41, 41))
                    stem = re.sub(r"\.mdl$", "", name)
                    if wp:
                        stem = re.sub(r"^v_", "", stem)
                    gfx.text(stem if len(stem) <= 26 else stem[:25] + "~", x + 10, y + 124, 13, (255, 255, 255), bold=True)
                    if wp:
                        tags = [(f"{i + 1}  {w.n}"[:12], (1, 1, 1, .14), (230, 230, 230))
                                for i, w in enumerate(ws) if w._m == name]
                    else:
                        tags = [(tk, c255(80, 140, 255, .25) if tk == "CT" else c255(255, 90, 80, .25),
                                 (80, 140, 255) if tk == "CT" else (255, 90, 80))
                                for tk in ("CT", "T") if g.pm_cfg[tk] == name]
                    bx = x + 10
                    for txt, bg, fg in tags:
                        tw2 = gfx.text_w(txt, 10, True) + 14
                        gfx.rect(bx, y + 144, tw2, 18, bg)
                        gfx.text(txt, bx + tw2 / 2, y + 147, 10, fg, bold=True, anchor="c")
                        bx += tw2 + 6
                    if not tags:
                        gfx.text("click to equip", x + 10, y + 147, 11, (100, 100, 100))
                    self.hot.append((x, y, cw2, ch2, lambda n=name: self.lo_pick(n), None))
        elif self.page == "settings" and self.set_sub == "controls":
            gfx.text("PERSONALIZE YOUR SETUP", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Settings.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Tune mouse sensitivity and remap controls. Click a binding, then press a key or mouse button.",
                     cx, 96, 13, (146, 146, 146))

            panel(cx, 126, cw, 70, "Mouse sensitivity")
            gfx.text(f"{g.sens / BASE_SENS:.2f}x", cx + cw - 20, 14 + 126, 13, (255, 255, 255), bold=True, anchor="r")
            slider(cx + 20, 126 + 40, cw - 40, g.sens, .0005, .01, lambda v: setattr(g, "sens", v))

            panel(cx, 204, cw, 70, "Weapon volume")
            gfx.text(f"{int(g.audio.volume * 100)}%", cx + cw - 20, 14 + 204, 13, (255, 255, 255), bold=True, anchor="r")
            slider(cx + 20, 204 + 40, cw - 40, g.audio.volume, 0, 1, lambda v: setattr(g.audio, "volume", v))

            panel(cx, 282, cw, 312, "Key bindings")
            gfx.text("Mouse 4 / Mouse 5 can be bound too. Esc cancels.", cx + cw - 20, 284 + 14, 11, (119, 119, 119), anchor="r")
            for i, (aid, label) in enumerate(BIND_META):
                col, row = divmod(i, 6)
                rx = cx + 20 + col * 330
                ry = 282 + 44 + row * 38
                gfx.box(rx, ry, 320, 32, c255(17, 17, 17), c255(41, 41, 41))
                gfx.text(label, rx + 12, ry + 8, 12, (230, 230, 230))
                waiting = self.waiting == aid
                bw = 124
                hv = hover(rx + 320 - bw - 6, ry + 3, bw, 26)
                gfx.box(rx + 320 - bw - 6, ry + 3, bw, 26,
                        c255(60, 60, 60) if waiting else (c255(41, 41, 41) if hv else c255(26, 26, 26)),
                        c255(200, 200, 200) if waiting else c255(58, 58, 58))
                gfx.text("Press a key..." if waiting else key_label(g.binds[aid]), rx + 320 - bw / 2 - 6, ry + 8, 12,
                         (255, 255, 255), anchor="c")
                self.hot.append((rx + 320 - bw - 6, ry + 3, bw, 26, lambda a=aid: setattr(self, "waiting", a), None))
            button(cx + 20, 282 + 44 + 6 * 38 + 4, 150, 32, "Reset defaults", self.reset_keys)
            button(cx + 182, 282 + 44 + 6 * 38 + 4, 150, 32, "Back to play", lambda: self.set_page("play"), True)
        elif self.page == "settings" and self.set_sub == "sounds":
            gfx.text("CUSTOM SOUNDS", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Sounds.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Pick your own .wav / .ogg / .mp3 files. Volume follows the weapon volume slider in Settings.",
                     cx, 96, 13, (146, 146, 146))
            rows = (("shot", "Shooting sound", "Plays when you fire a gun, instead of the weapon's own sound (not the knife)."),
                    ("hit", "Hit sound", "Plays when your shot hits an enemy."))
            for i, (kind, title, sub) in enumerate(rows):
                py = 126 + i * 130
                panel(cx, py, cw, 112, title, sub)
                path = getattr(g, f"custom_{kind}_path")
                gfx.text(("File: " + Path(path).name) if path else "Using the default sound", cx + 20, py + 58, 12,
                         (230, 230, 230) if path else (136, 136, 136))
                button(cx + 20, py + 76, 140, 28, "Choose file", lambda k=kind: self.pick_sound(k))
                button(cx + 172, py + 76, 90, 28, "Test",
                       lambda k=kind: g.audio.play(.6, getattr(g, f"custom_{k}")) if getattr(g, f"custom_{k}") else None)
                button(cx + 274, py + 76, 90, 28, "Reset",
                       lambda k=kind: setattr(self, "snd_status", g.set_custom_sound(k, "")))
            if self.snd_status:
                gfx.text(self.snd_status[:110], cx + 20, 396, 12, (170, 170, 170))
        elif self.page == "settings" and self.set_sub == "viewmodel":
            gfx.text("WEAPON POSITION", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Viewmodel.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Move the weapon and hands on your screen. Applies to every weapon.",
                     cx, 96, 13, (146, 146, 146))
            axes = ((0, "X  -  Forward / Back", "Positive pushes it away from you."),
                    (1, "Y  -  Right / Left", "Positive moves it to the right."),
                    (2, "Z  -  Up / Down", "Positive moves it up."))
            lw = 340
            for i, (ax, title, sub) in enumerate(axes):
                py = 126 + i * 92
                panel(cx, py, lw, 80, title, sub)
                gfx.text(f"{g.vm_adj[ax]:+.1f}", cx + lw - 20, py + 14, 13, (255, 255, 255), bold=True, anchor="r")
                slider(cx + 20, py + 50, lw - 40, g.vm_adj[ax], -15, 15,
                       lambda v, a=ax: g.vm_adj.__setitem__(a, round(v * 2) / 2))
            button(cx + 20, 126 + 3 * 92 + 8, 150, 32, "Reset position",
                   lambda: g.vm_adj.__setitem__(slice(0, 3), [0.0, 0.0, 0.0]))

            # live preview + weapon dropdown
            rx, rw = cx + 356, 324
            rh = 182
            gfx.box(rx - 2, 124, rw + 4, rh + 4, c255(20, 20, 20), c255(58, 58, 58))
            self.draw_preview(W, H, rx, 126, rw, rh)
            gfx.rect(rx + rw / 2 - 8, 126 + rh / 2, 16, 1, (1, 1, 1, .45))
            gfx.rect(rx + rw / 2, 126 + rh / 2 - 8, 1, 16, (1, 1, 1, .45))
            gfx.text("PREVIEW WEAPON", rx, 126 + rh + 14, 11, (189, 189, 189), bold=True)
            ws = CFG["weapons"]
            self.vm_prev = max(0, min(self.vm_prev, len(ws) - 1)) if ws else 0
            cur = ws[self.vm_prev] if ws else None
            ddy = 126 + rh + 34
            label = (str(cur.n) if cur and cur.n else "Weapon") + ("" if (cur and cur.vm) else "  (no model - box)")
            button(rx, ddy, rw, 32, label[:34] + ("   ^" if self.vm_dd else "   v"),
                   lambda: setattr(self, "vm_dd", not self.vm_dd))
            if self.vm_dd:
                for i, wp in enumerate(ws):
                    iy = ddy + 34 + i * 30
                    hv2 = hover(rx, iy, rw, 28)
                    gfx.box(rx, iy, rw, 28, c255(60, 60, 60) if i == self.vm_prev else (c255(41, 41, 41) if hv2 else c255(26, 26, 26)),
                            c255(85, 85, 85))
                    gfx.text(str(wp.n), rx + 12, iy + 6, 12, (238, 238, 238))
                    self.hot.append((rx, iy, rw, 28,
                                     lambda i=i: (setattr(self, "vm_prev", i), setattr(self, "vm_dd", False)), None))
        elif self.page == "settings" and self.set_sub == "crosshair":
            xh = g.xh
            gfx.text("AIM", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Crosshair.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Build your own crosshair, like in CS:GO. Changes show in the preview right away.",
                     cx, 96, 13, (146, 146, 146))
            panel(cx, 126, 340, 440, "Crosshair style")
            rows = (("Size", "size", 0, 20, "%d"), ("Thickness", "thick", 1, 6, "%d"),
                    ("Gap", "gap", -5, 15, "%d"), ("Red", "r", 0, 255, "%d"),
                    ("Green", "g", 0, 255, "%d"), ("Blue", "b", 0, 255, "%d"),
                    ("Opacity", "a", 0.1, 1.0, "%.2f"))
            for i, (lab, key, lo, hi, fmt) in enumerate(rows):
                yy = 170 + i * 52
                gfx.text(lab, cx + 20, yy, 12, (230, 230, 230))
                gfx.text(fmt % xh[key], cx + 320, yy, 13, (255, 255, 255), bold=True, anchor="r")
                if key == "a":
                    setter = lambda v, k=key: xh.__setitem__(k, round(v, 2))
                else:
                    setter = lambda v, k=key: xh.__setitem__(k, int(round(v)))
                slider(cx + 20, yy + 20, 300, xh[key], lo, hi, setter)

            rx, rw = cx + 356, 324
            gfx.box(rx - 2, 124, rw + 4, 204, c255(20, 20, 20), c255(58, 58, 58))
            gfx.rect(rx, 126, rw, 200, c255(88, 98, 110))
            draw_crosshair(gfx, rx + rw / 2, 126 + 100, xh)
            gfx.text("PREVIEW", rx, 336, 11, (189, 189, 189), bold=True)

            def flip(k):
                xh[k] = 0 if xh[k] else 1
            button(rx, 360, 158, 32, "Outline: ON" if xh["outline"] else "Outline: OFF",
                   lambda: flip("outline"), bool(xh["outline"]))
            button(rx + 166, 360, 158, 32, "Center dot: ON" if xh["dot"] else "Center dot: OFF",
                   lambda: flip("dot"), bool(xh["dot"]))
            gfx.text("COLOR PRESETS", rx, 410, 11, (189, 189, 189), bold=True)
            for i, (lab, c) in enumerate((("Green", (0, 255, 0)), ("Cyan", (0, 255, 255)),
                                          ("Yellow", (255, 255, 0)), ("White", (255, 255, 255)),
                                          ("Red", (255, 50, 50)))):
                button(rx + i * 66, 430, 60, 30, lab,
                       lambda c=c: xh.update(r=c[0], g=c[1], b=c[2]))
            button(rx, 480, 150, 32, "Reset crosshair", lambda: xh.update(DEFAULT_XH))
        else:
            gfx.text("DISPLAY", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Video.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Pick a screen shape. The game is drawn in that shape, then stretched to fill your window.",
                     cx, 96, 13, (146, 146, 146))
            panel(cx, 126, cw, 150, "Aspect ratio",
                  "Stretched 4:3 makes players and weapons look wider, like the old stretched-res trick.")
            opts = ((0.0, "Window (default)"), (16 / 9, "16:9"), (16 / 10, "16:10"), (4 / 3, "4:3"))
            for i, (val, lab) in enumerate(opts):
                bx = cx + 20 + i * 162
                button(bx, 190, 150, 44, lab, lambda v=val: setattr(g, "aspect", v), abs(g.aspect - val) < 1e-6)
            now = "Window (default)" if not g.aspect else next((l for v, l in opts if abs(g.aspect - v) < 1e-6), "Custom")
            gfx.text("Now: " + now, cx + 20, 248, 12, (170, 170, 170))
        gl.glPopMatrix()

    def set_pfp(self, path):
        try:
            surf = pygame.image.load(str(path)).convert_alpha()
            w, h = surf.get_size()
            s = min(w, h)
            crop = surf.subsurface(((w - s) // 2, (h - s) // 2, s, s)).copy()
            crop = pygame.transform.smoothscale(crop, (256, 256))
            arr = np.frombuffer(pygame.image.tostring(crop, "RGBA", False), np.uint8).reshape(256, 256, 4)
            tid = make_texture(arr, 256, 256)
        except Exception as ex:
            self.pfp_status = "Cannot load that image: " + str(ex)[:50]
            return False
        if self.pfp_tex:
            gl.glDeleteTextures([self.pfp_tex])
        self.pfp_tex = tid
        self.pfp_path = str(path)
        self.pfp_status = ""
        return True

    def pick_pfp(self):
        paths = self.pick_files("image")
        if paths and self.set_pfp(paths[0]):
            save_settings(self.g, self)

    def clear_pfp(self):
        if self.pfp_tex:
            gl.glDeleteTextures([self.pfp_tex])
        self.pfp_tex = None
        self.pfp_path = ""
        self.pfp_status = ""
        save_settings(self.g, self)

    def reset_keys(self):
        self.g.binds.clear()
        self.g.binds.update(DEFAULT_BINDS)
        self.waiting = None

    def set_sub_tab(self, sub):
        self.set_sub = sub
        self.vm_dd = False
        self.waiting = None

    def set_page(self, p):
        if p == "online":           # the old Online tab lives inside Play now
            p = "play"
        self.page = p
        self.vm_dd = False
        self.waiting = None
        self.focus = False

    # --- loadout helpers ---
    def lo_set(self, tab):
        self.lo_tab = tab
        self.lo_page = 0

    def wheel(self, dy):
        if self.page == "loadout" and dy:
            self.lo_page = max(0, self.lo_page + (-1 if dy > 0 else 1))

    def lo_pick(self, name):
        g = self.g
        if self.lo_tab == "arms":
            g.set_arms(name)
            self.status = "Arms: " + (name or "none")
        elif self.lo_tab == "weapons":
            g.set_weapon_pick(self.lo_slot, name)
            self.status = (g.vm_msg or "").strip() or self.status
        else:
            k = self.lo_team
            g.pm_cfg[k] = name
            g.pm_saved[k] = name
            for e in g.ents:
                if not e.isP:
                    g.build_entity_model(e)
        save_settings(g, self)

    def pv_weapon(self, name):
        g = self.g
        buf = g.asset["mdl"].get(name)
        if buf is None:
            return None
        k = ("w", name, len(buf), g.arms_pick)
        if k not in self.pv:
            try:
                inst = make_vm(buf, g.asset["pack"].get(key_norm(name)), g.src_arms)
                idle = inst.find("idle")
                inst.last = None
                inst.pose(idle[0] if idle else (0 if inst.seqs else -1), 0)
            except Exception as ex:
                print("Preview error:", name, ex)
                inst = None
            self.pv[k] = inst
        return self.pv[k]

    def pv_player(self, name):
        g = self.g
        buf = g.asset["player"].get(name)
        if buf is None:
            return None
        k = ("p", name, len(buf))
        if k not in self.pv:
            try:
                data = g.pm_cache.get(name)
                if data is None:
                    pack = g.asset["pack"].get(key_norm(re.sub(r"\.mdl$", "", name)))
                    data = g.pm_cache[name] = MdlData(buf, pack)
                inst = MdlInst(data)
                idle = inst.find("idle")
                if idle:
                    seq = idle[0]
                else:
                    valid = [i for i, sq in enumerate(inst.seqs) if not re.search(r"crouch|die|death", sq["label"], re.I)]
                    seq = valid[0] if valid else 0
                inst.last = None
                inst.pose(seq, 0, inst.base_seq, 0)
            except Exception as ex:
                print("Preview error:", name, ex)
                inst = None
            self.pv[k] = inst
        return self.pv[k]

    def _bbox(self, inst):
        bb = self.pv_bb.get(id(inst))
        if bb is None:
            mn, mx = np.full(3, 1e9), np.full(3, -1e9)
            for a in inst.pos:
                if a is not None and len(a):
                    mn = np.minimum(mn, a.min(axis=0))
                    mx = np.maximum(mx, a.max(axis=0))
            if mn[0] > mx[0]:
                mn, mx = np.zeros(3), np.ones(3)
            bb = self.pv_bb[id(inst)] = (mn, mx)
        return bb

    def draw_card_preview(self, W, H, px, py, pw, ph, kind, inst):
        """Auto-framed 3D preview of a weapon (side view, gentle sway) or a player (turntable)."""
        g = self.g
        ox, oy, s = self.xf
        vx, vy = int(round(ox + px * s)), int(round(H - (oy + (py + ph) * s)))
        vw, vh = max(1, int(round(pw * s))), max(1, int(round(ph * s)))
        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glScissor(vx, vy, vw, vh)
        gl.glViewport(vx, vy, vw, vh)
        gl.glClearColor(.075, .078, .09, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        if inst is not None:
            mn, mx = self._bbox(inst)
            c = (mn + mx) / 2
            size = np.maximum(mx - mn, 1.0)
            fov = 32.0
            aspect = vw / vh
            half = math.radians(fov / 2)
            half_h = math.atan(math.tan(half) * aspect)
            t = time.time()
            gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDisable(gl.GL_BLEND)
            gl.glMatrixMode(gl.GL_PROJECTION)
            gl.glPushMatrix()
            gl.glLoadIdentity()
            glu.gluPerspective(fov, aspect, 1, 5000)
            gl.glMatrixMode(gl.GL_MODELVIEW)
            gl.glPushMatrix()
            gl.glLoadIdentity()
            if kind == "player":
                d = max(size[2] / 2 * 1.12 / math.tan(half),
                        math.hypot(size[0], size[1]) / 2 * 1.1 / math.tan(half_h))
                glu.gluLookAt(d, 0, c[2], 0, 0, c[2], 0, 0, 1)
                gl.glRotatef((t * 40) % 360, 0, 0, 1)
                gl.glTranslatef(-c[0], -c[1], 0)
            else:
                d = max(math.hypot(size[0], size[1]) / 2 * 1.05 / math.tan(half_h),
                        size[2] / 2 * 1.25 / math.tan(half)) + size[1] * .25
                glu.gluLookAt(c[0], c[1] - d, c[2], c[0], c[1], c[2], 0, 0, 1)
                gl.glTranslatef(c[0], c[1], c[2])
                gl.glRotatef(math.sin(t * .9) * 28, 0, 0, 1)
                if inst.is_mirrored():
                    gl.glScalef(1, -1, 1)
                gl.glTranslatef(-c[0], -c[1], -c[2])
            inst.draw()
            gl.glPopMatrix()
            gl.glMatrixMode(gl.GL_PROJECTION)
            gl.glPopMatrix()
            gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glViewport(0, 0, W, H)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glColor4f(1, 1, 1, 1)

    def draw_preview(self, W, H, px, py, pw, ph):
        """3D viewmodel preview inside the menu rectangle (menu units)."""
        g = self.g
        ox, oy, s = self.xf
        vx, vy = int(round(ox + px * s)), int(round(H - (oy + (py + ph) * s)))
        vw, vh = max(1, int(round(pw * s))), max(1, int(round(ph * s)))
        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glScissor(vx, vy, vw, vh)
        gl.glViewport(vx, vy, vw, vh)
        gl.glClearColor(0x8a / 255, 0x9b / 255, 0xb0 / 255, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glPushMatrix()
        gl.glLoadIdentity()
        glu.gluPerspective(75, g.aspect or vw / vh, VM_NEAR, VM_FAR)
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glPushMatrix()
        gl.glLoadIdentity()
        glu.gluLookAt(0, 0, 0, 1, 0, 0, 0, 0, 1)
        ws = CFG["weapons"]
        if ws:
            w = ws[max(0, min(self.vm_prev, len(ws) - 1))]
            if w.vm:
                idle = w.vm.find("idle")
                if idle and 0 <= idle[0] < len(w.vm.seqs):
                    sq = w.vm.seqs[idle[0]]
                    f = (time.time() * sq["fps"]) % sq["n"]
                    w.vm.pose(idle[0], min(sq["n"] - 1, round(f * 16) / 16))
                o = w.vm_ofs or (0, 0, 0)
                gl.glTranslatef(o[0] + g.vm_adj[0], o[1] - g.vm_adj[1], o[2] + g.vm_adj[2])
                flip = bool(w.vm_flip) != bool(g.hand_swap)
                if flip:
                    gl.glScalef(1, -1, 1)
                    gl.glFrontFace(gl.GL_CW)
                w.vm.draw()
                if flip:
                    gl.glFrontFace(gl.GL_CCW)
            else:
                gl.glTranslatef(g.vm_adj[0], -g.vm_adj[1], g.vm_adj[2])
                draw_box(12, -5, -5, 8, 1.25, 1.75, (0x2b / 255, 0x2b / 255, 0x2b / 255))
        gl.glPopMatrix()
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glPopMatrix()
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glViewport(0, 0, W, H)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glColor4f(1, 1, 1, 1)

    def pick_sound(self, kind):
        paths = self.pick_files("sound")
        if paths:
            self.snd_status = self.g.set_custom_sound(kind, paths[0])

    def pick(self, mode):
        paths = self.pick_files(mode)
        if paths:
            self.status = self.g.load_files(paths, "player" if mode == "player" else "weapon") or self.status
            if mode == "arms":
                self.lo_page = 0


# --------------------------------------------------------------------------
# Renderer (3D scene + HUD)
# --------------------------------------------------------------------------
def draw_box(cx, cy, cz, hx, hy, hz, color):
    tex2d(False)
    gl.glColor3f(*color)
    x0, x1, y0, y1, z0, z1 = cx - hx, cx + hx, cy - hy, cy + hy, cz - hz, cz + hz
    gl.glBegin(gl.GL_QUADS)
    for q in (((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),
              ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)),
              ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),
              ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0)),
              ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)),
              ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))):
        for v in q:
            gl.glVertex3f(*v)
    gl.glEnd()
    gl.glColor4f(1, 1, 1, 1)
    tex2d(True)


def render_scene(game, W, H):
    P = game.P
    gl.glViewport(0, 0, W, H)
    FX.init()
    gl.glClearColor(*FOG_COLOR, 1)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    gl.glEnable(gl.GL_DEPTH_TEST)
    gl.glDisable(gl.GL_BLEND)
    gl.glMatrixMode(gl.GL_PROJECTION)
    gl.glLoadIdentity()
    glu.gluPerspective(game.fov, game.aspect or W / max(1, H), 1, 10000)
    gl.glMatrixMode(gl.GL_MODELVIEW)
    gl.glLoadIdentity()

    al = min(1.0, P.acc / TICK)
    ex = P.px + (P.x - P.px) * al
    ey = P.py + (P.y - P.py) * al
    ez = P.pz + (P.z - P.pz) * al + 17 + P.so
    vp = max(-1.5, min(1.5, P.pitch + P.pun_p))   # view = aim + recoil punch
    vy = P.yaw + P.pun_y
    cp = math.cos(vp)
    fx, fy, fz = cp * math.cos(vy), cp * math.sin(vy), math.sin(vp)
    glu.gluLookAt(ex, ey, ez, ex + fx, ey + fy, ez + fz, 0, 0, 1)

    FX.begin(0)
    game.world.draw()

    for e in game.ents:
        if e.isP:
            continue
        if not e.alive and not (e.pm and e.anim.get("ds", -1) >= 0):
            continue  # no death sequence to show: the body just vanishes
        ddx, ddy = e.x - ex, e.y - ey
        if ddx * ddx + ddy * ddy > 90000 and ddx * fx + ddy * fy < 0:
            continue  # behind the camera: don't draw
        if e.pm:
            gy = e.anim.get("gyaw")
            gl.glPushMatrix()
            gl.glTranslatef(e.x, e.y, e.z + e.pm_dz)
            gl.glRotatef(gy if gy is not None else math.degrees(e.yaw), 0, 0, 1)  # legs yaw; torso twists on top
            e.pm.draw()
            gl.glPopMatrix()
        else:
            gl.glPushMatrix()
            gl.glTranslatef(e.x, e.y, e.z)
            gl.glRotatef(math.degrees(e.yaw), 0, 0, 1)
            body = (0xd0 / 255, 0x40 / 255, 0x30 / 255) if e.team else (0x30 / 255, 0x70 / 255, 0xe0 / 255)
            swing = math.sin(e.ph) * e.bob * 45 * (1 - e.air)
            lift = abs(math.sin(e.ph)) * e.bob * 1.5 - e.land * 5
            dark = tuple(c * .65 for c in body)
            gl.glTranslatef(0, 0, lift)
            draw_box(0, 0, 4, 7, 12, 16, body)                       # torso
            draw_box(0, 0, 27, 7, 7, 7, (0xe8 / 255, 0xc9 / 255, 0xa0 / 255))  # head
            for side, sg in ((-1, 1), (1, -1)):
                # legs (pivot at hip), arms swing opposite to the leg on the same side
                gl.glPushMatrix()
                gl.glTranslatef(0, side * 5, -12 - lift)
                gl.glRotatef(swing * sg + e.air * (35 if sg == 1 else -45), 0, 1, 0)  # jump: legs split
                draw_box(0, 0, -12, 4, 4, 12, dark)
                gl.glPopMatrix()
                gl.glPushMatrix()
                gl.glTranslatef(0, side * 15, 18)
                gl.glRotatef(-swing * sg - e.air * 70, 0, 1, 0)  # jump: arms raised
                draw_box(0, 0, -11, 3, 3, 11, body)
                gl.glPopMatrix()
            gl.glPopMatrix()

    if game.tracers:
        tex2d(False)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDepthMask(gl.GL_FALSE)
        gl.glBegin(gl.GL_LINES)
        for t in game.tracers:
            gl.glColor4f(1, .86, .63, t["al"])
            gl.glVertex3f(*t["a"])
            gl.glVertex3f(*t["b"])
        gl.glEnd()
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        tex2d(True)
        gl.glColor4f(1, 1, 1, 1)

    # viewmodel pass (depth cleared, camera at origin looking +X).
    # Own projection with a tiny near plane so close parts of the gun are not clipped.
    FX.end()
    FX.begin(1)
    gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
    gl.glMatrixMode(gl.GL_PROJECTION)
    gl.glPushMatrix()
    gl.glLoadIdentity()
    glu.gluPerspective(game.fov, game.aspect or W / max(1, H), VM_NEAR, VM_FAR)
    gl.glMatrixMode(gl.GL_MODELVIEW)
    gl.glLoadIdentity()
    glu.gluLookAt(0, 0, 0, 1, 0, 0, 0, 0, 1)
    hide = P.zoom and P.w == 0
    w = CFG["weapons"][P.w]
    gl.glPushMatrix()
    gl.glTranslatef(0, math.sin(P.ph) * .7 * P.bob,
                    -abs(math.sin(P.ph)) * .9 * P.bob + P.air * .7 - P.land * 2.2)
    if not hide and P.alive:
        if w.vm:
            gl.glPushMatrix()
            o = w.vm_ofs or (0, 0, 0)
            gl.glTranslatef(o[0] + game.vm_adj[0], o[1] - game.vm_adj[1], o[2] + game.vm_adj[2])
            flip = bool(w.vm_flip) != bool(game.hand_swap)  # mirrored model and/or V key
            if flip:
                gl.glScalef(1, -1, 1)
                gl.glFrontFace(gl.GL_CW)
            w.vm.draw()
            if flip:
                gl.glFrontFace(gl.GL_CCW)
            gl.glPopMatrix()
        else:
            draw_box(12, -5, -5, 8, 1.25, 1.75, (0x2b / 255, 0x2b / 255, 0x2b / 255))
    gl.glPopMatrix()
    gl.glMatrixMode(gl.GL_PROJECTION)
    gl.glPopMatrix()
    gl.glMatrixMode(gl.GL_MODELVIEW)
    FX.end()
    FX.post()


_SPRAY_UP = [(0, 0), (1, .3), (3, 1.5), (5, 3.2), (8, 5.8), (10, 7.2), (15, 9.5), (20, 10.6), (30, 11.5)]
_SPRAY_RIGHT = [(0, 0), (6, 0), (10, -1.2), (14, -2.6), (18, -1.0), (22, 1.6), (26, .2), (30, -1.8)]


def _interp(tbl, n):
    if n <= tbl[0][0]:
        return tbl[0][1]
    for (a, va), (b, vb) in zip(tbl, tbl[1:]):
        if n <= b:
            return va + (vb - va) * (n - a) / (b - a)
    return tbl[-1][1]


def spray_pattern(n):
    """AK-47 style spray (like CS:GO): climbs straight up, pulls left, swings right, then left again.
    Returns the (pitch up, yaw) view offset in radians after n bullets."""
    up = math.radians(_interp(_SPRAY_UP, n))
    right = math.radians(_interp(_SPRAY_RIGHT, n))
    return up, -right   # yaw grows to the left in this game


def draw_crosshair(gfx, cx, cy, xh):
    """CS:GO style crosshair: four lines with a gap, optional centre dot and black outline."""
    s, t, gap, a = xh["size"], xh["thick"], xh["gap"], xh["a"]
    cx, cy = math.floor(cx), math.floor(cy)
    h = t / 2.0
    rects = []
    if s > 0:
        rects += [(cx - gap - s, cy - h, s, t), (cx + gap, cy - h, s, t),
                  (cx - h, cy - gap - s, t, s), (cx - h, cy + gap, t, s)]
    if xh["dot"]:
        rects.append((cx - h, cy - h, t, t))
    if xh["outline"]:
        for x, y, w, hh in rects:
            gfx.rect(x - 1, y - 1, w + 2, hh + 2, (0, 0, 0, a))
    col = (xh["r"] / 255, xh["g"] / 255, xh["b"] / 255, a)
    for x, y, w, hh in rects:
        gfx.rect(x, y, w, hh, col)


def draw_scoreboard(game, gfx, W, H):
    """Hold Tab: both teams, sorted by kills, with K / D."""
    colw = min(430, (W - 60) / 2)
    gap = 20
    rows = max(sum(1 for e in game.ents if e.team == 0), sum(1 for e in game.ents if e.team == 1))
    rh = 26
    top = max(60, H / 2 - (rows * rh + 110) / 2)
    total_w = colw * 2 + gap
    x0 = W / 2 - total_w / 2
    gfx.rect(x0 - 12, top - 12, total_w + 24, rows * rh + 110, (.03, .04, .06, .82))
    gfx.text("SCOREBOARD", W / 2, top - 4, 12, (150, 150, 150), bold=True, anchor="c")
    for team in (0, 1):
        x = x0 + team * (colw + gap)
        col = (80, 140, 255) if team == 0 else (255, 90, 80)
        gfx.rect(x, top + 22, colw, 34, (col[0] / 255, col[1] / 255, col[2] / 255, .25))
        gfx.text("COUNTER-TERRORISTS" if team == 0 else "TERRORISTS", x + 10, top + 30, 15, col, bold=True)
        gfx.text(str(game.score[team]), x + colw - 10, top + 28, 22, (255, 255, 255), bold=True, anchor="r")
        y = top + 62
        gfx.text("PLAYER", x + 10, y, 11, (140, 140, 140), bold=True)
        gfx.text("K", x + colw - 70, y, 11, (140, 140, 140), bold=True, anchor="r")
        gfx.text("D", x + colw - 12, y, 11, (140, 140, 140), bold=True, anchor="r")
        y += 20
        members = sorted((e for e in game.ents if e.team == team),
                         key=lambda e: (-e.kills, e.deaths, e.name))
        for e in members:
            if e.isP:
                gfx.rect(x, y - 2, colw, rh - 2, (1, 1, 1, .12))
            c = (240, 198, 116) if e.isP else ((255, 255, 255) if e.alive else (130, 130, 130))
            gfx.text(e.name or "UNKNOWN", x + 10, y, 14, c, bold=bool(e.isP))
            gfx.text(str(e.kills), x + colw - 70, y, 14, c, anchor="r")
            gfx.text(str(e.deaths), x + colw - 12, y, 14, c, anchor="r")
            y += rh


def render_hud(game, gfx, W, H, menu_visible):
    P = game.P
    gl.glMatrixMode(gl.GL_PROJECTION)
    gl.glLoadIdentity()
    gl.glOrtho(0, W, H, 0, -1, 1)
    gl.glMatrixMode(gl.GL_MODELVIEW)
    gl.glLoadIdentity()
    gl.glDisable(gl.GL_DEPTH_TEST)
    gl.glEnable(gl.GL_BLEND)
    gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

    # scope overlay
    if P.zoom and P.w == 0 and P.alive:
        r = .4 * min(W, H)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor4f(0, 0, 0, 1)
        gl.glBegin(gl.GL_TRIANGLE_STRIP)
        R2 = math.hypot(W, H)
        for i in range(0, 65):
            a = i / 64 * math.tau
            gl.glVertex2f(W / 2 + math.cos(a) * r, H / 2 + math.sin(a) * r)
            gl.glVertex2f(W / 2 + math.cos(a) * R2, H / 2 + math.sin(a) * R2)
        gl.glEnd()
        gfx.rect(0, H / 2 - 1, W, 2, (0, 0, 0, .8))
        gfx.rect(W / 2 - 1, 0, 2, H, (0, 0, 0, .8))
    elif P.alive:
        draw_crosshair(gfx, W / 2, H / 2, game.xh)

    # score
    sc = game.score
    a = gfx.text(f"CT {sc[0]}", W / 2 - 14, 18, 20, (80, 140, 255), bold=True, anchor="r")
    gfx.text("-", W / 2, 18, 20, (255, 255, 255), bold=True, anchor="c")
    gfx.text(f"{sc[1]} T", W / 2 + 14, 18, 20, (255, 90, 80), bold=True)
    gfx.text(f"(limit {CFG['frags']})", W / 2, 44, 12, (200, 200, 200), anchor="c")
    if game.over:
        gfx.text(("Blue" if sc[0] > sc[1] else "Red") + " team wins!", W / 2, 62, 20, (255, 255, 255), bold=True, anchor="c")

    if game.net.active:
        gfx.text("ROOM  %s  |  %d players" % (game.net.room[:24], len(game.net.players) + 1), 14, 14, 12, (170, 170, 170), bold=True)

    # Minimal health indicator: only a green bar, no number, panel, background, or cross.
    hp_value = max(0, min(100, math.ceil(P.hp / 2)))
    hp_ratio = hp_value / 100.0
    bar_x = max(28, W / 2 - 300)  # move health bar farther left
    bar_y = H - 51
    bar_w, bar_h = 110, 8
    if P.alive and hp_ratio > 0:
        gfx.rect(bar_x, bar_y, bar_w * hp_ratio, bar_h, (0.25, 0.78, 0.34, 0.98))
    w = CFG["weapons"][P.w]
    gfx.text(w.n, W - 22, H - 80 if not w.melee else H - 50, 22, (255, 255, 255), bold=True, anchor="r")
    if not w.melee:
        gfx.text(f"{P.ammo[P.w]} / {w.mag}" + (" (reloading)" if P.cool > 1 else ""), W - 22, H - 50, 22,
                 (255, 255, 255), bold=True, anchor="r")

    # kill feed (same look as the scoreboard: dark panel, team-tinted name bands)
    y = 18
    rh = 26
    for en in game.feed:
        label = en["weapon"]
        kw = gfx.text_w(en["killer"], 14, en["kl"]) + 20
        ww = gfx.text_w(label, 11, True) + 24
        vw = gfx.text_w(en["victim"], 14, en["vl"]) + 20
        tw = kw + ww + vw
        x = W - 18 - tw
        gfx.rect(x - 4, y - 4, tw + 8, rh + 8, (.03, .04, .06, .82))
        for sx, sw, team, mine in ((x, kw, en["kt"], en["kl"]), (x + kw + ww, vw, en["vt"], en["vl"])):
            col = (80, 140, 255) if team == 0 else (255, 90, 80)
            gfx.rect(sx, y, sw, rh, (col[0] / 255, col[1] / 255, col[2] / 255, .25))
            if mine:
                gfx.rect(sx, y, sw, rh, (1, 1, 1, .12))
        gfx.text(en["killer"], x + 10, y + 5, 14, (240, 198, 116) if en["kl"] else (255, 255, 255), bold=en["kl"])
        gfx.text(label, x + kw + ww / 2, y + 8, 11, (140, 140, 140), bold=True, anchor="c")
        gfx.text(en["victim"], x + kw + ww + 10, y + 5, 14, (240, 198, 116) if en["vl"] else (255, 255, 255), bold=en["vl"])
        y += rh + 12

    # kill streak counter (under the kill feed)
    if P.alive:
        ky = 18 + 5 * 38 + 8   # fixed spot: just below the room for 5 kill feed rows
        gfx.rect(W - 18 - 150, ky - 4, 150, 30, (.03, .04, .06, .82))
        gfx.text("KILL STREAK", W - 18 - 140, ky + 5, 11, (140, 140, 140), bold=True)
        gfx.text(str(game.streak), W - 18 - 10, ky + 2, 20, (240, 198, 116), bold=True, anchor="r")

    # Health vignette removed: no red border at low HP.

    # death screen (same look as the scoreboard)
    if not P.alive:
        gfx.rect(0, 0, W, H, (.03, .04, .06, .45))
        pw, ph = 380, 168
        px, py = W / 2 - pw / 2, H / 2 - ph / 2
        gfx.rect(px - 12, py - 12, pw + 24, ph + 24, (.03, .04, .06, .82))
        gfx.text("AKB / ARENA", W / 2, py - 4, 12, (150, 150, 150), bold=True, anchor="c")
        col = (80, 140, 255) if P.team == 0 else (255, 90, 80)
        gfx.rect(px, py + 22, pw, 34, (col[0] / 255, col[1] / 255, col[2] / 255, .25))
        gfx.text("ELIMINATED", px + 10, py + 30, 15, col, bold=True)
        gfx.text(f"{P.kills} / {P.deaths}", px + pw - 10, py + 28, 22, (255, 255, 255), bold=True, anchor="r")
        gfx.text("KILLED BY", px + 10, py + 66, 11, (140, 140, 140), bold=True)
        gfx.rect(px, py + 84, pw, 24, (1, 1, 1, .12))
        gfx.text(game.death_killer, px + 10, py + 88, 14, (240, 198, 116), bold=True)
        gfx.text(f"RESPAWNING IN {max(1, math.ceil(P.resp))}", W / 2, py + 130, 12, (150, 150, 150), bold=True, anchor="c")

    # scoreboard (hold Tab)
    if game.keys.get("tab") or game.over:
        draw_scoreboard(game, gfx, W, H)
    gl.glColor4f(1, 1, 1, 1)
    gl.glEnable(gl.GL_TEXTURE_2D)


# --------------------------------------------------------------------------
# Settings persistence
# --------------------------------------------------------------------------
def load_settings(game, menu):
    try:
        j = json.loads(SETTINGS_FILE.read_text())
        b = j.get("binds", {})
        for k in DEFAULT_BINDS:
            if isinstance(b.get(k), str):
                game.binds[k] = b[k]
        game.sens = float(j.get("sens", BASE_SENS))
        game.hand_swap = bool(j.get("hand_swap", False))
        game.bots_on = bool(j.get("bots_on", True))
        aj = j.get("arms")
        if aj == "":
            game.arms_off = True
        elif isinstance(aj, str):
            game.arms_want = aj
        pmj = j.get("player_models", {})
        for tk in ("CT", "T"):
            if isinstance(pmj.get(tk), str):
                game.pm_saved[tk] = pmj[tk]
        ar = float(j.get("aspect", 0) or 0)
        game.aspect = ar if 1.0 <= ar <= 2.5 else 0.0
        vo = j.get("vm_offset")
        if isinstance(vo, list) and len(vo) == 3:
            game.vm_adj = [max(-15.0, min(15.0, float(x))) for x in vo]
        for kind in ("shot", "hit"):
            sp = j.get(kind + "_sound")
            if sp and Path(sp).is_file():
                game.set_custom_sound(kind, sp)
        game.audio.volume = float(j.get("volume", .25))
        wmj = j.get("weapon_models", {})
        if isinstance(wmj, dict):
            game.wm_pick = {int(k): v for k, v in wmj.items() if str(k).isdigit() and isinstance(v, str)}
        menu.name = str(j.get("name", ""))[:16]
        xj = j.get("crosshair")
        if isinstance(xj, dict):
            for k, dv in DEFAULT_XH.items():
                try:
                    game.xh[k] = type(dv)(xj[k])
                except Exception:
                    pass
        pf = j.get("pfp")
        if pf and Path(pf).is_file():
            menu.set_pfp(pf)
    except Exception:
        pass


def save_settings(game, menu):
    try:
        SETTINGS_FILE.write_text(json.dumps({
            "arms": (game.arms_pick or ("" if game.arms_off else None)), "binds": game.binds, "hand_swap": game.hand_swap, "bots_on": game.bots_on, "player_models": game.pm_saved, "weapon_models": {str(k): v for k, v in game.wm_pick.items()}, "aspect": game.aspect, "vm_offset": game.vm_adj,
            "shot_sound": game.custom_shot_path, "hit_sound": game.custom_hit_path, "sens": game.sens, "volume": game.audio.volume, "name": menu.name, "pfp": menu.pfp_path, "crosshair": game.xh,
        }, indent=2))
    except Exception:
        pass


def file_dialog(mode):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return []
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    ft = {
        "map": [("GoldSrc map / WAD", "*.bsp *.wad")],
        "arms": [("CS:GO arms", "*.zip *.7z *.mdl *.vpk *.vvd *.vtx *.vtf *.vmt")],
        "weapon": [("Weapon files", "*.mdl *.wav *.json *.vpk *.zip *.7z *.vvd *.vtx *.vtf *.vmt")],
        "player": [("Player models", "*.mdl")],
        "sound": [("Sound files", "*.wav *.ogg *.mp3")],
        "image": [("Images", "*.png *.jpg *.jpeg *.bmp *.gif")],
    }[mode]
    try:
        paths = filedialog.askopenfilenames(title="Select files", filetypes=ft + [("All files", "*.*")])
    finally:
        root.destroy()
    return list(paths)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    pygame.init()
    try:
        pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 1)
        pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 4)
    except Exception:
        pass
    flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.RESIZABLE
    try:
        pygame.display.set_mode((1280, 720), flags, vsync=1)
    except Exception:
        pygame.display.set_mode((1280, 720), flags)
    pygame.display.set_caption("AKB / ARENA - Browser TDM")
    pygame.font.init()

    audio = Audio()
    game = Game(audio)
    gfx = Gfx()
    NET.start_discovery()
    NET.start_broker()

    def launch():
        menu.focus = False
        game.player_name = menu.name.strip() or "Player"
        game.P.name = game.player_name
        set_lock(True)

    def set_lock(v):
        game.lock = v
        pygame.event.set_grab(v)
        pygame.mouse.set_visible(not v)
        try:
            pygame.mouse.set_relative_mode(v)
        except Exception:
            pass
        pygame.mouse.get_rel()
        if not v:
            game.keys.clear()
            menu.set_page("play")

    menu = Menu(game, gfx, launch, file_dialog)
    load_settings(game, menu)
    game.set_world(ArenaWorld())
    game.player_name = menu.name.strip() or "Player"
    game.P.name = game.player_name

    # Auto-load weapon mods (.mdl / .wav / .json) from the "weapons" folder next to this script.
    try:
        wdir = Path(__file__).with_name("weapons")
        wdir.mkdir(exist_ok=True)
        wfiles = sorted(f for f in wdir.rglob("*")
                        if f.is_file() and f.suffix.lower() in (".mdl", ".wav", ".json", ".vpk", ".zip", ".7z", ".vvd", ".vtx", ".vtf", ".vmt"))
        if wfiles:
            st = game.load_files(wfiles, "weapon")
            if st:
                menu.status = st
    except Exception as ex:
        menu.status = "Weapons folder: " + str(ex)

    # Auto-load player models from the "player" folder next to this script.
    # The CT / T choice you make in the Play menu is remembered between runs.
    try:
        pdir = Path(__file__).with_name("player")
        pdir.mkdir(exist_ok=True)
        pfiles = sorted(f for f in pdir.rglob("*") if f.is_file() and f.suffix.lower() == ".mdl")
        if pfiles:
            game.load_files(pfiles, "player")
            menu.status = (menu.status + f" Players: {len(game.asset['player'])} model(s).").strip()
    except Exception as ex:
        menu.status = "Player folder: " + str(ex)

    # Auto-load custom sounds from the "sounds" folder next to this script:
    # a file with "shoot" / "shot" / "fire" in its name = shooting sound,
    # a file with "hit" in its name = hit sound. A sound picked in the menu wins.
    try:
        sdir = Path(__file__).with_name("sounds")
        sdir.mkdir(exist_ok=True)
        sfiles = sorted(f for f in sdir.rglob("*")
                        if f.is_file() and f.suffix.lower() in (".wav", ".ogg", ".mp3"))
        got = []
        for kind, rx in (("hit", r"hit"), ("shot", r"shoot|shot|fire")):
            if getattr(game, f"custom_{kind}_path"):
                continue
            f = next((f for f in sfiles if re.search(rx, f.stem, re.I)), None)
            if f:
                game.set_custom_sound(kind, str(f))
                if getattr(game, f"custom_{kind}_path"):
                    got.append(f"{kind} sound <- {f.name}")
        if got:
            menu.status = (menu.status + " Sounds: " + ", ".join(got) + ".").strip()
    except Exception as ex:
        menu.status = "Sounds folder: " + str(ex)

    clock = pygame.time.Clock()
    last = time.perf_counter()
    running = True
    while running:
        W, H = pygame.display.get_window_size()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.DROPFILE:
                paths = [ev.file]
                names = [Path(p).name.lower() for p in paths]
                has_view = any(n.startswith("v_") for n in names)
                has_player = any(n.endswith(".mdl") and not re.match(r"^[vpw]_", n) and not n.endswith("t.mdl") for n in names)
                st = game.load_files(paths, "player" if (has_player and not has_view) else "weapon")
                if st:
                    menu.status = st
            elif ev.type == pygame.KEYDOWN:
                if game.lock:
                    if ev.key == pygame.K_ESCAPE:
                        set_lock(False)
                        continue
                    name = pygame.key.name(ev.key)
                    game.keys[name] = True
                    b = game.binds
                    if name == b["jump"]:
                        game.P.jreq = 1
                    if name == b["reload"]:
                        game.reload_pressed()
                    for i in range(3):
                        if name == b[f"weapon{i + 1}"]:
                            game.select_weapon(i)
                    if name == b["swaphand"]:
                        game.hand_swap = not game.hand_swap
                    if name == "f":
                        FX.flash = not FX.flash
                else:
                    menu.key_down(ev)
            elif ev.type == pygame.KEYUP:
                game.keys[pygame.key.name(ev.key)] = False
            elif ev.type == pygame.MOUSEMOTION:
                if game.lock:
                    P = game.P
                    if P.alive or True:
                        sens = game.sens * ((0.0008 / 0.0022) if (P.zoom and P.w == 0) else 1)
                        P.yaw -= ev.rel[0] * sens
                        P.pitch = max(-1.5, min(1.5, P.pitch - ev.rel[1] * sens))
                else:
                    menu.mouse_move(ev.pos)
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                if game.lock:
                    btn = ev.button
                    if btn in (4, 5):
                        continue  # wheel handled by MOUSEWHEEL
                    if btn in (6, 7):
                        code = "Mouse" + str(btn - 2)
                        game.keys[code] = True
                        b = game.binds
                        if code == b["jump"]:
                            game.P.jreq = 1
                        if code == b["reload"]:
                            game.reload_pressed()
                        for i in range(3):
                            if code == b[f"weapon{i + 1}"]:
                                game.select_weapon(i)
                        if code == b["swaphand"]:
                            game.hand_swap = not game.hand_swap
                    elif btn == 1:
                        game.keys["mouse1"] = True
                    elif btn == 3:
                        P = game.P
                        if P.w == 0 and P.alive:
                            P.zoom = 0 if P.zoom else 1
                else:
                    if ev.button in (6, 7) and menu.waiting:
                        menu.bind_press("Mouse" + str(ev.button - 2))
                    else:
                        menu.mouse_down(ev.pos, ev.button)
            elif ev.type == pygame.MOUSEBUTTONUP:
                if ev.button == 1:
                    game.keys["mouse1"] = False
                    menu.mouse_up()
                elif ev.button in (6, 7):
                    game.keys["Mouse" + str(ev.button - 2)] = False
            elif ev.type == pygame.MOUSEWHEEL:
                if game.lock:
                    game.P.jreq = 1
                else:
                    menu.wheel(ev.y)

        now = time.perf_counter()
        dt = min(.05, now - last)
        last = now
        game.update(dt)
        if game.net_joined:           # the host accepted us: drop straight into the room
            game.net_joined = False
            launch()
        if game.net_notice:           # denied / lost the host: back to the Online tab with the reason
            menu.status = game.net_notice
            game.net_notice = ""
            if game.lock:
                set_lock(False)
            menu.set_page("online")

        if game.lock:
            render_scene(game, W, H)
            render_hud(game, gfx, W, H, False)
        else:
            gl.glViewport(0, 0, W, H)
            gl.glClearColor(.03, .03, .03, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
            gl.glMatrixMode(gl.GL_PROJECTION)
            gl.glLoadIdentity()
            gl.glOrtho(0, W, H, 0, -1, 1)
            gl.glMatrixMode(gl.GL_MODELVIEW)
            gl.glLoadIdentity()
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            menu.draw(W, H)
            if menu.name.strip() != game.P.name and menu.name.strip():
                game.player_name = menu.name.strip()
                game.P.name = game.player_name
        pygame.display.flip()
        clock.tick(300)

    NET.close()
    save_settings(game, menu)
    pygame.quit()


if __name__ == "__main__":
    main()
