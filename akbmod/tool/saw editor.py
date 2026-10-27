#!/usr/bin/env python3
"""
FAD-MOD MAP BUILDER - build maps for fad-mod.py in a 3D view.

Run:  python map_builder.py        (missing packages install automatically)

What you do
  1. Build with boxes (walls, floors, stairs, crates ...). Put an image on any box as its texture.
  2. Place spawn points for the BLACK team and the YELLOW team.
  3. Press F5 (Compile). You get a .fadmap file.  In the game: Play > Load map  (or drag the file onto the game).

Controls
  Camera   hold RIGHT mouse + mouse = look      W A S D = move     Q / E = down / up
           Shift = fast       mouse wheel = camera speed
  Tools    V select    B box    1 spawn BLACK team    2 spawn YELLOW team
  Box      click + drag on the floor (or on top of another box) = draw the footprint.
           It gets the current Height. Mouse wheel while dragging changes the height.
  Slope    press 3, press R (or T) to pick the uphill direction, then drag the footprint like a box.
           Height = how high the top end is.  A real smooth slope.
  Circle   press 4, click the CENTER and drag outward = radius.  Height works like a box.
           A real smooth cylinder.  Select / move / resize / texture them like boxes.
  Select   click a box / spawn.   DRAG one of its 3 arrows (red X, green Y, blue Z) to move it along that axis
           (Shift = free, no grid snap).   Dragging the object itself still slides it on the floor.
           Ctrl + click = add / remove it from the selection (select many; move, copy, delete them together)
           Arrows = move    PageUp / PageDown = up / down
           Shift + Up/Down = longer / shorter      Shift + Left/Right = narrower / wider
           Shift + PageUp/PageDown = taller / shorter
           Ctrl+C copy    Ctrl+X cut    Ctrl+V paste (lands where the mouse points: floor or top of a box)
           Del = delete      Ctrl+D = duplicate      R = turn a spawn (Shift+R = other way)
  Spin     press 5 (or Y), click a box / slope / circle / spawn. A ring with an ARROW appears around it:
           DRAG the arrow (or the ring) to spin it. Snaps to the turn step; hold Shift = free angle.
           , and . = turn the selection one step      U = change the step (5 / 15 / 30 / 45 / 90)
  Stretch  press 6 (or X), click a box. Arrows appear on its faces: DRAG an arrow to push / pull that
           face (snaps to the grid, Shift = free). Works on slopes and circles too.
           F5 keeps spun objects spun: the game rotates them for real (look and collision).
  Decal    press 7 (or L). Pick an image on the right (or "Add image"), then CLICK a wall or floor: it is stuck there.
           (Or DRAG an image from the right panel, or from your PC, onto a wall.)  Click a decal to select it.
           DRAG the decal = slide it on the wall.  DRAG a corner square = resize (same picture shape, Shift = stretch).
           Mouse wheel or - / = = bigger / smaller    Ctrl+D = copy    Del = delete    click another image = swap it
           F5 writes decals as flat pictures on the wall (no collision). Use the new fad-mod.py to see them.
  Skybox   "Set image" in the left panel (or drop an image on the empty sky) = skybox.
           Use a wide panorama (2:1) for best results.
           CUBEMAP: "Set cubemap" and pick 6 images named px nx py ny pz nz, OR ONE cross picture (4x3) with all faces (also posx/negx..., or
           right left up down front back, or rt lf up dn ft bk). Or drop the face files on the sky
           one by one. px/nx = east/west, py/ny = up/down, pz/nz = south/north. The game gets it baked.
Texture  DRAG an image file from your PC and DROP it on the face you want (that one face gets it).
           Or "Add image", then click it in the right panel (whole selected box),
           Shift+click = top only, Ctrl+click = sides only, or DRAG it from the panel onto a face.
           [ and ] = texture size
  PixelZone press 8 (or Z), drag a block like a box. It is INVISIBLE in the game and not solid: wherever you stand inside it
           you can pixel surf. Outside every zone, pixel surf does nothing. Select / move / stretch / spin / copy it like a box.
  Ladder   press 9 (or K), drag a block like a box (make it tall, thin and touching the wall), press R to pick which side
           you climb it from (the arrow points at the player; the wall is on the other side). Invisible block with rungs
           drawn in the editor; in the game it is a real CS:GO-style ladder: walk into it to grab it, W / S + look up or
           down to climb, A / D to shuffle sideways, Jump to leap off. Select / move / stretch / spin / copy it like a box
           (R on a selected ladder turns its side).
  Source   Ctrl+I (or the Import button, or drop the file on the window) = load a Garry's Mod addon (.gma, or a Workshop
  maps    legacy .bin), a Source addon package (.vpk) or map (.bsp). ALL of its geometry comes in: every brush, face and displacement is baked into the .fadmap, drawn
           with its textures and collided with exactly (slopes, angled walls, arches ...). Its player spawns come along, and its LADDERS
           (func_useableladder / func_ladder) become ladder zones (orange blocks) you can move, turn with R, or delete.
           Textures: the map's own pack is read; stock textures (hl2 / css / csgo / garrysmod) are found if those games
           are installed (every Steam library on every drive is searched). PROPS come in too: static props and
           prop_dynamic / prop_physics models are baked into the map (solid ones get a box of collision), and the
           info_decal pictures are stuck on the walls. Models too heavy for their number of copies use a simpler
           level of detail (see PROP_TRI_BUDGET). You can still build boxes, spawns and pixel surf zones on top of it.
  Files    Ctrl+S save project   Ctrl+O open   F5 compile   Ctrl+Z undo   Ctrl+Y redo
           G = next grid size    F = look at selection     F1 = help
"""
import io
import sys
import json
import math
import time
import base64
import re
import zlib
import lzma
import struct
import itertools
import zipfile
from pathlib import Path


def _ensure_deps():
    """Install missing packages automatically (same ones the game uses)."""
    import importlib.util
    import subprocess

    def pip(pkgs):
        base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:"]
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
    importlib.invalidate_caches()


_ensure_deps()

import numpy as np
import pygame
from OpenGL import GL as gl

HERE = Path(__file__).resolve().parent
MAP_FORMAT = "fadmap"
MAP_VERSION = 1
GRIDS = (8, 16, 32, 64, 128)
TILES = (32, 64, 128, 256, 512, 1024)
TEAM_NAMES = ("BLACK", "YELLOW")
TEAM_COL = ((0.35, 0.70, 1.00), (1.00, 0.84, 0.10))
DEFAULT_COL = (176, 160, 144)
HULL_R, HULL_H = 16.0, 72.0          # player: 32 wide, 72 tall (same as the game)
SELECT, BOX, SPAWN0, SPAWN1, RAMP, CIRC, ROT = "select", "box", "spawn0", "spawn1", "ramp", "circle", "rotate"
STRETCH = "stretch"
PXZ = "pxzone"                        # builds an invisible pixel surf zone
LADDER = "ladder"                     # builds a climbable ladder zone (CS:GO style)
LAD_DIRS = ("+X", "+Y", "-X", "-Y")   # the side the player climbs from (the ladder surface normal)
LAD_N = ((1, 0), (0, 1), (-1, 0), (0, -1))
DECAL = "decal"
DECAL_THICK = 0.1                    # compiled decals are paper thin; the game draws them flush with no collision
DEC_MIN = 8.0                        # smallest decal side
# decal face number (same numbers as Box.f): 0 top, 2 -y, 3 +y, 4 -x, 5 +x
# -> (axis of the normal, horizontal axis, vertical axis, +1 / -1 = which way the face looks)
DEC_AXES = {0: (2, 0, 1, 1), 2: (1, 0, 2, -1), 3: (1, 0, 2, 1), 4: (0, 1, 2, -1), 5: (0, 1, 2, 1)}
DEC_NORMAL = {0: (0, 0, 1), 2: (0, -1, 0), 3: (0, 1, 0), 4: (-1, 0, 0), 5: (1, 0, 0)}
TURN_SNAPS = (5, 15, 30, 45, 90)     # degrees, for the spin / turn tool
RAMP_DIRS = ("+X", "+Y", "-X", "-Y")


def gen_ramp(x0, y0, x1, y1, z, h, d):
    """Ramp = a row of thin boxes (so the game needs no changes). d: 0 +x, 1 +y, 2 -x, 3 -y = uphill direction."""
    along_x = d in (0, 2)
    L = (x1 - x0) if along_x else (y1 - y0)
    n = int(max(2, min(256, h / 1.5, L)))
    out = []
    for i in range(n):
        a, b = L * i / n, L * (i + 1) / n
        top = z + h * (i + 1) / n
        if d == 2:
            a, b = L - b, L - a
        if d == 3:
            a, b = L - b, L - a
        if along_x:
            out.append([x0 + a, y0, z, x0 + b, y1, top])
        else:
            out.append([x0, y0 + a, z, x1, y0 + b, top])
    return out


def gen_circle(cx, cy, r, z, h):
    """Round pillar / disc = strips of boxes."""
    s = max(2.0, 2 * r / 160.0)
    n = int(math.ceil(2 * r / s))
    out = []
    for j in range(n):
        ya = cy - r + j * s
        yb = min(ya + s, cy + r)
        dy = (ya + yb) / 2 - cy
        hw = math.sqrt(max(0.0, r * r - dy * dy))
        if hw >= 1 and yb - ya >= 1:
            out.append([cx - hw, ya, z, cx + hw, yb, z + h])
    return out
LW, RW, SB = 196, 236, 26           # left panel, right panel, status bar sizes


# --------------------------------------------------------------------------
# Map data
# --------------------------------------------------------------------------
class Box:
    __slots__ = ("b", "t", "tt", "tile", "col", "f", "k", "d", "yaw", "px", "lad")

    def __init__(self, b, t=-1, tt=None, tile=128.0, col=DEFAULT_COL, f=None, k=0, d=0, yaw=0.0, px=0, lad=0):
        self.b = [float(v) for v in b]
        self.lad = 1 if lad else 0               # 1 = LADDER ZONE: invisible, not solid; d = the side you climb it from (0 +x, 1 +y, 2 -x, 3 -y)
        self.px = 1 if px else 0                 # 1 = PIXEL SURF ZONE: an invisible block, no collision; inside it you can pixel surf
        self.yaw = float(yaw)                    # spin around the box centre, degrees (counter-clockwise from above)
        self.k = int(k)                          # 0 box, 1 slope, 2 cylinder
        self.d = int(d)                          # slope uphill direction: 0 +x, 1 +y, 2 -x, 3 -y
        self.t = int(t)                          # texture of the sides + bottom (-1 = flat colour)
        self.tt = self.t if tt is None else int(tt)   # texture of the top
        self.tile = float(tile)                  # world units per texture repeat
        self.col = tuple(col)
        # per-face texture override: [top, bottom, -y, +y, -x, +x]; -1 = use t / tt
        f = list(f) if f else []
        self.f = [int(v) for v in (f + [-1] * 6)[:6]]

    def copy(self):
        return Box(list(self.b), self.t, self.tt, self.tile, self.col, self.f, self.k, self.d, self.yaw, self.px, self.lad)

    def fix(self):
        b = self.b
        for i in range(3):
            if b[i] > b[i + 3]:
                b[i], b[i + 3] = b[i + 3], b[i]


class Spawn:
    __slots__ = ("team", "x", "y", "z", "yaw")

    def __init__(self, team, x, y, z, yaw=0.0):
        self.team, self.x, self.y, self.z, self.yaw = int(team), float(x), float(y), float(z), float(yaw)

    def copy(self):
        return Spawn(self.team, self.x, self.y, self.z, self.yaw)


class Decal:
    """A picture stuck flat on one face of a box (a wall side or a floor top).
    F5 turns it into a very thin box, so the game needs no change.
    fi = face number (see DEC_AXES), p = where the face is, c0..c1 = across, v0..v1 = up (floor: along y)."""
    __slots__ = ("tex", "fi", "p", "c0", "c1", "v0", "v1", "col", "hb", "fx", "fy", "op")

    def __init__(self, tex, fi, p, c0, c1, v0, v1, col=DEFAULT_COL, hb=None, fx=False, fy=False, op=1.0):
        self.fx, self.fy, self.op = bool(fx), bool(fy), max(0.0, min(1.0, float(op)))
        self.tex, self.fi, self.p = int(tex), int(fi), float(p)
        self.c0, self.c1, self.v0, self.v1 = float(c0), float(c1), float(v0), float(v1)
        self.col = tuple(col)
        self.hb = [float(v) for v in hb] if hb else None       # the face it was put on: [h0, h1, v0, v1]

    def copy(self):
        return Decal(self.tex, self.fi, self.p, self.c0, self.c1, self.v0, self.v1, self.col, self.hb,
                     self.fx, self.fy, self.op)

    def hv(self, s):
        """Across-value at s (0 = left .. 1 = right, as seen from outside)."""
        w = self.c1 - self.c0
        return self.c0 + s * w if self.fi in (0, 2, 5) else self.c1 - s * w

    def vv(self, t):
        """Up-value at t (0 = bottom .. 1 = top)."""
        return self.v0 + t * (self.v1 - self.v0)

    def pt(self, hv, vv, off=0.0):
        na, ha, va, sg = DEC_AXES[self.fi]
        q = [0.0, 0.0, 0.0]
        q[ha], q[va], q[na] = hv, vv, self.p + sg * off
        return tuple(q)

    def corner(self, s, t, off=0.0):
        return self.pt(self.hv(s), self.vv(t), off)

    def to_dict(self):
        return {"tex": self.tex, "fi": self.fi, "p": round(self.p, 3),
                "r": [round(v, 3) for v in (self.c0, self.c1, self.v0, self.v1)],
                "c": list(self.col), "hb": self.hb, "fx": self.fx, "fy": self.fy, "op": round(self.op, 3)}


class Tex:
    """An image used as a wall texture."""

    def __init__(self, name, surf):
        self.name = name
        self.surf = surf          # RGBA pygame Surface (max 1024 px)
        self.gid = None

    def upload(self):
        w, h = self.surf.get_size()
        self.gid = make_gl_tex(surf_bytes(self.surf), w, h, True)


def surf_bytes(surf):
    f = getattr(pygame.image, "tobytes", None) or pygame.image.tostring
    return f(surf, "RGBA")


def make_gl_tex(data, w, h, mip=True, clamp=False):
    tid = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
    wrap = gl.GL_CLAMP_TO_EDGE if clamp else gl.GL_REPEAT
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, wrap)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, wrap)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
    if mip:
        try:
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
        except Exception:
            pass
    return tid


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


# ----- Valve VTF textures (DXT1 / DXT3 / DXT5 + common plain formats) -----
_VTF_BPP = {0: 4, 1: 4, 2: 3, 3: 3, 4: 2, 5: 1, 6: 2, 7: 1, 8: 1, 11: 4, 12: 4, 16: 4, 17: 2,
            18: 2, 19: 2, 21: 2, 22: 2, 23: 4, 24: 8, 25: 8, 26: 4}


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


def load_vtf_bytes(blob, max_dim=1024):
    """VTF file bytes -> (rgba uint8 array (h,w,4), has_real_alpha), biggest mip <= max_dim."""
    head = blob[:96]
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
    if struct.unpack_from("<I", head, 8)[0] >= 3 and len(blob) >= 80:          # 7.3+: the picture is listed in a table
        for ri in range(min(struct.unpack_from("<I", blob, 68)[0], 32)):
            if blob[80 + ri * 8:83 + ri * 8] == b"\x30\x00\x00":
                off = struct.unpack_from("<I", blob, 84 + ri * 8)[0]
                break
    m = 0
    while max(w >> m, h >> m) > max_dim and m < mips - 1:
        m += 1
    sizes = [_vtf_size(fmt, max(1, w >> k), max(1, h >> k)) for k in range(mips)]
    faces = 6 if flags & 0x4000 else 1
    pos = off + sum(sizes[m + 1:]) * frames * faces
    mw, mh = max(1, w >> m), max(1, h >> m)
    raw = blob[pos:pos + sizes[m]]
    if len(raw) < sizes[m]:
        raise ValueError("VTF file is cut off")
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
        elif fmt in (4, 17, 18, 19, 21):
            v = a[:, :, 0].astype(np.uint32) | (a[:, :, 1].astype(np.uint32) << 8)
            if fmt in (4, 17):
                c5 = lambda x, s, m: ((x >> s) & m) * 255 // m
                r, g, b = c5(v, 11, 31), c5(v, 5, 63), c5(v, 0, 31)
                if fmt == 17:
                    r, b = b, r
                al = np.full_like(r, 255)
            elif fmt == 19:
                c4 = lambda s: ((v >> s) & 15) * 17
                b, g, r, al = c4(0), c4(4), c4(8), c4(12)
            else:
                c5 = lambda s: ((v >> s) & 31) * 255 // 31
                b, g, r = c5(0), c5(5), c5(10)
                al = ((v >> 15) & 1) * 255 if fmt == 21 else np.full_like(r, 255)
            img = np.stack([r, g, b, al], 2).astype(np.uint8)
        elif fmt in (24, 25):
            if fmt == 24:
                f = np.frombuffer(raw, np.float16, mw * mh * 4).reshape(mh, mw, 4).astype(np.float32)
                img = (np.clip(f, 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
            else:
                img = (np.frombuffer(raw, "<u2", mw * mh * 4).reshape(mh, mw, 4) >> 8).astype(np.uint8)
            img = np.concatenate([img[:, :, :3], np.full((mh, mw, 1), 255, np.uint8)], 2)
        elif fmt in (22, 23, 26):
            img = np.stack([a[:, :, 0], a[:, :, 1], np.full((mh, mw), 128, np.uint8), np.full((mh, mw), 255, np.uint8)], 2)
        else:
            raise ValueError("unsupported VTF format %d" % fmt)
    return np.ascontiguousarray(img, dtype=np.uint8), bool(flags & 0x2000 or flags & 0x1000)



def load_vtf_surface(path, max_side):
    rgba, _alpha = load_vtf_bytes(Path(path).read_bytes(), max_side)
    h, w = rgba.shape[:2]
    s = pygame.image.frombuffer(rgba.tobytes(), (w, h), "RGBA").convert_alpha()
    return s


def load_image(path, max_side=1024):
    """Any image file (also Valve .vtf) -> opaque RGBA Surface, shrunk to max_side."""
    if str(path).lower().endswith(".vtf"):
        s = load_vtf_surface(path, max_side)
    else:
        s = pygame.image.load(str(path))
    s = s.convert_alpha()
    w, h = s.get_size()
    if max(w, h) > max_side:
        k = max_side / max(w, h)
        s = pygame.transform.smoothscale(s, (max(1, int(w * k)), max(1, int(h * k))))
    return s                                  # keep the alpha channel (transparent PNGs stay transparent)


def encode_tex(tex, max_side):
    s = tex.surf
    w, h = s.get_size()
    if max(w, h) > max_side:
        k = max_side / max(w, h)
        s = pygame.transform.smoothscale(s, (max(1, int(w * k)), max(1, int(h * k))))
        w, h = s.get_size()
    try:
        bio = io.BytesIO()
        pygame.image.save(s, bio, "x.png")
        data = bio.getvalue()
        if data[:4] == b"\x89PNG":
            return {"name": tex.name, "w": w, "h": h, "png": base64.b64encode(data).decode("ascii")}
    except Exception:
        pass
    return {"name": tex.name, "w": w, "h": h, "raw": base64.b64encode(zlib.compress(surf_bytes(s), 6)).decode("ascii")}


def decode_tex(d):
    """dict from a map file -> Tex (needs a display for convert_alpha)."""
    w, h = int(d.get("w", 0)), int(d.get("h", 0))
    if "png" in d:
        s = pygame.image.load(io.BytesIO(base64.b64decode(d["png"])), "x.png").convert_alpha()
    else:
        raw = zlib.decompress(base64.b64decode(d["raw"]))
        s = pygame.image.frombuffer(raw, (w, h), "RGBA").convert_alpha()
    return Tex(d.get("name", "texture"), s)


def bake_decal(dc, tex):
    """Decal -> (Tex, tile). The game maps textures world-aligned (face_uv), so the picture is put at the
    right spot inside one texture tile: then a thin box with that tile shows it exactly on the decal."""
    w, h = dc.c1 - dc.c0, dc.v1 - dc.v0
    T = max(1.0, round(max(w, h), 2))
    iw, ih = tex.surf.get_size()
    S = int(max(32, min(512, round(max(iw * T / w, ih * T / h)))))
    pw, ph = max(1, int(round(w / T * S))), max(1, int(round(h / T * S)))
    img = pygame.transform.smoothscale(tex.surf, (pw, ph))
    flipx, flipy = (dc.fi in (3, 4)) != dc.fx, (dc.fi == 0) != dc.fy    # game mapping runs backwards on some faces + user flip
    if flipx or flipy:
        img = pygame.transform.flip(img, flipx, flipy)
    if dc.op < 0.999:                                     # opacity is baked into the alpha channel
        img = img.convert_alpha()
        img.fill((255, 255, 255, int(round(dc.op * 255))), special_flags=pygame.BLEND_RGBA_MULT)

    def fr(x):
        return x - math.floor(x)
    ox = int(round(fr(dc.c0 / T) * S))
    oy = int(round(fr((dc.v0 if dc.fi == 0 else -dc.v1) / T) * S))
    cell = pygame.Surface((S, S), pygame.SRCALPHA, 32)
    cell.fill((0, 0, 0, 0))                               # transparent background: keeps the PNG's see-through parts
    for ddx in (0, -S):                                   # the picture may wrap around the tile edge
        for ddy in (0, -S):
            cell.blit(img, (ox + ddx, oy + ddy))
    return Tex("decal_" + tex.name, cell), T


CUBE_FACES = ("px", "nx", "py", "ny", "pz", "nz")
CUBE_ALIASES = (("px", "posx", "right", "rt", "east"), ("nx", "negx", "left", "lf", "west"),
                ("py", "posy", "up", "top", "bottom_"), ("ny", "negy", "down", "dn", "bottom"),
                ("pz", "posz", "front", "ft"), ("nz", "negz", "back", "bk"))


def face_index(path):
    """File name -> cubemap face 0..5 (px nx py ny pz nz) or None."""
    stem = Path(str(path)).stem.lower()
    toks = [(a, i) for i, names in enumerate(CUBE_ALIASES) for a in names if a != "bottom_"]
    toks.sort(key=lambda t: -len(t[0]))
    for a, i in toks:                                   # whole word: "sky_px", "px-1", "px"
        if re.search(r"(?:^|[^a-z0-9])" + a + r"(?:$|[^a-z0-9])", stem):
            return i
    for a, i in toks:                                   # glued on the end: "skyboxpz", "cloudsft"
        if len(a) >= 2 and stem.endswith(a):
            return i
    return None


def split_cube_image(surf):
    """ONE image that holds all 6 faces -> 6 Surfaces in the order px nx py ny pz nz, or None.
    Understands: horizontal cross (4 wide x 3 high), vertical cross (3 x 4), and a strip of 6 (6 x 1 or 1 x 6)."""
    w, h = surf.get_size()

    def cut(col, row, tw, th, turn=0):
        sub = surf.subsurface((col * tw, row * th, tw, th)).copy()
        if turn:
            sub = pygame.transform.rotate(sub, turn)
        return sub
    if w * 3 == h * 4 and w >= 12:                      # horizontal cross:   .  +Y .  .
        t = w // 4                                      #                    -X +Z +X -Z
        pos = ((2, 1), (0, 1), (1, 0), (1, 2), (1, 1), (3, 1))   #            .  -Y .  .
        return [cut(c, r, t, t) for c, r in pos]
    if w * 4 == h * 3 and w >= 9:                       # vertical cross: +Y / -X +Z +X / -Y / -Z (turned)
        t = w // 3
        faces = [cut(2, 1, t, t), cut(0, 1, t, t), cut(1, 0, t, t), cut(1, 2, t, t), cut(1, 1, t, t), cut(1, 3, t, t, 180)]
        return faces
    if w == h * 6:                                      # strip: px nx py ny pz nz
        return [cut(i, 0, h, h) for i in range(6)]
    if h == w * 6:
        return [cut(0, i, w, w) for i in range(6)]
    return None


def make_gl_cube(faces, s):
    """6 Tex -> GL cube map texture (all faces s x s)."""
    tid = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, tid)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
    for i, t in enumerate(faces):
        sf = t.surf if t.surf.get_size() == (s, s) else pygame.transform.smoothscale(t.surf, (s, s))
        gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + i, 0, gl.GL_RGBA, s, s, 0, gl.GL_RGBA,
                        gl.GL_UNSIGNED_BYTE, surf_bytes(sf))
    for pn, v in ((gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR), (gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                  (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE), (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE),
                  (gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)):
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, pn, v)
    return tid


def cube_size(faces):
    return int(min(1024, max(max(t.surf.get_size()) for t in faces)))


def cube_to_equirect(faces, W=2048, H=1024):
    """6 cube faces -> one 2:1 panorama Surface (the sky the game already understands).
    World (x, y, z-up) is looked up in the cube as GL direction (x, z, -y)."""
    s = cube_size(faces)
    arrs = []
    for t in faces:
        sf = t.surf if t.surf.get_size() == (s, s) else pygame.transform.smoothscale(t.surf, (s, s))
        arrs.append(pygame.surfarray.array3d(sf).astype(np.float32))      # [x, y, rgb]
    lon = 2 * np.pi * (1 - (np.arange(W) + 0.5) / W)
    lat = np.pi / 2 - np.pi * (np.arange(H) + 0.5) / H
    LON, LAT = np.meshgrid(lon, lat)
    wx, wy, wz = np.cos(LAT) * np.cos(LON), np.cos(LAT) * np.sin(LON), np.sin(LAT)
    gx, gy, gz = wx, wz, -wy
    ax, ay, az = np.abs(gx), np.abs(gy), np.abs(gz)
    face = np.where((ax >= ay) & (ax >= az), np.where(gx > 0, 0, 1),
                    np.where(ay >= az, np.where(gy > 0, 2, 3), np.where(gz > 0, 4, 5)))
    maps = ((-gz, -gy, ax), (gz, -gy, ax), (gx, gz, ay), (gx, -gz, ay), (gx, -gy, az), (-gx, -gy, az))
    out = np.zeros((H, W, 3), np.float32)
    for i, (sc, tc, ma) in enumerate(maps):
        m = face == i
        if not m.any():
            continue
        fx = np.clip((sc[m] / ma[m] + 1) * 0.5 * (s - 1), 0, s - 1)
        fy = np.clip((tc[m] / ma[m] + 1) * 0.5 * (s - 1), 0, s - 1)
        x0, y0 = np.floor(fx).astype(int), np.floor(fy).astype(int)
        x1, y1 = np.minimum(x0 + 1, s - 1), np.minimum(y0 + 1, s - 1)
        ux, uy = (fx - x0)[:, None], (fy - y0)[:, None]
        a = arrs[i]
        out[m] = (a[x0, y0] * (1 - ux) * (1 - uy) + a[x1, y0] * ux * (1 - uy)
                  + a[x0, y1] * (1 - ux) * uy + a[x1, y1] * ux * uy)
    surf = pygame.surfarray.make_surface(np.ascontiguousarray(out.transpose(1, 0, 2)).astype(np.uint8))
    try:
        return surf.convert_alpha()
    except Exception:
        return surf


# --------------------------------------------------------------------------
# Source engine maps (.gma / .bsp)
#   The map is baked into the .fadmap as  "bsp": a triangle mesh (what you see)
#   + convex brushes (what you collide with).  Every brush, every face and every
#   displacement of the map ends up in the game - not just boxes.
# --------------------------------------------------------------------------
CONT_SOLID, CONT_WINDOW, CONT_GRATE, CONT_MOVEABLE, CONT_PLAYERCLIP = 0x1, 0x2, 0x8, 0x4000, 0x10000
SURF_NODRAW_MASK = 0x2 | 0x4 | 0x40 | 0x80 | 0x100 | 0x200   # sky2d sky trigger nodraw hint skip
BSP_PLAYER_MASK = CONT_SOLID | CONT_WINDOW | CONT_GRATE | CONT_MOVEABLE | CONT_PLAYERCLIP
BSP_SHOT_MASK = CONT_SOLID | CONT_WINDOW | CONT_MOVEABLE
BSP_SOLID_ENTS = {"func_brush", "func_wall", "func_detail", "func_breakable", "func_wall_toggle", "func_lod"}
BSP_VISUAL_ENTS = {"func_illusionary"}
BSP_TEX_MAX = 512                        # biggest texture side stored in the map
_COMBOS = {}


def _cstr(f):
    b = bytearray()
    while True:
        c = f.read(1)
        if not c or c == b"\0":
            return bytes(b).decode("utf-8", "replace")
        b += c


BIN_MAX_BYTES = 1 << 30                  # a .bin may unpack to at most 1 GiB (stops a tiny file that expands without end)


def bin_unwrap(path):
    """Contents of a Steam Workshop legacy .bin (or any LZMA-compressed file) -> bytes.
    Old Workshop downloads are a .gma compressed with LZMA ("LZMA alone" format, no GMAD header on disk).
    A file that is not compressed (already GMAD / VBSP / GoldSrc) is returned as it is."""
    raw = Path(path).read_bytes()
    if raw[:4] in (b"GMAD", b"VBSP"):
        return raw
    try:
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
        out = dec.decompress(raw, max_length=BIN_MAX_BYTES)   # a stream without an end marker just gives what it has
        if len(out) >= BIN_MAX_BYTES and not dec.eof:
            raise ValueError("this .bin unpacks to more than %d MB: not a map addon" % (BIN_MAX_BYTES >> 20))
    except lzma.LZMAError:
        out = b""
    if not out:
        if len(raw) >= 4 and struct.unpack_from("<i", raw, 0)[0] in (29, 30):
            return raw                       # a bare GoldSrc .bsp
        raise ValueError("this .bin is not an LZMA-compressed Workshop addon (.gma) or a map (.bsp)")
    return out


def gma_read_bsp(path, blob=None):
    """The first .bsp inside a Garry's Mod addon (.gma) -> (file name, bytes).
    blob = the already-read (and, for a legacy .bin, already decompressed) addon bytes."""
    if blob is None:
        blob = bin_unwrap(path) if str(path).lower().endswith(".bin") else None
    damaged = "this addon is damaged or incomplete (was the download cut short?)"
    with (io.BytesIO(blob) if blob is not None else open(path, "rb")) as f:
        if f.read(4) != b"GMAD":
            raise ValueError("not a .gma file (or it is compressed: open it with gmpublisher first)")
        try:
            ver = f.read(1)[0]
            f.read(16)                               # steam id + time stamp
            if ver > 1:
                while _cstr(f):                      # required content
                    pass
            _cstr(f), _cstr(f), _cstr(f)             # title, description, author
            f.read(4)
            files = []
            while True:
                num = struct.unpack("<I", f.read(4))[0]
                if num == 0:
                    break
                nm = _cstr(f)
                size = struct.unpack("<q", f.read(8))[0]
                f.read(4)
                if size < 0:
                    raise ValueError(damaged)
                files.append((nm, size))
            for nm, size in files:
                if nm.lower().endswith(".bsp"):
                    data = f.read(size)
                    if len(data) != size:            # the file ends inside the map
                        raise ValueError(damaged)
                    return nm, data
                f.seek(size, 1)
        except (struct.error, IndexError):
            raise ValueError(damaged)
    raise ValueError("there is no map (.bsp) inside this .gma")


class _Vpk:
    """Small Valve .vpk reader (only to find stock textures: hl2 / css / csgo / garrysmod)."""

    def __init__(self, path):
        self.path, self.files, self.fh = str(path), {}, {}
        with open(self.path, "rb") as f:
            sig, ver, tree = struct.unpack("<III", f.read(12))
            if sig != 0x55AA1234 or ver not in (1, 2):
                raise ValueError("not a VPK")
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

    def read(self, name):
        arch, off, ln, pre = self.files[name]
        if arch == 0x7FFF:
            fn, base = self.path, self.base
        else:
            fn, base = re.sub(r"_dir\.vpk$", "_%03d.vpk" % arch, self.path), 0
        if fn not in self.fh:
            self.fh[fn] = open(fn, "rb")
        h = self.fh[fn]
        h.seek(base + off)
        return pre + h.read(ln)


def vpk_read_bsp(path):
    """The map (.bsp) inside a Valve addon package (.vpk, e.g. an L4D2 / Portal 2 / CS:GO addon) -> (name, bytes, the _Vpk).
    A package with several maps gives the biggest one. A split package is opened through its _dir.vpk."""
    path = Path(path)
    m = re.match(r"(.+)_\d{3}\.vpk$", path.name, re.I)
    if m and (path.parent / (m.group(1) + "_dir.vpk")).is_file():
        path = path.parent / (m.group(1) + "_dir.vpk")
    try:
        v = _Vpk(path)
    except Exception:
        raise ValueError("not a Source 1 .vpk package (VPK v1 / v2)")
    maps = [n for n in v.files if n.endswith(".bsp")]
    if not maps:
        raise ValueError("there is no map (.bsp) inside this .vpk")
    best = max(maps, key=lambda n: (n.startswith("maps/"), v.files[n][2]))
    return Path(best).name, v.read(best), v


