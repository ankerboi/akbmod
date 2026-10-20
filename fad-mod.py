#!/usr/bin/env python3
"""
AKB / ARENA - Browser TDM, rewritten as a native Python game.

Run:       python akb_arena.py   (missing packages install automatically)

Mouse look uses SDL relative mode (raw input, OS pointer acceleration off).
Weapon mods (.mdl/.wav/.json, or CS:GO .vpk packages: models + animations only) put in a "weapons" folder next to this file load
automatically at start. Player models (GoldSrc .mdl, or CS:S / CS:GO models - loose, .zip / .7z / .rar / .vpk; Source models are posed
in code) go in a "player" folder (your CT / T choice is remembered). Custom sounds go in a "sounds" folder (name them
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
    "swaphand": "v", "crouch": "left ctrl", "sprint": "left shift",
}
DEFAULT_XH = {"size": 6, "thick": 2, "gap": 3, "dot": 0, "outline": 1, "r": 0, "g": 255, "b": 0, "a": 1.0}
BIND_META = [
    ("forward", "Move forward"), ("backward", "Move backward"),
    ("left", "Move left"), ("right", "Move right"),
    ("jump", "Jump"), ("slide", "Slide"), ("reload", "Reload"),
    ("weapon1", "Weapon slot 1"), ("weapon2", "Weapon slot 2"),
    ("weapon3", "Weapon slot 3"), ("swaphand", "Switch hand"),
    ("crouch", "Crouch"), ("sprint", "Sprint"),
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
SPRINT_MULT = 1.35   # sprint speed factor (hold the sprint key while moving forward)
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
        return [(cd.lstrip("/") + nm).lower() for cd in (self.cd or [""])] + [nm.lower()]

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


def src_rel(p):
    """Path of a loose model file relative to the folder that holds its models/ folder."""
    p = Path(p)
    try:
        return p.resolve().relative_to(src_root(p).resolve()).as_posix().lower()
    except Exception:
        return p.name.lower()


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


_ARM_BONE_RX = re.compile(r"bip01_[lr]_(hand|forearm|upperarm|finger)|^j_(wrist|elbow|shoulder|index|mid|pinky|ring|thumb)", re.I)


def _mesh_on_arm_bones(srcm):
    """True when the model's own mesh is skinned to arm / hand / finger bones (hands built into the view-model)."""
    try:
        arm = np.array([bool(_ARM_BONE_RX.search(b["name"])) for b in srcm.bones])
        dom = np.minimum(srcm.v_bi[:, 0], len(arm) - 1)
        return int(arm[dom].sum()) > 100
    except Exception:
        return False


class _CssBones:
    """Minimal stand-in for SrcModel: same skeleton as the weapon, plus the hand mesh skinned to it."""

    def __init__(self, bones, v_pos, v_bi, v_w=None):
        self.bones, self.nb = bones, len(bones)
        self.v_pos = v_pos.astype(np.float32)
        n = len(v_pos)
        v_bi = np.asarray(v_bi)
        if v_bi.ndim == 1:
            v_bi = v_bi[:, None]
        self.v_bi = np.zeros((n, 3), np.int64)
        self.v_bi[:, :v_bi.shape[1]] = v_bi
        self.v_w = np.zeros((n, 3), np.float32)
        if v_w is None:
            self.v_w[:, 0] = 1.0
        else:
            self.v_w[:, :v_w.shape[1]] = v_w
        self.v_nb = (self.v_w > 0).sum(1).clip(1, 3).astype(np.int64)


class _ProcHands:
    """Plain fallback arms + hands built in code from the weapon's own arm skeleton (used only when the
    CS:S hand mesh below cannot be fitted). Same interface as SrcData."""
    has_hands = True
    SKIN = (.78, .6, .5)
    SLEEVE = (.22, .24, .22)

    def __init__(self, weapon_model):
        self.name = "css_hands"
        self.seqs = []
        V, B, skin_t, sleeve_t = [], [], [], []
        fing_t = {}                                        # one mesh list per finger, so each can get its own shade
        bones = weapon_model.bones
        names = [b["name"].lower() for b in bones]
        wp = [np.linalg.inv(np.array(b["p2b"], np.float64))[:3, 3] for b in bones]
        kids = {}
        for i, b in enumerate(bones):
            kids.setdefault(b["par"], []).append(i)

        def find(*rxs):
            for rx in rxs:
                for i, n in enumerate(names):
                    if re.search(rx, n):
                        return i
            return None

        def add(verts, tris, bone, tgt):
            base = sum(len(v) for v in V)
            V.append(np.asarray(verts, np.float64))
            B.append(np.full(len(verts), bone, np.int64))
            tgt.append(np.asarray(tris, np.int64) + base)

        def tube(a, b, ra, rb, bone, tgt, n=10, u0=None, flat=1.0):
            a, b = np.asarray(a, float), np.asarray(b, float)
            d = b - a
            ln = np.linalg.norm(d)
            if ln < 1e-6:
                return
            d /= ln
            if u0 is not None:
                u = np.asarray(u0, float) - d * np.dot(u0, d)
            else:
                u = np.cross(d, [0, 0, 1.0])
                if np.linalg.norm(u) < .2:
                    u = np.cross(d, [0, 1.0, 0])
            if np.linalg.norm(u) < 1e-6:
                u = np.cross(d, [0, 0, 1.0])
            u /= np.linalg.norm(u)
            v = np.cross(d, u) * flat                      # flat < 1: squashed (palm / back of the hand)
            ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
            ring = np.stack([np.cos(ang), np.sin(ang)], 1)
            ra_ring = a + (ring[:, :1] * u + ring[:, 1:] * v) * ra
            rb_ring = b + (ring[:, :1] * u + ring[:, 1:] * v) * rb
            verts = np.vstack([ra_ring, rb_ring])
            tris = []
            for k in range(n):
                k2 = (k + 1) % n
                tris += [(k, k2, n + k), (k2, n + k2, n + k)]
            add(verts, tris, bone, tgt)

        def ball(c, r, bone, tgt, n=8):
            c = np.asarray(c, float)
            verts, tris = [], []
            for i in range(n + 1):
                th = np.pi * i / n
                for j in range(n):
                    ph = 2 * np.pi * j / n
                    verts.append(c + r * np.array([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)]))
            for i in range(n):
                for j in range(n):
                    a = i * n + j
                    b = i * n + (j + 1) % n
                    tris += [(a, b, a + n), (b, b + n, a + n)]
            add(verts, tris, bone, tgt)

        made = 0
        for sd, js in (("l", "le"), ("r", "ri")):
            up = find(r"^valvebiped\.bip01_%s_upperarm$" % sd, r"bip01_%s_upperarm$" % sd, r"^j_shoulder_%s$" % js)
            fo = find(r"^valvebiped\.bip01_%s_forearm$" % sd, r"bip01_%s_forearm$" % sd, r"^j_elbow_%s$" % js)
            hd = find(r"^valvebiped\.bip01_%s_hand$" % sd, r"bip01_%s_hand$" % sd, r"^j_wrist_%s$" % js)
            if up is None or fo is None or hd is None:
                continue
            made += 1
            P_up, P_fo, P_hd = wp[up], wp[fo], wp[hd]
            L = max(np.linalg.norm(P_hd - P_fo), 1e-3)
            k = L / 11.5                                   # model scale (a CS forearm is about 11.5 units)
            back = (P_up - P_fo) / max(np.linalg.norm(P_up - P_fo), 1e-6)
            # sleeve: shoulder (pushed back past the screen edge) -> elbow -> first third of the forearm
            ball(P_fo, 2.25 * k, fo, sleeve_t)
            tube(P_up + back * 6 * k, P_up, 2.4 * k, 2.4 * k, up, sleeve_t)
            tube(P_up, P_fo, 2.4 * k, 2.25 * k, up, sleeve_t)
            mid = P_fo + (P_hd - P_fo) * .35
            tube(P_fo, mid, 2.25 * k, 2.0 * k, fo, sleeve_t)
            # bare forearm + wrist
            tube(mid, P_hd, 1.75 * k, 1.35 * k, fo, skin_t)
            ball(P_hd, 1.4 * k, hd, skin_t)
            # hand: flat palm + one chain per finger. Chains are found by bone name (Finger0 / Finger01 / Finger02 ...)
            # first - some mods hang the finger bones off another skeleton branch - else by walking down from the hand bone.
            chains = []
            for n_ in range(5):
                ch_ = [find(r"^valvebiped\.bip01_%s_finger%d%s$" % (sd, n_, suf)) for suf in ("", "1", "2")]
                ch_ = [x for x in ch_ if x is not None]
                if ch_:
                    chains.append(ch_)
            if not chains:
                below = []

                def walk(i):
                    for c in kids.get(i, []):
                        if re.search(r"finger|index|mid|pinky|ring|thumb", names[c]):
                            below.append(c)
                        if c != i:
                            walk(c)
                walk(hd)
                for c in [x for x in below if bones[x]["par"] == hd]:
                    ch_ = [c]
                    while True:
                        nx = [x for x in kids.get(ch_[-1], []) if x in below]
                        if not nx:
                            break
                        ch_.append(nx[0])
                    chains.append(ch_)
            is_thumb = lambda ch_: bool(re.search(r"finger0|thumb", names[ch_[0]]))
            pal = [c_ for c_ in chains if not is_thumb(c_)] or chains
            if pal:
                pts = np.array([wp[c_[0]] for c_ in pal])
                knuckle = pts.mean(0)
                spread = np.linalg.svd(pts - knuckle)[2][0] if len(pts) > 1 else None
                halfw = max(np.linalg.norm(pts.max(0) - pts.min(0)) * .5 + .6 * k, 1.4 * k)
            else:
                knuckle = P_hd + (P_hd - P_fo) * .4
                spread, halfw = None, 1.8 * k
            hs = min(max(np.linalg.norm(knuckle - P_hd) / 3.5, .5 * k), 1.6 * k)    # hand scale
            # palm: wrist -> knuckles, wide and flat
            tube(P_hd, knuckle, 1.45 * k, halfw, hd, skin_t, n=12, u0=spread, flat=.6)
            ball(P_hd, 1.4 * k, hd, skin_t)
            for ch_ in chains:
                th = is_thumb(ch_)
                ft = fing_t.setdefault((sd, ch_[0]), [])
                for j_, c in enumerate(ch_):
                    last = j_ == len(ch_) - 1
                    prev = ch_[j_ - 1] if j_ else hd
                    r0 = ((.5 if th else .4) - .03 * j_) * hs
                    if not last:
                        end = wp[ch_[j_ + 1]]
                    else:                                  # fingertip: carry on in the same direction
                        end = wp[c] + (wp[c] - wp[prev]) * 1.0
                    r1 = r0 * (.9 if not last else .78)
                    tube(wp[c], end, r0, r1, c, ft, n=8)
                    ball(wp[c], r0, c, ft, n=6)            # rounded knuckle / joint
                    if last:
                        ball(end, r1, c, ft, n=6)          # rounded fingertip
        if not made:
            raise ValueError("no arm bones")
        pos = np.vstack(V)
        bi = np.concatenate(B)
        self.model = _CssBones(bones, pos, bi)
        n_all = len(pos)
        self.uv = np.zeros((n_all, 2), np.float32)
        self.parts = []
        shades = [(sleeve_t, self.SLEEVE), (skin_t, self.SKIN)]
        for j, key in enumerate(sorted(fing_t)):          # fingers a touch darker, alternating, so they read as separate fingers
            f = .86 if j % 2 else .93
            shades.append((fing_t[key], tuple(c * f for c in self.SKIN)))
        for tgt, col in shades:
            if tgt:
                idx = np.ascontiguousarray(np.vstack(tgt).reshape(-1), np.uint32)
                self.parts.append({"idx": idx, "mat": {"t": None, "alphatest": False, "blend": False,
                                                       "add": False, "tint": None, "fill": col}})


