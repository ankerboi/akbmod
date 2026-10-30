"""Load / save settings.json and file dialogs."""
import json
from pathlib import Path

import pygame

from .config import *
from .glutil import FX

def load_settings(game, menu):
    try:
        j = json.loads(SETTINGS_FILE.read_text())
        b = j.get("binds", {})
        for k in DEFAULT_BINDS:
            if isinstance(b.get(k), str):
                game.binds[k] = b[k]
        b2 = j.get("binds2", {})
        game.binds2 = {k: b2[k] for k in DEFAULT_BINDS if isinstance(b2.get(k), str) and b2[k]}
        game.sens = float(j.get("sens", BASE_SENS))
        game.hand_swap = bool(j.get("hand_swap", False))
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
        game.aspect = 0.0
        rs = j.get("res")
        if isinstance(rs, list) and len(rs) == 2:
            game.res = (max(160, min(7680, int(rs[0]))), max(120, min(4320, int(rs[1]))))
        menu.resw, menu.resh = str(game.res[0]), str(game.res[1])
        game.want_fullscreen = bool(j.get("fullscreen", False))
        game.show_fps = bool(j.get("show_fps", False))
        game.show_score = bool(j.get("show_score", True))
        try:
            game.fps_cap = max(30.0, min(300.0, float(j.get("fps_cap", 144.0))))
        except Exception:
            game.fps_cap = 144.0
        try:
            game.viewbob = max(0.0, min(2.0, float(j.get("viewbob", 0.5))))
        except Exception:
            game.viewbob = 0.5
        FX.day = bool(j.get("day", False))
        vo = j.get("vm_offset")
        if isinstance(vo, list) and len(vo) == 3:
            game.vm_adj = [max(-15.0, min(15.0, float(x))) for x in vo]
        game.audio.volume = float(j.get("volume", .25))
        try:
            pw = int(j.get("primary", game.primary))
            if 0 <= pw < len(CFG["weapons"]) and not CFG["weapons"][pw].melee:
                game.primary = pw
        except Exception:
            pass
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
        game.last_map_path = str(j.get("last_map", "") or "")
        # Profile picture comes from Steam only (Profile tab removed)
        pf = j.get("pfp")
        if pf and pf != "steam" and Path(str(pf)).is_file():
            pass  # ignore saved local pfp — Steam avatar is applied after load
    except Exception:
        pass


def _sdl_window():
    try:
        from pygame._sdl2.video import Window
        return Window.from_display_module()
    except Exception:
        return None


def set_fullscreen(game, on):
    """Borderless desktop fullscreen on/off without recreating the GL context (textures / models stay loaded)."""
    on = bool(on)
    if on == bool(game.fullscreen):
        return
    win = _sdl_window()
    if win is not None:
        try:
            if on:
                game._win_size = tuple(win.size)           # remember the window size to come back to
                win.set_fullscreen(desktop=True)           # SDL_WINDOW_FULLSCREEN_DESKTOP: keeps the GL context
            else:
                win.set_windowed()
                sz = getattr(game, "_win_size", None)
                if sz and sz[0] > 200 and sz[1] > 150:
                    win.size = sz
                try:
                    win.restore()
                except Exception:
                    pass
            game.fullscreen = on
            return
        except Exception as ex:
            print("Fullscreen (SDL2 window) failed:", ex)
    try:                                                   # fallback: pygame's own toggle
        pygame.display.toggle_fullscreen()
        game.fullscreen = on
    except Exception as ex:
        print("Fullscreen failed:", ex)


def save_settings(game, menu):
    try:
        SETTINGS_FILE.write_text(json.dumps({
            "arms": (game.arms_pick or ("" if game.arms_off else None)), "binds": game.binds, "binds2": game.binds2, "hand_swap": game.hand_swap, "player_models": game.pm_saved, "weapon_models": {str(k): v for k, v in game.wm_pick.items()}, "primary": game.primary, "day": bool(FX.day), "res": list(game.res), "fullscreen": bool(game.fullscreen), "show_fps": bool(game.show_fps), "show_score": bool(game.show_score), "fov": game.fov_cfg, "vm_offset": game.vm_adj, "fps_cap": float(game.fps_cap),
            "sens": game.sens, "volume": game.audio.volume, "name": menu.name, "pfp": menu.pfp_path, "crosshair": game.xh, "viewbob": float(game.viewbob), "last_map": getattr(game, "last_map_path", ""),
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
        "map": [("AKB-MOD map", "*.akbmap")],
        "arms": [("Arms / hands packs", "*.zip *.7z *.rar *.mdl *.vpk *.gma *.bin *.vpk.part *.vvd *.vtx *.vtf *.vmt")],
        "weapon": [("Weapon mods (L4D2 / Garry's Mod)", "*.mdl *.wav *.json *.vpk *.gma *.bin *.vpk.part *.zip *.7z *.rar *.vvd *.vtx *.vtf *.vmt")],
        "player": [("Player models", "*.mdl *.zip *.7z *.rar *.vpk *.gma *.bin *.vpk.part")],
        "image": [("Images", "*.png *.jpg *.jpeg *.bmp *.gif")],
    }[mode]
    try:
        paths = filedialog.askopenfilenames(title="Select files", filetypes=ft + [("All files", "*.*")])
    finally:
        root.destroy()
    return list(paths)