def _steam_commons():
    """Every 'steamapps/common' folder of this PC (all Steam libraries, any drive)."""
    homes = []
    home = Path.home()
    for dr in "CDEFGH":
        for s in ("Program Files (x86)/Steam", "Program Files/Steam", "Steam", "Games/Steam"):
            homes.append(Path("%s:/%s" % (dr, s)))
    homes += [home / ".steam/steam", home / ".local/share/Steam", home / "Library/Application Support/Steam",
              home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    try:
        import winreg
        for hive, sub, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    homes.append(Path(winreg.QueryValueEx(k, val)[0]))
            except OSError:
                pass
    except ImportError:
        pass
    out = []

    def add(p):
        if p not in out:
            out.append(p)
    for h in list(homes):
        add(h / "steamapps/common")
        try:
            txt = (h / "steamapps/libraryfolders.vdf").read_text("utf-8", "replace")
        except OSError:
            continue
        for m in re.findall(r'"path"\s+"([^"]+)"', txt):
            add(Path(m.replace("\\\\", "/")) / "steamapps/common")
    for dr in "CDEFGH":
        for s in ("SteamLibrary", "Steam Library", "Games/SteamLibrary"):
            add(Path("%s:/%s/steamapps/common" % (dr, s)))
    return out


def _find_vpks():
    """Valve game packages of this PC that probably hold the stock textures and models a map uses."""
    names = ("Counter-Strike Global Offensive/csgo/pak01_dir.vpk",
             "Counter-Strike Source/cstrike/cstrike_pak_dir.vpk",
             "Counter-Strike Source/hl2/hl2_textures_dir.vpk", "Counter-Strike Source/hl2/hl2_misc_dir.vpk",
             "Half-Life 2/hl2/hl2_textures_dir.vpk", "Half-Life 2/hl2/hl2_misc_dir.vpk",
             "GarrysMod/garrysmod/garrysmod_dir.vpk", "GarrysMod/sourceengine/hl2_textures_dir.vpk",
             "GarrysMod/sourceengine/hl2_misc_dir.vpk",
             "Half-Life 2 Deathmatch/hl2mp/hl2mp_pak_dir.vpk", "Day of Defeat Source/dod/dod_pak_dir.vpk",
             "Left 4 Dead 2/left4dead2/pak01_dir.vpk", "Portal 2/portal2/pak01_dir.vpk",
             "Team Fortress 2/tf/tf2_textures_dir.vpk", "Team Fortress 2/tf/tf2_misc_dir.vpk")
    out, seen = [], set()
    for r in _steam_commons():
        for n in names:
            p = r / n
            try:
                if p.is_file() and str(p).lower() not in seen:
                    seen.add(str(p).lower())
                    out.append(_Vpk(p))
            except Exception:
                pass
    return out


class _Files:
    """Looks a material file up: map's own pack first, then loose folders, then the Valve packages."""

    def __init__(self, pak, loose, vpks):
        self.pak = {n.lower(): n for n in pak.namelist()} if pak else {}
        self.zip, self.loose, self.vpks = pak, loose, vpks

    def read(self, rel):
        rel = rel.lower().replace("\\", "/")
        if rel in self.pak:
            return self.zip.read(self.pak[rel])
        for d in self.loose:
            p = Path(d) / rel
            if p.is_file():
                return p.read_bytes()
        for v in self.vpks:
            if rel in v.files:
                try:
                    return v.read(rel)
                except Exception:
                    pass
        return None


def _vmt_clean(txt):
    """Material text without // comments (a commented-out $basetexture must not win)."""
    return re.sub(r"//[^\n]*", "", txt)


def _vmt_vals(txt):
    out = {}
    for k, v in re.findall(r'"?(\$[a-z0-9_]+)"?\s+"?([^"\s]+)"?', _vmt_clean(txt).lower()):
        out.setdefault(k, v)
    return out


def _entities(txt):
    ents = []
    for blk in re.findall(r"\{([^{}]*)\}", txt):
        e = {}
        for k, v in re.findall(r'"([^"]*)"\s+"([^"]*)"', blk):
            e.setdefault(k.lower(), v)
        if e:
            ents.append(e)
    return ents


def _nums(s, n=3):
    try:
        v = [float(x) for x in s.split()[:n]]
    except Exception:
        v = []
    return v + [0.0] * (n - len(v))


def _brush_bbox(N, D):
    """Corners of the convex brush (planes N.x <= D) -> (mins, maxs) or None.

    Exact for typical brushes (few sides). For brushes with many bevel planes the
    naive C(k,3) corner search is capped so big Source maps (de_nuke, ...) cannot
    freeze the editor for minutes.
    """
    N = np.asarray(N, np.float64)
    D = np.asarray(D, np.float64).reshape(-1)
    k = len(D)
    if k < 4 or len(N) != k:
        return None

    def from_ix(ix):
        if len(ix) == 0:
            return None
        A = N[ix]
        B = D[ix]
        det = np.linalg.det(A)
        ok = np.abs(det) > 1e-5
        if not ok.any():
            return None
        P = np.linalg.solve(A[ok], B[ok][..., None])[..., 0]
        # keep only points inside (or on) every plane
        P = P[np.all(P @ N.T <= D + 0.15, axis=1)]
        if len(P) < 2:
            return None
        return P.min(0), P.max(0)

    # Drop near-duplicate planes (bevel spam) before the combinatorial search.
    if k > 12:
        keep = []
        for i in range(k):
            n, d = N[i], D[i]
            ln = float(np.linalg.norm(n)) or 1.0
            n1, d1 = n / ln, d / ln
            dup = False
            for j in keep:
                n2 = N[j]
                ln2 = float(np.linalg.norm(n2)) or 1.0
                if abs(float(n1 @ (n2 / ln2)) - 1.0) < 1e-3 and abs(d1 - D[j] / ln2) < 0.5:
                    dup = True
                    break
            if not dup:
                keep.append(i)
        if len(keep) >= 4 and len(keep) < k:
            N, D, k = N[keep], D[keep], len(keep)

    MAX_PLANES = 18          # C(18,3) = 816 — still exact and fast
    MAX_TRIPLES = 1200

    if k <= MAX_PLANES:
        ix = _COMBOS.get(k)
        if ix is None:
            ix = _COMBOS[k] = np.array(list(itertools.combinations(range(k), 3)), np.int32)
        return from_ix(ix)

    # Too many planes: sample a bounded set of triples (prefer diverse indices)
    # plus a cheap axis-aligned support estimate as a safety net.
    rng = np.random.RandomState(k * 997 + int(abs(D.sum()) * 10) % 997)
    # structured: consecutive triples + random
    trips = [(i, (i + 1) % k, (i + 2) % k) for i in range(k)]
    trips += [(i, (i + 1) % k, (i + k // 3) % k) for i in range(k)]
    need = max(0, MAX_TRIPLES - len(trips))
    if need:
        extra = rng.randint(0, k, size=(need, 3))
        # force distinct indices inside each triple
        for row in extra:
            if row[0] == row[1]:
                row[1] = (row[1] + 1) % k
            if row[2] == row[0] or row[2] == row[1]:
                row[2] = (row[2] + 2) % k
        trips.extend(map(tuple, extra.tolist()))
    ix = np.array(trips[:MAX_TRIPLES], np.int32)
    hit = from_ix(ix)
    if hit is not None:
        return hit

    # Fallback: axis-aligned bounds from plane inequalities alone
    # (loose but never freezes; still usable for broad-phase collision)
    lo = np.full(3, -1e6, np.float64)
    hi = np.full(3, 1e6, np.float64)
    for i in range(k):
        n, d = N[i], D[i]
        for ax in range(3):
            if abs(n[ax]) < 1e-6:
                continue
            # n[ax]*x_ax <= d - n_rest·x_rest  (worst-case rest = 0 is wrong;
            # use a soft bound assuming |x| not huge)
            v = d / n[ax]
            if n[ax] > 0:
                hi[ax] = min(hi[ax], v + 512.0)   # slight slack
            else:
                lo[ax] = max(lo[ax], v - 512.0)
    if not np.all(lo < hi - 1.0):
        return None
    # clamp absurd sizes so one bad brush cannot dominate the map bounds
    span = hi - lo
    for ax in range(3):
        if span[ax] > 20000:
            mid = 0.5 * (lo[ax] + hi[ax])
            lo[ax], hi[ax] = mid - 10000, mid + 10000
    return lo, hi


def _pack(a):
    return base64.b64encode(zlib.compress(np.ascontiguousarray(a).tobytes(), 6)).decode("ascii")


def _unpack(s, dtype, shape):
    return np.frombuffer(zlib.decompress(base64.b64decode(s)), dtype).reshape(shape)


def _img_hint(raw):
    return "x.jpg" if raw[:2] == b"\xff\xd8" else "x.png"


def _encode_rgba(rgba, opaque):
    """RGBA array -> base64 JPEG (opaque pictures, small) or PNG."""
    h, w = rgba.shape[:2]
    if opaque:
        s = pygame.image.frombuffer(np.ascontiguousarray(rgba[:, :, :3]).tobytes(), (w, h), "RGB")
        try:
            bio = io.BytesIO()
            pygame.image.save(s, bio, "x.jpg")
            if bio.getvalue()[:2] == b"\xff\xd8":
                return base64.b64encode(bio.getvalue()).decode("ascii")
        except Exception:
            pass
    s = pygame.image.frombuffer(np.ascontiguousarray(rgba).tobytes(), (w, h), "RGBA")
    bio = io.BytesIO()
    pygame.image.save(s, bio, "x.png")
    return base64.b64encode(bio.getvalue()).decode("ascii")


class BspWorld:
    """Baked Source map: triangle mesh + convex collision brushes (same format the game loads)."""

    def __init__(self):
        self.name = "map"
        self.pos = np.zeros((0, 3), np.float32)
        self.uv = np.zeros((0, 2), np.float32)
        self.col = np.zeros((0, 4), np.uint8)
        self.idx = np.zeros(0, np.uint32)
        self.batches = []                    # [texture, mode (0 solid, 1 cut-out, 2 see-through), first index, index count]
        self.tex = []                        # {"name", "w", "h", "rgba", "opaque"}
        self.planes = np.zeros((0, 4), np.float32)
        self.bfirst = np.zeros(0, np.int32)
        self.bcnt = np.zeros(0, np.int32)
        self.bflags = np.zeros(0, np.uint8)  # 1 blocks players, 2 stops bullets
        self.bbox = np.zeros((0, 6), np.float32)
        self.gids = None
        self.list = None
        self.nray = None                     # picking / floor search only look at the first nray indices (the map, not the props)
        self._tri = None
        self.ladders = []                    # [(bounds, d)] ladder zones found by the importer
        self._enc = None
        self.bounds = [0, 0, 0, 0, 0, 0]

    # ---- bounds / counts ----
    def calc_bounds(self):
        lo, hi = [], []
        if len(self.pos):
            lo.append(self.pos.min(0))
            hi.append(self.pos.max(0))
        if len(self.bbox):
            lo.append(self.bbox[:, :3].min(0))
            hi.append(self.bbox[:, 3:].max(0))
        if lo:
            self.bounds = [float(v) for v in np.min(lo, 0)] + [float(v) for v in np.max(hi, 0)]

    # ---- save / load ----
    def to_dict(self):
        if getattr(self, "_enc", None) is not None:
            return self._enc
        tx = []
        for t in self.tex:
            if t["rgba"] is None:
                tx.append({"name": t["name"]})
            else:
                tx.append({"name": t["name"], "w": t["w"], "h": t["h"], "img": _encode_rgba(t["rgba"], t["opaque"]),
                           "opaque": 1 if t["opaque"] else 0})
        self._enc = {"name": self.name, "n": int(len(self.pos)), "pos": _pack(self.pos), "uv": _pack(self.uv),
                "col": _pack(self.col), "idx": _pack(self.idx), "batches": [list(map(int, b)) for b in self.batches],
                "tex": tx, "nb": int(len(self.bfirst)), "np": int(len(self.planes)), "planes": _pack(self.planes),
                "first": _pack(self.bfirst), "cnt": _pack(self.bcnt), "flags": _pack(self.bflags),
                "bbox": _pack(self.bbox), "bounds": [round(v, 1) for v in self.bounds]}
        if self.nray is not None:
            self._enc["nray"] = int(self.nray)
        return self._enc

    @staticmethod
    def from_dict(d):
        b = BspWorld()
        b.name = d.get("name", "map")
        n, nb, npl = int(d["n"]), int(d["nb"]), int(d["np"])
        b.pos = _unpack(d["pos"], np.float32, (n, 3)).copy()
        b.uv = _unpack(d["uv"], np.float32, (n, 2)).copy()
        b.col = _unpack(d["col"], np.uint8, (n, 4)).copy()
        b.idx = _unpack(d["idx"], np.uint32, (-1,)).copy()
        b.batches = [list(x) for x in d.get("batches", [])]
        for t in d.get("tex", []):
            rgba = None
            if t.get("img"):
                try:
                    raw = base64.b64decode(t["img"])
                    s = pygame.image.load(io.BytesIO(raw), _img_hint(raw)).convert_alpha()
                    w, h = s.get_size()
                    rgba = np.frombuffer(pygame.image.tostring(s, "RGBA"), np.uint8).reshape(h, w, 4).copy()
                except Exception:
                    rgba = None
            b.tex.append({"name": t.get("name", "tex"), "w": int(t.get("w", 0)), "h": int(t.get("h", 0)),
                          "rgba": rgba, "opaque": bool(t.get("opaque", 1))})
        b.planes = _unpack(d["planes"], np.float32, (npl, 4)).copy()
        b.bfirst = _unpack(d["first"], np.int32, (nb,)).copy()
        b.bcnt = _unpack(d["cnt"], np.int32, (nb,)).copy()
        b.bflags = _unpack(d["flags"], np.uint8, (nb,)).copy()
        b.bbox = _unpack(d["bbox"], np.float32, (nb, 6)).copy()
        b.bounds = [float(v) for v in d.get("bounds", [0] * 6)]
        if d.get("nray") is not None:
            b.nray = int(d["nray"])
        return b

    # ---- drawing ----
    def free_gl(self):
        if self.gids:
            try:
                gl.glDeleteTextures([g for g in self.gids if g])
            except Exception:
                pass
        if self.list is not None:
            try:
                gl.glDeleteLists(self.list, 1)
            except Exception:
                pass
        self.gids, self.list = None, None

    def _build_gl(self, white):
        self.gids = []
        for t in self.tex:
            r = t["rgba"]
            self.gids.append(make_gl_tex(np.ascontiguousarray(r).tobytes(), r.shape[1], r.shape[0], True) if r is not None else 0)
        self.list = gl.glGenLists(1)
        gl.glNewList(self.list, gl.GL_COMPILE)
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glEnableClientState(gl.GL_COLOR_ARRAY)
        gl.glVertexPointer(3, gl.GL_FLOAT, 0, self.pos)
        gl.glTexCoordPointer(2, gl.GL_FLOAT, 0, self.uv)
        gl.glColorPointer(4, gl.GL_UNSIGNED_BYTE, 0, self.col)
        for mode in (0, 1, 2):
            if mode == 1:
                gl.glEnable(gl.GL_ALPHA_TEST)
                gl.glAlphaFunc(gl.GL_GREATER, 0.5)
            elif mode == 2:
                gl.glDisable(gl.GL_ALPHA_TEST)
                gl.glEnable(gl.GL_BLEND)
                gl.glDepthMask(gl.GL_FALSE)
            for ti, md, first, cnt in self.batches:
                if md != mode:
                    continue
                g = self.gids[ti] if 0 <= ti < len(self.gids) else 0
                gl.glBindTexture(gl.GL_TEXTURE_2D, g or white)
                gl.glDrawElements(gl.GL_TRIANGLES, cnt, gl.GL_UNSIGNED_INT, self.idx[first:first + cnt])
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_ALPHA_TEST)
        gl.glDisableClientState(gl.GL_COLOR_ARRAY)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEndList()

    def draw(self, white):
        if self.list is None:
            self._build_gl(white)
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glCallList(self.list)
        gl.glColor4f(1, 1, 1, 1)

    # ---- picking (mouse ray vs the mesh) ----
    def ray(self, o, d, maxt=60000.0):
        """Nearest hit of the ray on the drawn mesh -> (t, normal) or None."""
        key = (round(float(o[0]), 2), round(float(o[1]), 2), round(float(o[2]), 2), round(float(d[0]), 5),
               round(float(d[1]), 5), round(float(d[2]), 5))
        if getattr(self, "_rk", None) == key:
            return self._rv
        self._rk, self._rv = key, self._ray(o, d, maxt)
        return self._rv

    def _ray(self, o, d, maxt):
        if self._tri is None:
            lim = len(self.idx) if self.nray is None else self.nray
            keep = [(f, c) for ti, md, f, c in self.batches if md != 2 and f < lim]
            ids = np.concatenate([self.idx[f:f + c] for f, c in keep]) if keep else np.zeros(0, np.uint32)
            T = self.pos[ids.reshape(-1, 3)].astype(np.float64)
            self._tri = (T[:, 0], T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        if not len(self._tri[0]):
            return None
        a, e1, e2 = self._tri
        o = np.asarray(o, np.float64)
        d = np.asarray(d, np.float64)
        p = np.cross(d, e2)
        det = np.einsum("ij,ij->i", e1, p)
        ok = np.abs(det) > 1e-9
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        s = o - a
        u = np.einsum("ij,ij->i", s, p) * inv
        q = np.cross(s, e1)
        v = (q @ d) * inv
        t = np.einsum("ij,ij->i", e2, q) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0.01) & (t < maxt)
        if not hit.any():
            return None
        i = np.where(hit)[0][np.argmin(t[hit])]
        n = np.cross(e1[i], e2[i])
        n /= (np.linalg.norm(n) or 1.0)
        if n @ d > 0:
            n = -n
        if n[2] > 0.985:
            return float(t[i]), (0, 0, 1)                    # flat floor: same as a box top
        return float(t[i]), (float(n[0]), float(n[1]), float(n[2]))

    # ---- collision (same test the game does) ----
    def hull_hits(self, x, y, zc, mask=1):
        """True when the player hull (32 x 32 x 72, centre x y zc) overlaps a brush."""
        b = self.bbox
        m = ((b[:, 0] - HULL_R < x) & (x < b[:, 3] + HULL_R) & (b[:, 1] - HULL_R < y) & (y < b[:, 4] + HULL_R)
             & (b[:, 2] - HULL_H / 2 < zc) & (zc < b[:, 5] + HULL_H / 2) & ((self.bflags & mask) != 0))
        for i in np.where(m)[0]:
            pl = self.planes[self.bfirst[i]:self.bfirst[i] + self.bcnt[i]]
            de = pl[:, 3] + HULL_R * np.abs(pl[:, 0]) + HULL_R * np.abs(pl[:, 1]) + HULL_H / 2 * np.abs(pl[:, 2])
            if np.all(pl[:, 0] * x + pl[:, 1] * y + pl[:, 2] * zc < de):
                return True
        return False

    def floor_below(self, x, y, z, up=40.0, down=400.0):
        """Feet height of the floor under (x, y) searching from z + up down to z - down, or None."""
        zc = z + up + HULL_H / 2
        lo = z - down + HULL_H / 2
        step = 1.0
        if self.hull_hits(x, y, zc):
            return None
        while zc > lo:
            if self.hull_hits(x, y, zc - step):
                return zc - HULL_H / 2
            zc -= step
        return None

    def floor_spots(self, limit=4000):
        """Flat floor places a player could stand on: [(x, y, feet z)]."""
        if not len(self.idx):
            return []
        lim = len(self.idx) if self.nray is None else self.nray
        T = self.pos[self.idx[:lim].reshape(-1, 3)].astype(np.float64)
        n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        ar = np.linalg.norm(n, axis=1)
        flat = (ar > 2000) & (np.abs(n[:, 2]) > 0.97 * ar)
        c = T[flat].mean(1)
        rng = np.random.RandomState(1)
        sel = rng.permutation(len(c))[:limit * 3]
        out = []
        for i in sel:
            x, y, z = c[i]
            fz = self.floor_below(x, y, z + 1.0, 4.0, 12.0)
            if fz is not None and not self.hull_hits(x, y, fz + HULL_H / 2 + 0.5) and self.hull_hits(x, y, fz + HULL_H / 2 - 4):
                out.append((float(x), float(y), float(fz)))
                if len(out) >= limit:
                    break
        return out


# ---- Source models (props): .mdl + .vvd + .dx90.vtx -> triangles ----
def _mdl_cstr(b, o):
    e = b.find(b"\0", o)
    return b[o:e if e >= 0 else len(b)].decode("utf-8", "replace")


def load_model(files, path, max_tris=1 << 30):
    """Source model -> dict (vertices, triangles per material slot, material names) or None when a file is missing.
    Body part 0 of every group; the most detailed level of detail that stays under max_tris triangles."""
    base = path[:-4] if path.lower().endswith(".mdl") else path
    mdl = files.read(base + ".mdl")
    vvd = files.read(base + ".vvd")
    vtx = files.read(base + ".dx90.vtx") or files.read(base + ".dx80.vtx") or files.read(base + ".sw.vtx")
    if not mdl or not vvd or not vtx or mdl[:4] != b"IDST" or vvd[:4] != b"IDSV":
        return None
    i4 = lambda b, o: struct.unpack_from("<i", b, o)[0]
    ntex, texi, ncd, cdi, nskinref, nskinfam, skini, nbody, bodyi = struct.unpack_from("<9i", mdl, 204)
    texnames = [_mdl_cstr(mdl, texi + k * 64 + i4(mdl, texi + k * 64)) for k in range(ntex)]
    cds = []
    for k in range(ncd):
        s = _mdl_cstr(mdl, i4(mdl, cdi + k * 4)).replace("\\", "/").lstrip("/")
        cds.append(s + ("/" if s and not s.endswith("/") else ""))
    skins = []
    for f in range(max(1, nskinfam)):
        skins.append([struct.unpack_from("<h", mdl, skini + (f * nskinref + r) * 2)[0] for r in range(nskinref)])
    # ---- vertices (the fix-up table puts them in the order each level of detail uses) ----
    nfix, fixi, vdat = i4(vvd, 48), i4(vvd, 52), i4(vvd, 56)
    VT = np.dtype([("w", "<f4", 3), ("b", "u1", 4), ("p", "<f4", 3), ("n", "<f4", 3), ("t", "<f4", 2)])
    allv = np.frombuffer(vvd, VT, (len(vvd) - vdat) // 48, vdat)
    fx = np.frombuffer(vvd, "<i4", nfix * 3, fixi).reshape(-1, 3) if nfix else None

    def verts_of(lod):
        if fx is None:
            return allv
        parts = [allv[s:s + n] for fl, s, n in fx if fl >= lod]
        return np.concatenate(parts) if parts else allv

    nlods = max(1, i4(vtx, 20))
    vbp, nbp = i4(vtx, 32), i4(vtx, 28)

    def tris_of(lod, nvert):
        out = []
        for bi in range(min(nbody, nbp)):
            bpo = bodyi + bi * 16
            nmod, modi = i4(mdl, bpo + 4), i4(mdl, bpo + 12)
            if nmod < 1:
                continue
            mo = bpo + modi                                        # model 0 of this body part
            nmesh, meshi, vtxstart = i4(mdl, mo + 72), i4(mdl, mo + 76), i4(mdl, mo + 84)
            vb = vbp + bi * 8
            vmod = vb + i4(vtx, vb + 4)                            # vtx model 0
            if lod >= i4(vtx, vmod):
                continue
            vlod = vmod + i4(vtx, vmod + 4) + lod * 12
            vnm, vmo = i4(vtx, vlod), vlod + i4(vtx, vlod + 4)
            for mi in range(min(nmesh, vnm)):
                mm = mo + meshi + mi * 116
                matidx, voff = i4(mdl, mm), i4(mdl, mm + 12)
                vm = vmo + mi * 9
                nsg, sgo = i4(vtx, vm), vm + i4(vtx, vm + 4)
                if nsg < 1:
                    continue
                sgsz = max(25, i4(vtx, sgo + 4) // nsg)           # size of one strip group header
                tris, first = [], vtxstart // 48 + voff
                for g in range(nsg):
                    sg = sgo + g * sgsz
                    nvx, vxo, nix, ixo = i4(vtx, sg), i4(vtx, sg + 4), i4(vtx, sg + 8), i4(vtx, sg + 12)
                    if vtx[sg + 24] & 8 or nix < 3:
                        continue
                    orig = np.frombuffer(vtx, np.dtype([("a", "u1", 4), ("o", "<u2"), ("b", "u1", 3)]), nvx, sg + vxo)["o"].astype(np.int64)
                    ix = np.frombuffer(vtx, "<u2", nix - nix % 3, sg + ixo).astype(np.int64)
                    tris.append(first + orig[ix])
                if not tris:
                    continue
                tr = np.concatenate(tris).reshape(-1, 3)
                tr = tr[(tr < nvert).all(1)]
                if len(tr):
                    out.append((matidx, tr))
        return out
    best = None
    for lod in range(nlods):
        V = verts_of(lod)
        out = tris_of(lod, len(V))
        if not out:
            continue
        n = sum(len(t) for _, t in out)
        if best is None or n <= max_tris or n < best[0]:
            if best is None or n <= max_tris or best[0] > max_tris:
                best = (n, V, out)
        if n <= max_tris:
            break
    if best is None:
        return None
    _n, V, out = best
    P = V["p"].astype(np.float64)
    return {"P": P, "N": V["n"].astype(np.float64), "T": V["t"].astype(np.float64), "parts": out, "texnames": texnames,
            "cds": cds, "skins": skins, "bmin": P.min(0), "bmax": P.max(0)}


def angle_matrix(pitch, yaw, roll):
    """Source QAngle (degrees) -> 3x3 matrix (model space -> world)."""
    sp, cp = math.sin(math.radians(pitch)), math.cos(math.radians(pitch))
    sy, cy = math.sin(math.radians(yaw)), math.cos(math.radians(yaw))
    sr, cr = math.sin(math.radians(roll)), math.cos(math.radians(roll))
    return np.array([[cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
                     [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
                     [-sp, sr * cp, cr * cp]], np.float64)


PROP_ENTS = {"prop_dynamic", "prop_dynamic_override", "prop_physics", "prop_physics_multiplayer", "prop_physics_override",
             "prop_static", "prop_door_rotating", "prop_detail"}
PROP_COLLISION = True              # solid props block players / bullets (a box around the model)
PROP_TRI_BUDGET = 80000            # a model used N times gets budget / N triangles per copy (a lower level of detail is used if needed)
PROP_MIN_TRIS, PROP_MAX_TRIS = 800, 6000



def _bsp_decompress_lump(blob, uncompressed_size=0):
    """Decompress a Source-engine LZMA lump (fourCC != 0 in the lump directory).

    Header: 'LZMA' + uint32 actual_size + uint32 lzma_size + 5 prop bytes + payload.
    uncompressed_size is the fourCC field from the lump directory (preferred).
    """
    if not blob or blob[:4] != b"LZMA" or len(blob) < 17:
        return blob
    actual, lzma_size = struct.unpack_from("<II", blob, 4)
    props = blob[12:17]
    payload = blob[17:]
    if lzma_size and lzma_size <= len(payload):
        payload = payload[:lzma_size]
    b0 = props[0]
    lc = b0 % 9
    b0 //= 9
    lp = b0 % 5
    pb = b0 // 5
    dict_size = struct.unpack_from("<I", props, 1)[0]
    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}]
    target = uncompressed_size or actual or None
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    try:
        out = dec.decompress(payload, max_length=target) if target else dec.decompress(payload)
    except Exception:
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
        out = dec.decompress(payload)
    if target and len(out) > target:
        out = out[:target]
    return out




def _goldsrc_decode_miptex(blob, off):
    """Decode one GoldSrc miptex at offset into (name, w, h, rgba_u8_or_None)."""
    if off < 0 or off + 40 > len(blob):
        return None
    name = blob[off:off + 16].split(b"\0")[0].decode("latin1", "replace")
    w, h = struct.unpack_from("<ii", blob, off + 16)
    if w <= 0 or h <= 0 or w > 4096 or h > 4096:
        return name, 1, 1, None
    offsets = struct.unpack_from("<4i", blob, off + 24)
    # mip level 0
    m0 = offsets[0]
    if m0 <= 0:
        return name, w, h, None
    pix_off = off + m0
    n = w * h
    if pix_off + n > len(blob):
        return name, w, h, None
    # palette sits after mip3 + 2-byte count
    # layout: mip0, mip1, mip2, mip3, short palette_size, 256*3 RGB
    # safer: search palette near end of this texture block - use standard formula
    m3 = offsets[3]
    if m3 > 0:
        pal_off = off + m3 + (max(1, w >> 3) * max(1, h >> 3)) + 2
    else:
        # no mips: palette right after pixels + 2
        pal_off = pix_off + n + 2
    if pal_off + 768 > len(blob):
        # try alternate: after all mips
        pal_off = pix_off + n + (n // 4) + (n // 16) + (n // 64) + 2
    if pal_off + 768 > len(blob):
        return name, w, h, None
    pal = np.frombuffer(blob, np.uint8, 768, pal_off).reshape(256, 3)
    idx = np.frombuffer(blob, np.uint8, n, pix_off)
    rgb = pal[idx]
    # index 255 is usually transparent in GoldSrc
    a = np.where(idx == 255, 0, 255).astype(np.uint8)
    rgba = np.concatenate([rgb, a[:, None]], 1).reshape(h, w, 4)
    return name, w, h, rgba


LADDER_W, LADDER_T = 32.0, 20.0          # size of the ladder zone made for an imported ladder (along the wall / out from it)
_DIR_OF = {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}


def source_ladders(ents, models, world):
    """The ladders of a Source / GoldSrc map -> [([x0, y0, z0, x1, y1, z1], d)], ready to become ladder zones.
    func_useableladder (point0 / point1 = bottom and top) and func_ladder (a brush) are both found.
    d = the side the player climbs from (0 +x, 1 +y, 2 -x, 3 -y), taken from the info_ladder_dismount points at the foot of the
    ladder, or else from the nearest wall (the wall is behind the ladder)."""
    dis = []
    for e in ents:
        if e.get("classname") == "info_ladder_dismount" and "origin" in e:
            dis.append(_nums(e["origin"]))

    DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))

    def side(x, y, zb, zt, axis=None):
        """Score the 4 sides (only the 2 along `axis` when it is given) and return the best one:
        a wall right behind the ladder and free room in front of it count most; the info_ladder_dismount points at the
        foot (you step on from there) and at the top (you step off the other way) are the tie-breaker."""
        score = [0.0, 0.0, 0.0, 0.0]
        zm = (zb + zt) / 2.0
        for ci, (cx, cy) in enumerate(DIRS):
            if (axis == 0 and cy) or (axis == 1 and cx):
                score[ci] = -99.0
                continue
            try:
                back = world.ray((x, y, zm), (-cx, -cy, 0.0))     # the wall behind the climber
                front = world.ray((x, y, zm), (cx, cy, 0.0))      # and the room in front of the ladder
            except Exception:
                back = front = None
            if back and back[0] <= 28.0:
                score[ci] += 2.0
            if front and front[0] <= 24.0:
                score[ci] -= 2.0
        for (dx, dy, dz) in dis:
            ox, oy = dx - x, dy - y
            dist = math.hypot(ox, oy)
            if not 12.0 <= dist <= 56.0:
                continue
            ci = (0 if ox > 0 else 2) if abs(ox) >= abs(oy) else (1 if oy > 0 else 3)
            off = min(abs(ox), abs(oy))
            if off > 12.0 or score[ci] <= -99.0:
                continue
            w = 1.0 - off / 24.0
            if abs(dz - zb) <= 24.0:
                score[ci] += w                                    # on the climbing side at the foot
            elif abs(dz - zt) <= 24.0 and score[(ci + 2) % 4] > -99.0:
                score[(ci + 2) % 4] += 0.5 * w                    # you leave over the top onto the wall side
        best = max(range(4), key=lambda i: (score[i], i == (0 if axis != 1 else 1)))
        return best

    out = []
    for e in ents:
        cls = e.get("classname", "")
        try:
            if cls == "func_useableladder" and ("point0" in e or "point1" in e or "origin" in e):
                p0 = _nums(e.get("point0", e.get("origin", "0 0 0")))
                p1 = _nums(e.get("point1", e.get("origin", "0 0 0")))
                x, y = (p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0
                zb, zt = min(p0[2], p1[2]), max(p0[2], p1[2])
                if zt - zb < 8.0:
                    continue
                d = side(x, y, zb, zt)
                hw, t = LADDER_W / 2.0, LADDER_T
                if d == 0:
                    b = [x, y - hw, zb, x + t, y + hw, zt]
                elif d == 2:
                    b = [x - t, y - hw, zb, x, y + hw, zt]
                elif d == 1:
                    b = [x - hw, y, zb, x + hw, y + t, zt]
                else:
                    b = [x - hw, y - t, zb, x + hw, y, zt]
                out.append(([round(v, 1) for v in b], d))
            elif cls == "func_ladder" and e.get("model", "").startswith("*"):
                m = models[int(e["model"][1:])]
                org = _nums(e.get("origin", "0 0 0"))
                lo = [float(v) + org[i] for i, v in enumerate(m["mins"])]
                hi = [float(v) + org[i] for i, v in enumerate(m["maxs"])]
                if hi[2] - lo[2] < 8.0:
                    continue
                axis = 0 if (hi[0] - lo[0]) <= (hi[1] - lo[1]) else 1      # a ladder brush is thin: that is the side you climb from
                d = side((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, lo[2], hi[2], axis)
                for a in (0, 1):                                            # never thinner than a player can touch
                    if hi[a] - lo[a] < 8.0:
                        c = (hi[a] + lo[a]) / 2.0
                        lo[a], hi[a] = c - 4.0, c + 4.0
                out.append(([round(v, 1) for v in lo + hi], d))
        except Exception as ex:
            print("ladder skipped:", cls, ex)
    return out


def import_goldsrc_map(data, name, progress=None, loose_dirs=()):
    """Half-Life 1 / CS 1.6 BSP (version 30) -> (BspWorld, spawns, name)."""
    say = progress or (lambda text, frac=None: None)
    if len(data) < 4 + 15 * 8:
        raise ValueError("file too small for a GoldSrc map")
    ver = struct.unpack_from("<i", data, 0)[0]
    if ver not in (29, 30):
        raise ValueError("GoldSrc map version %d not supported (need 29 or 30)" % ver)
    say("Reading GoldSrc map (HL1 / CS 1.6) ...", 0.08)
    L = [struct.unpack_from("<ii", data, 4 + i * 8) for i in range(15)]

    def glump(i):
        o, ln = L[i]
        if o < 0 or ln <= 0 or o + ln > len(data):
            return b""
        return data[o:o + ln]

    ent_txt = glump(0).split(b"\0")[0].decode("latin1", "replace")
    ents = _entities(ent_txt)

    planes = np.frombuffer(glump(1), np.dtype([("n", "<f4", 3), ("d", "<f4"), ("type", "<i4")])).copy()
    verts = np.frombuffer(glump(3), ("<f4", 3)).copy()
    edges = np.frombuffer(glump(12), ("<i2", 2)).copy()
    surfedges = np.frombuffer(glump(13), "<i4").copy()
    faces = np.frombuffer(glump(7), np.dtype([
        ("plane", "<u2"), ("side", "<u2"), ("firstedge", "<i4"), ("numedges", "<i2"),
        ("texinfo", "<i2"), ("styles", "u1", 4), ("lightofs", "<i4")
    ])).copy()
    texinfo = np.frombuffer(glump(6), np.dtype([
        ("v", "<f4", (2, 4)), ("miptex", "<i4"), ("flags", "<i4")
    ])).copy()
    models = np.frombuffer(glump(14), np.dtype([
        ("mins", "<f4", 3), ("maxs", "<f4", 3), ("origin", "<f4", 3),
        ("headnode", "<i4", 4), ("visleafs", "<i4"), ("firstface", "<i4"), ("numfaces", "<i4")
    ])).copy()

    # textures lump
    texblob = glump(2)
    mip_offsets = []
    tex_rgba = []  # list of (name, w, h, rgba|None)
    if len(texblob) >= 4:
        ntex = struct.unpack_from("<i", texblob, 0)[0]
        ntex = max(0, min(ntex, (len(texblob) - 4) // 4))
        offs = [struct.unpack_from("<i", texblob, 4 + i * 4)[0] for i in range(ntex)]
        for off in offs:
            if off < 0:
                tex_rgba.append(("*", 1, 1, None))
                continue
            try:
                rec = _goldsrc_decode_miptex(texblob, off)
            except Exception:
                rec = None
            if rec is None:
                tex_rgba.append(("tex", 1, 1, None))
            else:
                tex_rgba.append(rec)

    say("GoldSrc: %d faces, %d verts, %d textures" % (len(faces), len(verts), len(tex_rgba)), 0.15)

    # Build mesh from world model (model 0) + solid submodels
    P_, T_, C_, A_ = [], [], [], []
    mat_tris = {}
    nv = 0
    mats_order = []  # material key order
    mat_meta = {}    # key -> (name, rgba)

    def shade(n):
        return float(np.clip(0.68 + 0.32 * n[2] + 0.1 * (abs(n[1]) - abs(n[0])), 0.45, 1.0))

    def add_poly(P, UV, n, key):
        nonlocal nv
        k = len(P)
        if k < 3:
            return
        tri = np.concatenate([[0] * (k - 2), np.arange(1, k - 1), np.arange(2, k)]).reshape(3, -1).T
        e = np.cross(P[1] - P[0], P[2] - P[0])
        if e @ n < 0:
            tri = tri[:, ::-1]
        s = shade(n)
        P_.append(P.astype(np.float32))
        T_.append(UV.astype(np.float32))
        C_.append(np.tile(np.array([s, s, s], np.float32), (k, 1)))
        A_.append(np.ones(k, np.float32))
        if key not in mat_tris:
            mat_tris[key] = []
            mats_order.append(key)
        mat_tris[key].append((tri + nv).astype(np.uint32).reshape(-1))
        nv += k

    def face_poly(f):
        ne = int(f["numedges"])
        fe = int(f["firstedge"])
        if ne < 3 or fe < 0 or fe + ne > len(surfedges):
            return np.zeros((0, 3), np.float64)
        se = surfedges[fe:fe + ne]
        ai = np.abs(se).astype(np.int64)
        ok = (ai >= 0) & (ai < len(edges))
        if ok.sum() < 3:
            return np.zeros((0, 3), np.float64)
        se = se[ok]
        ed = edges[ai[ok]]
        vi = np.where(se >= 0, ed[:, 0], ed[:, 1]).astype(np.int64)
        okv = (vi >= 0) & (vi < len(verts))
        vi = vi[okv]
        if len(vi) < 3:
            return np.zeros((0, 3), np.float64)
        return verts[vi].astype(np.float64)

    n_faces = max(1, len(faces))
    # Only model 0 (world) + any models referenced - for GoldSrc, iterate model 0 faces
    face_range = []
    if len(models):
        m0 = models[0]
        f0 = int(m0["firstface"])
        f1 = min(len(faces), f0 + max(0, int(m0["numfaces"])))
        face_range = list(range(max(0, f0), f1))
    else:
        face_range = list(range(len(faces)))

    for ji, fi in enumerate(face_range):
        if ji % 500 == 0:
            say("Building mesh %d/%d ..." % (ji, len(face_range)), 0.2 + 0.5 * ji / max(1, len(face_range)))
            try:
                import pygame
                pygame.event.pump()
            except Exception:
                pass
        f = faces[fi]
        ti = int(f["texinfo"])
        if ti < 0 or ti >= len(texinfo):
            continue
        inf = texinfo[ti]
        if int(inf["flags"]) & 1:  # TEX_SPECIAL (nodraw-ish / sky often)
            # still draw sky-ish as flat if needed; skip pure special tools
            pass
        mip = int(inf["miptex"])
        P = face_poly(f)
        if len(P) < 3:
            continue
        pi = int(f["plane"])
        if not 0 <= pi < len(planes):
            continue
        n = planes[pi]["n"].astype(np.float64)
        if int(f["side"]):
            n = -n
        # UV from texinfo
        v = inf["v"].astype(np.float64)
        w = h = 64
        key = "flat"
        rgba = None
        tname = "flat"
        if 0 <= mip < len(tex_rgba):
            tname, w, h, rgba = tex_rgba[mip]
            w = max(1, int(w)); h = max(1, int(h))
            key = "tex_%d" % mip
            if key not in mat_meta:
                mat_meta[key] = (tname or "tex", rgba)
        # skip AAATRIGGER / CLIP / sky-ish empty
        tn_up = (tname or "").upper()
        if tn_up in ("AAATRIGGER", "CLIP", "SKIP", "HINT", "NULL", "BSPCLIP", "CONTENTSWATER"):
            continue
        if rgba is None and (int(inf["flags"]) & 1):
            continue
        UV = np.stack([
            (P @ v[0, :3] + v[0, 3]) / w,
            (P @ v[1, :3] + v[1, 3]) / h
        ], 1)
        add_poly(P, UV, n, key)

    if not P_:
        raise ValueError("GoldSrc map has no visible faces")

    say("Assembling mesh ...", 0.75)
    pos = np.concatenate(P_)
    uvs = np.concatenate(T_)
    shd = np.concatenate(C_)
    col = np.zeros((len(pos), 4), np.uint8)
    col[:, :3] = np.clip(shd * 255, 0, 255).astype(np.uint8)
    col[:, 3] = 255
    npos = len(pos)
    idx_parts, batches = [], []
    tex_list = []
    for key in mats_order:
        ix = np.concatenate(mat_tris[key]).astype(np.int64)
        ix = ix[(ix >= 0) & (ix < npos)]
        ix = ix[:len(ix) - (len(ix) % 3)]
        if len(ix) < 3:
            continue
        name_t, rgba = mat_meta.get(key, (key, None))
        ti = len(tex_list)
        tex_list.append({
            "name": name_t,
            "w": int(rgba.shape[1]) if rgba is not None else 1,
            "h": int(rgba.shape[0]) if rgba is not None else 1,
            "rgba": rgba.reshape(-1, 4) if rgba is not None else None,
            "opaque": True if rgba is None else bool(np.all(rgba[:, :, 3] == 255)),
        })
        start = sum(len(p) for p in idx_parts)
        idx_parts.append(ix.astype(np.uint32))
        batches.append([ti, 0, start, len(ix)])

    out = BspWorld()
    out.name = name
    out.pos = pos.astype(np.float32)
    out.uv = uvs.astype(np.float32)
    out.col = col
    out.idx = np.concatenate(idx_parts) if idx_parts else np.zeros(0, np.uint32)
    out.batches = batches
    out.tex = tex_list
    out.nray = int(len(out.idx))

    # Simple collision: AABB per world model from model mins/maxs
    PL, BF, BC, BG, BB = [], [], [], [], []
    npl = 0
    if len(models):
        m = models[0]
        mins = m["mins"].astype(np.float64)
        maxs = m["maxs"].astype(np.float64)
        N6 = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]], np.float64)
        D6 = np.array([maxs[0], -mins[0], maxs[1], -mins[1], maxs[2], -mins[2]], np.float64)
        PL.append(np.concatenate([N6, D6[:, None]], 1).astype(np.float32))
        BF.append(0); BC.append(6); BG.append(3)
        BB.append(np.concatenate([mins - 1, maxs + 1]).astype(np.float32))
        npl = 6
    out.planes = np.concatenate(PL) if PL else np.zeros((0, 4), np.float32)
    out.bfirst = np.array(BF, np.int32)
    out.bcnt = np.array(BC, np.int32)
    out.bflags = np.array(BG, np.uint8)
    out.bbox = np.array(BB, np.float32).reshape(-1, 6) if BB else np.zeros((0, 6), np.float32)
    out.calc_bounds()

    # Spawns
    spawns, k = [], 0
    for e in ents:
        cls = e.get("classname", "")
        if "origin" not in e:
            continue
        if cls not in ("info_player_start", "info_player_deathmatch", "info_player_counterterrorist",
                       "info_player_terrorist", "info_player_axis", "info_player_ally",
                       "info_player_teamspawn", "info_player_coop"):
            if not cls.startswith("info_player"):
                continue
        try:
            x, y, z = _nums(e["origin"])
        except Exception:
            continue
        yaw = 0.0
        try:
            yaw = _nums(e.get("angles", "0 0 0"))[1]
        except Exception:
            pass
        if "counterterrorist" in cls or "ally" in cls or cls == "info_player_start":
            team = 0
        elif "terrorist" in cls or "axis" in cls:
            team = 1
        else:
            team, k = k % 2, k + 1
        # GoldSrc Z is up already in our coords (same as Source for these maps)
        spawns.append((team, x, y, z, yaw))

    ntex = sum(1 for t in out.tex if t["rgba"] is not None)
    say("Imported GoldSrc %s: %d tris, %d/%d textures" % (name, len(out.idx) // 3, ntex, len(out.tex)), 1.0)
    try:
        out.ladders = source_ladders(ents, models, out)
    except Exception as ex:
        print("ladders:", ex)
        out.ladders = []
    return out, spawns, name



def import_source_map(path, progress=None, loose_dirs=(), use_vpk=True):
    """.bin (Workshop legacy), .bsp, .gma or .vpk (with a .bsp inside) -> (BspWorld, spawns [(team, x, y, z, yaw)], map name).
    progress(text, frac=None) may be called during long steps (frac 0..1).
    The map may be a Source OR a GoldSrc (HL1 / CS 1.6) .bsp."""
    say = progress or (lambda text, frac=None: None)
    path = Path(path)
    own_vpk = None                                   # a .vpk map: its own textures / models are searched first
    suf = path.suffix.lower()
    if suf == ".gma":
        say("Reading %s ..." % path.name)
        nm, data = gma_read_bsp(path)
        name = Path(nm).stem
    elif suf == ".vpk":
        say("Reading %s ..." % path.name)
        nm, data, own_vpk = vpk_read_bsp(path)
        name = Path(nm).stem
    elif suf == ".bin":                              # Workshop legacy.bin: an LZMA-compressed .gma
        say("Unpacking %s ..." % path.name)
        blob = bin_unwrap(path)
        if blob[:4] == b"GMAD":
            nm, data = gma_read_bsp(path, blob)
            name = Path(nm).stem
        else:                                        # a compressed bare .bsp
            data = blob
            name = path.stem
    elif suf == ".bsp":
        data = path.read_bytes()
        name = path.stem
    else:
        raise ValueError("unsupported file type %s: only .bin, .bsp, .gma and .vpk (with a .bsp inside) can be imported" % (suf or "(none)"))
    # GoldSrc (HL1 / CS 1.6) maps start with version int 29/30, not "VBSP"
    if data[:4] != b"VBSP":
        ver_gs = struct.unpack_from("<i", data, 0)[0] if len(data) >= 4 else -1
        if ver_gs in (29, 30):
            return import_goldsrc_map(data, name, progress=say, loose_dirs=loose_dirs)
        raise ValueError(
            "this is not a Source map (VBSP) or GoldSrc map (v29/v30): "
            "only Source / HL1 / CS 1.6 .bsp maps (also inside .bin / .vpk / .gma) can be imported")
    if len(data) < 8 + 64 * 16:
        raise ValueError("this .bsp is damaged or incomplete (the file is too short)")
    ver = struct.unpack_from("<i", data, 4)[0]
    L = [struct.unpack_from("<iiii", data, 8 + i * 16) for i in range(64)]
    # Lumps with fourCC != 0 are LZMA-compressed (handled in lump())

    def lump(i, dt):
        o, ln, _v, four = L[i]
        if isinstance(dt, dict):
            dt = np.dtype({"names": dt["names"], "formats": dt["formats"],
                           "offsets": dt["offsets"], "itemsize": dt["itemsize"]})
        else:
            dt = np.dtype(dt)
        if ln <= 0 or dt.itemsize <= 0:
            return np.zeros(0, dt)
        blob = data[o:o + ln]
        # fourCC != 0 → Source LZMA-compressed lump (uncompressed size = fourCC)
        if four:
            blob = _bsp_decompress_lump(blob, four)
        n = len(blob) // dt.itemsize
        return np.frombuffer(bytearray(blob), dt, n).copy()

    say("Reading the map ...")
    planes = lump(1, [("n", "<f4", 3), ("d", "<f4"), ("t", "<i4")])
    texdata = lump(2, [("refl", "<f4", 3), ("name", "<i4"), ("w", "<i4"), ("h", "<i4"), ("vw", "<i4"), ("vh", "<i4")])
    verts = lump(3, ("<f4", 3))
    nodes = lump(5, [("plane", "<i4"), ("ch", "<i4", 2), ("mins", "<i2", 3), ("maxs", "<i2", 3), ("first", "<u2"),
                     ("num", "<u2"), ("area", "<i2"), ("pad", "<i2")])
    texinfo = lump(6, [("tv", "<f4", (2, 4)), ("lv", "<f4", (2, 4)), ("flags", "<i4"), ("td", "<i4")])
    faces = lump(7, [("plane", "<u2"), ("side", "u1"), ("onnode", "u1"), ("firstedge", "<i4"), ("numedges", "<i2"),
                     ("texinfo", "<i2"), ("dispinfo", "<i2"), ("fog", "<i2"), ("styles", "u1", 4), ("lightofs", "<i4"),
                     ("area", "<f4"), ("lmins", "<i4", 2), ("lsize", "<i4", 2), ("orig", "<i4"), ("nprims", "<u2"),
                     ("firstprim", "<u2"), ("smooth", "<u4")])
    def _read_leafs(itemsize):
        return lump(10, {"names": ["contents", "lface", "nlface", "lbrush", "nlbrush"],
                         "formats": ["<i4", "<u2", "<u2", "<u2", "<u2"],
                         "offsets": [0, 20, 22, 24, 26], "itemsize": itemsize})
    leafbrushes = lump(17, "<u2")
    n_lb = max(1, len(leafbrushes))
    def _leafs_look_ok(lf):
        if len(lf) < 1:
            return False
        sample = lf[::max(1, len(lf) // 64)][:64]
        ok = 0
        for row in sample:
            a, n = int(row["lbrush"]), int(row["nlbrush"])
            if n == 0:
                ok += 1
            elif 0 <= a and a + n <= n_lb and n < 512:
                ok += 1
        return ok >= max(1, len(sample) * 3 // 4)
    lsz_primary = 56 if ver < 20 else 32
    leafs = _read_leafs(lsz_primary)
    if not _leafs_look_ok(leafs):
        alt = _read_leafs(32 if lsz_primary == 56 else 56)
        if _leafs_look_ok(alt):
            leafs = alt
            say("Leaf struct size adjusted for this BSP version", 0.13)
    edges = lump(12, ("<u2", 2))
    surfedges = lump(13, "<i4")
    models = lump(14, [("mins", "<f4", 3), ("maxs", "<f4", 3), ("origin", "<f4", 3), ("head", "<i4"), ("first", "<i4"),
                       ("num", "<i4")])
    brushes = lump(18, [("first", "<i4"), ("num", "<i4"), ("contents", "<i4")])
    sides = lump(19, [("plane", "<u2"), ("texinfo", "<i2"), ("disp", "<i2"), ("bevel", "<i2")])
    dispinfo = lump(26, {"names": ["start", "vstart", "power", "face"], "formats": [("<f4", 3), "<i4", "<i4", "<u2"],
                         "offsets": [0, 12, 20, 36], "itemsize": 176})
    dverts = lump(33, [("vec", "<f4", 3), ("dist", "<f4"), ("alpha", "<f4")])
    strdata = bytes(lump(43, "u1"))
    strtab = lump(44, "<i4")

    def mat_name(i):
        """Texture name for texdata index i; safe when the string table is broken."""
        try:
            i = int(i)
            if not 0 <= i < len(texdata):
                return "unknown/missing"
            ni = int(texdata[i]["name"])
            if not 0 <= ni < len(strtab):
                return "unknown/missing"
            o = int(strtab[ni])
            if not 0 <= o < len(strdata):
                return "unknown/missing"
            e = strdata.find(b"\0", o)
            if e < 0:
                e = min(len(strdata), o + 128)
            return strdata[o:e].decode("utf-8", "replace") or "unknown/missing"
        except Exception:
            return "unknown/missing"

    ents = _entities(bytes(lump(0, "u1")).split(b"\0")[0].decode("utf-8", "replace"))

    # ---- files: the map's own pack, loose folders next to it, Valve packages ----
    pak = None
    o, ln, _v, _u = L[40]
    if ln:
        try:
            pak = zipfile.ZipFile(io.BytesIO(data[o:o + ln]))
        except Exception:
            pak = None
    vpks = ([own_vpk] if own_vpk is not None else []) + (_find_vpks() if use_vpk else [])
    files = _Files(pak, [Path(path).parent] + [Path(d) for d in loose_dirs], vpks)

    # ---- materials (loaded once, the first time somebody asks) ----
    matc = {}
    nfound = [0]

    def _tex_of(vals, key):
        t = vals.get(key)
        if not t:
            return None
        vb = files.read("materials/%s.vtf" % t.replace("\\", "/").replace(".vtf", ""))
        if not vb:
            return None
        try:
            ow, oh = struct.unpack_from("<HH", vb, 16)
            img, has_a = load_vtf_bytes(vb, BSP_TEX_MAX)
            return np.ascontiguousarray(img), has_a, (ow, oh)
        except Exception:
            return None

    def matinfo(nm):
        nm = nm.replace("\\", "/").lower().strip("/")
        if nm.endswith(".vmt"):
            nm = nm[:-4]
        r = matc.get(nm)
        if r is not None:
            return r
        r = {"name": nm, "rgba": None, "mode": 0, "alpha": 1.0, "tint": None, "rgba2": None, "size": None,
             "decal": False, "modulate": False, "scale": 1.0}
        matc[nm] = r
        try:
            raw = files.read("materials/%s.vmt" % nm)
            if raw:
                txt = _vmt_clean(raw.decode("utf-8", "replace"))
                vals = {}
                inc = re.search(r'"include"\s+"([^"]+)"', txt, re.I)
                if inc:
                    r2 = files.read(inc.group(1))
                    if r2:
                        vals.update(_vmt_vals(_vmt_clean(r2.decode("utf-8", "replace"))))
                vals.update(_vmt_vals(txt))
                r["modulate"] = bool(re.match(r'\s*"?decalmodulate', txt, re.I))
                t = _tex_of(vals, "$basetexture")
                if t is not None:
                    img, has_a, r["size"] = t
                    r["decal"] = vals.get("$decal") == "1"
                    if (vals.get("$translucent") == "1" or r["decal"]) and has_a:
                        r["mode"] = 2
                    elif vals.get("$alphatest") == "1" and has_a:
                        r["mode"] = 1
                    if r["mode"] == 0:
                        img[:, :, 3] = 255
                    try:
                        r["alpha"] = float(vals.get("$alpha", 1.0))
                    except ValueError:
                        pass
                    if r["mode"] == 0 and r["alpha"] < 0.999 and vals.get("$translucent") == "1":
                        r["mode"] = 2
                    r["rgba"] = img
                    nfound[0] += 1
                t2 = _tex_of(vals, "$basetexture2")
                if t2 is not None and r["rgba"] is not None:
                    img2 = t2[0]
                    img2[:, :, 3] = 255
                    r["rgba2"] = img2
                for k in ("$color", "$color2"):
                    m = re.search(r'"?' + re.escape(k) + r'"?\s+"?([\[{])\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)', txt, re.I)
                    if m:
                        c = np.array([float(m.group(2)), float(m.group(3)), float(m.group(4))])
                        if m.group(1) == "{":
                            c = c / 255.0
                        if np.any(np.abs(c - 1.0) > 0.02):
                            r["tint"] = np.clip(c, 0, 1.5) if r["tint"] is None else r["tint"] * np.clip(c, 0, 1.5)
                try:
                    r["scale"] = float(vals.get("$decalscale", 1.0))
                except ValueError:
                    pass
        except Exception:
            r["rgba"] = None
        return r

    # ---- which brush models are part of the world ----
    todo = [(0, (0.0, 0.0, 0.0), True, True)]               # (model, offset, drawn, solid)
    for e in ents:
        m = e.get("model", "")
        if not m.startswith("*"):
            continue
        cls = e.get("classname", "")
        if cls not in BSP_SOLID_ENTS and cls not in BSP_VISUAL_ENTS:
            continue                                         # doors, triggers, buttons ... are not static
        try:
            mi = int(m[1:])
        except ValueError:
            continue
        if not 0 < mi < len(models) or e.get("startdisabled") == "1" or e.get("rendermode") == "10":
            continue
        solid = cls in BSP_SOLID_ENTS and e.get("solidity", "0") != "1" and e.get("solid", "1") != "0"
        off = tuple(_nums(e["origin"])) if "origin" in e else (0.0, 0.0, 0.0)
        todo.append((mi, off, True, solid))

    _brush_cache = {}

    def model_brushes(head):
        hit = _brush_cache.get(head)
        if hit is not None:
            return hit
        out, stack = set(), [head]
        # hard limit so a corrupt / huge node tree cannot hang the import forever
        steps = 0
        while stack and steps < 500000:
            steps += 1
            i = stack.pop()
            if i >= 0:
                if i < len(nodes):
                    c0, c1 = int(nodes[i]["ch"][0]), int(nodes[i]["ch"][1])
                    # children are node indices (>=0) or ~leaf (<0); ignore nonsense values
                    if c0 < len(nodes) or c0 < 0:
                        stack.append(c0)
                    if c1 < len(nodes) or c1 < 0:
                        stack.append(c1)
            else:
                li = -1 - i
                if 0 <= li < len(leafs):
                    lf = leafs[li]
                    a = int(lf["lbrush"])
                    n = int(lf["nlbrush"])
                    if n > 0 and 0 <= a and a + n <= len(leafbrushes) and n < 512:
                        out.update(int(v) for v in leafbrushes[a:a + n]
                                   if 0 <= int(v) < len(brushes))
        _brush_cache[head] = out
        return out

    # ---- collision brushes ----
    say("Building collision brushes ...", 0.18)
    PL, BF, BC, BG, BB = [], [], [], [], []
    npl = 0
    n_added = 0
    n_skipped = 0

    def add_brush(N, D, flags):
        nonlocal npl, n_added, n_skipped
        # Cap insane side counts (bevel explosion) — still collide via a tight-enough AABB
        if len(D) > 48:
            # keep the strongest planes (largest |n| components first) + all near-axis ones
            score = np.abs(N).max(1) + 0.15 * np.abs(N).sum(1)
            order = np.argsort(-score)
            keep = list(order[:32])
            for i in order[32:]:
                if np.max(np.abs(N[i])) > 0.9:
                    keep.append(int(i))
                if len(keep) >= 40:
                    break
            N, D = N[keep], D[keep]
        bb = _brush_bbox(N, D)
        if bb is None:
            n_skipped += 1
            return
        PL.append(np.concatenate([N, D[:, None]], 1).astype(np.float32))
        BF.append(npl)
        BC.append(len(D))
        BG.append(flags)
        BB.append(np.concatenate([bb[0] - 0.5, bb[1] + 0.5]).astype(np.float32))
        npl += len(D)
        n_added += 1

    # Collect unique solid brushes first (one tree walk per unique model head)
    solid_jobs = []  # (brush_index, offset_xyz)
    seen = set()
    heads_done = set()
    for mi, off, _dr, solid in todo:
        if not solid:
            continue
        if not 0 <= mi < len(models):
            continue
        head = int(models[mi]["head"])
        if head not in heads_done:
            heads_done.add(head)
            # progress while walking large BSP trees
            say("Building collision brushes (scan models)...", 0.20)
        for bi in model_brushes(head):
            if bi in seen:
                continue
            seen.add(bi)
            solid_jobs.append((bi, off if mi else (0.0, 0.0, 0.0)))

    n_jobs = max(1, len(solid_jobs))
    say("Building collision brushes (0 / %d)..." % n_jobs, 0.22)
    for ji, (bi, off) in enumerate(solid_jobs):
        if ji % 200 == 0 or ji + 1 == n_jobs:
            say("Building collision brushes (%d / %d)..." % (ji + 1, n_jobs),
                0.22 + 0.12 * (ji + 1) / n_jobs)
        if not (0 <= bi < len(brushes)):
            continue
        br = brushes[bi]
        fl = (1 if br["contents"] & BSP_PLAYER_MASK else 0) | (2 if br["contents"] & BSP_SHOT_MASK else 0)
        if not fl or br["num"] < 4:
            continue
        first = int(br["first"])
        num = int(br["num"])
        if first < 0 or num < 4 or first >= len(sides):
            continue
        num = min(num, len(sides) - first)
        sd = sides[first:first + num]
        pidx = sd["plane"].astype(np.int64)
        # plane indices are uint16 in the file; drop any that fall outside the planes lump
        valid = (pidx >= 0) & (pidx < len(planes))
        if not valid.all():
            if valid.sum() < 4:
                continue
            pidx = pidx[valid]
        if len(pidx) < 4:
            continue
        pn = planes[pidx]
        N = pn["n"].astype(np.float64)
        D = pn["d"].astype(np.float64) + N @ np.array(off, np.float64)
        add_brush(N, D, fl)
    say("Collision brushes: %d ready (%d skipped)" % (n_added, n_skipped), 0.34)

    # ---- visible faces ----
    say("Building the mesh ...")
    P_, T_, C_, A_ = [], [], [], []
    mat_tris = {}                            # material key -> list of index arrays (into the global vertex list)
    nv = 0

    def shade(nz, nx, ny):
        return float(np.clip(0.68 + 0.32 * nz + 0.1 * (abs(ny) - abs(nx)), 0.45, 1.0))

    def add_poly(P, UV, n, key, offset, va=None, lift=0.0):
        nonlocal nv
        k = len(P)
        if k < 3:
            return
        tri = np.concatenate([[0] * (k - 2), np.arange(1, k - 1), np.arange(2, k)]).reshape(3, -1).T
        e = np.cross(P[tri[0, 1]] - P[tri[0, 0]], P[tri[0, 2]] - P[tri[0, 0]])
        if e @ n < 0:
            tri = tri[:, ::-1]
        s = shade(n[2], n[0], n[1])
        P_.append((P + np.array(offset) + n * lift).astype(np.float32))
        T_.append(UV.astype(np.float32))
        C_.append(np.tile(np.array([s, s, s], np.float32), (k, 1)))
        A_.append(np.ones(k, np.float32) if va is None else np.asarray(va, np.float32))
        mat_tris.setdefault(key, []).append((tri + nv).astype(np.uint32).reshape(-1))
        nv += k

    def add_mesh(P, UV, N, tris, key, tint=None):
        """A model part: shared vertices, shaded per vertex."""
        nonlocal nv
        used, inv = np.unique(tris, return_inverse=True)
        k = len(used)
        n = N[used]
        s = np.clip(0.68 + 0.32 * n[:, 2] + 0.1 * (np.abs(n[:, 1]) - np.abs(n[:, 0])), 0.45, 1.0)
        c = np.repeat(s[:, None], 3, 1)
        if tint is not None:
            c = c * np.asarray(tint)[None, :]
        P_.append(P[used].astype(np.float32))
        T_.append(UV[used].astype(np.float32))
        C_.append(c.astype(np.float32))
        A_.append(np.ones(k, np.float32))
        mat_tris.setdefault(key, []).append((inv.reshape(-1) + nv).astype(np.uint32))
        nv += k

    def face_info(f):
        ti = int(f["texinfo"])
        if ti < 0 or ti >= len(texinfo):
            return None
        inf = texinfo[ti]
        if inf["flags"] & SURF_NODRAW_MASK:
            return None
        td = int(inf["td"])
        if not 0 <= td < len(texdata):
            return None
        nm = mat_name(td)
        # Skip real Source tool textures only (nodraw/sky/trigger/...).
        # Do NOT skip our "unknown/missing" placeholder or every face disappears.
        nu = nm.upper().replace("\\", "/")
        if nu.startswith("TOOLS/") and "MISSING" not in nu and "UNKNOWN" not in nu:
            return None
        return inf, td

    def face_poly(f):
        ne = int(f["numedges"])
        fe = int(f["firstedge"])
        if ne < 3 or fe < 0 or fe >= len(surfedges):
            return np.zeros((0, 3), np.float64)
        ne = min(ne, len(surfedges) - fe)
        se = surfedges[fe:fe + ne]
        ai = np.abs(se).astype(np.int64)
        ok_e = (ai >= 0) & (ai < len(edges))
        if ok_e.sum() < 3:
            return np.zeros((0, 3), np.float64)
        se = se[ok_e]
        ed = edges[ai[ok_e]]
        vi = np.where(se >= 0, ed[:, 0], ed[:, 1]).astype(np.int64)
        ok_v = (vi >= 0) & (vi < len(verts))
        vi = vi[ok_v]
        if len(vi) < 3:
            return np.zeros((0, 3), np.float64)
        return verts[vi].astype(np.float64)


    disp_brushes = 0
    blended = 0
    face_jobs = []
    for mi, off, _dr, solid in todo:
        if not 0 <= mi < len(models):
            continue
        m = models[mi]
        f0 = max(0, int(m["first"]))
        f1 = min(len(faces), f0 + max(0, int(m["num"])))
        for fi in range(f0, f1):
            face_jobs.append((fi, off, solid))
    n_faces = max(1, len(face_jobs))
    _mat_two_cache = {}
    say("Building the mesh (faces, displacements) 0/%d ..." % n_faces, 0.35)

    def add_disp_batch(pos, uv, dalpha, n, td, off, solid, two):
        """Displacement grid -> triangles. Collision = one AABB."""
        nonlocal disp_brushes, blended, npl
        size = int(round(math.sqrt(len(pos))))
        if size * size != len(pos) or size < 2:
            return
        yy, xx = np.mgrid[0:size - 1, 0:size - 1]
        yy, xx = yy.ravel(), xx.ravel()
        a = yy * size + xx
        b = a + 1
        c2 = a + size + 1
        d2 = a + size
        even = ((xx + yy) % 2) == 0
        t0 = np.where(even[:, None], np.stack([a, b, c2], 1), np.stack([a, b, d2], 1))
        t1 = np.where(even[:, None], np.stack([a, c2, d2], 1), np.stack([b, c2, d2], 1))
        tris = np.concatenate([t0, t1], 0)
        Q0, Q1, Q2 = pos[tris[:, 0]], pos[tris[:, 1]], pos[tris[:, 2]]
        tn = np.cross(Q1 - Q0, Q2 - Q0)
        ln = np.linalg.norm(tn, axis=1)
        good = ln > 1e-3
        if not good.any():
            return
        tris = tris[good]
        tn = tn[good] / ln[good, None]
        flip = (tn @ n) < 0
        tn = np.where(flip[:, None], -tn, tn)
        if flip.any():
            tris = tris.copy()
            tris[flip] = tris[flip][:, ::-1]
        # vectorized vertex normals
        Nv = np.zeros_like(pos)
        np.add.at(Nv, tris[:, 0], tn)
        np.add.at(Nv, tris[:, 1], tn)
        np.add.at(Nv, tris[:, 2], tn)
        cnt = np.zeros(len(pos), np.float64)
        np.add.at(cnt, tris[:, 0], 1.0)
        np.add.at(cnt, tris[:, 1], 1.0)
        np.add.at(cnt, tris[:, 2], 1.0)
        Nv = Nv / np.maximum(cnt, 1.0)[:, None]
        add_mesh(pos, uv, Nv, tris, td, None)
        # blend layer: skip expensive per-tri add_poly; bake average alpha into mesh only
        # (visual blend is optional and was the main remaining cost on big maps)
        if solid:
            off_a = np.array(off, np.float64)
            bb0 = pos.min(0) + off_a - 1.0
            bb1 = pos.max(0) + off_a + 1.0
            N6 = np.array([[1., 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]], np.float64)
            D6 = np.array([bb1[0], -bb0[0], bb1[1], -bb0[1], bb1[2], -bb0[2]], np.float64)
            PL.append(np.concatenate([N6, D6[:, None]], 1).astype(np.float32))
            BF.append(npl)
            BC.append(6)
            BG.append(3)
            BB.append(np.concatenate([bb0 - 0.5, bb1 + 0.5]).astype(np.float32))
            npl += 6
            disp_brushes += 1

    for ji, (fi, off, solid) in enumerate(face_jobs):
        if ji % 100 == 0 or ji + 1 == n_faces:
            say("Building the mesh (faces, displacements) %d/%d ..." % (ji + 1, n_faces),
                0.35 + 0.20 * (ji + 1) / n_faces)
        f = faces[fi]
        fin = face_info(f)
        if fin is None:
            continue
        inf, td = fin
        P = face_poly(f)
        if len(P) < 3:
            continue
        pi = int(f["plane"])
        if not 0 <= pi < len(planes):
            continue
        n = planes[pi]["n"].astype(np.float64)
        if f["side"]:
            n = -n
        w, h = max(1, int(texdata[td]["w"])), max(1, int(texdata[td]["h"]))
        tv = inf["tv"].astype(np.float64)

        def uvf(Q, _tv=tv, _w=w, _h=h):
            return np.stack([(Q @ _tv[0, :3] + _tv[0, 3]) / _w,
                             (Q @ _tv[1, :3] + _tv[1, 3]) / _h], 1)

        if f["dispinfo"] >= 0:
            if len(P) != 4 or int(f["dispinfo"]) >= len(dispinfo):
                continue
            di = dispinfo[int(f["dispinfo"])]
            power = int(di["power"])
            if power < 1 or power > 4:
                continue
            size = (1 << power) + 1
            k = int(np.argmin(np.linalg.norm(P - di["start"].astype(np.float64), axis=1)))
            c = np.roll(P, -k, axis=0)
            g = np.linspace(0.0, 1.0, size)
            fx, fy = np.meshgrid(g, g)
            fx, fy = fx.reshape(-1, 1), fy.reshape(-1, 1)
            base = (c[0] + fx * (c[1] - c[0]) + fy * (c[3] - c[0])
                    + fx * fy * (c[0] + c[2] - c[1] - c[3]))
            vs0 = int(di["vstart"])
            need = size * size
            if vs0 < 0 or vs0 + need > len(dverts):
                continue
            dv = dverts[vs0:vs0 + need]
            pos = base + dv["vec"].astype(np.float64) * dv["dist"].astype(np.float64)[:, None]
            uv = uvf(base)
            dalpha = np.clip(dv["alpha"].astype(np.float64) / 255.0, 0.0, 1.0)
            nm_td = mat_name(td)
            if nm_td not in _mat_two_cache:
                _mat_two_cache[nm_td] = matinfo(nm_td).get("rgba2") is not None
            two = _mat_two_cache[nm_td]
            add_disp_batch(pos, uv, dalpha, n, td, off, solid, two)
            if ji % 25 == 0:
                say("Building the mesh (faces, displacements) %d/%d ..." % (ji + 1, n_faces),
                    0.35 + 0.20 * (ji + 1) / n_faces)
            continue
        add_poly(P, uvf(P), n, td, off)
    say("Mesh: %d faces scanned, %d displacement colliders" % (n_faces, disp_brushes), 0.55)

    if not P_:
        raise ValueError(
            "this map has no visible geometry (faces=%d verts=%d edges=%d models=%d texinfo=%d — "
            "all faces were nodraw/tools or failed to parse)" % (
                len(faces), len(verts), len(edges), len(models), len(texinfo)))

    say("Loading props ...", 0.56)
    # ---- props: static props (game lump "sprp") + prop_dynamic / prop_physics entities ----
    nprop_inst, nprop_miss, props_missing = 0, 0, set()
    mcache, rcache, ucount, insts = {}, {}, {}, []

    def prop_model(mp):
        _low = mp.replace("\\\\", "/").lower()
        if any(s in _low for s in ("/gibs/", "/particle", "/effects/", "_gib", "debris")):
            mcache[mp] = None
            return None
        if len(mcache) >= 80:
            mcache[mp] = None
            return None
        # progress tick while loading unique models
        say("Loading model %s" % (mp.split("/")[-1][:40]), max(0.56, min(0.68, 0.56 + 0.001 * len(mcache))))
        mp = mp.replace("\\", "/").lower()
        if mp in mcache:
            return mcache[mp]
        r = None
        try:
            r = load_model(files, mp, int(min(PROP_MAX_TRIS, max(PROP_MIN_TRIS, PROP_TRI_BUDGET // max(1, ucount.get(mp, 1))))))
            if r is not None:
                P, N = r["P"], r["N"]
                fl = {}
                for mi_, tr in r["parts"]:                      # make every triangle face the way its normals point
                    gn = np.cross(P[tr[:, 1]] - P[tr[:, 0]], P[tr[:, 2]] - P[tr[:, 0]])
                    vn = N[tr[:, 0]] + N[tr[:, 1]] + N[tr[:, 2]]
                    fl[id(tr)] = float(np.einsum("ij,ij->i", gn, vn).sum()) < 0
                r["parts"] = [(mi_, tr[:, ::-1] if fl[id(tr)] else tr) for mi_, tr in r["parts"]]
        except Exception as ex:
            print("model failed:", mp, ex)
            r = None
        mcache[mp] = r
        return r

    def prop_mat(r, mp, matidx, skin):
        sk = r["skins"][min(max(skin, 0), len(r["skins"]) - 1)]
        ti = sk[matidx] if 0 <= matidx < len(sk) else matidx
        names = r["texnames"]
        if not 0 <= ti < len(names):
            return None
        ck = (mp, ti)
        if ck in rcache:
            return rcache[ck]
        tn = names[ti].replace("\\", "/").lower()
        cands = [cd.lstrip("/") + tn for cd in r["cds"]] + [tn]
        key = None
        for c in cands:
            if files.read("materials/%s.vmt" % c) is not None:
                key = c.strip("/")
                break
        if key is None:
            key = cands[0].strip("/")
        rcache[ck] = key
        return key

    def place_prop(mp, org, ang, skin=0, scale=1.0, tint=None, solid=0):
        nonlocal nprop_inst
        r = prop_model(mp)
        if r is None:
            props_missing.add(mp)
            return
        M = angle_matrix(*ang)
        Pw = (r["P"] * scale) @ M.T + np.array(org)
        Nw = r["N"] @ M.T
        for matidx, tr in r["parts"]:
            key = prop_mat(r, mp.lower(), matidx, skin)
            if key is None:
                continue
            t = tint
            mi_ = matinfo(key)
            if mi_["tint"] is not None:
                t = mi_["tint"] if t is None else np.asarray(t) * mi_["tint"]
            add_mesh(Pw, r["T"], Nw, tr, key, t)
        nprop_inst += 1
        if PROP_COLLISION and solid:
            lo, hi = r["bmin"] * scale, r["bmax"] * scale
            if (hi - lo).min() > 4.0:
                N6, D6 = [], []
                for ax in range(3):
                    a = M[:, ax]
                    N6 += [a, -a]
                    D6 += [a @ np.array(org) + hi[ax], -(a @ np.array(org)) - lo[ax]]
                add_brush(np.array(N6, np.float64), np.array(D6, np.float64), 3)

    # the static props
    try:
        o, ln, _v, _u = L[35]
        if ln:
            ng = struct.unpack_from("<i", data, o)[0]
            for gi in range(ng):
                gid, gfl, gver, gofs, glen = struct.unpack_from("<4sHHii", data, o + 4 + gi * 16)
                if gid[::-1] != b"sprp":
                    continue
                p = gofs
                nd = struct.unpack_from("<i", data, p)[0]
                p += 4
                dic = [data[p + i * 128:p + i * 128 + 128].split(b"\0")[0].decode("utf-8", "replace") for i in range(nd)]
                p += nd * 128
                nl = struct.unpack_from("<i", data, p)[0]
                p += 4 + nl * 2
                npr = struct.unpack_from("<i", data, p)[0]
                p += 4
                if npr <= 0:
                    break
                stride = (gofs + glen - p) // npr
                say("Reading props (%d static) ..." % npr)
                for i in range(npr):
                    b = p + i * stride
                    px, py, pz, pitch, yaw, roll = struct.unpack_from("<6f", data, b)
                    pt, _fl, _lc, solid, _flags, skin = struct.unpack_from("<HHHBBi", data, b + 24)
                    if not 0 <= pt < len(dic):
                        continue
                    scale = 1.0
                    if gver >= 11 and stride >= 80:
                        sc = struct.unpack_from("<f", data, b + stride - 4)[0]
                        if 0.01 < sc < 100:
                            scale = sc
                    tint = None
                    if gver >= 7 and stride >= 72:
                        cr, cg, cb, ca = struct.unpack_from("<4B", data, b + 68)
                        if (cr, cg, cb) != (255, 255, 255) and (cr or cg or cb):
                            tint = (cr / 255.0, cg / 255.0, cb / 255.0)
                    insts.append((dic[pt], (px, py, pz), (pitch, yaw, roll), skin, scale, tint, solid in (2, 3, 6)))
                break
    except Exception as ex:
        print("static props failed:", ex)

    # the prop entities
    nent = sum(1 for e in ents if e.get("classname") in PROP_ENTS and e.get("model", "").lower().endswith(".mdl"))
    if nent:
        say("Reading props (%d entities) ..." % nent)
    for e in ents:
        cls = e.get("classname", "")
        mp = e.get("model", "")
        if cls not in PROP_ENTS or not mp.lower().endswith(".mdl"):
            continue
        if e.get("startdisabled") == "1" or e.get("rendermode") == "10":
            continue
        if e.get("rendermode", "0") not in ("0", "") and e.get("renderamt", "255") == "0":
            continue
        try:
            skin = int(float(e.get("skin", "0") or 0))
            scale = float(e.get("modelscale", "1") or 1)
        except ValueError:
            skin, scale = 0, 1.0
        tint = None
        if "rendercolor" in e:
            rc = _nums(e["rendercolor"])
            if rc != [255.0, 255.0, 255.0] and any(rc):
                tint = tuple(v / 255.0 for v in rc)
        solid = cls not in ("prop_door_rotating", "prop_detail") and e.get("solid", "6") not in ("0", "1")
        insts.append((mp, _nums(e.get("origin", "0 0 0")), _nums(e.get("angles", "0 0 0")), skin, scale if scale > 0 else 1.0,
                      tint, solid))
    for it in insts:
        k_ = it[0].replace("\\", "/").lower()
        ucount[k_] = ucount.get(k_, 0) + 1
    if insts:
        say("Building %d props ..." % len(insts))
    for it in insts:
        place_prop(*it)

    # ---- decals (infodecal) stuck on the nearest wall / floor ----
    ndecal = 0
    try:
        decs = [e for e in ents if e.get("classname") == "infodecal" and e.get("texture") and "origin" in e]
        if decs:
            say("Placing decals (%d) ..." % len(decs))
            Pa = np.concatenate(P_).astype(np.float64)
            parts = [np.concatenate(v) for k, v in mat_tris.items()
                     if not (isinstance(k, tuple) and k[0] == "blend") and v]
            if not parts:
                raise RuntimeError("no mesh for decals")
            Ia = np.concatenate(parts).astype(np.int64)
            Ia = Ia[(Ia >= 0) & (Ia < len(Pa))]
            Ia = Ia[:len(Ia) - (len(Ia) % 3)].reshape(-1, 3)
            if len(Ia) == 0:
                raise RuntimeError("no triangles for decals")
            TR = Pa[Ia]
            tlo, thi = TR.min(1), TR.max(1)
            for e in decs:
                mi_ = matinfo(e["texture"])
                if mi_["rgba"] is None or mi_["modulate"] or mi_["mode"] == 0:
                    continue
                org = np.array(_nums(e["origin"]), np.float64)
                near = np.where((tlo <= org + 72).all(1) & (thi >= org - 72).all(1))[0]
                if not len(near):
                    continue
                T3 = TR[near]
                best = None
                for dr in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    dd = np.array(dr, np.float64)
                    e1, e2 = T3[:, 1] - T3[:, 0], T3[:, 2] - T3[:, 0]
                    pp = np.cross(dd, e2)
                    det = np.einsum("ij,ij->i", e1, pp)
                    ok = np.abs(det) > 1e-9
                    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
                    s_ = org - T3[:, 0]
                    u = np.einsum("ij,ij->i", s_, pp) * inv
                    q = np.cross(s_, e1)
                    v = (q @ dd) * inv
                    t = np.einsum("ij,ij->i", e2, q) * inv
                    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0.0) & (t < 72.0)
                    if hit.any():
                        i = np.where(hit)[0][np.argmin(t[hit])]
                        if best is None or t[i] < best[0]:
                            nn = np.cross(e1[i], e2[i])
                            nn /= (np.linalg.norm(nn) or 1.0)
                            if nn @ dd > 0:
                                nn = -nn
                            best = (float(t[i]), org + dd * float(t[i]), nn)
                if best is None:
                    continue
                _t, hp, nn = best
                ow, oh = mi_["size"] or (64, 64)
                sc = mi_["scale"] if mi_["scale"] > 0 else 1.0
                hw, hh = ow * sc / 2.0, oh * sc / 2.0
                up = np.array([0.0, 0.0, 1.0]) if abs(nn[2]) < 0.9 else np.array([0.0, 1.0, 0.0])
                ru = np.cross(up, nn)
                ru /= (np.linalg.norm(ru) or 1.0)
                rv = np.cross(nn, ru)
                Q = np.array([hp - ru * hw - rv * hh, hp + ru * hw - rv * hh, hp + ru * hw + rv * hh, hp - ru * hw + rv * hh])
                UV = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], np.float64)
                add_poly(Q, UV, nn, ("decal", mi_["name"]), (0, 0, 0), None, 0.4)
                ndecal += 1
    except Exception as ex:
        print("decals failed:", ex)

    # ---- materials and textures ----
    allk = list(mat_tris)
    isprop = lambda k: isinstance(k, str)
    used = [k for k in allk if not isprop(k)] + [k for k in allk if isprop(k)]      # the map first, props last
    n_used = max(1, len(used))
    say("Loading textures (%d materials) ..." % len(used), 0.80)
    out = BspWorld()
    out.name = name
    mats = []
    for mi_, key in enumerate(used):
        if mi_ % max(1, n_used // 20) == 0 or mi_ + 1 == n_used:
            say("Loading textures (%d / %d) ..." % (mi_ + 1, n_used), 0.80 + 0.15 * (mi_ + 1) / n_used)
        rf = np.array([0.6, 0.6, 0.6])
        if isinstance(key, int):
            nm = mat_name(key)
            info = matinfo(nm)
            rf = np.clip(texdata[key]["refl"].astype(np.float64), 0, 1) ** (1 / 2.2)
            rgba, mode, alpha, tint = info["rgba"], info["mode"], info["alpha"], info["tint"]
        elif key[0] == "blend":
            nm = mat_name(key[1])
            info = matinfo(nm)
            nm += "#2"
            rgba, mode, alpha, tint = info["rgba2"], 2, 1.0, info["tint"]
        elif key[0] == "decal":
            info = matinfo(key[1])
            nm = key[1]
            rgba, mode, alpha, tint = info["rgba"], info["mode"] or 1, info["alpha"], info["tint"]
            if rgba is not None and mode == 1 and info["mode"] == 0:
                rgba = rgba.copy()
        else:
            nm = key
            info = matinfo(nm)
            rgba, mode, alpha, tint = info["rgba"], info["mode"], info["alpha"], info["tint"]
        if rgba is None and nm.upper().startswith(("GLASS/", "WATER", "NATURE/WATER")):
            mode, alpha = 2, 0.45
        mats.append((key, rgba, mode, alpha, rf, tint))
        out.tex.append({"name": nm, "w": 0 if rgba is None else rgba.shape[1], "h": 0 if rgba is None else rgba.shape[0],
                        "rgba": rgba, "opaque": rgba is None or bool((rgba[:, :, 3] == 255).all())})

    say("Finalizing mesh buffers ...", 0.90)
    # ---- one vertex / index array, batches in draw order ----
    pos = np.concatenate(P_)
    uvs = np.concatenate(T_)
    shd = np.concatenate(C_)
    col = np.zeros((len(pos), 4), np.uint8)
    col[:, :3] = np.clip(shd * 255, 0, 255).astype(np.uint8)
    col[:, 3] = np.clip(np.concatenate(A_) * 255, 0, 255).astype(np.uint8)
    npos = len(pos)
    idx_parts, batches, pos_i = [], [], 0
    for mi_, (key, rgba, mode, alpha, rf, tint) in enumerate(mats):
        if key not in mat_tris:
            continue
        ix = np.concatenate(mat_tris[key]).astype(np.int64, copy=False)
        ix = ix[(ix >= 0) & (ix < npos)]
        if len(ix) < 3:
            continue
        # drop incomplete triangles after filtering
        ix = ix[:len(ix) - (len(ix) % 3)]
        if len(ix) < 3:
            continue
        vs = np.unique(ix)
        if rgba is None:                                   # no picture found: paint it with the material's average colour
            col[vs, :3] = np.clip(col[vs, :3] * rf[None, :], 0, 255).astype(np.uint8)
        elif tint is not None:
            col[vs, :3] = np.clip(col[vs, :3] * np.asarray(tint)[None, :], 0, 255).astype(np.uint8)
        if alpha < 0.999:
            col[vs, 3] = int(max(0.0, min(1.0, alpha)) * 255)
        idx_parts.append(ix)
        batches.append([mi_, mode, pos_i, len(ix)])
        pos_i += len(ix)
    out.pos, out.uv, out.col = pos, uvs, col
    out.idx = np.concatenate(idx_parts).astype(np.uint32)
    out.nray = int(sum(len(ix) for ix, k in zip(idx_parts, used) if not isprop(k)))   # mouse picking ignores props
    out.batches = batches
    out.planes = np.concatenate(PL) if PL else np.zeros((0, 4), np.float32)
    out.bfirst = np.array(BF, np.int32)
    out.bcnt = np.array(BC, np.int32)
    out.bflags = np.array(BG, np.uint8)
    out.bbox = np.array(BB, np.float32).reshape(-1, 6)
    out.calc_bounds()

    # ---- spawn points ----
    spawns, k = [], 0
    for e in ents:
        cls = e.get("classname", "")
        if not cls.startswith("info_player") or "origin" not in e:
            continue
        x, y, z = _nums(e["origin"])
        yaw = _nums(e.get("angles", "0 0 0"))[1]
        if cls.endswith("counterterrorist") or cls.endswith("combine"):
            team = 0
        elif cls.endswith("terrorist") or cls.endswith("rebel"):
            team = 1
        else:
            team, k = k % 2, k + 1
        fz = out.floor_below(x, y, z, 16.0, 64.0)
        if fz is None:
            continue
        spawns.append((team, x, y, fz, yaw))
    nreal = sum(1 for t in out.tex if t["rgba"] is not None)
    say("Imported %s: %d triangles, %d brushes, %d props, %d decals, %d/%d textures found" % (
        name, len(out.idx) // 3, len(out.bfirst), nprop_inst, ndecal, nreal, len(out.tex)))
    if props_missing:
        print("props whose model files were not found (install CS:GO / CS:S / HL2 / GMod or pass loose_dirs):")
        for mp in sorted(props_missing):
            print("   ", mp)
    miss = [t["name"] for t in out.tex if t["rgba"] is None]
    if miss:
        print("materials without a picture (%d):" % len(miss), ", ".join(miss[:40]))
    say("Import complete", 1.0)
    try:
        out.ladders = source_ladders(ents, models, out)
    except Exception as ex:
        print("ladders:", ex)
        out.ladders = []
    return out, spawns, name


class MapDoc:
    def __init__(self):
        self.cube = [None] * 6            # sky as 6 faces (px nx py ny pz nz): Tex or None
        self.boxes = []
        self.decals = []
        self.spawns = []
        self.tex = []
        self.sky = None
        self.bsp = None                   # imported Source map (BspWorld) or None
        self.path = None
        self.name = "untitled"
        self.dirty = False

    def new_default(self):
        self.boxes = [Box([-1024, -1024, -32, 1024, 1024, 0])]
        self.spawns = []

    def snap(self):
        return ([b.copy() for b in self.boxes], [s.copy() for s in self.spawns], [d.copy() for d in self.decals])

    def restore(self, s):
        self.boxes = [b.copy() for b in s[0]]
        self.spawns = [x.copy() for x in s[1]]
        self.decals = [d.copy() for d in s[2]] if len(s) > 2 else []

    def to_dict(self, compiled=False, cam=None, max_side=1024):
        boxes = self.boxes
        used = sorted({t for b in boxes for t in (b.t, b.tt, *b.f) if t >= 0}) if compiled else list(range(len(self.tex)))
        remap = {old: i for i, old in enumerate(used)}
        d = {
            "format": MAP_FORMAT, "version": MAP_VERSION, "name": self.name,
            "textures": [encode_tex(self.tex[i], max_side) for i in used],
            "boxes": [dict({"b": [round(v, 3) for v in b.b], "t": remap.get(b.t, -1), "tt": remap.get(b.tt, -1),
                            "tile": b.tile, "c": list(b.col), "k": b.k, "d": b.d,
                            "f": [remap.get(v, -1) for v in b.f]},
                           **({"yaw": round(b.yaw, 3)} if b.yaw else {}),
                           **({"px": 1} if b.px else {}),
                           **({"lad": 1} if b.lad else {})) for b in boxes],
            "spawns": [[{"p": [round(s.x, 2), round(s.y, 2), round(s.z, 2)], "yaw": round(s.yaw, 1)}
                        for s in self.spawns if s.team == t] for t in (0, 1)],
        }
        if self.sky is not None:
            d["sky"] = encode_tex(self.sky, 2048)
        if self.bsp is not None:
            d["bsp"] = self.bsp.to_dict()
        if all(self.cube) and not compiled:
            d["skycube"] = [encode_tex(t, 1024) for t in self.cube]
        if self.decals and not compiled:
            d["decals"] = [x.to_dict() for x in self.decals]
        if cam:
            d["cam"] = cam
        return d

    def from_dict(self, d):
        if d.get("format") != MAP_FORMAT:
            raise ValueError("not a FAD-MOD map file")
        self.tex = [decode_tex(t) for t in d.get("textures", [])]
        self.boxes = [Box(b["b"], b.get("t", -1), b.get("tt"), b.get("tile", 128), b.get("c", DEFAULT_COL), b.get("f"), b.get("k", 0), b.get("d", 0), b.get("yaw", 0.0), b.get("px", 0), b.get("lad", 0))
                      for b in d.get("boxes", [])]
        for b in self.boxes:
            b.fix()
        self.spawns = []
        for t, lst in enumerate(d.get("spawns", [[], []])[:2]):
            for s in lst:
                p = s["p"]
                self.spawns.append(Spawn(t, p[0], p[1], p[2], s.get("yaw", 0)))
        self.decals = []
        for e in d.get("decals", []):
            try:
                r = e["r"]
                if int(e["fi"]) not in DEC_AXES:
                    continue
                self.decals.append(Decal(e["tex"], e["fi"], e["p"], r[0], r[1], r[2], r[3],
                                         e.get("c", DEFAULT_COL), e.get("hb"),
                                         e.get("fx", False), e.get("fy", False), e.get("op", 1.0)))
            except Exception:
                continue
        self.sky = decode_tex(d["sky"]) if d.get("sky") else None
        sc = d.get("skycube")
        self.cube = [decode_tex(x) for x in sc] if sc and len(sc) == 6 else [None] * 6
        self.bsp = BspWorld.from_dict(d["bsp"]) if d.get("bsp") else None
        self.name = d.get("name", "untitled")
        return d.get("cam")


# --------------------------------------------------------------------------
# Compile (checks the map, fixes spawn points, writes the game file)
# --------------------------------------------------------------------------
def hull_hits(boxes, x, y, zc, bsp=None):
    """True if the player hull centred at (x, y, zc) overlaps a box (strict, like the game).
    A box is [x0,y0,z0,x1,y1,z1] or (that list, yaw degrees) for a spun one."""
    if bsp is not None and bsp.hull_hits(x, y, zc):
        return True
    for b in boxes:
        if isinstance(b, tuple):
            b, yaw = b
            c, s = yaw_cs(yaw)
            cx, cy = (b[0] + b[3]) / 2, (b[1] + b[4]) / 2
            dx, dy = x - cx, y - cy
            x, y = cx + dx * c + dy * s, cy - dx * s + dy * c      # point in the box's own frame
            if (b[0] - HULL_R < x < b[3] + HULL_R and b[1] - HULL_R < y < b[4] + HULL_R
                    and b[2] - HULL_H / 2 < zc < b[5] + HULL_H / 2):
                return True
            x, y = cx + dx, cy + dy
            continue
        if (b[0] - HULL_R < x < b[3] + HULL_R and b[1] - HULL_R < y < b[4] + HULL_R
                and b[2] - HULL_H / 2 < zc < b[5] + HULL_H / 2):
            return True
    return False


def standing_ok(boxes, x, y, z, bsp=None):
    """Feet at z: nothing in the way, and a floor right below."""
    zc = z + HULL_H / 2 + 0.5
    if hull_hits(boxes, x, y, zc, bsp):
        return False
    return hull_hits(boxes, x, y, zc - 6.0, bsp)


def compile_doc(doc, out_path, progress=None):
    """Returns (summary lines, warnings). Writes out_path.
    progress(text, frac=None) is called during long steps (frac 0..1)."""
    say = progress or (lambda text, frac=None: None)
    msgs, warns = [], []
    boxes, seen, dropped, dups = [], set(), 0, 0
    flat = []
    say("Preparing boxes...", 0.08)
    zones = [z.copy() for z in doc.boxes if z.px and z.b[3] - z.b[0] >= 1 and z.b[4] - z.b[1] >= 1 and z.b[5] - z.b[2] >= 1]
    ladders = [z.copy() for z in doc.boxes if z.lad and not z.px and z.b[3] - z.b[0] >= 1 and z.b[4] - z.b[1] >= 1 and z.b[5] - z.b[2] >= 1]
    for src in doc.boxes:                    # the game turns spun objects itself: keep their spin
        if src.px or src.lad:                # pixel surf / ladder zones are invisible and not solid: they go in their own lists
            continue
        nb = src.copy()
        nb.yaw = src.yaw % 360.0
        if nb.yaw < 1e-4 or nb.yaw > 360.0 - 1e-4:
            nb.yaw = 0.0
        flat.append(nb)
    for b in flat:
        c = tuple(round(v, 2) for v in b.b)
        if c[3] - c[0] < 1 or c[4] - c[1] < 1 or c[5] - c[2] < 1:
            dropped += 1
            continue
        key = (c, b.t, b.tt, b.tile, b.col, tuple(b.f), b.k, b.d, round(b.yaw, 3))
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        nb = b.copy()
        nb.b = list(c)
        boxes.append(nb)
    if dropped:
        warns.append("removed %d empty box(es)" % dropped)
    if dups:
        warns.append("removed %d duplicate box(es)" % dups)
    if not boxes and doc.bsp is None:
        raise ValueError("The map is empty. Build something first.")
    say("Checking spawn points...", 0.25)
    def _aabb(b):                            # box that holds a spun box (for bounds / centre)
        x0, y0, z0, x1, y1, z1 = b.b
        if not b.yaw:
            return [x0, y0, z0, x1, y1, z1]
        c, s = yaw_cs(b.yaw)
        hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
        ex, ey = abs(hx * c) + abs(hy * s), abs(hx * s) + abs(hy * c)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return [cx - ex, cy - ey, z0, cx + ex, cy + ey, z1]
    bl = [_aabb(b) for b in boxes]
    if doc.bsp is not None:
        bl.append(list(doc.bsp.bounds))
    hl = [(b.b, b.yaw) if b.yaw else b.b for b in boxes]      # for the spawn checks

    spawns = []
    for s in doc.spawns:
        if standing_ok(hl, s.x, s.y, s.z, doc.bsp):
            spawns.append(s.copy())
        else:
            warns.append("a %s spawn at (%d, %d, %d) is inside a wall or floating: dropped" % (TEAM_NAMES[s.team], s.x, s.y, s.z))
    # a team without spawn points gets some automatically
    cands = []
    for b in [x.b for x in boxes if x.k != 1 and not x.yaw]:
        if b[3] - b[0] < 96 or b[4] - b[1] < 96:
            continue
        z = b[5]
        x = b[0] + 48
        while x <= b[3] - 47:
            y = b[1] + 48
            while y <= b[4] - 47:
                if standing_ok(hl, x, y, z, doc.bsp):
                    cands.append((x, y, z))
                y += 64
            x += 64
        if len(cands) > 4000:
            break
    if doc.bsp is not None and any(not any(s.team == t for s in spawns) for t in (0, 1)):
        cands += doc.bsp.floor_spots(1500)       # a Source map: floor places taken from the map itself
    cx = sum((b[0] + b[3]) / 2 for b in bl) / len(bl)
    cy = sum((b[1] + b[4]) / 2 for b in bl) / len(bl)
    for team in (0, 1):
        if any(s.team == team for s in spawns):
            continue
        if not cands:
            raise ValueError("No flat floor found for the %s team spawn points. Build a floor and place spawns." % TEAM_NAMES[team])
        key = (lambda p: p[0] + p[1]) if team == 0 else (lambda p: -(p[0] + p[1]))
        start = min(cands, key=key)
        near = sorted(cands, key=lambda p: (p[0] - start[0]) ** 2 + (p[1] - start[1]) ** 2)
        picked = []
        for p in near:
            if all((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 >= 48 ** 2 for q in picked):
                picked.append(p)
            if len(picked) >= 5:
                break
        for p in picked:
            yaw = math.degrees(math.atan2(cy - p[1], cx - p[0]))
            spawns.append(Spawn(team, p[0], p[1], p[2], yaw))
        warns.append("no %s spawn points were placed: added %d automatically" % (TEAM_NAMES[team], len(picked)))

    say("Baking decals...", 0.45)
    tex_all = list(doc.tex)                  # decals become thin boxes with their own baked texture
    dec_boxes, dec_bad = [], 0
    for i, dc in enumerate(doc.decals):
        if not (0 <= dc.tex < len(doc.tex)) or dc.c1 - dc.c0 < 1 or dc.v1 - dc.v0 < 1:
            dec_bad += 1
            continue
        bt, T = bake_decal(dc, doc.tex[dc.tex])
        tex_all.append(bt)
        na, ha, va, sg = DEC_AXES[dc.fi]
        off = 0.05 * (i % 20)                # decals on top of each other do not fight
        lo, hi = (dc.p + off, dc.p + off + DECAL_THICK) if sg > 0 else (dc.p - off - DECAL_THICK, dc.p - off)
        bb = [0.0] * 6
        bb[ha], bb[ha + 3] = dc.c0, dc.c1
        bb[va], bb[va + 3] = dc.v0, dc.v1
        bb[na], bb[na + 3] = lo, hi
        f = [-1] * 6
        f[dc.fi] = len(tex_all) - 1
        dec_boxes.append(Box([round(v, 2) for v in bb], -1, -1, T, dc.col, f))
    if dec_bad:
        warns.append("skipped %d broken decal(s)" % dec_bad)
    say("Encoding textures & geometry...", 0.65)
    tmp = MapDoc()
    tmp.boxes, tmp.spawns, tmp.tex, tmp.name = boxes + dec_boxes, spawns, tex_all, doc.name
    tmp.sky = doc.sky
    tmp.bsp = doc.bsp
    if all(doc.cube):
        tmp.sky = Tex("sky_from_cubemap", cube_to_equirect(doc.cube))
    d = tmp.to_dict(compiled=True, max_side=512)
    if zones:
        d["pxz"] = [dict({"b": [round(v, 2) for v in z.b]}, **({"yaw": round(z.yaw % 360.0, 3)} if z.yaw else {})) for z in zones]
        msgs.append("%d pixel surf zone(s)" % len(zones))
    else:
        warns.append("no pixel surf zones: nobody can pixel surf on this map (tool 8 builds one)")
    if ladders:
        d["lad"] = [dict({"b": [round(v, 2) for v in z.b], "d": z.d % 4}, **({"yaw": round(z.yaw % 360.0, 3)} if z.yaw else {}))
                    for z in ladders]
        msgs.append("%d ladder(s)" % len(ladders))
        for z in ladders:
            if z.b[5] - z.b[2] < 40:
                warns.append("a ladder at (%d, %d, %d) is only %d tall: too short to climb" % (z.b[0], z.b[1], z.b[2], z.b[5] - z.b[2]))
    d["void_z"] = round(min(b[2] for b in bl) - 800.0, 1)
    d["bounds"] = [round(min(b[i] for b in bl), 1) for i in range(3)] + [round(max(b[i + 3] for b in bl), 1) for i in range(3)]
    for e in d["boxes"][len(boxes):]:         # the last ones are the decals: the game makes them non-solid
        e["dc"] = 1
    say("Writing .fadmap file...", 0.88)
    raw = json.dumps(d, separators=(",", ":")).encode("utf-8")
    Path(out_path).write_bytes(raw)
    say("Done", 1.0)
    n0 = sum(1 for s in spawns if s.team == 0)
    msgs.append("%d boxes, %d decals, %d textures, spawns: %d BLACK / %d YELLOW, %.1f KB" % (
        len(boxes), len(dec_boxes), len(d["textures"]), n0, len(spawns) - n0, len(raw) / 1024.0))
    if doc.bsp is not None:
        msgs[-1] += ", Source map: %d triangles, %d brushes" % (len(doc.bsp.idx) // 3, len(doc.bsp.bfirst))
    return msgs, warns


# --------------------------------------------------------------------------
# Spin / turn helpers
# --------------------------------------------------------------------------
def yaw_cs(yaw):
    a = math.radians(yaw)
    return math.cos(a), math.sin(a)


def rot_point(box, p, inv=False):
    """Point p -> where it ends up after the box is spun about its own centre (inv = the other way)."""
    if not box.yaw:
        return p
    c, s = yaw_cs(-box.yaw if inv else box.yaw)
    cx, cy = (box.b[0] + box.b[3]) / 2.0, (box.b[1] + box.b[4]) / 2.0
    dx, dy = p[0] - cx, p[1] - cy
    return (cx + dx * c - dy * s, cy + dx * s + dy * c, p[2])


def rot_vec(box, v, inv=False):
    if not box.yaw:
        return v
    c, s = yaw_cs(-box.yaw if inv else box.yaw)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c, v[2])


def _solve_range(a, b, h):
    """All t with |a*t + b| <= h  ->  (lo, hi) or None."""
    if abs(a) < 1e-9:
        return (-1e18, 1e18) if abs(b) <= h else None
    t1, t2 = (-h - b) / a, (h - b) / a
    return (min(t1, t2), max(t1, t2))


def _strips(cx, cy, ex, ey, hmin, fn):
    """Fill a turned shape with thin axis-aligned strips. fn(stack_y, offset) -> (lo, hi) across the strip."""
    stack_y = ey <= ex                          # stack along the shorter side: fewer strips
    ext, c0 = (ey, cy) if stack_y else (ex, cx)
    s = max(2.0, min(16.0, hmin / 2.0))
    n = int(max(1, min(200, math.ceil(2 * ext / s))))
    step = 2 * ext / n
    out = []
    for j in range(n):
        a = c0 - ext + j * step
        iv = fn(stack_y, a + step / 2 - c0)
        if not iv or iv[1] - iv[0] < 1.0:
            continue
        if stack_y:
            out.append((cx + iv[0], a, cx + iv[1], a + step))
        else:
            out.append((a, cy + iv[0], a + step, cy + iv[1]))
    return out


def bake_box(b):
    """A spun box / slope / circle -> plain axis-aligned boxes (what the game understands)."""
    yaw = b.yaw % 360.0
    x0, y0, z0, x1, y1, z1 = b.b
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
    q = int(round(yaw / 90.0))
    if abs(yaw - q * 90.0) < 1e-3:               # 0 / 90 / 180 / 270: exact, keeps real slopes and cylinders
        q %= 4
        f = list(b.f)
        for _ in range(q):
            f = [f[0], f[1], f[4], f[5], f[3], f[2]]
            hx, hy = hy, hx
        nb = b.copy()
        nb.yaw = 0.0
        nb.b = [cx - hx, cy - hy, z0, cx + hx, cy + hy, z1]
        nb.f = f
        if b.k == 1:
            nb.d = (b.d + q) % 4
        return [nb]
    if b.k == 2 and abs(hx - hy) < 1e-3:         # a round cylinder looks the same spun
        nb = b.copy()
        nb.yaw = 0.0
        return [nb]
    c, s = yaw_cs(yaw)
    rects = []                                    # (x0, y0, z0, x1, y1, z1)
    if b.k == 1:                                  # slope: a grid of little columns following the surface
        ex, ey = abs(hx * c) + abs(hy * s), abs(hx * s) + abs(hy * c)
        cs = max(6.0, math.sqrt(4 * hx * hy / 250.0))
        nx, ny = max(1, int(math.ceil(2 * ex / cs))), max(1, int(math.ceil(2 * ey / cs)))
        sx, sy = 2 * ex / nx, 2 * ey / ny
        for i in range(nx):
            for j in range(ny):
                xa, ya = cx - ex + i * sx, cy - ey + j * sy
                X, Y = xa + sx / 2 - cx, ya + sy / 2 - cy
                u, v = c * X + s * Y, -s * X + c * Y
                if abs(u) > hx or abs(v) > hy:
                    continue
                t = ((u + hx) / (2 * hx), (v + hy) / (2 * hy), 1 - (u + hx) / (2 * hx), 1 - (v + hy) / (2 * hy))[b.d % 4]
                rects.append((xa, ya, z0, xa + sx, ya + sy, z0 + max(1.0, (z1 - z0) * t)))
    elif b.k == 2:                                # elliptic cylinder
        ex, ey = math.hypot(hx * c, hy * s), math.hypot(hx * s, hy * c)

        def ell(stack_y, off):
            B = 2 * c * s * off * (1 / (hx * hx) - 1 / (hy * hy))
            if stack_y:
                A = c * c / (hx * hx) + s * s / (hy * hy)
                C = off * off * (s * s / (hx * hx) + c * c / (hy * hy)) - 1
            else:
                A = s * s / (hx * hx) + c * c / (hy * hy)
                C = off * off * (c * c / (hx * hx) + s * s / (hy * hy)) - 1
            disc = B * B - 4 * A * C
            if disc < 0:
                return None
            r = math.sqrt(disc)
            return (-B - r) / (2 * A), (-B + r) / (2 * A)
        rects = [(a, b_, z0, c_, d_, z1) for a, b_, c_, d_ in _strips(cx, cy, ex, ey, 2 * min(hx, hy), ell)]
    else:                                         # plain box
        ex, ey = abs(hx * c) + abs(hy * s), abs(hx * s) + abs(hy * c)

        def rect(stack_y, off):
            if stack_y:
                r1, r2 = _solve_range(c, s * off, hx), _solve_range(-s, c * off, hy)
            else:
                r1, r2 = _solve_range(s, c * off, hx), _solve_range(c, -s * off, hy)
            if r1 is None or r2 is None:
                return None
            lo, hi = max(r1[0], r2[0]), min(r1[1], r2[1])
            return (lo, hi) if hi > lo else None
        rects = [(a, b_, z0, c_, d_, z1) for a, b_, c_, d_ in _strips(cx, cy, ex, ey, 2 * min(hx, hy), rect)]
    if not rects:
        ex, ey = abs(hx * c) + abs(hy * s), abs(hx * s) + abs(hy * c)
        rects = [(cx - ex, cy - ey, z0, cx + ex, cy + ey, z1)]
    return [Box(r, b.t, b.tt, b.tile, b.col, [b.f[0], b.f[1], -1, -1, -1, -1]) for r in rects]


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def box_faces(b):
    x0, y0, z0, x1, y1, z1 = b
    return (
        ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1)),
        ((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0), (0, 0, -1)),
        ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0)),
        ((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (0, 1, 0)),
        ((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (-1, 0, 0)),
        ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0)),
    )


def face_uv(p, n, tile):
    """World-aligned texture mapping (the game uses exactly the same one)."""
    if abs(n[2]) > .5:
        return p[0] / tile, p[1] / tile
    if abs(n[0]) > .5:
        return p[1] / tile, -p[2] / tile
    return p[0] / tile, -p[2] / tile


def shade(n):
    if n[2] > .5:
        return 1.0
    if n[2] < -.5:
        return 0.5
    return 0.68 if abs(n[0]) > .5 else 0.82


def ray_box(o, d, b):
    tmin, tmax, axis, sign = -1e30, 1e30, 0, 1
    for i in range(3):
        if abs(d[i]) < 1e-9:
            if o[i] < b[i] or o[i] > b[i + 3]:
                return None
        else:
            t1 = (b[i] - o[i]) / d[i]
            t2 = (b[i + 3] - o[i]) / d[i]
            sg = -1
            if t1 > t2:
                t1, t2, sg = t2, t1, 1
            if t1 > tmin:
                tmin, axis, sign = t1, i, sg
            if t2 < tmax:
                tmax = t2
            if tmin > tmax:
                return None
    if tmax < 0 or tmin < 0:
        return None
    n = [0, 0, 0]
    n[axis] = sign
    return tmin, tuple(n)


def ray_seg(o, d, a, b):
    """Closest approach of a ray (o, unit d) to the segment a-b -> (distance, s along segment 0..1, t along ray)."""
    o, d, a, b = (np.asarray(v, np.float64) for v in (o, d, a, b))
    u, w = b - a, o - a
    A, B, C, D, E = d @ d, d @ u, u @ u, d @ w, u @ w
    den = A * C - B * B
    s = 0.0 if den < 1e-9 else (A * E - B * D) / den
    s = min(1.0, max(0.0, s))
    t = max(0.0, (B * s - D) / A)
    return float(np.linalg.norm((o + d * t) - (a + u * s))), s, t


def line_param(o, d, p, u):
    """Where along the infinite line p + u*s (u unit) the ray (o, d) comes closest -> s, or None if parallel."""
    o, d, p, u = (np.asarray(v, np.float64) for v in (o, d, p, u))
    w = o - p
    B, D, E = d @ u, d @ w, u @ w
    den = 1.0 - B * B
    if den < 1e-6:
        return None
    return float((E - B * D) / den)


def perspective(fovy, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fovy) / 2)
    m = np.zeros((4, 4), np.float32)
    m[0, 0], m[1, 1] = f / aspect, f
    m[2, 2], m[2, 3] = (far + near) / (near - far), 2 * far * near / (near - far)
    m[3, 2] = -1
    return m


def look_at(eye, f, up=(0, 0, 1)):
    f = np.array(f, np.float64)
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float64)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[:3, 3] = -m[:3, :3] @ np.array(eye, np.float64)
    return m.astype(np.float32)


def snap(v, g):
    return round(v / g) * g


# --------------------------------------------------------------------------
# File dialogs (tkinter, optional)
# --------------------------------------------------------------------------
def _tk_dialog(fn):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        return fn(filedialog)
    finally:
        root.destroy()


def ask_open(kinds, title="Open"):
    r = _tk_dialog(lambda fd: fd.askopenfilename(title=title, filetypes=kinds + [("All files", "*.*")],
                                                 initialdir=str(HERE)))
    return r or None


def ask_open_many(kinds, title="Add images"):
    r = _tk_dialog(lambda fd: fd.askopenfilenames(title=title, filetypes=kinds + [("All files", "*.*")],
                                                  initialdir=str(HERE)))
    return list(r) if r else []


def ask_save(default, ext, title="Save as"):
    r = _tk_dialog(lambda fd: fd.asksaveasfilename(title=title, defaultextension=ext, initialfile=default,
                                                   initialdir=str(HERE), filetypes=[("Map", "*" + ext)]))
    return r or None


IMG_KINDS = [("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.tga *.webp *.vtf")]


# --------------------------------------------------------------------------
# The editor
# --------------------------------------------------------------------------
class App:
    def __init__(self, size=(1360, 800)):
        pygame.init()
        try:
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 1)
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 4)
        except Exception:
            pass
        flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.RESIZABLE
        try:
            pygame.display.set_mode(size, flags, vsync=1)
        except Exception:
            try:
                pygame.display.set_mode(size, flags)
            except Exception:
                pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 0)
                pygame.display.set_mode(size, flags)
        pygame.display.set_caption("FAD-MOD Map Builder")
        pygame.font.init()
        self.fonts, self.ttex = {}, {}
        self.W, self.H = pygame.display.get_window_size()
        self.doc = MapDoc()
        self.doc.new_default()
        self.undo, self.redo = [], []
        self.sky_list = None
        self.cube_gid = None             # GL cube map of the skybox faces
        self.cube_dirty = True
        self.texdrag = None              # (texture index, key mods) while dragging a thumbnail
        self.tool = BOX
        self.gi = 2                      # grid index
        self.height = 128.0              # height of new boxes
        self.tile = 128.0                # texture size of new boxes
        self.cur_tex = -1                # texture for new boxes
        self.ramp_dir = 0                # uphill direction of new ramps
        self.lad_dir = 0                 # side a new ladder is climbed from (0 +x, 1 +y, 2 -x, 3 -y)
        self.turn_i = 1                  # index into TURN_SNAPS (15 degrees)
        self.gz_hover = False            # mouse is over the spin ring / arrow
        self.st_hover = None             # (axis, hi) of the stretch arrow under the mouse
        self.mv_hover = None             # axis (0 x, 1 y, 2 z) of the move arrow under the mouse (select tool)
        self.clip = None                 # copied objects: [("box", Box) / ("spawn", Spawn), ...]
        self.extra = []                  # more selected objects (Ctrl+click): [("box", i) / ("spawn", i), ...]
        self.sel = None                  # ("box", i) / ("spawn", i)
        self.dsel = None                 # index of the selected decal (decal tool)
        self.dec_hover = None            # decal under the mouse
        self.dec_ghost = None            # preview of the decal a click would place
        self.dec_hh = None               # (s, t) of the resize square under the mouse
        self.dec_size = 128.0            # longest side of new decals (follows your last resize)
        self._dscale_t = 0.0
        self.hover = None
        self.cam = np.array([0.0, -1100.0, 620.0])
        self.yaw, self.pitch = math.pi / 2, -0.52
        self.speed = 700.0
        self.look = False
        self.drag = None
        self.hot = []
        self.mouse = (0, 0)
        self.msgs = []
        self.help = False
        self.tex_scroll = 0
        self.list_id = None
        self.dirty_list = True
        self.white = make_gl_tex(b"\xff\xff\xff\xff" * 4, 2, 2, False)
        self.cursor3d = None
        self.running = True
        self.last = time.perf_counter()
        self.progress = None              # (frac 0..1 or None = indeterminate, label) while loading
        self.msg("Welcome. B = build boxes, add an image on the right, F5 = compile. F1 = help.", 8)

    def set_progress(self, frac, text=""):
        try:
            pygame.event.pump()
        except Exception:
            pass
        """Show a green loading bar. frac = 0..1, or None for a pulsing indeterminate bar."""
        self.progress = (None if frac is None else max(0.0, min(1.0, float(frac))), str(text or ""))
        self.frame()
        pygame.display.flip()

    def clear_progress(self):
        self.progress = None

    # ----- small helpers -----
    @property
    def grid(self):
        return GRIDS[self.gi]

    @property
    def sel(self):
        return self._sel

    @sel.setter
    def sel(self, v):
        self._sel = v
        self.extra = []                  # setting a single selection drops the extra (Ctrl+click) ones

    def sel_all(self):
        """Every selected object as (kind, index, obj): the main one first, then the Ctrl+click ones."""
        out = []
        for k, i in ([self._sel] if self._sel else []) + list(self.extra):
            lst = self.doc.boxes if k == "box" else self.doc.spawns
            if 0 <= i < len(lst):
                out.append((k, i, lst[i]))
        return out

    def delete_members(self, members):
        for kind in ("box", "spawn"):
            lst = self.doc.boxes if kind == "box" else self.doc.spawns
            for i in sorted({i for k, i, _o in members if k == kind}, reverse=True):
                lst.pop(i)

    def msg(self, text, secs=4.0):
        self.msgs = [(text, time.perf_counter() + secs)]

    def draw_progress_bar(self):
        """Centered green loading bar overlay (compile / save / import / open)."""
        if not self.progress:
            return
        frac, label = self.progress
        W, H = self.W, self.H
        bw, bh = min(520, max(280, W // 2)), 28
        bx, by = (W - bw) // 2, H // 2 - 20
        # dim backdrop
        self.rect(0, 0, W, H, (0.0, 0.0, 0.0, 0.45))
        # panel
        pad = 18
        ph = bh + 56
        self.rect(bx - pad, by - 36, bw + 2 * pad, ph, (0.08, 0.09, 0.10, 0.96))
        self.outline(bx - pad, by - 36, bw + 2 * pad, ph, (0.22, 0.55, 0.32, 1.0), 2.0)
        title = label or "Loading..."
        self.text(title[:70], W / 2, by - 28, 15, (220, 235, 220), "c")
        # track
        self.rect(bx, by, bw, bh, (0.12, 0.14, 0.13, 1.0))
        self.outline(bx, by, bw, bh, (0.25, 0.35, 0.28, 1.0), 1.0)
        # fill (green)
        if frac is None:
            # indeterminate: sliding chunk
            t = time.perf_counter()
            chunk = max(40, bw // 4)
            xoff = int((math.sin(t * 2.4) * 0.5 + 0.5) * (bw - chunk))
            self.rect(bx + xoff, by + 2, chunk, bh - 4, (0.22, 0.78, 0.35, 1.0))
            pct = ""
        else:
            fw = max(0, int(round(frac * (bw - 4))))
            if fw > 0:
                self.rect(bx + 2, by + 2, fw, bh - 4, (0.22, 0.78, 0.35, 1.0))
                # brighter edge on the leading end
                if fw > 6:
                    self.rect(bx + 2 + fw - 4, by + 2, 4, bh - 4, (0.45, 0.95, 0.55, 1.0))
            pct = "  %d%%" % int(round(frac * 100))
        self.text(pct.strip() or "...", W / 2, by + 5, 14, (255, 255, 255), "c")

    def push_undo(self):
        self.undo.append(self.doc.snap())
        if len(self.undo) > 200:
            self.undo.pop(0)
        self.redo = []
        self.doc.dirty = True
        self.dirty_list = True

    def do_undo(self):
        if self.undo:
            self.redo.append(self.doc.snap())
            self.doc.restore(self.undo.pop())
            self.sel = None
            self.dsel = None
            self.dirty_list = True
            self.msg("Undo")

    def do_redo(self):
        if self.redo:
            self.undo.append(self.doc.snap())
            self.doc.restore(self.redo.pop())
            self.sel = None
            self.dsel = None
            self.dirty_list = True
            self.msg("Redo")

    def sel_obj(self):
        if not self.sel:
            return None
        kind, i = self.sel
        if kind == "box":
            lst = self.doc.boxes
        elif kind == "spawn":
            lst = self.doc.spawns
        else:
            return None
        return lst[i] if 0 <= i < len(lst) else None

    # ----- camera / rays -----
    def basis(self):
        cp = math.cos(self.pitch)
        f = np.array([cp * math.cos(self.yaw), cp * math.sin(self.yaw), math.sin(self.pitch)])
        r = np.cross(f, (0, 0, 1.0))
        r /= np.linalg.norm(r)
        u = np.cross(r, f)
        return f, r, u

    FOV = 70.0

    def ray(self, pos):
        f, r, u = self.basis()
        t = math.tan(math.radians(self.FOV) / 2)
        nx = (2 * pos[0] / self.W - 1) * t * (self.W / max(1, self.H))
        ny = (1 - 2 * pos[1] / self.H) * t
        d = f + r * nx + u * ny
        d /= np.linalg.norm(d)
        return self.cam.copy(), d

    def in_view(self, pos):
        return LW <= pos[0] < self.W - RW and 0 <= pos[1] < self.H - SB

    def pick_all(self, pos, want_spawns=True):
        """Nearest thing under the mouse: (t, kind, index, normal). Ground plane z=0 is ("ground", -1)."""
        o, d = self.ray(pos)
        best = None
        zone_ok = self.tool in (SELECT, ROT, STRETCH)      # zones are invisible blocks: only the editing tools can grab them
        for i, b in enumerate(self.doc.boxes):
            if (b.px or b.lad) and not zone_ok:
                continue
            h = ray_box(rot_point(b, o, True), rot_vec(b, d, True), b.b)     # spun boxes: ray in the box's own frame
            if h and (best is None or h[0] < best[0]):
                best = (h[0], "box", i, h[1])
        if self.doc.bsp is not None:
            h = self.doc.bsp.ray(o, d)
            if h and (best is None or h[0] < best[0]):
                best = (h[0], "bsp", -1, h[1])
        if want_spawns:
            for i, s in enumerate(self.doc.spawns):
                h = ray_box(o, d, (s.x - 16, s.y - 16, s.z, s.x + 16, s.y + 16, s.z + HULL_H))
                if h and (best is None or h[0] < best[0]):
                    best = (h[0], "spawn", i, h[1])
        if d[2] < -1e-6 and o[2] > 0 and self.doc.bsp is None:
            t = -o[2] / d[2]
            if best is None or t < best[0]:
                best = (t, "ground", -1, (0, 0, 1))
        return best, o, d

    @staticmethod
    def plane_point(o, d, z):
        if abs(d[2]) < 1e-6:
            return None
        t = (z - o[2]) / d[2]
        if t < 0 or t > 60000:
            return None
        return o + d * t

    # ----- textures -----
    def add_texture(self, path, apply=True):
        try:
            s = load_image(path)
        except Exception as ex:
            self.msg("Cannot read that image: %s" % str(ex)[:60])
            return -1
        t = Tex(Path(path).name, s)
        t.upload()
        self.doc.tex.append(t)
        self.cur_tex = len(self.doc.tex) - 1
        if apply:
            self.apply_texture(self.cur_tex, 0)
        self.msg("Added texture %s" % t.name)
        return self.cur_tex

    def set_sky(self, path):
        if self.try_cube_image(path):                     # a cross / strip picture is a cubemap, not a panorama
            return
        try:
            surf = load_image(path, 2048)
        except Exception as ex:
            self.msg("Cannot read that image: %s" % str(ex)[:60])
            return
        if self.doc.sky is not None and self.doc.sky.gid:
            gl.glDeleteTextures([self.doc.sky.gid])
        self.drop_cube()
        t = Tex(Path(path).name, surf)
        t.upload()
        self.doc.sky = t
        self.doc.dirty = True
        self.msg("Skybox: %s" % t.name)

    def drop_cube(self):
        """Forget the cubemap faces (and its GL texture)."""
        self.doc.cube = [None] * 6
        self.reset_cube_gl()

    def reset_cube_gl(self):
        if self.cube_gid is not None:
            try:
                gl.glDeleteTextures([self.cube_gid])
            except Exception:
                pass
        self.cube_gid = None
        self.cube_dirty = True

    def try_cube_image(self, path):
        """If the image is one picture with all 6 faces (cross / strip): use it as the cubemap. True if it was."""
        try:
            surf = load_image(path, 8192)
        except Exception:
            return False
        parts = split_cube_image(surf)
        if parts is None:
            return False
        if self.doc.sky is not None:
            if self.doc.sky.gid:
                gl.glDeleteTextures([self.doc.sky.gid])
            self.doc.sky = None
        name = Path(path).stem
        for i, p in enumerate(parts):
            if p.get_width() > 1024:
                p = pygame.transform.smoothscale(p, (1024, 1024))
            self.doc.cube[i] = Tex("%s_%s" % (name, CUBE_FACES[i]), p)
        self.reset_cube_gl()
        self.doc.dirty = True
        self.msg("Cubemap skybox ready (cut from one image)", 6)
        return True

    def set_cube_face(self, i, path):
        try:
            surf = load_image(path, 1024)
        except Exception as ex:
            self.msg("Cannot read that image: %s" % str(ex)[:60])
            return False
        if self.doc.sky is not None:                      # a cubemap replaces the panorama
            if self.doc.sky.gid:
                gl.glDeleteTextures([self.doc.sky.gid])
            self.doc.sky = None
        self.doc.cube[i] = Tex(Path(path).name, surf)
        self.reset_cube_gl()
        self.doc.dirty = True
        have = sum(1 for t in self.doc.cube if t)
        if have == 6:
            self.msg("Cubemap skybox ready (6/6 faces)", 6)
        else:
            miss = [CUBE_FACES[k] for k in range(6) if not self.doc.cube[k]]
            self.msg("Cube face %s set (%d/6). Still needed: %s" % (CUBE_FACES[i], have, " ".join(miss)), 8)
        return True

    def pick_cube(self):
        paths = ask_open_many(IMG_KINDS, "Pick the cubemap faces (px nx py ny pz nz)")
        if not paths:
            if _tk_dialog(lambda fd: True) is None:
                self.msg("No file dialog here: drop the face images onto the sky instead.")
            return
        if len(paths) == 1 and self.try_cube_image(paths[0]):
            return
        named = [(face_index(p), p) for p in paths]
        if len(paths) == 6 and (any(i is None for i, _ in named) or len({i for i, _ in named}) < 6):
            named = [(i, p) for i, p in enumerate(paths)]          # no usable names: take them in the order picked
            self.msg("Faces taken in the order you picked them: " + " ".join(CUBE_FACES), 8)
        ok = 0
        for i, p in named:
            if i is None:
                continue
            ok += self.set_cube_face(i, p)
        if not ok:
            self.msg("No face names found. Name the files px nx py ny pz nz (or pick exactly 6 files).", 8)

    def cube_ready(self):
        return all(self.doc.cube)

    def clear_sky(self):
        if self.doc.sky is not None:
            if self.doc.sky.gid:
                gl.glDeleteTextures([self.doc.sky.gid])
            self.doc.sky = None
            self.doc.dirty = True
            self.msg("Skybox removed")
        if any(self.doc.cube):
            self.drop_cube()
            self.doc.dirty = True
            self.msg("Skybox removed")

    def draw_cube_sky(self):
        if self.cube_gid is None or self.cube_dirty:
            if self.cube_gid is not None:
                gl.glDeleteTextures([self.cube_gid])
            self.cube_gid = make_gl_cube(self.doc.cube, cube_size(self.doc.cube))
            self.cube_dirty = False
        R = 1000.0
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glEnable(gl.GL_TEXTURE_CUBE_MAP)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, self.cube_gid)
        gl.glColor3f(1, 1, 1)
        gl.glPushMatrix()
        gl.glTranslatef(float(self.cam[0]), float(self.cam[1]), float(self.cam[2]))
        gl.glBegin(gl.GL_QUADS)
        for n, u, v in (((1, 0, 0), (0, 1, 0), (0, 0, 1)), ((-1, 0, 0), (0, 1, 0), (0, 0, 1)),
                        ((0, 1, 0), (1, 0, 0), (0, 0, 1)), ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
                        ((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((0, 0, -1), (1, 0, 0), (0, 1, 0))):
            for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                x, y, z = (n[k] + su * u[k] + sv * v[k] for k in range(3))
                gl.glTexCoord3f(x, z, -y)               # world (z up) -> cube map direction (y up)
                gl.glVertex3f(x * R, y * R, z * R)
        gl.glEnd()
        gl.glPopMatrix()
        gl.glDisable(gl.GL_TEXTURE_CUBE_MAP)
        gl.glEnable(gl.GL_DEPTH_TEST)

    def pick_sky(self):
        p = ask_open(IMG_KINDS, "Skybox image")
        if p:
            self.set_sky(p)
        elif _tk_dialog(lambda fd: True) is None:
            self.msg("No file dialog here: drop an image on the empty sky instead.")

    def draw_sky(self):
        if self.cube_ready():
            self.draw_cube_sky()
            return
        sky = self.doc.sky
        if sky is None or not sky.gid:
            return
        if self.sky_list is None:
            self.sky_list = sky_sphere_list(2000.0)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glBindTexture(gl.GL_TEXTURE_2D, sky.gid)
        gl.glColor3f(1, 1, 1)
        gl.glPushMatrix()
        gl.glTranslatef(float(self.cam[0]), float(self.cam[1]), float(self.cam[2]))
        gl.glCallList(self.sky_list)
        gl.glPopMatrix()
        gl.glEnable(gl.GL_DEPTH_TEST)

    def apply_texture(self, ti, mods=0):
        """Make ti the current texture; if a box is selected, put it on that box (shift = top only, ctrl = sides only)."""
        self.cur_tex = ti
        if self.tool == DECAL:                   # in the decal tool a picture never textures a box
            return
        obj = self.sel_obj()
        if self.sel and self.sel[0] == "box" and obj is not None:
            self.push_undo()
            if mods & pygame.KMOD_SHIFT:
                obj.tt = ti
            elif mods & pygame.KMOD_CTRL:
                obj.t = ti
            else:
                obj.t = obj.tt = ti
                obj.f = [-1] * 6
            self.dirty_list = True

    FACE_OF = {(0, 0, 1): 0, (0, 0, -1): 1, (0, -1, 0): 2, (0, 1, 0): 3, (-1, 0, 0): 4, (1, 0, 0): 5}
    FACE_NAMES = ("top", "bottom", "front", "back", "left", "right")

    def texture_face_at(self, ti, pos):
        """Put texture ti on the single box face under screen position pos. Returns True if a face was hit."""
        if ti < 0 or not self.in_view(pos):
            return False
        hit, o, d = self.pick_all(pos, False)
        if not hit or hit[1] != "box":
            return False
        fi = self.FACE_OF.get(tuple(hit[3]))
        if fi is None:
            return False
        self.push_undo()
        box = self.doc.boxes[hit[2]]
        box.f[fi] = ti
        self.sel = ("box", hit[2])
        self.cur_tex = ti
        self.dirty_list = True
        self.msg("Textured the %s face of box %d" % (self.FACE_NAMES[fi], hit[2] + 1))
        return True

    # ----- decals -----
    def sel_decal(self):
        i = self.dsel
        return self.doc.decals[i] if (i is not None and 0 <= i < len(self.doc.decals)) else None

    def decal_aspect(self, ti):
        t = self.doc.tex[ti] if 0 <= ti < len(self.doc.tex) else None
        if t is None:
            return 1.0
        w, h = t.surf.get_size()
        return w / max(1, h)

    def decal_step(self):
        return max(2.0, self.grid / 4.0)

    def clamp_decal(self, dc):
        """Keep a decal inside the face it was put on (when it fits)."""
        if not dc.hb:
            return
        h0, h1, g0, g1 = dc.hb
        w, h = dc.c1 - dc.c0, dc.v1 - dc.v0
        if w <= h1 - h0:
            dc.c0 = min(max(dc.c0, h0), h1 - w)
            dc.c1 = dc.c0 + w
        if h <= g1 - g0:
            dc.v0 = min(max(dc.v0, g0), g1 - h)
            dc.v1 = dc.v0 + h

    def make_decal(self, ti, hit, o, d):
        """A new decal with picture ti on the face the ray hit -> (Decal or None, reason if None)."""
        if ti < 0 or ti >= len(self.doc.tex):
            return None, "Pick an image on the right first (or drag one onto a wall)"
        if not hit or hit[1] != "box":
            return None, "Click on a wall or a floor to put the decal there"
        box = self.doc.boxes[hit[2]]
        fi = self.FACE_OF.get(tuple(hit[3]))
        if fi is None or fi == 1:
            return None, "Decals go on walls and floors, not on the underside"
        if box.k:
            return None, "Decals go on plain boxes (not slopes or circles)"
        if box.yaw:
            return None, "Decals need a straight box: this one is spun"
        na, ha, va, sg = DEC_AXES[fi]
        b = box.b
        p = b[na + 3] if sg > 0 else b[na]
        asp = self.decal_aspect(ti)
        size = max(DEC_MIN, self.dec_size)
        w, h = (size, size / asp) if asp >= 1 else (size * asp, size)
        w, h = max(DEC_MIN, w), max(DEC_MIN, h)
        q = o + d * hit[0]
        st = self.decal_step()
        c0 = snap(float(q[ha]), st) - w / 2
        v0 = snap(float(q[va]), st) - h / 2
        dc = Decal(ti, fi, p, c0, c0 + w, v0, v0 + h, box.col, [b[ha], b[ha + 3], b[va], b[va + 3]])
        self.clamp_decal(dc)
        return dc, ""

    def place_decal(self, ti, pos):
        """Stick picture ti on the wall / floor under screen position pos. True if it was placed."""
        if not self.in_view(pos):
            return False
        hit, o, d = self.pick_all(pos, False)
        dc, why = self.make_decal(ti, hit, o, d)
        if dc is None:
            self.msg(why)
            return False
        self.push_undo()
        self.doc.decals.append(dc)
        self.dsel = len(self.doc.decals) - 1
        self.cur_tex = ti
        self.msg("Decal placed. Drag it to move it, drag a corner square to resize.", 5)
        return True

    def decal_set_tex(self, ti):
        """Click on a picture in the right panel while a decal is selected: swap its picture."""
        self.cur_tex = ti
        dc = self.sel_decal()
        if dc is None or ti < 0 or ti >= len(self.doc.tex) or ti == dc.tex:
            return
        self.push_undo()
        cx, cy = (dc.c0 + dc.c1) / 2, (dc.v0 + dc.v1) / 2
        size = max(dc.c1 - dc.c0, dc.v1 - dc.v0)
        asp = self.decal_aspect(ti)
        w, h = (size, size / asp) if asp >= 1 else (size * asp, size)
        w, h = max(DEC_MIN, w), max(DEC_MIN, h)
        dc.tex = ti
        dc.c0, dc.c1, dc.v0, dc.v1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
        self.clamp_decal(dc)
        self.msg("Picture swapped")

    def decal_hit_plane(self, dc, o, d, off=0.0):
        """Where a ray meets the decal's plane -> (t, across, up) or None."""
        na, ha, va, sg = DEC_AXES[dc.fi]
        if abs(d[na]) < 1e-6:
            return None
        t = (dc.p + sg * off - o[na]) / d[na]
        if t <= 0:
            return None
        q = o + d * t
        return float(t), float(q[ha]), float(q[va])

    def decal_pick(self, pos):
        """Nearest decal under the mouse -> (t, index) or None."""
        o, d = self.ray(pos)
        best = None
        for i, dc in enumerate(self.doc.decals):
            r = self.decal_hit_plane(dc, o, d, 0.3)
            if r and dc.c0 <= r[1] <= dc.c1 and dc.v0 <= r[2] <= dc.v1 and (best is None or r[0] < best[0]):
                best = (r[0], i)
        return best

    def decal_handles(self):
        """The four resize squares on the corners of the selected decal."""
        dc = self.sel_decal()
        if dc is None or self.tool != DECAL:
            return []
        out = []
        for s, t in ((0, 0), (1, 0), (1, 1), (0, 1)):
            P = np.array(dc.corner(s, t, 0.6), np.float64)
            r = max(5.0, float(np.linalg.norm(self.cam - P)) * 0.018)
            out.append({"s": s, "t": t, "P": P, "r": r})
        return out

    def decal_handle_pick(self, pos):
        if not self.in_view(pos):
            return None
        o, d = self.ray(pos)
        best = None
        for h in self.decal_handles():
            dist, _, t = ray_seg(o, d, h["P"], h["P"])
            if dist <= h["r"] * 1.6 and (best is None or t < best[0]):
                best = (t, h)
        return best[1] if best else None

    def decal_down(self, pos):
        """Left click in the decal tool: grab a resize square, grab a decal, or place a new one."""
        dc = self.sel_decal()
        h = self.decal_handle_pick(pos) if dc is not None else None
        o, d = self.ray(pos)
        if h is not None:
            fs, ft = 1 - h["s"], 1 - h["t"]
            self.drag = {"kind": "dresize", "i": self.dsel, "z": 0.0, "pushed": False, "key": (h["s"], h["t"]),
                         "b0": [dc.c0, dc.c1, dc.v0, dc.v1],
                         "fh": dc.hv(fs), "fv": dc.vv(ft),
                         "sh": 1.0 if dc.hv(h["s"]) > dc.hv(fs) else -1.0,
                         "sv": 1.0 if h["t"] else -1.0,
                         "asp": (dc.c1 - dc.c0) / max(1e-6, dc.v1 - dc.v0)}
            return
        hit = self.pick_all(pos, False)[0]
        dp = self.decal_pick(pos)
        if dp is not None and (hit is None or dp[0] <= hit[0] + 1.0):
            self.dsel = dp[1]
            dc = self.doc.decals[self.dsel]
            r = self.decal_hit_plane(dc, o, d, 0.3)
            if r:
                self.drag = {"kind": "dmove", "i": self.dsel, "z": 0.0, "pushed": False,
                             "b0": [dc.c0, dc.c1, dc.v0, dc.v1], "start": (r[1], r[2])}
            return
        if hit and hit[1] == "box":
            if self.place_decal(self.cur_tex, pos):
                dc = self.sel_decal()
                r = self.decal_hit_plane(dc, o, d, 0.3)
                if r:        # keep the button down and you can slide the new decal into place
                    self.drag = {"kind": "dmove", "i": self.dsel, "z": 0.0, "pushed": True,
                                 "b0": [dc.c0, dc.c1, dc.v0, dc.v1], "start": (r[1], r[2])}
        else:
            self.dsel = None
            self.msg("Click on a wall or a floor to put the decal there")

    def decal_motion(self, dr, o, d):
        i = dr["i"]
        if not (0 <= i < len(self.doc.decals)):
            return
        dc = self.doc.decals[i]
        r = self.decal_hit_plane(dc, o, d, 0.3)
        if r is None:
            return
        free = bool(pygame.key.get_mods() & pygame.KMOD_SHIFT)
        st = self.decal_step()
        b0 = dr["b0"]
        if dr["kind"] == "dmove":
            dh, dv = r[1] - dr["start"][0], r[2] - dr["start"][1]
            if not free:
                dh, dv = snap(dh, st), snap(dv, st)
            nb = [b0[0] + dh, b0[1] + dh, b0[2] + dv, b0[3] + dv]
        else:
            nw = (r[1] - dr["fh"]) * dr["sh"]
            nh = (r[2] - dr["fv"]) * dr["sv"]
            if free:                                        # Shift: stretch the picture
                w, h = max(DEC_MIN, nw), max(DEC_MIN, nh)
            else:                                           # keep the picture shape
                asp = dr["asp"]
                w = max(nw, nh * asp)
                w = max(DEC_MIN * max(1.0, asp), snap(w, st))
                h = w / asp
            c0, c1 = (dr["fh"], dr["fh"] + w) if dr["sh"] > 0 else (dr["fh"] - w, dr["fh"])
            v0, v1 = (dr["fv"], dr["fv"] + h) if dr["sv"] > 0 else (dr["fv"] - h, dr["fv"])
            nb = [c0, c1, v0, v1]
        if not dr["pushed"]:
            if max(abs(a - b) for a, b in zip(nb, b0)) < 1e-6:
                return
            dc.c0, dc.c1, dc.v0, dc.v1 = b0
            self.push_undo()                                # the undo step keeps the original
            dr["pushed"] = True
        dc.c0, dc.c1, dc.v0, dc.v1 = nb
        if dr["kind"] == "dmove":
            self.clamp_decal(dc)
        else:
            self.dec_size = max(nb[1] - nb[0], nb[3] - nb[2])

    def decal_scale(self, f):
        dc = self.sel_decal()
        if dc is None:
            return
        now = time.perf_counter()
        if now - self._dscale_t > 0.8:
            self.push_undo()                                # one undo step for a whole run of wheel clicks
        self._dscale_t = now
        cx, cy = (dc.c0 + dc.c1) / 2, (dc.v0 + dc.v1) / 2
        w, h = (dc.c1 - dc.c0) * f, (dc.v1 - dc.v0) * f
        k = max(1.0, DEC_MIN / max(1e-6, min(w, h)))
        w, h = w * k, h * k
        dc.c0, dc.c1, dc.v0, dc.v1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
        self.dec_size = max(w, h)
        self.msg("Decal %d x %d" % (w, h), 1.5)

    def decal_flip(self, vertical):
        dc = self.sel_decal()
        if dc is None:
            return
        self.push_undo()
        if vertical:
            dc.fy = not dc.fy
        else:
            dc.fx = not dc.fx
        self.msg("Decal flipped " + ("up/down" if vertical else "left/right"), 2)

    def decal_opacity(self, delta):
        dc = self.sel_decal()
        if dc is None:
            return
        self.push_undo()
        dc.op = max(0.05, min(1.0, round(dc.op + delta, 2)))
        self.msg("Decal opacity %d%%" % round(dc.op * 100), 1.5)

    def decal_duplicate(self):
        dc = self.sel_decal()
        if dc is None:
            return
        self.push_undo()
        c = dc.copy()
        s = max(8.0, float(self.grid))
        c.c0 += s
        c.c1 += s
        self.clamp_decal(c)
        self.doc.decals.append(c)
        self.dsel = len(self.doc.decals) - 1
        self.msg("Decal copied")

    def decal_key(self, k, mods, ctrl):
        """Keys in the decal tool. True if the key was used."""
        dc = self.sel_decal()
        dr = self.drag
        if k == pygame.K_ESCAPE:
            if dr and dr.get("kind") in ("dmove", "dresize"):      # cancel a move / resize in progress
                self.drag = None
                if dr["pushed"]:
                    if 0 <= dr["i"] < len(self.doc.decals):
                        e = self.doc.decals[dr["i"]]
                        e.c0, e.c1, e.v0, e.v1 = dr["b0"]
                    if self.undo:
                        self.undo.pop()
                return True
            if dc is not None:
                self.dsel = None
                return True
            return False
        if dc is None or (ctrl and k != pygame.K_d):
            return False
        if ctrl and k == pygame.K_d:
            self.decal_duplicate()
            return True
        if k in (pygame.K_DELETE, pygame.K_BACKSPACE):
            self.push_undo()
            self.doc.decals.pop(self.dsel)
            self.dsel = None
            return True
        if k == pygame.K_h:
            self.decal_flip(False)
            return True
        if k == pygame.K_j:
            self.decal_flip(True)
            return True
        if k == pygame.K_PAGEUP:
            self.decal_opacity(0.1)
            return True
        if k == pygame.K_PAGEDOWN:
            self.decal_opacity(-0.1)
            return True
        if k in (pygame.K_MINUS, pygame.K_KP_MINUS):
            self.decal_scale(1 / 1.1)
            return True
        if k in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
            self.decal_scale(1.1)
            return True
        return False

    def draw_decal_quad(self, dc, off, alpha=1.0):
        tex = self.doc.tex[dc.tex] if 0 <= dc.tex < len(self.doc.tex) else None
        sh = shade(DEC_NORMAL[dc.fi])
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex.gid if tex else self.white)
        alpha *= dc.op
        if tex:
            gl.glColor4f(sh, sh, sh, alpha)
        else:
            gl.glColor4f(dc.col[0] / 255 * sh, dc.col[1] / 255 * sh, dc.col[2] / 255 * sh, alpha)
        gl.glBegin(gl.GL_QUADS)
        for s, t in ((0, 0), (1, 0), (1, 1), (0, 1)):
            gl.glTexCoord2f(1 - s if dc.fx else s, t if dc.fy else 1 - t)
            gl.glVertex3f(*dc.corner(s, t, off))
        gl.glEnd()

    def draw_decals(self):
        if not self.doc.decals:
            return
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
        gl.glPolygonOffset(-2.0, -2.0)
        for i, dc in enumerate(self.doc.decals):
            self.draw_decal_quad(dc, 0.3 + 0.02 * (i % 20))
        gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)

    def decal_outline(self, dc, col, lw, off=0.7):
        gl.glLineWidth(lw)
        gl.glColor4f(*col)
        gl.glBegin(gl.GL_LINE_LOOP)
        for s, t in ((0, 0), (1, 0), (1, 1), (0, 1)):
            gl.glVertex3f(*dc.corner(s, t, off))
        gl.glEnd()
        gl.glLineWidth(1.0)

    def draw_decal_overlay(self):
        gl.glDisable(gl.GL_TEXTURE_2D)
        act = self.drag if (self.drag and self.drag.get("kind") in ("dmove", "dresize")) else None
        if self.dec_ghost is not None and not self.drag:
            gl.glEnable(gl.GL_TEXTURE_2D)
            self.draw_decal_quad(self.dec_ghost, 0.5, 0.6)
            gl.glDisable(gl.GL_TEXTURE_2D)
            self.decal_outline(self.dec_ghost, (0.4, 0.9, 1.0, 1.0), 2.0, 0.6)
        hv = self.dec_hover
        if hv is not None and hv != self.dsel and 0 <= hv < len(self.doc.decals):
            self.decal_outline(self.doc.decals[hv], (1.0, 1.0, 1.0, 0.9), 2.0)
        dc = self.sel_decal()
        if dc is None:
            return
        gl.glDisable(gl.GL_DEPTH_TEST)
        self.decal_outline(dc, (1.0, 0.85, 0.1, 1.0), 3.0, 0.8)
        na, ha, va, sg = DEC_AXES[dc.fi]
        ex, ey = np.zeros(3), np.zeros(3)
        ex[ha] = 1.0
        ey[va] = 1.0
        for h in self.decal_handles():
            key = (h["s"], h["t"])
            hot = (act is not None and act.get("key") == key) or (act is None and self.dec_hh == key)
            r = h["r"] * (1.3 if hot else 1.0)
            P = h["P"]
            pts = [P + ex * a * r + ey * b * r for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            gl.glColor4f(*((1.0, 0.9, 0.2, 1.0) if hot else (1.0, 1.0, 1.0, 1.0)))
            gl.glBegin(gl.GL_QUADS)
            for p in pts:
                gl.glVertex3f(*p)
            gl.glEnd()
            gl.glLineWidth(2.0)
            gl.glColor4f(0.0, 0.0, 0.0, 1.0)
            gl.glBegin(gl.GL_LINE_LOOP)
            for p in pts:
                gl.glVertex3f(*p)
            gl.glEnd()
            gl.glLineWidth(1.0)
        gl.glEnable(gl.GL_DEPTH_TEST)

    # ----- editing -----
    def new_box(self, b):
        self.push_undo()
        nb = Box(b, self.cur_tex, self.cur_tex, self.tile)
        if self.tool == PXZ:
            nb.px = 1
        elif self.tool == LADDER:
            nb.lad = 1
            nb.d = self.lad_dir
        nb.fix()
        self.doc.boxes.append(nb)
        self.sel = ("box", len(self.doc.boxes) - 1)
        if self.tool == LADDER:
            self.msg("Ladder built. It faces %s (the arrow shows where you climb from). R = turn it to the next side." % LAD_DIRS[self.lad_dir], 8)

    def new_shape(self, bounds, kind, name):
        self.push_undo()
        nb = Box(bounds, self.cur_tex, self.cur_tex, self.tile, k=kind, d=self.ramp_dir)
        nb.fix()
        self.doc.boxes.append(nb)
        self.sel = ("box", len(self.doc.boxes) - 1)
        self.msg("Built " + name)

    def shape_bounds(self, dr):
        """Bounding box of the slope / circle being dragged."""
        (ax, ay), (cx, cy) = dr["a"], dr["cur"]
        g = self.grid
        z = dr["z"]
        if dr["tool"] == CIRC:
            r = max(g, snap(math.hypot(cx - ax, cy - ay), g))
            return [ax - r, ay - r, z, ax + r, ay + r, z + self.height]
        x0, x1, y0, y1 = min(ax, cx), max(ax, cx), min(ay, cy), max(ay, cy)
        if x1 - x0 < g:
            x1 = x0 + g
        if y1 - y0 < g:
            y1 = y0 + g
        return [x0, y0, z, x1, y1, z + self.height]

    def nav_axes(self):
        """Arrow keys relative to the camera: returns (forward axis/sign, right axis/sign)."""
        fx, fy = math.cos(self.yaw), math.sin(self.yaw)
        if abs(fx) >= abs(fy):
            fwd = (0, 1 if fx > 0 else -1)
            right = (1, -1 if fx > 0 else 1)
        else:
            fwd = (1, 1 if fy > 0 else -1)
            right = (0, 1 if fy > 0 else -1)
            # right of +y is +x ... right of -y is -x
            right = (0, 1 if fy > 0 else -1)
        return fwd, right

    def set_tool(self, t):
        self.tool = t
        if t == PXZ:
            self.msg("Pixel surf zone: drag a box like a normal box. It is invisible in the game; inside it you can pixel surf. Wheel / - = = height.", 8)
        if t == LADDER:
            self.msg("Ladder: drag a tall thin block against a wall. R = which side you climb from (arrow = where the player stands).", 9)
        if t == DECAL:
            self.sel = None
            self.msg("Decal: pick an image on the right, then CLICK a wall or floor. Drag the corner squares to resize.", 8)
        elif t == ROT:
            self.msg("Spin / turn: select something, then DRAG the arrow or the ring. Shift = free angle.", 7)
        elif t == SELECT:
            self.msg("Select / move: click an object, then DRAG an arrow (red X, green Y, blue Z) to move it. Shift = free.", 7)
        elif t == STRETCH:
            self.msg("Stretch: select a box, then DRAG an arrow on one of its faces. Shift = free size.", 7)

    def turn_ladder(self):
        """R / T for ladders: with the ladder tool, pick the side for new ladders (a selected ladder follows);
        with another tool, turn the selected ladder to its next side. Returns True when it handled the key."""
        obj = self.sel_obj() if (self.sel and self.sel[0] == "box") else None
        lad = obj if (obj is not None and obj.lad) else None
        if self.tool != LADDER and lad is None:
            return False
        if self.tool == LADDER:
            self.lad_dir = (self.lad_dir + 1) % 4
            nd = self.lad_dir
        else:
            nd = (lad.d + 1) % 4
        if lad is not None:
            self.push_undo()
            lad.d = nd
            self.dirty_list = True
        self.msg("Ladder is climbed from the %s side (wall on the other side)" % LAD_DIRS[nd])
        return True

    def cycle_turn_snap(self):
        self.turn_i = (self.turn_i + 1) % len(TURN_SNAPS)
        self.msg("Turn step %d deg" % TURN_SNAPS[self.turn_i])

    def turn_sel(self, sign):
        obj = self.sel_obj()
        if obj is None:
            self.msg("Select a box or spawn first, then turn it")
            return
        st = TURN_SNAPS[self.turn_i]
        self.push_undo()
        obj.yaw = (round((obj.yaw + sign * st) / st) * st) % 360.0
        self.dirty_list = True
        self.msg("Turned to %d deg" % round(obj.yaw))

    def gizmo_info(self):
        """(centre x, centre y, plane z, ring radius, yaw) of the spin ring around the selection."""
        obj = self.sel_obj()
        if obj is None:
            return None
        if self.sel[0] == "box":
            b = obj.b
            R = math.hypot(b[3] - b[0], b[4] - b[1]) / 2 + 24
            return (b[0] + b[3]) / 2, (b[1] + b[4]) / 2, b[2] + 0.5, max(R, 56.0), obj.yaw
        return obj.x, obj.y, obj.z + 0.5, 56.0, obj.yaw

    def gizmo_hit(self, pos):
        """Point on the spin plane if the mouse is on the ring or the arrow, else None."""
        gi = self.gizmo_info()
        if gi is None or not self.in_view(pos):
            return None
        cx, cy, z, R, yaw = gi
        o, d = self.ray(pos)
        p = self.plane_point(o, d, z)
        if p is None:
            return None
        tol = max(18.0, R * 0.08)
        dx, dy = p[0] - cx, p[1] - cy
        if abs(math.hypot(dx, dy) - R) <= tol:
            return p
        ux, uy = yaw_cs(yaw)
        a = dx * ux + dy * uy                      # along the arrow
        lat = abs(dx * uy - dy * ux)               # sideways from it
        L = min(max(40.0, R * 0.35), 220.0)
        if R - tol <= a <= R + L + tol and lat <= tol:
            return p
        return None

    def stretch_handles(self):
        """The six face arrows of the selected box: dicts with axis, hi, base point P, direction D, length L."""
        if not self.sel or self.sel[0] != "box":
            return []
        obj = self.sel_obj()
        if obj is None:
            return []
        b = obj.b
        mid = [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2]
        out = []
        for ax in range(3):
            for hi in (0, 1):
                c = list(mid)
                c[ax] = b[ax + 3] if hi else b[ax]
                dl = [0.0, 0.0, 0.0]
                dl[ax] = 1.0 if hi else -1.0
                P = np.array(rot_point(obj, c), np.float64)
                D = np.array(rot_vec(obj, dl), np.float64)
                L = max(24.0, float(np.linalg.norm(self.cam - P)) * 0.11)
                out.append({"ax": ax, "hi": hi, "P": P, "D": D, "L": L})
        return out

    def stretch_pick(self, pos):
        if not self.in_view(pos):
            return None
        o, d = self.ray(pos)
        best = None
        for h in self.stretch_handles():
            dist, _, t = ray_seg(o, d, h["P"], h["P"] + h["D"] * h["L"])
            if dist <= max(5.0, h["L"] * 0.13) and (best is None or t < best[0]):
                best = (t, h)
        return best[1] if best else None

    def stretch_motion(self, dr, o, d):
        obj = self.sel_obj()
        sv = line_param(o, d, dr["P"], dr["D"])
        if obj is None or sv is None:
            return
        ax, hi, b0 = dr["ax"], dr["hi"], dr["b0"]
        idx = ax + (3 if hi else 0)
        val = b0[idx] + (1 if hi else -1) * (sv - dr["s0"])
        g = self.grid
        if not (pygame.key.get_mods() & pygame.KMOD_SHIFT):
            val = snap(val, g)
        val = max(val, b0[ax] + g) if hi else min(val, b0[ax + 3] - g)
        nb = list(b0)
        nb[idx] = val
        if abs(val - b0[idx]) < 1e-6 and not dr["pushed"]:
            return
        if not dr["pushed"]:
            obj.b = list(b0)
            self.push_undo()
            dr["pushed"] = True
        if obj.yaw and ax < 2:
            # keep the opposite face where it is in the world (the spin centre moves when a face moves)
            fixed = [(b0[0] + b0[3]) / 2, (b0[1] + b0[4]) / 2, (b0[2] + b0[5]) / 2]
            fixed[ax] = b0[ax if hi else ax + 3]
            obj.b = list(b0)
            before = rot_point(obj, fixed)
            obj.b = nb
            after = rot_point(obj, fixed)
            ddx, ddy = before[0] - after[0], before[1] - after[1]
            nb = [nb[0] + ddx, nb[1] + ddy, nb[2], nb[3] + ddx, nb[4] + ddy, nb[5]]
        obj.b = nb
        self.dirty_list = True

    # ----- move arrows (select tool): three world-axis arrows on the selection -----
    def move_handles(self):
        """X / Y / Z arrows at the centre of everything selected: dicts with ax, P, D, L."""
        mem = self.sel_all()
        if not mem:
            return []
        lo, hi = [1e18] * 3, [-1e18] * 3
        for k, _i, ob in mem:
            pts = [(ob.b[0], ob.b[1], ob.b[2]), (ob.b[3], ob.b[4], ob.b[5])] if k == "box" else [(ob.x, ob.y, ob.z)]
            for p in pts:
                for a in range(3):
                    lo[a], hi[a] = min(lo[a], p[a]), max(hi[a], p[a])
        P = np.array([(lo[a] + hi[a]) / 2 for a in range(3)], np.float64)
        L = max(40.0, float(np.linalg.norm(self.cam - P)) * 0.16)
        out = []
        for ax in range(3):
            D = np.zeros(3)
            D[ax] = 1.0
            out.append({"ax": ax, "P": P, "D": D, "L": L})
        return out

    def move_pick(self, pos):
        if not self.in_view(pos):
            return None
        o, d = self.ray(pos)
        best = None
        for h in self.move_handles():
            dist, _, t = ray_seg(o, d, h["P"], h["P"] + h["D"] * h["L"])
            if dist <= max(5.0, h["L"] * 0.10) and (best is None or t < best[0]):
                best = (t, h)
        return best[1] if best else None

    def move_motion(self, dr, o, d):
        sv = line_param(o, d, dr["P"], dr["D"])
        if sv is None:
            return
        delta = sv - dr["s0"]
        if not (pygame.key.get_mods() & pygame.KMOD_SHIFT):
            delta = snap(delta, self.grid)
        if abs(delta) < 1e-6 and not dr["pushed"]:
            return
        if not dr["pushed"]:
            self.push_undo()                        # nothing has moved yet: remember the original places for undo
            dr["pushed"] = True
        ax = dr["ax"]
        for k, i, v in dr["origs"]:
            lst = self.doc.boxes if k == "box" else self.doc.spawns
            if not (0 <= i < len(lst)):
                continue
            ob = lst[i]
            if k == "box":
                nb = list(v)
                nb[ax] += delta
                nb[ax + 3] += delta
                ob.b = nb
            else:
                nv = list(v)
                nv[ax] += delta
                ob.x, ob.y, ob.z = nv
        self.dirty_list = True

    def draw_move(self):
        hs = self.move_handles()
        if not hs:
            return
        act = self.drag if (self.drag and self.drag.get("kind") == "amove") else None
        cols = ((1.0, 0.28, 0.28), (0.3, 0.9, 0.3), (0.38, 0.58, 1.0))
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glDisable(gl.GL_DEPTH_TEST)
        for h in hs:
            hot = (act is not None and act["ax"] == h["ax"]) or (act is None and self.mv_hover == h["ax"])
            col = (1.0, 0.9, 0.2) if hot else cols[h["ax"]]
            P, D, L = h["P"], h["D"], h["L"]
            base, tip = P + D * L * 0.70, P + D * L
            gl.glLineWidth(5.0 if hot else 3.5)
            gl.glColor4f(*col, 1.0)
            gl.glBegin(gl.GL_LINES)
            gl.glVertex3f(*P)
            gl.glVertex3f(*base)
            gl.glEnd()
            ref = np.array([0.0, 0.0, 1.0]) if abs(D[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
            u = np.cross(D, ref)
            u /= np.linalg.norm(u)
            v = np.cross(D, u)
            r = L * (0.11 if hot else 0.09)
            gl.glBegin(gl.GL_TRIANGLES)
            for i in range(12):
                a0, a1 = 2 * math.pi * i / 12, 2 * math.pi * (i + 1) / 12
                p0 = base + (u * math.cos(a0) + v * math.sin(a0)) * r
                p1 = base + (u * math.cos(a1) + v * math.sin(a1)) * r
                gl.glVertex3f(*tip)
                gl.glVertex3f(*p0)
                gl.glVertex3f(*p1)
                gl.glVertex3f(*base)
                gl.glVertex3f(*p1)
                gl.glVertex3f(*p0)
            gl.glEnd()
        gl.glLineWidth(1.0)
        gl.glEnable(gl.GL_DEPTH_TEST)

    def draw_stretch(self):
        hs = self.stretch_handles()
        if not hs:
            return
        act = self.drag if (self.drag and self.drag.get("kind") == "stretch") else None
        cols = ((1.0, 0.28, 0.28), (0.3, 0.9, 0.3), (0.38, 0.58, 1.0))
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glDisable(gl.GL_DEPTH_TEST)
        for h in hs:
            key = (h["ax"], h["hi"])
            hot = (act is not None and (act["ax"], act["hi"]) == key) or (act is None and self.st_hover == key)
            col = (1.0, 0.9, 0.2) if hot else cols[h["ax"]]
            P, D, L = h["P"], h["D"], h["L"]
            base, tip = P + D * L * 0.62, P + D * L
            gl.glLineWidth(5.0 if hot else 3.5)
            gl.glColor4f(*col, 1.0)
            gl.glBegin(gl.GL_LINES)
            gl.glVertex3f(*P)
            gl.glVertex3f(*base)
            gl.glEnd()
            ref = np.array([0.0, 0.0, 1.0]) if abs(D[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
            u = np.cross(D, ref)
            u /= np.linalg.norm(u)
            v = np.cross(D, u)
            r = L * (0.17 if hot else 0.14)
            gl.glBegin(gl.GL_TRIANGLES)
            for i in range(12):
                a0, a1 = 2 * math.pi * i / 12, 2 * math.pi * (i + 1) / 12
                p0 = base + (u * math.cos(a0) + v * math.sin(a0)) * r
                p1 = base + (u * math.cos(a1) + v * math.sin(a1)) * r
                gl.glVertex3f(*tip)
                gl.glVertex3f(*p0)
                gl.glVertex3f(*p1)
                gl.glVertex3f(*base)
                gl.glVertex3f(*p1)
                gl.glVertex3f(*p0)
            gl.glEnd()
        gl.glLineWidth(1.0)
        gl.glEnable(gl.GL_DEPTH_TEST)

    def edit_obj(self, kind, obj, key, shift, g, dirs):
        if key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            s = g if key == pygame.K_PAGEUP else -g
            if kind == "spawn":
                obj.z += s
            elif shift:
                obj.b[5] += s
                if obj.b[5] < obj.b[2] + 1:
                    obj.b[5] = obj.b[2] + g
            else:
                obj.b[2] += s
                obj.b[5] += s
        else:
            fwd, right = self.nav_axes()
            code = dirs[key]
            ax, sg = (fwd if code[0] == "f" else right)
            s = sg * (g if code[1] == "+" else -g)
            if kind == "spawn":
                if ax == 0:
                    obj.x += s
                else:
                    obj.y += s
            elif shift:
                # Up/Right grow the face on that side, Down/Left shrink it
                lo, hi = ax, ax + 3
                side = hi if sg * (1 if code[1] == "+" else -1) > 0 else lo
                # the face on the arrow's side moves by +-g
                if code[1] == "+":
                    face = hi if sg > 0 else lo
                    obj.b[face] += g * sg
                else:
                    face = hi if sg > 0 else lo
                    obj.b[face] -= g * sg
                if obj.b[hi] < obj.b[lo] + 1:
                    obj.b[face] = obj.b[lo] + g if face == hi else obj.b[hi] - g
            else:
                if ax == 0:
                    obj.b[0] += s
                    obj.b[3] += s
                else:
                    obj.b[1] += s
                    obj.b[4] += s

    def key_edit(self, key, mods):
        obj = self.sel_obj()
        if obj is None:
            return False
        g = self.grid
        shift = bool(mods & pygame.KMOD_SHIFT)
        dirs = {pygame.K_UP: "f+", pygame.K_DOWN: "f-", pygame.K_RIGHT: "r+", pygame.K_LEFT: "r-"}
        if key in dirs or key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            self.push_undo()
            for kind, _i, o_ in self.sel_all():
                self.edit_obj(kind, o_, key, shift, g, dirs)
            self.dirty_list = True
            return True
        if key == pygame.K_DELETE or key == pygame.K_BACKSPACE:
            self.push_undo()
            self.delete_members(self.sel_all())
            self.sel = None
            return True
        if key == pygame.K_r and self.sel[0] == "spawn":
            self.push_undo()
            obj.yaw = (obj.yaw + (-45 if shift else 45)) % 360
            return True
        return False

    def duplicate(self):
        members = self.sel_all()
        if not members:
            return
        self.push_undo()
        g = self.grid
        made = []
        for kind, _i, obj in members:
            c = obj.copy()
            if kind == "box":
                c.b[0] += g
                c.b[3] += g
                c.b[1] += g
                c.b[4] += g
                self.doc.boxes.append(c)
                made.append(("box", len(self.doc.boxes) - 1))
            else:
                c.x += g
                c.y += g
                self.doc.spawns.append(c)
                made.append(("spawn", len(self.doc.spawns) - 1))
        self.sel = made[0]
        self.extra = made[1:]
        self.dirty_list = True

    def copy_sel(self, cut=False):
        members = self.sel_all()
        if not members:
            self.msg("Select a box or spawn first, then copy it")
            return
        self.clip = [(k, o.copy()) for k, _i, o in members]
        if cut:
            self.push_undo()
            self.delete_members(members)
            self.sel = None
            self.dirty_list = True
            self.msg("Cut. Ctrl+V to paste it")
        else:
            self.msg("Copied. Ctrl+V to paste it where the mouse points")

    def paste(self):
        if not self.clip:
            self.msg("Nothing copied yet: select something and press Ctrl+C")
            return
        g = self.grid
        gg = max(8, g)
        spot = None                                   # (x, y, z) under the mouse, on a floor / box top
        if self.in_view(self.mouse) and not self.look:
            hit, o, d = self.pick_all(self.mouse, False)
            if hit and hit[1] in ("box", "ground", "bsp") and tuple(hit[3]) == (0, 0, 1):
                p = o + d * hit[0]
                spot = (float(p[0]), float(p[1]), float(self.doc.boxes[hit[2]].b[5]) if hit[1] == "box" else (float(p[2]) if hit[1] == "bsp" else 0.0))
        self.push_undo()
        ntex = len(self.doc.tex)
        items = []
        for kind, src in self.clip:
            c = src.copy()
            if kind == "box":
                c.t = c.t if c.t < ntex else -1
                c.tt = c.tt if c.tt < ntex else -1
                c.f = [v if v < ntex else -1 for v in c.f]
            items.append((kind, c))
        # one shift for the whole group, worked out from the first object (so they keep their places to each other)
        k0, c0 = items[0]
        if spot:
            if k0 == "box":
                w, dpt = c0.b[3] - c0.b[0], c0.b[4] - c0.b[1]
                ox, oy, oz = snap(spot[0] - w / 2, g) - c0.b[0], snap(spot[1] - dpt / 2, g) - c0.b[1], spot[2] - c0.b[2]
            else:
                ox, oy, oz = snap(spot[0], gg) - c0.x, snap(spot[1], gg) - c0.y, spot[2] - c0.z
        else:
            step = g if all(k == "box" for k, _c in items) else gg
            ox, oy, oz = step, step, 0.0

        def moved(kind, c, mx, my):
            if kind == "box":
                return [c.b[0] + mx, c.b[1] + my, c.b[2] + oz, c.b[3] + mx, c.b[4] + my, c.b[5] + oz]
            return None

        mx, my = ox, oy
        for _ in range(60):                           # never land exactly on top of identical boxes
            clash = False
            for kind, c in items:
                if kind != "box":
                    continue
                nb = moved(kind, c, mx, my)
                if any(all(abs(a - b) < 0.01 for a, b in zip(nb, o_.b)) for o_ in self.doc.boxes):
                    clash = True
                    break
            if not clash:
                break
            mx, my = mx + g, my + g
        made = []
        for kind, c in items:
            if kind == "box":
                c.b = moved(kind, c, mx, my)
                self.doc.boxes.append(c)
                made.append(("box", len(self.doc.boxes) - 1))
            else:
                c.x, c.y, c.z = c.x + mx, c.y + my, c.z + oz
                self.doc.spawns.append(c)
                made.append(("spawn", len(self.doc.spawns) - 1))
        self.sel = made[0]
        self.extra = made[1:]
        self.dirty_list = True
        self.msg("Pasted")

    def focus_sel(self):
        obj = self.sel_obj()
        if obj is None:
            return
        if self.sel[0] == "box":
            c = np.array([(obj.b[0] + obj.b[3]) / 2, (obj.b[1] + obj.b[4]) / 2, (obj.b[2] + obj.b[5]) / 2])
            r = max(obj.b[3] - obj.b[0], obj.b[4] - obj.b[1], obj.b[5] - obj.b[2], 100)
        else:
            c = np.array([obj.x, obj.y, obj.z + 36])
            r = 120
        f, _, _ = self.basis()
        self.cam = c - f * (r * 1.6 + 150)

    # ----- files -----
    def title(self):
        pygame.display.set_caption("FAD-MOD Map Builder - %s%s" % (self.doc.name, "*" if self.doc.dirty else ""))

    def new_map(self):
        for t in self.doc.tex + ([self.doc.sky] if self.doc.sky else []):
            if t.gid:
                gl.glDeleteTextures([t.gid])
        if self.doc.bsp is not None:
            self.doc.bsp.free_gl()
        self.doc = MapDoc()
        self.doc.new_default()
        self.reset_cube_gl()
        self.undo, self.redo, self.sel, self.cur_tex = [], [], None, -1
        self.dsel = None
        self.dirty_list = True
        self.msg("New map")

    def save(self, as_new=False):
        path = self.doc.path
        if as_new or not path:
            path = ask_save((self.doc.name or "my_map") + ".fadproj", ".fadproj", "Save project")
            if not path:
                if not as_new and not self.doc.path and _tk_dialog(lambda fd: True) is None:
                    path = str(HERE / (self.doc.name + ".fadproj"))     # no file dialog available
                else:
                    return None
        path = str(path)
        self.doc.path = path
        self.doc.name = Path(path).stem
        try:
            self.set_progress(0.15, "Saving project...")
            cam = [float(self.cam[0]), float(self.cam[1]), float(self.cam[2]), self.yaw, self.pitch]
            self.set_progress(0.45, "Encoding textures...")
            data = json.dumps(self.doc.to_dict(False, cam), separators=(",", ":"))
            self.set_progress(0.85, "Writing file...")
            Path(path).write_text(data, encoding="utf-8")
            self.set_progress(1.0, "Saved")
            self.doc.dirty = False
            self.title()
            self.msg("Saved " + path)
            return path
        finally:
            self.clear_progress()

    def import_source(self, path=None):
        """Load a Garry's Mod addon (.gma / Workshop legacy .bin), a Source addon package (.vpk) or a Source map (.bsp): all its geometry, textures and spawns."""
        path = path or ask_open([("Source / GoldSrc map", "*.bin *.bsp *.gma *.vpk")], "Import a map (.bin / .bsp / .gma / .vpk)")
        if not path:
            return

        # Map known import stage messages -> approximate progress fraction
        _STAGE = (
            ("Reading", 0.05),
            ("Reading the map", 0.12),
            ("Building collision", 0.22),
            ("Building the mesh", 0.35),
            ("Reading props", 0.50),
            ("Building", 0.58),
            ("Placing decals", 0.68),
            ("Loading textures", 0.80),
            ("Imported", 0.95),
        )

        def prog(text, frac=None):
            if frac is None:
                for key, f in _STAGE:
                    if text.startswith(key):
                        frac = f
                        break
                else:
                    frac = None
            self.msgs = []
            self.msg(text, 600)
            self.set_progress(frac if frac is not None else None, text)
        try:
            self.set_progress(0.02, "Starting import...")
            bsp, spawns, name = import_source_map(path, prog)
        except Exception as ex:
            self.clear_progress()
            self.msgs = []
            self.msg("Cannot import: %s" % str(ex)[:120], 12)
            print("import failed:", ex)
            import traceback
            traceback.print_exc()
            return
        try:
            self.set_progress(0.97, "Finishing import...")
            for t in self.doc.tex + ([self.doc.sky] if self.doc.sky else []):
                if t.gid:
                    gl.glDeleteTextures([t.gid])
            if self.doc.bsp is not None:
                self.doc.bsp.free_gl()
            doc = MapDoc()
            doc.name = name
            doc.bsp = bsp
            doc.spawns = [Spawn(t, x, y, z, yaw) for t, x, y, z, yaw in spawns]
            lads = list(getattr(bsp, "ladders", None) or [])
            for lb, ld in lads:                      # every ladder of the map becomes a ladder zone you can climb
                doc.boxes.append(Box(lb, lad=1, d=ld))
            doc.dirty = True
            self.doc = doc
            self.reset_cube_gl()
            self.undo, self.redo, self.sel, self.cur_tex = [], [], None, -1
            self.extra, self.dsel = [], None
            b = bsp.bounds
            size = max(b[3] - b[0], b[4] - b[1], 1000.0)
            self.speed = float(min(4000.0, max(700.0, size / 6.0)))
            if spawns:
                _t, x, y, z, yaw = spawns[0]
                self.yaw, self.pitch = math.radians(yaw), -0.25
                f, _r, _u = self.basis()
                self.cam = np.array([x, y, z + 72.0], np.float64) - f * 250.0
            else:
                self.yaw, self.pitch = math.pi / 2, -0.5
                self.cam = np.array([(b[0] + b[3]) / 2, b[1] - size * 0.3, b[5] + size * 0.15], np.float64)
            self.dirty_list = True
            self.title()
            self.msgs = []
            self.msg("Imported %s: %d triangles, %d brushes, %d spawn points, %d ladders. Build on top of it or press F5." % (
                name, len(bsp.idx) // 3, len(bsp.bfirst), len(spawns), len(lads)), 12)
        finally:
            self.clear_progress()

    def open_project(self, path=None):
        path = path or ask_open([("Map project / Source map", "*.fadproj *.fadmap *.bin *.bsp *.gma *.vpk")], "Open map")
        if not path:
            return
        if str(path).lower().endswith((".bin", ".bsp", ".gma", ".vpk")):
            self.import_source(path)
            return
        try:
            self.set_progress(0.1, "Opening project...")
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            self.set_progress(0.4, "Decoding map data...")
            doc = MapDoc()
            cam = doc.from_dict(d)
            self.set_progress(0.7, "Loading textures...")
            for t in self.doc.tex + ([self.doc.sky] if self.doc.sky else []):
                if t.gid:
                    gl.glDeleteTextures([t.gid])
            if self.doc.bsp is not None:
                self.doc.bsp.free_gl()
            for t in doc.tex + ([doc.sky] if doc.sky else []):
                t.upload()
            self.set_progress(0.95, "Almost done...")
            doc.path = path if str(path).endswith(".fadproj") else None
            doc.name = Path(path).stem
            self.doc = doc
            self.reset_cube_gl()
            self.undo, self.redo, self.sel, self.cur_tex = [], [], None, -1
            self.dsel = None
            if cam and len(cam) == 5:
                self.cam = np.array(cam[:3], np.float64)
                self.yaw, self.pitch = cam[3], cam[4]
            self.dirty_list = True
            self.title()
            self.msg("Opened " + Path(path).name)
        except Exception as ex:
            self.msg("Cannot open: %s" % str(ex)[:70])
        finally:
            self.clear_progress()

    def compile(self):
        if not self.doc.path:
            if not self.save():
                self.msg("Save the project first, then compile.")
                return
        out = Path(self.doc.path).with_suffix(".fadmap")
        try:
            self.set_progress(0.05, "Compiling map...")
            def prog(text, frac=None):
                self.set_progress(frac if frac is not None else None, text)
            msgs, warns = compile_doc(self.doc, out, progress=prog)
            self.set_progress(1.0, "Compile done")
        except Exception as ex:
            self.clear_progress()
            self.msg("Compile failed: %s" % ex, 9)
            return
        finally:
            self.clear_progress()
        if warns:
            # auto-made spawn points are kept in the project so you can see and move them
            pass
        txt = "Compiled %s: %s" % (out.name, msgs[0])
        if warns:
            txt += "   (" + "; ".join(warns) + ")"
        self.msg(txt, 14)
        print(txt)
        print("In the game: Play > Load map, then pick", out)

    # ----- events -----
    def handle(self, ev):
        t = ev.type
        if t == pygame.QUIT:
            self.running = False
        elif t == pygame.VIDEORESIZE:
            self.W, self.H = pygame.display.get_window_size()
        elif t == pygame.DROPFILE:
            p = ev.file
            if p.lower().endswith((".fadproj", ".fadmap", ".gma", ".bin", ".vpk", ".bsp")):
                self.open_project(p)
            else:
                pos = getattr(ev, "pos", None)
                if not pos:
                    pos = pygame.mouse.get_pos()
                fi = face_index(p)
                hit = self.pick_all(pos, False)[0] if self.in_view(pos) else None
                on_sky = self.in_view(pos) and (hit is None or hit[1] == "ground")
                if (on_sky or (pos[0] < LW and not self.in_view(pos))) and self.try_cube_image(p):
                    return                                  # one picture with all 6 faces
                if fi is not None and (on_sky or (pos[0] < LW and not self.in_view(pos))):
                    self.set_cube_face(fi, p)        # a cubemap face (px nx py ny pz nz)
                    return
                if self.in_view(pos) and hit is None:
                    self.set_sky(p)          # dropped on the empty sky
                    return
                ti = self.add_texture(p, apply=False)
                if ti >= 0 and self.tool == DECAL and self.in_view(pos):
                    self.place_decal(ti, pos)        # dropped on a wall in the decal tool: a decal
                    return
                if ti >= 0 and not self.texture_face_at(ti, pos):
                    self.msg("Added %s. Drop an image right on a box face to texture just that face." % self.doc.tex[ti].name, 6)
        elif t == pygame.KEYDOWN:
            self.on_key(ev)
        elif t == pygame.MOUSEMOTION:
            self.on_motion(ev)
        elif t == pygame.MOUSEBUTTONDOWN:
            self.on_down(ev)
        elif t == pygame.MOUSEBUTTONUP:
            self.on_up(ev)
        elif t == pygame.MOUSEWHEEL:
            self.on_wheel(ev)

    def on_key(self, ev):
        k, mods = ev.key, pygame.key.get_mods()
        ctrl = bool(mods & pygame.KMOD_CTRL)
        if self.tool == DECAL and self.decal_key(k, mods, ctrl):
            return
        if ctrl and k == pygame.K_s:
            self.save(bool(mods & pygame.KMOD_SHIFT))
        elif ctrl and k == pygame.K_o:
            self.open_project()
        elif ctrl and k == pygame.K_i:
            self.import_source()
        elif ctrl and k == pygame.K_z:
            self.do_undo()
        elif ctrl and k == pygame.K_y:
            self.do_redo()
        elif ctrl and k == pygame.K_d:
            self.duplicate()
        elif ctrl and k == pygame.K_c:
            self.copy_sel()
        elif ctrl and k == pygame.K_x:
            self.copy_sel(cut=True)
        elif ctrl and k == pygame.K_v:
            self.paste()
        elif ctrl and k == pygame.K_n:
            self.new_map()
        elif k == pygame.K_F5:
            self.compile()
        elif k == pygame.K_F1:
            self.help = not self.help
        elif k == pygame.K_ESCAPE:
            if self.drag:
                dr, self.drag = self.drag, None
                if dr.get("kind") in ("rot", "stretch") and dr["pushed"]:       # cancel a spin / stretch in progress
                    obj = self.sel_obj()
                    if obj is not None:
                        if dr["kind"] == "rot":
                            obj.yaw = dr["yaw0"]
                        else:
                            obj.b = list(dr["b0"])
                    if self.undo:
                        self.undo.pop()
                    self.dirty_list = True
            else:
                self.sel = None
        elif k == pygame.K_v:
            self.tool = SELECT
        elif k == pygame.K_b:
            self.tool = BOX
        elif k == pygame.K_8 or k == pygame.K_z:
            self.set_tool(PXZ)
        elif k == pygame.K_9 or k == pygame.K_k:
            self.set_tool(LADDER)
        elif k == pygame.K_1:
            self.tool = SPAWN0
        elif k == pygame.K_2:
            self.tool = SPAWN1
        elif k == pygame.K_3:
            self.tool = RAMP
        elif k == pygame.K_4:
            self.tool = CIRC
        elif k == pygame.K_5 or k == pygame.K_y:
            self.set_tool(ROT)
        elif k == pygame.K_6 or k == pygame.K_x:
            self.set_tool(STRETCH)
        elif k == pygame.K_7 or k == pygame.K_l:
            self.set_tool(DECAL)
        elif k == pygame.K_u:
            self.cycle_turn_snap()
        elif k == pygame.K_COMMA or k == pygame.K_PERIOD:
            self.turn_sel(-1 if k == pygame.K_COMMA else 1)
        elif (k == pygame.K_t or k == pygame.K_r) and self.turn_ladder():
            pass
        elif k == pygame.K_t or (k == pygame.K_r and not (self.sel and self.sel[0] == "spawn")):
            self.ramp_dir = (self.ramp_dir + 1) % 4
            self.msg("Slope goes uphill toward " + RAMP_DIRS[self.ramp_dir])
        elif k == pygame.K_g:
            self.gi = (self.gi + 1) % len(GRIDS)
            self.msg("Grid %d" % self.grid)
        elif k == pygame.K_f:
            self.focus_sel()
        elif k == pygame.K_LEFTBRACKET or k == pygame.K_RIGHTBRACKET:
            self.step_tile(-1 if k == pygame.K_LEFTBRACKET else 1)
        elif k == pygame.K_MINUS or k == pygame.K_KP_MINUS:
            self.height = max(self.grid, self.height - self.grid)
        elif k in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
            self.height += self.grid
        elif self.sel and not ctrl:
            self.key_edit(k, mods)

    def step_tile(self, d):
        obj = self.sel_obj()
        cur = obj.tile if (obj is not None and self.sel[0] == "box") else self.tile
        i = min(range(len(TILES)), key=lambda j: abs(TILES[j] - cur))
        v = TILES[max(0, min(len(TILES) - 1, i + d))]
        if obj is not None and self.sel[0] == "box":
            self.push_undo()
            obj.tile = float(v)
        self.tile = float(v)
        self.dirty_list = True

    def on_wheel(self, ev):
        x, y = self.mouse
        if x >= self.W - RW:
            self.tex_scroll = max(0, self.tex_scroll - ev.y * 40)
        elif self.drag and self.drag.get("kind") == "box":
            self.height = max(self.grid, self.height + ev.y * self.grid)
        elif self.tool == DECAL and not self.look and self.sel_decal() is not None and self.in_view(self.mouse):
            self.decal_scale(1.1 ** ev.y)
        else:
            self.speed = max(80.0, min(6000.0, self.speed * (1.2 ** ev.y)))
            self.msg("Camera speed %d" % self.speed, 1.5)

    def on_down(self, ev):
        self.mouse = ev.pos
        if ev.button == 3:
            self.look = True
            try:
                pygame.mouse.set_visible(False)
                pygame.event.set_grab(True)
                pygame.mouse.set_relative_mode(True)
            except Exception:
                pass
            return
        if ev.button != 1:
            return
        for x, y, w, h, cb in reversed(self.hot):
            if x <= ev.pos[0] < x + w and y <= ev.pos[1] < y + h:
                cb()
                return
        if not self.in_view(ev.pos):
            return
        if self.tool == SELECT:
            if not (pygame.key.get_mods() & pygame.KMOD_CTRL):
                mh = self.move_pick(ev.pos)
                if mh is not None:                   # grabbed a move arrow
                    o, d = self.ray(ev.pos)
                    s0 = line_param(o, d, mh["P"], mh["D"])
                    if s0 is not None:
                        origs = [(k, i, list(ob.b) if k == "box" else [ob.x, ob.y, ob.z]) for k, i, ob in self.sel_all()]
                        self.drag = {"kind": "amove", "ax": mh["ax"], "P": mh["P"], "D": mh["D"], "s0": s0,
                                     "origs": origs, "pushed": False, "z": 0.0}
                    return
            hit, o, d = self.pick_all(ev.pos)
            if pygame.key.get_mods() & pygame.KMOD_CTRL:
                # Ctrl + click: add / remove that object from the selection
                if hit and hit[1] in ("box", "spawn"):
                    grp = [(k, i) for k, i, _o in self.sel_all()]
                    one = (hit[1], hit[2])
                    if one in grp:
                        grp.remove(one)
                    else:
                        grp.append(one)
                    if grp:
                        self.sel = grp[0]
                        self.extra = grp[1:]
                    else:
                        self.sel = None
                return
            if hit and hit[1] in ("box", "spawn"):
                if (hit[1], hit[2]) not in [(k, i) for k, i, _o in self.sel_all()]:
                    self.sel = (hit[1], hit[2])          # clicking one of the selected ones keeps the whole group
                p = o + d * hit[0]
                origs = [(k, i, list(ob.b) if k == "box" else (ob.x, ob.y)) for k, i, ob in self.sel_all()]
                self.drag = {"kind": "move", "z": float(p[2]), "start": self.plane_point(o, d, float(p[2])),
                             "origs": origs, "pushed": False}
            else:
                self.sel = None
        elif self.tool == ROT:
            gp = self.gizmo_hit(ev.pos)
            if gp is not None:                       # grabbed the arrow / ring
                cx, cy, z, R, yaw = self.gizmo_info()
                self.drag = {"kind": "rot", "z": z, "cx": cx, "cy": cy, "yaw0": yaw, "acc": 0.0,
                             "last": math.atan2(gp[1] - cy, gp[0] - cx), "pushed": False}
                return
            hit, o, d = self.pick_all(ev.pos)
            self.sel = (hit[1], hit[2]) if (hit and hit[1] in ("box", "spawn")) else None
        elif self.tool == STRETCH:
            h = self.stretch_pick(ev.pos)
            if h is not None:                        # grabbed a face arrow
                o, d = self.ray(ev.pos)
                s0 = line_param(o, d, h["P"], h["D"])
                if s0 is not None:
                    self.drag = {"kind": "stretch", "ax": h["ax"], "hi": h["hi"], "P": h["P"], "D": h["D"],
                                 "s0": s0, "b0": list(self.sel_obj().b), "pushed": False, "z": 0.0}
                return
            hit, o, d = self.pick_all(ev.pos, False)
            self.sel = ("box", hit[2]) if (hit and hit[1] == "box") else None
        elif self.tool == DECAL:
            self.decal_down(ev.pos)
        elif self.tool in (BOX, PXZ, LADDER, RAMP, CIRC):
            z = self.place_plane(ev.pos)
            if z is None:
                self.msg("Click on the floor or on top of a box to start a box")
                return
            o, d = self.ray(ev.pos)
            p = self.plane_point(o, d, z)
            if p is None:
                return
            g = self.grid
            self.drag = {"kind": "box", "tool": self.tool, "z": z, "a": (snap(p[0], g), snap(p[1], g)), "cur": (snap(p[0], g), snap(p[1], g))}
        else:
            hit, o, d = self.pick_all(ev.pos, False)
            if hit and hit[1] in ("box", "ground", "bsp") and hit[3] == (0, 0, 1):
                p = o + d * hit[0]
                g = max(8, self.grid)
                x, y = snap(p[0], g), snap(p[1], g)
                z = p[2] if hit[1] in ("box", "bsp") else 0.0
                if hit[1] == "box":
                    z = self.doc.boxes[hit[2]].b[5]
                team = 0 if self.tool == SPAWN0 else 1
                bl = [b.b for b in self.doc.boxes]
                if hull_hits(bl, x, y, z + HULL_H / 2 + 0.5, self.doc.bsp):
                    self.msg("Something is in the way there")
                    return
                cx = sum((b[0] + b[3]) / 2 for b in bl) / max(1, len(bl))
                cy = sum((b[1] + b[4]) / 2 for b in bl) / max(1, len(bl))
                yaw = round(math.degrees(math.atan2(cy - y, cx - x)) / 45) * 45
                self.push_undo()
                self.doc.spawns.append(Spawn(team, x, y, z, yaw))
                self.sel = ("spawn", len(self.doc.spawns) - 1)
            else:
                self.msg("Click on a floor (top of a box) to place a spawn point")

    def start_texdrag(self, ti):
        self.cur_tex = ti
        self.texdrag = (ti, pygame.key.get_mods())

    def place_plane(self, pos):
        hit, o, d = self.pick_all(pos, False)
        if not hit:
            return None
        p = o + d * hit[0]
        if hit[1] == "bsp":
            return float(round(p[2], 2))
        if hit[3] == (0, 0, 1):
            return float(self.doc.boxes[hit[2]].b[5]) if hit[1] == "box" else 0.0
        if hit[3] == (0, 0, -1):
            return float(self.doc.boxes[hit[2]].b[2])
        return float(snap(p[2], self.grid))

    def on_up(self, ev):
        if ev.button == 1 and self.texdrag:
            ti, mods = self.texdrag
            self.texdrag = None
            if self.in_view(ev.pos):
                if self.tool == DECAL:
                    self.place_decal(ti, ev.pos)       # dragged onto a wall: a decal
                else:
                    self.texture_face_at(ti, ev.pos)   # dragged onto a face
            elif self.tool == DECAL:
                self.decal_set_tex(ti)                 # plain click on the thumbnail: swap the selected decal's picture
            else:
                self.apply_texture(ti, mods)           # plain click on the thumbnail
            return
        if ev.button == 3 and self.look:
            self.look = False
            try:
                pygame.mouse.set_relative_mode(False)
                pygame.event.set_grab(False)
                pygame.mouse.set_visible(True)
            except Exception:
                pass
            return
        if ev.button != 1 or not self.drag:
            return
        dr, self.drag = self.drag, None
        if dr["kind"] == "box" and dr.get("tool") in (RAMP, CIRC):
            self.new_shape(self.shape_bounds(dr), 1 if dr["tool"] == RAMP else 2,
                           "slope" if dr["tool"] == RAMP else "circle")
        elif dr["kind"] == "box":
            (ax, ay), (cx, cy) = dr["a"], dr["cur"]
            g = self.grid
            x0, x1 = min(ax, cx), max(ax, cx)
            y0, y1 = min(ay, cy), max(ay, cy)
            if x1 - x0 < g:
                x1 = x0 + g * 2 if (ax == cx and ay == cy) else x0 + g
            if y1 - y0 < g:
                y1 = y0 + g * 2 if (ax == cx and ay == cy) else y0 + g
            z = dr["z"]
            self.new_box([x0, y0, z, x1, y1, z + self.height])
        elif dr["kind"] == "move" and not dr["pushed"]:
            pass

    def on_motion(self, ev):
        self.mouse = ev.pos
        if self.look:
            self.yaw -= ev.rel[0] * 0.0032
            self.pitch = max(-1.5, min(1.5, self.pitch - ev.rel[1] * 0.0032))
            return
        dr = self.drag
        if not dr:
            return
        o, d = self.ray(ev.pos)
        if dr["kind"] == "stretch":
            self.stretch_motion(dr, o, d)
            return
        if dr["kind"] == "amove":
            self.move_motion(dr, o, d)
            return
        if dr["kind"] in ("dmove", "dresize"):
            self.decal_motion(dr, o, d)
            return
        p = self.plane_point(o, d, dr["z"])
        if p is None:
            return
        g = self.grid
        if dr["kind"] == "box":
            dr["cur"] = (snap(p[0], g), snap(p[1], g))
        elif dr["kind"] == "rot":
            obj = self.sel_obj()
            if obj is None:
                return
            a = math.atan2(p[1] - dr["cy"], p[0] - dr["cx"])
            da = a - dr["last"]
            while da > math.pi:
                da -= 2 * math.pi
            while da < -math.pi:
                da += 2 * math.pi
            dr["acc"] += da
            dr["last"] = a
            new = dr["yaw0"] + math.degrees(dr["acc"])
            if not (pygame.key.get_mods() & pygame.KMOD_SHIFT):
                st = TURN_SNAPS[self.turn_i]
                new = round(new / st) * st
            new = round(new % 360.0, 3) % 360.0
            if abs(new - obj.yaw) > 1e-6:
                if not dr["pushed"]:
                    obj.yaw = dr["yaw0"]
                    self.push_undo()
                    dr["pushed"] = True
                obj.yaw = new
                self.dirty_list = True
        elif dr["kind"] == "move" and dr["start"] is not None:
            dx, dy = snap(p[0] - dr["start"][0], g), snap(p[1] - dr["start"][1], g)
            if not dr["pushed"] and (dx or dy):
                self.push_undo()                    # nothing has moved yet: remember the original places for undo
                dr["pushed"] = True
            if dr["pushed"]:
                for k, i, v in dr["origs"]:
                    lst = self.doc.boxes if k == "box" else self.doc.spawns
                    if not (0 <= i < len(lst)):
                        continue
                    ob = lst[i]
                    if k == "box":
                        ob.b = [v[0] + dx, v[1] + dy, v[2], v[3] + dx, v[4] + dy, v[5]]
                    else:
                        ob.x, ob.y = v[0] + dx, v[1] + dy
                self.dirty_list = True

    # ----- per frame -----
    def update(self, dt):
        if self.look:
            keys = pygame.key.get_pressed()
            f, r, _ = self.basis()
            sp = self.speed * (3.0 if (keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]) else 1.0) * dt
            mv = np.zeros(3)
            if keys[pygame.K_w]:
                mv += f
            if keys[pygame.K_s]:
                mv -= f
            if keys[pygame.K_d]:
                mv += r
            if keys[pygame.K_a]:
                mv -= r
            if keys[pygame.K_e] or keys[pygame.K_SPACE]:
                mv[2] += 1
            if keys[pygame.K_q] or keys[pygame.K_LCTRL]:
                mv[2] -= 1
            self.cam += mv * sp
        self.hover = None
        self.cursor3d = None
        self.gz_hover = False
        self.st_hover = None
        self.mv_hover = None
        self.dec_hover, self.dec_ghost, self.dec_hh = None, None, None
        if not self.look and not self.drag and self.in_view(self.mouse):
            hit, o, d = self.pick_all(self.mouse, self.tool in (SELECT, ROT))
            if self.tool in (SELECT, ROT, STRETCH):
                if hit and hit[1] in ("box", "spawn"):
                    self.hover = (hit[1], hit[2])
                if self.tool == ROT:
                    self.gz_hover = self.gizmo_hit(self.mouse) is not None
                if self.tool == SELECT:
                    mh = self.move_pick(self.mouse)
                    self.mv_hover = mh["ax"] if mh else None
                if self.tool == STRETCH:
                    hh = self.stretch_pick(self.mouse)
                    self.st_hover = (hh["ax"], hh["hi"]) if hh else None
            elif self.tool == DECAL:
                hh = self.decal_handle_pick(self.mouse)
                if hh is not None:
                    self.dec_hh = (hh["s"], hh["t"])
                else:
                    dp = self.decal_pick(self.mouse)
                    if dp is not None and (hit is None or dp[0] <= hit[0] + 1.0):
                        self.dec_hover = dp[1]
                    elif hit and hit[1] == "box":
                        self.dec_ghost = self.make_decal(self.cur_tex, hit, o, d)[0]
            elif hit:
                self.cursor3d = o + d * hit[0]
                self.cursor_n = hit[3]
        now = time.perf_counter()
        self.msgs = [m for m in self.msgs if m[1] > now]

    # ----- drawing: 3D -----
    def set_camera(self):
        W, H = self.W, self.H
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadMatrixf(np.ascontiguousarray(perspective(self.FOV, W / max(1, H), 3.0, 40000.0).T))
        f, _, _ = self.basis()
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glLoadMatrixf(np.ascontiguousarray(look_at(self.cam, f).T))

    def build_list(self):
        if self.list_id is None:
            self.list_id = gl.glGenLists(1)
        gl.glNewList(self.list_id, gl.GL_COMPILE)
        gl.glEnable(gl.GL_TEXTURE_2D)
        for b in self.doc.boxes:
            if b.px or b.lad:
                continue                     # zones are drawn translucent in draw_world
            if b.k:
                self.draw_shape(b)
                continue
            for fi, f in enumerate(box_faces(b.b)):
                n = f[4]
                ti = b.tt if n[2] > .5 else b.t
                if b.f[fi] >= 0:
                    ti = b.f[fi]
                tex = self.doc.tex[ti] if 0 <= ti < len(self.doc.tex) else None
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex.gid if tex else self.white)
                s = shade(n)
                if tex:
                    gl.glColor3f(s, s, s)
                else:
                    gl.glColor3f(b.col[0] / 255 * s, b.col[1] / 255 * s, b.col[2] / 255 * s)
                gl.glBegin(gl.GL_QUADS)
                nr = rot_vec(b, n)
                for p in f[:4]:
                    p = rot_point(b, p)
                    u, v = face_uv(p, nr, b.tile)
                    gl.glTexCoord2f(u, v)
                    gl.glVertex3f(*p)
                gl.glEnd()
        gl.glEndList()
        self.dirty_list = False

    def _poly(self, b, ti, pts, sh, uv):
        tex = self.doc.tex[ti] if 0 <= ti < len(self.doc.tex) else None
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex.gid if tex else self.white)
        if tex:
            gl.glColor3f(sh, sh, sh)
        else:
            gl.glColor3f(b.col[0] / 255 * sh, b.col[1] / 255 * sh, b.col[2] / 255 * sh)
        gl.glBegin(gl.GL_POLYGON)
        for p, (u, v) in zip(pts, uv):
            gl.glTexCoord2f(u, v)
            gl.glVertex3f(*p)
        gl.glEnd()

    def draw_shape(self, b):
        """A real slope (wedge) or cylinder."""
        x0, y0, z0, x1, y1, z1 = b.b
        tile = b.tile
        top_t = b.f[0] if b.f[0] >= 0 else b.tt
        bot_t = b.f[1] if b.f[1] >= 0 else b.t
        side_t = next((v for v in b.f[2:] if v >= 0), b.t)
        if b.k == 1:
            d = b.d % 4

            def W(u, v, w):
                if d == 0:
                    x, y = x0 + u * (x1 - x0), y0 + v * (y1 - y0)
                elif d == 2:
                    x, y = x1 - u * (x1 - x0), y0 + v * (y1 - y0)
                elif d == 1:
                    x, y = x0 + v * (x1 - x0), y0 + u * (y1 - y0)
                else:
                    x, y = x0 + v * (x1 - x0), y1 - u * (y1 - y0)
                return (x, y, z0 + w * (z1 - z0))
            ax = d in (0, 2)
            wn = ((1, 0, 0), (0, 1, 0), (-1, 0, 0), (0, -1, 0))[d]
            sa, sb = ((0, -1, 0), (0, 1, 0)) if ax else ((-1, 0, 0), (1, 0, 0))
            polys = (
                ([(0, 1, 0), (1, 1, 0), (1, 0, 0), (0, 0, 0)], (0, 0, -1), bot_t),
                ([(0, 0, 0), (1, 0, 1), (1, 1, 1), (0, 1, 0)], (0, 0, 1), top_t),
                ([(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)], wn, side_t),
                ([(0, 0, 0), (1, 0, 0), (1, 0, 1)], sa, side_t),
                ([(0, 1, 0), (1, 1, 0), (1, 1, 1)], sb, side_t))
            for loc, n, ti in polys:
                pts = [rot_point(b, W(*q)) for q in loc]
                nr = rot_vec(b, n)
                self._poly(b, ti, pts, shade(n), [face_uv(p, nr, tile) for p in pts])
        else:
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
            N = 64
            ring = [(cx + rx * math.cos(2 * math.pi * i / N), cy + ry * math.sin(2 * math.pi * i / N)) for i in range(N)]
            ring = [rot_point(b, (px, py, 0.0))[:2] for px, py in ring]
            run = 0.0
            for i in range(N):
                a, c = ring[i], ring[(i + 1) % N]
                seg = math.hypot(c[0] - a[0], c[1] - a[1])
                pts = [(a[0], a[1], z0), (c[0], c[1], z0), (c[0], c[1], z1), (a[0], a[1], z1)]
                uv = [(run / tile, -z0 / tile), ((run + seg) / tile, -z0 / tile),
                      ((run + seg) / tile, -z1 / tile), (run / tile, -z1 / tile)]
                run += seg
                sh = 0.75 + 0.07 * math.cos(2 * math.pi * (i + .5) / N)
                self._poly(b, side_t, pts, sh, uv)
            top = [(px, py, z1) for px, py in ring]
            self._poly(b, top_t, top, 1.0, [face_uv(p, (0, 0, 1), tile) for p in top])
            bot = [(px, py, z0) for px, py in reversed(ring)]
            self._poly(b, bot_t, bot, 0.5, [face_uv(p, (0, 0, 1), tile) for p in bot])

    def draw_grid(self):
        g = self.grid
        step = g
        while step < 40 and step < 512:
            step *= 2
        c = self.cam
        n = 28
        x0, y0 = snap(c[0], step * 4), snap(c[1], step * 4)
        R = step * n
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glLineWidth(1.0)
        gl.glBegin(gl.GL_LINES)
        for i in range(-n, n + 1):
            x = x0 + i * step
            major = (round(x / step) % 4 == 0)
            gl.glColor4f(0.42, 0.45, 0.52, 0.55 if major else 0.22)
            gl.glVertex3f(x, y0 - R, 0.1)
            gl.glVertex3f(x, y0 + R, 0.1)
            y = y0 + i * step
            major = (round(y / step) % 4 == 0)
            gl.glColor4f(0.42, 0.45, 0.52, 0.55 if major else 0.22)
            gl.glVertex3f(x0 - R, y, 0.1)
            gl.glVertex3f(x0 + R, y, 0.1)
        gl.glColor4f(0.9, 0.3, 0.3, 0.9)
        gl.glVertex3f(-30000, 0, 0.2)
        gl.glVertex3f(30000, 0, 0.2)
        gl.glColor4f(0.3, 0.9, 0.3, 0.9)
        gl.glVertex3f(0, -30000, 0.2)
        gl.glVertex3f(0, 30000, 0.2)
        gl.glEnd()

    @staticmethod
    def wire_box(b, col, w=2.0, e=0.4, yaw=0.0):
        x0, y0, z0, x1, y1, z1 = b[0] - e, b[1] - e, b[2] - e, b[3] + e, b[4] + e, b[5] + e
        if yaw:
            mx, my = (b[0] + b[3]) / 2, (b[1] + b[4]) / 2
            cc, ss = yaw_cs(yaw)

            def P(q):
                dx, dy = q[0] - mx, q[1] - my
                return (mx + dx * cc - dy * ss, my + dx * ss + dy * cc, q[2])
        else:
            def P(q):
                return q
        gl.glLineWidth(w)
        gl.glColor4f(*col)
        gl.glBegin(gl.GL_LINES)
        for a, c in (((x0, y0, z0), (x1, y0, z0)), ((x1, y0, z0), (x1, y1, z0)), ((x1, y1, z0), (x0, y1, z0)),
                     ((x0, y1, z0), (x0, y0, z0)), ((x0, y0, z1), (x1, y0, z1)), ((x1, y0, z1), (x1, y1, z1)),
                     ((x1, y1, z1), (x0, y1, z1)), ((x0, y1, z1), (x0, y0, z1)), ((x0, y0, z0), (x0, y0, z1)),
                     ((x1, y0, z0), (x1, y0, z1)), ((x1, y1, z0), (x1, y1, z1)), ((x0, y1, z0), (x0, y1, z1))):
            gl.glVertex3f(*P(a))
            gl.glVertex3f(*P(c))
        gl.glEnd()

    @staticmethod
    def ladder_frame(zb):
        """Local frame of a ladder zone: (point on the BACK face centre at z0, n = unit vector toward the climber,
        t = unit vector along the face, width, depth). Spin is not applied yet."""
        x0, y0, z0, x1, y1, z1 = zb.b
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        d = zb.d % 4
        if d == 0:
            return (x0, cy), (1.0, 0.0), (0.0, 1.0), y1 - y0, x1 - x0
        if d == 2:
            return (x1, cy), (-1.0, 0.0), (0.0, 1.0), y1 - y0, x1 - x0
        if d == 1:
            return (cx, y0), (0.0, 1.0), (1.0, 0.0), x1 - x0, y1 - y0
        return (cx, y1), (0.0, -1.0), (1.0, 0.0), x1 - x0, y1 - y0

    def draw_ladder_marks(self, zb):
        """Rails + rungs on the wall side of the zone and an arrow showing where the climber stands."""
        (bx, by), (nx, ny), (tx, ty), W, T = self.ladder_frame(zb)
        z0, z1 = zb.b[2], zb.b[5]
        off = min(3.0, T / 2)
        half = max(2.0, W / 2 - min(5.0, W / 4))

        def P(u, t, z):                                  # u along n from the back face, t along the face
            return rot_point(zb, (bx + nx * u + tx * t, by + ny * u + ty * t, z))
        gl.glLineWidth(2.0)
        gl.glColor4f(1.0, 0.75, 0.3, 1.0)
        gl.glBegin(gl.GL_LINES)
        for sg in (-1, 1):                               # the two rails
            gl.glVertex3f(*P(off, sg * half, z0))
            gl.glVertex3f(*P(off, sg * half, z1))
        n = int(max(1, min(300, (z1 - z0) / 12.0)))
        for i in range(n):                               # the rungs
            z = z0 + 6.0 + i * 12.0
            if z > z1:
                break
            gl.glVertex3f(*P(off, -half, z))
            gl.glVertex3f(*P(off, half, z))
        zm = (z0 + z1) / 2                               # arrow: from the front face outward = where you stand
        a = P(T, 0.0, zm)
        b = P(T + 56.0, 0.0, zm)
        h1 = P(T + 40.0, 12.0, zm)
        h2 = P(T + 40.0, -12.0, zm)
        gl.glEnd()
        gl.glLineWidth(4.0)
        gl.glColor4f(1.0, 0.9, 0.2, 1.0)
        gl.glBegin(gl.GL_LINES)
        for p, q in ((a, b), (b, h1), (b, h2)):
            gl.glVertex3f(*p)
            gl.glVertex3f(*q)
        gl.glEnd()
        gl.glLineWidth(1.0)

    def draw_spawn(self, s, selected=False, hover=False):
        col = TEAM_COL[s.team]
        b = (s.x - 16, s.y - 16, s.z, s.x + 16, s.y + 16, s.z + HULL_H)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glDepthMask(gl.GL_FALSE)
        gl.glColor4f(col[0], col[1], col[2], 0.28)
        gl.glBegin(gl.GL_QUADS)
        for f in box_faces(b):
            for p in f[:4]:
                gl.glVertex3f(*p)
        gl.glEnd()
        gl.glDepthMask(gl.GL_TRUE)
        self.wire_box(b, (*col, 1.0), 3.0 if selected else 1.6, 0.2)
        yaw = math.radians(s.yaw)
        cx, cy, cz = s.x, s.y, s.z + 54
        ex, ey = cx + math.cos(yaw) * 46, cy + math.sin(yaw) * 46
        gl.glLineWidth(3.0)
        gl.glColor4f(*col, 1.0)
        gl.glBegin(gl.GL_LINES)
        gl.glVertex3f(cx, cy, cz)
        gl.glVertex3f(ex, ey, cz)
        for a in (2.6, -2.6):
            gl.glVertex3f(ex, ey, cz)
            gl.glVertex3f(ex + math.cos(yaw + a) * 14, ey + math.sin(yaw + a) * 14, cz)
        gl.glEnd()

    def draw_gizmo(self):
        """Hammer-style spin handle: a ring with tick marks, turn arrows and a big arrow showing the facing."""
        gi = self.gizmo_info()
        if gi is None:
            return
        cx, cy, z, R, yaw = gi
        active = bool(self.drag and self.drag.get("kind") == "rot")
        hot = active or self.gz_hover
        ring = (1.0, 0.85, 0.1, 1.0) if hot else (0.35, 0.85, 1.0, 0.95)
        arrow = (1.0, 1.0, 1.0, 1.0) if hot else (1.0, 0.85, 0.1, 1.0)

        def P(a, r):
            return (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)), z)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glDisable(gl.GL_DEPTH_TEST)
        # ring
        gl.glLineWidth(3.5 if hot else 2.5)
        gl.glColor4f(*ring)
        gl.glBegin(gl.GL_LINE_LOOP)
        for i in range(120):
            gl.glVertex3f(*P(i * 3.0, R))
        gl.glEnd()
        # tick marks at every turn step (longer every 90 degrees)
        st = TURN_SNAPS[self.turn_i]
        gl.glLineWidth(1.5)
        gl.glColor4f(1, 1, 1, 0.75)
        gl.glBegin(gl.GL_LINES)
        for a in range(0, 360, st):
            ln = R * (0.10 if a % 90 == 0 else 0.045)
            gl.glVertex3f(*P(a, R - ln))
            gl.glVertex3f(*P(a, R + ln))
        gl.glEnd()
        # little cross in the middle
        gl.glLineWidth(2.0)
        gl.glColor4f(*ring)
        gl.glBegin(gl.GL_LINES)
        gl.glVertex3f(cx - 10, cy, z)
        gl.glVertex3f(cx + 10, cy, z)
        gl.glVertex3f(cx, cy - 10, z)
        gl.glVertex3f(cx, cy + 10, z)
        gl.glEnd()
        # turn arrows on both sides of the handle
        hs = max(9.0, min(R * 0.07, 24.0))
        gl.glColor4f(*ring)
        gl.glBegin(gl.GL_TRIANGLES)
        for sg in (1, -1):
            a = yaw + sg * 36
            ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
            tx, ty = -sa * sg, ca * sg                    # direction of travel
            px, py, _ = P(a, R)
            gl.glVertex3f(px + tx * hs * 1.7, py + ty * hs * 1.7, z)
            gl.glVertex3f(px + ca * hs * .9, py + sa * hs * .9, z)
            gl.glVertex3f(px - ca * hs * .9, py - sa * hs * .9, z)
        gl.glEnd()
        # the big arrow (drag me)
        L = min(max(40.0, R * 0.35), 220.0)
        ux, uy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        hl = min(L * 0.5, 46.0)
        hw = hl * 0.6
        gl.glLineWidth(6.0)
        gl.glColor4f(*arrow)
        gl.glBegin(gl.GL_LINES)
        gl.glVertex3f(cx + ux * R, cy + uy * R, z)
        gl.glVertex3f(cx + ux * (R + L - hl * .5), cy + uy * (R + L - hl * .5), z)
        gl.glEnd()
        gl.glBegin(gl.GL_TRIANGLES)
        bx, by = cx + ux * (R + L - hl), cy + uy * (R + L - hl)
        gl.glVertex3f(bx - uy * hw, by + ux * hw, z)
        gl.glVertex3f(bx + uy * hw, by - ux * hw, z)
        gl.glVertex3f(cx + ux * (R + L), cy + uy * (R + L), z)
        gl.glEnd()
        gl.glLineWidth(1.0)
        gl.glEnable(gl.GL_DEPTH_TEST)

    def draw_world(self):
        W, H = self.W, self.H
        gl.glViewport(0, 0, W, H)
        gl.glClearColor(0.04, 0.04, 0.06, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LEQUAL)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        self.set_camera()
        self.draw_sky()
        if self.dirty_list or self.list_id is None:
            self.build_list()
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glCallList(self.list_id)
        if self.doc.bsp is not None:
            self.doc.bsp.draw(self.white)
        self.draw_decals()
        self.draw_grid()
        gl.glDisable(gl.GL_TEXTURE_2D)
        for i, s in enumerate(self.doc.spawns):
            self.draw_spawn(s, self.sel == ("spawn", i) or ("spawn", i) in self.extra)
        zl = [zb for zb in self.doc.boxes if zb.px and not zb.lad]
        ll = [zb for zb in self.doc.boxes if zb.lad]
        if ll:                                   # ladders: see-through orange blocks with rungs and an arrow to the climbing side
            gl.glDepthMask(gl.GL_FALSE)
            gl.glColor4f(1.0, 0.55, 0.1, 0.16)
            gl.glBegin(gl.GL_QUADS)
            for zb in ll:
                for f in box_faces(zb.b):
                    for p in f[:4]:
                        gl.glVertex3f(*rot_point(zb, p))
            gl.glEnd()
            gl.glDepthMask(gl.GL_TRUE)
            for zb in ll:
                self.wire_box(zb.b, (1.0, 0.6, 0.1, 1.0), 2.0, 0.4, zb.yaw)
                self.draw_ladder_marks(zb)
        if zl:                                   # pixel surf zones: see-through cyan blocks
            gl.glDepthMask(gl.GL_FALSE)
            gl.glColor4f(0.1, 0.95, 0.85, 0.16)
            gl.glBegin(gl.GL_QUADS)
            for zb in zl:
                for f in box_faces(zb.b):
                    for p in f[:4]:
                        gl.glVertex3f(*rot_point(zb, p))
            gl.glEnd()
            gl.glDepthMask(gl.GL_TRUE)
            for zb in zl:
                self.wire_box(zb.b, (0.1, 1.0, 0.85, 1.0), 2.0, 0.4, zb.yaw)
        if self.hover and self.hover[0] == "box":
            hb = self.doc.boxes[self.hover[1]]
            self.wire_box(hb.b, (1, 1, 1, 0.8), 2.0, 0.4, hb.yaw)
        if self.sel and self.sel[0] == "box" and 0 <= self.sel[1] < len(self.doc.boxes):
            sb = self.doc.boxes[self.sel[1]]
            self.wire_box(sb.b, (1.0, 0.85, 0.1, 1.0), 3.0, 0.4, sb.yaw)
        for k_, i_, ob_ in self.sel_all()[1:]:
            if k_ == "box":
                self.wire_box(ob_.b, (1.0, 0.85, 0.1, 1.0), 3.0, 0.4, ob_.yaw)
        if self.tool == ROT:
            self.draw_gizmo()
        elif self.tool == STRETCH:
            self.draw_stretch()
        elif self.tool == SELECT:
            self.draw_move()
        elif self.tool == DECAL:
            self.draw_decal_overlay()
        dr = self.drag
        if dr and dr["kind"] == "box" and dr.get("tool") in (RAMP, CIRC):
            lst = [self.shape_bounds(dr)]
            gl.glDepthMask(gl.GL_FALSE)
            gl.glColor4f(0.3, 0.8, 1.0, 0.25)
            gl.glBegin(gl.GL_QUADS)
            for b in lst:
                for f in box_faces(b):
                    for p in f[:4]:
                        gl.glVertex3f(*p)
            gl.glEnd()
            gl.glDepthMask(gl.GL_TRUE)
            if lst:
                self.wire_box((min(b[0] for b in lst), min(b[1] for b in lst), dr["z"],
                               max(b[3] for b in lst), max(b[4] for b in lst), max(b[5] for b in lst)),
                              (0.4, 0.9, 1.0, 1.0), 2.5, 0)
        elif dr and dr["kind"] == "box":
            (ax, ay), (cx, cy) = dr["a"], dr["cur"]
            g = self.grid
            x0, x1, y0, y1 = min(ax, cx), max(ax, cx), min(ay, cy), max(ay, cy)
            if x1 - x0 < g:
                x1 = x0 + g
            if y1 - y0 < g:
                y1 = y0 + g
            b = (x0, y0, dr["z"], x1, y1, dr["z"] + self.height)
            gl.glDepthMask(gl.GL_FALSE)
            gl.glColor4f(0.3, 0.8, 1.0, 0.25)
            gl.glBegin(gl.GL_QUADS)
            for f in box_faces(b):
                for p in f[:4]:
                    gl.glVertex3f(*p)
            gl.glEnd()
            gl.glDepthMask(gl.GL_TRUE)
            self.wire_box(b, (0.4, 0.9, 1.0, 1.0), 2.5, 0)
        elif self.cursor3d is not None and self.tool in (BOX, PXZ, LADDER, RAMP, CIRC, SPAWN0, SPAWN1):
            p = self.cursor3d
            g = self.grid
            x, y = snap(p[0], g), snap(p[1], g)
            z = p[2]
            n = getattr(self, "cursor_n", (0, 0, 1))
            if self.tool in (BOX, PXZ, LADDER, RAMP, CIRC) and n == (0, 0, 1):
                gl.glDepthMask(gl.GL_FALSE)
                gl.glColor4f(0.3, 0.8, 1.0, 0.35)
                gl.glBegin(gl.GL_QUADS)
                gl.glVertex3f(x - g / 2, y - g / 2, z + 0.3)
                gl.glVertex3f(x + g / 2, y - g / 2, z + 0.3)
                gl.glVertex3f(x + g / 2, y + g / 2, z + 0.3)
                gl.glVertex3f(x - g / 2, y + g / 2, z + 0.3)
                gl.glEnd()
                gl.glDepthMask(gl.GL_TRUE)
            elif self.tool in (SPAWN0, SPAWN1) and n == (0, 0, 1):
                team = 0 if self.tool == SPAWN0 else 1
                gg = max(8, g)
                self.draw_spawn(Spawn(team, snap(p[0], gg), snap(p[1], gg), z, 0))

    # ----- drawing: 2D UI -----
    def font(self, size):
        if size not in self.fonts:
            f = None
            try:
                f = pygame.font.SysFont("segoeui,arial,dejavusans,liberationsans", size)
            except Exception:
                f = None
            self.fonts[size] = f or pygame.font.Font(None, int(size * 1.2))
        return self.fonts[size]

    def text_tex(self, s, size, col):
        key = (s, size, col)
        t = self.ttex.get(key)
        if t is None:
            if len(self.ttex) > 500:
                for v in self.ttex.values():
                    gl.glDeleteTextures([v[0]])
                self.ttex = {}
            surf = self.font(size).render(s or " ", True, col).convert_alpha()
            w, h = surf.get_size()
            t = (make_gl_tex(surf_bytes(surf), w, h, False, True), w, h)
            self.ttex[key] = t
        return t

    def text(self, s, x, y, size=14, col=(225, 228, 235), anchor="l"):
        tid, w, h = self.text_tex(s, size, col)
        if anchor == "r":
            x -= w
        elif anchor == "c":
            x -= w / 2
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
        gl.glColor4f(1, 1, 1, 1)
        self.quad(x, y, w, h)
        return w

    def quad(self, x, y, w, h):
        gl.glBegin(gl.GL_QUADS)
        gl.glTexCoord2f(0, 0)
        gl.glVertex2f(x, y)
        gl.glTexCoord2f(1, 0)
        gl.glVertex2f(x + w, y)
        gl.glTexCoord2f(1, 1)
        gl.glVertex2f(x + w, y + h)
        gl.glTexCoord2f(0, 1)
        gl.glVertex2f(x, y + h)
        gl.glEnd()

    def rect(self, x, y, w, h, col):
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor4f(*col)
        gl.glBegin(gl.GL_QUADS)
        gl.glVertex2f(x, y)
        gl.glVertex2f(x + w, y)
        gl.glVertex2f(x + w, y + h)
        gl.glVertex2f(x, y + h)
        gl.glEnd()

    def outline(self, x, y, w, h, col, lw=1.0):
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glLineWidth(lw)
        gl.glColor4f(*col)
        gl.glBegin(gl.GL_LINE_LOOP)
        gl.glVertex2f(x + .5, y + .5)
        gl.glVertex2f(x + w - .5, y + .5)
        gl.glVertex2f(x + w - .5, y + h - .5)
        gl.glVertex2f(x + .5, y + h - .5)
        gl.glEnd()

    def button(self, x, y, w, h, label, cb, active=False, size=13, col=None, nav=False, pad=14):
        """Flat dark button. nav = a full-width list row (text on the left), the active one is lighter."""
        mx, my = self.mouse
        over = x <= mx < x + w and y <= my < y + h
        if col is not None:
            base = col if not over else tuple(min(1, c + 0.06) for c in col)
        elif active:
            base = (0.125, 0.125, 0.135, 1)
        elif over:
            base = (0.095, 0.095, 0.105, 1)
        else:
            base = (0.0, 0.0, 0.0, 0.0) if nav else (0.085, 0.085, 0.095, 1)
        if base[3] > 0:
            self.rect(x, y, w, h, base)
        tc = (255, 255, 255) if (active or col is not None) else ((215, 218, 225) if over else (150, 154, 162))
        if nav:
            self.text(label, x + pad, y + (h - size) / 2 - 1, size, tc)
        else:
            self.text(label, x + w / 2, y + (h - size) / 2 - 1, size, tc, "c")
        self.hot.append((x, y, w, h, cb))

    def draw_ui(self):
        W, H = self.W, self.H
        self.hot = []
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glEnable(gl.GL_BLEND)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glOrtho(0, W, H, 0, -1, 1)
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glLoadIdentity()
        bg = (0.063, 0.063, 0.067, 1)
        self.rect(0, 0, LW, H - SB, bg)
        self.rect(W - RW, 0, RW, H - SB, bg)
        self.rect(LW - 1, 0, 1, H - SB, (0.11, 0.11, 0.12, 1))
        self.rect(W - RW, 0, 1, H - SB, (0.11, 0.11, 0.12, 1))
        self.rect(0, H - SB, W, SB, (0.04, 0.04, 0.043, 1))
        # ---- left panel ----
        x, y = 12, 10
        self.text("Map Builder", x, y - 2, 21, (255, 255, 255))
        self.text(self.doc.name + ("*" if self.doc.dirty else ""), x, y + 26, 12, (120, 124, 132))
        y = 56
        bw = (LW - 36) // 2
        b3 = (LW - 24 - 12) // 3
        self.button(x, y, b3, 28, "New", self.new_map)
        self.button(x + b3 + 6, y, b3, 28, "Open", self.open_project)
        self.button(x + 2 * (b3 + 6), y, b3, 28, "Import", self.import_source, col=(0.20, 0.36, 0.62, 1))
        y += 34
        self.button(x, y, bw, 28, "Save", lambda: self.save(False))
        self.button(x + bw + 12, y, bw, 28, "Save as", lambda: self.save(True))
        y += 34
        self.button(x, y, LW - 24, 34, "COMPILE  (F5)", self.compile, col=(0.16, 0.52, 0.28, 1), size=14)
        y += 50
        self.text("TOOLS", x, y, 11, (130, 140, 155))
        y += 18
        for tool, label in ((SELECT, "Select / move  (V)"), (BOX, "Build box  (B)"),
                            (RAMP, "Build slope  (3)"), (CIRC, "Build circle  (4)"),
                            (ROT, "Spin / turn  (5)"), (STRETCH, "Stretch  (6)"), (DECAL, "Decal  (7)"),
                            (PXZ, "Pixel surf zone  (8)"), (LADDER, "Ladder  (9)"),
                            (SPAWN0, "Spawn BLACK  (1)"), (SPAWN1, "Spawn YELLOW  (2)")):
            sp_ = tool in (SPAWN0, SPAWN1)
            self.button(0, y, LW - 1, 29, label, lambda t=tool: self.set_tool(t), self.tool == tool,
                        nav=True, pad=32 if sp_ else 14)
            if sp_:
                c = TEAM_COL[0 if tool == SPAWN0 else 1]
                self.rect(14, y + 10, 9, 9, (*c, 1))
            y += 29
        y += 8
        self.text("GRID  (G)", x, y, 11, (130, 140, 155))
        y += 18
        gw = (LW - 24 - 4 * 4) // 5
        for i, g in enumerate(GRIDS):
            self.button(x + i * (gw + 4), y, gw, 24, str(g), lambda i=i: setattr(self, "gi", i), self.gi == i, 12)
        y += 36
        self.text("NEW BOX HEIGHT  (- / =)", x, y, 11, (130, 140, 155))
        y += 18
        self.button(x, y, 34, 26, "-", lambda: setattr(self, "height", max(self.grid, self.height - self.grid)))
        self.text(str(int(self.height)), x + 34 + (LW - 24 - 68) / 2, y + 5, 14, (255, 255, 255), "c")
        self.button(x + LW - 24 - 34, y, 34, 26, "+", lambda: setattr(self, "height", self.height + self.grid))
        y += 38
        self.text("TEXTURE SIZE  ( [ / ] )", x, y, 11, (130, 140, 155))
        y += 18
        obj = self.sel_obj()
        tv = obj.tile if (obj is not None and self.sel[0] == "box") else self.tile
        self.button(x, y, 34, 26, "-", lambda: self.step_tile(-1))
        self.text(str(int(tv)), x + 34 + (LW - 24 - 68) / 2, y + 5, 14, (255, 255, 255), "c")
        self.button(x + LW - 24 - 34, y, 34, 26, "+", lambda: self.step_tile(1))
        y += 40
        self.text("SKYBOX", x, y, 11, (130, 140, 155))
        y += 18
        sbw = (LW - 36) // 2
        self.button(x, y, sbw, 26, "Set image", self.pick_sky)
        self.button(x + sbw + 12, y, sbw, 26, "Clear", self.clear_sky)
        y += 30
        have = sum(1 for t in self.doc.cube if t)
        self.button(x, y, LW - 24, 26, "Set cubemap (6 faces)", self.pick_cube, active=have == 6)
        y += 30
        if have:
            sk = "cubemap %d/6: %s" % (have, " ".join(CUBE_FACES[k] for k in range(6) if self.doc.cube[k]))
        else:
            sk = self.doc.sky.name if self.doc.sky else "none"
        self.text(sk[:30], x, y, 11, (150, 160, 175))
        y += 26
        self.text("F1 = help", x, y, 12, (130, 140, 155))
        # ---- right panel: textures ----
        rx = W - RW + 10
        self.text("TEXTURES", rx, 10, 11, (130, 140, 155))
        self.button(rx, 30, RW - 20, 30, "+  Add image...", self.pick_images, col=(0.20, 0.36, 0.62, 1))
        top, bot = 70, H - SB - 190
        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glScissor(W - RW, SB + 190, RW, max(1, bot - top))
        th, gap = 64, 8
        items = [-1] + list(range(len(self.doc.tex)))
        per = 3
        rows = (len(items) + per - 1) // per
        maxscroll = max(0, rows * (th + 18 + gap) - (bot - top))
        self.tex_scroll = max(0, min(self.tex_scroll, maxscroll))
        for k, ti in enumerate(items):
            cx = rx + (k % per) * (th + gap)
            cy = top + (k // per) * (th + 18 + gap) - self.tex_scroll
            if cy + th + 18 < top or cy > bot:
                continue
            if ti < 0:
                self.rect(cx, cy, th, th, (DEFAULT_COL[0] / 255, DEFAULT_COL[1] / 255, DEFAULT_COL[2] / 255, 1))
                nm = "flat colour"
            else:
                t = self.doc.tex[ti]
                gl.glEnable(gl.GL_TEXTURE_2D)
                gl.glBindTexture(gl.GL_TEXTURE_2D, t.gid)
                gl.glColor4f(1, 1, 1, 1)
                self.quad(cx, cy, th, th)
                nm = t.name
            sel = ti == self.cur_tex
            self.outline(cx, cy, th, th, (1, 0.85, 0.1, 1) if sel else (0.16, 0.17, 0.21, 1), 3.0 if sel else 1.0)
            self.text(nm[:11] + ("." if len(nm) > 11 else ""), cx, cy + th + 2, 11, (170, 178, 190))
            if top <= self.mouse[1] < bot:
                self.hot.append((cx, max(cy, top), th, min(th, bot - cy), lambda ti=ti: self.start_texdrag(ti)))
        gl.glDisable(gl.GL_SCISSOR_TEST)
        # ---- right panel: selection info ----
        py = H - SB - 180
        self.rect(W - RW, py - 6, RW, 1, (0.16, 0.17, 0.21, 1))
        self.text("SELECTED" + (" (%d)" % (1 + len(self.extra)) if self.extra else ""), rx, py, 11, (130, 140, 155))
        py += 20
        obj = self.sel_obj()
        dec = self.sel_decal() if self.tool == DECAL else None
        if dec is not None:
            tn_ = self.doc.tex[dec.tex].name[:16] if 0 <= dec.tex < len(self.doc.tex) else "?"
            self.text("DECAL", rx, py + 1, 12, (230, 140, 140))
            self.text("%d" % (self.dsel + 1), rx + 52, py, 14, (255, 255, 255))
            self.text("size  %d x %d" % (dec.c1 - dec.c0, dec.v1 - dec.v0), rx, py + 22, 13)
            self.text("on     " + ("floor" if dec.fi == 0 else "wall"), rx, py + 40, 13)
            self.text("image  " + tn_, rx, py + 58, 12, (190, 198, 210))
            fw = (RW - 30) // 2
            self.button(rx, py + 78, fw, 24, "Flip H  (H)", lambda: self.decal_flip(False), dec.fx, 12)
            self.button(rx + fw + 10, py + 78, fw, 24, "Flip V  (J)", lambda: self.decal_flip(True), dec.fy, 12)
            self.text("opacity", rx, py + 112, 12, (190, 198, 210))
            self.button(rx + 66, py + 106, 28, 24, "-", lambda: self.decal_opacity(-0.1), size=14)
            self.text("%d%%" % round(dec.op * 100), rx + 66 + 28 + 40, py + 110, 14, (255, 255, 255), "c")
            self.button(rx + 66 + 28 + 80, py + 106, 28, 24, "+", lambda: self.decal_opacity(0.1), size=14)
            self.text("PgUp / PgDn = opacity", rx, py + 138, 11, (120, 128, 140))
            self.text("Drag corner = resize, Del = delete", rx, py + 153, 11, (120, 128, 140))
        elif self.tool == DECAL:
            self.text("No decal selected", rx, py, 13, (160, 168, 180))
            self.text("Pick an image above, then", rx, py + 20, 12, (120, 128, 140))
            self.text("click a wall or a floor.", rx, py + 36, 12, (120, 128, 140))
            self.text("Click a decal to select it.", rx, py + 52, 12, (120, 128, 140))
        elif obj is None:
            self.text("Nothing selected", rx, py, 13, (160, 168, 180))
            self.text("Click a box or spawn", rx, py + 20, 12, (120, 128, 140))
            self.text("with the Select tool (V).", rx, py + 36, 12, (120, 128, 140))
        elif self.sel[0] == "box":
            b = obj.b
            self.text("LADDER" if obj.lad else ("PIXEL ZONE" if obj.px else "BOX"), rx, py + 1, 12,
                      (255, 160, 40) if obj.lad else ((60, 230, 205) if obj.px else (230, 160, 50)))
            self.text("%d" % (self.sel[1] + 1), rx + (70 if obj.lad else (98 if obj.px else 52)), py, 14, (255, 255, 255))
            self.text("size  %d x %d x %d" % (b[3] - b[0], b[4] - b[1], b[5] - b[2]), rx, py + 22, 13)
            self.text("at    %d, %d, %d" % (b[0], b[1], b[2]), rx, py + 40, 13)

            def tn(i):
                return self.doc.tex[i].name[:16] if 0 <= i < len(self.doc.tex) else "flat colour"
            self.text("sides  " + tn(obj.t), rx, py + 62, 12, (190, 198, 210))
            self.text("top    " + tn(obj.tt), rx, py + 80, 12, (190, 198, 210))
            self.text("tile   %d     turn  %d deg" % (obj.tile, round(obj.yaw)), rx, py + 98, 12, (190, 198, 210))
            if obj.lad:
                self.text("Climb it from the %s side (R)" % LAD_DIRS[obj.d % 4], rx, py + 120, 11, (255, 190, 110))
                self.text("Walk into it: W / S climb, A / D shuffle.", rx, py + 135, 11, (120, 128, 140))
            elif obj.px:
                self.text("Invisible in the game. Inside", rx, py + 120, 11, (120, 128, 140))
                self.text("this block you can pixel surf.", rx, py + 135, 11, (120, 128, 140))
            else:
                self.text("Drop / drag an image on a face", rx, py + 120, 11, (120, 128, 140))
                self.text("= texture just that face", rx, py + 135, 11, (120, 128, 140))
        else:
            self.text("SPAWN", rx, py + 1, 12, (80, 200, 170))
            self.text(TEAM_NAMES[obj.team], rx + 52, py, 14, (*[int(c * 255) for c in TEAM_COL[obj.team]],))
            self.text("at  %d, %d, %d" % (obj.x, obj.y, obj.z), rx, py + 22, 13)
            self.text("facing  %d deg" % obj.yaw, rx, py + 40, 13)
            self.text("R = turn,  Del = remove", rx, py + 62, 12, (190, 198, 210))
        # ---- status bar ----
        n0 = sum(1 for s in self.doc.spawns if s.team == 0)
        n1 = len(self.doc.spawns) - n0
        info = "%d boxes   spawns: %d black / %d yellow   grid %d   %s" % (
            len(self.doc.boxes), n0, n1, self.grid, {SELECT: "select", BOX: "build box", RAMP: "build slope", CIRC: "build circle", ROT: "spin / turn", STRETCH: "stretch", DECAL: "decal", PXZ: "pixel surf zone", LADDER: "ladder", SPAWN0: "spawn black", SPAWN1: "spawn yellow"}[self.tool])
        if self.tool == RAMP:
            info += "   uphill %s (R = turn it)" % RAMP_DIRS[self.ramp_dir]
        elif self.tool == LADDER:
            info += "   climbed from the %s side (R = turn it)" % LAD_DIRS[self.lad_dir]
        elif self.tool == ROT:
            info += "   turn step %d deg (U)" % TURN_SNAPS[self.turn_i]
        self.text(info, 10, H - SB + 5, 13, (170, 178, 190))
        if self.msgs:
            self.text(self.msgs[-1][0][:170], W - 10, H - SB + 5, 13, (255, 220, 120), "r")
        if self.texdrag and self.texdrag[0] >= 0:
            t_ = self.doc.tex[self.texdrag[0]]
            gl.glEnable(gl.GL_TEXTURE_2D)
            gl.glBindTexture(gl.GL_TEXTURE_2D, t_.gid)
            gl.glColor4f(1, 1, 1, 0.8)
            self.quad(self.mouse[0] + 8, self.mouse[1] + 8, 48, 48)
        if self.drag and self.drag.get("kind") == "stretch":
            o_ = self.sel_obj()
            if o_ is not None:
                mx_, my_ = self.mouse
                self.rect(mx_ + 12, my_ + 8, 150, 24, (0, 0, 0, 0.7))
                self.text("%d x %d x %d" % (o_.b[3] - o_.b[0], o_.b[4] - o_.b[1], o_.b[5] - o_.b[2]),
                          mx_ + 18, my_ + 11, 16, (255, 220, 120))
        if self.drag and self.drag.get("kind") == "rot":
            o_ = self.sel_obj()
            if o_ is not None:
                mx_, my_ = self.mouse
                self.rect(mx_ + 12, my_ + 8, 74, 24, (0, 0, 0, 0.7))
                self.text("%d deg" % round(o_.yaw), mx_ + 18, my_ + 11, 16, (255, 220, 120))
        # crosshair hint while flying
        if self.look:
            self.rect(self.W / 2 - 1, self.H / 2 - 8, 2, 16, (1, 1, 1, 0.6))
            self.rect(self.W / 2 - 8, self.H / 2 - 1, 16, 2, (1, 1, 1, 0.6))
        if self.help:
            self.draw_help()
        self.draw_progress_bar()

    def draw_help(self):
        lines = (__doc__ or "").strip("\n").split("\n")
        lh = 20.0 if 20 * len(lines) + 30 <= self.H - 20 else max(12.0, (self.H - 50.0) / len(lines))
        w, h = 860, lh * len(lines) + 30
        x, y = (self.W - w) / 2, max(10, (self.H - h) / 2)
        self.rect(x, y, w, h, (0.05, 0.06, 0.08, 0.97))
        self.outline(x, y, w, h, (0.4, 0.5, 0.7, 1), 2)
        for i, l in enumerate(lines):
            self.text(l, x + 18, y + 14 + i * lh, 13 if lh >= 17 else 11,
                      (235, 238, 245) if (l and not l.startswith(" ")) else (190, 198, 212))

    def pick_images(self):
        paths = ask_open_many(IMG_KINDS)
        for p in paths:
            self.add_texture(p)
        if not paths and _tk_dialog(lambda fd: True) is None:
            self.msg("No file dialog here: drag an image file onto this window instead.")

    # ----- main loop -----
    def frame(self):
        self.W, self.H = pygame.display.get_window_size()
        now = time.perf_counter()
        dt = min(0.1, now - self.last)
        self.last = now
        self.update(dt)
        self.draw_world()
        self.draw_ui()
        self.title()

    def run(self):
        clock = pygame.time.Clock()
        while self.running:
            for ev in pygame.event.get():
                self.handle(ev)
            self.frame()
            pygame.display.flip()
            clock.tick(120)
        pygame.quit()


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
    app = App()
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
        app.open_project(sys.argv[1])
    app.run()


if __name__ == "__main__":
    main()