# CS:S hand + sleeve mesh (from the Remington 870 / CS:S shotgun view-model: sleeve + v_hands parts, with their
# skin weights) - packed with zlib + base64. CssHands re-fits it onto the arm skeleton of any weapon mod.
_CSS_HANDS_META = {"bones": ["bala en la RECARGA", "REMINGTON", "bombeo", "gatillo", "recamara", "puertita", "bala en la recamara", "fuego", "Bone001", "frame main-bone", "frame r upperarm", "frame r forearm", "frame r wriste", "frame r index low", "frame r index mid", "frame r index tip", "frame r thumb low", "frame r thumb mid", "frame r thumb tip", "frame r pinky low", "frame r pinky mid", "frame r pinky tip", "frame r middlelow", "frame r middle mid", "frame r middle tip", "frame r ring low", "frame r ring mid", "frame r ring tip", "frame r armtwist1", "frame r armtwist2", "frame r armtwist3", "frame r armtwist4", "frame l upperarm", "frame l forearm", "frame l wriste", "frame l thumb low", "frame l thumb mid", "frame l thumb tip", "frame l ring low", "frame l ring mid", "frame l ring tip", "frame l middlelow", "frame l middle mid", "frame l middle tip", "frame l index low", "frame l index mid", "frame l index tip", "frame l pinky low", "frame l pinky mid", "frame l pinky tip", "frame l armtwist1", "frame l armtwist2", "frame l armtwist3", "frame l armtwist4"], "part_tris": [1470, 6868], "n": 4854, "nt": 8338, "nbone": 54}
_CSS_HANDS_BLOB = (
    "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"
    "9LYra+Uv9R5fNsrfwdu77Ht/MemEQCvSrwKNvCeW3R0s9d4Xf8DX2bsxPscnX7/LF/L1vaVlK4KNvSeXzaLM6/FFvlKvq2yt7zg4m76OcP7N19p7XFkPfy"
    "vKbPIXefuXdQq08B6K/+Yr8NYr+9PX1Nu8rCHtro+/7yvxvhLf4KtPmS3+htQa62/sbVV2or+e9/SyUwK53jPKmgTzvKeWzQ3kewciZ763V9ksvwc+N/mb"
    "eUvK2sO/qOxXXyPeeoLtvI3Ktvn6elvTL/09W5nvVG+DshP82XBbEKzpPbtsTtDrNcqOJuesss8DBbT7svOvALrR+sP+LvS9ONDT277snkBf+tIu0NXbtu"
    "zrQBtvp7IewU68fSCg/+XdIf8p3hZlG/xtvN3Kmgd78/Yzf3dv17IxgS7ezmU7AqXNVEFXRVpT/a+NeZVilZnpMU+pvJrpOTXLUyF0mSPFROF/Wj69YrJu"
    "M1VVLK0qHqmA+jex9P+g7v+6mNrN0r9VQunfRWbH37+tHJP5f1++yld/L0yFYlW2/j/Hzs10bNY5EVUiPWKsrJC/L1mh1pG6nMrtX736z+KRxPu7ptXfqa"
    "KyRaXKnNpcZSVU7bVSfFqae7G9RypR2aEl3Vq5t2mS4JDMr2lnJmOymEp5TMQq/W2S4b+KXQuO4LrTc2Q9qvLVEavn/Sfy/G9a2tJNQmixjarjEdadqgsk"
    "MmuqcqIKy6ykduFTbhJH7kVyaqQ+lhN5aWX+zpOr/8lv/8sF4r81TEdi/g9lqCxJcldWK1fVaqhq1YVoqOlclVvLzszMlsijjpm1dH5mbq3M5Cudn0nMqi"
    "X5mbWcaL/KTM1JRuEmtVLzG/4fxLoNazmEHelL3VqVoqoq82+j1klKLM9UKTHxKi1TYr4dU+kjRVWxmJZWJWQWIqeKXjhNJ4VMiFS3kor0EDesm/6oYyVl"
    "5iZpbQ7otaE2jNzEwCUjDSXoSm9VekzkUy6ZlmeKPaj0mDCVWimPOubWKk//fcytQKfwyXWMVqcYf2otmREphp3QQK4ox44qMWVIs20mSspnJ+PfCCYzRc"
    "qUV+HJjrWzVXbtWtm1amfz1n60c5yYfEzNtGMtu5YQqQVqpRarVKsKno4AvKpVW/eOx1oVYm0RLyXmS6yVbUdISTHv7PxEgfJ+2V1TdmE7TZ0X2eWxUlu1"
    "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"
    "suIkV5niSkvPfxbLrylSz5tHjkfiky5SG+maXmDtLmirKFbOctbGWQ1T6jrRebRnZfJVccLgy8uXpJh3kkjGNom5nJ5ZcbFLLp1/u+rJRDjSqwrFKsQj5S"
    "fmWtf0jY2zm+qayJKYJDWteRam7dlk29ZVqFSRkhxUpVf/NhZWFVUK8S9iqj4r0YntpN2p5PbS0UPX8o44W9ByX1HZaTjuonJOstGkJ2lR2LJFy8IWLVVq"
    "bKnSYwsnpr1tkZZf4bEiw9TqlWqlFS6sVB0JdWahQyTETrjEllLL6YvkJN7qNNG7cglVIq3QR5VSIBErdK1irf9ibFEpqqrUeyTZKrGqQo0tqxi4ZCy3k5"
    "Yq3SQKRY2FVamutJk2J9L8pk5smp+glROb5jepmFnLzm9ix8qvnI+xiLpueo6OtfKlmOQ73NKIRJpSMj2q1OZSMm35m+TZsakjnpZQOW+THZRO2QWaSEnS"
    "JvZn50wxUqFFP5XFkJzUr6oqP/7H8ch8UtVYtRiJKtmpVZzv4pwP3uwch41KY5n8riw1366b/MrO+dbOyajctPNFXqqQ6cKrCl34B7FCr7Mrc2DIkrR8cv"
    "z3PFM+Naz8XWLiE77kd4NCl389mPxiMDfx3WBm+atsVZlO5qT+alz+87H8HFwr8StwksjNSvuVOfV358o/bbvT7rFTo1SR37Irfqus0r7oK//uN/FRvTMp"
    "7H9h0SQv5TN75UyxJvaEatqk/DHffiUTzZliiSjcUiem4z3s6Slv9QAlCgg36OTMlbq2U0rzPKmsUueypB3aO4RzV1xcfnucekMut7upl+FVFk67bS5Ou5"
    "+vcOfspCrl/rlDOefyy/b0KqlX1qkSvtVG3X+Jemyo+kv9b43fZ6ov6qmDWerzXBXvol7vpDO/c6uPG6lXT1Yv9VNlndWLA9Sj5ymrq358/hT1cl9d5pU+"
    "6q5R+vGhC9TqS9W9l+t8tIFO5PFvGv0oT9eCw1NeXeX2sbqJfyItIs2/VvOnLiJVWebbbB0/bKo+aajj263VvuZpBTaepbZ30B3f3erf6Qq1UIuIrmjif+"
    "+g/5O4p0i9V6A+q6/pr+ukGcz6c9QTg7UOUS85mMpXddWW/mpHsfq0gdrVVr3RUUW6KfN49eTZatFktWqEjhgGo8ZwY0jPnKkeuFhtK9H2RuE32+sIcd9l"
    "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"
    "XQNsQ0+/8Bm+8eWg=="
)


_CSS_DONOR = {}


def _css_donor():
    if not _CSS_DONOR:
        import zlib
        import base64
        raw = zlib.decompress(base64.b64decode("".join(_CSS_HANDS_BLOB)))
        n, nt, nbn = _CSS_HANDS_META["n"], _CSS_HANDS_META["nt"], _CSS_HANDS_META["nbone"]
        o = 0
        pos = np.frombuffer(raw, np.float16, n * 3, o).reshape(n, 3).astype(np.float64); o += n * 6
        bi = np.frombuffer(raw, np.uint8, n * 3, o).reshape(n, 3).astype(np.int64); o += n * 3
        w = np.frombuffer(raw, np.uint8, n * 3, o).reshape(n, 3).astype(np.float64) / 255.0; o += n * 3
        tri = np.frombuffer(raw, np.uint16, nt * 3, o).reshape(nt, 3).astype(np.int64); o += nt * 6
        bp = np.frombuffer(raw, np.float32, nbn * 3, o).reshape(nbn, 3).astype(np.float64)
        _CSS_DONOR.update(pos=pos, bi=bi, w=w, tri=tri, bp=bp, names=[x.lower() for x in _CSS_HANDS_META["bones"]],
                          split=_CSS_HANDS_META["part_tris"][0])
    return _CSS_DONOR


_CSS_BONE_NAMES = {"upperarm": "UpperArm", "forearm": "Forearm", "wriste": "Hand",
                   "thumb low": "Finger0", "thumb mid": "Finger01", "thumb tip": "Finger02",
                   "index low": "Finger1", "index mid": "Finger11", "index tip": "Finger12",
                   "middlelow": "Finger2", "middle mid": "Finger21", "middle tip": "Finger22",
                   "ring low": "Finger3", "ring mid": "Finger31", "ring tip": "Finger32",
                   "pinky low": "Finger4", "pinky mid": "Finger41", "pinky tip": "Finger42",
                   "armtwist1": "Forearm", "armtwist2": "Forearm", "armtwist3": "Forearm", "armtwist4": "Forearm"}
_CSS_CHAINS = [("thumb low", "thumb mid", "thumb tip"), ("index low", "index mid", "index tip"),
               ("middlelow", "middle mid", "middle tip"), ("ring low", "ring mid", "ring tip"),
               ("pinky low", "pinky mid", "pinky tip")]


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else v


def _frame3(a, ref):
    """Orthonormal frame (columns): axis a, second axis = ref made perpendicular to a, third = cross."""
    a = _unit(np.asarray(a, float))
    r = np.asarray(ref, float)
    r = r - a * np.dot(r, a)
    if np.linalg.norm(r) < 1e-6:
        r = np.cross(a, [0, 0, 1.0])
        if np.linalg.norm(r) < 1e-6:
            r = np.cross(a, [0, 1.0, 0])
    r = _unit(r)
    return np.stack([a, r, np.cross(a, r)], 1)


