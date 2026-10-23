"""World collision and rendering: ArenaWorld and FadMapWorld."""
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

    def draw_sky(self, x, y, z):
        pass

    def in_px_zone(self, x, y, z):
        """Pixel surf zones (map builder) - none by default, so pixel surf only works on maps that have one."""
        return False

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
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glEndList()

    @staticmethod
    def _solid_box(x0, y0, z0, x1, y1, z1):
        gl.glBegin(gl.GL_QUADS)
        for f in (((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),
                  ((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0)),
                  ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),
                  ((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)),
                  ((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)),
                  ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))):
            for p in f:
                gl.glVertex3f(*p)
        gl.glEnd()

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
        if not self.B:
            raise ValueError("the map has no boxes")
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
        gl.glColor4f(1, 1, 1, 1)

