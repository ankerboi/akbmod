"""3D scene rendering and HUD."""
import math
import random
import re
import time

import numpy as np
from OpenGL import GL as gl
from OpenGL import GLU as glu

from .config import *
from .glutil import FX, tex2d
from .source.model import PLAYER_SCALE, HIT_HALF_W, HIT_HALF_H, HIT_HALF_H_LOW
from .gfx import team_tint, team_logo, draw_team_logo, c255
from .source.loaders import _arc

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


def held_points(w):
    """(right-hand point, left-hand point) on weapon w's model, in the model's own units (cached)."""
    vm = getattr(w, "wvm", None) if w else None
    if vm is None or vm.vtx is None:
        return None
    hp = getattr(vm, "_hp", None)
    if hp is None:
        v = vm.pos[0]
        lo, hi = v.min(axis=0), v.max(axis=0)
        ext = hi - lo
        name = str(w.n or "").lower()
        g, h = HELD_WPN_ANCHOR, HELD_WPN_FORE
        if w.melee:
            g = h = ((1.0 - HELD_KNIFE_ANCHOR[0],) + tuple(HELD_KNIFE_ANCHOR[1:])) if HELD_KNIFE_FLIP else HELD_KNIFE_ANCHOR
        else:
            for rx, gg, hh in HELD_ANCHORS:
                if re.search(rx, name):
                    g, h = gg, hh
                    break
        ofs = np.array(HELD_WPN_OFS, np.float64)
        hp = vm._hp = (lo + ext * np.array(g, np.float64) + ofs, lo + ext * np.array(h, np.float64) + ofs)
    return hp