def _fit_css_hands(weapon_model):
    """CS:S hands re-fitted onto a weapon's ValveBiped arm skeleton: every donor bone gets a rigid frame
    (+ scale) that takes it from the donor's bind pose to the weapon's, the mesh is moved with its own skin
    weights, then skinned to the weapon's bones. Returns (verts, bone ids (n,3), weights (n,3), tris, split)."""
    D = _css_donor()
    bones = weapon_model.bones
    tn = [b["name"].lower() for b in bones]
    twp = [np.linalg.inv(np.array(b["p2b"], np.float64))[:3, 3] for b in bones]

    def tfind(side, suffix):
        side, suffix = side.lower(), suffix.lower()
        nm = "valvebiped.bip01_%s_%s" % (side, suffix)
        if nm in tn:
            return tn.index(nm)
        for i, n in enumerate(tn):
            if n.endswith("bip01_%s_%s" % (side, suffix)):
                return i
        return None

    nbn = len(D["names"])
    A = np.tile(np.eye(3), (nbn, 1, 1))
    C = np.zeros((nbn, 3))
    tmap = np.zeros(nbn, np.int64)
    dmask = np.zeros(nbn, bool)
    for dside, tside in (("r", "R"), ("l", "L")):
        def dpos(k):
            return D["bp"][D["names"].index("frame %s %s" % (dside, k))]

        t_idx = {}
        for k, suf in _CSS_BONE_NAMES.items():
            ti = tfind(tside, suf)
            if ti is None and k not in ("upperarm", "forearm", "wriste"):   # missing finger joint: use the one before
                for alt in (suf[:-1], suf[:-2]):
                    ti = tfind(tside, alt) if alt else None
                    if ti is not None:
                        break
            t_idx[k] = ti
        for need in ("upperarm", "forearm", "wriste", "index low", "pinky low"):
            if t_idx[need] is None:
                raise ValueError("weapon has no %s bone" % need)
        tp = {k: twp[i] for k, i in t_idx.items() if i is not None}
        dp = {k: dpos(k) for k in t_idx}
        # arm plane (elbow bend) is the roll reference for the arm bones
        dn_ = np.cross(dp["forearm"] - dp["upperarm"], dp["wriste"] - dp["forearm"])
        tn_ = np.cross(tp["forearm"] - tp["upperarm"], tp["wriste"] - tp["forearm"])
        d_spread = dp["index low"] - dp["pinky low"]
        t_spread = tp["index low"] - tp["pinky low"]
        if np.linalg.norm(dn_) < 1e-3 or np.linalg.norm(tn_) < 1e-3:
            dn_, tn_ = d_spread, t_spread

        def set_T(k, dO, da, dref, tO, ta, tref, scale):
            Fd, Ft = _frame3(da, dref), _frame3(ta, tref)
            R = (Ft @ Fd.T) * scale
            for name in [k] + (["armtwist1", "armtwist2", "armtwist3", "armtwist4"] if k == "forearm" else []):
                bi_ = D["names"].index("frame %s %s" % (dside, name))
                A[bi_] = R
                C[bi_] = tO - R @ dO
                tmap[bi_] = t_idx[k]
                dmask[bi_] = True

        def seg(a, b, ta, tb):
            return max(np.linalg.norm(tb - ta), 1e-6) / max(np.linalg.norm(b - a), 1e-6)
        set_T("upperarm", dp["upperarm"], dp["forearm"] - dp["upperarm"], dn_, tp["upperarm"],
              tp["forearm"] - tp["upperarm"], tn_, seg(dp["upperarm"], dp["forearm"], tp["upperarm"], tp["forearm"]))
        set_T("forearm", dp["forearm"], dp["wriste"] - dp["forearm"], dn_, tp["forearm"],
              tp["wriste"] - tp["forearm"], tn_, seg(dp["forearm"], dp["wriste"], tp["forearm"], tp["wriste"]))
        roots_d = [dp[c[0]] for c in _CSS_CHAINS if c[0] != "thumb low"]
        roots_t = [tp[c[0]] for c in _CSS_CHAINS if c[0] != "thumb low" and c[0] in tp]
        dk, tk = np.mean(roots_d, 0), np.mean(roots_t, 0)
        set_T("wriste", dp["wriste"], dk - dp["wriste"], d_spread, tp["wriste"], tk - tp["wriste"], t_spread,
              seg(dp["wriste"], dk, tp["wriste"], tk))
        for chain in _CSS_CHAINS:
            prev_scale = None
            for j, k in enumerate(chain):
                if t_idx[k] is None:
                    continue
                last = j == len(chain) - 1
                if not last and t_idx[chain[j + 1]] is not None and t_idx[chain[j + 1]] != t_idx[k]:
                    da_ = dp[chain[j + 1]] - dp[k]
                    ta_ = tp[chain[j + 1]] - tp[k]
                    sc = seg(dp[k], dp[chain[j + 1]], tp[k], tp[chain[j + 1]])
                    prev_scale = sc
                else:                                           # tip: carry on in the previous direction
                    pk = chain[j - 1] if j else "wriste"
                    da_ = dp[k] - dp[pk]
                    ta_ = (tp[k] - tp[pk]) if t_idx[pk] is not None and t_idx[pk] != t_idx[k] else tp[k] - tp["wriste"]
                    sc = prev_scale or seg(dp[pk], dp[k], tp[pk] if pk in tp else tp["wriste"], tp[k])
                set_T(k, dp[k], da_, d_spread, tp[k], ta_, t_spread, sc)
    # move every vertex with its own skin weights (bind pose), then skin it to the weapon's bones
    pos, bi, w = D["pos"], D["bi"], D["w"]
    out = np.zeros_like(pos)
    for k in range(3):
        b = bi[:, k]
        out += w[:, k:k + 1] * (np.einsum("nij,nj->ni", A[b], pos) + C[b])
    ids = np.zeros((len(pos), 3), np.int64)
    wt = np.zeros((len(pos), 3))
    for k in range(3):
        ids[:, k] = tmap[bi[:, k]]
        wt[:, k] = w[:, k]
    for k in (1, 2):                                         # merge repeated weapon bones (e.g. the arm twist bones)
        for j in range(k):
            same = (ids[:, k] == ids[:, j]) & (wt[:, k] > 0)
            wt[same, j] += wt[same, k]
            wt[same, k] = 0
    return out, ids, wt, D["tri"], D["split"]


class CssHands(_ProcHands):
    """CS:S arms + hands (CS:S hand / sleeve mesh re-fitted onto the weapon's arm skeleton). Used for CS:GO
    weapon mods that ship without hands. Same interface as SrcData."""

    def __init__(self, weapon_model):
        try:
            v, ids, wt, tri, split = _fit_css_hands(weapon_model)
        except Exception as ex:
            print("CS:S hand mesh could not be fitted (%s) - using plain hands" % ex)
            _ProcHands.__init__(self, weapon_model)
            return
        self.name = "css_hands"
        self.seqs = []
        self.model = _CssBones(weapon_model.bones, v, ids, wt)
        self.uv = np.zeros((len(v), 2), np.float32)
        self.parts = []
        for sl, col in ((tri[:split], self.SLEEVE), (tri[split:], self.SKIN)):
            self.parts.append({"idx": np.ascontiguousarray(sl.reshape(-1), np.uint32),
                               "mat": {"t": None, "alphatest": False, "blend": False, "add": False,
                                       "tint": None, "fill": col}})


def css_hands_for(data):
    """CssHands for a SrcData weapon (cached on it), or None when the weapon has no arm skeleton."""
    if not hasattr(data, "_css_hands"):
        try:
            data._css_hands = CssHands(data.model)
        except Exception as ex:
            print("CSS hands unavailable for", data.name, "-", ex)
            data._css_hands = None
    return data._css_hands


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
        # (whole words only: a "handguard" / "handle" texture is part of the gun, not a hand)
        self.has_hands = (any(re.search(r"(?<![a-z])(hands?|sleeves?|gloves?|fingers?|palms?)(?![a-z])|^arms?$|_arms?$", t, re.I)
                              for t in srcm.tex_names)
                          or any(re.search(r"/(hands|arms|sleeves|gloves)/", c) for c in srcm.cd)
                          or _mesh_on_arm_bones(srcm))
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


# --------------------------------------------------------------------------
# Source engine (CS:S / CS:GO) PLAYER models
#   These packs usually hold only the mesh + textures (the animations live in other game files),
#   so the body is posed here in code: walk / run cycle, crouch, slide, jump, aim pitch, torso
#   twist, two-handed weapon hold (arm IK), shooting kick, reload and a death fall.
#   Everything is done in model space with one cumulative rotation per bone (D) about the bone's
#   rest joint, so it does not matter how the model's bone axes are oriented.
# --------------------------------------------------------------------------
def _cross3(a, b):
    return np.array((a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]))


def _rot_axis(ax, ang):
    x, y, z = float(ax[0]), float(ax[1]), float(ax[2])
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / n, y / n, z / n
    c, s = math.cos(ang), math.sin(ang)
    t = 1 - c
    return np.array(((c + x * x * t, x * y * t - z * s, x * z * t + y * s),
                     (y * x * t + z * s, c + y * y * t, y * z * t - x * s),
                     (z * x * t - y * s, z * y * t + x * s, c + z * z * t)))


def _arc(a, b):
    """Rotation matrix that turns vector a into vector b (shortest way)."""
    na = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) or 1.0
    nb_ = math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2]) or 1.0
    a = (a[0] / na, a[1] / na, a[2] / na)
    b = (b[0] / nb_, b[1] / nb_, b[2] / nb_)
    c = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    if c > 0.999999:
        return np.eye(3)
    if c < -0.999999:
        ax = _cross3(a, (1.0, 0.0, 0.0))
        if abs(ax).sum() < 1e-3:
            ax = _cross3(a, (0.0, 1.0, 0.0))
        return _rot_axis(ax, math.pi)
    v0, v1, v2 = a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]
    vx = np.array(((0.0, -v2, v1), (v2, 0.0, -v0), (-v1, v0, 0.0)))
    return np.eye(3) + vx + (vx @ vx) * (1.0 / (1.0 + c))


def _sstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / ((b - a) or 1e-9)))
    return t * t * (3 - 2 * t)


def _lerp(a, b, t):
    return a + (b - a) * t


class _NoSeqs:
    """What update_entity_anim expects in pm.d: a source player model has no sequences."""
    seqs = []

    def pick(self, *pats):
        return -1

    def pick_all(self, rx):
        return []


