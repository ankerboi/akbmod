"""2D graphics helpers, team logos, RenderTarget, Gfx text/draw API."""
import math
import time
from pathlib import Path

import numpy as np
import pygame
from OpenGL import GL as gl

from .config import *
from .glutil import make_texture

def team_tint(col):
    """Solid band behind a team label: black team = yellow band (black text), yellow team = black band (yellow text)."""
    return (1.0, 214 / 255, 0.0, 1.0) if col == TEAM_CT else (0.0, 0.0, 0.0, 1.0)


TEAM_NAMES = ("BUM", "FAD")     # team 0 (black) = BUM, team 1 (yellow) = FAD
_TEAM_LOGO = {}


def team_logo(team):
    """(texture id, w, h) of the faction logo, or None. Looks for bum.png / fad.png in the mod/image folder."""
    team = int(team) & 1
    if team in _TEAM_LOGO:
        return _TEAM_LOGO[team]
    res = None
    nm = TEAM_NAMES[team].lower()
    try:
        for d in mod_folders("image"):
            for f in sorted(d.iterdir()):
                if (res is None and f.is_file() and f.stem.lower() == nm
                        and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                    surf = pygame.image.load(str(f)).convert_alpha()
                    w, h = surf.get_size()
                    arr = np.frombuffer(pygame.image.tostring(surf, "RGBA", False), np.uint8).reshape(h, w, 4)
                    res = (make_texture(arr, w, h), w, h)
    except Exception as ex:
        print("team logo failed:", nm, ex)
    _TEAM_LOGO[team] = res
    return res


_MOD_IMG = {}


def mod_image(name):
    """(texture id, w, h) of an image named <name>.png/jpg/... in the mod/image folder, or None."""
    name = name.lower()
    if name in _MOD_IMG:
        return _MOD_IMG[name]
    res = None
    try:
        for d in mod_folders("image"):
            for f in sorted(d.iterdir()):
                if (res is None and f.is_file() and f.stem.lower() == name
                        and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                    surf = pygame.image.load(str(f)).convert_alpha()
                    w, h = surf.get_size()
                    arr = np.frombuffer(pygame.image.tostring(surf, "RGBA", False), np.uint8).reshape(h, w, 4)
                    res = (make_texture(arr, w, h), w, h)
    except Exception as ex:
        print("mod image failed:", name, ex)
    _MOD_IMG[name] = res
    return res


def draw_mod_image(name, x, y, size):
    """Draw a mod/image picture fitted in a size x size box. Returns True if drawn."""
    lg = mod_image(name)
    if not lg:
        return False
    tid, w, h = lg
    k = size / float(max(w, h))
    dw, dh = w * k, h * k
    x0, y0 = x + (size - dw) / 2, y + (size - dh) / 2
    gl.glEnable(gl.GL_TEXTURE_2D)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
    gl.glColor4f(1, 1, 1, 1)
    gl.glBegin(gl.GL_QUADS)
    gl.glTexCoord2f(0, 0); gl.glVertex2f(x0, y0)
    gl.glTexCoord2f(1, 0); gl.glVertex2f(x0 + dw, y0)
    gl.glTexCoord2f(1, 1); gl.glVertex2f(x0 + dw, y0 + dh)
    gl.glTexCoord2f(0, 1); gl.glVertex2f(x0, y0 + dh)
    gl.glEnd()
    return True


def draw_team_logo(x, y, size, team):
    """Draw the faction logo fitted in a size x size box (layout units). Returns True if drawn."""
    lg = team_logo(team)
    if not lg:
        return False
    tid, w, h = lg
    k = size / float(max(w, h))
    dw, dh = w * k, h * k
    x0, y0 = x + (size - dw) / 2, y + (size - dh) / 2
    gl.glEnable(gl.GL_TEXTURE_2D)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
    gl.glColor4f(1, 1, 1, 1)
    gl.glBegin(gl.GL_QUADS)
    gl.glTexCoord2f(0, 0); gl.glVertex2f(x0, y0)
    gl.glTexCoord2f(1, 0); gl.glVertex2f(x0 + dw, y0)
    gl.glTexCoord2f(1, 1); gl.glVertex2f(x0 + dw, y0 + dh)
    gl.glTexCoord2f(0, 1); gl.glVertex2f(x0, y0 + dh)
    gl.glEnd()
    return True


RENDER_W, RENDER_H = 1280, 720    # the game always renders at this resolution, then is scaled to the window


class RenderTarget:
    """Fixed-size off-screen buffer (optional MSAA). The whole game draws into it at RENDER_W x RENDER_H and
    present() scales it into the window, keeping 16:9 (black bars if the window has another shape)."""

    def __init__(self, w, h, samples=0):
        self.w, self.h = w, h
        self.ok = False
        self.view = (0, 0, w, h)      # where the image sits in the window (pixels, top-left origin)
        self.msaa = False
        try:
            # Color = texture (what present() draws). Depth attached so 3D works without MSAA.
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
            self.depth_rb = gl.glGenRenderbuffers(1)
            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, self.depth_rb)
            gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH24_STENCIL8, w, h)
            gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                         gl.GL_RENDERBUFFER, self.depth_rb)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError("resolve FBO incomplete")
            self.fbo = self.res_fbo
            self.ok = True
            # Optional MSAA path (disabled by default: samples=0)
            if samples > 0:
                fbo = gl.glGenFramebuffers(1)
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
                rbs = gl.glGenRenderbuffers(2)
                for rb, fmt, att in ((rbs[0], gl.GL_RGBA8, gl.GL_COLOR_ATTACHMENT0),
                                     (rbs[1], gl.GL_DEPTH24_STENCIL8, gl.GL_DEPTH_STENCIL_ATTACHMENT)):
                    gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, rb)
                    gl.glRenderbufferStorageMultisample(gl.GL_RENDERBUFFER, samples, fmt, w, h)
                    gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, att, gl.GL_RENDERBUFFER, rb)
                if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE:
                    self.fbo, self.msaa = fbo, True
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
        vw, vh = ww, wh                  # stretched to fill the whole window (no black bars)
        vx, vy = 0, 0
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