def draw_held_weapon(w, grip):
    """Draw weapon w (weapon-only model) in a player's right hand. grip = (pos, fwd, left, up) in the player's own space."""
    vm = getattr(w, "wvm", None) if w else None
    if vm is None or vm.vtx is None:
        return
    G, H = held_points(w)
    an = tuple(float(x) for x in G)
    p, f, l, u, tl, two = grip
    if w.melee and HELD_KNIFE_FLIP:
        f, l = -f, -l          # knife turned around (yaw 180) so the blade points the other way
    if two and not w.melee:
        # turn the gun so its foregrip lands on the left hand while the grip stays in the right hand
        gv = np.asarray(H) - np.asarray(G)                              # grip -> foregrip, in the gun's own axes
        v_w = f * gv[0] + l * gv[1] + u * gv[2]                         # ... in the player's axes
        wv = np.asarray(tl) - np.asarray(p)                             # right hand -> left hand
        wv = wv - u * float(np.dot(wv, u)) * HELD_WPN_LEVEL             # keep the muzzle from tipping up
        nv, nw = float(np.linalg.norm(v_w)), float(np.linalg.norm(wv))
        if nv > 1e-3 and nw > 1e-3 and float(np.dot(v_w, wv)) / (nv * nw) > 0.7:
            R = _arc(v_w, wv)
            f, l, u = R @ f, R @ l, R @ u
    mat = np.array([f[0], f[1], f[2], 0, l[0], l[1], l[2], 0, u[0], u[1], u[2], 0, p[0], p[1], p[2], 1], np.float32)
    gl.glPushMatrix()
    gl.glMultMatrixf(mat)
    gl.glTranslatef(-an[0], -an[1], -an[2])
    flip = bool(w.vm_flip)
    if flip:
        gl.glScalef(1, -1, 1)
        gl.glFrontFace(gl.GL_CW)
    vm.draw_world()
    if flip:
        gl.glFrontFace(gl.GL_CCW)
    gl.glPopMatrix()


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
    ppx, ppy, ppz = ex, ey, P.pz + (P.z - P.pz) * al      # where your own body is drawn (third person)
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
    tp = bool(game.thirdperson) and not (P.zoom and P.w == 0)       # scoped AWP always stays first person
    if tp:
        if getattr(P, "_pm_team", None) != P.team:
            P._pm_team = P.team
            P._pm_tried = False
        if P.pm is None and not getattr(P, "_pm_tried", False):
            P._pm_tried = True
            game.build_entity_model(P, True)
        # pull the camera back along the view direction, stopping short of walls
        back = 0.0
        for dd in range(6, 121, 6):
            if game.world.solid(ex - fx * dd, ey - fy * dd, ez - fz * dd + 6):
                break
            back = float(dd)
        back = max(0.0, back - 8.0)
        ex, ey, ez = ex - fx * back, ey - fy * back, ez - fz * back + 6.0 * (back / 120.0)
    glu.gluLookAt(ex, ey, ez, ex + fx, ey + fy, ez + fz, 0, 0, 1)

    game.world.draw_sky(ex, ey, ez)
    FX.begin(0)
    game.world.draw()

    for e in game.ents:
        if e.isP and not tp:
            continue
        if not e.alive and not (e.pm and e.anim.get("ds", -1) >= 0):
            continue  # no death sequence to show: the body just vanishes
        bx, by, bz = (ppx, ppy, ppz) if e.isP else (e.x, e.y, e.z)     # your own body uses the smoothed position
        ddx, ddy = bx - ex, by - ey
        if ddx * ddx + ddy * ddy > 90000 and ddx * fx + ddy * fy < 0:
            continue  # behind the camera: don't draw
        if e.pm:
            gy = e.anim.get("gyaw")
            gl.glPushMatrix()
            gl.glTranslatef(bx, by, bz + e.pm_dz)
            gl.glScalef(PLAYER_SCALE, PLAYER_SCALE, PLAYER_SCALE)   # shrink around the feet
            gl.glRotatef(gy if gy is not None else math.degrees(e.yaw), 0, 0, 1)  # legs yaw; torso twists on top
            e.pm.draw()
            _gg = getattr(e.pm, "grip_g", None)
            if HELD_WPN and e.alive and _gg is not None and 0 <= e.w < len(CFG["weapons"]):
                draw_held_weapon(CFG["weapons"][e.w], _gg)
            gl.glPopMatrix()
        else:
            gl.glPushMatrix()
            gl.glTranslatef(bx, by, bz - 36)
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

    # sv_showhitboxes: wireframe player collision + bullet hitboxes
    if game.show_hitboxes:
        tex2d(False)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_DEPTH_TEST)
        for e in game.ents:
            if not e.alive and not (e.pm and e.anim.get("ds", -1) >= 0):
                continue
            if e.isP:
                continue  # own body is the camera; show others only
            sl = (getattr(e, "sl", 0) or 0) > 0 or (getattr(e, "cr", 0) or 0) > 0.5
            scale = PLAYER_SCALE
            # bullet hitbox (tight)
            hh = (HIT_HALF_H_LOW if sl else HIT_HALF_H) * scale
            hw = HIT_HALF_W * scale
            # collision hull (slightly wider — what bodies block with)
            chw = hw * 1.15
            cx, cy, cz = e.x, e.y, e.z - (16 if sl else 0)
            # match the raycast vertical placement used in fire()
            cz_adj = cz - ((21 if sl else 35) - hh)
            def _wire_box(x, y, z, hx, hy, hz, rgba):
                gl.glColor4f(*rgba)
                x0, x1 = x - hx, x + hx
                y0, y1 = y - hy, y + hy
                z0, z1 = z - hz, z + hz
                edges = (
                    (x0, y0, z0), (x1, y0, z0), (x1, y0, z0), (x1, y1, z0),
                    (x1, y1, z0), (x0, y1, z0), (x0, y1, z0), (x0, y0, z0),
                    (x0, y0, z1), (x1, y0, z1), (x1, y0, z1), (x1, y1, z1),
                    (x1, y1, z1), (x0, y1, z1), (x0, y1, z1), (x0, y0, z1),
                    (x0, y0, z0), (x0, y0, z1), (x1, y0, z0), (x1, y0, z1),
                    (x1, y1, z0), (x1, y1, z1), (x0, y1, z0), (x0, y1, z1),
                )
                gl.glBegin(gl.GL_LINES)
                for v in edges:
                    gl.glVertex3f(*v)
                gl.glEnd()
            # collision hull in cyan
            _wire_box(e.x, e.y, e.z, chw, chw, hh, (0.2, 0.9, 1.0, 0.85))
            # bullet hitboxes: body in yellow, head in red (exactly what fire() tests)
            _bb, _hb = game.target_shape(e)
            _wire_box(*_bb, (1.0, 0.9, 0.2, 0.9))
            _wire_box(*_hb, (1.0, 0.15, 0.15, 0.95))
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glColor4f(1, 1, 1, 1)
        tex2d(True)

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
    hide = (P.zoom and P.w == 0) or tp          # no first-person gun in third person
    w = CFG["weapons"][P.w]
    gl.glPushMatrix()
    if P.slk > 0.001:
        gl.glTranslatef(0.4 * P.slk, 0.8 * P.slk, -1.2 * P.slk)    # gun drops and shifts while sliding
        gl.glRotatef(-9 * P.slk, 1, 0, 0)
        gl.glRotatef(-5 * P.slk, 0, 1, 0)
    # run bob (subtle) + mouse-look lag (main feel)
    _vb = max(0.0, min(2.0, float(getattr(game, "viewbob", 1.0))))
    _lag_y = getattr(game, "vm_lag_y", 0.0)   # +right when looking left
    _lag_p = getattr(game, "vm_lag_p", 0.0)   # +up when looking down
    gl.glTranslatef(
        0,
        math.sin(P.ph) * VM_BOB_SIDE * P.bob * 0.35 * _vb - _lag_y,
        -(math.sin(P.ph) ** 2) * VM_BOB_UP * P.bob * 0.35 * _vb
        + P.air * .4 - P.land * VM_LAND - _lag_p,
    )
    # slight rotate so the gun feels heavier when turning
    if abs(_lag_y) > 0.01 or abs(_lag_p) > 0.01:
        gl.glRotatef(-_lag_y * 0.9, 0, 0, 1)   # yaw lag
        gl.glRotatef(_lag_p * 1.1, 0, 1, 0)    # pitch lag
    if P.alive and P.shoveT > 0 and not game.shove_seq:
        # L4D2 style shove: pull back, thrust forward and sweep across, then recover
        sf, sl, su, syaw, spit, sroll = shove_pose(1.0 - P.shoveT / max(1e-3, P.shoveD))
        if game.hand_swap:
            sl, syaw, sroll = -sl, -syaw, -sroll
        gl.glTranslatef(sf, sl, su)
        gl.glRotatef(syaw * SHOVE_ROT, 0, 0, 1)
        gl.glRotatef(-spit * SHOVE_ROT, 0, 1, 0)
        gl.glRotatef(sroll * SHOVE_ROT, 1, 0, 0)
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