class SrcPlayerInst:
    """Procedurally animated Source player model. Vertices come out in game axes
    (x = forward, y = left, z = up), feet at z = 0 when standing."""
    is_source = True
    is_src_player = True
    _I3 = np.eye(3)

    def __init__(self, data):
        self.data = data
        m = self.m = data.model
        self.d = _NoSeqs()
        self.seqs = []
        self.base_seq = 0
        self.last = None
        nb = m.nb
        self.par = [b["par"] for b in m.bones]
        # ---- rest pose in model space (world rotation + joint position of every bone)
        Wr, Wp = [None] * nb, [None] * nb
        for i, b in enumerate(m.bones):
            R = _quat_mat3(b["quat"])
            p = self.par[i]
            if p >= 0:
                Wr[i] = Wr[p] @ R
                Wp[i] = Wp[p] + Wr[p] @ np.asarray(b["pos"], np.float64)
            else:
                Wr[i] = R
                Wp[i] = np.asarray(b["pos"], np.float64).copy()
        self.Wr, self.Wp = Wr, Wp
        # ---- bones by role
        ix = {}
        for i, b in enumerate(m.bones):
            k = re.sub(r".*bip01_", "", b["name"].lower())
            ix.setdefault(k, i)
        self.ix = ix
        self.spine = [ix[k] for k in ("spine", "spine1", "spine2", "spine4") if k in ix]
        need = ("pelvis", "l_thigh", "r_thigh", "l_calf", "r_calf", "l_foot", "r_foot",
                "l_upperarm", "r_upperarm", "l_forearm", "r_forearm", "l_hand", "r_hand", "head1")
        self.can = bool(self.spine) and all(k in ix for k in need)
        # ---- model axes: left from the hips, forward = left x up (right handed)
        U = np.array([0.0, 0.0, 1.0])
        F, L = np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])
        if self.can:
            l = Wp[ix["l_thigh"]] - Wp[ix["r_thigh"]]
            l[2] = 0.0
            if np.linalg.norm(l) > 1e-3:
                L = l / np.linalg.norm(l)
                F = _cross3(L, U)
            toes = [(ix["l_toe0"], ix["l_foot"]), (ix["r_toe0"], ix["r_foot"])] if "l_toe0" in ix and "r_toe0" in ix else []
            if toes and sum(float(np.dot(Wp[t] - Wp[f], F)) for t, f in toes) < 0:
                F, L = -F, -L          # toes point the other way: face them
        self.F, self.L, self.U = F, L, U
        self.Rg = np.array([F, L, U])      # model -> game axes
        # ---- skinning: one entry per (vertex, bone) influence, sorted by bone
        nbn = nb
        nv = len(m.v_pos)
        ev, eb, ew = [], [], []
        for k in range(3):
            w = np.where(m.v_nb > k, m.v_w[:, k], 0.0).astype(np.float64)
            keep = w > 1e-5
            ev.append(np.nonzero(keep)[0])
            eb.append(np.minimum(m.v_bi[keep, k], nbn - 1))
            ew.append(w[keep])
        ev, eb, ew = np.concatenate(ev), np.concatenate(eb), np.concatenate(ew)
        tot = np.zeros(nv)
        np.add.at(tot, ev, ew)
        ew = ew / np.maximum(tot[ev], 1e-9)
        order = np.argsort(eb, kind="stable")
        self.ev, self.eb, self.ew = ev[order], eb[order], ew[order]
        Wp_a = np.array(Wp)
        self.er = (m.v_pos[self.ev] - Wp_a[self.eb])            # vertex in the joint's rest frame (model axes)
        self.nv = nv
        bs = np.searchsorted(self.eb, np.arange(nb + 1))
        self._act = [(b, int(bs[b]), int(bs[b + 1])) for b in range(nb) if bs[b + 1] > bs[b]]
        self.er32 = np.ascontiguousarray(self.er, np.float32)
        self.ew32 = np.ascontiguousarray(self.ew, np.float32)[:, None]
        self._o = np.empty((len(self.eb), 3), np.float32)
        # ---- animation state
        self.ph = 0.0           # gait phase
        self.mv = 0.0           # eased movement 0..1
        self.cr = 0.0           # eased crouch 0..1
        self.sl = 0.0           # eased slide 0..1
        self.ak = 0.0           # eased in-air 0..1
        self.sp = 0.0           # eased sprint 0..1
        self.air_t = 0.0        # seconds since the feet left the ground
        self.lead = 0           # leg that leads (knee up) in a jump
        self.was_air = False
        self.prev_vz = 0.0
        self.land = 0.0         # how hard the last landing was 0..1
        self.land_t = 9.0       # seconds since touchdown
        self._snap = None       # last living pose (legs, lean): a death starts from it
        self.leg = 36.0         # hip -> ankle length (sets the stride so the feet do not skate)
        self._seg = (17.0, 17.0)
        if self.can:
            t_ = float(np.linalg.norm(Wp[ix["l_calf"]] - Wp[ix["l_thigh"]]))
            c_ = float(np.linalg.norm(Wp[ix["l_foot"]] - Wp[ix["l_calf"]]))
            if t_ > 1.0 and c_ > 1.0:
                self._seg = (t_, c_)
                self.leg = t_ + c_
        self.vtx = None
        self._key = None
        self._since = 1.0
        self.rest_min = float(m.v_pos[:, 2].min())
        self.animate(0.0, 0.0, False, False, False, 0.0, 0.0, 0.0, None, False)
        self.rest_min = float(self.vtx[:, 2].min())

    # ---- same small interface the game uses for GoldSrc models ----
    def find(self, kind):
        return []

    def is_mirrored(self):
        return False

    def tick(self, dt):
        pass

    def begin_fade(self, dur=0.2):
        pass

    def pose(self, *a, **k):
        pass

    def pose_player(self, *a, **k):
        pass

    @property
    def pos(self):
        return [self.vtx]

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
        SrcInst._draw_set(self.data, self.vtx)
        tex2d(True)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)

    # ---- rotations in the model's own forward / left / up frame ----
    def _RL(self, a):      # + = lean forward / swing a hanging limb backward
        return _rot_axis(self.L, a)

    def _RU(self, a):      # + = turn left
        return _rot_axis(self.U, a)

    def _RF(self, a):      # + = swing a hanging limb out to the left
        return _rot_axis(self.F, a)

    # ---- two-bone arm IK: returns the new cumulative rotations of upper arm and forearm ----
    def _arm(self, ua, fa, Dp, S, T, pole):
        Wp = self.Wp
        u = float(np.linalg.norm(Wp[fa] - Wp[ua]))
        hand = self.ix["l_hand"] if ua == self.ix["l_upperarm"] else self.ix["r_hand"]
        f = float(np.linalg.norm(Wp[hand] - Wp[fa]))
        v = T - S
        d = float(np.linalg.norm(v)) or 1e-6
        dirv = v / d
        d = max(abs(u - f) + 0.5, min(u + f - 0.05, d))
        a = (u * u - f * f + d * d) / (2 * d)
        h = math.sqrt(max(u * u - a * a, 0.0))
        pv = pole - dirv * float(np.dot(pole, dirv))
        n = float(np.linalg.norm(pv))
        pv = pv / n if n > 1e-6 else _cross3(dirv, self.L)
        elbow = S + dirv * a + pv * h
        hand_p = S + dirv * d
        r_ua = (Wp[fa] - Wp[ua])
        r_fa = (Wp[hand] - Wp[fa])
        Dua = _arc(Dp @ r_ua, elbow - S) @ Dp
        Dfa = _arc(Dua @ r_fa, hand_p - elbow) @ Dua
        return Dua, Dfa

    # ---- public: alive pose ----
    @staticmethod
    def _mix(a, b, t):
        return tuple(x + (y - x) * t for x, y in zip(a, b))

    def _loco_legs(self, side, rf, A):
        """Walk / run / sprint cycle for one leg -> (hip angle, knee bend, ankle plantar-flex).
        The hip swings about the cycle, the knee folds through the swing and dips a little after the heel
        strike, the ankle points the toes at push-off and lifts them at the strike."""
        p = self.ph + (math.pi if side else 0.0)
        pm = p % (2 * math.pi)
        swing = max(0.0, math.cos(p - 0.30)) ** 1.5
        kmax = _lerp(0.80, 1.10, rf) + 0.40 * self.sp
        load = 0.20 * rf * math.sin(2 * (pm - math.pi / 2)) if math.pi / 2 < pm < math.pi else 0.0
        a = A * math.sin(p)
        k = 0.06 + kmax * swing + load
        pl = 0.62 * max(0.0, -math.sin(p + 0.35)) ** 2 - 0.30 * max(0.0, math.sin(p - 0.10)) ** 2
        return a, k, pl

    def _air_legs(self, vz, air_t, lead, run):
        """Jump / fall legs from the vertical speed: toe-off, lead knee up, apex tuck, legs reaching for the floor.
        run = 0 (standing jump: both knees tuck evenly) .. 1 (running jump: stride with a trailing leg)."""
        s = max(-1.0, min(1.0, vz / 300.0))
        e = lambda x: x * x * (3 - 2 * x)
        sym = (0.95, 1.50, 0.40)
        up_l = self._mix(sym, (1.20, 1.55, 0.25), run)
        up_t = self._mix(sym, (-0.25, 0.70, 0.65), run)
        ap_l, ap_t = (0.85, 1.35, 0.15), (0.30, 1.25, 0.30)
        dn_l, dn_t = (0.35, 0.30, 0.25), (0.10, 0.22, 0.30)
        if s >= 0:
            L, T = self._mix(ap_l, up_l, e(s)), self._mix(ap_t, up_t, e(s))
        else:
            L, T = self._mix(ap_l, dn_l, e(-s)), self._mix(ap_t, dn_t, e(-s))
        push = math.exp(-air_t / 0.085) if s > 0.25 else 0.0        # toe-off: both legs drive straight
        pu = (0.02, 0.08, 0.85)
        L, T = self._mix(L, pu, push), self._mix(T, pu, push)
        return [L, T] if lead == 0 else [T, L]

    def _wall(self, t, psi):
        """Wall kick: turn the hips to the wall, knees to the chest on contact, drive the legs out, open the arms."""
        c = _sstep(0.0, 0.07, t)
        x = _sstep(0.12, 0.30, t)
        r = _sstep(0.30, 0.60, t)
        w = c * (1.0 - r)
        a, k, pl = _lerp(1.30, 0.40, x), _lerp(1.80, 0.12, x), _lerp(-0.15, 0.75, x)
        return {"w": w, "legs": [(a + 0.06, k, pl), (a - 0.06, k + 0.12, pl)],
                "yaw": max(-1.2, min(1.2, psi)) * 0.85 * (1.0 - _sstep(0.14, 0.34, t)),
                "tilt": _lerp(-0.34, 0.12, x), "lean": _lerp(0.42, -0.12, x),
                "arms": 0.35 + 0.65 * x}

    def animate(self, dt, spd, air, crouch, slide, twist_deg, pitch, kick, reload, melee,
                sprint=0.0, vz=0.0, wall=None):
        """spd = ground speed, twist_deg = torso twist vs legs, pitch = aim pitch (rad, + up),
        kick = recoil 1..0, reload = progress 0..1 or None, sprint = 0..1, vz = vertical speed,
        wall = None or (seconds since a wall kick, angle from the facing to the wall in rad)."""
        if not self.can:
            self._skin_rest()
            return
        sprint = max(0.0, min(1.0, float(sprint)))
        k = min(1.0, dt * 9) if dt > 0 else 1.0
        self.mv += ((1.0 if spd > 12 else 0.0) - self.mv) * k
        self.cr += ((1.0 if crouch else 0.0) - self.cr) * k
        self.sl += ((1.0 if slide else 0.0) - self.sl) * min(1.0, dt * 12) if dt > 0 else (1.0 if slide else 0.0) - self.sl
        self.ak += ((1.0 if air else 0.0) - self.ak) * min(1.0, dt * 14) if dt > 0 else (1.0 if air else 0.0) - self.ak
        tsp = sprint if (spd > 60 and not crouch and not slide and not air) else 0.0
        self.sp += (tsp - self.sp) * min(1.0, dt * 7) if dt > 0 else tsp - self.sp
        # ---- take-off / touch-down bookkeeping
        if dt > 0:
            if air and not self.was_air:
                self.air_t = 0.0
                self.lead = 0 if math.sin(self.ph) >= 0 else 1      # the leg that is forward leads the jump
            elif air:
                self.air_t += dt
            if self.was_air and not air:
                hit = -self.prev_vz
                self.land = max(0.0, min(1.0, 0.2 + (hit - 60.0) / 420.0)) if hit > 90.0 else 0.0
                self.land_t = 0.0
            if not air:
                self.land_t += dt
            if air:
                self.prev_vz = vz
            self.was_air = bool(air)
        mv, cr, sl, ak, sp = self.mv, self.cr, self.sl, self.ak, self.sp
        rf = min(1.0, spd / 250.0)
        # hip swing amplitude, then the cadence that keeps the planted foot from skating
        A = _lerp(_lerp(0.30, 0.52, rf) + 0.22 * sp, 0.26, cr)
        if spd > 12 and not air:
            leg = self.leg * (1.0 - 0.35 * cr)
            self.ph = (self.ph + dt * spd / max(leg * A * 0.95, 1.0)) % (2 * math.pi)
        env = _sstep(0.0, 0.08, self.land_t) * (1.0 - _sstep(0.10, 0.45, self.land_t)) if self.land > 0 else 0.0
        lc = self.land * env
        wl = None
        if wall is not None and air and 0.0 <= wall[0] < 0.62:
            wl = self._wall(wall[0], wall[1])
        key = (round(self.ph * 24), round(mv * 20), round(cr * 20), round(sl * 20), round(ak * 20), round(sp * 20),
               round(twist_deg * .6), round(pitch * 24), round(kick * 6), None if reload is None else round(reload * 40),
               bool(melee), round(vz / 25.0) if air else 0, round(self.air_t * 30) if air else 0, round(env * 30),
               None if wl is None else round(wall[0] * 40))
        self._since += dt
        if key == self._key or (self._since < 0.028 and dt > 0):
            return
        self._since = 0.0
        self._key = key
        # ---------- legs ----------
        run_k = max(0.0, min(1.0, spd / 120.0))
        air_l = self._air_legs(vz, self.air_t, self.lead, run_k)
        legs = []
        for side in (0, 1):
            a, kk, pl = self._loco_legs(side, rf, A)
            st = (mv * a, 0.03 + mv * (kk - 0.06), mv * pl)
            ps = self.ph + (math.pi if side else 0.0)
            c_a = (0.95 + 0.20 * mv * math.sin(ps), 1.70 + 0.30 * mv * max(0.0, math.cos(ps)), 0.0)
            g = self._mix(st, c_a, cr)
            sl_p = (1.30, 0.05, -0.25) if side == 0 else (0.55, 2.00, 0.20)      # slide: front leg out, back leg folded
            g = self._mix(g, sl_p, sl)
            g = self._mix(g, air_l[side], ak)
            if lc > 0:
                g = self._mix(g, (0.30 + 0.75 * self.land, 0.55 + 1.25 * self.land, 0.0), env)
            legs.append(g)
        air_lean = 0.10 + 0.12 * rf + 0.06 * max(0.0, min(1.0, vz / 300.0))
        lean = (0.06 + 0.10 * rf) * mv + 0.20 * sp + 0.30 * cr - 0.50 * sl + air_lean * ak + 0.38 * lc
        hip_yaw = -(0.05 + 0.07 * rf + 0.05 * sp) * mv * math.sin(self.ph) * (1.0 - ak) * (1.0 - 0.7 * cr) * (1.0 - sl)
        air_arms = ak * (0.30 + 0.70 * max(0.0, min(1.0, -vz / 420.0)))
        root = None
        ground = ak < 0.5
        if wl is not None:
            w = wl["w"]
            legs = [self._mix(legs[i], wl["legs"][i], w) for i in (0, 1)]
            lean = _lerp(lean, wl["lean"], w)
            air_arms = _lerp(air_arms, wl["arms"], w)
            hip_yaw += wl["yaw"] * w                       # hips and legs turn to the wall, the shoulders stay on target
            root = (self._RL(wl["tilt"] * w), np.asarray(self.Wp[0], np.float64), np.zeros(3))
            ground = False
        self._snap = (legs, lean)
        self._solve(legs, lean, math.radians(twist_deg), pitch, kick, reload, melee, ground=ground, root=root,
                    hip_yaw=hip_yaw, sprint=sp * (1.0 - ak), air_arms=air_arms, arm_drop=lc,
                    squat=min(1.0, cr + lc), ik=True)

    # ---- core solver ----
    def _solve(self, legs, lean, twist, pitch, kick, reload, melee, ground=True, root=None, extra=None,
               hip_yaw=0.0, sprint=0.0, air_arms=0.0, arm_drop=0.0, squat=0.0, ik=None, arm_local=None):
        m, ix, Wp = self.m, self.ix, self.Wp
        nb = m.nb
        F, L, U = self.F, self.L, self.U
        I3 = self._I3
        E = {}
        # spine: lean + share of the aim pitch + twist (the hips' own yaw is counter-rotated so the shoulders stay square)
        sp = self.spine
        w = np.array([1.0, 1.2, 1.5, 1.5][-len(sp):] if len(sp) <= 4 else [1.0] * len(sp))
        w = w / w.sum()
        pit_sp = pitch * 0.45
        tw = np.array([0.15] + [0.25] * (len(sp) - 1)) if len(sp) > 1 else np.array([1.0])
        tw = tw / tw.sum()
        tw_eff = twist - hip_yaw * 0.85
        for j, bi in enumerate(sp):
            E[bi] = self._RU(tw_eff * tw[j]) @ self._RL((lean - pit_sp) * w[j])
        if hip_yaw and "pelvis" in ix:
            E[ix["pelvis"]] = self._RU(hip_yaw)
        if "neck1" in ix:
            E[ix["neck1"]] = self._RL(-pitch * 0.15 - lean * 0.30)
        E[ix["head1"]] = self._RL(-pitch * 0.40 - lean * 0.55)       # the head keeps looking ahead
        # legs: hip angle, knee bend, ankle (flat on the floor unless a plantar-flex is given)
        for side, pre in ((0, "l_"), (1, "r_")):
            lg = legs[side]
            a, k = lg[0], lg[1]
            foot = (a - k) * 0.88 if len(lg) < 3 else (a - k) + lg[2]
            E[ix[pre + "thigh"]] = self._RL(-a)
            E[ix[pre + "calf"]] = self._RL(k)
            E[ix[pre + "foot"]] = self._RL(foot)
        if extra:
            for bi, r in extra.items():
                E[bi] = r @ E[bi] if bi in E else r
        # ---------- forward kinematics (arms solved with IK on the way) ----------
        D, Pn = [None] * nb, [None] * nb
        ovr = {}
        chest = sp[-1]
        l_ua, r_ua = ix["l_upperarm"], ix["r_upperarm"]
        l_fa, r_fa = ix["l_forearm"], ix["r_forearm"]
        use_ik = ik if ik is not None else (extra is None)
        for i in range(nb):
            p = self.par[i]
            Dp = D[p] if p >= 0 else I3
            if p >= 0:
                Pn[i] = Pn[p] + Dp @ (Wp[i] - Wp[p])
            elif root is not None:
                Rr, piv, sh = root[0], root[1], root[2]
                pre_s = root[3] if len(root) > 3 else 0.0
                Pn[i] = piv + Rr @ (Wp[i] + pre_s - piv) + sh
                Dp = Rr
            else:
                Pn[i] = Wp[i].copy()
            if i in ovr:
                D[i] = ovr[i]
            elif p < 0 and root is not None:
                D[i] = root[0] @ E[i] if i in E else root[0]
            else:
                D[i] = Dp @ E[i] if i in E else Dp
            if use_ik and i in (l_ua, r_ua):
                left = (i == l_ua)
                Dc = D[chest]
                C = Pn[chest]
                if arm_local is not None:
                    hl = arm_local[0] if left else arm_local[1]
                    T = C + Dc @ (F * hl[0] + L * hl[1] + U * hl[2])
                    pl_ = arm_local[2]
                    pole = Dc @ (F * pl_[0] + L * (pl_[1] if left else -pl_[1]) + U * pl_[2])
                else:
                    Fh = Dc @ F
                    Fh = np.array([Fh[0], Fh[1], 0.0])
                    Fh = Fh / (np.linalg.norm(Fh) or 1.0)
                    Fa = Fh * math.cos(pitch) + U * math.sin(pitch)
                    Ua = U - Fa * float(np.dot(U, Fa))
                    Ua = Ua / (np.linalg.norm(Ua) or 1.0)
                    La = _cross3(Ua, Fa)
                    back = 1.6 * kick
                    up = 0.9 * kick
                    if melee:
                        s_ = 1.0 - max(0.0, min(1.0, kick))          # 0 at the start of a slash .. 1 at the end
                        sw = math.sin(s_ * math.pi) if kick > 0 else 0.0
                        Tr = C + Fa * (10 + 8 * sw) - La * (6 - 9 * s_ * (kick > 0)) - Ua * (8 - 8 * sw)
                        Tl = C + Fa * 4 + La * 5 - Ua * 17
                    else:
                        Tr = C + Fa * (8.0 - back) - La * 3.0 - Ua * (12.0 - up)
                        Tl = C + Fa * (21.0 - back) + La * 1.0 - Ua * (7.0 - up)
                        if sprint > 0:                                # low ready: weapon tucked in close, muzzle down
                            Tr = Tr + (C + Fa * 6.5 - La * 3.5 - Ua * 14.5 - Tr) * sprint
                            Tl = Tl + (C + Fa * 12.5 + La * 2.0 - Ua * 10.5 - Tl) * sprint
                        if reload is not None:
                            down = _sstep(0.0, 0.22, reload) - _sstep(0.68, 0.92, reload)
                            slap = 1.5 * math.sin(max(0.0, min(1.0, (reload - 0.4) / 0.2)) * math.pi)
                            pouch = C + Fa * 6 + La * 5 - Ua * (19.0 + slap)
                            Tl = Tl + (pouch - Tl) * down
                            Tr = Tr + (C + Fa * 7 - La * 3 - Ua * 14 - Tr) * down * 0.5
                    if air_arms > 0:                                  # arms lift and open for balance
                        Tr = Tr + U * (3.5 * air_arms) - La * (3.0 * air_arms)
                        Tl = Tl + U * (3.5 * air_arms) + La * (3.0 * air_arms)
                    if arm_drop > 0:
                        Tr = Tr - U * (2.5 * arm_drop)
                        Tl = Tl - U * (2.5 * arm_drop)
                    bob = 0.5 * self.mv * (1.0 + 1.2 * sprint) * math.sin(self.ph * 2 + (0 if left else 1.0))
                    T = (Tl if left else Tr) + U * bob
                    pole = -Ua * 1.0 + (La if left else -La) * 0.55
                fa = l_fa if left else r_fa
                Dua, Dfa = self._arm(i, fa, Dp, Pn[i], T, pole)
                D[i] = Dua
                ovr[fa] = Dfa
        # ground lock: keep the lowest foot on the floor (and, squatting, the feet under the hips)
        shift = np.zeros(3)
        if ground and root is None:
            fz = min(Pn[ix[k]][2] for k in ("l_foot", "r_foot", "l_toe0", "r_toe0") if k in ix)
            rz = min(Wp[ix[k]][2] for k in ("l_foot", "r_foot", "l_toe0", "r_toe0") if k in ix)
            shift[2] = rz - fz
            if squat > 0:
                mf = 0.5 * (Pn[ix["l_foot"]] + Pn[ix["r_foot"]])
                rmf = 0.5 * (Wp[ix["l_foot"]] + Wp[ix["r_foot"]])
                shift = shift - F * float(np.dot(mf - rmf, F)) * min(1.0, squat) * 0.9
        self._skin(D, Pn, shift)

    def _skin_rest(self):
        nb = self.m.nb
        D = [self._I3] * nb
        self._skin(D, [np.asarray(w) for w in self.Wp], np.zeros(3))

    def _skin(self, D, Pn, shift):
        """Linear-blend skinning: every (vertex, bone) influence is moved with one small matmul per
        bone, then the weighted results are summed per vertex."""
        Dt = np.ascontiguousarray(np.stack(D).transpose(0, 2, 1), np.float32)     # (nb,3,3), transposed
        T = (np.asarray(Pn) + shift).astype(np.float32)
        o = self._o
        er = self.er32
        for b, s0, e0 in self._act:
            seg = o[s0:e0]
            np.dot(er[s0:e0], Dt[b], out=seg)
            seg += T[b]
        o *= self.ew32
        ev, nv = self.ev, self.nv
        out = np.empty((nv, 3), np.float32)
        for c in range(3):
            out[:, c] = np.bincount(ev, weights=o[:, c], minlength=nv)
        self.vtx = np.ascontiguousarray(out @ self.Rg.T.astype(np.float32))

    # ---- public: death ----
    # One keyframe track per fall style. phi / roll / yaw tip the whole body about a point on the floor,
    # lean / twist / head bend the spine, la.. are (hip, knee, ankle) per leg and lh.. / rh.. are hand targets
    # in the chest's own frame (forward, left, up), so the arms are solved by IK whatever the rest pose is.
    _DK = ("phi", "roll", "yaw", "lean", "twist", "head", "la", "lk", "lp", "ra", "rk", "rp",
           "lhf", "lhl", "lhu", "rhf", "rhl", "rhu")
    _DK0 = dict(phi=0.0, roll=0.0, yaw=0.0, lean=0.0, twist=0.0, head=0.0,
                la=0.0, lk=0.03, lp=0.0, ra=0.0, rk=0.03, rp=0.0,
                lhf=21.0, lhl=1.0, lhu=-7.0, rhf=8.0, rhl=-3.0, rhu=-12.0)      # weapon hold
    _DSPEC = {
        # 0: shot in the chest - jolts back, knees give, sits down and falls on the back
        0: (-6.0, [
            (0.00, {}),
            (0.07, dict(lean=-0.30, head=-0.45, BL=(0.10, 0.32, 0.0), BH=(4, 18, 9))),
            (0.22, dict(lean=-0.10, head=-0.25, LL=(0.55, 1.0, 0.0), RL=(0.35, 0.8, 0.0), BH=(1, 20, 6))),
            (0.42, dict(phi=-0.35, lean=0.05, head=-0.10, LL=(0.85, 1.6, 0.0), RL=(0.70, 1.3, 0.0), BH=(-3, 19, 6))),
            (0.60, dict(phi=-1.00, lean=0.0, head=0.05, LL=(0.80, 1.4, 0.0), RL=(0.60, 1.2, 0.0), BH=(-4, 18, 8))),
            (0.74, dict(phi=-1.50, lean=-0.05, head=-0.35, LL=(0.50, 1.0, 0.0), RL=(0.30, 0.8, 0.0), BH=(0, 21, -1))),
            (0.88, dict(phi=-1.40, head=0.0, BH=(0, 21, 3))),
            (1.15, dict(phi=-1.50, head=0.10, twist=0.15, LL=(0.30, 0.60, 0.3), RL=(0.02, 0.12, 0.35), BH=(-2, 20, -3))),
        ]),
        # 1: shot in the back - arches, knees buckle, kneels, then folds forward onto the chest
        1: (5.0, [
            (0.00, {}),
            (0.08, dict(lean=-0.30, head=-0.40, BL=(0.05, 0.25, 0.0), BH=(-8, 15, 6))),
            (0.24, dict(phi=0.10, lean=0.15, head=0.0, LL=(0.70, 1.35, 0.0), RL=(0.45, 1.0, 0.0), BH=(3, 9, -14))),
            (0.46, dict(phi=0.30, lean=0.35, head=0.3, LL=(0.20, 2.2, 0.0), RL=(0.05, 2.0, 0.0), BH=(9, 8, -12))),
            (0.68, dict(phi=0.95, lean=0.30, head=0.1, LL=(0.05, 1.6, 0.0), RL=(-0.05, 1.2, 0.0), BH=(16, 8, -6))),
            (0.86, dict(phi=1.50, lean=0.10, head=-0.25, twist=0.25, LL=(-0.05, 0.5, 0.0), RL=(0.10, 0.2, 0.0), BH=(14, 12, 0))),
            (1.05, dict(phi=1.42, head=-0.10)),
            (1.40, dict(phi=1.50, head=-0.15, twist=0.3, LL=(-0.10, 0.15, 0.5), RL=(0.25, 0.85, 0.4), LH=(10, 17, -2), RH=(4, -15, -2))),
        ]),
        # 2: spins away from the hit and crumples onto one side (mirrored by the fall direction)
        2: (0.0, [
            (0.00, {}),
            (0.08, dict(twist=0.35, lean=-0.2, head=-0.3, yaw=0.25, BL=(0.10, 0.32, 0.0), LH=(2, 17, 7), RH=(-6, -14, 6))),
            (0.26, dict(yaw=0.55, roll=-0.15, lean=0.2, twist=0.2, LL=(0.65, 1.3, 0.0), RL=(0.35, 0.95, 0.0), LH=(6, 14, -4), RH=(8, -6, -8))),
            (0.48, dict(yaw=0.85, roll=0.55, phi=0.2, lean=0.25, LL=(0.80, 1.5, 0.0), RL=(0.5, 1.2, 0.0), LH=(6, 12, -8), RH=(4, -10, -14))),
            (0.68, dict(yaw=1.0, roll=1.40, phi=0.25, lean=0.2, LL=(0.9, 1.5, 0.0), RL=(0.5, 1.1, 0.0), LH=(12, 5, -8), RH=(6, -8, -10))),
            (0.82, dict(roll=1.50, head=-0.15)),
            (1.00, dict(roll=1.40, yaw=0.95)),
            (1.30, dict(roll=1.50, yaw=0.95, head=0.1, LL=(0.75, 1.35, 0.2), RL=(0.3, 0.9, 0.0), LH=(10, 12, -10), RH=(4, -6, -6))),
        ]),
        # 3: shot while crouching - tips over from the squat
        3: (3.0, [
            (0.00, dict(lean=0.30, BL=(0.95, 1.70, 0.0))),
            (0.08, dict(lean=0.05, head=-0.3, BH=(4, 16, 8))),
            (0.28, dict(phi=0.25, roll=0.2, lean=0.3, head=0.0, LL=(1.0, 1.9, 0.0), RL=(0.8, 1.6, 0.0), BH=(6, 10, -14))),
            (0.52, dict(phi=0.7, roll=0.8, yaw=0.3, lean=0.3, LL=(0.8, 1.6, 0.0), RL=(0.5, 1.2, 0.0), LH=(8, 12, -10), RH=(8, -8, -12))),
            (0.72, dict(phi=1.1, roll=1.35, yaw=0.5, LL=(0.6, 1.2, 0.0), RL=(0.3, 0.8, 0.0))),
            (1.00, dict(phi=1.15, roll=1.45, yaw=0.5, head=0.1, LL=(0.5, 1.0, 0.2), RL=(0.2, 0.6, 0.3), LH=(10, 12, -8), RH=(2, -8, -4))),
        ]),
        # 4: shot mid-slide - slides out flat on the back
        4: (-8.0, [
            (0.00, dict(lean=-0.50, LL=(1.30, 0.05, -0.25), RL=(0.55, 2.0, 0.2))),
            (0.10, dict(lean=-0.70, head=-0.4, BH=(4, 18, 9))),
            (0.35, dict(phi=-0.7, lean=-0.3, LL=(1.2, 0.2, 0.0), RL=(0.9, 1.3, 0.0), BH=(-2, 19, 5))),
            (0.65, dict(phi=-1.35, lean=-0.1, head=-0.2, LL=(0.35, 0.1, 0.2), RL=(0.15, 0.35, 0.3), BH=(0, 21, -2))),
            (0.80, dict(phi=-1.28, head=0.0)),
            (1.10, dict(phi=-1.35, head=0.1, twist=0.1, BH=(-2, 20, -3))),
        ]),
    }
    _DCACHE = {}

    @classmethod
    def _dead_track(cls, style, mirror):
        ck = (style, mirror)
        if ck in cls._DCACHE:
            return cls._DCACHE[ck]
        poff, spec = cls._DSPEC.get(style, cls._DSPEC[0])
        cur = dict(cls._DK0)
        T, V = [], []
        for t, kw in spec:
            kw = dict(kw)
            if "BL" in kw:
                kw["LL"] = kw["RL"] = kw.pop("BL")
            if "BH" in kw:
                f, l, u = kw.pop("BH")
                kw["LH"], kw["RH"] = (f, l, u), (f, -l, u)
            for nm, pre in (("LL", "l"), ("RL", "r")):
                if nm in kw:
                    kw[pre + "a"], kw[pre + "k"], kw[pre + "p"] = kw.pop(nm)
            for nm, pre in (("LH", "lh"), ("RH", "rh")):
                if nm in kw:
                    kw[pre + "f"], kw[pre + "l"], kw[pre + "u"] = kw.pop(nm)
            cur.update(kw)
            T.append(t)
            V.append([cur[k] for k in cls._DK])
        T, V = np.array(T, np.float64), np.array(V, np.float64)
        if mirror < 0:
            o = V.copy()
            o[:, 1], o[:, 2], o[:, 4] = -V[:, 1], -V[:, 2], -V[:, 4]
            o[:, 6:9], o[:, 9:12] = V[:, 9:12], V[:, 6:9]
            o[:, 12], o[:, 13], o[:, 14] = V[:, 15], -V[:, 16], V[:, 17]
            o[:, 15], o[:, 16], o[:, 17] = V[:, 12], -V[:, 13], V[:, 14]
            V = o
        cls._DCACHE[ck] = (T, V, poff, float(T[-1]))
        return cls._DCACHE[ck]

    @staticmethod
    def _kf_eval(T, V, t):
        """Cubic Hermite through the keys with finite-difference tangents: no stop-and-go between keys."""
        n = len(T)
        if t <= T[0]:
            return V[0]
        if t >= T[-1]:
            return V[-1]
        i = int(np.searchsorted(T, t, side="right")) - 1
        h = T[i + 1] - T[i]
        s = (t - T[i]) / h

        def tan(j):
            if j <= 0 or j >= n - 1:
                return np.zeros(V.shape[1])
            return (V[j + 1] - V[j - 1]) / (T[j + 1] - T[j - 1])
        m0, m1 = tan(i) * h, tan(i + 1) * h
        s2, s3 = s * s, s * s * s
        return (2 * s3 - 3 * s2 + 1) * V[i] + (s3 - 2 * s2 + s) * m0 + (-2 * s3 + 3 * s2) * V[i + 1] + (s3 - s2) * m1

    def animate_dead(self, t, ddir=1.0, style=0):
        """t = seconds since death, style 0..4 (back / forward / spin-to-the-side / from a crouch / from a slide),
        ddir = +-1 mirrors the side falls. Holds the last frame (the corpse)."""
        if not self.can:
            return
        mirror = 1.0 if ddir >= 0 else -1.0
        T, V, poff, dur = self._dead_track(int(style), mirror if style in (2, 3) else 1.0)
        tt = min(max(t, 0.0), dur)
        key = ("d", round(tt * 50), int(style), mirror)
        self._since = 1.0
        if key == self._key:
            return
        self._key = key
        v = self._kf_eval(T, V, tt)
        phi, roll, yaw, lean, twist, head = (float(x) for x in v[:6])
        legs = [tuple(float(x) for x in v[6:9]), tuple(float(x) for x in v[9:12])]
        # blend out of the last living pose so the fall starts from whatever the player was doing
        snap = getattr(self, "_snap", None)
        b = 1.0 - _sstep(0.0, 0.14, tt)
        if snap is not None and b > 0:
            legs = [self._mix(legs[i], snap[0][i], b) for i in (0, 1)]
            lean = lean + (snap[1] - lean) * b
        ix, Wp = self.ix, self.Wp
        t_, c_ = self._seg
        reach = max(t_ * math.cos(g[0]) + c_ * math.cos(g[0] - g[1]) for g in legs)
        drop = max(0.0, t_ + c_ - reach)                    # how far folded legs let the pelvis sink
        floor = min(Wp[ix[k]][2] for k in ("l_foot", "r_foot", "l_toe0", "r_toe0") if k in ix)
        piv = np.array([Wp[0][0], Wp[0][1], floor]) + self.F * poff
        Rr = self._RU(yaw) @ self._RF(roll) @ self._RL(phi)
        hl = (float(v[12]), float(v[13]), float(v[14]))
        hr = (float(v[15]), float(v[16]), float(v[17]))
        self._solve(legs, lean, twist, 0.0, 0.0, None, False, ground=False,
                    root=(Rr, piv, np.zeros(3), -self.U * drop),
                    extra={ix["head1"]: self._RL(head)}, ik=True, arm_local=(hl, hr, (0.0, 0.6, -1.0)))
        vt = self.vtx
        vt[:, 2] -= (vt[:, 2].min() - self.rest_min)


