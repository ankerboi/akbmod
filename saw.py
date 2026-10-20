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
  Select   click a box / spawn.   Drag it to move it.
           Arrows = move    PageUp / PageDown = up / down
           Shift + Up/Down = longer / shorter      Shift + Left/Right = narrower / wider
           Shift + PageUp/PageDown = taller / shorter
           Del = delete      Ctrl+D = duplicate      R = turn a spawn (Shift+R = other way)
  Texture  "Add image" (or drop an image file on the window), then click it in the right panel.
           Shift+click = top face only, Ctrl+click = sides only.      [ and ] = texture size
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
import zlib
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
SELECT, BOX, SPAWN0, SPAWN1 = "select", "box", "spawn0", "spawn1"
LW, RW, SB = 196, 236, 26           # left panel, right panel, status bar sizes


# --------------------------------------------------------------------------
# Map data
# --------------------------------------------------------------------------
class Box:
    __slots__ = ("b", "t", "tt", "tile", "col")

    def __init__(self, b, t=-1, tt=None, tile=128.0, col=DEFAULT_COL):
        self.b = [float(v) for v in b]
        self.t = int(t)                          # texture of the sides + bottom (-1 = flat colour)
        self.tt = self.t if tt is None else int(tt)   # texture of the top
        self.tile = float(tile)                  # world units per texture repeat
        self.col = tuple(col)

    def copy(self):
        return Box(list(self.b), self.t, self.tt, self.tile, self.col)

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


def load_image(path, max_side=1024):
    """Any image file -> opaque RGBA Surface, shrunk to max_side."""
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


class MapDoc:
    def __init__(self):
        self.boxes = []
        self.spawns = []
        self.tex = []
        self.path = None
        self.name = "untitled"
        self.dirty = False

    def new_default(self):
        self.boxes = [Box([-1024, -1024, -32, 1024, 1024, 0])]
        self.spawns = []

    def snap(self):
        return ([b.copy() for b in self.boxes], [s.copy() for s in self.spawns])

    def restore(self, s):
        self.boxes = [b.copy() for b in s[0]]
        self.spawns = [x.copy() for x in s[1]]

    def to_dict(self, compiled=False, cam=None, max_side=1024):
        boxes = self.boxes
        used = sorted({t for b in boxes for t in (b.t, b.tt) if t >= 0}) if compiled else list(range(len(self.tex)))
        remap = {old: i for i, old in enumerate(used)}
        d = {
            "format": MAP_FORMAT, "version": MAP_VERSION, "name": self.name,
            "textures": [encode_tex(self.tex[i], max_side) for i in used],
            "boxes": [{"b": [round(v, 3) for v in b.b], "t": remap.get(b.t, -1), "tt": remap.get(b.tt, -1),
                       "tile": b.tile, "c": list(b.col)} for b in boxes],
            "spawns": [[{"p": [round(s.x, 2), round(s.y, 2), round(s.z, 2)], "yaw": round(s.yaw, 1)}
                        for s in self.spawns if s.team == t] for t in (0, 1)],
        }
        if cam:
            d["cam"] = cam
        return d

    def from_dict(self, d):
        if d.get("format") != MAP_FORMAT:
            raise ValueError("not a FAD-MOD map file")
        self.tex = [decode_tex(t) for t in d.get("textures", [])]
        self.boxes = [Box(b["b"], b.get("t", -1), b.get("tt"), b.get("tile", 128), b.get("c", DEFAULT_COL))
                      for b in d.get("boxes", [])]
        for b in self.boxes:
            b.fix()
        self.spawns = []
        for t, lst in enumerate(d.get("spawns", [[], []])[:2]):
            for s in lst:
                p = s["p"]
                self.spawns.append(Spawn(t, p[0], p[1], p[2], s.get("yaw", 0)))
        self.name = d.get("name", "untitled")
        return d.get("cam")


