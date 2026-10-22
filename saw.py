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
           7 ladder (click a vertical wall face)
  Box      click + drag on the floor (or on top of another box) = draw the footprint.
           It gets the current Height. Mouse wheel while dragging changes the height.
  Slope    press 3, press R (or T) to pick the uphill direction, then drag the footprint like a box.
           Height = how high the top end is.  A real smooth slope.
  Circle   press 4, click the CENTER and drag outward = radius.  Height works like a box.
           A real smooth cylinder.  Select / move / resize / texture them like boxes.
  Ladder   press 7, click a vertical face of a box. Invisible climb volume (Hammer func_ladder):
           only shown as a green tool box in the editor. Full face height, half-width 22.
           Select + arrows / PageUp/Down / Shift+arrows (width). Del removes. F5 compiles it.
  Select   click a box / spawn / ladder.   Drag it to move it.
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
  Skybox   "Set image" in the left panel (or drop an image on the empty sky) = skybox.
           Use a wide panorama (2:1) for best results.
           CUBEMAP: "Set cubemap" and pick 6 images named px nx py ny pz nz, OR ONE cross picture (4x3) with all faces (also posx/negx..., or
           right left up down front back, or rt lf up dn ft bk). Or drop the face files on the sky
           one by one. px/nx = east/west, py/ny = up/down, pz/nz = south/north. The game gets it baked.
Texture  DRAG an image file from your PC and DROP it on the face you want (that one face gets it).
           Or "Add image", then click it in the right panel (whole selected box),
           Shift+click = top only, Ctrl+click = sides only, or DRAG it from the panel onto a face.
           [ and ] = texture size
  Files    Ctrl+S save project   Ctrl+O open   F5 compile   Ctrl+Z undo   Ctrl+Y redo
           G = next grid size    F = look at selection     F1 = help