def make_vm(buf, pack=None, arms=None, css_hands=True):
    """View-model instance from whatever is stored in game.asset['mdl'] (GoldSrc bytes or a Source spec).
    arms: optional SrcSpec of an arms model, bone-merged onto Source weapons.
    css_hands: a Source weapon with no hands of its own and no arms model picked gets CS:S style hands."""
    if isinstance(buf, SrcSpec):
        a = None
        if arms is not None:
            try:
                a = arms.load()
            except Exception as ex:
                print("Arms model failed:", ex)
        data = buf.load()
        if data.has_hands:
            a = None
        elif a is None and css_hands:
            a = css_hands_for(data)
        return SrcInst(data, a)
    return MdlInst(MdlData(buf, pack))


def extract_rar(path, out):
    """.rar has no pure-Python reader, so use whatever extractor the PC has: 7-Zip, unrar / WinRAR,
    unar or tar (Windows 10+ ships a tar that reads .rar)."""
    import shutil
    import subprocess
    path, out = str(path), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cmds = []
    for exe in ("7z", "7zz", "7za"):
        w = shutil.which(exe)
        if w:
            cmds.append([w, "x", "-y", "-o" + str(out), path])
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        w = os.path.join(base, "7-Zip", "7z.exe")
        if os.path.exists(w):
            cmds.append([w, "x", "-y", "-o" + str(out), path])
        for nm in ("UnRAR.exe", "WinRAR.exe"):
            w = os.path.join(base, "WinRAR", nm)
            if os.path.exists(w):
                cmds.append([w, "x", "-y", path, str(out) + os.sep])
    w = shutil.which("unrar")
    if w:
        cmds.append([w, "x", "-y", path, str(out) + os.sep])
    w = shutil.which("unar")
    if w:
        cmds.append([w, "-o", str(out), "-f", path])
    for exe in ("bsdtar", "tar"):
        w = shutil.which(exe)
        if w:
            cmds.append([w, "-xf", path, "-C", str(out)])
    def good():
        files = [f for f in out.rglob("*") if f.is_file() and f.suffix.lower() in (".mdl", ".vvd", ".vtx", ".vtf", ".vmt")]
        return any(f.suffix.lower() == ".mdl" for f in files) and all(f.stat().st_size > 0 for f in files)

    def fresh():
        shutil.rmtree(out, ignore_errors=True)       # old 7-Zip builds leave empty files for RAR5: start clean
        out.mkdir(parents=True, exist_ok=True)
    for cmd in cmds:
        fresh()
        try:
            subprocess.run(cmd, capture_output=True, timeout=900)
        except Exception:
            continue
        if good():
            return
    try:                                              # python-libarchive-c (needs the libarchive library)
        import libarchive
        fresh()
        with libarchive.file_reader(path) as arc:
            for ent in arc:
                if ent.isdir:
                    continue
                dst = out / Path(ent.pathname.replace("\\", "/"))
                dst.parent.mkdir(parents=True, exist_ok=True)
                with open(dst, "wb") as fh:
                    for blk in ent.get_blocks():
                        fh.write(blk)
        if good():
            return
    except Exception:
        pass
    raise RuntimeError("cannot unpack .rar - install 7-Zip (or WinRAR), or unpack it yourself and drop the folder / .zip in")


