#!/usr/bin/env python3
"""FAD-MOD entry point."""
import os
import sys
import time
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

from fadmod.config import *
from fadmod.glutil import FX
from fadmod.world import ArenaWorld, FadMapWorld
from fadmod.audio import Audio
from fadmod.net import NET
from fadmod.game import Game
from fadmod.gfx import Gfx, RenderTarget
from fadmod.menu import Menu
from fadmod.render import render_scene, render_hud
from fadmod.settings import load_settings, save_settings, set_fullscreen, file_dialog

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
    # MSAA off (sharper image; no edge soft-blur from multisampling)
    flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.RESIZABLE
    # vsync off: uncapped FPS (may tear; highest frame rate)
    try:
        pygame.display.gl_set_attribute(pygame.GL_SWAP_CONTROL, 0)
    except Exception:
        pass
    try:
        pygame.display.set_mode((1280, 720), flags, vsync=0)
    except TypeError:
        pygame.display.set_mode((1280, 720), flags)
    except Exception:
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
            save_settings(game, menu)      # keeps your XP

    menu = Menu(game, gfx, launch, file_dialog)
    game.menu = menu               # lets the scoreboard draw your profile picture
    load_settings(game, menu)
    game.set_world(ArenaWorld())
    if getattr(game, "want_fullscreen", False):
        set_fullscreen(game, True)
    game.player_name = menu.name.strip() or "Player"
    game.P.name = game.player_name

    # Scan the mod folders so everything shows up in Skins. Nothing is EQUIPPED automatically
    # (game.manual_mods): you pick the weapons / players / arms yourself, or play with none.
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

    if not any(mod_folders(n) for n in ("weapon", "player", "arms")):
        menu.status = (menu.status + " No mod folder found next to the script (mod/weapon, mod/player, mod/arms).").strip()

    clock = pygame.time.Clock()
    last = time.perf_counter()
    running = True
    rt = RenderTarget(*game.res)
    while running:
        win_w, win_h = pygame.display.get_window_size()
        if (rt.w, rt.h) != tuple(game.res):          # resolution changed in Settings > Video
            old = rt
            rt = RenderTarget(*game.res)
            try:
                # Without MSAA, fbo == res_fbo — must not delete the same id twice
                fbos = list({int(x) for x in (getattr(old, "fbo", 0), getattr(old, "res_fbo", 0)) if x})
                if fbos:
                    gl.glDeleteFramebuffers(len(fbos), fbos)
                if getattr(old, "tex", None):
                    gl.glDeleteTextures([old.tex])
                if getattr(old, "depth_rb", None):
                    gl.glDeleteRenderbuffers(1, [old.depth_rb])
            except Exception:
                pass
        W, H = (rt.w, rt.h) if rt.ok else (win_w, win_h)
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
                # toggle console with the bound key (default ` / tilde)
                _kn = pygame.key.name(ev.key).lower()
                _uni = (ev.unicode or "").lower()
                _is_con = False
                for _ck in ((game.binds.get("console") or "`").lower().strip(),
                            (game.binds2.get("console") or "").lower().strip()):
                    if not _ck:
                        continue
                    if _ck in ("`", "~", "tilde", "backquote"):
                        _is_con = _is_con or (ev.key == pygame.K_BACKQUOTE or _kn in ("`", "~", "tilde", "backquote")
                                              or _uni in ("`", "~"))
                    else:
                        _is_con = _is_con or (_kn == _ck or _uni == _ck)
                if _is_con:
                    game.con_toggle()
                    continue
                if game.con_open:
                    if ev.key == pygame.K_ESCAPE:
                        game.con_toggle()
                    elif ev.key == pygame.K_RETURN or ev.key == pygame.K_KP_ENTER:
                        game.con_run(game.con_text)
                        game.con_text = ""
                    elif ev.key == pygame.K_BACKSPACE:
                        game.con_text = game.con_text[:-1]
                    elif ev.key == pygame.K_UP:
                        if game.con_hist:
                            if game.con_hist_i < 0:
                                game.con_hist_i = len(game.con_hist) - 1
                            else:
                                game.con_hist_i = max(0, game.con_hist_i - 1)
                            game.con_text = game.con_hist[game.con_hist_i]
                    elif ev.key == pygame.K_DOWN:
                        if game.con_hist and game.con_hist_i >= 0:
                            game.con_hist_i += 1
                            if game.con_hist_i >= len(game.con_hist):
                                game.con_hist_i = -1
                                game.con_text = ""
                            else:
                                game.con_text = game.con_hist[game.con_hist_i]
                    elif ev.key == pygame.K_PAGEUP:
                        game.con_scroll = min(len(game.con_log), game.con_scroll + 3)
                    elif ev.key == pygame.K_PAGEDOWN:
                        game.con_scroll = max(0, game.con_scroll - 3)
                    elif ev.key == pygame.K_TAB:
                        # simple autocomplete for common commands
                        prefix = game.con_text.strip().lower()
                        cands = [c for c in (
                            "help", "bot_add", "bot_add_ct", "bot_add_t", "bot_place",
                            "bot_kick", "bot_kick_all",
                            "bot_kill", "kill", "restart", "god", "noclip",
                            "clear", "fps", "cl_viewbob", "viewbob",
                            "sv_showhitboxes", "quit", "exit", "day", "night") if c.startswith(prefix)]
                        if len(cands) == 1:
                            game.con_text = cands[0] + " "
                        elif len(cands) > 1:
                            game.con_print("  ".join(cands), (160, 180, 200))
                    else:
                        ch = ev.unicode
                        if ch and ch.isprintable() and ch not in "`~":
                            game.con_text += ch
                            if len(game.con_text) > 120:
                                game.con_text = game.con_text[:120]
                    continue
                if game.lock:
                    if ev.key == pygame.K_ESCAPE:
                        set_lock(False)
                        continue
                    raw = pygame.key.name(ev.key)
                    game.held_raw.add(raw)
                    for name in game.key_aliases(raw):       # the key itself + whatever it is a 2nd bind for
                        game.keys[name] = True
                        b = game.binds
                        if name == b["jump"]:
                            game.P.jreq = 1
                        if name == b["reload"]:
                            game.reload_pressed()
                        for i in range(2):
                            if name == b[f"weapon{i + 1}"]:
                                game.select_weapon(i)
                        if name == b["swaphand"]:
                            game.hand_swap = not game.hand_swap
                        if name == b["melee"]:
                            game.melee_pressed()
                        if name == "f":
                            FX.flash = not FX.flash
                else:
                    menu.key_down(ev)
            elif ev.type == pygame.KEYUP:
                _raw = pygame.key.name(ev.key)       # always: keys held when the console opened must not get stuck
                game.held_raw.discard(_raw)
                for _n in game.key_aliases(_raw):
                    game.keys[_n] = game.key_held(_n)
            elif ev.type == pygame.MOUSEMOTION:
                if game.con_open:
                    pass
                elif game.lock:
                    P = game.P
                    # Allow look while dead (death cam). Scoped AWP uses reduced sensitivity.
                    sens = game.sens * ((0.0008 / BASE_SENS) if (P.zoom and P.w == 0) else 1)
                    P.yaw -= ev.rel[0] * sens
                    P.pitch = max(-1.5, min(1.5, P.pitch - ev.rel[1] * sens))
                else:
                    menu.mouse_move(ev.pos)
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                if game.con_open:
                    pass
                elif game.lock:
                    btn = ev.button
                    if btn in (4, 5):
                        continue  # wheel handled by MOUSEWHEEL
                    if btn in (6, 7):
                        raw = "Mouse" + str(btn - 2)
                        game.held_raw.add(raw)
                        for code in game.key_aliases(raw):
                            game.keys[code] = True
                            b = game.binds
                            if code == b["jump"]:
                                game.P.jreq = 1
                            if code == b["reload"]:
                                game.reload_pressed()
                            for i in range(2):
                                if code == b[f"weapon{i + 1}"]:
                                    game.select_weapon(i)
                            if code == b["swaphand"]:
                                game.hand_swap = not game.hand_swap
                            if code == b["melee"]:
                                game.melee_pressed()
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
                        menu.mouse_down(ev.pos, ev.button)
            elif ev.type == pygame.MOUSEBUTTONUP:
                if ev.button == 1:
                    game.keys["mouse1"] = False
                    menu.mouse_up()
                elif ev.button in (6, 7):
                    _raw = "Mouse" + str(ev.button - 2)
                    game.held_raw.discard(_raw)
                    for _n in game.key_aliases(_raw):
                        game.keys[_n] = game.key_held(_n)
            elif ev.type == pygame.MOUSEWHEEL:
                if game.lock:
                    game.P.jreq = 1
                else:
                    menu.wheel(ev.y)

        if menu.quit_req or game.quit_req:
            running = False
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

        if game.lock:
            if rt.ok:
                rt.begin()                      # in-game: drawn at your typed resolution, stretched later
            render_scene(game, W, H)
            render_hud(game, gfx, W, H, False)
            if rt.ok:
                rt.present(win_w, win_h)
        else:
            # menu: drawn straight to the window at its real size (never stretched)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            W, H = win_w, win_h
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
            if game.con_open:
                # console also available from the menu
                ch = min(int(H * 0.42), 320)
                gfx.rect(0, 0, W, ch, (12 / 255, 14 / 255, 18 / 255, 0.92))
                gfx.rect(0, ch - 1, W, 1, (60 / 255, 70 / 255, 85 / 255, 1))
                line_h = 16
                max_lines = max(1, (ch - 36) // line_h)
                log = game.con_log
                start = max(0, len(log) - max_lines - game.con_scroll)
                end = max(0, len(log) - game.con_scroll)
                visible = log[start:end]
                y0 = ch - 36 - line_h * len(visible)
                for i, ln in enumerate(visible):
                    gfx.text(ln["t"][:120], 10, y0 + i * line_h, 13, ln.get("c", (200, 200, 200)), mono=True)
                gfx.rect(0, ch - 28, W, 28, (8 / 255, 10 / 255, 14 / 255, 0.98))
                caret = "|" if int(time.time() * 2) % 2 == 0 else " "
                gfx.text("] " + game.con_text + caret, 10, ch - 22, 14, (255, 255, 255), mono=True)
                gfx.text("~ close  |  help", W - 12, ch - 20, 11, (120, 130, 140), anchor="r", mono=True)
        pygame.display.flip()
        clock.tick()   # no FPS cap

    NET.close()
    save_settings(game, menu)
    pygame.quit()


if __name__ == "__main__":
    main()