XP_KILL, XP_KNIFE = 100, 150
XP_YELLOW = (255, 214, 0)


def xp_info(xp):
    """(rank, xp into this rank, xp needed for the next rank). Rank 1 needs 600 XP, each next one 100 more."""
    lvl, need = 1, 600
    while xp >= need:
        xp -= need
        lvl += 1
        need += 100
    return lvl, xp, need


HM_TIME = 0.28   # how long the hit marker shows (seconds)


def draw_hitmarker(cx, cy, t, kill):
    """Call of Duty style hit marker: four short diagonal ticks around the crosshair.
    White on a hit, red on a kill. Pops out a little, then settles and fades."""
    k = 1.0 + 0.7 * (t / HM_TIME)
    a = min(1.0, t / 0.12)
    d1, d2 = 5.0 * k, 12.0 * k
    col = (1.0, 0.18, 0.18) if kill else (1.0, 1.0, 1.0)
    gl.glDisable(gl.GL_TEXTURE_2D)
    for width, c, al in ((5.0, (0.0, 0.0, 0.0), a * 0.55), (2.5, col, a)):
        gl.glLineWidth(width)
        gl.glColor4f(c[0], c[1], c[2], al)
        gl.glBegin(gl.GL_LINES)
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            gl.glVertex2f(cx + sx * d1, cy + sy * d1)
            gl.glVertex2f(cx + sx * d2, cy + sy * d2)
        gl.glEnd()
    gl.glLineWidth(1.0)


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
    """Hold Tab: big team logos + names on each side, the score in the middle, player tables under them."""
    colw = min(470, (W - 120) / 2)
    gap = 120
    rh = 28
    rows = max(1, max(sum(1 for e in game.ents if e.team == 0), sum(1 for e in game.ents if e.team == 1)))
    head_h, th_h = 104, 26
    total_w = colw * 2 + gap
    total_h = head_h + th_h + rows * rh + 24
    x0 = W / 2 - total_w / 2
    top = max(20, H / 2 - total_h / 2)
    gfx.rect(x0 - 16, top - 12, total_w + 32, total_h + 24, (14 / 255, 14 / 255, 14 / 255, .93))
    LOGO = 84
    sc = game.score
    accent = ((210, 210, 210), (210, 210, 210))
    # header: logo + name on the outer edges, score in the middle
    for team in (0, 1):
        x = x0 + team * (colw + gap)
        col = accent[team]
        if team == 0:
            lg = draw_team_logo(x, top + 4, LOGO, 0)
            if not lg:   # logo only; the name is just a fallback when no logo could be drawn
                gfx.text(TEAM_NAMES[0], x + 6, top + 30, 34, (255, 255, 255), bold=True)
        else:
            lg = draw_team_logo(x + colw - LOGO, top + 4, LOGO, 1)
            if not lg:
                gfx.text(TEAM_NAMES[1], x + colw - 6, top + 30, 34, (255, 255, 255), bold=True, anchor="r")
        gfx.rect(x, top + head_h - 4, colw, 3, (col[0] / 255, col[1] / 255, col[2] / 255, 1))
    mid = W / 2
    gfx.text(str(sc[0]), mid - 22, top + 26, 44, (255, 255, 255), bold=True, anchor="r", mono=True)
    gfx.text(str(sc[1]), mid + 22, top + 26, 44, (255, 255, 255), bold=True, anchor="l", mono=True)
    gfx.rect(mid - 1, top + 22, 2, 50, (1, 1, 1, .25))
    # tables
    for team in (0, 1):
        x = x0 + team * (colw + gap)
        y = top + head_h + 4
        gfx.rect(x, y, colw, th_h, (30 / 255, 30 / 255, 30 / 255, 1))
        gfx.text("PLAYER", x + 12, y + 6, 11, (154, 154, 154), bold=True)
        gfx.text("RANK", x + colw - 130, y + 6, 11, (154, 154, 154), bold=True, anchor="r")
        gfx.text("KILLS", x + colw - 70, y + 6, 11, (154, 154, 154), bold=True, anchor="r")
        gfx.text("DEATHS", x + colw - 10, y + 6, 11, (154, 154, 154), bold=True, anchor="r")
        y += th_h
        members = sorted((e for e in game.ents if e.team == team),
                         key=lambda e: (-e.kills, e.deaths, e.name))
        for i, e in enumerate(members):
            if e.isP:
                gfx.rect(x, y, colw, rh, (48 / 255, 48 / 255, 48 / 255, 1))
                gfx.rect(x, y, 3, rh, (157 / 255, 211 / 255, 103 / 255, 1))
            else:
                v = 24 if i % 2 else 19
                gfx.rect(x, y, colw, rh, (v / 255, v / 255, v / 255, 1))
            c = (255, 193, 7) if e.isP else ((255, 255, 255) if e.alive else (130, 130, 130))
            nx = x + 12
            _m = getattr(game, "menu", None)
            if e.isP and _m is not None and getattr(_m, "pfp_tex", None):
                gfx.rect(x + 11, y + 3, 22, 22, (157 / 255, 211 / 255, 103 / 255, 1))
                gl.glEnable(gl.GL_TEXTURE_2D)
                gl.glBindTexture(gl.GL_TEXTURE_2D, _m.pfp_tex)
                gl.glColor4f(1, 1, 1, 1)
                gl.glBegin(gl.GL_QUADS)
                gl.glTexCoord2f(0, 0); gl.glVertex2f(x + 13, y + 5)
                gl.glTexCoord2f(1, 0); gl.glVertex2f(x + 31, y + 5)
                gl.glTexCoord2f(1, 1); gl.glVertex2f(x + 31, y + 23)
                gl.glTexCoord2f(0, 1); gl.glVertex2f(x + 13, y + 23)
                gl.glEnd()
                nx = x + 40
            gfx.text(e.name or "UNKNOWN", nx, y + 6, 14, c, bold=bool(e.isP), mono=True)
            if e.isP:
                gfx.text(str(xp_info(game.xp)[0]), x + colw - 130, y + 6, 14, (255, 214, 0), bold=True, anchor="r", mono=True)
            gfx.text(str(e.kills), x + colw - 70, y + 6, 14, c, anchor="r", mono=True)
            gfx.text(str(e.deaths), x + colw - 10, y + 6, 14, c, anchor="r", mono=True)
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
    if P.alive and game.hm > 0:
        draw_hitmarker(W / 2, H / 2, game.hm, game.hm_kill)

    # XP popups (yellow, rise and fade next to the crosshair)
    if P.alive:
        for i, pp_ in enumerate(reversed(game.xp_pops)):
            a_ = min(1.0, pp_["t"] / 0.5)
            yy_ = H / 2 + 34 + i * 24 - (1.6 - pp_["t"]) * 10
            gfx.text(pp_["txt"], W / 2 + 28, yy_, 20, XP_YELLOW, alpha=a_, bold=True, mono=True)
            gfx.text(pp_["sub"], W / 2 + 28 + gfx.text_w(pp_["txt"], 20, True, True) + 8, yy_ + 6, 12,
                     (255, 240, 160), alpha=a_, bold=True)
    lv_ = xp_info(game.xp)[0]
    if game.rank_flash > 0:
        gfx.text("RANK UP!", W / 2, H * 0.22, 40, XP_YELLOW, alpha=min(1.0, game.rank_flash), bold=True, anchor="c")
        gfx.text("RANK %d" % lv_, W / 2, H * 0.22 + 46, 22, (255, 255, 255), alpha=min(1.0, game.rank_flash), bold=True, anchor="c")

    # (top score bar removed; the win message below still shows when the match ends)
    sc = game.score
    if game.over:
        gfx.text(TEAM_NAMES[0 if sc[0] > sc[1] else 1] + " wins!", W / 2, 62, 20, (255, 255, 255), bold=True, anchor="c")

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
    hud_txt = w.n if w.melee else f"{P.ammo[P.w]} / {w.mag}"
    hud_w = gfx.text_w(hud_txt, 22, True, True)
    gfx.rect(W - 22 - hud_w - 12, H - 50 - 6, hud_w + 24, 22 + 12, (22 / 255, 22 / 255, 22 / 255, .94))  # same dark gray as HP background
    gfx.text(hud_txt, W - 22, H - 50, 22, (255, 255, 255), bold=True, anchor="r", mono=True)
    # reload progress: thin white line right under the dark panel (fills left to right, replaces the "(reloading)" text)
    if P.reloadT > 0 and not w.melee:
        game._rl_hud_max = max(getattr(game, "_rl_hud_max", 0.0), P.reloadT)
        prog = 1.0 - P.reloadT / max(game._rl_hud_max, 1e-3)
        panel_w = hud_w + 24
        gfx.rect(W - 22 - hud_w - 12, H - 50 - 6 + 22 + 12, panel_w * max(0.0, min(1.0, prog)), 3, (1, 1, 1, 1))
    else:
        game._rl_hud_max = 0.0

    # chat (bottom-left): jump stats show up here and fade out after a few seconds
    cy_ = H - 104
    for ln in reversed(game.chat):
        al_ = max(0.0, min(1.0, ln["t"] / 1.5))
        tg_ = "[%s] " % ln["tag"]
        tw_ = gfx.text_w(tg_, 10, True) + gfx.text_w(ln["text"], 10)
        gfx.rect(10, cy_ - 2, tw_ + 12, 16, (22 / 255, 22 / 255, 22 / 255, .8 * al_))
        gfx.text(tg_, 16, cy_, 10, (255, 193, 7), alpha=al_, bold=True)
        gfx.text(ln["text"], 16 + gfx.text_w(tg_, 10, True), cy_, 10, (255, 255, 255), alpha=al_)
        cy_ -= 19

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
    if game.show_speed and P.alive and not game.over:
        sp_ = math.hypot(P.vx, P.vy)                     # horizontal speed, units / second
        gfx.text(f"{int(round(sp_))}", W / 2, H - 66, 26, (255, 255, 255), bold=True, anchor="c", mono=True)

    # CS:GO-style developer console
    if game.con_open:
        ch = min(int(H * 0.42), 320)
        gfx.rect(0, 0, W, ch, (12 / 255, 14 / 255, 18 / 255, 0.92))
        gfx.rect(0, ch - 1, W, 1, (60 / 255, 70 / 255, 85 / 255, 1))
        # log lines (bottom of panel = input area)
        line_h = 16
        max_lines = max(1, (ch - 36) // line_h)
        log = game.con_log
        start = max(0, len(log) - max_lines - game.con_scroll)
        end = max(0, len(log) - game.con_scroll)
        visible = log[start:end]
        y0 = ch - 36 - line_h * len(visible)
        for i, ln in enumerate(visible):
            gfx.text(ln["t"][:120], 10, y0 + i * line_h, 13, ln.get("c", (200, 200, 200)), mono=True)
        # input row
        gfx.rect(0, ch - 28, W, 28, (8 / 255, 10 / 255, 14 / 255, 0.98))
        caret = "|" if int(time.time() * 2) % 2 == 0 else " "
        gfx.text("] " + game.con_text + caret, 10, ch - 22, 14, (255, 255, 255), mono=True)
        gfx.text("~ close  |  help  |  arrows history", W - 12, ch - 20, 11, (120, 130, 140), anchor="r", mono=True)

    gl.glColor4f(1, 1, 1, 1)
    gl.glEnable(gl.GL_TEXTURE_2D)