def is_src_player(rel):
    """models/player/... (CS:S, CS:GO legacy player models). Arms / hands / ragdoll helpers are not players."""
    rel = rel.replace("\\", "/").lower()
    nm = rel.rsplit("/", 1)[-1]
    if not re.search(r"(^|/)player/", rel) or not nm.endswith(".mdl"):
        return False
    return not re.search(r"arms|hands|sleeve|glove|ragdoll|_anim|^v_|^w_|^p_", nm)


def src_players(arch):
    """Player models (models/player/*.mdl) inside a Source archive."""
    return sorted(nm for nm in arch.files if is_src_player(nm))


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
        self.spr = False  # sprinting (physics)
        self.sprk = 0.0   # eased sprint pose 0..1 (animation)
        self.slk = 0.0   # eased slide pose 0..1 (camera drop, lean, viewmodel)
        self.cr = 0.0
        self.wj = 0
        self.wjt = 99.0           # seconds since the last wall kick (animation)
        self.wjn = (0.0, 0.0)     # normal of the wall it kicked off (points away from the wall)
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
        self._new_pl = 0
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
        self.fov = 75.0          # world camera FOV right now (slider + sprint push, 20 when scoped)
        self.fov_cfg = 75.0      # FOV slider in Settings > Video (50..110), world only
        self.vm_fov = 75.0       # viewmodel FOV: never follows the slider
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
                spec = self.asset["player"][name]
                if isinstance(spec, SrcSpec):          # CS:S / CS:GO model, posed in code
                    pm = SrcPlayerInst(spec.load())
                    pm.last = None
                    e.pm_dz = -(self.world.feet_offset + pm.min_z())
                    e.pm = pm
                    e.anim = fresh_anim()
                    return
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
            cap = MAXS * (SPRINT_MULT if e.spr else 1.0) * BHCAP
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
            ms = MAXS * (SPRINT_MULT if e.spr else 1.0)
            self.accel(e, wx, wy, ms, ACC, dt, ms)
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
            k = max(0.0, e.sl) / 1.1
            kk = k * k * (3 - 2 * k)         # smooth ease-out: friction-like slow-down
            v = 120 + 300 * kk
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
                e.wjt = 0.0
                e.wjn = (nx, ny)
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
        if not e.alive and getattr(pm, "is_src_player", False):
            if not A["dead"]:
                A["dead"] = True
                A["dt"] = 0.0
                A["ds"] = 0                      # keeps the corpse on screen
                A["ddir"] = random.choice((-1.0, 1.0))
                A["gyaw"] = math.degrees(e.yaw)
                # slide -> slides out on the back, crouch -> tips over from the squat, else back / forward / side
                A["dstyle"] = 4 if e.sl > 0 else (3 if e.cr > .5 else random.choice((0, 0, 1, 1, 2)))
            A["dt"] += dt
            pm.animate_dead(A["dt"], A["ddir"], A.get("dstyle", 0))
            return
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
        if getattr(pm, "is_src_player", False):
            self.update_src_player(e, pm, A, dt, spd, in_air, crouch, wpn)
            return

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

    def update_src_player(self, e, pm, A, dt, spd, in_air, crouch, wpn):
        """Source (CS:S / CS:GO) player model: gait, crouch, slide, jump, aim, recoil and reload are
        all computed in code by SrcPlayerInst (these models ship without animations)."""
        if e.reloadT > 0:
            e.reloadT -= dt
        if e.shootT > 0:
            e.shootT -= dt
        rl = None
        melee = bool(wpn and wpn.melee)
        if e.reloadT > 0 and not melee:
            A["rtot"] = max(A.get("rtot", 0.0), e.reloadT)
            rl = 1.0 - e.reloadT / max(A["rtot"], 1e-3)
        else:
            A["rtot"] = 0.0
        kick = max(0.0, min(1.0, e.shootT / 0.15)) if e.shootT > 0 else 0.0
        A["init"] = True
        # sprint pose: the local player eases e.sprk; bots and online players are read off their speed
        est = (spd - MAXS * 1.12) / max(MAXS * (SPRINT_MULT - 1.12), 1.0)
        sprint = max(float(e.sprk), max(0.0, min(1.0, est)))
        # wall kick: seconds since it started and the angle from the legs' facing to the wall
        wall = None
        e.wjt = getattr(e, "wjt", 99.0) + dt
        if e.wjt < 0.62 and in_air:
            wn = getattr(e, "wjn", (0.0, 0.0))
            gy = A["gyaw"] if A["gyaw"] is not None else math.degrees(e.yaw)
            wall = (e.wjt, math.radians(wrap180(math.degrees(math.atan2(-wn[1], -wn[0])) - gy)))
        pm.animate(dt, spd, in_air, crouch and not in_air, e.sl > 0, A["twist"], e.pitch, kick, rl, melee,
                   sprint, float(e.vz), wall)

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
                w.vm = make_vm(asset["mdl"][w._m], asset["pack"].get(key_norm(w._m)), self.src_arms, not self.arms_off)
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
        pls = src_players(arch)
        if not names and not pls:
            return f"{Path(path).name}: no weapon view-model (v_*.mdl) or player model inside."
        for nm in names:
            self.asset["mdl"][nm.rsplit("/", 1)[-1]] = SrcSpec(arch, nm)
        for nm in pls:
            self.add_src_player(nm.rsplit("/", 1)[-1], SrcSpec(arch, nm), Path(path).stem[:14])
        self._new_pl += len(pls)
        if names and src_arms(arch):
            self.add_arms(src_arms(arch).rsplit("/", 1)[-1], SrcSpec(arch, src_arms(arch)))
        parts = []
        if names:
            parts.append(f"{len(names)} CS:GO view-model(s)")
        if pls:
            parts.append(f"{len(pls)} player model(s)")
        return f"{Path(path).name}: {' + '.join(parts)} - pick them in Loadout."

    def add_src_player(self, name, spec, tag=""):
        """Register a Source (CS:S / CS:GO) player model in asset['player']; returns its key."""
        key = name
        old = self.asset["player"].get(key)
        if old is not None and not (isinstance(old, SrcSpec) and old.arch is spec.arch and old.mdl == spec.mdl):
            key = re.sub(r"\.mdl$", "", name) + " (" + (tag or "mod")[:14] + ").mdl"
        self.asset["player"][key] = spec
        self.pm_cache.pop(key, None)
        for tk, sv in self.pm_saved.items():
            if sv == key and not self.pm_cfg[tk]:
                self.pm_cfg[tk] = key
        return key

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
        """.zip / .7z / .rar with CS:GO or CS:S weapon, arms or player model files (models, materials, or a .vpk inside).
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
            if path.suffix.lower() == ".rar":
                extract_rar(path, out)
            elif path.suffix.lower() == ".7z":
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
            return f"{path.name}: no CS:GO / CS:S weapon, arms or player model inside."
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
                    if self._new_pl:
                        pm_chg, self._new_pl = True, 0
                except Exception as ex:
                    status = f"Cannot open {p.name}: {ex}"
                continue
            if n.endswith((".zip", ".7z", ".rar")):  # archive with CS:GO / CS:S weapons, arms and / or player models
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
                srcv = len(data) > 250 and data[:4] == b"IDST" and 44 <= struct.unpack_from("<i", data, 4)[0] <= 49
                if mode == "player" or (srcv and is_src_player(src_rel(p))):
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
                if len(data) > 250 and data[:4] == b"IDST" and 44 <= struct.unpack_from("<i", data, 4)[0] <= 49:
                    # CS:GO / CS:S .mdl (its .vvd/.vtx/.vtf/.vmt are found in the same mod folder)
                    root = src_root(p)
                    arch = self.src_dirs.get(root)
                    if arch is None:
                        arch = self.src_dirs[root] = DirSrcFiles(root)
                    rel = src_rel(p)
                    spec = SrcSpec(arch, rel)
                    if is_src_player(rel) or (mode == "player" and not re.search(r"arms|hands|ragdoll|^[vwp]_", n)):
                        self.add_src_player(n, spec, root.name or "mod")     # player model, posed in code
                        continue
                    if mode == "player":
                        continue
                    if re.search(r"_zoom|_scope|display|dropped|^w_", n):
                        continue
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
                            e.sl = 1.1
                            e.scd = 1.6
                            e.sdx = (c * f + sn * s) / n  # slide the way you are moving (any direction)
                            e.sdy = (sn * f - c * s) / n
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
                    e.slk += ((1.0 if e.sl > 0 else 0.0) - e.slk) * min(1.0, dt * (9 if e.sl > 0 else 6))
                    cdrop = 11 * e.cr
                    e.so = -(cdrop + (16 - cdrop) * e.slk)
                    # sprint: hold the key while moving forward; stops when you crouch, slide, scope or shoot
                    e.spr = bool(K.get(B["sprint"]) and f > 0 and e.sl <= 0 and e.cr < .3
                                 and not (e.zoom and e.w == 0) and not K.get("mouse1"))
                    e.sprk += ((1.0 if (e.spr and math.hypot(e.vx, e.vy) > 60) else 0.0) - e.sprk) * min(1.0, dt * 8)
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

        zoomed = bool(P.zoom and P.w == 0)
        push = 7.0 * P.sprk                                   # slight FOV push while sprinting
        target = 20.0 if zoomed else self.fov_cfg + push      # world: follows the FOV slider
        vm_target = 20.0 if zoomed else 75.0 + push           # viewmodel: fixed, ignores the slider
        if abs(self.fov - target) > .1:
            self.fov += (target - self.fov) * min(1, dt * 20)
        else:
            self.fov = target
        if abs(self.vm_fov - vm_target) > .1:
            self.vm_fov += (vm_target - self.vm_fov) * min(1, dt * 20)
        else:
            self.vm_fov = vm_target

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
                if not self.check_ready():
                    return
                g.player_name = pname()
                g.P.name = g.player_name
                ok = g.net_host(self.room.strip() or (pname() + "'s room"))
                self.status = net.status
                if ok:
                    self.on_launch()

            def do_join(r):
                if not self.check_ready():
                    return
                g.player_name = pname()
                g.P.name = g.player_name
                g.net_join(r)

            def do_offline():
                if self.check_ready():
                    self.on_launch()

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
                button(cx + 232, 226, 200, 36, "Offline match >", do_offline, True)
                button(cx + 444, 226, 100, 36, "Bots: ON" if g.bots_on else "Bots: OFF", toggle_bots)
                button(cx + 556, 226, 104, 36, "Load map", lambda: self.pick("map"))
                miss = self.missing()
                if miss:
                    gfx.text("Pick mods in Loadout first: " + ", ".join(miss), cx + 20, 266, 11, (230, 160, 90))
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
                info = f"{self.lo_team} team: " + (g.pm_cfg[self.lo_team] or "none picked yet")
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
                col, row = divmod(i, 7)
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
            button(cx + 350, 282 + 44 + 6 * 38, 150, 32, "Reset defaults", self.reset_keys)
            button(cx + 512, 282 + 44 + 6 * 38, 150, 32, "Back to play", lambda: self.set_page("play"), True)
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
            panel(cx, 292, cw, 92, "Field of view",
                  "Changes how much of the world you see. The viewmodel (your weapon) stays the same.")
            gfx.text(f"{int(round(g.fov_cfg))}", cx + cw - 20, 14 + 292, 13, (255, 255, 255), bold=True, anchor="r")
            slider(cx + 20, 292 + 62, cw - 190, g.fov_cfg, 50, 110, lambda v: setattr(g, "fov_cfg", float(round(v))))
            button(cx + cw - 150, 292 + 54, 130, 30, "Reset to 75", lambda: setattr(g, "fov_cfg", 75.0))
        gl.glPopMatrix()

    def missing(self):
        """What still needs a mod before a game can start: every weapon slot and both teams' player models."""
        g = self.g
        out = []
        for i, w in enumerate(CFG["weapons"][:3]):
            if not w.vm:
                out.append("weapon %d (%s)" % (i + 1, w.n))
        for tk in ("CT", "T"):
            if not (g.pm_cfg[tk] and g.pm_cfg[tk] in g.asset["player"]):
                out.append("%s player model" % tk)
        return out

    def check_ready(self):
        m = self.missing()
        if m:
            self.status = "Cannot play yet - pick a mod in Loadout for: " + ", ".join(m)
            return False
        return True

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
        if self.lo_tab == "weapons":
            g.set_weapon_pick(self.lo_slot, name)
            self.status = (g.vm_msg or "").strip() or self.status
        else:
            if not name:
                return   # a player model must always be picked: no default box
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
                inst = make_vm(buf, g.asset["pack"].get(key_norm(name)), g.src_arms, not g.arms_off)
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
        if k not in self.pv and isinstance(buf, SrcSpec):
            try:
                self.pv[k] = SrcPlayerInst(buf.load())
            except Exception as ex:
                print("Preview error:", name, ex)
                self.pv[k] = None
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
    if P.slk > 0.001:
        lat = P.sdx * math.sin(P.yaw) - P.sdy * math.cos(P.yaw)   # sideways part of the slide
        gl.glRotatef(P.slk * (1.2 + 2.5 * lat), 0, 0, 1)           # slight lean into the slide
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
    glu.gluPerspective(game.vm_fov, game.aspect or W / max(1, H), VM_NEAR, VM_FAR)
    gl.glMatrixMode(gl.GL_MODELVIEW)
    gl.glLoadIdentity()
    glu.gluLookAt(0, 0, 0, 1, 0, 0, 0, 0, 1)
    hide = P.zoom and P.w == 0
    w = CFG["weapons"][P.w]
    gl.glPushMatrix()
    if P.slk > 0.001:
        gl.glTranslatef(0.4 * P.slk, 0.8 * P.slk, -1.2 * P.slk)    # gun drops and shifts while sliding
        gl.glRotatef(-9 * P.slk, 1, 0, 0)
        gl.glRotatef(-5 * P.slk, 0, 1, 0)
    if P.sprk > 0.001:
        # sprint pose: gun lowered, angled across the body, a little closer
        gl.glTranslatef(-0.5 * P.sprk, 0, -0.7 * P.sprk)
        gl.glRotatef(11 * P.sprk, 0, 1, 0)
        gl.glRotatef(9 * P.sprk, 0, 0, 1)
    gl.glTranslatef(0, math.sin(P.ph) * .7 * P.bob * (1 + .9 * P.sprk),
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
        if isinstance(aj, str) and aj:
            game.arms_want = aj
        pmj = j.get("player_models", {})
        for tk in ("CT", "T"):
            if isinstance(pmj.get(tk), str):
                game.pm_saved[tk] = pmj[tk]
        game.fov_cfg = max(50.0, min(110.0, float(j.get("fov", 75.0) or 75.0)))
        game.fov = game.fov_cfg
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
            "arms": (game.arms_pick or ("" if game.arms_off else None)), "binds": game.binds, "hand_swap": game.hand_swap, "bots_on": game.bots_on, "player_models": game.pm_saved, "weapon_models": {str(k): v for k, v in game.wm_pick.items()}, "aspect": game.aspect, "fov": game.fov_cfg, "vm_offset": game.vm_adj,
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
        "arms": [("CS:GO arms", "*.zip *.7z *.rar *.mdl *.vpk *.vvd *.vtx *.vtf *.vmt")],
        "weapon": [("Weapon files", "*.mdl *.wav *.json *.vpk *.zip *.7z *.rar *.vvd *.vtx *.vtf *.vmt")],
        "player": [("Player models", "*.mdl *.zip *.7z *.rar *.vpk")],
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
                        if f.is_file() and f.suffix.lower() in (".mdl", ".wav", ".json", ".vpk", ".zip", ".7z", ".rar", ".vvd", ".vtx", ".vtf", ".vmt"))
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
        pfiles = sorted(f for f in pdir.rglob("*") if f.is_file() and f.suffix.lower() in (".mdl", ".zip", ".7z", ".rar", ".vpk"))
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