# --------------------------------------------------------------------------
# Compile (checks the map, fixes spawn points, writes the game file)
# --------------------------------------------------------------------------
def hull_hits(boxes, x, y, zc):
    """True if the player hull centred at (x, y, zc) overlaps a box (strict, like the game)."""
    for b in boxes:
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
    for b in doc.boxes:
        c = tuple(round(v, 2) for v in b.b)
        if c[3] - c[0] < 1 or c[4] - c[1] < 1 or c[5] - c[2] < 1:
            dropped += 1
            continue
        key = (c, b.t, b.tt, b.tile, b.col)
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
    bl = [b.b for b in boxes]

    spawns = []
    for s in doc.spawns:
        if standing_ok(bl, s.x, s.y, s.z):
            spawns.append(s.copy())
        else:
            warns.append("a %s spawn at (%d, %d, %d) is inside a wall or floating: dropped" % (TEAM_NAMES[s.team], s.x, s.y, s.z))
    # a team without spawn points gets some automatically
    cands = []
    for b in bl:
        if b[3] - b[0] < 96 or b[4] - b[1] < 96:
            continue
        z = b[5]
        x = b[0] + 48
        while x <= b[3] - 47:
            y = b[1] + 48
            while y <= b[4] - 47:
                if standing_ok(bl, x, y, z):
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

    tmp = MapDoc()
    tmp.boxes, tmp.spawns, tmp.tex, tmp.name = boxes, spawns, doc.tex, doc.name
    d = tmp.to_dict(compiled=True, max_side=512)
    d["void_z"] = round(min(b[2] for b in bl) - 800.0, 1)
    d["bounds"] = [round(min(b[i] for b in bl), 1) for i in range(3)] + [round(max(b[i + 3] for b in bl), 1) for i in range(3)]
    raw = json.dumps(d, separators=(",", ":")).encode("utf-8")
    Path(out_path).write_bytes(raw)
    n0 = sum(1 for s in spawns if s.team == 0)
    msgs.append("%d boxes, %d textures, spawns: %d BLACK / %d YELLOW, %.1f KB" % (
        len(boxes), len(d["textures"]), n0, len(spawns) - n0, len(raw) / 1024.0))
    return msgs, warns


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


