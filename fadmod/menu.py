"""Main menu, loadout, skins, settings UI."""
import math
import re
import time
from pathlib import Path

import numpy as np
import pygame
from OpenGL import GL as gl
from OpenGL import GLU as glu

from .config import *
from .glutil import make_texture, FX
from .net import NET_MAX_PLAYERS, BROKER_ROOT
from .gfx import Gfx, c255, team_tint, team_logo, draw_team_logo, draw_mod_image, mod_image
from .source.loaders import make_vm, SrcInst, SrcPlayerInst, SrcSpec
from .settings import save_settings, set_fullscreen
from .render import draw_crosshair, draw_box

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
        self.quit_req = False
        self.resw = str(RENDER_W)   # video: typed resolution
        self.resh = str(RENDER_H)
        self.pfp_path = ""    # profile picture file
        self.pfp_tex = None
        self.pfp_status = ""
        self.status = "Built-in arena loaded."
        self.vm_prev = 0      # weapon shown in the viewmodel preview
        self.vm_dd = False    # preview dropdown open?
        self.cm_open = False      # Play tab: match settings panel shown?
        self.dd = None            # Play tab: which dropdown is open (mode / time)
        self._dd_was = None       # dropdown that was open when the mouse went down
        self.play_online = False  # Play tab: create match as Online (host server) instead of Offline
        self.sb_tab = "all"       # server browser tab: all | net | lan
        self.sb_sort = "n"        # server browser sort column
        self.sb_rev = False
        self.sb_sel = None        # selected server id
        self.sb_click = (None, 0.0)
        self.sb_scroll = 0
        self.lo_tab = "weapons"   # Skins tab: "weapons" | "players"
        self.lo_slot = 0          # weapon slot being edited
        self.lo_team = "CT"       # team being edited
        self.lo_page = 0
        self.set_sub = "controls"  # Settings sub-tab: controls | viewmodel | crosshair | video
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
        self._dd_was = self.dd      # a click anywhere closes an open dropdown
        self.dd = None
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
        if getattr(self, "team_drag", False):
            self.team_drag = False
            if self.g.net.active:
                self.g.net_set_team(1 if self.mp[0] > getattr(self, "_team_mid", 0) else 0)

    def bind_press(self, code):
        """Assign `code` to the waiting action; returns True if consumed."""
        if not self.waiting:
            return False
        action = self.waiting
        binds, binds2 = self.g.binds, self.g.binds2
        self.waiting = None
        if action.endswith("#2"):                       # second key for the action
            aid = action[:-2]
            if code != binds.get(aid):
                binds2[aid] = code                      # the same key may serve several actions
        else:
            binds[action] = code
            if binds2.get(action) == code:
                del binds2[action]
        return True

    def key_down(self, event):
        if self.waiting:
            if event.key == pygame.K_ESCAPE:
                self.waiting = None
            elif event.key in (pygame.K_BACKSPACE, pygame.K_DELETE) and self.waiting.endswith("#2"):
                self.g.binds2.pop(self.waiting[:-2], None)      # clear the second key
                self.waiting = None
            else:
                self.bind_press(pygame.key.name(event.key))
            return True
        if self.edit:
            lim = {"room": 28, "name": 16, "resw": 5, "resh": 5}.get(self.edit, 40)
            cur = getattr(self, self.edit)
            if event.key in (pygame.K_RETURN, pygame.K_ESCAPE):
                if self.edit in ("resw", "resh") and event.key == pygame.K_RETURN:
                    self.apply_res()
                self.edit = None
                save_settings(self.g, self)
            elif event.key == pygame.K_BACKSPACE:
                setattr(self, self.edit, cur[:-1])
            elif event.unicode and event.unicode.isprintable() and len(cur) < lim and (
                    event.unicode.isdigit() or self.edit not in ("resw", "resh")):
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
        s_side = max(.4, min(W / 1000, H / 600))     # side menu scale (unchanged)
        KC = max(.4, min(1.0, MENU_CONTENT_SCALE))
        m = 1.0 / KC                                  # side-menu units -> content units
        s = s_side * KC                               # content scale: pages are drawn smaller than the side menu
        LW, LH = W / s, H / s
        ox, oy = 0, 0
        self.xf = (ox, oy, s)
        gfx.set_xf(ox, oy, s)
        self.hot = []
        mx, my = self.mp

        gl.glDisable(gl.GL_DEPTH_TEST)
        gfx.rect(0, 0, W, H, c255(10, 10, 10))
        gl.glPushMatrix()
        gl.glTranslatef(ox, oy, 0)
        gl.glScalef(s, s, 1)

        gfx.rect(0, 0, LW, LH, c255(10, 10, 10))          # content: #0a0a0a
        # ---- side menu: drawn at its own (original) size, hit areas converted to content units ----
        LHs = LH / m                                   # window height in side-menu units
        smx, smy = mx / m, my / m                      # mouse in side-menu units
        gl.glPushMatrix()
        gl.glScalef(m, m, 1)
        gfx.set_xf(ox, oy, s * m)
        gfx.rect(0, 0, 240, LHs, c255(22, 22, 22))          # sidebar: #161616
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

        def shover(x, y, w, h):
            return x <= smx <= x + w and y <= smy <= y + h

        def nav(label, page, y):
            act = self.page == page
            h = shover(0, y, 240, 44)
            gfx.rect(0, y, 240, 44, c255(42, 42, 42) if act else (c255(30, 30, 30) if h else (0, 0, 0, 0)))
            gfx.text(label, 20, y + 12, 14, (255, 255, 255) if act else (154, 154, 154), bold=True)
            self.hot.append((0, y * m, 240 * m, 44 * m, lambda p=page: self.set_page(p), None))

        nav("Play", "play", 80)
        nav("Profile", "profile", 124)
        nav("Loadout", "loadout", 168)
        nav("Skins", "skins", 212)
        nav("Settings", "settings", 256)
        if g.net.active:
            gfx.text("ONLINE: " + g.net.room[:18], 20, LHs - 48, 11, (104, 104, 104), mono=True)
        gfx.text("FAD-MOD", 20, LHs - 26, 12, (154, 154, 154), bold=True)

        qy = LHs - 100
        qh = shover(20, qy, 200, 38)
        gfx.rect(20, qy, 200, 38, c255(42, 42, 42) if qh else c255(24, 24, 24))
        gfx.text("Quit", 120, qy + 19 - 9, 13, (229, 57, 53), bold=True, anchor="c")
        self.hot.append((20 * m, qy * m, 200 * m, 38 * m, lambda: setattr(self, "quit_req", True), None))
        gl.glPopMatrix()
        gfx.set_xf(ox, oy, s)                           # back to the (smaller) content scale

        def hover(x, y, w, h):
            return x <= mx <= x + w and y <= my <= y + h

        def button(x, y, w, h, label, fn, primary=False):
            hv = hover(x, y, w, h)
            if primary:
                fill = c255(58, 58, 58) if hv else c255(42, 42, 42)
            else:
                fill = c255(42, 42, 42) if hv else c255(24, 24, 24)
            gfx.rect(x, y, w, h, fill)
            col = (229, 57, 53) if label in ("Close server", "Leave server", "Cancel", "Remove", "Quit") else (255, 255, 255)
            gfx.text(label, x + w / 2, y + h / 2 - 9, 13, col, bold=True, anchor="c")
            self.hot.append((x, y, w, h, fn, None))


        def panel(x, y, w, h, title, sub=None):
            gfx.rect(x, y, w, h, c255(17, 17, 17))
            if title:
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

        cx = int(240 * m) + 40                      # sidebar width (content units) + gap
        cw = max(680, int(LW - cx - 40))
        if self.page == "settings":
            for i, (sub, lab) in enumerate((("controls", "Controls"),
                                            ("viewmodel", "Viewmodel"), ("crosshair", "Crosshair"),
                                            ("video", "Video"))):
                button(cx + cw - 4 * 98 + 6 + i * 98, 40, 92, 30, lab,
                       lambda sub=sub: self.set_sub_tab(sub), self.set_sub == sub)
        if self.page == "play":
            net = g.net
            gfx.text("READY WHEN YOU ARE", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Enter the arena.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Create a match, or join a server from the server browser. No port forwarding needed.",
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

            overlays = []     # open dropdown lists: drawn last so they sit on top

            def do_host():
                if not self.check_ready():
                    return
                g.player_name = pname()
                g.P.name = g.player_name
                ok = g.net_host(self.room.strip() or (pname() + "'s server"))
                self.status = net.status
                if ok:
                    self.on_launch()

            def do_join(r):
                if not self.check_ready():
                    return
                g.player_name = pname()
                g.P.name = g.player_name
                if g.net_join(r) is False and getattr(g, "join_msg", ""):
                    self.status = g.join_msg
                elif getattr(g, "join_msg", ""):
                    self.status = g.join_msg

            def do_offline():
                if self.check_ready():
                    self.on_launch()

            def do_create():
                self.cm_open = False
                if self.play_online:
                    do_host()
                else:
                    do_offline()

            def do_leave():
                g.net_leave("Left the server.")
                self.status = "Left the server."

            def set_mode(v):
                self.play_online = (v == "online")

            def set_time(v):
                FX.day = (v == "day")
                save_settings(g, self)

            def dropdown(x, y, w, key, label, value, opts, setter, enabled=True):
                gfx.text(label, x, y - 17, 11, (119, 119, 119), bold=True)
                openn = self.dd == key
                hv = enabled and hover(x, y, w, 34)
                gfx.box(x, y, w, 34, c255(36, 36, 36) if (hv or openn) else c255(24, 24, 24),
                        c255(90, 90, 90) if openn else c255(42, 42, 42))
                gfx.text(value, x + 12, y + 9, 13, (255, 255, 255) if enabled else (100, 100, 100), bold=True)
                gfx.text("^" if openn else "v", x + w - 16, y + 9, 12, (154, 154, 154) if enabled else (70, 70, 70), anchor="c")
                if enabled:
                    self.hot.append((x, y, w, 34, lambda k=key: setattr(self, "dd", None if self._dd_was == k else k), None))
                    if openn:
                        overlays.append((x, y + 35, w, opts, setter, value))

            # --- top panel: current server / create match ---
            if net.mode == "host":
                panel(cx, 128, cw, 112, "Hosting: " + net.room[:40],
                      "Your server is listed in the server browser. Friends join with one click.")
                gfx.text("Players: %d / %d" % (len(net.peers) + 1, NET_MAX_PLAYERS),
                         cx + 20, 180, 13, (200, 200, 200))
                button(cx + 20, 202, 150, 32, "Close server", do_leave)
                button(cx + 182, 202, 150, 32, "Back to game", self.on_launch, True)
                ly = 252
            elif net.mode == "client":
                panel(cx, 128, cw, 112, "On server: " + net.room[:40], "Connected to the host. Players: %d" % (len(net.players) + 1))
                button(cx + 20, 202, 150, 32, "Leave server", do_leave)
                button(cx + 182, 202, 150, 32, "Back to game", self.on_launch, True)
                ly = 252
            elif net.mode == "joining":
                panel(cx, 128, cw, 112, "Connecting ...", "Reaching the server. This can take a few seconds.")
                button(cx + 20, 202, 150, 32, "Cancel", do_leave)
                ly = 252
            else:
                if self.cm_open:
                    online = self.play_online
                    byy = 330 if online else 272
                    panel(cx, 128, cw, byy + 38 + 24 - 128, "Create match", "Pick your options, then press Start match. Change your name in Profile.")
                    dropdown(cx + 20, 204, 150, "mode", "MODE", "Online" if online else "Offline",
                             [("Offline", "offline"), ("Online", "online")], set_mode)
                    dropdown(cx + 182, 204, 150, "time", "TIME", "Day" if FX.day else "Night",
                             [("Night", "night"), ("Day", "day")], set_time)
                    if online:
                        gfx.text("SERVER NAME", cx + 20, 255, 11, (119, 119, 119), bold=True)
                        field(cx + 20, 272, 474, 34, "room", "Server name")
                    button(cx + 20, byy, 170, 38, "Start match >", do_create, True)
                    button(cx + 202, byy, 100, 38, "Load map", lambda: self.pick("map"))
                    button(cx + 314, byy, 100, 38, "Back", lambda: setattr(self, "cm_open", False))
                    miss = self.missing()
                    if miss:
                        gfx.text("Pick mods in Skins first: " + ", ".join(miss), cx + 430, byy + 12, 11, (255, 193, 7))
                    ly = LH
                else:
                    ly = 128

            # --- roster / server browser ---
            if net.active:
                panel(cx, ly, cw, LH - 22 - ly, "Players on the server")
                rows = [(g.P.name or "You", g.P.team, True)] + [(p["n"], p["tm"], False) for p in net.players.values()]
                gfx.text("Drag yourself to the other team to switch", cx + cw - 20, ly + 16, 11, (130, 130, 130), anchor="r")
                colw_ = (cw - 60) / 2
                self._team_mid = cx + cw / 2
                dragging = bool(getattr(self, "team_drag", False))
                mx_, my_ = self.mp

                def draw_row(rx, ry, rw, nm, tm, me):
                    col_ = TEAM_CT if tm == 0 else TEAM_T
                    gfx.box(rx, ry, rw, 28, c255(36, 36, 36), c255(157, 211, 103) if me else c255(48, 48, 48))
                    gfx.rect(rx + 4, ry + 2, 24, 24, c255(157, 211, 103) if me else c255(60, 60, 60))
                    if me and self.pfp_tex:
                        gl.glEnable(gl.GL_TEXTURE_2D)
                        gl.glBindTexture(gl.GL_TEXTURE_2D, self.pfp_tex)
                        gl.glColor4f(1, 1, 1, 1)
                        gl.glBegin(gl.GL_QUADS)
                        gl.glTexCoord2f(0, 0); gl.glVertex2f(rx + 7, ry + 5)
                        gl.glTexCoord2f(1, 0); gl.glVertex2f(rx + 25, ry + 5)
                        gl.glTexCoord2f(1, 1); gl.glVertex2f(rx + 25, ry + 23)
                        gl.glTexCoord2f(0, 1); gl.glVertex2f(rx + 7, ry + 23)
                        gl.glEnd()
                    else:
                        gfx.text((nm.strip() or "?")[0].upper(), rx + 16, ry + 5, 13, (230, 230, 230), bold=True, anchor="c")
                    gfx.text(nm[:20] + ("  (you)" if me else ""), rx + 36, ry + 6, 13,
                             (255, 255, 255), bold=me, mono=True)

                for t_ in (0, 1):
                    x0 = cx + 20 + t_ * (colw_ + 20)
                    over = dragging and ((mx_ > cx + cw / 2) == bool(t_))
                    gfx.box(x0, ly + 44, colw_, LH - 22 - ly - 64,
                            c255(34, 46, 28) if over else c255(22, 22, 22),
                            c255(157, 211, 103) if over else c255(42, 42, 42))
                    gfx.rect(x0, ly + 44, colw_, 34, team_tint(TEAM_CT if t_ == 0 else TEAM_T))
                    draw_team_logo(x0 + 4, ly + 46, 30, t_)
                    gfx.text(TEAM_NAMES[t_], x0 + 44, ly + 52, 15, TEAM_CT if t_ == 0 else TEAM_T, bold=True)
                    yy = ly + 86
                    for nm, tm, me in rows:
                        if tm != t_:
                            continue
                        if not (me and dragging):
                            draw_row(x0 + 6, yy, colw_ - 12, nm, tm, me)
                            if me:
                                self.hot.append((x0 + 6, yy, colw_ - 12, 28, None,
                                                 lambda mxl: setattr(self, "team_drag", True)))
                        yy += 34
                if dragging:
                    draw_row(mx_ - (colw_ - 12) / 2, my_ - 14, colw_ - 12, g.P.name or "You", g.P.team, True)
            elif not net.mode and not self.cm_open:
                allr = net.rooms
                shown = list(allr.values())
                keyf = {"n": lambda r: r["n"].lower(), "h": lambda r: r["h"].lower(),
                        "map": lambda r: r["map"], "p": lambda r: r["p"]}
                shown.sort(key=keyf.get(self.sb_sort, keyf["n"]), reverse=self.sb_rev)

                by, bh = ly, LH - 22 - ly
                gfx.rect(cx, by, cw, bh, c255(17, 17, 17))
                gfx.text("Server browser", cx + 20, by + 12, 14, (255, 255, 255), bold=True)
                button(cx + 20, by + 36, 150, 28, "Create match",
                       lambda: setattr(self, "cm_open", not self.cm_open), True)

                # column header
                hy = by + 68
                cols = [("n", "Server name", 34), ("h", "Host", int(cw * 0.40)), ("map", "Map", int(cw * 0.58)),
                        ("p", "Players", int(cw * 0.76)), (None, "Type", int(cw * 0.88))]
                gfx.rect(cx, hy, cw, 24, c255(28, 28, 28))
                gfx.rect(cx, hy + 23, cw, 1, c255(42, 42, 42))
                for i, (ck, cl, co) in enumerate(cols):
                    nxt = cols[i + 1][2] if i + 1 < len(cols) else cw
                    arrow = ("  ^" if not self.sb_rev else "  v") if (ck and self.sb_sort == ck) else ""
                    gfx.text(cl + arrow, cx + co, hy + 5, 11, (200, 200, 200) if ck and self.sb_sort == ck else (140, 140, 140), bold=True)
                    if i:
                        gfx.rect(cx + co - 8, hy + 4, 1, 16, c255(48, 48, 48))
                    if ck:
                        def sort_by(k=ck):
                            if self.sb_sort == k:
                                self.sb_rev = not self.sb_rev
                            else:
                                self.sb_sort, self.sb_rev = k, False
                        self.hot.append((cx + co - 8, hy, nxt - co, 24, sort_by, None))

                # rows
                RH = 26
                ry0 = hy + 24
                vis = max(1, int((by + bh - 52 - ry0) // RH))
                self.sb_scroll = max(0, min(self.sb_scroll, max(0, len(shown) - vis)))
                if not shown:
                    gfx.text("No servers found yet.", cx + cw / 2, ry0 + 40, 14, (150, 150, 150), bold=True, anchor="c")
                    gfx.text("Create an online match, or ask a friend to - servers show up here by themselves.",
                             cx + cw / 2, ry0 + 64, 12, (110, 110, 110), anchor="c")
                sel = next((r for r in shown if r["id"] == self.sb_sel), None)
                if not sel:
                    self.sb_sel = None
                for i, r in enumerate(shown[self.sb_scroll:self.sb_scroll + vis]):
                    yy = ry0 + i * RH
                    full = r["p"] >= r["m"]
                    other = r["map"] != g.map_sig()
                    is_sel = r["id"] == self.sb_sel
                    hv = hover(cx, yy, cw, RH)
                    if is_sel:
                        gfx.rect(cx, yy, cw, RH, c255(38, 48, 30))
                        gfx.rect(cx, yy, 3, RH, c255(157, 211, 103))
                    else:
                        gfx.rect(cx, yy, cw, RH, c255(30, 30, 30) if hv else (c255(21, 21, 21) if i % 2 else c255(17, 17, 17)))
                    dot = (229, 57, 53) if full else ((255, 193, 7) if other else (157, 211, 103))
                    gfx.rect(cx + 14, yy + 9, 8, 8, (dot[0] / 255, dot[1] / 255, dot[2] / 255, 1.0))
                    gfx.text(r["n"][:26], cx + cols[0][2], yy + 6, 13, (255, 255, 255), bold=is_sel, mono=True)
                    gfx.text(r["h"][:14], cx + cols[1][2], yy + 7, 12, (170, 170, 170), mono=True)
                    mp = r["map"]
                    gfx.text("arena" if mp == "arena" else "custom_" + mp[:5], cx + cols[2][2], yy + 7, 12,
                             (255, 193, 7) if other else (200, 200, 200), mono=True)
                    gfx.text("%d/%d" % (r["p"], r["m"]), cx + cols[3][2], yy + 7, 12,
                             (229, 57, 53) if full else (200, 200, 200), mono=True)
                    gfx.text("LAN" if r.get("src") == "lan" else "Internet", cx + cols[4][2], yy + 7, 12, (140, 140, 140), mono=True)

                    def pick_row(r=r):
                        now_ = time.time()
                        if self.sb_click[0] == r["id"] and now_ - self.sb_click[1] < 0.45 and r["p"] < r["m"]:
                            do_join(r)
                        self.sb_sel = r["id"]
                        self.sb_click = (r["id"], now_)
                    self.hot.append((cx, yy, cw, RH, pick_row, None))
                if len(shown) > vis:      # scroll hint
                    gfx.text("scroll for more (%d-%d of %d)" % (self.sb_scroll + 1, min(len(shown), self.sb_scroll + vis), len(shown)),
                             cx + cw - 20, by + bh - 74, 11, (110, 110, 110), anchor="r")

                # bottom bar: Refresh / Connect
                fy = by + bh - 46
                gfx.rect(cx, fy, cw, 46, c255(22, 22, 22))
                gfx.rect(cx, fy, cw, 1, c255(42, 42, 42))

                def do_refresh():
                    net.lan_rooms.clear()
                    net.net_rooms.clear()
                    try:
                        t = "%s/rooms/+" % BROKER_ROOT
                        net.broker.unsub(t)
                        net.broker.sub(t)
                    except Exception:
                        pass
                    self.sb_scroll = 0

                def do_connect():
                    if sel and sel["p"] < sel["m"]:
                        do_join(sel)
                button(cx + 14, fy + 7, 110, 32, "Refresh", do_refresh)
                ok_conn = bool(sel) and sel["p"] < sel["m"]
                if ok_conn:
                    button(cx + 134, fy + 7, 130, 32, "Connect", do_connect, True)
                else:
                    gfx.box(cx + 134, fy + 7, 130, 32, c255(20, 20, 20), c255(32, 32, 32))
                    gfx.text("Connect", cx + 199, fy + 16, 13, (80, 80, 80), bold=True, anchor="c")
                gfx.text("%d server%s" % (len(shown), "" if len(shown) == 1 else "s"), cx + cw - 20, fy + 16, 12, (140, 140, 140), anchor="r")
                if sel and sel["map"] != g.map_sig():
                    have = sel["map"] == "arena" or g.find_map_by_sig(sel["map"]) is not None
                    gfx.text("Other map - you have it, it loads on join." if have else
                             "Other map - you don't have it.", cx + 284, fy + 16, 11,
                             (157, 211, 103) if have else (255, 193, 7))

            # --- open dropdown lists (on top of everything) ---
            for (ox_, oy_, ow_, opts_, setter_, cur_) in overlays:
                gfx.box(ox_, oy_, ow_, 32 * len(opts_) + 2, c255(17, 17, 17), c255(90, 90, 90))
                for i, (lab, val) in enumerate(opts_):
                    iy = oy_ + 1 + i * 32
                    hv = hover(ox_, iy, ow_, 32)
                    gfx.rect(ox_ + 1, iy, ow_ - 2, 32, c255(46, 46, 46) if hv else c255(17, 17, 17))
                    gfx.text(lab, ox_ + 12, iy + 9, 13, (255, 255, 255) if lab == cur_ else (190, 190, 190), bold=(lab == cur_))
                    self.hot.append((ox_ + 1, iy, ow_ - 2, 32, lambda v=val, f=setter_: f(v), None))

            # --- status line (map loaded, join errors, connection progress) ---
            msg = net.status if net.mode else self.status
            bad = msg.startswith(("Denied", "Could", "Conn", "Lost", "The host", "Cannot", "Error"))
            col = (255, 193, 7) if bad else (170, 170, 170)
            if net.mode or bad:   # only show room status / errors, not the weapons-loaded text
                lines = [msg[i:i + 56] for i in range(0, min(len(msg), 168), 56)]
                tw = max(gfx.text_w(l, 12, False, True) for l in lines) + 32
                th = 16 + 17 * len(lines)
                tx, ty = LW - 40 - tw, LH - 20 - th
                gfx.rect(tx, ty, tw, th, c255(41, 41, 41))          # toast: #292929
                for i, l in enumerate(lines):
                    gfx.text(l, tx + 16, ty + 9 + i * 17, 12, col if bad else (255, 255, 255), mono=True)
        elif self.page == "profile":
            gfx.text("YOUR IDENTITY", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Profile.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Change your username and profile picture.", cx, 96, 13, (154, 154, 154))
            panel(cx, 128, cw, 250, "")
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
            # Class screen: grid of weapon cards with live 3D model previews (same renderer as the Skins tab)
            gfx.text("CLASS", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Loadout.", cx, 48, 34, (255, 255, 255), bold=True)
            gfx.text("Choose your primary weapon. You carry one gun at a time, plus your knife. Skins are changed in the Skins tab.",
                     cx, 96, 13, (154, 154, 154))
            wl = CFG["weapons"]
            guns = [i for i, w in enumerate(wl) if not w.melee]
            kn = g.knife_index()
            cur = wl[g.primary]

            def lo_stats(w):
                pel = int(w.pellets or 1)
                dmg = (w.dmg or 0) * (pel * .45 if pel > 1 else 1)
                return (("Damage", dmg / 100.0), ("Fire rate", (w.rpm or 0) / 600.0),
                        ("Magazine", (w.mag or 0) / 30.0), ("Accuracy", 1.0 - (w.spr or 0) / .05))

            # equipped summary strip
            gfx.text("EQUIPPED", cx, 134, 11, (119, 119, 119), bold=True)
            gfx.rect(cx, 154, cw, 44, c255(17, 17, 17))
            gfx.rect(cx, 154, 4, 44, c255(157, 211, 103))
            gfx.text(str(cur.n), cx + 20, 162, 20, (255, 255, 255), bold=True)
            gfx.text(str(cur.cls or "Weapon"), cx + 20 + gfx.text_w(str(cur.n), 20, True) + 12, 168, 12, (130, 130, 130))
            gfx.text("+ " + str(wl[kn].n) + "  (key 2)", cx + cw - 16, 168, 12, (154, 154, 154), anchor="r")

            gap = 16
            cards = [(i, False) for i in guns] + [(kn, True)]
            gy0 = 222
            avail = max(150, int(LH - gy0 - 24))
            gap = 12
            # Fixed 4-column x 2-row grid (8 slots per page); previews shrink to fit the window height.
            ncols, nrows = 4, 2
            pvh = max(60, min(132, int((avail - gap * (nrows - 1)) / nrows - 118)))
            cwd = (cw - gap * (ncols - 1)) / ncols
            chd = pvh + 118
            per_pg = ncols * nrows
            npg = max(1, (len(cards) + per_pg - 1) // per_pg)
            self.lo_gpage = max(0, min(getattr(self, "lo_gpage", 0), npg - 1))
            if npg > 1:
                gfx.text("%d / %d  (scroll)" % (self.lo_gpage + 1, npg), cx + cw, 134, 11, (119, 119, 119), bold=True, anchor="r")
            for idx, (i, is_melee) in enumerate(cards[self.lo_gpage * per_pg:(self.lo_gpage + 1) * per_pg]):
                r_, c_ = divmod(idx, ncols)
                x = cx + c_ * (cwd + gap)
                y = gy0 + r_ * (chd + gap)
                w = wl[i]
                on = (i == g.primary) and not is_melee
                hv = hover(x, y, cwd, chd) and not is_melee
                gfx.box(x, y, cwd, chd, c255(26, 26, 26) if (on or hv) else c255(17, 17, 17),
                        c255(157, 211, 103) if on else (c255(120, 120, 120) if hv else c255(42, 42, 42)))
                inst = self.pv_weapon(w._m) if w._m else None
                self.draw_card_preview(W, H, x + 2, y + 2, cwd - 4, pvh, "weapon", inst)
                if inst is None:
                    gfx.text("NO MODEL LOADED", x + cwd / 2, y + 2 + pvh / 2 - 6, 11, (90, 90, 90), bold=True, anchor="c")
                gfx.rect(x + 2, y + pvh + 2, cwd - 4, 1, c255(42, 42, 42))
                ty = y + pvh + 10
                gfx.text(str(w.n), x + 12, ty, 17, (255, 255, 255) if (on or is_melee) else (210, 210, 210), bold=True)
                gfx.text(str(w.cls or ("Melee" if is_melee else "Weapon")), x + 12, ty + 24, 11, (130, 130, 130))
                if on:
                    gfx.rect(x + cwd - 84, ty + 2, 72, 18, c255(157, 211, 103, .18))
                    gfx.text("EQUIPPED", x + cwd - 48, ty + 5, 10, (157, 211, 103), bold=True, anchor="c")
                elif is_melee:
                    gfx.text("ALWAYS", x + cwd - 12, ty + 5, 10, (130, 130, 130), bold=True, anchor="r")
                if is_melee:
                    gfx.text("Always equipped (key 2)", x + 12, ty + 52, 11, (110, 110, 110))
                else:
                    bw_ = (cwd - 24 - 12) / 2
                    for k, (lab, v) in enumerate(lo_stats(w)):
                        bx = x + 12 + (k % 2) * (bw_ + 12)
                        by = ty + 48 + (k // 2) * 28
                        gfx.text(lab.upper(), bx, by, 9, (110, 110, 110), bold=True)
                        gfx.rect(bx, by + 14, bw_, 5, c255(42, 42, 42))
                        gfx.rect(bx, by + 14, bw_ * max(.03, min(1.0, v)), 5,
                                 c255(157, 211, 103) if on else c255(200, 200, 200))
                    self.hot.append((x, y, cwd, chd, lambda i=i: g.set_primary(i), None))
        elif self.page == "skins":
            gfx.text("YOUR KIT", cx, 32, 11, (119, 119, 119), bold=True)
            gfx.text("Skins.", cx, 48, 34, (255, 255, 255), bold=True)
            _lx = cx
            _has_l4d2 = draw_mod_image("l4d2", cx, 93, 20)
            _has_gmod = draw_mod_image("gmod", cx + (26 if _has_l4d2 else 0), 93, 20)
            if _has_l4d2 or _has_gmod:
                _lx = cx + (26 if _has_l4d2 else 0) + (26 if _has_gmod else 0)
            gfx.text("Mod support", _lx, 96, 13, (154, 154, 154))
            wp = self.lo_tab == "weapons"
            ar = self.lo_tab == "arms"
            button(cx, 124, 130, 32, "Weapon mods", lambda: self.lo_set("weapons"), wp)
            button(cx + 140, 124, 130, 32, "Player models", lambda: self.lo_set("players"), self.lo_tab == "players")
            button(cx + 280, 124, 100, 32, "Arms", lambda: self.lo_set("arms"), ar)
            ws = CFG["weapons"][:5]
            if wp:
                items = g.weapon_mods(self.lo_slot)
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
                    button(cx + i * 140, 166, 132, 30, w.n[:16],
                           lambda i=i: setattr(self, "lo_slot", i), self.lo_slot == i)
                cur = ws[self.lo_slot] if ws else None
                info = ("%s: " % cur.n + (cur._m or "no model - box")
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
                    # logo only, centred in the button (name is just a fallback when no logo file exists)
                    if not draw_team_logo(bx_ + (150 - 26) / 2, 168, 26, 0 if ct_ else 1):
                        gfx.text(TEAM_NAMES[0 if ct_ else 1], bx_ + 75, 166 + 30 / 2 - 9, 13,
                                 (TEAM_CT if ct_ else TEAM_T) if on_ else (154, 154, 154), bold=True, anchor="c")
                    self.hot.append((bx_, 166, 150, 30, lambda tk=tk: setattr(self, "lo_team", tk), None))
                info = (g.pm_cfg[self.lo_team] or "none picked yet")
            gfx.text(info[:100], cx, 204, 12, (170, 170, 170))

            gy, cw2, ch2 = 226, 216, 170
            if not items or (ar and len(items) <= 1 and not g.arms_all):
                if wp and ws:
                    wname = str(ws[self.lo_slot].n)
                    empty = f"No models for {wname}."
                    hint = f"Put them in mod/weapon/{wname.lower().replace(' ', '-')}/  (only that folder's models show here)."
                else:
                    empty = ("No weapon mods loaded yet." if wp else
                             ("No arms models loaded yet." if ar else "No player models loaded yet."))
                    hint = "Use Add files, drop files on the window, or put them in the " + (
                        "weapons" if wp else ("arms (or drop a .vpk / .gma)" if ar else "player")) + " folder."
                gfx.text(empty, cx + cw / 2, gy + 120, 15, (200, 200, 200), bold=True, anchor="c")
                gfx.text(hint, cx + cw / 2, gy + 146, 12, (130, 130, 130), anchor="c")
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
                    tags = [(TEAM_NAMES[0 if tk == "CT" else 1], team_tint(TEAM_CT) if tk == "CT" else team_tint(TEAM_T),
                             TEAM_CT if tk == "CT" else TEAM_T)
                            for tk in ("CT", "T") if g.pm_cfg[tk] == name]
                bx = x + 10
                for txt, bg, fg in tags:
                    tm_ = TEAM_NAMES.index(txt) if (not wp and not ar and txt in TEAM_NAMES) else None
                    if tm_ is not None and team_logo(tm_):
                        # team tag = logo only
                        gfx.rect(bx, y + 144, 30, 18, bg)
                        draw_team_logo(bx + 7, y + 145, 16, tm_)
                        bx += 30 + 6
                        continue
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
            gfx.text("Two keys per action (left = main, right = second). The same key can be used for several actions. Backspace clears the second.", cx + cw - 20, 284 + 14, 11, (119, 119, 119), anchor="r")
            for i, (aid, label) in enumerate(BIND_META):
                col, row = divmod(i, 7)
                rx = cx + 20 + col * 330
                ry = 282 + 44 + row * 38
                gfx.box(rx, ry, 320, 32, c255(17, 17, 17), c255(24, 24, 24))
                gfx.text(label, rx + 10, ry + 9, 11, (230, 230, 230))
                bw = 76
                for slot, sid in enumerate((aid, aid + "#2")):      # main key, second key
                    bx = rx + 320 - (2 - slot) * (bw + 4) - 2
                    waiting = self.waiting == sid
                    hv = hover(bx, ry + 3, bw, 26)
                    gfx.box(bx, ry + 3, bw, 26,
                            c255(52, 52, 52) if waiting else (c255(42, 42, 42) if hv else c255(24, 24, 24)),
                            c255(200, 200, 200) if waiting else c255(42, 42, 42))
                    val = g.binds[aid] if slot == 0 else g.binds2.get(aid, "")
                    txt = "Press..." if waiting else (key_label(val).replace("Mouse ", "M").replace(" / Back", "").replace(" / Forward", "") if val else "-")
                    gfx.text(txt, bx + bw / 2, ry + 9, 11, (255, 255, 255) if val or waiting else (110, 110, 110), anchor="c")
                    self.hot.append((bx, ry + 3, bw, 26, lambda a=sid: setattr(self, "waiting", a), None))
            button(cx + 350, 282 + 44 + 6 * 38, 150, 32, "Reset defaults", self.reset_keys)
            button(cx + 512, 282 + 44 + 6 * 38, 150, 32, "Back to play", lambda: self.set_page("play"), True)
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
            gfx.text("Type the resolution you want. Only the game is stretched to fill the window; menus stay normal.",
                     cx, 96, 13, (154, 154, 154))
            panel(cx, 126, cw, 150, "Resolution",
                  "Smaller / narrower = stretched look (players and weapons look wider). Press Enter or Apply.")
            for fx_, key_, lab_ in ((cx + 20, "resw", "Width"), (cx + 190, "resh", "Height")):
                gfx.text(lab_.upper(), fx_, 172, 11, (189, 189, 189), bold=True)
                on_ = self.edit == key_
                gfx.box(fx_, 192, 140, 36, c255(10, 10, 10), c255(170, 170, 170) if on_ else c255(42, 42, 42))
                val_ = getattr(self, key_)
                gfx.text(val_ + ("|" if on_ and int(time.time() * 2) % 2 == 0 else ""), fx_ + 12, 200, 14, (255, 255, 255))
                self.hot.append((fx_, 192, 140, 36, lambda k=key_: setattr(self, "edit", k), None))
            gfx.text("x", cx + 172, 200, 16, (154, 154, 154), bold=True, anchor="c")
            button(cx + 360, 192, 110, 36, "Apply", self.apply_res, True)
            button(cx + 480, 192, 150, 36, "Reset 1280x720",
                   lambda: (setattr(self, "resw", "1280"), setattr(self, "resh", "720"), self.apply_res()))
            gfx.text("Now: %d x %d" % g.res, cx + 20, 248, 12, (170, 170, 170))
            panel(cx, 292, cw, 92, "Field of view",
                  "Changes how much of the world you see. The viewmodel (your weapon) stays the same.")
            gfx.text(f"{int(round(g.fov_cfg))}", cx + cw - 20, 14 + 292, 13, (255, 255, 255), bold=True, anchor="r")
            slider(cx + 20, 292 + 62, cw - 190, g.fov_cfg, 50, 110, lambda v: setattr(g, "fov_cfg", float(round(v))))
            button(cx + cw - 150, 292 + 54, 130, 30, "Reset to 75", lambda: setattr(g, "fov_cfg", 75.0))
            panel(cx, 400, cw, 100, "Fullscreen", "Fills the whole screen. You can also press F11 any time.")
            button(cx + 20, 400 + 54, 200, 36, "Fullscreen: ON" if g.fullscreen else "Fullscreen: OFF",
                   lambda: (set_fullscreen(g, not g.fullscreen), save_settings(g, self)), bool(g.fullscreen))
            panel(cx, 508, cw, 100, "FPS counter / Speed meter / Jump stats",
                  "FPS: top-left. Speed: bottom center. Jump stats: distance, speed and strafes in chat.")
            button(cx + 20, 508 + 54, 200, 36, "FPS counter: ON" if g.show_fps else "FPS counter: OFF",
                   lambda: (setattr(g, "show_fps", not g.show_fps), save_settings(g, self)), bool(g.show_fps))
            button(cx + 240, 508 + 54, 200, 36, "Speed meter: ON" if g.show_speed else "Speed meter: OFF",
                   lambda: (setattr(g, "show_speed", not g.show_speed), save_settings(g, self)), bool(g.show_speed))
            button(cx + 460, 508 + 54, 200, 36, "Jump stats: ON" if g.show_jstats else "Jump stats: OFF",
                   lambda: (setattr(g, "show_jstats", not g.show_jstats), save_settings(g, self)), bool(g.show_jstats))
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

    def apply_res(self):
        try:
            w = int(self.resw or 0)
            h = int(self.resh or 0)
        except ValueError:
            w = h = 0
        w = max(160, min(7680, w or self.g.res[0]))
        h = max(120, min(4320, h or self.g.res[1]))
        self.g.res = (w, h)
        self.resw, self.resh = str(w), str(h)
        save_settings(self.g, self)

    def reset_keys(self):
        self.g.binds.clear()
        self.g.binds.update(DEFAULT_BINDS)
        self.g.binds2.clear()
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
        self.dd = None
        self.waiting = None
        self.focus = False

    # --- loadout helpers ---
    def lo_set(self, tab):
        self.lo_tab = tab
        self.lo_page = 0

    def wheel(self, dy):
        if self.page == "play" and dy:
            self.sb_scroll = max(0, self.sb_scroll + (-1 if dy > 0 else 1))
        if self.page == "skins" and dy:
            self.lo_page = max(0, self.lo_page + (-1 if dy > 0 else 1))
        if self.page == "loadout" and dy:
            self.lo_gpage = max(0, getattr(self, "lo_gpage", 0) + (-1 if dy > 0 else 1))

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
            if g.P:
                g.P.pm = None              # third-person model is rebuilt on demand
                g.P._pm_tried = False
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
        """Standalone arms model for the Skins Arms tab preview."""
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
                # Stable idle pose (force) so the shared instance is not mid-fade / rate-limited
                if hasattr(inst, "pose") and hasattr(inst, "find"):
                    try:
                        idle = inst.find("idle")
                        if idle:
                            inst.pose(idle[0], 0.0, loop=True, force=True)
                    except Exception:
                        pass
            inst.draw()
            gl.glPopMatrix()
            gl.glMatrixMode(gl.GL_PROJECTION)
            gl.glPopMatrix()
            gl.glMatrixMode(gl.GL_MODELVIEW)
        self._end_3d_preview(W, H)

    def _end_3d_preview(self, W, H):
        """Restore fixed-function state so the 2D menu is not corrupted after a 3D preview."""
        try:
            gl.glFrontFace(gl.GL_CCW)
            gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
            gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
            gl.glDisable(gl.GL_ALPHA_TEST)
            gl.glDepthMask(gl.GL_TRUE)
            gl.glDisable(gl.GL_CULL_FACE)
        except Exception:
            pass
        gl.glViewport(0, 0, W, H)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_TEXTURE_2D)
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
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_ALPHA_TEST)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glPushMatrix()
        gl.glLoadIdentity()
        # Always use the preview panel aspect — g.aspect can be 0 or stale from a previous match
        aspect = (vw / max(1, vh)) if vw and vh else (16 / 9)
        glu.gluPerspective(75, aspect, VM_NEAR, VM_FAR)
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glPushMatrix()
        gl.glLoadIdentity()
        glu.gluLookAt(0, 0, 0, 1, 0, 0, 0, 0, 1)
        ws = CFG["weapons"]
        flip = False
        try:
            if ws:
                w = ws[max(0, min(self.vm_prev, len(ws) - 1))]
                if w.vm:
                    idle = w.vm.find("idle")
                    if idle and 0 <= idle[0] < len(w.vm.seqs):
                        # Hold idle frame 0 (force): avoids rate-limit stutter and leftover
                        # shoot/reload fade from the live viewmodel instance.
                        w.vm.pose(idle[0], 0.0, loop=True, force=True)
                    o = w.vm_ofs or (0, 0, 0)
                    gl.glTranslatef(o[0] + g.vm_adj[0], o[1] - g.vm_adj[1], o[2] + g.vm_adj[2])
                    flip = bool(w.vm_flip) != bool(g.hand_swap)
                    if flip:
                        gl.glScalef(1, -1, 1)
                        gl.glFrontFace(gl.GL_CW)
                    w.vm.draw()
                else:
                    gl.glTranslatef(g.vm_adj[0], -g.vm_adj[1], g.vm_adj[2])
                    draw_box(12, -5, -5, 8, 1.25, 1.75, (0x2b / 255, 0x2b / 255, 0x2b / 255))
        finally:
            if flip:
                try:
                    gl.glFrontFace(gl.GL_CCW)
                except Exception:
                    pass
            gl.glPopMatrix()
            gl.glMatrixMode(gl.GL_PROJECTION)
            gl.glPopMatrix()
            gl.glMatrixMode(gl.GL_MODELVIEW)
            self._end_3d_preview(W, H)

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

