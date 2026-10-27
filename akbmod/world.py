"""World collision and rendering: AkbMapWorld (maps come from .akbmap files)."""
import base64
import io
import math
import zlib
import numpy as np
import pygame
from OpenGL import GL as gl

from .config import *
from .glutil import make_texture, checker_tex, sky_sphere_list, FX, tex2d

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
    _FACES = ((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0),
              (0.0, 1.0, 0.0), (0.0, 0.0, -1.0), (0.0, 0.0, 1.0))

    def draw_sky(self, x, y, z):
        pass

    def in_px_zone(self, x, y, z):
        """Pixel surf zones (map builder) - none by default, so pixel surf only works on maps that have one."""
        return False

    def ladder_at(self, x, y, z, dx=0.0, dy=0.0, reach=0.0):
        """Ladders (map builder tool 9): outward normal (nx, ny) of a ladder the player hull touches - none by default."""
        return None

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


class NoMapWorld(World):
    """Placeholder used until a .akbmap is loaded: no geometry, nothing to draw. Matches cannot start on it."""
    B = []
    meta = []
    name = "No map"

    def __init__(self):
        self.spawns = [[[0, 0, 40]], [[0, 0, 40]]]

    def solid(self, x, y, z):
        return False

    def draw(self):
        pass


class AkbMapWorld(World):
    """A map compiled by the AKB-MOD Map Builder (.akbmap): boxes with optional textures + team spawns."""

    def __init__(self, d):
        import base64
        import zlib
        if not isinstance(d, dict) or d.get("format") not in ("akbmap", "fadmap"):
            raise ValueError("not a AKB-MOD map file")
        self.name = str(d.get("name", "map"))
        self.B, self.meta = [], []
        self.K, self.D, self.Y = [], [], []      # shape kind per box (0 box, 1 slope, 2 cylinder), slope uphill direction, spin (deg)
        self.DEC = []                            # decals: flat pictures on walls, drawn only (no collision)
        for b in d.get("boxes", []):
            if b.get("dc"):
                self.DEC.append(([float(x) for x in b["b"]],
                                 [int(v) for v in ((b.get("f") or []) + [-1] * 6)[:6]],
                                 float(b.get("tile", 128) or 128), tuple(b.get("c") or (176, 160, 144))))
                continue
            v = [float(x) for x in b["b"]]
            for i in range(3):
                if v[i] > v[i + 3]:
                    v[i], v[i + 3] = v[i + 3], v[i]
            self.B.append(v)
            _k = int(b.get("k", 0) or 0)
            self.K.append(_k if _k in (0, 1, 2) else 0)
            self.D.append(int(b.get("d", 0) or 0) % 4)
            try:
                _y = float(b.get("yaw", 0) or 0) % 360.0
            except (TypeError, ValueError):
                _y = 0.0
            self.Y.append(0.0 if (_y < 1e-4 or _y > 360.0 - 1e-4) else _y)
            col = b.get("c") or (176, 160, 144)
            self.meta.append((int(b.get("t", -1)), int(b.get("tt", b.get("t", -1)) if b.get("tt") is not None else b.get("t", -1)),
                              float(b.get("tile", 128) or 128), tuple(col),
                              [int(v) for v in ((b.get("f") or []) + [-1] * 6)[:6]]))
        bsp = d.get("bsp")
        if not self.B and not bsp:
            raise ValueError("the map has no boxes")
        self._bsp = None
        self._BG = self._BS = None               # Source map brushes: grid of cells -> player hull / bullet tests
        self._bsp_list = None
        if bsp:
            self._bsp_init(bsp)
        # plain boxes are tested the fast way; slopes / cylinders have their own tests
        self._bx = [b for b, k, y in zip(self.B, self.K, self.Y) if k == 0 and not y]
        self._sp = [(b, k, dd) for b, k, dd, y in zip(self.B, self.K, self.D, self.Y) if k and not y]
        # spun shapes: tested in their own frame (point is turned back by the spin, normals are turned forward)
        self._rt = []
        for b, k, dd, y in zip(self.B, self.K, self.D, self.Y):
            if y:
                a = math.radians(y)
                self._rt.append((b, k, dd, math.cos(a), math.sin(a), (b[0] + b[3]) * .5, (b[1] + b[4]) * .5))
        # pixel surf zones (map builder tool 8): invisible, not solid; pixel surf only works while you are inside one
        self.PXZ = []
        for z in d.get("pxz") or []:
            try:
                zb = [float(v) for v in z["b"]]
                for i in range(3):
                    if zb[i] > zb[i + 3]:
                        zb[i], zb[i + 3] = zb[i + 3], zb[i]
                self.PXZ.append((zb, float(z.get("yaw", 0) or 0)))
            except Exception:
                continue
        # ladders (map builder tool 9): invisible, not solid. n = unit vector from the ladder toward the climber (spun with the zone)
        self.LAD = []
        for z in d.get("lad") or []:
            try:
                zb = [float(v) for v in z["b"]]
                for i in range(3):
                    if zb[i] > zb[i + 3]:
                        zb[i], zb[i + 3] = zb[i + 3], zb[i]
                dd = int(z.get("d", 0) or 0) % 4
                yaw = float(z.get("yaw", 0) or 0)
                nl = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[dd]
                a = math.radians(yaw)
                ca, sa = math.cos(a), math.sin(a)
                self.LAD.append((zb, yaw, dd, (nl[0] * ca - nl[1] * sa, nl[0] * sa + nl[1] * ca)))
            except Exception:
                continue
        self.spawns = [[], []]
        for team, lst in enumerate((d.get("spawns") or [[], []])[:2]):
            for sp in lst:
                p = sp["p"]
                # builder z = feet, game z = body centre (feet + 36); +0.5 so we never start inside the floor
                self.spawns[team].append([float(p[0]), float(p[1]), float(p[2]) + 36.5, float(sp.get("yaw", 0))])
        for team in (0, 1):
            if not self.spawns[team]:
                if self.B:
                    top = max(self.B, key=lambda b: (b[3] - b[0]) * (b[4] - b[1]))
                    self.spawns[team].append([(top[0] + top[3]) / 2, (top[1] + top[4]) / 2, top[5] + 36.5, 0.0])
                else:
                    bb = self._bsp["bounds"]
                    self.spawns[team].append([(bb[0] + bb[3]) / 2, (bb[1] + bb[4]) / 2, bb[5] + 36.5, 0.0])
        vz = d.get("void_z")
        if vz is not None:
            self.void_z = float(vz)
        elif self.B:
            self.void_z = min(b[2] for b in self.B) - 800.0
        else:
            self.void_z = float(self._bsp["bounds"][2]) - 800.0
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
        # skybox image from the map builder (equirectangular panorama)
        self._sky_raw = None
        self._sky_tid = None
        self._sky_list = None
        sk = d.get("sky")
        if sk:
            try:
                if "png" in sk:
                    surf = pygame.image.load(io.BytesIO(base64.b64decode(sk["png"])), "x.png").convert_alpha()
                    w, h = surf.get_size()
                    data = pygame.image.tostring(surf, "RGBA", False)
                else:
                    w, h = int(sk["w"]), int(sk["h"])
                    data = zlib.decompress(base64.b64decode(sk["raw"]))
                self._sky_raw = (data, w, h)
            except Exception as ex:
                print("map skybox failed:", ex)

    def draw_sky(self, x, y, z):
        if self._sky_raw is None:
            return
        if self._sky_tid is None:
            data, w, h = self._sky_raw
            self._sky_tid = make_texture(np.frombuffer(data, np.uint8).reshape(h, w, 4), w, h)
            self._sky_list = sky_sphere_list(5000.0)
        gl.glUseProgram(0)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._sky_tid)
        c = 0.30 if not FX.day else 1.0          # night maps get a dark sky
        gl.glColor3f(c, c, c)
        gl.glPushMatrix()
        gl.glTranslatef(x, y, z)
        gl.glCallList(self._sky_list)
        gl.glPopMatrix()
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glColor4f(1, 1, 1, 1)

    # ---- slopes (wedges) and cylinders ----
    # slope: bounding box b, uphill direction d (0 +x, 1 +y, 2 -x, 3 -y); the top rises from b[2] (low end) to b[5] (high end)
    # cylinder: the ellipse inscribed in the bounding box, from b[2] to b[5]
    @staticmethod
    def _slope_u(b, d, x, y):
        """Position along the uphill direction: 0 at the low end .. 1 at the high end (not clamped)."""
        if d == 0:
            return (x - b[0]) / max(b[3] - b[0], 1e-6)
        if d == 2:
            return (b[3] - x) / max(b[3] - b[0], 1e-6)
        if d == 1:
            return (y - b[1]) / max(b[4] - b[1], 1e-6)
        return (b[4] - y) / max(b[4] - b[1], 1e-6)

    @classmethod
    def _slope_top(cls, b, d, x, y):
        u = cls._slope_u(b, d, x, y)
        u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)
        return b[2] + u * (b[5] - b[2])

    @staticmethod
    def _slope_n(b, d):
        """Unit normal of the slanted top surface."""
        along_x = d in (0, 2)
        L = (b[3] - b[0]) if along_x else (b[4] - b[1])
        H = b[5] - b[2]
        sx, sy = ((1, 0), (0, 1), (-1, 0), (0, -1))[d]
        l = math.sqrt(H * H + L * L) or 1.0
        return (-sx * H / l, -sy * H / l, L / l)

    # ---- Source maps (.gma / .bsp made in the map builder): triangle mesh + convex brushes ----
    _CELL = 256.0

    def _bsp_init(self, b):
        def u(k, dt, shape):
            return np.frombuffer(zlib.decompress(base64.b64decode(b[k])), dt).reshape(shape)
        n, nb, npl = int(b["n"]), int(b["nb"]), int(b["np"])
        self._bsp = {"n": n, "bounds": [float(v) for v in b.get("bounds", [0] * 6)], "d": b,
                     "pos": u("pos", np.float32, (n, 3)), "uv": u("uv", np.float32, (n, 2)),
                     "col": u("col", np.uint8, (n, 4)), "idx": u("idx", np.uint32, (-1,))}
        planes = u("planes", np.float32, (npl, 4)).astype(np.float64)
        first, cnt = u("first", np.int32, (nb,)), u("cnt", np.int32, (nb,))
        flags, bbox = u("flags", np.uint8, (nb,)), u("bbox", np.float32, (nb, 6)).astype(np.float64)
        cell = self._CELL
        gp, gs = {}, {}
        for i in range(nb):
            f, c, fl = int(first[i]), int(cnt[i]), int(flags[i])
            pl = planes[f:f + c]
            bb = bbox[i]
            nx, ny, nz, dd = pl[:, 0].tolist(), pl[:, 1].tolist(), pl[:, 2].tolist(), pl[:, 3]
            for grid, hull in ((gp, fl & 1), (gs, fl & 2)):
                if not hull:
                    continue
                if grid is gp:                      # the player is a 32 x 32 x 72 box: push every plane out by its reach
                    de = (dd + 16.0 * np.abs(pl[:, 0]) + 16.0 * np.abs(pl[:, 1]) + 36.0 * np.abs(pl[:, 2])).tolist()
                    ex = (16.0, 16.0, 36.0)
                else:
                    de = dd.tolist()
                    ex = (0.0, 0.0, 0.0)
                ent = (bb[0] - ex[0], bb[1] - ex[1], bb[2] - ex[2], bb[3] + ex[0], bb[4] + ex[1], bb[5] + ex[2],
                       tuple(zip(nx, ny, nz, de)))
                for cx in range(int(math.floor(ent[0] / cell)), int(math.floor(ent[3] / cell)) + 1):
                    for cy in range(int(math.floor(ent[1] / cell)), int(math.floor(ent[4] / cell)) + 1):
                        grid.setdefault((cx, cy), []).append(ent)
        self._BG = gp if gp else None
        self._BS = gs if gs else None

    def _bsp_hit(self, grid, x, y, z):
        lst = grid.get((int(x // 256.0), int(y // 256.0)))
        if lst:
            for x0, y0, z0, x1, y1, z1, pl in lst:
                if x0 < x < x1 and y0 < y < y1 and z0 < z < z1:
                    for nx, ny, nz, d in pl:
                        if nx * x + ny * y + nz * z >= d:
                            break
                    else:
                        return True
        return False

    def _bsp_normal(self, x, y, z, want_up, move):
        """Normal of the Source-map surface touching the (empty) point, or None."""
        lst = self._BG.get((int(x // 256.0), int(y // 256.0)))
        if not lst:
            return None
        best, bd = None, 1e30
        for x0, y0, z0, x1, y1, z1, pl in lst:
            if not (x0 - 3 < x < x1 + 3 and y0 - 3 < y < y1 + 3 and z0 - 3 < z < z1 + 3):
                continue
            sv = [nx * x + ny * y + nz * z - d for nx, ny, nz, d in pl]
            m = max(sv)
            if m > 1.5:
                continue                                   # not touching this brush
            for (nx, ny, nz, d), s in zip(pl, sv):
                if want_up:
                    if nz < 0.05 or s < m - 6.0:
                        continue
                elif s < m - 0.5:
                    continue
                if move is not None and nx * move[0] + ny * move[1] + nz * move[2] > -1e-4:
                    continue
                if abs(s) < bd:
                    bd = abs(s)
                    best = (nx, ny, nz)
        return best

    def _bsp_build(self):
        b = self._bsp
        tids = []
        for t in b["d"].get("tex", []):
            if not t.get("img"):
                tids.append(None)
                continue
            try:
                raw = base64.b64decode(t["img"])
                s = pygame.image.load(io.BytesIO(raw), "x.jpg" if raw[:2] == b"\xff\xd8" else "x.png").convert_alpha()
                w, h = s.get_size()
                tids.append(make_texture(np.frombuffer(pygame.image.tostring(s, "RGBA", False), np.uint8).reshape(h, w, 4), w, h))
            except Exception as ex:
                print("map texture failed:", ex)
                tids.append(None)
        pos, uv, col, idx = b["pos"], b["uv"], b["col"], b["idx"]
        self._bsp_keep = (pos, uv, col, idx, tids)
        self._bsp_list = gl.glGenLists(1)
        gl.glNewList(self._bsp_list, gl.GL_COMPILE)
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glEnableClientState(gl.GL_COLOR_ARRAY)
        gl.glVertexPointer(3, gl.GL_FLOAT, 0, pos)
        gl.glTexCoordPointer(2, gl.GL_FLOAT, 0, uv)
        gl.glColorPointer(4, gl.GL_UNSIGNED_BYTE, 0, col)
        batches = b["d"].get("batches", [])
        for mode in (0, 1, 2):
            if mode == 1:
                gl.glEnable(gl.GL_ALPHA_TEST)
                gl.glAlphaFunc(gl.GL_GREATER, 0.5)
            elif mode == 2:
                gl.glDisable(gl.GL_ALPHA_TEST)
                gl.glEnable(gl.GL_BLEND)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                gl.glDepthMask(gl.GL_FALSE)
            for ti, md, first, cnt in batches:
                if md != mode:
                    continue
                tid = tids[ti] if 0 <= ti < len(tids) else None
                gl.glBindTexture(gl.GL_TEXTURE_2D, tid or self._white)
                gl.glDrawElements(gl.GL_TRIANGLES, int(cnt), gl.GL_UNSIGNED_INT, idx[int(first):int(first) + int(cnt)])
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_ALPHA_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glDisableClientState(gl.GL_COLOR_ARRAY)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEndList()

    def ladder_at(self, x, y, z, dx=0.0, dy=0.0, reach=0.0):
        """Normal (nx, ny) of a ladder the player hull (x, y = centre, z = body centre) touches after moving `reach`
        units along the unit direction (dx, dy). Only ladders that direction heads INTO count (like the Source trace)."""
        for b, yaw, _d, n in self.LAD:
            if dx * n[0] + dy * n[1] > LADDER_MOUNT_DOT:
                continue
            px, py = x + dx * reach, y + dy * reach
            if yaw:
                a = math.radians(yaw)
                c, s = math.cos(a), math.sin(a)
                cx, cy = (b[0] + b[3]) * .5, (b[1] + b[4]) * .5
                ex, ey = px - cx, py - cy
                px, py = cx + ex * c + ey * s, cy - ex * s + ey * c
            if (b[0] - 16 < px < b[3] + 16 and b[1] - 16 < py < b[4] + 16
                    and z - 36 < b[5] and z + 36 > b[2]):
                return n
        return None

    def in_px_zone(self, x, y, z):
        """True while any part of the player (x, y = centre, z = body centre) is inside a pixel surf zone."""
        for b, yaw in self.PXZ:
            if yaw:
                a = math.radians(yaw)
                c, s = math.cos(a), math.sin(a)
                cx, cy = (b[0] + b[3]) * .5, (b[1] + b[4]) * .5
                dx, dy = x - cx, y - cy
                x, y = cx + dx * c + dy * s, cy - dx * s + dy * c
            if (b[0] - 16 < x < b[3] + 16 and b[1] - 16 < y < b[4] + 16
                    and z - 36 < b[5] and z + 36 > b[2]):
                return True
        return False

    def solid(self, x, y, z):
        if self._BG is not None and self._bsp_hit(self._BG, x, y, z):
            return True
        for b in self._bx:
            if (b[0] - 16 < x < b[3] + 16 and b[1] - 16 < y < b[4] + 16
                    and b[2] - 36 < z < b[5] + 36):
                return True
        for b, k, d in self._sp:
            if not (b[0] - 16 < x < b[3] + 16 and b[1] - 16 < y < b[4] + 16 and b[2] - 36 < z < b[5] + 36):
                continue
            if k == 2:
                ex = (x - (b[0] + b[3]) * .5) / ((b[3] - b[0]) * .5 + 16)
                ey = (y - (b[1] + b[4]) * .5) / ((b[4] - b[1]) * .5 + 16)
                if ex * ex + ey * ey < 1.0:
                    return True
            elif z < self._slope_top(b, d, x, y) + 36:
                return True
        for b, k, d, c, s, cx, cy in self._rt:
            dx, dy = x - cx, y - cy
            lx, ly = cx + dx * c + dy * s, cy - dx * s + dy * c
            if not (b[0] - 16 < lx < b[3] + 16 and b[1] - 16 < ly < b[4] + 16 and b[2] - 36 < z < b[5] + 36):
                continue
            if k == 0:
                return True
            if k == 2:
                ex = (lx - cx) / ((b[3] - b[0]) * .5 + 16)
                ey = (ly - cy) / ((b[4] - b[1]) * .5 + 16)
                if ex * ex + ey * ey < 1.0:
                    return True
            elif z < self._slope_top(b, d, lx, ly) + 36:
                return True
        return False

    def bullet_solid(self, x, y, z):
        if self._BS is not None and self._bsp_hit(self._BS, x, y, z):
            return True
        for b in self._bx:
            if b[0] < x < b[3] and b[1] < y < b[4] and b[2] < z < b[5]:
                return True
        for b, k, d in self._sp:
            if not (b[0] < x < b[3] and b[1] < y < b[4] and b[2] < z < b[5]):
                continue
            if k == 2:
                ex = (x - (b[0] + b[3]) * .5) / ((b[3] - b[0]) * .5)
                ey = (y - (b[1] + b[4]) * .5) / ((b[4] - b[1]) * .5)
                if ex * ex + ey * ey < 1.0:
                    return True
            elif z < self._slope_top(b, d, x, y):
                return True
        for b, k, d, c, s, cx, cy in self._rt:
            dx, dy = x - cx, y - cy
            lx, ly = cx + dx * c + dy * s, cy - dx * s + dy * c
            if not (b[0] < lx < b[3] and b[1] < ly < b[4] and b[2] < z < b[5]):
                continue
            if k == 0:
                return True
            if k == 2:
                ex = (lx - cx) / ((b[3] - b[0]) * .5)
                ey = (ly - cy) / ((b[4] - b[1]) * .5)
                if ex * ex + ey * ey < 1.0:
                    return True
            elif z < self._slope_top(b, d, lx, ly):
                return True
        return False

    def _cand1(self, b, k, d, x, y, z):
        """(signed distances, normals) of one slope / cylinder / box at the point, in the shape's own frame."""
        if k == 0:
            return ((b[0] - 16 - x, x - (b[3] + 16), b[1] - 16 - y, y - (b[4] + 16), b[2] - 36 - z, z - (b[5] + 36)),
                    self._FACES)
        if k == 2:
            cx, cy = (b[0] + b[3]) * .5, (b[1] + b[4]) * .5
            Rx, Ry = (b[3] - b[0]) * .5 + 16, (b[4] - b[1]) * .5 + 16
            dx, dy = x - cx, y - cy
            r = math.hypot(dx, dy)
            if r > 1e-6:
                ca, sa = dx / r, dy / r
                rr = 1.0 / math.sqrt((ca / Rx) ** 2 + (sa / Ry) ** 2)
                gx, gy = dx / (Rx * Rx), dy / (Ry * Ry)
                gl_ = math.hypot(gx, gy) or 1.0
                side, ns = r - rr, (gx / gl_, gy / gl_, 0.0)
            else:
                side, ns = -min(Rx, Ry), (1.0, 0.0, 0.0)
            return ((side, b[2] - 36 - z, z - (b[5] + 36)), (ns, (0.0, 0.0, -1.0), (0.0, 0.0, 1.0)))
        u = self._slope_u(b, d, x, y)
        if 0.0 < u < 1.0:
            tn = self._slope_n(b, d)
            top = b[2] + u * (b[5] - b[2])
            tsv = (z - 36 - top) * tn[2]
        else:
            tn = (0.0, 0.0, 1.0)
            tsv = z - 36 - (b[5] if u >= 1.0 else b[2])
        if d in (0, 2):
            sides = ((b[1] - 16 - y, (0.0, -1.0, 0.0)), (y - (b[4] + 16), (0.0, 1.0, 0.0)))
        else:
            sides = ((b[0] - 16 - x, (-1.0, 0.0, 0.0)), (x - (b[3] + 16), (1.0, 0.0, 0.0)))
        back = ((x - (b[3] + 16), (1.0, 0.0, 0.0)), (y - (b[4] + 16), (0.0, 1.0, 0.0)),
                (b[0] - 16 - x, (-1.0, 0.0, 0.0)), (b[1] - 16 - y, (0.0, -1.0, 0.0)))[d]
        return ((b[2] - 36 - z, tsv, sides[0][0], sides[1][0], back[0]),
                ((0.0, 0.0, -1.0), tn, sides[0][1], sides[1][1], back[1]))

    def normal_at(self, x, y, z, want_up=False, move=None):
        if self._BG is not None:
            r = self._bsp_normal(x, y, z, want_up, move)
            if r is not None:
                return r
        if want_up:
            # standing on a slope: its slanted surface normal (steep ones are surfed, like in CS)
            for b, k, d in self._sp:
                if k == 1 and b[0] - 16 < x < b[3] + 16 and b[1] - 16 < y < b[4] + 16:
                    u = self._slope_u(b, d, x, y)
                    if 0.0 < u < 1.0:
                        top = b[2] + u * (b[5] - b[2])
                        if top - 3.0 <= z - 36 <= top + 6.0:
                            return self._slope_n(b, d)
            for b, k, d, c, s, cx, cy in self._rt:
                if k != 1:
                    continue
                dx, dy = x - cx, y - cy
                lx, ly = cx + dx * c + dy * s, cy - dx * s + dy * c
                if b[0] - 16 < lx < b[3] + 16 and b[1] - 16 < ly < b[4] + 16:
                    u = self._slope_u(b, d, lx, ly)
                    if 0.0 < u < 1.0:
                        top = b[2] + u * (b[5] - b[2])
                        if top - 3.0 <= z - 36 <= top + 6.0:
                            n = self._slope_n(b, d)
                            return (n[0] * c - n[1] * s, n[0] * s + n[1] * c, n[2])
            return (0.0, 0.0, 1.0)
        best, bd = None, 1e30
        F = self._FACES
        cand = []                                  # (signed distances, normals) per shape
        for b in self._bx:
            cand.append(((b[0] - 16 - x, x - (b[3] + 16), b[1] - 16 - y,
                          y - (b[4] + 16), b[2] - 36 - z, z - (b[5] + 36)), F))
        for b, k, d in self._sp:
            cand.append(self._cand1(b, k, d, x, y, z))
        for b, k, d, c, s, cx, cy in self._rt:
            dx, dy = x - cx, y - cy
            sv, ns = self._cand1(b, k, d, cx + dx * c + dy * s, cy - dx * s + dy * c, z)
            cand.append((sv, tuple((n[0] * c - n[1] * s, n[0] * s + n[1] * c, n[2]) for n in ns)))
        for sv, ns in cand:
            m = max(sv)
            for i in range(len(sv)):
                if sv[i] < m - .5:
                    continue  # not a face we are touching
                n = ns[i]
                if move is not None and n[0] * move[0] + n[1] * move[1] + n[2] * move[2] > -1e-4:
                    continue
                if abs(sv[i]) < bd:
                    bd = abs(sv[i])
                    best = n
        return best

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
        for b, (t, tt, tile, col, fo), _k, _d, _y in zip(self.B, self.meta, self.K, self.D, self.Y):
            if _y:
                _a = math.radians(_y)
                _c, _s = math.cos(_a), math.sin(_a)
                _cx, _cy = (b[0] + b[3]) * .5, (b[1] + b[4]) * .5
                R = lambda p, _c=_c, _s=_s, _cx=_cx, _cy=_cy: (_cx + (p[0] - _cx) * _c - (p[1] - _cy) * _s,
                                                              _cy + (p[0] - _cx) * _s + (p[1] - _cy) * _c, p[2])
                RV = lambda v, _c=_c, _s=_s: (v[0] * _c - v[1] * _s, v[0] * _s + v[1] * _c, v[2])
            else:
                R = RV = (lambda p: p)
            if _k:
                self._build_shape(b, _k, _d, t, tt, tile, col, fo, R, RV)
                continue
            for fi, f in enumerate(self._faces(b)):
                n = f[4]
                ti = tt if n[2] > .5 else t
                if fo[fi] >= 0:
                    ti = fo[fi]            # per-face texture from the map builder
                tid = self._tids[ti] if 0 <= ti < len(self._tids) else None
                gl.glBindTexture(gl.GL_TEXTURE_2D, tid or self._white)
                sh = self._shade(n)
                if tid:
                    gl.glColor3f(sh, sh, sh)
                else:
                    gl.glColor3f(col[0] / 255 * sh, col[1] / 255 * sh, col[2] / 255 * sh)
                gl.glBegin(gl.GL_QUADS)
                nr = RV(n)
                for pt in f[:4]:
                    pt = R(pt)
                    u, v = self._uv(pt, nr, tile)
                    gl.glTexCoord2f(u, v)
                    gl.glVertex3f(*pt)
                gl.glEnd()
        if self.DEC:                              # decals: only the picture face, pulled toward the viewer a bit
            gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
            gl.glPolygonOffset(-2.0, -2.0)
            # decals carry their own alpha (PNG see-through parts + the builder's Opacity slider):
            # blend it, and drop fully clear pixels so they never hide what is behind them
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            gl.glEnable(gl.GL_ALPHA_TEST)
            gl.glAlphaFunc(gl.GL_GREATER, 0.01)
            for b, fo, tile, col in self.DEC:
                for fi, f in enumerate(self._faces(b)):
                    if fo[fi] < 0:
                        continue
                    n = f[4]
                    tid = self._tids[fo[fi]] if 0 <= fo[fi] < len(self._tids) else None
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
            gl.glDisable(gl.GL_ALPHA_TEST)
            gl.glDisable(gl.GL_BLEND)
            gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)
        gl.glEndList()
        if self._bsp:
            self._bsp_build()

    def _draw_ladder(self, b, yaw, d):
        """Two rails and a rung every 12 units, lying on the wall side of the ladder zone."""
        x0, y0, z0, x1, y1, z1 = b
        cx, cy = (x0 + x1) * .5, (y0 + y1) * .5
        if d in (0, 2):
            W, T = y1 - y0, x1 - x0
        else:
            W, T = x1 - x0, y1 - y0
        off = min(3.0, T / 2)                     # how far the ladder sits in front of the wall
        half = max(2.0, W / 2 - min(5.0, W / 4))  # rail centre line, from the middle of the ladder

        def box(n0, n1, t0, t1, za, zb_):         # n = distance from the wall, t = across the ladder
            if d == 0:
                return [x0 + n0, cy + t0, za, x0 + n1, cy + t1, zb_]
            if d == 2:
                return [x1 - n1, cy + t0, za, x1 - n0, cy + t1, zb_]
            if d == 1:
                return [cx + t0, y0 + n0, za, cx + t1, y0 + n1, zb_]
            return [cx + t0, y1 - n1, za, cx + t1, y1 - n0, zb_]
        parts = []
        for sg in (-1, 1):
            parts.append((box(off - 1.2, off + 1.2, sg * half - 1.5, sg * half + 1.5, z0, z1), (0.62, 0.45, 0.28)))
        n = int(max(1, min(400, (z1 - z0) / 12.0)))
        for i in range(n):
            z = z0 + 6.0 + i * 12.0
            if z + 1.0 > z1:
                break
            parts.append((box(off - 1.0, off + 1.0, -half, half, z - 1.0, z + 1.0), (0.72, 0.72, 0.76)))
        if yaw:
            a = math.radians(yaw)
            c, s = math.cos(a), math.sin(a)
            R = lambda p: (cx + (p[0] - cx) * c - (p[1] - cy) * s, cy + (p[0] - cx) * s + (p[1] - cy) * c, p[2])
            RV = lambda v: (v[0] * c - v[1] * s, v[0] * s + v[1] * c, v[2])
        else:
            R = RV = (lambda p: p)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._white)
        gl.glBegin(gl.GL_QUADS)
        for bb, col in parts:
            for f in self._faces(bb):
                sh = self._shade(RV(f[4]))
                gl.glColor3f(col[0] * sh, col[1] * sh, col[2] * sh)
                for pt in f[:4]:
                    gl.glVertex3f(*R(pt))
        gl.glEnd()

    def _poly(self, ti, col, pts, sh, uv):
        tid = self._tids[ti] if 0 <= ti < len(self._tids) else None
        gl.glBindTexture(gl.GL_TEXTURE_2D, tid or self._white)
        if tid:
            gl.glColor3f(sh, sh, sh)
        else:
            gl.glColor3f(col[0] / 255 * sh, col[1] / 255 * sh, col[2] / 255 * sh)
        gl.glBegin(gl.GL_POLYGON)
        for pt, (u, v) in zip(pts, uv):
            gl.glTexCoord2f(u, v)
            gl.glVertex3f(*pt)
        gl.glEnd()

    def _build_shape(self, b, k, d, t, tt, tile, col, fo, R=None, RV=None):
        """Slope (wedge) or cylinder, drawn exactly like the Map Builder shows it."""
        x0, y0, z0, x1, y1, z1 = b
        R = R or (lambda p: p)
        RV = RV or (lambda v: v)
        top_t = fo[0] if fo[0] >= 0 else tt
        bot_t = fo[1] if fo[1] >= 0 else t
        side_t = next((v for v in fo[2:] if v >= 0), t)
        if k == 1:
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
                pts = [R(W(*q)) for q in loc]
                nr = RV(n)
                self._poly(ti, col, pts, self._shade(n), [self._uv(pt, nr, tile) for pt in pts])
        else:
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
            N = 64
            ring = [R((cx + rx * math.cos(2 * math.pi * i / N), cy + ry * math.sin(2 * math.pi * i / N), 0.0))[:2] for i in range(N)]
            run = 0.0
            for i in range(N):
                a, c = ring[i], ring[(i + 1) % N]
                seg = math.hypot(c[0] - a[0], c[1] - a[1])
                pts = [(a[0], a[1], z0), (c[0], c[1], z0), (c[0], c[1], z1), (a[0], a[1], z1)]
                uv = [(run / tile, -z0 / tile), ((run + seg) / tile, -z0 / tile),
                      ((run + seg) / tile, -z1 / tile), (run / tile, -z1 / tile)]
                run += seg
                self._poly(side_t, col, pts, 0.75 + 0.07 * math.cos(2 * math.pi * (i + .5) / N), uv)
            topp = [(px, py, z1) for px, py in ring]
            self._poly(top_t, col, topp, 1.0, [self._uv(pt, (0, 0, 1), tile) for pt in topp])
            botp = [(px, py, z0) for px, py in reversed(ring)]
            self._poly(bot_t, col, botp, 0.5, [self._uv(pt, (0, 0, 1), tile) for pt in botp])

    def draw(self):
        if self._list is None:
            self._build()
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glCallList(self._list)
        if self._bsp_list is not None:
            gl.glCallList(self._bsp_list)
        gl.glColor4f(1, 1, 1, 1)