# Vertical nudge for the UI font so caps sit where the menu layout expects them.
# DINOT-Medium is taller than Stratum; a smaller shift keeps baseline alignment.
STRATUM_DY = 0.12
STRATUM_DY_MONO = 0.04


_UI_FONT = None


def _ui_font_path():
    """Find the UI font: prefer assets/font/DINOT-Medium.ttf, then stratum.ttf, then any .ttf/.otf in assets/font/."""
    global _UI_FONT
    if _UI_FONT is None:
        assets = Path(__file__).parent / "assets"
        fdir = assets / "font"
        for name in ("DINOT-Medium.ttf", "dinot-medium.ttf", "DINOT.ttf", "stratum.ttf"):
            cand = fdir / name
            if cand.is_file():
                _UI_FONT = cand
                break
        if _UI_FONT is None:
            found = sorted(p for p in fdir.glob("*") if p.suffix.lower() in (".ttf", ".otf")) if fdir.is_dir() else []
            _UI_FONT = found[0] if found else assets / "stratum.ttf"
    return _UI_FONT


class Gfx:
    def __init__(self):
        self.fonts = {}
        self.cache = {}
        self.ox = self.oy = 0.0   # screen offset of the current 2D layout (pixels)
        self.sc = 1.0             # screen pixels per layout unit (menu is laid out in 1000x600 units)

    def set_xf(self, ox=0.0, oy=0.0, sc=1.0):
        self.ox, self.oy, self.sc = ox, oy, sc

    def font(self, size, bold, mono=False, face=None):
        face = face or "stratum"       # "stratum" key = custom UI font (DINOT-Medium); face="sys" = system font
        k = (size, bold, mono, face)
        if k not in self.fonts and face == "stratum":
            try:      # custom UI font from assets/font/ (DINOT-Medium preferred)
                self.fonts[k] = pygame.font.Font(str(_ui_font_path()), size)
            except Exception:
                face = "sys"
        if k not in self.fonts:
            fam = ("consolas,lucidaconsole,couriernew,dejavusansmono,liberationmono,monospace" if mono
                   else "segoeui,arial,helvetica,dejavusans,freesans")
            self.fonts[k] = pygame.font.SysFont(fam, size, bold=bold)
        return self.fonts[k]

    def _px(self, size):
        return max(6, int(round(size * self.sc)))

    def _tex(self, s, size, color, bold, mono=False, face=None):
        px = self._px(size)
        k = (s, px, color, bold, mono, face)
        c = self.cache.get(k)
        if c:
            return c
        if len(self.cache) > 400:
            for tid, _, _ in self.cache.values():
                gl.glDeleteTextures([tid])
            self.cache.clear()
        fnt = self.font(px, bold, mono, face)
        if (face or "stratum") == "stratum" and s and any(m is None for m in fnt.metrics(s)):
            fnt = self.font(px, bold, mono, "sys")      # a glyph Stratum lacks (accents, symbols): system font for this string
        surf = fnt.render(s if s else " ", True, color)
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

    def text_w(self, s, size, bold=False, mono=False, face=None):
        return self.font(self._px(size), bold, mono, face).size(s)[0] / self.sc

    def text_h(self, size, bold=False, mono=False, face=None):
        return self.font(self._px(size), bold, mono, face).size("Ag")[1] / self.sc

    def text(self, s, x, y, size=14, color=(255, 255, 255), alpha=1.0, bold=False, anchor="l", mono=False, face=None, fix=True):
        tid, pw, ph = self._tex(s, size, tuple(color), bold, mono, face)
        if fix and (face or "stratum") == "stratum":
            y += size * (STRATUM_DY_MONO if mono else STRATUM_DY)   # lower Stratum to where the old font's glyphs sat
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

    def stext(self, s, x, y, size, color, alpha=1.0, anchor="l", shadow=False, outline=False, face="stratum"):
        """Source-style text: optional 1px black drop shadow or outline behind it."""
        if shadow:
            self.text(s, x + 1, y + 1, size, (0, 0, 0), alpha=alpha, anchor=anchor, face=face, fix=False)
        if outline:
            for ox_, oy_ in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
                self.text(s, x + ox_, y + oy_, size, (0, 0, 0), alpha=alpha, anchor=anchor, face=face, fix=False)
        self.text(s, x, y, size, color, alpha=alpha, anchor=anchor, face=face, fix=False)

    def rrect(self, x, y, w, h, r, rgba):
        """Filled rounded rectangle (layout units)."""
        r = max(0.0, min(r, w / 2, h / 2))
        pts = []
        for cx_, cy_, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
            for i in range(5):
                a = math.radians(a0 + i * 22.5)
                pts.append((cx_ + math.cos(a) * r, cy_ + math.sin(a) * r))
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor4f(*rgba)
        gl.glBegin(gl.GL_POLYGON)
        for px_, py_ in pts:
            gl.glVertex2f(px_, py_)
        gl.glEnd()

    def wordbox(self, x, y, s, size, boxcol, color, bs=5, shadow=False, outline=False, alpha=1.0):
        """Garry's Mod draw.WordBox, centred on x: box = text + bs padding, rounded by bs, top at y."""
        tw, th = self.text_w(s, size, face="stratum"), self.text_h(size, face="stratum")
        if boxcol[3] > 0:
            self.rrect(x - tw / 2 - bs, y, tw + bs * 2, th + bs * 2, bs, (boxcol[0], boxcol[1], boxcol[2], boxcol[3] * alpha))
        self.stext(s, x, y + bs, size, color, alpha=alpha, anchor="c", shadow=shadow, outline=outline)

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

    def rr(self, x, y, w, h, rgba, r=6):
        """Rounded rect snapped to whole screen pixels (radius is clamped to half the shorter side)."""
        sc, ox, oy = self.sc, self.ox, self.oy
        x0, y0 = round(ox + x * sc), round(oy + y * sc)
        x1, y1 = max(x0 + 1, round(ox + (x + w) * sc)), max(y0 + 1, round(oy + (y + h) * sc))
        self.rrect((x0 - ox) / sc, (y0 - oy) / sc, (x1 - x0) / sc, (y1 - y0) / sc, r, rgba)

    def box(self, x, y, w, h, fill, border=None, r=4):
        if border:
            self.rr(x, y, w, h, border, r)
            self.rr(x + 1, y + 1, w - 2, h - 2, fill, max(0, r - 1))
        else:
            self.rr(x, y, w, h, fill, r)


def c255(r, g, b, a=1.0):
    return (r / 255, g / 255, b / 255, a)

