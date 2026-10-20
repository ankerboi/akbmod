#!/usr/bin/env python3
"""
AKB / ARENA - Browser TDM, rewritten as a native Python game.

Run:       python fad-mod.py   (missing packages install automatically)

Mouse look uses SDL relative mode (raw input, OS pointer acceleration off).
Mods in the "mod" folder are listed in Loadout but NOT equipped automatically - pick the ones you want
(or drag & drop files) before joining a map, or play with none (nothing is ever created for you):
  mod/weapon/   weapon mods (Source engine: CS:GO / L4D2 .vpk packages — models + animations only)
  mod/player/   player models (Source only — loose, .zip / .7z / .rar / .vpk; posed in code)
  mod/arms/     arms/hands packs
  mod/sound/    custom sounds (shoot.wav / hit.wav)
Drag & drop .vpk / .mdl / .wav / .json onto the window, or use Loadout buttons (needs tkinter).

CS 1.6 (GoldSrc) maps/models and CS:S hand meshes are not supported.

Silent Hill look: fog + dim light + flashlight (F toggles it) and vignette.
Online play (Play tab > Online) needs NO port forwarding: rooms on a public MQTT broker,
STUN + UDP hole punching.
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


def _find_sub(parent, names):
    """Existing sub-folder of `parent` whose name matches one of `names` (case-insensitive)."""
    try:
        for d in sorted(parent.iterdir()):
            if d.is_dir() and d.name.lower() in names:
                return d
    except Exception:
        pass
    return None


def mod_folders(*names):
    """Existing folders called one of `names` inside the "mod" folder (next to this script, or in cwd).
    Never creates anything - if the folder is not there, the list is empty."""
    names = tuple(n.lower() for n in names)
    out, seen = [], set()
    for base in (Path(__file__).resolve().parent, Path.cwd()):
        mroot = _find_sub(base, ("mod",))
        if not mroot:
            continue
        d = _find_sub(mroot, names)
        if d:
            k = str(d.resolve())
            if k not in seen:
                seen.add(k)
                out.append(d)
    return out

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
MAX_TWIST = 50.0     # keep idle torso turns restrained instead of twisting sharply
VM_BOB_SIDE = 0.22    # first-person gun sway left/right while running (was 0.7). 0 = none
VM_BOB_UP = 0.28      # first-person gun up/down bounce while running (was 0.9). 0 = none
VM_LAND = 1.0         # gun dip when landing (was 2.2)
VM_IDLE_ANIM = False    # False = the weapon holds a still pose when you stand still; its animation only plays while you move. True = always play the idle animation
SHAKE = 0.6            # screen shake when you shoot: 0 = off, 0.3 = light, 0.6 = medium, 1 = strong
SHAKE_DECAY = 7.0      # how fast the shake dies down (higher = shorter)
MAXS, FRIC, STOPS, ACC, AIRACC, AIRCAP, JUMPV, BHCAP = 250, 5.2, 80, 5.5, 12, 30, 270, 1.1

DEATH_RX = re.compile(r"^(death|die|dead|crouch_?die|head$|gutshot|forward$|back$|left$|right$)", re.I)


def fresh_anim():
    """Per-entity animation state (Source players + legacy fields kept for compatibility)."""
    return {"seq": -1, "time": 0.0, "loop": True, "kind": "",
            "init": False, "gyaw": None, "twist": 0.0,
            "gs": -1, "gt": 0.0, "gl": True, "gk": "",
            "us": -1, "ut": 0.0, "ul": True, "uk": "", "ukey": None, "pst": 0.0,
            "dead": False, "ds": -1, "dt": 0.0, "air": False,
            "state": "", "state_time": 0.0}


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
    vec3 beam = vec3(1.0, 0.92, 0.74) * (spot * 1.35 + wide * 0.10) * att * on * (1.0 - uDay);
    vec3 amb = (uMode > 0.5) ? vec3(0.50, 0.50, 0.52) : vec3(0.15, 0.16, 0.17);
    amb = mix(amb, vec3(1.0, 0.98, 0.93), uDay);             // day: full sunlight
    vec3 lit = base.rgb * (amb + beam);
    float l = dot(lit, vec3(0.299, 0.587, 0.114));
    vec3 night = mix(vec3(l), lit, 0.72) * vec3(1.04, 1.0, 0.90);   // drained, slightly sickly tint
    lit = mix(night, lit, uDay);
    vec3 fogc = uFog + vec3(0.95, 0.88, 0.7) * wide * 0.07 * on * (1.0 - att * 0.5) * (1.0 - uDay);
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


# --------------------------------------------------------------------------
# WAD / BSP
# --------------------------------------------------------------------------
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

class FadMapWorld(World):
    """A map compiled by the FAD-MOD Map Builder (.fadmap): boxes with optional textures + team spawns."""

    _FACES = ArenaWorld._FACES

    def __init__(self, d):
        import base64
        import zlib
        if not isinstance(d, dict) or d.get("format") != "fadmap":
            raise ValueError("not a FAD-MOD map file")
        self.name = str(d.get("name", "map"))
        self.B, self.meta = [], []
        for b in d.get("boxes", []):
            v = [float(x) for x in b["b"]]
            for i in range(3):
                if v[i] > v[i + 3]:
                    v[i], v[i + 3] = v[i + 3], v[i]
            self.B.append(v)
            col = b.get("c") or (176, 160, 144)
            self.meta.append((int(b.get("t", -1)), int(b.get("tt", b.get("t", -1)) if b.get("tt") is not None else b.get("t", -1)),
                              float(b.get("tile", 128) or 128), tuple(col)))
        if not self.B:
            raise ValueError("the map has no boxes")
        self.spawns = [[], []]
        for team, lst in enumerate((d.get("spawns") or [[], []])[:2]):
            for sp in lst:
                p = sp["p"]
                # builder z = feet, game z = body centre (feet + 36); +0.5 so we never start inside the floor
                self.spawns[team].append([float(p[0]), float(p[1]), float(p[2]) + 36.5, float(sp.get("yaw", 0))])
        for team in (0, 1):
            if not self.spawns[team]:
                top = max(self.B, key=lambda b: (b[3] - b[0]) * (b[4] - b[1]))
                self.spawns[team].append([(top[0] + top[3]) / 2, (top[1] + top[4]) / 2, top[5] + 36.5, 0.0])
        vz = d.get("void_z")
        self.void_z = float(vz) if vz is not None else min(b[2] for b in self.B) - 800.0
        # textures: decode now (pygame surfaces), upload to the GPU on first draw
        self._tex_raw = []
        for t in d.get("textures", []):
            try:
                if "png" in t:
                    surf = pygame.image.load(io.BytesIO(base64.b64decode(t["png"])), "x.png").convert_alpha()
                    w, h = surf.get_size()
                    data = pygame.image.tostring(surf, "RGBA", False)
                else:
                    w, h = int(t["w"]), int(t["h"])
                    data = zlib.decompress(base64.b64decode(t["raw"]))
                self._tex_raw.append((data, w, h))
            except Exception as ex:
                print("map texture failed:", ex)
                self._tex_raw.append(None)
        self._tids = None
        self._white = None
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

    def normal_at(self, x, y, z, want_up=False, move=None):
        return ArenaWorld.normal_at(self, x, y, z, want_up, move)

    @staticmethod
    def _faces(b):
        x0, y0, z0, x1, y1, z1 = b
        return (
            ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1)),
            ((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0), (0, 0, -1)),
            ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0)),
            ((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (0, 1, 0)),
            ((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (-1, 0, 0)),
            ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0)),
        )

    @staticmethod
    def _uv(p, n, tile):
        if abs(n[2]) > .5:
            return p[0] / tile, p[1] / tile
        if abs(n[0]) > .5:
            return p[1] / tile, -p[2] / tile
        return p[0] / tile, -p[2] / tile

    @staticmethod
    def _shade(n):
        if n[2] > .5:
            return 1.0
        if n[2] < -.5:
            return 0.5
        return 0.68 if abs(n[0]) > .5 else 0.82

    def _build(self):
        self._white = make_texture(np.full((2, 2, 4), 255, np.uint8), 2, 2, mip=False)
        self._tids = []
        for t in self._tex_raw:
            if t is None:
                self._tids.append(None)
                continue
            data, w, h = t
            arr = np.frombuffer(data, np.uint8).reshape(h, w, 4)
            self._tids.append(make_texture(arr, w, h))
        self._list = gl.glGenLists(1)
        gl.glNewList(self._list, gl.GL_COMPILE)
        for b, (t, tt, tile, col) in zip(self.B, self.meta):
            for f in self._faces(b):
                n = f[4]
                ti = tt if n[2] > .5 else t
                tid = self._tids[ti] if 0 <= ti < len(self._tids) else None
                gl.glBindTexture(gl.GL_TEXTURE_2D, tid or self._white)
                sh = self._shade(n)
                if tid:
                    gl.glColor3f(sh, sh, sh)
                else:
                    gl.glColor3f(col[0] / 255 * sh, col[1] / 255 * sh, col[2] / 255 * sh)
                gl.glBegin(gl.GL_QUADS)
                for pt in f[:4]:
                    u, v = self._uv(pt, n, tile)
                    gl.glTexCoord2f(u, v)
                    gl.glVertex3f(*pt)
                gl.glEnd()
        gl.glEndList()

    def draw(self):
        if self._list is None:
            self._build()
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glCallList(self._list)
        gl.glColor4f(1, 1, 1, 1)



# --------------------------------------------------------------------------
# Source engine (CS:GO) weapon mods: .vpk packages
#   VPK v1/v2 reader, MDL v44-49 + VVD + VTX model loader (with RLE animation),
#   VTF textures (DXT1/3/5 + common uncompressed formats), KeyValues parser.
# Pure python + numpy. No GL here: SrcModel only produces numpy arrays.
# --------------------------------------------------------------------------
class VpkArchive:
    """Reads a Valve .vpk (single file, or *_dir.vpk with *_000.vpk archives). Lazy: files are
    read from disk only when asked for, so a 165 MB package costs no memory.

    Accepts incomplete downloads (.vpk.part): the directory tree is still parsed and any entry
    whose payload is fully present on disk can be read; truncated entries raise a clear error.
    """

    def __init__(self, path):
        self.path = str(path)
        self.files = {}
        self._fh = {}
        self._disk_size = Path(path).stat().st_size
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
        # Flag truncated packages (common with browser .vpk.part downloads)
        self.incomplete = False
        for arch, off, ln, pre in self.files.values():
            if arch == 0x7FFF and ln and (self.base + off + ln) > self._disk_size:
                self.incomplete = True
                break

    def complete(self, name):
        """True when the full payload for this entry is present on disk."""
        arch, off, ln, pre = self.files[name.lower().replace("\\", "/")]
        if arch != 0x7FFF:
            return True  # multi-file VPK: assume paired _000.vpk is intact
        return (self.base + off + ln) <= self._disk_size

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
        key = name.lower().replace("\\", "/")
        arch, off, ln, pre = self.files[key]
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
            if arch == 0x7FFF and (self.base + off + start2 + length2) > self._disk_size:
                raise ValueError(
                    "%s is incomplete in this VPK (download truncated at %d bytes; need ~%d). "
                    "Re-download the full .vpk." % (key, self._disk_size, self.base + off + ln))
            fh = self._open(arch)
            fh.seek((self.base if arch == 0x7FFF else 0) + off + start2)
            chunk = fh.read(min(length2, ln - start2))
            if len(chunk) < length2:
                raise ValueError("%s truncated in VPK (got %d of %d bytes)" % (key, len(chunk), length2))
            out += chunk
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


HIT_HALF_W = 12.0     # hitbox half-width (was 19); smaller = narrower hitbox
HIT_HALF_H = 29.0     # hitbox half-height standing (was 35)
HIT_HALF_H_LOW = 17.0 # half-height while sliding / crouching (was 21)
PLAYER_SCALE = 0.85   # visual + hitbox size of other players (1.0 = original); lower = smaller


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
                              "loop": bool(I(p + 12) & 1), "descs": descs})
        # External .ani anim blocks (HL2 / L4D2 shared anims: male_shared.ani, …)
        self.anim_blocks = [(0, 0)]
        self.ani_data = None  # bytes of the companion .ani when present
        try:
            nblk, blk_ix = I(352), I(356)
            if 0 < nblk < 512 and 0 < blk_ix < len(b) - 8:
                self.anim_blocks = [struct.unpack_from("<ii", b, blk_ix + i * 8) for i in range(nblk)]
        except Exception:
            pass
        self._cache = {}

    def attach_ani(self, ani_bytes):
        """Attach a companion .ani (IDAG) file so FRAMEANIM / block animations can be decoded."""
        if not ani_bytes or len(ani_bytes) < 16:
            return
        if ani_bytes[:4] not in (b"IDAG", b"IDST"):
            # still accept raw blocks
            pass
        self.ani_data = ani_bytes
        self._cache.clear()

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

    def _anim_base(self, d, frame):
        """Return (buffer, base_offset, frame_within_section) for an anim desc."""
        block, idx = d["block"], d["idx"]
        fr = frame
        # Sectioned anims: pick the section that holds this frame
        if d["sect"] and d["sfr"]:
            n, sfr = d["n"], d["sfr"]
            if n > sfr and frame == n - 1:
                si, fr = n // sfr + 1, 0
            else:
                si = frame // sfr
                fr = frame - si * sfr
            sp = d["p"] + d["sect"] + si * 8
            block, idx = struct.unpack_from("<ii", self.b, sp)
        if block == 0:
            if idx == 0:
                return None, 0, fr
            return self.b, d["p"] + idx, fr
        # External .ani block
        if not self.ani_data or block < 0 or block >= len(self.anim_blocks):
            return None, 0, fr
        ds, de = self.anim_blocks[block]
        off = ds + idx
        if off < 0 or off >= len(self.ani_data):
            return None, 0, fr
        return self.ani_data, off, fr

    @staticmethod
    def _quat48(buf, o):
        x, y, z = struct.unpack_from("<HHH", buf, o)
        wneg = z >> 15
        qx = (x - 32768) / 32768.0
        qy = (y - 32768) / 32768.0
        qz = ((z & 0x7FFF) - 16384) / 16384.0
        qw = math.sqrt(max(0.0, 1 - qx * qx - qy * qy - qz * qz))
        return (qx, qy, qz, -qw if wneg else qw)

    @staticmethod
    def _quat48s(buf, o):
        """Quaternion48S used by FRAMEANIM const/anim rot2."""
        v = struct.unpack_from("<Q", buf, o)[0] if False else None
        # 3x16-bit with different packing — fall back to quat48 layout used by Crowbar
        return SrcModel._quat48(buf, o)

    def _decode_frameanim(self, buf, base, frame, pos, quat, delta):
        """STUDIO_FRAMEANIM (0x40): constants + per-frame bone packed data."""
        nb = self.nb
        if base + 24 > len(buf):
            return pos, quat
        c_off, f_off, f_len = struct.unpack_from("<iii", buf, base)
        bone_flags = buf[base + 24: base + 24 + nb]
        # constants stream
        cp = base + c_off if c_off else None
        # frame stream
        fp = base + f_off + frame * f_len if f_off else None
        if fp is not None and (fp < 0 or fp + f_len > len(buf)):
            return pos, quat
        for bone in range(min(nb, len(bone_flags))):
            fl = bone_flags[bone]
            B = self.bones[bone]
            if fl == 0:
                continue
            # constants first
            if cp is not None:
                if fl & 0x20:  # CONST_POS2: float32 x3
                    pos[bone] = struct.unpack_from("<3f", buf, cp)
                    cp += 12
                elif fl & 0x01:  # CONST_POS: Vector48
                    pos[bone] = np.frombuffer(buf, "<f2", 3, cp).astype(np.float64)
                    cp += 6
                if fl & 0x40:  # CONST_ROT2
                    quat[bone] = self._quat48(buf, cp)
                    cp += 6
                elif fl & 0x02:  # CONST_ROT
                    quat[bone] = self._quat48(buf, cp)
                    cp += 6
            # per-frame data
            if fp is not None:
                if fl & 0x10:  # ANIM_POS2: float32 x3
                    pos[bone] = struct.unpack_from("<3f", buf, fp)
                    fp += 12
                elif fl & 0x04:  # ANIM_POS: Vector48
                    pos[bone] = np.frombuffer(buf, "<f2", 3, fp).astype(np.float64)
                    fp += 6
                if fl & 0x80:  # ANIM_ROT2
                    quat[bone] = self._quat48(buf, fp)
                    fp += 6
                elif fl & 0x08:  # ANIM_ROT
                    quat[bone] = self._quat48(buf, fp)
                    fp += 6
            if delta and not (fl & (0x01 | 0x04 | 0x10 | 0x20)):
                pass  # keep zero pos
            elif not delta and not (fl & (0x01 | 0x04 | 0x10 | 0x20)):
                pos[bone] = B["pos"]
        return pos, quat

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
        buf, base, fr = self._anim_base(d, frame)
        if buf is None:
            self._cache[key] = (pos, quat)
            return pos, quat
        # FRAMEANIM (flag 0x40): newer by-frame encoding used by HL2/L4D2 shared anims
        if d["flags"] & 0x40:
            pos, quat = self._decode_frameanim(buf, base, fr, pos, quat, delta)
            self._cache[key] = (pos, quat)
            return pos, quat
        # Classic RLE bone stream
        p = base
        guard = 0
        while guard < 4096:
            guard += 1
            if p + 4 > len(buf):
                break
            bone, flags, nxt = buf[p], buf[p + 1], struct.unpack_from("<h", buf, p + 2)[0]
            if bone < nb:
                B = self.bones[bone]
                q0 = p + 4
                if flags & 0x2:        # RAWROT: Quaternion48
                    quat[bone] = self._quat48(buf, q0)
                    q0 += 6
                elif flags & 0x20:     # RAWROT2: Quaternion64
                    v = struct.unpack_from("<Q", buf, q0)[0]
                    qx = ((v & 0x1FFFFF) - 1048576) / 1048576.5
                    qy = (((v >> 21) & 0x1FFFFF) - 1048576) / 1048576.5
                    qz = (((v >> 42) & 0x1FFFFF) - 1048576) / 1048576.5
                    qw = math.sqrt(max(0.0, 1 - qx * qx - qy * qy - qz * qz))
                    quat[bone] = (qx, qy, qz, -qw if (v >> 63) else qw)
                    q0 += 8
                rot_ptr = None
                if flags & 0x8:
                    rot_ptr = q0
                    q0 += 6
                pos_ptr = None
                if flags & 0x4:
                    pos_ptr = q0
                    q0 += 6
                if rot_ptr is not None:
                    offs = struct.unpack_from("<3h", buf, rot_ptr)
                    ang = [0.0, 0.0, 0.0]
                    for j in range(3):
                        if offs[j]:
                            ang[j] = self._rle(buf, rot_ptr + offs[j], fr, B["rs"][j])
                        if not delta:
                            ang[j] += B["rot"][j]
                    quat[bone] = _angle_quat(*ang)
                if flags & 0x1:        # RAWPOS: Vector48
                    pos[bone] = np.frombuffer(buf, "<f2", 3, q0).astype(np.float64)
                elif pos_ptr is not None:
                    offs = struct.unpack_from("<3h", buf, pos_ptr)
                    for j in range(3):
                        v = self._rle(buf, pos_ptr + offs[j], fr, B["ps"][j]) if offs[j] else 0.0
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


def _is_arms_mdl(nm):
    """True for a separate arm / hand / glove / sleeve view-model in any Source pack.

    Matches common naming from CS:GO, CS:S, L4D2, GMod, etc.:
      v_arms_zoey.mdl, t_arms.mdl, c_arms_*.mdl, v_glove_*.mdl, arms.mdl, hands.mdl
      models/weapons/arms/..., models/.../hands/...
    Pure gun view-models (v_ak47, v_knife_..., w_*, ...) are rejected.
    """
    rel = nm.replace("\\", "/").lower()
    b = rel.rsplit("/", 1)[-1]
    if not b.endswith(".mdl"):
        return False
    stem = b[:-4]
    # world / dropped / display models are never equippable arms
    if re.match(r"^[wp]_", stem) or re.search(r"(^|_)(dropped|display|world|ragdoll|anim)(_|$)", stem):
        return False
    # path folders that only hold arm / hand meshes
    if re.search(r"/(arms?|hands?|sleeves?|gloves?)/", rel):
        return True
    # filename keywords
    if re.search(r"(^|_)(arms?|hands?|sleeves?|gloves?|fingers?)(_|$)", stem):
        return True
    if re.search(r"(arms?|hands?|sleeves?|gloves?)", stem):
        return True
    # pure weapon view-models (v_ak47, v_m4a1, ...) – not arms
    if re.match(r"^v_", stem):
        return False
    return False


def _mdl_looks_like_arms(buf):
    """Fallback: Source MDL with mostly arm/hand bones and no full body → treat as arms pack."""
    try:
        if len(buf) < 220 or buf[:4] != b"IDST":
            return False
        ver = struct.unpack_from("<i", buf, 4)[0]
        if not 44 <= ver <= 49:
            return False
        I = lambda o: struct.unpack_from("<i", buf, o)[0]
        nb, bo = I(156), I(160)
        if nb <= 0 or nb > 256 or bo <= 0 or bo + nb * 216 > len(buf):
            return False
        arm = body = 0
        for i in range(nb):
            p = bo + i * 216
            no = I(p)
            if no <= 0 or p + no >= len(buf):
                continue
            end = buf.find(b"\0", p + no)
            if end < 0:
                end = min(len(buf), p + no + 64)
            name = buf[p + no:end].decode("latin1", "replace").lower()
            if re.search(r"hand|finger|forearm|upperarm|wrist|ulna|clavicle|shoulder|thumb", name):
                arm += 1
            if re.search(r"pelvis|thigh|calf|foot|toe|head|neck|spine|hip", name):
                body += 1
        return arm >= 8 and body <= 2
    except Exception:
        return False


def src_arms(arch):
    """First arm / hand model inside a Source archive (legacy single-pick helper)."""
    all_ = src_arms_all(arch)
    return all_[0] if all_ else None


def src_arms_all(arch):
    """Every arm / hand / glove model inside any Source archive (.vpk / folder / zip)."""
    found = [nm for nm in arch.files if _is_arms_mdl(nm)]
    if found:
        return sorted(found)
    # Ambiguous filenames: peek at MDL bone layout for leftover .mdl files
    extra = []
    for nm in arch.files:
        low = nm.replace("\\", "/").lower()
        if not low.endswith(".mdl"):
            continue
        b = low.rsplit("/", 1)[-1]
        if re.match(r"^[wp]_", b) or re.search(r"player|ragdoll|dropped|display", low):
            continue
        try:
            sz = arch.size(nm) if hasattr(arch, "size") else 65536
            buf = arch.read(nm, 0, min(65536, sz))
        except Exception:
            continue
        if _mdl_looks_like_arms(buf):
            extra.append(nm)
    return sorted(set(found) | set(extra))


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

    @staticmethod
    def _solid_tex(rgb):
        """1x1 solid colour GL texture (used when a Source pack ships mesh only, no .vtf)."""
        key = ("solid", tuple(round(c, 4) for c in rgb))
        if key not in SrcData._tcache:
            r, g, b = [max(0, min(255, int(c * 255))) for c in rgb]
            rgba = np.full((4, 4, 4), (r, g, b, 255), np.uint8)
            SrcData._tcache[key] = make_texture(rgba, 4, 4, mip=False)
        return SrcData._tcache[key]

    def _material(self, mat):
        arch = self.arch
        info = {"t": None, "alphatest": False, "blend": False, "add": False, "tint": None}
        nm0 = (self.model.tex_names[self.model.skin[mat]] if 0 <= mat < len(self.model.skin)
               and self.model.skin[mat] < len(self.model.tex_names) else "")
        # Guess a readable fill from the material / path name (L4D2 arms packs often have no .vtf)
        is_arms_path = any(re.search(r"/(arms|hands|sleeves|gloves)/", c, re.I) for c in (self.model.cd or []))
        is_arms_name = bool(re.search(r"arms|hands|sleeve|glove|finger|palm", nm0, re.I))
        if re.search(r"hand|finger|palm|skin", nm0, re.I):
            info["fill"] = (.82, .64, .52)           # skin
        elif re.search(r"sleeve|arm|glove|shirt|jacket|anarchist|bill|zoey|louis|francis|coach|gambler|mechanic|producer", nm0, re.I):
            info["fill"] = (.35, .38, .42)           # fabric / sleeve (lighter so it shows on dark fog)
        elif is_arms_path or is_arms_name:
            info["fill"] = (.55, .48, .40)           # generic arms mesh: warm mid-tone
        else:
            info["fill"] = (.55, .55, .58)
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
            # $color / $color2 tint when present (helps when texture is missing)
            for ck in ("$color2", "$color"):
                if ck in root:
                    try:
                        cols = [float(x) for x in re.findall(r"-?[0-9.]+", str(root[ck]))[:3]]
                        if len(cols) == 3:
                            # Source colours are often 0-1, sometimes 0-255
                            if max(cols) > 1.5:
                                cols = [c / 255.0 for c in cols]
                            info["fill"] = tuple(max(0.0, min(1.0, c)) for c in cols)
                    except Exception:
                        pass
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
        # No .vtf in the pack (common for L4D2 arms-only VPKs): bake a solid texture from the fill
        # so the mesh is visible in both the game viewmodel and the Loadout preview.
        if info.get("fill") and not info["t"]:
            info["t"] = SrcData._solid_tex(info["fill"])
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
        _mv = r"run|sprint|walk|jog|move|strafe"
        if kind == "idle":
            return (pick(r"^idle$", r"^idle(?!_active)", r"idle", bad=_mv)
                    or pick(r".", bad=_mv + r"|draw|deploy|pullout|raise|ready|fire|shoot|attack|reload|slash|stab|swing|inspect|look")[:1]
                    or pick(r".", bad=_mv)[:1] or pick(r".")[:1])
        if kind == "run":
            return pick(r"^sprint", r"^run", r"^walk", r"run|sprint|walk|move", bad=r"idle|draw|fire|shoot|reload|stab|slash")[:1]
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
            try:
                self._build_arms(W)
            except Exception as ex:
                print("Arms pose failed, hiding arms:", ex)
                self.a, self.avtx = None, None

    def _build_arms(self, W):
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
        try:
            self._draw_set(self.d, self.vtx)
        finally:
            gl.glDisable(gl.GL_ALPHA_TEST)
        if self.a is not None and self.avtx is not None:
            try:
                self._draw_set(self.a, self.avtx)
            except Exception as ex:
                print("Arms draw failed, hiding arms:", ex)
                self.a, self.avtx = None, None
                gl.glDisable(gl.GL_ALPHA_TEST)
                gl.glDisable(gl.GL_BLEND)
                gl.glDepthMask(gl.GL_TRUE)
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
        # Real sequence list from the MDL (used when the pack ships multi-frame clips)
        self.seqs = list(getattr(m, "seqs", []) or [])
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
            # ValveBiped.Bip01_Head1 / bip01_head / ValveBiped.Head — normalise to short role name
            raw = b["name"].lower()
            k = re.sub(r"^.*bip01_", "", raw)
            k = re.sub(r"^valvebiped\.", "", k)
            ix.setdefault(k, i)
            # aliases used by some L4D2 / GMod packs
            if k in ("head", "head1", "head2"):
                ix.setdefault("head1", i)
            if k in ("neck", "neck1"):
                ix.setdefault("neck1", i)
        self.ix = ix
        # L4D2 hunters/survivors use spine / spine1 / spine2 / spine4 (no spine3)
        self.spine = [ix[k] for k in ("spine", "spine1", "spine2", "spine3", "spine4") if k in ix]
        need = ("pelvis", "l_thigh", "r_thigh", "l_calf", "r_calf", "l_foot", "r_foot",
                "l_upperarm", "r_upperarm", "l_forearm", "r_forearm", "l_hand", "r_hand")
        has_head = "head1" in ix or "head" in ix
        self.can = bool(self.spine) and has_head and all(k in ix for k in need)
        if "head1" not in ix and "head" in ix:
            ix["head1"] = ix["head"]
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
        self.ph = 0.0
        self._snap = None
        self._simple_legs = [(0.0, 0.03, 0.0), (0.0, 0.03, 0.0)]
        self._simple_lean = 0.0
        self.leg = 36.0
        self._seg = (17.0, 17.0)
        if self.can:
            t_ = float(np.linalg.norm(Wp[ix["l_calf"]] - Wp[ix["l_thigh"]]))
            c_ = float(np.linalg.norm(Wp[ix["l_foot"]] - Wp[ix["l_calf"]]))
            if t_ > 1.0 and c_ > 1.0:
                self._seg = (t_, c_)
                self.leg = t_ + c_
        self.vtx = None
        self._key = None
        self.rest_min = float(m.v_pos[:, 2].min())
        self._skin_rest()
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

    # ---- simple, smooth procedural player animation: idle / walk / run / crouch / slide / jump / shoot ----
    @staticmethod
    def _sm(cur, tgt, rate, dt):
        """Frame-rate independent smoothing toward a target."""
        return cur + (tgt - cur) * (1.0 - math.exp(-rate * dt))

    def _leg(self, a, k, p=None):
        """(hip, knee, ankle) for one leg; the ankle keeps the foot about flat unless p is given."""
        return (a, k, -0.12 * (a - k) if p is None else p)

    def animate(self, dt, state, spd=0.0, pitch=0.0, kick=0.0, vz=0.0, melee=False, reload=None):
        """state: idle | run | crouch | slide | jump.  spd = ground speed, vz = vertical speed,
        kick = 0..1 shot pulse (recoil for guns, slash for a knife), reload = None or 0..1 progress.
        Every pose weight is smoothed, so walking into a crouch, a jump or a slide never pops."""
        if not self.can:
            self._skin_rest()
            return
        dt = max(0.0, min(float(dt), 0.1))
        S = getattr(self, "_an", None)
        if S is None:
            S = self._an = dict(t=0.0, ph=0.0, g=0.0, run=0.0, cr=0.0, air=0.0, sl=0.0, up=0.0, land=0.0,
                                k=0.0, fz=0.0)
        S["t"] += dt
        # ---- target weights from the game state ----
        in_air = state == "jump"
        g_t = _sstep(6.0, 90.0, spd) if not in_air else 0.0           # walking gait strength
        run_t = _sstep(110.0, 230.0, spd)                              # walk -> run (longer, bigger strides)
        cr_t = 1.0 if state == "crouch" else 0.0
        sl_t = 1.0 if state == "slide" else 0.0
        was_air = S["air"]
        S["g"] = self._sm(S["g"], g_t, 11.0, dt)
        S["run"] = self._sm(S["run"], run_t, 7.0, dt)
        S["cr"] = self._sm(S["cr"], cr_t, 12.0, dt)
        S["sl"] = self._sm(S["sl"], sl_t, 16.0, dt)
        S["air"] = self._sm(S["air"], 1.0 if in_air else 0.0, 16.0 if in_air else 20.0, dt)
        S["up"] = self._sm(S["up"], max(-1.0, min(1.0, vz / 260.0)), 10.0, dt)   # +1 rising .. -1 falling
        if was_air > 0.5 and S["air"] <= 0.5 and not in_air:
            S["land"] = 1.0                                            # touched down: knees absorb it
        S["land"] = max(0.0, S["land"] - dt * 5.0)
        # shot pulse: snaps in, eases out
        S["k"] = self._sm(S["k"], kick, 45.0 if kick > S["k"] else 14.0, dt)
        g, run, cr, air, sl, up = S["g"], S["run"], S["cr"], S["air"], S["sl"], S["up"]
        land = S["land"]
        # ---- gait phase: stride length grows with the leg, so the feet match the ground speed ----
        stride = max(30.0, self.leg * (2.3 + 0.5 * run) * (1.0 - 0.35 * cr))
        S["ph"] = (S["ph"] + dt * min(3.8, spd / stride)) % 1.0
        p = S["ph"] * 2.0 * math.pi
        ground = max(0.0, 1.0 - air) * (1.0 - sl)
        cw = min(1.0, cr + 0.5 * land)                                 # crouch weight (landing dips a bit)
        legs = []
        for side in (0, 1):
            th = p + side * math.pi
            amp = (0.26 + 0.40 * run) * g * (1.0 - 0.5 * cw) * ground
            a = amp * math.sin(th)
            k = (0.06 + g * (0.10 + (0.50 + 0.85 * run) * max(0.0, math.cos(th - 0.4)) ** 1.4)
                 * (1.0 - 0.35 * cw)) * ground
            # standing -> crouching: thighs forward, knees folded (feet stay under the hips via squat)
            a += 0.95 * cw
            k += 1.70 * cw * (1.0 - 0.15 * g)
            ground_leg = self._leg(a, k)
            # in the air: rising = knees tucked (lead leg forward when running), falling = legs reach down
            rj = _sstep(40.0, 160.0, spd)
            if side == 0:
                rise = self._mix((0.60, 1.10), (0.95, 1.55), rj)
                fall = (0.30, 0.35)
            else:
                rise = self._mix((0.45, 0.90), (-0.15, 0.95), rj)
                fall = (-0.05, 0.25)
            tuck = self._mix(fall, rise, _sstep(-0.3, 0.4, up))
            air_leg = self._leg(tuck[0], tuck[1])
            leg = self._mix(ground_leg, air_leg, air)
            # sliding: lead leg straight out, the other knee folded under
            sl_leg = (1.30, 0.05, -0.25) if side == 0 else (0.55, 2.0, 0.2)
            legs.append(self._mix(leg, sl_leg, sl))
        # ---- upper body ----
        breath = 0.012 * math.sin(S["t"] * 1.7) * (1.0 - g)
        lean = breath + 0.05 * g + 0.13 * run * g + 0.30 * cw + 0.08 * air * (1.0 - sl) - 0.5 * sl
        lean -= 0.07 * S["k"]                                          # the shot rocks the torso back
        sway = g * ground
        twist = 0.06 * run * sway * math.sin(p)
        hip_yaw = -0.09 * (0.4 + 0.6 * run) * sway * math.sin(p)
        arm_drop = 0.5 * run * g * ground * (0.5 + 0.5 * math.cos(2.0 * p)) + 0.3 * sl
        squat = max(cw, 0.6 * sl)
        rl = None if reload is None else max(0.0, min(1.0, float(reload)))
        # ---- skip the solve if nothing visible changed ----
        flat = [x for lg in legs for x in lg]
        key = tuple(round(x, 3) for x in flat + [lean, twist, hip_yaw, pitch, S["k"], air, arm_drop, squat,
                                                  -1.0 if rl is None else rl]) + (bool(melee),)
        self._snap = (legs, lean)
        if key == self._key:
            return
        self._key = key
        self._solve(legs, lean, twist, pitch, S["k"], rl, melee, ground=True, hip_yaw=hip_yaw,
                    air_arms=air * (1.0 - sl), arm_drop=arm_drop, squat=squat)

    # ---- core solver ----
    def _solve(self, legs, lean, twist, pitch, kick, reload, melee, ground=True, root=None, extra=None,
               hip_yaw=0.0, air_arms=0.0, arm_drop=0.0, squat=0.0, ik=None, arm_local=None):
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
                    T = Tl if left else Tr
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


def make_vm(buf, pack=None, arms=None, css_hands=False):
    """View-model from a Source Spec (CS:GO / L4D2 only).
    arms: optional SrcSpec of an arms model, bone-merged onto the weapon skeleton.
    css_hands is ignored (CS:S fallback hands were removed)."""
    if not isinstance(buf, SrcSpec):
        raise ValueError("Only Source (CS:GO / L4D2) view-models are supported — not CS 1.6 / GoldSrc")
    a = None
    if arms is not None:
        try:
            a = arms.load()
        except Exception as ex:
            print("Arms model failed:", ex)
    data = buf.load()
    if data.has_hands:
        a = None  # weapon already has hands built in
    if a is not None:
        try:
            return SrcInst(data, a)
        except Exception as ex:
            print("Arms do not fit this weapon, using it without arms:", ex)
    return SrcInst(data, None)


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
    """True for a full-body Source character model usable as CT/T.

    Paths recognised:
      models/player/...                 (CS:S / CS:GO)
      models/survivors/...              (L4D / L4D2 survivors)
      models/infected/...               (L4D / L4D2 specials & commons)
      models/humans/...                 (HL2-style NPCs sometimes used as players)
    Skips arms/hands, view-models, world models, ragdoll helpers, and anim-only stubs.
    """
    rel = rel.replace("\\", "/").lower()
    nm = rel.rsplit("/", 1)[-1]
    if not nm.endswith(".mdl"):
        return False
    # view / world / arms models are never third-person players
    if re.match(r"^[vwp]_", nm) or re.search(r"(^|_)(arms?|hands?|sleeves?|gloves?|ragdoll|_anim|physics|gib)(_|\.|$)", nm):
        return False
    if re.search(r"/(arms?|hands?|sleeves?|gloves?|weapons?|v_models|w_models)/", rel):
        return False
    # Known character folders
    if re.search(r"(^|/)(player|survivors?|infected|humans?|npc|characters?)/", rel):
        return True
    # Filename hints used by many L4D2 workshop packs
    if re.search(r"(survivor|infected|hunter|smoker|boomer|tank|spitter|jockey|charger|witch|common_)", nm):
        return True
    return False


def src_players(arch):
    """Character models (CS player, L4D2 survivor/infected, ...) inside a Source archive."""
    out = []
    for nm in arch.files:
        if not is_src_player(nm):
            continue
        # Skip truncated VPK entries
        if hasattr(arch, "complete") and not arch.complete(nm):
            continue
        low = nm.replace("\\", "/").lower()
        base = low[:-4]
        if hasattr(arch, "has"):
            has_vvd = arch.has(base + ".vvd")
            has_vtx = any(arch.has(base + e) for e in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx"))
            if not (has_vvd and has_vtx):
                continue
            if hasattr(arch, "complete"):
                if not arch.complete(base + ".vvd"):
                    continue
                if not any(arch.complete(base + e) for e in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx") if arch.has(base + e)):
                    continue
        out.append(nm)
    return sorted(out)


def src_scan(arch):
    """Weapon view-models inside a Source archive (CS:GO, CS:S, L4D2, GMod, ...).

    Recognises:
      models/weapons/v_*.mdl          (CS:GO / CS:S)
      models/v_models/v_*.mdl         (L4D / L4D2)
      models/v_models/weapons/v_*.mdl
      any other path ending in v_*.mdl that is not arms/hands/world/display
    World models (w_*) and incomplete VPK entries are skipped.
    """
    out = []
    for nm in arch.files:
        low = nm.replace("\\", "/").lower()
        if not low.endswith(".mdl"):
            continue
        b = low.rsplit("/", 1)[-1]
        # L4D2 world models live under w_models/ — never use them as viewmodels
        if "/w_models/" in low or re.match(r"^w_", b) or re.match(r"^p_", b):
            continue
        if re.search(r"display|dropped|arms|sleeve|glove|hands|_zoom|_scope|ragdoll", low):
            continue
        # view-model naming: v_*.mdl (any folder)
        if not re.match(r"^v_[^/]*\.mdl$", b):
            continue
        # Skip truncated downloads so Loadout only lists usable models
        if hasattr(arch, "complete") and not arch.complete(nm):
            continue
        # Prefer entries that still have their .vvd / .vtx on disk
        base = low[:-4]
        has_mesh = True
        if hasattr(arch, "has"):
            has_vvd = arch.has(base + ".vvd")
            has_vtx = any(arch.has(base + e) for e in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx"))
            has_mesh = has_vvd and has_vtx
            if has_mesh and hasattr(arch, "complete"):
                if not arch.complete(base + ".vvd"):
                    has_mesh = False
                if has_mesh and not any(arch.complete(base + e) for e in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx") if arch.has(base + e)):
                    has_mesh = False
        if not has_mesh:
            continue
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
                            "sc": self.score_cb(), "frags": CFG["frags"], "day": int(FX.day)})
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
                FX.day = bool(m.get("day", 0))       # the host picks night / day for the map
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
        self.manual_mods = True   # loaded mods are only listed in Loadout; nothing is equipped until you pick it
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
        self.aspect = 0.0  # 0 = match the window; else forced aspect (stretched to fill); 1.0 = 1:1
        self.fullscreen = False
        self.show_fps = False
        self.fps_val = 0.0
        self.vm_adj = [0.0, 0.0, 0.0]  # viewmodel x (forward), y (right), z (up)
        self.custom_shot = self.custom_hit = None
        self.custom_shot_path = self.custom_hit_path = ""
        self.sens = BASE_SENS
        self.lock = False
        self.flash = 0.0
        self.shake = 0.0         # screen shake strength 0..1 (view only, does not change where bullets go)
        self.fov = 75.0          # world camera FOV right now (slider, 20 when scoped)
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
            name = self._bot_names.pop() if self._bot_names else ("Yellow" if team else "Black") + "-" + str(i)
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
                if not isinstance(spec, SrcSpec):
                    print("Player model skipped (only Source models supported):", name)
                    return
                pm = SrcPlayerInst(spec.load())
                pm.last = None
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
        if len(s) > 3:
            e.yaw = math.radians(s[3])     # spawn point facing from the map builder
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
            ms = MAXS
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
            e.zoom = 0   # scope out when a reload starts
            return
        if not w.melee:
            e.ammo[e.w] -= 1
        e.cool = 60 / w.rpm
        if e.isP and not w.melee:
            _n = str(w.n or "").lower()
            _snip = ("awp" in _n or "scout" in _n or (not w.auto and 0 < (w.mag or 0) <= 10))
            self.shake = min(1.0, self.shake + (0.9 if _snip else 0.28))
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
            hh = HIT_HALF_H_LOW if sl else HIT_HALF_H
            h = [HIT_HALF_W, HIT_HALF_W, hh]
            p[2] -= (21 if sl else 35) - hh   # keep the feet where they were
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
    def _vm_moving(self):
        P = self.P
        # moving = any real motion: running, sliding, or in the air (jumping / falling / air-strafing)
        return bool(P.alive and (math.hypot(P.vx, P.vy) > 40 or (not P.walk and abs(P.vz) > 40)))

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
        self.vmA["loop"] = kind in ("idle", "run")
        self.vmA["kind"] = kind
        # standing still: hold the first frame of the idle sequence instead of playing it
        self.vmA["hold"] = bool(kind == "idle" and not VM_IDLE_ANIM and not self._vm_moving())
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
        moving = self._vm_moving()
        if A["loop"] and A.get("kind") in ("idle", "run"):
            want = "run" if (moving and vm.find("run")) else "idle"
            if A.get("kind") != want:
                self.vm_play(vm, want)
        if not (0 <= A["s"] < len(vm.seqs)):
            return
        s = vm.seqs[A["s"]]
        still_idle = bool(A["loop"] and A.get("kind") == "idle" and not VM_IDLE_ANIM and not moving)
        if A["loop"] and A.get("kind") == "idle" and not VM_IDLE_ANIM and moving:
            A["hold"] = False            # moving again: let the animation play
        if still_idle and A.get("hold"):
            A["t"] = 0.0
            vm.pose(A["s"], 0.0, loop=True)   # held still pose
            return
        if still_idle:
            # just stopped: blend smoothly into the still pose (no fast playback)
            vm.begin_fade(0.3)
            A["hold"] = True
            A["t"] = 0.0
            vm.pose(A["s"], 0.0, loop=True)
            return
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
        """Simple state animation: death, shoot, slide, crouch, run, idle."""
        pm = e.pm
        if not pm:
            return

        def align_model_to_ground():
            if not self.airborne(e):
                z = pm.min_z()
                if math.isfinite(z):
                    e.pm_dz = -(self.world.feet_offset + z)

        A = e.anim
        if "state" not in A:
            A.update({k: v for k, v in fresh_anim().items() if k not in A})
        d = pm.d
        is_src = getattr(pm, "is_src_player", False)
        if not is_src:
            pm.tick(dt)
        wpn = CFG["weapons"][e.w] if 0 <= e.w < len(CFG["weapons"]) else None
        spd = math.hypot(e.vx, e.vy)
        in_air = self.airborne(e)
        crouch = e.cr > 0.5 or e.sl > 0
        A["gyaw"] = math.degrees(e.yaw)
        A["twist"] = 0.0

        # Death is one clip, played once and held on its last frame.
        if not e.alive:
            if is_src:
                if not A["dead"]:
                    A["dead"] = True
                    A["dt"] = 0.0
                    A["ddir"] = random.choice((-1.0, 1.0))
                    # fall style follows what the player was doing: sliding / crouching, else a random stand fall
                    A["dstyle"] = 4 if e.sl > 0 else (3 if e.cr > 0.5 else random.choice((0, 0, 1, 2)))
                A["dt"] += dt
                pm.animate_dead(A["dt"], A["ddir"], A.get("dstyle", 0))
            A["dead"] = True
            align_model_to_ground()
            return
        if A["dead"]:
            A["dead"] = False
            A["state"] = ""

        # Only Source player models are supported (posed procedurally).
        if is_src:
            self.update_src_player(e, pm, A, dt, spd, in_air, crouch, wpn)
            align_model_to_ground()
        # Non-Source models: leave T-pose / last pose (CS 1.6 path removed)

    def update_src_player(self, e, pm, A, dt, spd, in_air, crouch, wpn):
        """Drive states for Source / L4D2 player models (sequence clips or procedural IK)."""
        if e.reloadT > 0:
            e.reloadT = max(0.0, e.reloadT - dt)
        melee = bool(wpn and wpn.melee)
        kick = max(0.0, min(1.0, e.shootT / 0.15)) if e.shootT > 0 else 0.0     # shot pulse (gun recoil / knife slash)
        e.shootT = max(0.0, e.shootT - dt)
        if e.reloadT > 0:
            A["rl_max"] = max(A.get("rl_max", 0.0), e.reloadT)
            reload = 1.0 - e.reloadT / max(A["rl_max"], 1e-3)
        else:
            A["rl_max"] = 0.0
            reload = None
        if e.sl > 0:
            state = "slide"
        elif crouch:
            state = "crouch"
        elif in_air:
            state = "jump"
        elif spd > 8:  # low threshold so bots/players start the run cycle quickly
            state = "run"
        else:
            state = "idle"
        A["state"] = state
        pm.animate(dt, state, spd, e.pitch, kick, float(e.vz), melee, reload)

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
            if n not in asset["mdl"] and not self.manual_mods:
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
            if n not in asset["wav"] and not self.manual_mods:
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
        """Loaded weapon view-models (v_*.mdl style), for the Loadout tab. Arms/hands packs are listed under Arms."""
        return sorted(n for n in self.asset["mdl"]
                      if not re.match(r"^[pw]_", n) and not _is_arms_mdl(n) and n not in self.arms_all)

    def load_src_vpk(self, path):
        """Any Source .vpk (CS:GO, CS:S, L4D2, GMod): view-models, players, arms/hands.
        L4D2 packs use models/v_models/v_*.mdl; CS packs use models/weapons/v_*.mdl.
        Only models, textures and animations are used - weapon stats stay the game's own.
        Incomplete downloads (.vpk.part) are accepted for whatever entries are fully present."""
        path = Path(path)
        arch = VpkArchive(path)
        names = src_scan(arch)
        pls = src_players(arch)
        arms = src_arms_all(arch)
        if not names and not pls and not arms:
            hint = ""
            if getattr(arch, "incomplete", False):
                hint = " VPK looks truncated (re-download the full file)."
            elif any(n.endswith(".mdl") for n in arch.files):
                hint = " Found .mdl files but none are usable view-models (need matching .vvd/.vtx)."
            return f"{path.name}: no weapon, player or arms/hands model inside.{hint}"
        tag = path.stem[:18]
        # strip browser temp suffixes from the display tag
        tag = re.sub(r"\.(part|crdownload|tmp)$", "", tag, flags=re.I)[:18]
        for nm in names:
            key = nm.rsplit("/", 1)[-1]
            old = self.asset["mdl"].get(key)
            if old is not None and not (isinstance(old, SrcSpec) and old.arch is arch and old.mdl == nm):
                key = re.sub(r"\.mdl$", "", key) + " (" + tag + ").mdl"
            self.asset["mdl"][key] = SrcSpec(arch, nm)
        for nm in pls:
            self.add_src_player(nm.rsplit("/", 1)[-1], SrcSpec(arch, nm), tag)
        self._new_pl += len(pls)
        for nm in arms:
            # only register arms whose mesh is complete
            if hasattr(arch, "complete") and not arch.complete(nm):
                continue
            self.add_arms(nm.rsplit("/", 1)[-1], SrcSpec(arch, nm), tag)
        parts = []
        if names:
            l4d = sum(1 for n in names if "/v_models/" in n.replace("\\", "/").lower())
            if l4d == len(names):
                parts.append(f"{len(names)} L4D2 view-model(s)")
            elif l4d:
                parts.append(f"{len(names)} view-model(s) ({l4d} L4D2)")
            else:
                parts.append(f"{len(names)} view-model(s)")
        if pls:
            n_l4d = sum(1 for n in pls if re.search(r"/(infected|survivors?)/", n.replace("\\", "/").lower()))
            if n_l4d == len(pls):
                parts.append(f"{len(pls)} L4D2 player model(s)")
            elif n_l4d:
                parts.append(f"{len(pls)} player model(s) ({n_l4d} L4D2)")
            else:
                parts.append(f"{len(pls)} player model(s)")
        n_arms = sum(1 for n in arms if not hasattr(arch, "complete") or arch.complete(n))
        if n_arms:
            parts.append(f"{n_arms} arms/hands model(s)")
        msg = f"{path.name}: {' + '.join(parts)} - pick them in Loadout."
        if getattr(arch, "incomplete", False):
            msg += " (package incomplete — re-download the full .vpk for missing models/textures)"
        return msg

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

    def add_arms(self, name, spec, tag=""):
        """Register an arms/hands/gloves model. name is the file basename; tag disambiguates packs."""
        key = name
        old = self.arms_all.get(key)
        if old is not None and not (isinstance(old, SrcSpec) and old.arch is spec.arch and old.mdl == spec.mdl):
            key = re.sub(r"\.mdl$", "", name) + " (" + (tag or "mod")[:14] + ").mdl"
        self.arms_all[key] = spec
        if self.arms_off:
            return key
        if (self.arms_want and key == self.arms_want) or (self.arms_want and name == self.arms_want) \
                or (not self.arms_want and self.arms_pick is None and not self.manual_mods):
            self.arms_pick, self.src_arms = key, spec
        return key

    def load_arms_folder(self):
        """Scan arms/ folders and load every arms/hands pack found.

        Looks in <script_dir>/mod/arms/ (or <cwd>/mod/arms/). Never creates folders.
        Accepts .vpk, .vpk.part, .zip, .7z, .rar, and loose .mdl (+ companions).
        """
        uniq_roots = mod_folders("arms")
        if not uniq_roots:
            return "No mod/arms folder found."

        exts = (".vpk", ".zip", ".7z", ".rar", ".mdl")
        files = []
        for root in uniq_roots:
            for f in sorted(root.rglob("*")):
                if not f.is_file():
                    continue
                n = f.name.lower()
                if f.suffix.lower() in exts or n.endswith(".vpk.part"):
                    files.append(f)

        if not files:
            return "mod/arms is empty — drop .vpk / .mdl packs into it."

        before = set(self.arms_all.keys())
        # Load each file; VPKs/archives go through normal Source loader
        status_parts = []
        for f in files:
            try:
                st = self.load_files([f], "weapon")
                if st:
                    status_parts.append(st)
            except Exception as ex:
                status_parts.append(f"{f.name}: {ex}")

        # Second pass: any VPK in arms/ that registered only as a weapon but looks like hands
        for f in files:
            n = f.name.lower()
            if not (n.endswith(".vpk") or n.endswith(".vpk.part")):
                continue
            try:
                arch = VpkArchive(f)
            except Exception:
                continue
            for nm in src_arms_all(arch):
                if hasattr(arch, "complete") and not arch.complete(nm):
                    continue
                key = nm.rsplit("/", 1)[-1]
                if key not in self.arms_all:
                    self.add_arms(key, SrcSpec(arch, nm), f.stem[:18])
            # Aggressive: in arms/ folder, any v_*.mdl with hand/arm bones counts as arms
            for nm in arch.files:
                low = nm.replace("\\", "/").lower()
                if not low.endswith(".mdl"):
                    continue
                if _is_arms_mdl(nm):
                    continue  # already handled
                b = low.rsplit("/", 1)[-1]
                if re.match(r"^w_", b) or "/w_models/" in low:
                    continue
                try:
                    buf = arch.read(nm, 0, min(65536, arch.size(nm)))
                except Exception:
                    continue
                if _mdl_looks_like_arms(buf) or re.search(r"arms?|hands?|glove|sleeve", b):
                    if hasattr(arch, "complete") and not arch.complete(nm):
                        continue
                    self.add_arms(b, SrcSpec(arch, nm), f.stem[:18])

        after = set(self.arms_all.keys())
        added = sorted(after - before)

        # Auto-equip default if nothing selected
        if self.arms_all and self.arms_pick is None and not self.arms_off and not self.manual_mods:
            want = self.arms_want
            if want and want in self.arms_all:
                self.set_arms(want)
            else:
                prefer = ("v_arms_zoey.mdl", "v_arms_bill.mdl", "v_arms_francis.mdl",
                          "v_arms_louis.mdl", "v_arms_coach_new.mdl", "v_arms_gambler_new.mdl",
                          "v_arms_mechanic_new.mdl", "v_arms_producer_new.mdl",
                          "t_arms.mdl", "c_arms.mdl", "v_arms.mdl")
                pick = next((n for n in prefer if n in self.arms_all), None)
                if pick is None:
                    pick = sorted(self.arms_all.keys())[0]
                self.set_arms(pick)
        elif self.arms_want and self.arms_want in self.arms_all and self.arms_pick != self.arms_want:
            self.set_arms(self.arms_want)

        roots_s = ", ".join(str(r) for r in uniq_roots)
        if added:
            msg = f"Arms folder ({roots_s}): {len(added)} model(s) — {', '.join(added[:6])}{'…' if len(added) > 6 else ''}."
        else:
            msg = f"Arms folder ({roots_s}): scanned {len(files)} file(s), {len(self.arms_all)} arms total."
        if self.arms_pick:
            msg += f" Equipped: {self.arms_pick}."
        return msg

    def set_arms(self, name):
        """Equip an arms model (name from arms_all) or None = no arms."""
        self.arms_pick = name if name in self.arms_all else None
        self.arms_want = self.arms_pick
        self.arms_off = self.arms_pick is None
        self.src_arms = self.arms_all.get(self.arms_pick)
        try:
            self.bind_assets()
        except Exception as ex:
            print("set_arms: rebuilding weapons failed:", ex)
            self.src_arms = None
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

    def load_map(self, path):
        """Load a .fadmap made with the FAD-MOD Map Builder. Returns a status string."""
        path = Path(path)
        if self.net.active:
            return "Leave the online room before loading a map."
        try:
            raw = path.read_bytes()
            world = FadMapWorld(json.loads(raw.decode("utf-8")))
        except Exception as ex:
            return f"Cannot load map {path.name}: {ex}"
        self.files["bsp"] = raw          # used for the online map signature
        self.set_world(world)
        return f"Map loaded: {world.name} ({len(world.B)} boxes)."

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
            if n.endswith(".fadmap"):
                status = self.load_map(p)
                continue
            # CS:GO / L4D2 / GMod weapon packs (.vpk, or browser partial downloads .vpk.part)
            if n.endswith(".vpk") or n.endswith(".vpk.part") or re.search(r"\.vpk\.(part|crdownload|tmp)$", n):
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
            if n.endswith((".vvd", ".vtx", ".vtf", ".vmt", ".ani")):
                # Companion mesh files (picked up next to their .mdl); .ani ignored for now
                continue
            try:
                data = p.read_bytes()
            except Exception as ex:
                return f"Cannot read {p.name}: {ex}"
            if n.endswith((".bsp", ".wad")):
                status = f"{p.name}: GoldSrc maps (CS 1.6 .bsp/.wad) are not supported."
                continue
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
            if n.endswith(".json"):
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
                    # Any arms/hands/gloves mesh (by name, path, or bone layout)
                    if _is_arms_mdl(rel) or _is_arms_mdl(n) or _mdl_looks_like_arms(data):
                        self.add_arms(key, spec, root.name or "mod")
                    else:
                        self.asset["mdl"][key] = spec
                    continue
                # Reject GoldSrc / CS 1.6 (MDL v10) and unknown formats
                status = f"{n}: only Source engine models (CS:GO / L4D2, MDL v44-49) are supported."
                continue
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
                    return status or f"{nsrc} Source view-model(s) loaded - pick them in Loadout."
                return (self.vm_msg or "No weapon files matched.").strip() if not status else status
        return status or ""

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
                vz_ = getattr(self.world, "void_z", None)
                if vz_ is not None and e.z < vz_:      # fell off the map
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
        target = 20.0 if zoomed else self.fov_cfg            # world: follows the FOV slider
        vm_target = 20.0 if zoomed else 75.0                 # viewmodel: fixed, ignores the slider
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
            P.zoom = 0   # reloading always scopes out
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
                e.ph += dt * spd * 0.03
            e.bob += ((min(1.0, spd / MAXS) if run else 0.0) - e.bob) * min(1.0, dt * 10)
            if not run and e.bob < 0.02:
                e.bob = 0.0
        for t in self.tracers[:]:
            t["t"] -= dt
            if t["t"] <= 0:
                self.tracers.remove(t)
        self.flash = max(0.0, self.flash - dt * 2.5)
        self.shake = max(0.0, self.shake - dt * SHAKE_DECAY * max(0.2, self.shake))
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
TEAM_CT = (0, 0, 0)        # CT = black
TEAM_T = (255, 214, 0)     # T = yellow team


def team_tint(col):
    """Solid band behind a team label: black team = yellow band (black text), yellow team = black band (yellow text)."""
    return (1.0, 214 / 255, 0.0, 1.0) if col == TEAM_CT else (0.0, 0.0, 0.0, 1.0)


RENDER_W, RENDER_H = 1280, 720    # the game always renders at this resolution, then is scaled to the window


class RenderTarget:
    """Fixed-size (MSAA) off-screen buffer. The whole game draws into it at RENDER_W x RENDER_H and
    present() scales it into the window, keeping 16:9 (black bars if the window has another shape)."""

    def __init__(self, w, h, samples=4):
        self.w, self.h = w, h
        self.ok = False
        self.view = (0, 0, w, h)      # where the image sits in the window (pixels, top-left origin)
        try:
            self.res_fbo = gl.glGenFramebuffers(1)
            self.tex = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.res_fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, self.tex, 0)
            self.msaa = False
            self.fbo = self.res_fbo
            for n in (samples, 0):
                fbo = gl.glGenFramebuffers(1)
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
                rbs = gl.glGenRenderbuffers(2)
                for rb, fmt, att in ((rbs[0], gl.GL_RGBA8, gl.GL_COLOR_ATTACHMENT0),
                                     (rbs[1], gl.GL_DEPTH24_STENCIL8, gl.GL_DEPTH_STENCIL_ATTACHMENT)):
                    gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, rb)
                    if n:
                        gl.glRenderbufferStorageMultisample(gl.GL_RENDERBUFFER, n, fmt, w, h)
                    else:
                        gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, fmt, w, h)
                    gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, att, gl.GL_RENDERBUFFER, rb)
                if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE:
                    self.fbo, self.msaa = fbo, bool(n)
                    self.ok = True
                    break
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        except Exception as ex:
            print("Render target unavailable, drawing at window size:", ex)
            self.ok = False
            try:
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            except Exception:
                pass

    def begin(self):
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, self.w, self.h)

    def present(self, ww, wh):
        if self.msaa:   # resolve the multisampled buffer into the texture
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.fbo)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.res_fbo)
            gl.glBlitFramebuffer(0, 0, self.w, self.h, 0, 0, self.w, self.h, gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        ww, wh = max(1, ww), max(1, wh)
        sc = min(ww / self.w, wh / self.h)
        vw, vh = self.w * sc, self.h * sc
        vx, vy = (ww - vw) / 2, (wh - vh) / 2
        self.view = (vx, vy, vw, vh)
        gl.glViewport(0, 0, ww, wh)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_FOG)
        gl.glUseProgram(0)
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glOrtho(0, ww, 0, wh, -1, 1)
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glLoadIdentity()
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glColor4f(1, 1, 1, 1)
        gl.glBegin(gl.GL_QUADS)
        gl.glTexCoord2f(0, 0); gl.glVertex2f(vx, vy)
        gl.glTexCoord2f(1, 0); gl.glVertex2f(vx + vw, vy)
        gl.glTexCoord2f(1, 1); gl.glVertex2f(vx + vw, vy + vh)
        gl.glTexCoord2f(0, 1); gl.glVertex2f(vx, vy + vh)
        gl.glEnd()

    def to_render(self, pos):
        """Window mouse position -> render-buffer position."""
        vx, vy, vw, vh = self.view
        return ((pos[0] - vx) * self.w / max(1.0, vw), (pos[1] - vy) * self.h / max(1.0, vh))


class Gfx:
    def __init__(self):
        self.fonts = {}
        self.cache = {}
        self.ox = self.oy = 0.0   # screen offset of the current 2D layout (pixels)
        self.sc = 1.0             # screen pixels per layout unit (menu is laid out in 1000x600 units)

    def set_xf(self, ox=0.0, oy=0.0, sc=1.0):
        self.ox, self.oy, self.sc = ox, oy, sc

    def font(self, size, bold, mono=False):
        k = (size, bold, mono)
        if k not in self.fonts:
            fam = ("consolas,lucidaconsole,couriernew,dejavusansmono,liberationmono,monospace" if mono
                   else "segoeui,arial,helvetica,dejavusans,freesans")
            self.fonts[k] = pygame.font.SysFont(fam, size, bold=bold)
        return self.fonts[k]

    def _px(self, size):
        return max(6, int(round(size * self.sc)))

    def _tex(self, s, size, color, bold, mono=False):
        px = self._px(size)
        k = (s, px, color, bold, mono)
        c = self.cache.get(k)
        if c:
            return c
        if len(self.cache) > 400:
            for tid, _, _ in self.cache.values():
                gl.glDeleteTextures([tid])
            self.cache.clear()
        surf = self.font(px, bold, mono).render(s if s else " ", True, color)
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
        self.cache[k] = (tid, w, h)   # w, h are real pixels
        return self.cache[k]

    def text_w(self, s, size, bold=False, mono=False):
        return self.font(self._px(size), bold, mono).size(s)[0] / self.sc

    def text(self, s, x, y, size=14, color=(255, 255, 255), alpha=1.0, bold=False, anchor="l", mono=False):
        tid, pw, ph = self._tex(s, size, tuple(color), bold, mono)
        sc = self.sc
        w, h = pw / sc, ph / sc             # size in layout units = exactly pw x ph screen pixels
        if anchor == "c":
            x -= w / 2
        elif anchor == "r":
            x -= w
        # snap the top-left corner to a whole screen pixel so glyphs are not smeared
        x = (round(self.ox + x * sc) - self.ox) / sc
        y = (round(self.oy + y * sc) - self.oy) / sc
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

    def rect(self, x, y, w, h, rgba):
        # snap edges to whole screen pixels (a 1-unit border is always at least 1 pixel, never blurry)
        sc, ox, oy = self.sc, self.ox, self.oy
        x0, y0 = round(ox + x * sc), round(oy + y * sc)
        x1, y1 = round(ox + (x + w) * sc), round(oy + (y + h) * sc)
        if x1 <= x0:
            x1 = x0 + 1
        if y1 <= y0:
            y1 = y0 + 1
        x0, x1 = (x0 - ox) / sc, (x1 - ox) / sc
        y0, y1 = (y0 - oy) / sc, (y1 - oy) / sc
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor4f(*rgba)
        gl.glBegin(gl.GL_QUADS)
        gl.glVertex2f(x0, y0); gl.glVertex2f(x1, y0)
        gl.glVertex2f(x1, y1); gl.glVertex2f(x0, y1)
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
        try:
            self._draw(W, H)
        finally:
            self.gfx.set_xf(0, 0, 1)

    def _draw(self, W, H):
        g, gfx = self.g, self.gfx
        s = max(.4, min((W - 24) / 1000, (H - 24) / 600, 1.5))
        ox, oy = (W - 1000 * s) / 2, (H - 600 * s) / 2
        ox, oy = round(ox), round(oy)
        self.xf = (ox, oy, s)
        gfx.set_xf(ox, oy, s)
        self.hot = []
        mx, my = self.mp

        gl.glDisable(gl.GL_DEPTH_TEST)
        gfx.rect(0, 0, W, H, c255(10, 10, 10))
        gl.glPushMatrix()
        gl.glTranslatef(ox, oy, 0)
        gl.glScalef(s, s, 1)

        gfx.rect(0, 0, 1000, 600, c255(10, 10, 10))          # content: #0a0a0a
        gfx.rect(0, 0, 240, 600, c255(22, 22, 22))           # sidebar: #161616
        # profile block: avatar with green rim + username
        gfx.rect(18, 16, 40, 40, c255(157, 211, 103))
        if self.pfp_tex:
            gl.glEnable(gl.GL_TEXTURE_2D)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.pfp_tex)
            gl.glColor4f(1, 1, 1, 1)
            gl.glBegin(gl.GL_QUADS)
            gl.glTexCoord2f(0, 0); gl.glVertex2f(21, 19)
            gl.glTexCoord2f(1, 0); gl.glVertex2f(55, 19)
            gl.glTexCoord2f(1, 1); gl.glVertex2f(55, 53)
            gl.glTexCoord2f(0, 1); gl.glVertex2f(21, 53)
            gl.glEnd()
        else:
            gfx.rect(21, 19, 34, 34, c255(30, 30, 30))
            gfx.text((self.name.strip() or "P")[0].upper(), 38, 24, 20, (150, 150, 150), bold=True, anchor="c")
        gfx.text((self.name.strip() or "Player")[:14], 70, 26, 15, (255, 255, 255), bold=True)

        def hover(x, y, w, h):
            return x <= mx <= x + w and y <= my <= y + h

        def nav(label, page, y):
            act = self.page == page
            h = hover(0, y, 240, 44)
            gfx.rect(0, y, 240, 44, c255(42, 42, 42) if act else (c255(30, 30, 30) if h else (0, 0, 0, 0)))
            gfx.text(label, 20, y + 12, 14, (255, 255, 255) if act else (154, 154, 154), bold=True)
            self.hot.append((0, y, 240, 44, lambda p=page: self.set_page(p), None))

        nav("Play", "play", 80)
        nav("Profile", "profile", 124)
        nav("Loadout", "loadout", 168)
        nav("Settings", "settings", 212)
        if g.net.active:
            gfx.text("ONLINE: " + g.net.room[:18], 20, 552, 11, (104, 104, 104), mono=True)
        gfx.text("FAD-MOD", 20, 574, 12, (154, 154, 154), bold=True)

        def button(x, y, w, h, label, fn, primary=False):
            hv = hover(x, y, w, h)
            if primary:
                fill = c255(58, 58, 58) if hv else c255(42, 42, 42)
            else:
                fill = c255(42, 42, 42) if hv else c255(24, 24, 24)
            gfx.rect(x, y, w, h, fill)
            col = (229, 57, 53) if label in ("Close room", "Leave room", "Cancel", "Remove") else (255, 255, 255)
            gfx.text(label, x + w / 2, y + h / 2 - 9, 13, col, bold=True, anchor="c")
            self.hot.append((x, y, w, h, fn, None))

        def panel(x, y, w, h, title, sub=None):
            gfx.rect(x, y, w, h, c255(17, 17, 17))
            gfx.text(title, x + 20, y + 14, 14, (255, 255, 255), bold=True)
            if sub:
                gfx.text(sub, x + 20, y + 34, 12, (130, 130, 130))

        def slider(x, y, w, val, lo, hi, setter):
            t = (val - lo) / (hi - lo)
            gfx.rect(x, y + 8, w, 4, c255(42, 42, 42))
            gfx.rect(x, y + 8, w * t, 4, c255(85, 85, 85))
            gfx.rect(x + w * t - 6, y + 4, 12, 12, c255(230, 230, 230))

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
                     cx, 96, 13, (154, 154, 154))

            def field(x, y, w, h, key, ph):
                val = getattr(self, key)
                on = self.edit == key
                gfx.box(x, y, w, h, c255(10, 10, 10), c255(170, 170, 170) if on else c255(42, 42, 42))
                shown = val if (val or on) else ph
                gfx.text(shown + ("|" if on and int(time.time() * 2) % 2 == 0 else ""), x + 12, y + 8, 14,
                         (255, 255, 255) if val else (110, 110, 110))
                self.hot.append((x, y, w, h, lambda k=key: setattr(self, "edit", k), None))

            def pname():
                return self.name.strip() or "Player"

            def toggle_day():
                FX.day = not FX.day
                save_settings(g, self)

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
                gfx.text("Players: %d / %d" % (len(net.peers) + 1, NET_MAX_PLAYERS),
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
                button(cx + 20, 226, 150, 36, "Host room >", do_host, True)
                button(cx + 180, 226, 160, 36, "Offline match >", do_offline, True)
                button(cx + 350, 226, 96, 36, "Bots: ON" if g.bots_on else "Bots: OFF", toggle_bots)
                button(cx + 456, 226, 100, 36, "Time: Day" if FX.day else "Time: Night", toggle_day)
                button(cx + 566, 226, 94, 36, "Load map", lambda: self.pick("map"))
                miss = self.missing()
                if miss:
                    gfx.text("Pick mods in Loadout first: " + ", ".join(miss), cx + 20, 266, 11, (255, 193, 7))
                ly = 290

            # --- roster / room browser ---
            if net.active:
                panel(cx, ly, cw, 578 - ly, "Players in the room")
                rows = [(g.P.name or "You", g.P.team, True)] + [(p["n"], p["tm"], False) for p in net.players.values()]
                for i, (nm, tm, me) in enumerate(rows[:NET_MAX_PLAYERS]):
                    xx = cx + 20 + (i % 2) * 330
                    yy = ly + 40 + (i // 2) * 34
                    col = TEAM_CT if tm == 0 else TEAM_T
                    gfx.box(xx, yy, 320, 28, team_tint(col), c255(42, 42, 42))
                    gfx.text(nm[:20] + ("  (you)" if me else ""), xx + 10, yy + 6, 13, (0, 0, 0) if tm == 0 else (255, 214, 0), bold=me, mono=True)
                    gfx.text("BLACK" if tm == 0 else "YELLOW", xx + 308, yy + 6, 12, col, bold=True, anchor="r")
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
                    gfx.box(cx + 20, yy, cw - 40, 30, c255(17, 17, 17), c255(24, 24, 24))
                    gfx.text(r["n"][:26], cx + 32, yy + 7, 14, (255, 255, 255), bold=True, mono=True)
                    gfx.text("by " + r["h"][:14], cx + 290, yy + 8, 12, (140, 140, 140), mono=True)
                    gfx.text("%d/%d" % (r["p"], r["m"]), cx + 430, yy + 8, 12, (200, 200, 200), mono=True)
                    if other:
                        gfx.text("other map", cx + 480, yy + 8, 11, (255, 193, 7))
                    if full:
                        gfx.text("FULL", cx + cw - 36, yy + 8, 12, (150, 150, 150), anchor="r")
                    else:
                        button(cx + cw - 110, yy + 2, 80, 26, "Join", lambda r=r: do_join(r), True)

            # --- status line (map loaded, join errors, connection progress) ---
            msg = net.status if net.mode else self.status
            bad = msg.startswith(("Denied", "Could", "Conn", "Lost", "The host", "Cannot", "Error"))
            col = (255, 193, 7) if bad else (170, 170, 170)
            if net.mode or bad:   # only show room status / errors, not the weapons-loaded text
                lines = [msg[i:i + 56] for i in range(0, min(len(msg), 168), 56)]
                tw = max(gfx.text_w(l, 12, False, True) for l in lines) + 32
                th = 16 + 17 * len(lines)
                tx, ty = 960 - tw, 580 - th
                gfx.rect(tx, ty, tw, th, c255(41, 41, 41))          # toast: #292929
                for i, l in enumerate(lines):
                    gfx.text(l, tx + 16, ty + 9 + i * 17, 12, col if bad else (255, 255, 255), mono=True)
        elif self.page == "profile":
            gfx.text("YOUR IDENTITY", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Profile.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Change your username and profile picture.", cx, 96, 13, (154, 154, 154))
            panel(cx, 128, cw, 250, "Your profile", "Your username is shown to other players in online rooms and on the scoreboard.")
            px, py = cx + 20, 176
            gfx.box(px - 2, py - 2, 164, 164, c255(10, 10, 10), c255(42, 42, 42))
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
            gfx.box(cx + 210, 196, 300, 36, c255(10, 10, 10), c255(170, 170, 170) if on else c255(42, 42, 42))
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
                gfx.text(self.pfp_status[:80], cx + 210, 336, 12, (255, 193, 7))
        elif self.page == "loadout":
            gfx.text("YOUR KIT", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Loadout.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Pick weapon mods, player models and arms. Click a card to equip it.",
                     cx, 96, 13, (154, 154, 154))
            wp = self.lo_tab == "weapons"
            ar = self.lo_tab == "arms"
            button(cx, 124, 130, 32, "Weapon mods", lambda: self.lo_set("weapons"), wp)
            button(cx + 140, 124, 130, 32, "Player models", lambda: self.lo_set("players"), self.lo_tab == "players")
            button(cx + 280, 124, 100, 32, "Arms", lambda: self.lo_set("arms"), ar)
            ws = CFG["weapons"][:3]
            if wp:
                items = g.weapon_mods()
            elif ar:
                # leading empty string = "No arms" (procedural / built-in hands)
                items = [""] + sorted(g.arms_all.keys())
            else:
                items = sorted(g.asset["player"].keys())
            per = 6
            pages = max(1, (len(items) + per - 1) // per)
            self.lo_page = max(0, min(self.lo_page, pages - 1))
            add_x = cx + cw - 130
            pick_mode = "weapon" if wp else ("arms" if ar else "player")
            button(add_x, 124, 130, 32, "Add files", lambda m=pick_mode: self.pick(m))
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
                cur = ws[self.lo_slot] if ws else None
                info = ("Slot %d (%s): " % (self.lo_slot + 1, cur.n) + (cur._m or "no model - box")
                        + ("" if self.lo_slot in g.wm_pick else "  (auto)")) if cur else ""
            elif ar:
                info = "Arms: " + (g.arms_pick or ("none (built-in hands)" if g.arms_off or not g.arms_pick else "none"))
                if g.arms_pick:
                    info = "Arms: " + g.arms_pick
                elif g.arms_off or g.arms_pick is None:
                    info = "Arms: none (built-in / procedural hands)"
            else:
                for i, tk in enumerate(("CT", "T")):
                    bx_, on_ = cx + i * 160, self.lo_team == tk
                    ct_ = tk == "CT"
                    if on_:
                        gfx.box(bx_, 166, 150, 30, team_tint(TEAM_CT if ct_ else TEAM_T), None)
                    else:
                        gfx.rect(bx_, 166, 150, 30, c255(42, 42, 42) if hover(bx_, 166, 150, 30) else c255(24, 24, 24))
                    gfx.text("Black team" if ct_ else "Yellow team", bx_ + 75, 166 + 30 / 2 - 9, 13,
                             (TEAM_CT if ct_ else TEAM_T) if on_ else (154, 154, 154), bold=True, anchor="c")
                    self.hot.append((bx_, 166, 150, 30, lambda tk=tk: setattr(self, "lo_team", tk), None))
                info = ("Black" if self.lo_team == "CT" else "Yellow") + " team: " + (g.pm_cfg[self.lo_team] or "none picked yet")
            gfx.text(info[:100], cx, 204, 12, (170, 170, 170))

            gy, cw2, ch2 = 226, 216, 170
            if not items or (ar and len(items) <= 1 and not g.arms_all):
                empty = ("No weapon mods loaded yet." if wp else
                         ("No arms models loaded yet." if ar else "No player models loaded yet."))
                gfx.text(empty, cx + cw / 2, gy + 120, 15, (200, 200, 200), bold=True, anchor="c")
                folder = "weapons" if wp else ("arms (or drop a .vpk)" if ar else "player")
                gfx.text("Use Add files, drop files on the window, or put them in the " + folder + " folder.",
                         cx + cw / 2, gy + 146, 12, (130, 130, 130), anchor="c")
            for idx, name in enumerate(items[self.lo_page * per:(self.lo_page + 1) * per]):
                r, c = divmod(idx, 3)
                x, y = cx + c * (cw2 + 16), gy + r * (ch2 + 14)
                if wp:
                    here = bool(ws) and ws[self.lo_slot]._m == name
                    inst = self.pv_weapon(name)
                    kind = "weapon"
                elif ar:
                    here = (name == "" and (g.arms_off or g.arms_pick is None)) or (name and g.arms_pick == name)
                    inst = self.pv_arms(name) if name else None
                    kind = "arms"
                else:
                    here = g.pm_cfg[self.lo_team] == name
                    inst = self.pv_player(name)
                    kind = "player"
                hv = hover(x, y, cw2, ch2)
                gfx.box(x, y, cw2, ch2, c255(17, 17, 17) if not hv else c255(24, 24, 24),
                        c255(255, 193, 7) if here else (c255(120, 120, 120) if hv else c255(42, 42, 42)))
                self.draw_card_preview(W, H, x + 2, y + 2, cw2 - 4, 116, kind, inst)
                gfx.rect(x + 2, y + 118, cw2 - 4, 1, c255(42, 42, 42))
                if ar and name == "":
                    stem = "No arms"
                else:
                    stem = re.sub(r"\.mdl$", "", name)
                    if wp or ar:
                        stem = re.sub(r"^v_", "", stem)
                gfx.text(stem if len(stem) <= 26 else stem[:25] + "~", x + 10, y + 124, 13, (255, 255, 255), bold=True)
                if wp:
                    tags = [(f"{i + 1}  {w.n}"[:12], (1, 1, 1, .14), (230, 230, 230))
                            for i, w in enumerate(ws) if w._m == name]
                elif ar:
                    tags = [("EQUIPPED", (1, 1, 1, .14), (255, 193, 7))] if here else []
                else:
                    tags = [("BLACK" if tk == "CT" else "YELLOW", team_tint(TEAM_CT) if tk == "CT" else team_tint(TEAM_T),
                             TEAM_CT if tk == "CT" else TEAM_T)
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
                     cx, 96, 13, (154, 154, 154))

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
                gfx.box(rx, ry, 320, 32, c255(17, 17, 17), c255(24, 24, 24))
                gfx.text(label, rx + 12, ry + 8, 12, (230, 230, 230))
                waiting = self.waiting == aid
                bw = 124
                hv = hover(rx + 320 - bw - 6, ry + 3, bw, 26)
                gfx.box(rx + 320 - bw - 6, ry + 3, bw, 26,
                        c255(52, 52, 52) if waiting else (c255(42, 42, 42) if hv else c255(24, 24, 24)),
                        c255(200, 200, 200) if waiting else c255(42, 42, 42))
                gfx.text("Press a key..." if waiting else key_label(g.binds[aid]), rx + 320 - bw / 2 - 6, ry + 8, 12,
                         (255, 255, 255), anchor="c")
                self.hot.append((rx + 320 - bw - 6, ry + 3, bw, 26, lambda a=aid: setattr(self, "waiting", a), None))
            button(cx + 350, 282 + 44 + 6 * 38, 150, 32, "Reset defaults", self.reset_keys)
            button(cx + 512, 282 + 44 + 6 * 38, 150, 32, "Back to play", lambda: self.set_page("play"), True)
        elif self.page == "settings" and self.set_sub == "sounds":
            gfx.text("CUSTOM SOUNDS", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Sounds.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Pick your own .wav / .ogg / .mp3 files. Volume follows the weapon volume slider in Settings.",
                     cx, 96, 13, (154, 154, 154))
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
                     cx, 96, 13, (154, 154, 154))
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
            gfx.box(rx - 2, 124, rw + 4, rh + 4, c255(17, 17, 17), c255(24, 24, 24))
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
                    gfx.box(rx, iy, rw, 28, c255(52, 52, 52) if i == self.vm_prev else (c255(42, 42, 42) if hv2 else c255(24, 24, 24)),
                            c255(85, 85, 85))
                    gfx.text(str(wp.n), rx + 12, iy + 6, 12, (238, 238, 238))
                    self.hot.append((rx, iy, rw, 28,
                                     lambda i=i: (setattr(self, "vm_prev", i), setattr(self, "vm_dd", False)), None))
        elif self.page == "settings" and self.set_sub == "crosshair":
            xh = g.xh
            gfx.text("AIM", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Crosshair.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Build your own crosshair, like in CS:GO. Changes show in the preview right away.",
                     cx, 96, 13, (154, 154, 154))
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
            gfx.box(rx - 2, 124, rw + 4, 204, c255(17, 17, 17), c255(24, 24, 24))
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
                     cx, 96, 13, (154, 154, 154))
            panel(cx, 126, cw, 150, "Aspect ratio",
                  "Stretched 4:3 makes players and weapons look wider, like the old stretched-res trick.")
            opts = ((0.0, "Window"), (16 / 9, "16:9"), (16 / 10, "16:10"), (4 / 3, "4:3"), (1.0, "1:1"))
            for i, (val, lab) in enumerate(opts):
                bx = cx + 20 + i * 128
                button(bx, 190, 120, 44, lab, lambda v=val: (setattr(g, "aspect", v), save_settings(g, self)),
                       abs(g.aspect - val) < 1e-6)
            now = "Window (default)" if not g.aspect else next((l for v, l in opts if abs(g.aspect - v) < 1e-6), "Custom")
            gfx.text("Now: " + now, cx + 20, 248, 12, (170, 170, 170))
            panel(cx, 292, cw, 92, "Field of view",
                  "Changes how much of the world you see. The viewmodel (your weapon) stays the same.")
            gfx.text(f"{int(round(g.fov_cfg))}", cx + cw - 20, 14 + 292, 13, (255, 255, 255), bold=True, anchor="r")
            slider(cx + 20, 292 + 62, cw - 190, g.fov_cfg, 50, 110, lambda v: setattr(g, "fov_cfg", float(round(v))))
            button(cx + cw - 150, 292 + 54, 130, 30, "Reset to 75", lambda: setattr(g, "fov_cfg", 75.0))
            panel(cx, 400, cw, 92, "Fullscreen", "Fills the whole screen. You can also press F11 any time.")
            button(cx + 20, 400 + 44, 200, 36, "Fullscreen: ON" if g.fullscreen else "Fullscreen: OFF",
                   lambda: (set_fullscreen(g, not g.fullscreen), save_settings(g, self)), bool(g.fullscreen))
            panel(cx, 508, cw, 92, "FPS counter", "Shows your frames per second in the top-left corner while playing.")
            button(cx + 20, 508 + 44, 200, 36, "FPS counter: ON" if g.show_fps else "FPS counter: OFF",
                   lambda: (setattr(g, "show_fps", not g.show_fps), save_settings(g, self)), bool(g.show_fps))
        gl.glPopMatrix()

    def missing(self):
        """Mods are optional: you can always play without any (box weapon / box players)."""
        return []

    def check_ready(self):
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
        elif self.lo_tab == "arms":
            # empty string = turn arms off (built-in / procedural hands)
            g.set_arms(name if name else None)
            self.status = ("Arms off (built-in hands)." if not name
                           else f"Arms: {name}")
            self.pv.clear()  # viewmodels must rebuild with the new arms
            self.pv_bb.clear()
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

    def pv_arms(self, name):
        """Standalone arms model for the Loadout Arms tab preview."""
        g = self.g
        spec = g.arms_all.get(name)
        if spec is None:
            return None
        k = ("a", name, id(spec))
        if k not in self.pv:
            try:
                data = spec.load()  # SrcData (builds solid fill textures if .vtf missing)
                inst = SrcInst(data)  # arms alone, not bone-merged onto a gun
                idle = inst.find("idle")
                inst.last = None
                inst.pose(idle[0] if idle else (0 if inst.seqs else -1), 0)
                self.pv[k] = inst
            except Exception as ex:
                print("Arms preview error:", name, ex)
                self.pv[k] = None
        return self.pv[k]

    def pv_player(self, name):
        g = self.g
        buf = g.asset["player"].get(name)
        if buf is None:
            return None
        k = ("p", name, len(buf))
        if k not in self.pv:
            if isinstance(buf, SrcSpec):
                try:
                    self.pv[k] = SrcPlayerInst(buf.load())
                except Exception as ex:
                    print("Preview error:", name, ex)
                    self.pv[k] = None
            else:
                self.pv[k] = None  # GoldSrc player models no longer supported
        return self.pv[k]

    def _bbox(self, inst):
        bb = getattr(inst, "_pv_bb", None)   # stored on the instance itself: id() gets reused after pv.clear()
        if bb is None:
            mn, mx = np.full(3, 1e9), np.full(3, -1e9)
            for a in inst.pos:
                if a is not None and len(a):
                    mn = np.minimum(mn, a.min(axis=0))
                    mx = np.maximum(mx, a.max(axis=0))
            if mn[0] > mx[0]:
                mn, mx = np.zeros(3), np.ones(3)
            bb = (mn, mx)
            try:
                inst._pv_bb = bb
            except Exception:
                pass
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
                # Player preview shows the rest pose (T-pose)
                if hasattr(inst, "animate"):
                    try:
                        # stand still (idle) and advance by REAL elapsed time, so the speed never
                        # depends on the frame rate; the model just turns on the turntable
                        # T-pose: hold the model's rest (bind) pose and just spin it on the turntable
                        if not getattr(inst, "_pv_tpose", False):
                            inst._skin_rest()
                            inst._pv_tpose = True
                    except Exception:
                        pass
                    mn, mx = self._bbox(inst)
                    c = (mn + mx) / 2
                    size = np.maximum(mx - mn, 1.0)
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
            # arms packs are Source .vpk / archives - same loader as weapons (adds arms via src_arms_all)
            self.status = self.g.load_files(paths, "player" if mode == "player" else "weapon") or self.status
            if mode == "arms":
                self.lo_page = 0
                if self.g.arms_all and not self.g.arms_pick and not self.g.arms_off:
                    # auto-equip first newly available arms if nothing was equipped yet
                    first = sorted(self.g.arms_all.keys())[0]
                    self.g.set_arms(first)
                    self.status = (self.status + f" Equipped {first}.").strip()


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
    gl.glClearColor(*FX.fog_color(), 1)
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
    _sk = game.shake * SHAKE
    if _sk > 0.002:
        _a = _sk * _sk * 0.03 + _sk * 0.004              # radians of jitter
        vp = max(-1.5, min(1.5, vp + random.uniform(-_a, _a)))
        vy += random.uniform(-_a, _a)
        gl.glRotatef(random.uniform(-1, 1) * _sk * 0.9, 0, 0, 1)   # tiny roll
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
            gl.glScalef(PLAYER_SCALE, PLAYER_SCALE, PLAYER_SCALE)   # shrink around the feet
            gl.glRotatef(gy if gy is not None else math.degrees(e.yaw), 0, 0, 1)  # legs yaw; torso twists on top
            e.pm.draw()
            gl.glPopMatrix()
        else:
            gl.glPushMatrix()
            gl.glTranslatef(e.x, e.y, e.z - 36)
            gl.glScalef(PLAYER_SCALE, PLAYER_SCALE, PLAYER_SCALE)   # shrink around the feet
            gl.glTranslatef(0, 0, 36)
            gl.glRotatef(math.degrees(e.yaw), 0, 0, 1)
            body = (0.95, 0.85, 0.0) if e.team else (0.08, 0.08, 0.08)   # T = yellow, CT = black
            swing = math.sin(e.ph) * e.bob * 45 * (1 - e.air)
            lift = (math.sin(e.ph) ** 2) * e.bob * 0.6 - e.land * 3
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
    gl.glTranslatef(0, math.sin(P.ph) * VM_BOB_SIDE * P.bob,
                    -(math.sin(P.ph) ** 2) * VM_BOB_UP * P.bob
                    + P.air * .4 - P.land * VM_LAND)
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
    gfx.rect(x0 - 12, top - 12, total_w + 24, rows * rh + 110, (22 / 255, 22 / 255, 22 / 255, .94))
    gfx.text("SCOREBOARD", W / 2, top - 4, 12, (154, 154, 154), bold=True, anchor="c")
    for team in (0, 1):
        x = x0 + team * (colw + gap)
        col = TEAM_CT if team == 0 else TEAM_T
        gfx.rect(x, top + 22, colw, 34, team_tint(col))
        gfx.text("BLACK TEAM" if team == 0 else "YELLOW TEAM", x + 10, top + 30, 15, col, bold=True)
        gfx.text(str(game.score[team]), x + colw - 10, top + 28, 22, col, bold=True, anchor="r")
        y = top + 62
        gfx.text("PLAYER", x + 10, y, 11, (154, 154, 154), bold=True)
        gfx.text("K", x + colw - 70, y, 11, (154, 154, 154), bold=True, anchor="r")
        gfx.text("D", x + colw - 12, y, 11, (154, 154, 154), bold=True, anchor="r")
        y += 20
        members = sorted((e for e in game.ents if e.team == team),
                         key=lambda e: (-e.kills, e.deaths, e.name))
        for e in members:
            if e.isP:
                gfx.rect(x, y - 2, colw, rh - 2, (42 / 255, 42 / 255, 42 / 255, 1))
            c = (255, 193, 7) if e.isP else ((255, 255, 255) if e.alive else (130, 130, 130))
            gfx.text(e.name or "UNKNOWN", x + 10, y, 14, c, bold=bool(e.isP), mono=True)
            gfx.text(str(e.kills), x + colw - 70, y, 14, c, anchor="r", mono=True)
            gfx.text(str(e.deaths), x + colw - 12, y, 14, c, anchor="r", mono=True)
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
    cw_ = gfx.text_w(f"BLACK {sc[0]}", 20, True) + 20
    tw_ = gfx.text_w(f"{sc[1]} YELLOW", 20, True) + 20
    gfx.rect(W / 2 - 14 - cw_, 12, cw_, 32, team_tint(TEAM_CT))
    gfx.rect(W / 2 + 14, 12, tw_, 32, team_tint(TEAM_T))
    gfx.text(f"BLACK {sc[0]}", W / 2 - 14 - 10, 16, 20, TEAM_CT, bold=True, anchor="r")
    gfx.text("-", W / 2, 16, 20, (255, 255, 255), bold=True, anchor="c")
    gfx.text(f"{sc[1]} YELLOW", W / 2 + 14 + 10, 16, 20, TEAM_T, bold=True)
    gfx.text(f"(limit {CFG['frags']})", W / 2, 50, 12, (200, 200, 200), anchor="c")
    if game.over:
        gfx.text(("Black" if sc[0] > sc[1] else "Yellow") + " team wins!", W / 2, 62, 20, (255, 255, 255), bold=True, anchor="c")

    # Minimal health indicator: dark gray track + bar (green, red under 30 HP), no number or cross.
    hp_value = max(0, min(100, math.ceil(P.hp / 2)))
    hp_ratio = hp_value / 100.0
    bar_x = max(28, W / 2 - 300)  # move health bar farther left
    bar_y = H - 51
    bar_w, bar_h = 110, 8
    if P.alive:
        gfx.rect(bar_x, bar_y, bar_w, bar_h, (22 / 255, 22 / 255, 22 / 255, .94))  # dark gray background
        if hp_ratio > 0:
            hp_col = (0.86, 0.15, 0.15, 0.98) if hp_value < 30 else (0.25, 0.78, 0.34, 0.98)
            gfx.rect(bar_x, bar_y, bar_w * hp_ratio, bar_h, hp_col)
    w = CFG["weapons"][P.w]
    # Weapon HUD: guns show only the ammo counter, the knife shows its name; both on a dark panel like the HP bar.
    hud_txt = w.n if w.melee else f"{P.ammo[P.w]} / {w.mag}" + (" (reloading)" if P.cool > 1 else "")
    hud_w = gfx.text_w(hud_txt, 22, True, True)
    gfx.rect(W - 22 - hud_w - 12, H - 50 - 6, hud_w + 24, 22 + 12, (22 / 255, 22 / 255, 22 / 255, .94))  # same dark gray as HP background
    gfx.text(hud_txt, W - 22, H - 50, 22, (255, 255, 255), bold=True, anchor="r", mono=True)

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
        gfx.rect(x - 4, y - 4, tw + 8, rh + 8, (22 / 255, 22 / 255, 22 / 255, .94))
        for sx, sw, team, mine in ((x, kw, en["kt"], en["kl"]), (x + kw + ww, vw, en["vt"], en["vl"])):
            col = TEAM_CT if team == 0 else TEAM_T
            gfx.rect(sx, y, sw, rh, team_tint(col))
            if mine:
                gfx.rect(sx, y + rh - 3, sw, 3, (1, 1, 1, 1) if col == TEAM_CT else (1, 214 / 255, 0, 1))
        _kc = (0, 0, 0) if en["kt"] == 0 else (255, 214, 0)
        _vc = (0, 0, 0) if en["vt"] == 0 else (255, 214, 0)
        gfx.text(en["killer"], x + 10, y + 5, 14, _kc, bold=en["kl"], mono=True)
        gfx.text(label, x + kw + ww / 2, y + 8, 11, (154, 154, 154), bold=True, anchor="c")
        gfx.text(en["victim"], x + kw + ww + 10, y + 5, 14, _vc, bold=en["vl"], mono=True)
        y += rh + 12

    # kill streak counter (under the kill feed)
    if P.alive:
        ky = 18 + 5 * 38 + 8   # fixed spot: just below the room for 5 kill feed rows
        gfx.rect(W - 18 - 150, ky - 4, 150, 30, (22 / 255, 22 / 255, 22 / 255, .94))
        gfx.text("KILL STREAK", W - 18 - 140, ky + 5, 11, (154, 154, 154), bold=True)
        gfx.text(str(game.streak), W - 18 - 10, ky + 2, 20, (255, 193, 7), bold=True, anchor="r", mono=True)

    # Health vignette removed: no red border at low HP.

    # death screen (same look as the scoreboard)
    if not P.alive:
        gfx.rect(0, 0, W, H, (0, 0, 0, .45))
        pw, ph = 380, 168
        px, py = W / 2 - pw / 2, H / 2 - ph / 2
        gfx.rect(px - 12, py - 12, pw + 24, ph + 24, (22 / 255, 22 / 255, 22 / 255, .94))
        gfx.text("AKB / ARENA", W / 2, py - 4, 12, (154, 154, 154), bold=True, anchor="c")
        col = TEAM_CT if P.team == 0 else TEAM_T
        gfx.rect(px, py + 22, pw, 34, team_tint(col))
        gfx.text("ELIMINATED", px + 10, py + 30, 15, col, bold=True)
        gfx.text(f"{P.kills} / {P.deaths}", px + pw - 10, py + 28, 22, col, bold=True, anchor="r")
        gfx.text("KILLED BY", px + 10, py + 66, 11, (154, 154, 154), bold=True)
        gfx.rect(px, py + 84, pw, 24, (42 / 255, 42 / 255, 42 / 255, 1))
        gfx.text(game.death_killer, px + 10, py + 88, 14, (255, 193, 7), bold=True, mono=True)
        gfx.text(f"RESPAWNING IN {max(1, math.ceil(P.resp))}", W / 2, py + 130, 12, (154, 154, 154), bold=True, anchor="c")

    # scoreboard (hold Tab)
    if game.keys.get("tab") or game.over:
        draw_scoreboard(game, gfx, W, H)
    if game.show_fps:
        gfx.text(f"{int(round(game.fps_val))} FPS", 12, 10, 14, (255, 255, 255), bold=True, mono=True)
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
        if isinstance(aj, str):
            if aj:
                game.arms_want = aj
            else:
                # explicit empty string from settings = user chose "No arms"
                game.arms_want = None
                game.arms_off = True
        elif aj is None and "arms" in j:
            game.arms_want = None
            game.arms_off = True
        pmj = j.get("player_models", {})
        for tk in ("CT", "T"):
            if isinstance(pmj.get(tk), str):
                game.pm_saved[tk] = pmj[tk]
        game.fov_cfg = max(50.0, min(110.0, float(j.get("fov", 75.0) or 75.0)))
        game.fov = game.fov_cfg
        ar = float(j.get("aspect", 0) or 0)
        game.aspect = ar if 1.0 <= ar <= 2.5 else 0.0
        game.want_fullscreen = bool(j.get("fullscreen", False))
        game.show_fps = bool(j.get("show_fps", False))
        FX.day = bool(j.get("day", False))
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


def set_fullscreen(game, on):
    """Switch fullscreen on/off without recreating the GL context (so textures / models stay loaded)."""
    on = bool(on)
    if on == bool(game.fullscreen):
        return
    try:
        pygame.display.toggle_fullscreen()
        game.fullscreen = on
    except Exception as ex:
        print("Fullscreen failed:", ex)


def save_settings(game, menu):
    try:
        SETTINGS_FILE.write_text(json.dumps({
            "arms": (game.arms_pick or ("" if game.arms_off else None)), "binds": game.binds, "hand_swap": game.hand_swap, "bots_on": game.bots_on, "player_models": game.pm_saved, "weapon_models": {str(k): v for k, v in game.wm_pick.items()}, "day": bool(FX.day), "aspect": game.aspect, "fullscreen": bool(game.fullscreen), "show_fps": bool(game.show_fps), "fov": game.fov_cfg, "vm_offset": game.vm_adj,
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
        "map": [("FAD-MOD map", "*.fadmap")],
        "arms": [("Arms / hands packs", "*.zip *.7z *.rar *.mdl *.vpk *.vpk.part *.vvd *.vtx *.vtf *.vmt")],
        "weapon": [("Weapon mods (CS / L4D2)", "*.mdl *.wav *.json *.vpk *.vpk.part *.zip *.7z *.rar *.vvd *.vtx *.vtf *.vmt")],
        "player": [("Player models", "*.mdl *.zip *.7z *.rar *.vpk *.vpk.part")],
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
    if sys.platform == "win32":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    pygame.init()
    try:
        pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 1)
        pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 4)
    except Exception:
        pass
    flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.RESIZABLE
    try:
        pygame.display.set_mode((1280, 720), flags, vsync=0)
    except Exception:
        pygame.display.set_mode((1280, 720), flags)
    pygame.display.set_caption("FAD-MOD")
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
    if getattr(game, "want_fullscreen", False):
        set_fullscreen(game, True)
    game.player_name = menu.name.strip() or "Player"
    game.P.name = game.player_name

    # Scan the mod folders so everything shows up in Loadout. Nothing is EQUIPPED automatically
    # (game.manual_mods): you pick the weapons / players / arms / sounds yourself, or play with none.
    # Weapon mods (.mdl / .wav / .json) from mod/weapon (never created).
    try:
        wfiles = sorted(f for wdir in mod_folders("weapon") for f in wdir.rglob("*")
                        if f.is_file() and (f.suffix.lower() in (".mdl", ".wav", ".json", ".vpk", ".zip", ".7z", ".rar", ".vvd", ".vtx", ".vtf", ".vmt")
                                            or f.name.lower().endswith(".vpk.part")))
        if wfiles:
            st = game.load_files(wfiles, "weapon")
            if st:
                menu.status = st
    except Exception as ex:
        menu.status = "mod/weapon: " + str(ex)

    # Auto-load player models from mod/player (never created).
    # The CT / T choice you make in the Play menu is remembered between runs.
    try:
        pfiles = sorted(f for pdir in mod_folders("player") for f in pdir.rglob("*") if f.is_file() and (
            f.suffix.lower() in (".mdl", ".zip", ".7z", ".rar", ".vpk") or f.name.lower().endswith(".vpk.part")))
        if pfiles:
            game.load_files(pfiles, "player")
            menu.status = (menu.status + f" Players: {len(game.asset['player'])} model(s).").strip()
    except Exception as ex:
        menu.status = "mod/player: " + str(ex)

    # Auto-load arms/hands from the "arms/" folder (next to this script and/or cwd).
    try:
        st = game.load_arms_folder()
        if st:
            menu.status = (menu.status + " " + st).strip()
    except Exception as ex:
        menu.status = (menu.status + " Arms: " + str(ex)).strip()

    # Auto-load custom sounds from mod/sound (never created):
    # a file with "shoot" / "shot" / "fire" in its name = shooting sound,
    # a file with "hit" in its name = hit sound. A sound picked in the menu wins.
    try:
        sfiles = sorted(f for sdir in mod_folders("sound") for f in sdir.rglob("*")
                        if f.is_file() and f.suffix.lower() in (".wav", ".ogg", ".mp3"))
        got = []
        for kind, rx in (("hit", r"hit"), ("shot", r"shoot|shot|fire")):
            if game.manual_mods or getattr(game, f"custom_{kind}_path"):
                continue   # sounds are picked in Settings > Sounds, not applied automatically
            f = next((f for f in sfiles if re.search(rx, f.stem, re.I)), None)
            if f:
                game.set_custom_sound(kind, str(f))
                if getattr(game, f"custom_{kind}_path"):
                    got.append(f"{kind} sound <- {f.name}")
        if got:
            menu.status = (menu.status + " Sounds: " + ", ".join(got) + ".").strip()
    except Exception as ex:
        menu.status = "mod/sound: " + str(ex)

    if not any(mod_folders(n) for n in ("weapon", "player", "arms", "sound")):
        menu.status = (menu.status + " No mod folder found next to the script (mod/weapon, mod/player, mod/arms, mod/sound).").strip()

    clock = pygame.time.Clock()
    last = time.perf_counter()
    running = True
    rt = RenderTarget(RENDER_W, RENDER_H)
    while running:
        win_w, win_h = pygame.display.get_window_size()
        W, H = (RENDER_W, RENDER_H) if rt.ok else (win_w, win_h)
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
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F11:
                set_fullscreen(game, not game.fullscreen)
                save_settings(game, menu)
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
                    menu.mouse_move(rt.to_render(ev.pos) if rt.ok else ev.pos)
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
                            if P.zoom:
                                P.zoom = 0
                            elif P.reloadT <= 0:   # can't scope in while reloading
                                P.zoom = 1
                else:
                    if ev.button in (6, 7) and menu.waiting:
                        menu.bind_press("Mouse" + str(ev.button - 2))
                    else:
                        menu.mouse_down(rt.to_render(ev.pos) if rt.ok else ev.pos, ev.button)
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
        raw_dt = now - last
        dt = min(.05, raw_dt)
        last = now
        if raw_dt > 0:   # smoothed FPS for the counter
            game.fps_val = game.fps_val * 0.9 + (1.0 / raw_dt) * 0.1 if game.fps_val else 1.0 / raw_dt
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

        if rt.ok:
            rt.begin()
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
        if rt.ok:
            rt.present(win_w, win_h)
        pygame.display.flip()
        clock.tick()   # no FPS cap

    NET.close()
    save_settings(game, menu)
    pygame.quit()


if __name__ == "__main__":
    main()