"""
import os
import io
import sys
import json
import math
import time
import base64
import re
import zlib
import struct
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
LADDER = "ladder"
TURN_SNAPS = (5, 15, 30, 45, 90)     # degrees, for the spin / turn tool
RAMP_DIRS = ("+X", "+Y", "-X", "-Y")
DEFAULT_LADDER_HW = 22.0             # half-width (same as the built-in arena ladders)


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
    __slots__ = ("b", "t", "tt", "tile", "col", "f", "k", "d", "yaw")

    def __init__(self, b, t=-1, tt=None, tile=128.0, col=DEFAULT_COL, f=None, k=0, d=0, yaw=0.0):
        self.b = [float(v) for v in b]
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
        return Box(list(self.b), self.t, self.tt, self.tile, self.col, self.f, self.k, self.d, self.yaw)

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


class Ladder:
    """Climbable ladder on a vertical wall face.
    Stored as (face_x, face_y, normal_x, normal_y, half_width, bottom_z, top_z) — same as the game."""
    __slots__ = ("fx", "fy", "nx", "ny", "hw", "z0", "z1")

    def __init__(self, fx, fy, nx, ny, hw, z0, z1):
        self.fx, self.fy = float(fx), float(fy)
        self.nx, self.ny = float(nx), float(ny)
        # keep normal unit length on the XY plane
        ln = math.hypot(self.nx, self.ny) or 1.0
        self.nx, self.ny = self.nx / ln, self.ny / ln
        self.hw = float(max(4.0, hw))
        self.z0, self.z1 = float(min(z0, z1)), float(max(z0, z1))

    def copy(self):
        return Ladder(self.fx, self.fy, self.nx, self.ny, self.hw, self.z0, self.z1)

    def as_tuple(self):
        return (self.fx, self.fy, self.nx, self.ny, self.hw, self.z0, self.z1)


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
    out = pygame.Surface(s.get_size(), pygame.SRCALPHA, 32)
    out.fill((0, 0, 0, 255))
    out.blit(s, (0, 0))
    arr = pygame.surfarray.pixels_alpha(out)
    arr[:, :] = 255
    del arr
    return out


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


class MapDoc:
    def __init__(self):
        self.cube = [None] * 6            # sky as 6 faces (px nx py ny pz nz): Tex or None
        self.boxes = []
        self.spawns = []
        self.ladders = []                 # list of Ladder
        self.tex = []
        self.sky = None
        self.path = None
        self.name = "untitled"
        self.dirty = False

    def new_default(self):
        self.boxes = [Box([-1024, -1024, -32, 1024, 1024, 0])]
        self.spawns = []
        self.ladders = []

    def snap(self):
        return ([b.copy() for b in self.boxes], [s.copy() for s in self.spawns],
                [L.copy() for L in self.ladders])

    def restore(self, s):
        self.boxes = [b.copy() for b in s[0]]
        self.spawns = [x.copy() for x in s[1]]
        self.ladders = [L.copy() for L in (s[2] if len(s) > 2 else [])]

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
                           **({"yaw": round(b.yaw, 3)} if b.yaw else {})) for b in boxes],
            "spawns": [[{"p": [round(s.x, 2), round(s.y, 2), round(s.z, 2)], "yaw": round(s.yaw, 1)}
                        for s in self.spawns if s.team == t] for t in (0, 1)],
            "ladders": [[round(L.fx, 2), round(L.fy, 2), round(L.nx, 4), round(L.ny, 4),
                         round(L.hw, 2), round(L.z0, 2), round(L.z1, 2)] for L in self.ladders],
        }
        if self.sky is not None:
            d["sky"] = encode_tex(self.sky, 2048)
        if all(self.cube) and not compiled:
            d["skycube"] = [encode_tex(t, 1024) for t in self.cube]
        if cam:
            d["cam"] = cam
        return d

    def from_dict(self, d):
        if d.get("format") != MAP_FORMAT:
            raise ValueError("not a FAD-MOD map file")
        self.tex = [decode_tex(t) for t in d.get("textures", [])]
        self.boxes = [Box(b["b"], b.get("t", -1), b.get("tt"), b.get("tile", 128), b.get("c", DEFAULT_COL), b.get("f"), b.get("k", 0), b.get("d", 0), b.get("yaw", 0.0))
                      for b in d.get("boxes", [])]
        for b in self.boxes:
            b.fix()
        self.spawns = []
        for t, lst in enumerate(d.get("spawns", [[], []])[:2]):
            for s in lst:
                p = s["p"]
                self.spawns.append(Spawn(t, p[0], p[1], p[2], s.get("yaw", 0)))
        self.ladders = []
        for row in d.get("ladders", []) or []:
            try:
                if len(row) >= 7:
                    self.ladders.append(Ladder(row[0], row[1], row[2], row[3], row[4], row[5], row[6]))
            except Exception:
                pass
        self.sky = decode_tex(d["sky"]) if d.get("sky") else None
        sc = d.get("skycube")
        self.cube = [decode_tex(x) for x in sc] if sc and len(sc) == 6 else [None] * 6
        self.name = d.get("name", "untitled")
        return d.get("cam")


# --------------------------------------------------------------------------
# Compile (checks the map, fixes spawn points, writes the game file)
# --------------------------------------------------------------------------
def hull_hits(boxes, x, y, zc):
    """True if the player hull centred at (x, y, zc) overlaps a box (strict, like the game).
    A box is [x0,y0,z0,x1,y1,z1] or (that list, yaw degrees) for a spun one."""
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


def standing_ok(boxes, x, y, z):
    """Feet at z: nothing in the way, and a floor right below."""
    zc = z + HULL_H / 2 + 0.5
    if hull_hits(boxes, x, y, zc):
        return False
    return hull_hits(boxes, x, y, zc - 6.0)


def compile_doc(doc, out_path):
    """Returns (summary lines, warnings). Writes out_path."""
    msgs, warns = [], []
    boxes, seen, dropped, dups = [], set(), 0, 0
    flat = []
    for src in doc.boxes:                    # the game turns spun objects itself: keep their spin
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
    if not boxes:
        raise ValueError("The map is empty. Build something first.")
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
    hl = [(b.b, b.yaw) if b.yaw else b.b for b in boxes]      # for the spawn checks

    spawns = []
    for s in doc.spawns:
        if standing_ok(hl, s.x, s.y, s.z):
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
                if standing_ok(hl, x, y, z):
                    cands.append((x, y, z))
                y += 64
            x += 64
        if len(cands) > 4000:
            break
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

    # keep valid ladders (positive height, unit normal)
    ladders = []
    for L in doc.ladders:
        if L.z1 - L.z0 < 8 or L.hw < 4:
            warns.append("removed a tiny ladder at (%.0f, %.0f)" % (L.fx, L.fy))
            continue
        ladders.append(L.copy())

    tmp = MapDoc()
    tmp.boxes, tmp.spawns, tmp.ladders, tmp.tex, tmp.name = boxes, spawns, ladders, doc.tex, doc.name
    tmp.sky = doc.sky
    if all(doc.cube):
        tmp.sky = Tex("sky_from_cubemap", cube_to_equirect(doc.cube))
    d = tmp.to_dict(compiled=True, max_side=512)
    d["void_z"] = round(min(b[2] for b in bl) - 800.0, 1)
    d["bounds"] = [round(min(b[i] for b in bl), 1) for i in range(3)] + [round(max(b[i + 3] for b in bl), 1) for i in range(3)]
    raw = json.dumps(d, separators=(",", ":")).encode("utf-8")
    Path(out_path).write_bytes(raw)
    n0 = sum(1 for s in spawns if s.team == 0)
    msgs.append("%d boxes, %d textures, spawns: %d BLACK / %d YELLOW, %d ladders, %.1f KB" % (
        len(boxes), len(d["textures"]), n0, len(spawns) - n0, len(ladders), len(raw) / 1024.0))
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
        self.turn_i = 1                  # index into TURN_SNAPS (15 degrees)
        self.gz_hover = False            # mouse is over the spin ring / arrow
        self.st_hover = None             # (axis, hi) of the stretch arrow under the mouse
        self.clip = None                 # copied object: ("box", Box) or ("spawn", Spawn)
        self.sel = None                  # ("box", i) / ("spawn", i)
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
        self.msg("Welcome. B = build boxes, add an image on the right, F5 = compile. F1 = help.", 8)

    # ----- small helpers -----
    @property
    def grid(self):
        return GRIDS[self.gi]

    def msg(self, text, secs=4.0):
        self.msgs = [(text, time.perf_counter() + secs)]

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
            self.dirty_list = True
            self.msg("Undo")

    def do_redo(self):
        if self.redo:
            self.undo.append(self.doc.snap())
            self.doc.restore(self.redo.pop())
            self.sel = None
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
        elif kind == "ladder":
            lst = self.doc.ladders
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
        for i, b in enumerate(self.doc.boxes):
            h = ray_box(rot_point(b, o, True), rot_vec(b, d, True), b.b)     # spun boxes: ray in the box's own frame
            if h and (best is None or h[0] < best[0]):
                best = (h[0], "box", i, h[1])
        if want_spawns:
            for i, s in enumerate(self.doc.spawns):
                h = ray_box(o, d, (s.x - 16, s.y - 16, s.z, s.x + 16, s.y + 16, s.z + HULL_H))
                if h and (best is None or h[0] < best[0]):
                    best = (h[0], "spawn", i, h[1])
            for i, L in enumerate(self.doc.ladders):
                # thin volume in front of the ladder face (same zone the game uses to grab)
                tx, ty = -L.ny, L.nx
                # AABB around the ladder zone for a quick ray test
                xs = [L.fx + L.nx * o_ + tx * lat for o_ in (0.5, 8.0) for lat in (-L.hw - 2, L.hw + 2)]
                ys = [L.fy + L.ny * o_ + ty * lat for o_ in (0.5, 8.0) for lat in (-L.hw - 2, L.hw + 2)]
                bb = (min(xs), min(ys), L.z0 - 2, max(xs), max(ys), L.z1 + 2)
                h = ray_box(o, d, bb)
                if h and (best is None or h[0] < best[0]):
                    best = (h[0], "ladder", i, (L.nx, L.ny, 0.0))
        if d[2] < -1e-6 and o[2] > 0:
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

    # ----- editing -----
    def new_box(self, b):
        self.push_undo()
        nb = Box(b, self.cur_tex, self.cur_tex, self.tile)
        nb.fix()
        self.doc.boxes.append(nb)
        self.sel = ("box", len(self.doc.boxes) - 1)

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
        if t == ROT:
            self.msg("Spin / turn: select something, then DRAG the arrow or the ring. Shift = free angle.", 7)
        elif t == STRETCH:
            self.msg("Stretch: select a box, then DRAG an arrow on one of its faces. Shift = free size.", 7)

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

    def key_edit(self, key, mods):
        obj = self.sel_obj()
        if obj is None:
            return False
        g = self.grid
        shift = bool(mods & pygame.KMOD_SHIFT)
        dirs = {pygame.K_UP: "f+", pygame.K_DOWN: "f-", pygame.K_RIGHT: "r+", pygame.K_LEFT: "r-"}
        if key in dirs or key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            self.push_undo()
            if self.sel[0] == "ladder":
                if key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                    s = g if key == pygame.K_PAGEUP else -g
                    if shift:
                        obj.z1 = max(obj.z0 + 16, obj.z1 + s)
                    else:
                        obj.z0 += s
                        obj.z1 += s
                else:
                    code = dirs[key]
                    fwd, right = self.nav_axes()
                    ax, sg = (fwd if code[0] == "f" else right)
                    s = sg * (g if code[1] == "+" else -g)
                    if shift:
                        obj.hw = max(4.0, obj.hw + (g if code[1] == "+" else -g))
                    else:
                        if ax == 0:
                            obj.fx += s
                        else:
                            obj.fy += s
                self.dirty_list = True
                return True
            if key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                s = g if key == pygame.K_PAGEUP else -g
                if self.sel[0] == "spawn":
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
                if self.sel[0] == "spawn":
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
            self.dirty_list = True
            return True
        if key == pygame.K_DELETE or key == pygame.K_BACKSPACE:
            self.push_undo()
            kind, i = self.sel
            if kind == "box":
                self.doc.boxes.pop(i)
            elif kind == "spawn":
                self.doc.spawns.pop(i)
            elif kind == "ladder":
                self.doc.ladders.pop(i)
            self.sel = None
            return True
        if key == pygame.K_r and self.sel[0] == "spawn":
            self.push_undo()
            obj.yaw = (obj.yaw + (-45 if shift else 45)) % 360
            return True
        return False

    def duplicate(self):
        obj = self.sel_obj()
        if obj is None:
            return
        self.push_undo()
        g = self.grid
        c = obj.copy()
        if self.sel[0] == "box":
            c.b[0] += g
            c.b[3] += g
            c.b[1] += g
            c.b[4] += g
            self.doc.boxes.append(c)
            self.sel = ("box", len(self.doc.boxes) - 1)
        elif self.sel[0] == "ladder":
            c.fx += g
            c.fy += g
            self.doc.ladders.append(c)
            self.sel = ("ladder", len(self.doc.ladders) - 1)
        else:
            c.x += g
            c.y += g
            self.doc.spawns.append(c)
            self.sel = ("spawn", len(self.doc.spawns) - 1)

    def copy_sel(self, cut=False):
        obj = self.sel_obj()
        if obj is None:
            self.msg("Select a box, spawn or ladder first, then copy it")
            return
        self.clip = (self.sel[0], obj.copy())
        if cut:
            self.push_undo()
            kind, i = self.sel
            if kind == "box":
                self.doc.boxes.pop(i)
            elif kind == "spawn":
                self.doc.spawns.pop(i)
            elif kind == "ladder":
                self.doc.ladders.pop(i)
            self.sel = None
            self.msg("Cut. Ctrl+V to paste it")
        else:
            self.msg("Copied. Ctrl+V to paste it where the mouse points")

    def paste(self):
        if self.clip is None:
            self.msg("Nothing copied yet: select something and press Ctrl+C")
            return
        kind, src = self.clip
        g = self.grid
        spot = None                                   # (x, y, z) under the mouse, on a floor / box top
        if self.in_view(self.mouse) and not self.look:
            hit, o, d = self.pick_all(self.mouse, False)
            if hit and hit[1] in ("box", "ground") and tuple(hit[3]) == (0, 0, 1):
                p = o + d * hit[0]
                spot = (float(p[0]), float(p[1]), float(self.doc.boxes[hit[2]].b[5]) if hit[1] == "box" else 0.0)
        self.push_undo()
        c = src.copy()
        if kind == "box":
            ntex = len(self.doc.tex)
            c.t = c.t if c.t < ntex else -1
            c.tt = c.tt if c.tt < ntex else -1
            c.f = [v if v < ntex else -1 for v in c.f]
            w, dpt, h = c.b[3] - c.b[0], c.b[4] - c.b[1], c.b[5] - c.b[2]
            if spot:
                x0, y0 = snap(spot[0] - w / 2, g), snap(spot[1] - dpt / 2, g)
                z0 = spot[2]
            else:
                x0, y0, z0 = c.b[0] + g, c.b[1] + g, c.b[2]
            c.b = [x0, y0, z0, x0 + w, y0 + dpt, z0 + h]
            for _ in range(60):                      # never land exactly on top of an identical box
                if not any(all(abs(a - b) < 0.01 for a, b in zip(c.b, o_.b)) for o_ in self.doc.boxes):
                    break
                c.b = [c.b[0] + g, c.b[1] + g, c.b[2], c.b[3] + g, c.b[4] + g, c.b[5]]
            self.doc.boxes.append(c)
            self.sel = ("box", len(self.doc.boxes) - 1)
            self.dirty_list = True
        elif kind == "ladder":
            c.fx, c.fy = c.fx + g, c.fy + g
            self.doc.ladders.append(c)
            self.sel = ("ladder", len(self.doc.ladders) - 1)
        else:
            gg = max(8, g)
            if spot:
                c.x, c.y, c.z = snap(spot[0], gg), snap(spot[1], gg), spot[2]
            else:
                c.x, c.y = c.x + gg, c.y + gg
            self.doc.spawns.append(c)
            self.sel = ("spawn", len(self.doc.spawns) - 1)
        self.msg("Pasted")

    def focus_sel(self):
        obj = self.sel_obj()
        if obj is None:
            return
        if self.sel[0] == "box":
            c = np.array([(obj.b[0] + obj.b[3]) / 2, (obj.b[1] + obj.b[4]) / 2, (obj.b[2] + obj.b[5]) / 2])
            r = max(obj.b[3] - obj.b[0], obj.b[4] - obj.b[1], obj.b[5] - obj.b[2], 100)
        elif self.sel[0] == "ladder":
            c = np.array([obj.fx + obj.nx * 20, obj.fy + obj.ny * 20, (obj.z0 + obj.z1) / 2])
            r = max(obj.z1 - obj.z0, obj.hw * 2, 100)
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
        self.doc = MapDoc()
        self.doc.new_default()
        self.reset_cube_gl()
        self.undo, self.redo, self.sel, self.cur_tex = [], [], None, -1
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
        cam = [float(self.cam[0]), float(self.cam[1]), float(self.cam[2]), self.yaw, self.pitch]
        Path(path).write_text(json.dumps(self.doc.to_dict(False, cam), separators=(",", ":")), encoding="utf-8")
        self.doc.dirty = False
        self.title()
        self.msg("Saved " + path)
        return path

    def open_project(self, path=None):
        path = path or ask_open([("Map project", "*.fadproj *.fadmap")], "Open map")
        if not path:
            return
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            doc = MapDoc()
            cam = doc.from_dict(d)
        except Exception as ex:
            self.msg("Cannot open: %s" % str(ex)[:70])
            return
        for t in self.doc.tex + ([self.doc.sky] if self.doc.sky else []):
            if t.gid:
                gl.glDeleteTextures([t.gid])
        for t in doc.tex + ([doc.sky] if doc.sky else []):
            t.upload()
        doc.path = path if str(path).endswith(".fadproj") else None
        doc.name = Path(path).stem
        self.doc = doc
        self.reset_cube_gl()
        self.undo, self.redo, self.sel, self.cur_tex = [], [], None, -1
        if cam and len(cam) == 5:
            self.cam = np.array(cam[:3], np.float64)
            self.yaw, self.pitch = cam[3], cam[4]
        self.dirty_list = True
        self.title()
        self.msg("Opened " + Path(path).name)

    def compile(self):
        if not self.doc.path:
            if not self.save():
                self.msg("Save the project first, then compile.")
                return
        out = Path(self.doc.path).with_suffix(".fadmap")
        try:
            msgs, warns = compile_doc(self.doc, out)
        except Exception as ex:
            self.msg("Compile failed: %s" % ex, 9)
            return
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
            if p.lower().endswith((".fadproj", ".fadmap")):
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
        if ctrl and k == pygame.K_s:
            self.save(bool(mods & pygame.KMOD_SHIFT))
        elif ctrl and k == pygame.K_o:
            self.open_project()
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
        elif k == pygame.K_7:
            self.set_tool(LADDER)
            self.msg("Ladder: click a vertical wall face to place one", 5)
        elif k == pygame.K_u:
            self.cycle_turn_snap()
        elif k == pygame.K_COMMA or k == pygame.K_PERIOD:
            self.turn_sel(-1 if k == pygame.K_COMMA else 1)
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
            hit, o, d = self.pick_all(ev.pos)
            if hit and hit[1] in ("box", "spawn", "ladder"):
                self.sel = (hit[1], hit[2])
                p = o + d * hit[0]
                obj = self.sel_obj()
                if hit[1] == "box":
                    orig = list(obj.b)
                elif hit[1] == "spawn":
                    orig = (obj.x, obj.y)
                else:
                    orig = (obj.fx, obj.fy, obj.z0, obj.z1)
                self.drag = {"kind": "move", "z": float(p[2]), "start": self.plane_point(o, d, float(p[2])),
                             "orig": orig, "pushed": False}
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
        elif self.tool in (BOX, RAMP, CIRC):
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
        elif self.tool == LADDER:
            hit, o, d = self.pick_all(ev.pos, False)
            if not hit or hit[1] != "box":
                self.msg("Click a vertical wall face of a box to place a ladder")
                return
            n = hit[3]
            if abs(n[2]) > 0.5:
                self.msg("Ladders go on vertical faces (sides), not top or bottom")
                return
            p = o + d * hit[0]
            box = self.doc.boxes[hit[2]]
            # world-space normal (spun boxes)
            wn = rot_vec(box, n)
            nx, ny = float(wn[0]), float(wn[1])
            ln = math.hypot(nx, ny) or 1.0
            nx, ny = nx / ln, ny / ln
            # face plane: project click onto the box face, snap along the wall
            g = self.grid
            tx, ty = -ny, nx
            lat = (p[0] - (box.b[0] + box.b[3]) / 2) * tx + (p[1] - (box.b[1] + box.b[4]) / 2) * ty
            # face centre in XY
            if abs(n[0]) > 0.5:
                fx = box.b[3] if n[0] > 0 else box.b[0]
                fy = snap(p[1], g)
                fx, fy, _ = rot_point(box, (fx, fy, 0.0))
            else:
                fx = snap(p[0], g)
                fy = box.b[4] if n[1] > 0 else box.b[1]
                fx, fy, _ = rot_point(box, (fx, fy, 0.0))
            z0, z1 = box.b[2], box.b[5]
            if z1 - z0 < 16:
                self.msg("That wall is too short for a ladder")
                return
            self.push_undo()
            self.doc.ladders.append(Ladder(fx, fy, nx, ny, DEFAULT_LADDER_HW, z0, z1))
            self.sel = ("ladder", len(self.doc.ladders) - 1)
            self.msg("Ladder placed (%d high). Select + arrows / PageUp to adjust." % int(z1 - z0), 5)
        else:
            hit, o, d = self.pick_all(ev.pos, False)
            if hit and hit[1] in ("box", "ground") and hit[3] == (0, 0, 1):
                p = o + d * hit[0]
                g = max(8, self.grid)
                x, y = snap(p[0], g), snap(p[1], g)
                z = p[2] if hit[1] == "box" else 0.0
                if hit[1] == "box":
                    z = self.doc.boxes[hit[2]].b[5]
                team = 0 if self.tool == SPAWN0 else 1
                bl = [b.b for b in self.doc.boxes]
                if hull_hits(bl, x, y, z + HULL_H / 2 + 0.5):
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
                self.texture_face_at(ti, ev.pos)       # dragged onto a face
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
            obj = self.sel_obj()
            if obj is None:
                return
            if not dr["pushed"] and (dx or dy):
                # the object is still in its original place: remember it for undo
                if self.sel[0] == "box":
                    cur = list(obj.b)
                elif self.sel[0] == "ladder":
                    cur = (obj.fx, obj.fy, obj.z0, obj.z1)
                else:
                    cur = (obj.x, obj.y)
                self.restore_obj(obj, dr["orig"])
                self.push_undo()
                self.restore_obj(obj, cur)
                dr["pushed"] = True
            if dr["pushed"]:
                o0 = dr["orig"]
                if self.sel[0] == "box":
                    obj.b = [o0[0] + dx, o0[1] + dy, o0[2], o0[3] + dx, o0[4] + dy, o0[5]]
                elif self.sel[0] == "ladder":
                    obj.fx, obj.fy = o0[0] + dx, o0[1] + dy
                else:
                    obj.x, obj.y = o0[0] + dx, o0[1] + dy
                self.dirty_list = True

    def restore_obj(self, obj, v):
        if self.sel[0] == "box":
            obj.b = list(v)
        elif self.sel[0] == "ladder":
            obj.fx, obj.fy, obj.z0, obj.z1 = v[0], v[1], v[2], v[3]
        else:
            obj.x, obj.y = v

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
        if not self.look and not self.drag and self.in_view(self.mouse):
            hit, o, d = self.pick_all(self.mouse, self.tool in (SELECT, ROT))
            if self.tool in (SELECT, ROT, STRETCH):
                if hit and hit[1] in ("box", "spawn", "ladder"):
                    self.hover = (hit[1], hit[2])
                if self.tool == ROT:
                    self.gz_hover = self.gizmo_hit(self.mouse) is not None
                if self.tool == STRETCH:
                    hh = self.stretch_pick(self.mouse)
                    self.st_hover = (hh["ax"], hh["hi"]) if hh else None
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

    def draw_ladder(self, L, selected=False, hover=False):
        """Editor-only: translucent climb volume (Hammer func_ladder style). Invisible in-game."""
        fx, fy, nx, ny, hw, zb, zt = L.as_tuple()
        tx, ty = -ny, nx
        # thin slab in front of the wall = the climb zone the game uses
        o0, o1 = 0.5, 20.0
        xs = [fx + nx * o + tx * lat for o in (o0, o1) for lat in (-hw, hw)]
        ys = [fy + ny * o + ty * lat for o in (o0, o1) for lat in (-hw, hw)]
        b = (min(xs), min(ys), zb, max(xs), max(ys), zt)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glDepthMask(gl.GL_FALSE)
        a = 0.45 if selected else (0.32 if hover else 0.22)
        gl.glColor4f(0.2, 0.85, 0.45, a)          # green tool volume
        gl.glBegin(gl.GL_QUADS)
        for f in box_faces(b):
            for p in f[:4]:
                gl.glVertex3f(*p)
        gl.glEnd()
        gl.glDepthMask(gl.GL_TRUE)
        col = (1.0, 0.85, 0.1, 1.0) if selected else ((0.5, 1.0, 0.6, 0.95) if hover else (0.25, 0.9, 0.5, 0.7))
        self.wire_box(b, col, 2.5 if selected else 1.4, 0)
        # normal arrow so you see which way the wall faces
        mx = (b[0] + b[3]) / 2
        my = (b[1] + b[4]) / 2
        mz = (zb + zt) / 2
        gl.glLineWidth(2.5)
        gl.glColor4f(1.0, 0.9, 0.2, 0.95)
        gl.glBegin(gl.GL_LINES)
        gl.glVertex3f(mx, my, mz)
        gl.glVertex3f(mx + nx * 28, my + ny * 28, mz)
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
        self.draw_grid()
        gl.glDisable(gl.GL_TEXTURE_2D)
        for i, s in enumerate(self.doc.spawns):
            self.draw_spawn(s, self.sel == ("spawn", i))
        for i, L in enumerate(self.doc.ladders):
            self.draw_ladder(L, self.sel == ("ladder", i),
                             self.hover == ("ladder", i))
        if self.hover and self.hover[0] == "box":
            hb = self.doc.boxes[self.hover[1]]
            self.wire_box(hb.b, (1, 1, 1, 0.8), 2.0, 0.4, hb.yaw)
        if self.sel and self.sel[0] == "box" and 0 <= self.sel[1] < len(self.doc.boxes):
            sb = self.doc.boxes[self.sel[1]]
            self.wire_box(sb.b, (1.0, 0.85, 0.1, 1.0), 3.0, 0.4, sb.yaw)
        if self.tool == ROT:
            self.draw_gizmo()
        elif self.tool == STRETCH:
            self.draw_stretch()
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
        elif self.cursor3d is not None and self.tool in (BOX, RAMP, CIRC, SPAWN0, SPAWN1):
            p = self.cursor3d
            g = self.grid
            x, y = snap(p[0], g), snap(p[1], g)
            z = p[2]
            n = getattr(self, "cursor_n", (0, 0, 1))
            if self.tool in (BOX, RAMP, CIRC) and n == (0, 0, 1):
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

    def button(self, x, y, w, h, label, cb, active=False, size=13, col=None):
        mx, my = self.mouse
        over = x <= mx < x + w and y <= my < y + h
        base = (0.26, 0.45, 0.78, 1) if active else ((0.14, 0.14, 0.18, 1) if over else (0.08, 0.08, 0.10, 1))
        if col is not None:
            base = col if not over else tuple(min(1, c + 0.08) for c in col)
        self.rect(x, y, w, h, base)
        self.outline(x, y, w, h, (0.20, 0.21, 0.26, 1) if not active else (0.55, 0.72, 1, 1))
        self.text(label, x + w / 2, y + (h - size) / 2 - 1, size, (240, 242, 248), "c")
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
        bg = (0.035, 0.035, 0.045, 0.97)
        self.rect(0, 0, LW, H - SB, bg)
        self.rect(W - RW, 0, RW, H - SB, bg)
        self.rect(0, H - SB, W, SB, (0.015, 0.015, 0.02, 1))
        # ---- left panel ----
        x, y = 12, 10
        self.text("MAP BUILDER", x, y, 18, (255, 255, 255))
        self.text(self.doc.name + ("*" if self.doc.dirty else ""), x, y + 24, 12, (150, 160, 175))
        y = 56
        bw = (LW - 36) // 2
        self.button(x, y, bw, 28, "New", self.new_map)
        self.button(x + bw + 12, y, bw, 28, "Open", self.open_project)
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
                            (ROT, "Spin / turn  (5)"), (STRETCH, "Stretch  (6)"),
                            (LADDER, "Build ladder  (7)"),
                            (SPAWN0, "Spawn BLACK  (1)"), (SPAWN1, "Spawn YELLOW  (2)")):
            self.button(x, y, LW - 24, 28, label, lambda t=tool: self.set_tool(t), self.tool == tool)
            if tool in (SPAWN0, SPAWN1):
                c = TEAM_COL[0 if tool == SPAWN0 else 1]
                self.rect(x + 6, y + 9, 10, 10, (*c, 1))
            y += 32
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
        self.text("SELECTED", rx, py, 11, (130, 140, 155))
        py += 20
        obj = self.sel_obj()
        if obj is None:
            self.text("Nothing selected", rx, py, 13, (160, 168, 180))
            self.text("Click a box or spawn", rx, py + 20, 12, (120, 128, 140))
            self.text("with the Select tool (V).", rx, py + 36, 12, (120, 128, 140))
        elif self.sel[0] == "box":
            b = obj.b
            self.text("Box %d" % (self.sel[1] + 1), rx, py, 14, (255, 255, 255))
            self.text("size  %d x %d x %d" % (b[3] - b[0], b[4] - b[1], b[5] - b[2]), rx, py + 22, 13)
            self.text("at    %d, %d, %d" % (b[0], b[1], b[2]), rx, py + 40, 13)

            def tn(i):
                return self.doc.tex[i].name[:16] if 0 <= i < len(self.doc.tex) else "flat colour"
            self.text("sides  " + tn(obj.t), rx, py + 62, 12, (190, 198, 210))
            self.text("top    " + tn(obj.tt), rx, py + 80, 12, (190, 198, 210))
            self.text("tile   %d     turn  %d deg" % (obj.tile, round(obj.yaw)), rx, py + 98, 12, (190, 198, 210))
            self.text("Drop / drag an image on a face", rx, py + 120, 11, (120, 128, 140))
            self.text("= texture just that face", rx, py + 135, 11, (120, 128, 140))
        elif self.sel[0] == "ladder":
            self.text("Ladder %d" % (self.sel[1] + 1), rx, py, 14, (255, 255, 255))
            self.text("at    %d, %d" % (obj.fx, obj.fy), rx, py + 22, 13)
            self.text("height %d   width %d" % (int(obj.z1 - obj.z0), int(obj.hw * 2)), rx, py + 40, 13)
            self.text("z     %d .. %d" % (int(obj.z0), int(obj.z1)), rx, py + 58, 13)
            self.text("arrows move, Shift+arrows width", rx, py + 80, 11, (120, 128, 140))
            self.text("PageUp/Down height, Del remove", rx, py + 96, 11, (120, 128, 140))
        else:
            self.text("%s spawn" % TEAM_NAMES[obj.team], rx, py, 14, (*[int(c * 255) for c in TEAM_COL[obj.team]],))
            self.text("at  %d, %d, %d" % (obj.x, obj.y, obj.z), rx, py + 22, 13)
            self.text("facing  %d deg" % obj.yaw, rx, py + 40, 13)
            self.text("R = turn,  Del = remove", rx, py + 62, 12, (190, 198, 210))
        # ---- status bar ----
        n0 = sum(1 for s in self.doc.spawns if s.team == 0)
        n1 = len(self.doc.spawns) - n0
        info = "%d boxes   spawns: %d black / %d yellow   %d ladders   grid %d   %s" % (
            len(self.doc.boxes), n0, n1, len(self.doc.ladders), self.grid, {SELECT: "select", BOX: "build box", RAMP: "build slope", CIRC: "build circle", ROT: "spin / turn", STRETCH: "stretch", LADDER: "build ladder", SPAWN0: "spawn black", SPAWN1: "spawn yellow"}[self.tool])
        if self.tool == RAMP:
            info += "   uphill %s (R = turn it)" % RAMP_DIRS[self.ramp_dir]
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

    def draw_help(self):
        lines = (__doc__ or "").strip("\n").split("\n")
        w, h = 860, 20 * len(lines) + 30
        x, y = (self.W - w) / 2, max(10, (self.H - h) / 2)
        self.rect(x, y, w, h, (0.05, 0.06, 0.08, 0.97))
        self.outline(x, y, w, h, (0.4, 0.5, 0.7, 1), 2)
        for i, l in enumerate(lines):
            self.text(l, x + 18, y + 14 + i * 20, 13, (235, 238, 245) if (l and not l.startswith(" ")) else (190, 198, 212))

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