IMG_KINDS = [("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.tga *.webp")]


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
        self.tool = BOX
        self.gi = 2                      # grid index
        self.height = 128.0              # height of new boxes
        self.tile = 128.0                # texture size of new boxes
        self.cur_tex = -1                # texture for new boxes
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
        lst = self.doc.boxes if kind == "box" else self.doc.spawns
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
            h = ray_box(o, d, b.b)
            if h and (best is None or h[0] < best[0]):
                best = (h[0], "box", i, h[1])
        if want_spawns:
            for i, s in enumerate(self.doc.spawns):
                h = ray_box(o, d, (s.x - 16, s.y - 16, s.z, s.x + 16, s.y + 16, s.z + HULL_H))
                if h and (best is None or h[0] < best[0]):
                    best = (h[0], "spawn", i, h[1])
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
    def add_texture(self, path):
        try:
            s = load_image(path)
        except Exception as ex:
            self.msg("Cannot read that image: %s" % str(ex)[:60])
            return -1
        t = Tex(Path(path).name, s)
        t.upload()
        self.doc.tex.append(t)
        self.cur_tex = len(self.doc.tex) - 1
        self.apply_texture(self.cur_tex, 0)
        self.msg("Added texture %s" % t.name)
        return self.cur_tex

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
            self.dirty_list = True

    # ----- editing -----
    def new_box(self, b):
        self.push_undo()
        nb = Box(b, self.cur_tex, self.cur_tex, self.tile)
        nb.fix()
        self.doc.boxes.append(nb)
        self.sel = ("box", len(self.doc.boxes) - 1)

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

    def key_edit(self, key, mods):
        obj = self.sel_obj()
        if obj is None:
            return False
        g = self.grid
        shift = bool(mods & pygame.KMOD_SHIFT)
        dirs = {pygame.K_UP: "f+", pygame.K_DOWN: "f-", pygame.K_RIGHT: "r+", pygame.K_LEFT: "r-"}
        if key in dirs or key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            self.push_undo()
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
            (self.doc.boxes if kind == "box" else self.doc.spawns).pop(i)
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
        else:
            c.x += g
            c.y += g
            self.doc.spawns.append(c)
            self.sel = ("spawn", len(self.doc.spawns) - 1)

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
        for t in self.doc.tex:
            if t.gid:
                gl.glDeleteTextures([t.gid])
        self.doc = MapDoc()
        self.doc.new_default()
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
        for t in self.doc.tex:
            if t.gid:
                gl.glDeleteTextures([t.gid])
        for t in doc.tex:
            t.upload()
        doc.path = path if str(path).endswith(".fadproj") else None
        doc.name = Path(path).stem
        self.doc = doc
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
                self.add_texture(p)
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
        elif ctrl and k == pygame.K_n:
            self.new_map()
        elif k == pygame.K_F5:
            self.compile()
        elif k == pygame.K_F1:
            self.help = not self.help
        elif k == pygame.K_ESCAPE:
            if self.drag:
                self.drag = None
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
            if hit and hit[1] in ("box", "spawn"):
                self.sel = (hit[1], hit[2])
                p = o + d * hit[0]
                obj = self.sel_obj()
                self.drag = {"kind": "move", "z": float(p[2]), "start": self.plane_point(o, d, float(p[2])),
                             "orig": list(obj.b) if hit[1] == "box" else (obj.x, obj.y), "pushed": False}
            else:
                self.sel = None
        elif self.tool == BOX:
            z = self.place_plane(ev.pos)
            if z is None:
                self.msg("Click on the floor or on top of a box to start a box")
                return
            o, d = self.ray(ev.pos)
            p = self.plane_point(o, d, z)
            if p is None:
                return
            g = self.grid
            self.drag = {"kind": "box", "z": z, "a": (snap(p[0], g), snap(p[1], g)), "cur": (snap(p[0], g), snap(p[1], g))}
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
        if dr["kind"] == "box":
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
        p = self.plane_point(o, d, dr["z"])
        if p is None:
            return
        g = self.grid
        if dr["kind"] == "box":
            dr["cur"] = (snap(p[0], g), snap(p[1], g))
        elif dr["kind"] == "move" and dr["start"] is not None:
            dx, dy = snap(p[0] - dr["start"][0], g), snap(p[1] - dr["start"][1], g)
            obj = self.sel_obj()
            if obj is None:
                return
            if not dr["pushed"] and (dx or dy):
                # the object is still in its original place: remember it for undo
                cur = list(obj.b) if self.sel[0] == "box" else (obj.x, obj.y)
                self.restore_obj(obj, dr["orig"])
                self.push_undo()
                self.restore_obj(obj, cur)
                dr["pushed"] = True
            if dr["pushed"]:
                o0 = dr["orig"]
                if self.sel[0] == "box":
                    obj.b = [o0[0] + dx, o0[1] + dy, o0[2], o0[3] + dx, o0[4] + dy, o0[5]]
                else:
                    obj.x, obj.y = o0[0] + dx, o0[1] + dy
                self.dirty_list = True

    def restore_obj(self, obj, v):
        if self.sel[0] == "box":
            obj.b = list(v)
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
        if not self.look and not self.drag and self.in_view(self.mouse):
            hit, o, d = self.pick_all(self.mouse, self.tool == SELECT)
            if self.tool == SELECT:
                if hit and hit[1] in ("box", "spawn"):
                    self.hover = (hit[1], hit[2])
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
            for f in box_faces(b.b):
                n = f[4]
                ti = b.tt if n[2] > .5 else b.t
                tex = self.doc.tex[ti] if 0 <= ti < len(self.doc.tex) else None
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex.gid if tex else self.white)
                s = shade(n)
                if tex:
                    gl.glColor3f(s, s, s)
                else:
                    gl.glColor3f(b.col[0] / 255 * s, b.col[1] / 255 * s, b.col[2] / 255 * s)
                gl.glBegin(gl.GL_QUADS)
                for p in f[:4]:
                    u, v = face_uv(p, n, b.tile)
                    gl.glTexCoord2f(u, v)
                    gl.glVertex3f(*p)
                gl.glEnd()
        gl.glEndList()
        self.dirty_list = False

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
    def wire_box(b, col, w=2.0, e=0.4):
        x0, y0, z0, x1, y1, z1 = b[0] - e, b[1] - e, b[2] - e, b[3] + e, b[4] + e, b[5] + e
        gl.glLineWidth(w)
        gl.glColor4f(*col)
        gl.glBegin(gl.GL_LINES)
        for a, c in (((x0, y0, z0), (x1, y0, z0)), ((x1, y0, z0), (x1, y1, z0)), ((x1, y1, z0), (x0, y1, z0)),
                     ((x0, y1, z0), (x0, y0, z0)), ((x0, y0, z1), (x1, y0, z1)), ((x1, y0, z1), (x1, y1, z1)),
                     ((x1, y1, z1), (x0, y1, z1)), ((x0, y1, z1), (x0, y0, z1)), ((x0, y0, z0), (x0, y0, z1)),
                     ((x1, y0, z0), (x1, y0, z1)), ((x1, y1, z0), (x1, y1, z1)), ((x0, y1, z0), (x0, y1, z1))):
            gl.glVertex3f(*a)
            gl.glVertex3f(*c)
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

    def draw_world(self):
        W, H = self.W, self.H
        gl.glViewport(0, 0, W, H)
        gl.glClearColor(0.11, 0.12, 0.15, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LEQUAL)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        self.set_camera()
        if self.dirty_list or self.list_id is None:
            self.build_list()
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glCallList(self.list_id)
        self.draw_grid()
        gl.glDisable(gl.GL_TEXTURE_2D)
        for i, s in enumerate(self.doc.spawns):
            self.draw_spawn(s, self.sel == ("spawn", i))
        if self.hover and self.hover[0] == "box":
            self.wire_box(self.doc.boxes[self.hover[1]].b, (1, 1, 1, 0.8), 2.0)
        if self.sel and self.sel[0] == "box" and 0 <= self.sel[1] < len(self.doc.boxes):
            self.wire_box(self.doc.boxes[self.sel[1]].b, (1.0, 0.85, 0.1, 1.0), 3.0)
        dr = self.drag
        if dr and dr["kind"] == "box":
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
        elif self.cursor3d is not None and self.tool in (BOX, SPAWN0, SPAWN1):
            p = self.cursor3d
            g = self.grid
            x, y = snap(p[0], g), snap(p[1], g)
            z = p[2]
            n = getattr(self, "cursor_n", (0, 0, 1))
            if self.tool == BOX and n == (0, 0, 1):
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
        base = (0.26, 0.45, 0.78, 1) if active else ((0.24, 0.26, 0.32, 1) if over else (0.17, 0.19, 0.23, 1))
        if col is not None:
            base = col if not over else tuple(min(1, c + 0.08) for c in col)
        self.rect(x, y, w, h, base)
        self.outline(x, y, w, h, (0.36, 0.40, 0.48, 1) if not active else (0.55, 0.72, 1, 1))
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
        bg = (0.09, 0.10, 0.12, 0.94)
        self.rect(0, 0, LW, H - SB, bg)
        self.rect(W - RW, 0, RW, H - SB, bg)
        self.rect(0, H - SB, W, SB, (0.07, 0.08, 0.10, 1))
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
                            (SPAWN0, "Spawn BLACK  (1)"), (SPAWN1, "Spawn YELLOW  (2)")):
            self.button(x, y, LW - 24, 28, label, lambda t=tool: setattr(self, "tool", t), self.tool == tool)
            if tool in (SPAWN0, SPAWN1):
                c = TEAM_COL[0 if tool == SPAWN0 else 1]
                self.rect(x + 6, y + 9, 10, 10, (*c, 1))
            y += 32
        y += 6
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
            self.outline(cx, cy, th, th, (1, 0.85, 0.1, 1) if sel else (0.3, 0.33, 0.4, 1), 3.0 if sel else 1.0)
            self.text(nm[:11] + ("." if len(nm) > 11 else ""), cx, cy + th + 2, 11, (170, 178, 190))
            if top <= self.mouse[1] < bot:
                self.hot.append((cx, max(cy, top), th, min(th, bot - cy), lambda ti=ti: self.apply_texture(ti, pygame.key.get_mods())))
        gl.glDisable(gl.GL_SCISSOR_TEST)
        # ---- right panel: selection info ----
        py = H - SB - 180
        self.rect(W - RW, py - 6, RW, 1, (0.3, 0.33, 0.4, 1))
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
            self.text("tile   %d" % obj.tile, rx, py + 98, 12, (190, 198, 210))
            self.text("Shift+click texture = top", rx, py + 120, 11, (120, 128, 140))
            self.text("Ctrl+click texture = sides", rx, py + 135, 11, (120, 128, 140))
        else:
            self.text("%s spawn" % TEAM_NAMES[obj.team], rx, py, 14, (*[int(c * 255) for c in TEAM_COL[obj.team]],))
            self.text("at  %d, %d, %d" % (obj.x, obj.y, obj.z), rx, py + 22, 13)
            self.text("facing  %d deg" % obj.yaw, rx, py + 40, 13)
            self.text("R = turn,  Del = remove", rx, py + 62, 12, (190, 198, 210))
        # ---- status bar ----
        n0 = sum(1 for s in self.doc.spawns if s.team == 0)
        n1 = len(self.doc.spawns) - n0
        info = "%d boxes   spawns: %d black / %d yellow   grid %d   %s" % (
            len(self.doc.boxes), n0, n1, self.grid, {SELECT: "select", BOX: "build box", SPAWN0: "spawn black", SPAWN1: "spawn yellow"}[self.tool])
        self.text(info, 10, H - SB + 5, 13, (170, 178, 190))
        if self.msgs:
            self.text(self.msgs[-1][0][:170], W - 10, H - SB + 5, 13, (255, 220, 120), "r")
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
