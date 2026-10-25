"""Core game state, physics, weapons, bots, match logic."""
import hashlib
import io
import json
import math
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np
import pygame
from OpenGL import GL as gl

from .config import *
from .glutil import FX, make_texture
from .world import World, NoMapWorld, FadMapWorld
from .audio import Audio
from .entity import Ent
from .net import NET, Net, NET_STATE_HZ  # NET is the global instance
from .source.vpk import VpkArchive
from .source.gma import GmaArchive, is_gma_path
from .source.loaders import (
    LooseSrcFiles, DirSrcFiles, src_root, src_rel, _is_arms_mdl, _mdl_looks_like_arms,
    src_arms, src_arms_all, SrcSpec, SrcData, SrcInst, SrcPlayerInst, make_vm,
    extract_rar, is_src_player, src_players, src_scan,
)
from .source.model import VM_EVENT_GAIN, SHOTGUN_RELOAD_SCALE, PLAYER_SCALE, HIT_HALF_W, HIT_HALF_H, HIT_HALF_H_LOW, HEAD_FRAC, HEAD_MIN_R, HEAD_MAX_R
from .source.vtf import parse_kv
from .render import spray_pattern, HM_TIME, held_points

class Game:
    """All game state and logic. Sections: setup; developer console; online glue; physics and movement; combat; view-model playback and weapon animation sounds; mods; maps; per-frame update."""

    # ==========================================================================
    # setup
    # ==========================================================================

    def __init__(self, audio):
        self.audio = audio
        self.texdb = {}
        self.files = {"map_raw": None}
        self.asset = {"mdl": {}, "pack": {}, "wav": {}, "player": {}, "mdl_folder": {}}
        self.src_loose = LooseSrcFiles()
        self.src_dirs = {}
        self.src_arms = None      # SrcSpec of the arms model (hands holding the weapon)
        self.arms_all = {}        # name -> SrcSpec of every arms model loaded
        self.arms_pick = None     # name of the equipped arms
        self.arms_want = None     # name remembered from settings
        self.arms_off = False     # "No arms" chosen
        self.pm_cfg = {"CT": None, "T": None}
        self.primary = 1   # index in CFG["weapons"] of the one gun you carry (the knife is always with you)
        self.wm_pick = {}  # weapon slot index -> chosen view-model filename (Skins tab)
        self.manual_mods = True   # loaded mods are only listed in Skins; nothing is equipped until you pick it
        self.pm_saved = {"CT": None, "T": None}  # remembered team models (from settings)
        self.pm_cache = {}
        self._new_pl = 0
        self.vm_msg = ""
        self.world = None
        self.ents = []
        self.P = None
        self.score = [0, 0]
        self.feed = []
        self.tracers = []
        self.over = 0.0
        self.keys = {}
        self.binds = dict(DEFAULT_BINDS)
        self.binds2 = {}          # second key for any action (action -> key name); either key triggers the action
        self.held_raw = set()       # physical keys / buttons currently held
        self.hand_swap = False
        self.net = NET
        self.net.score_cb = lambda: self.score
        self.remotes = {}
        self.my_team = 0
        self.net_notice = ""
        self.net_joined = False
        self.res = (RENDER_W, RENDER_H)   # resolution the game renders at (typed in Settings > Video), stretched to the window
        self.aspect = 0.0  # 0 = match the window; else forced aspect (stretched to fill); 1.0 = 1:1
        self.fullscreen = False
        self.show_fps = False
        self.fps_cap = 144.0      # game FPS cap (Settings > Video); 300 = unlimited
        self.show_score = True    # sMove score system on/off (HUD + points)
        self.chat = []            # chat lines: {"tag", "text", "t"} (t = seconds left)
        self.fps_val = 0.0
        self.vm_adj = [0.0, 0.0, 0.0]  # viewmodel x (forward), y (right), z (up)
        self.sens = BASE_SENS
        self.lock = False
        self.flash = 0.0
        self.shake = 0.0         # screen shake strength 0..1 (view only, does not change where bullets go)
        self.fov = 75.0          # world camera FOV right now (slider, 20 when scoped)
        self.fov_cfg = 75.0      # FOV slider in Settings > Video (50..110), world only
        self.vm_fov = 75.0       # viewmodel FOV: never follows the slider
        self.death_killer = "UNKNOWN"
        self.player_name = "Player"
        self.streak = 0
        # sMove score system: held trick points are banked (plus the kill bonus) when you kill
        self.sc_total = 0        # banked score
        self.sc_held = 0         # trick points not banked yet; lost when the timer runs out
        self.sc_timer = 0.0      # seconds left to keep the combo alive (SM_TIMER)
        self.sc_trick = ""       # last trick label ("bhop x3", "jb", "eb", "px 0.42s")
        self.sc_chain = 0        # bunnyhops chained so far
        self.sc_jvel = 0         # speed at your last chained bhop take-off
        self.sc_flash_score = 0.0   # banked score turns cyan for a moment when it changes
        self.sc_flash_ind = 0.0     # trick label / velocity turn cyan on every trick
        self.sc_kill_t = 0.0        # "+N | Killed X" notice timer
        self.sc_kill_pts = 0
        self.sc_kill_name = ""
        self.sc_px_t0 = None        # time the current pixelsurf started
        self.hm = 0.0            # hit marker timer (CoD style X at the crosshair)
        self.hm_kill = False     # red marker when the hit was a kill
        self.xh = dict(DEFAULT_XH)   # crosshair settings
        self.vmA = {"vm": None, "w": -1, "s": 0, "t": 0.0, "loop": True}
        self.quit_req = False        # set by the quit console command
        self.show_hitboxes = False   # sv_showhitboxes: draw player collision / bullet AABBs
        self.thirdperson = False     # thirdperson console command: camera behind your player model
        self.viewbob = 0.5           # viewmodel mouse-lag / sway strength 0..2 (0.5 = default)
        self._vm_lyaw = None         # last frame yaw (for mouse lag)
        self._vm_lpitch = None
        self.vm_lag_y = 0.0          # current lag offset: + = gun shifts right
        self.vm_lag_p = 0.0          # + = gun shifts up
        # CS:GO-style developer console (~)
        self.con_open = False
        self.con_text = ""
        self.con_hist = []       # command history
        self.con_hist_i = -1
        self.con_log = []        # recent output lines [{"t": str, "c": (r,g,b)}]
        self.con_scroll = 0

    def set_world(self, w):
        self.world = w
        self.reset_game()

    def mk_ent(self, team, is_p, i=0):
        if is_p:
            name = self.player_name or "Player"
        else:
            name = ("Yellow" if team else "Black") + "-" + str(i)
        e = Ent(team, is_p, name)
        if not is_p:
            self.build_entity_model(e)
        return e

    def build_entity_model(self, e, force=False):
        e.pm = None
        if e.isP and not force:
            return
        name = self.pm_cfg["CT"] if e.team == 0 else self.pm_cfg["T"]
        if name and name in self.asset["player"]:
            try:
                spec = self.asset["player"][name]
                if not isinstance(spec, SrcSpec):
                    print("Player model skipped (only Source models supported):", name)
                    return
                pm = SrcPlayerInst(spec.load())
                pm.last = None
                e.pm_dz = -(self.world.feet_offset + pm.min_z())
                e.pm = pm
                e.anim = fresh_anim()
            except Exception as ex:
                print("Player model error:", ex)
                e.pm = None

    def spawn(self, e):
        home = getattr(e, "home", None)
        if home is not None and not e.isP and not e.remote:
            s = (home[0], home[1], home[2], math.degrees(home[3]))      # a bot added by hand respawns where it was added
        else:
            home = None
            lst = self.world.spawns[e.team]
            s = random.choice(lst)
        e.x, e.y, e.z = s[0], s[1], s[2]
        n = 0
        while self.world.solid(e.x, e.y, e.z) and n < 80:
            e.z += 4
            n += 1
        e.vx = e.vy = e.vz = 0.0
        e.px, e.py, e.pz = e.x, e.y, e.z
        e.acc = 0.0
        e.jreq = 0
        e.sl = 0.0
        e.scd = 0.0
        e.so = 0.0
        e.cr = 0.0
        e.hp = 200 if e.isP else 100   # the player secretly has double health (HUD still shows 100)
        e.regen_delay = 5.0
        e.alive = 1
        e.yaw = -math.pi / 2 if e.team else math.pi / 2
        if len(s) > 3:
            e.yaw = math.radians(s[3])     # spawn point facing from the map builder
        e.pitch = 0.0
        e.ammo = [w.mag for w in CFG["weapons"]]
        e.w = self.primary if e.isP else (home[4] if home is not None else 1)
        e.cool = 0.0
        e.zoom = 0
        e.pun_p = e.pun_y = 0.0
        e.spray = 0.0
        e.pend_p = e.pend_y = 0.0
        e.sprayw = False
        if e.isP:
            self.vmA["vm"] = None   # restart the viewmodel (draw animation) on every spawn
        e.anim = fresh_anim()
        e.shootT = 0.0
        e.reloadT = 0.0
        e.edgebug = False
        e.jumpbug = False
        e.was_walk = False
        e.land_ticks = 99
        e.jc_used = 0.0
        e.pxsurf = False
        e.ladder = None
        e.jb_tick = -999
        e.eb_cd = 0.0
        e._tick = 0

    def reset_game(self):
        self.ents = []
        online = self.net.active
        self.P = self.mk_ent(self.my_team if online else 0, True)
        self.ents.append(self.P)
        self.remotes = {}
        if online:
            for pid, p in self.net.players.items():
                self.mk_remote(pid, p)
        for e in self.ents:
            self.spawn(e)
        self.score = [0, 0]
        self.feed = []
        self.over = 0.0
        self.streak = 0

    def chat_add(self, text, tag="JUMP", life=9.0):
        self.chat.append({"tag": tag, "text": text, "t": life})
        self.chat = self.chat[-6:]

    def spatial(self, x, y, z, base):
        """3D sound: (gain, pan) for a sound at (x, y, z) as heard by the local player.
        Louder when close, fading to silence at ~2600 units; pan follows the player's view."""
        P = self.P
        dx, dy, dz = x - P.x, y - P.y, z - (P.z + 17)
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        att = (1.0 - min(dist, 2600.0) / 2600.0) ** 2
        hd = math.hypot(dx, dy)
        lat = (dx * math.sin(P.yaw) - dy * math.cos(P.yaw)) / max(hd, 1.0)  # +1 = right of you
        return base * att, lat * min(1.0, hd / 120.0)

    # ==========================================================================
    # developer console (CS:GO style)
    # ==========================================================================

    def con_print(self, msg, col=(200, 200, 200)):
        """Append a line to the console log (keeps last ~80)."""
        self.con_log.append({"t": str(msg), "c": col})
        if len(self.con_log) > 80:
            self.con_log = self.con_log[-80:]
        self.con_scroll = 0

    def con_toggle(self):
        if self.net.active and not self.con_open:
            self.chat_add("The console is disabled in online games.", tag="CONSOLE", life=4.0)
            return
        self.con_open = not self.con_open
        self.con_text = ""
        self.con_hist_i = -1
        if self.con_open:
            pygame.event.set_grab(False)
            pygame.mouse.set_visible(True)
            try:
                pygame.mouse.set_relative_mode(False)
            except Exception:
                pass
        elif self.lock:
            pygame.event.set_grab(True)
            pygame.mouse.set_visible(False)
            try:
                pygame.mouse.set_relative_mode(True)
            except Exception:
                pass
            pygame.mouse.get_rel()

    def _con_next_bot_name(self, team):
        used = {e.name for e in self.ents}
        for n in BOT_NAMES:
            nm = "Bot " + n
            if nm not in used:
                return nm
        return ("Yellow" if team else "Black") + "-" + str(len(self.ents))

    def con_add_bot(self, team=None):
        """Spawn a dummy bot (stands still, no AI) in front of you. team 0=BUM/Black, 1=FAD/Yellow, None=auto balance."""
        if self.net.active:
            self.con_print("Cannot add bots while online.", (255, 180, 80))
            return None
        if team is None:
            n0 = sum(1 for e in self.ents if not e.isP and e.team == 0)
            n1 = sum(1 for e in self.ents if not e.isP and e.team == 1)
            team = 1 if n1 <= n0 else 0
        team = int(team) & 1
        name = self._con_next_bot_name(team)
        e = Ent(team, False, name)
        self.build_entity_model(e)
        if self.P:
            # place a few units in front of the player, same yaw
            P = self.P
            fwd = 48.0
            e.x = P.x + math.cos(P.yaw) * fwd
            e.y = P.y + math.sin(P.yaw) * fwd
            e.z = P.z
            e.yaw = P.yaw + math.pi   # face the player
            e.pitch = 0.0
            # drop onto ground
            for _ in range(40):
                if self.world.solid(e.x, e.y, e.z - 1):
                    break
                e.z -= 2
            e.alive = 1
            e.hp = 100
            e.ammo = [w.mag for w in CFG["weapons"]]
            e.w = self.primary if not CFG["weapons"][self.primary].melee else 0
            e.anim = fresh_anim()
            e.home = (e.x, e.y, e.z, e.yaw, e.w)     # respawn here (not at the map spawn points)
        else:
            self.spawn(e)
        self.ents.append(e)
        side = TEAM_NAMES[1 if team else 0]
        self.con_print("Added bot '%s' (%s)" % (name, side), (157, 211, 103))
        return e

    def con_kick_bots(self, all_bots=True, name=None):
        if self.net.active:
            self.con_print("Cannot kick bots while online.", (255, 180, 80))
            return
        removed = []
        for e in list(self.ents):
            if e.isP or e.remote:
                continue
            if name is not None and e.name.lower() != name.lower():
                continue
            self.ents.remove(e)
            removed.append(e.name)
        if removed:
            self.con_print("Kicked %d bot(s): %s" % (len(removed), ", ".join(removed[:8])), (66, 165, 245))
        else:
            self.con_print("No bots to kick.", (180, 180, 180))

    def con_run(self, line):
        """Parse and execute a console command. Returns nothing; prints to con_log."""
        line = (line or "").strip()
        if not line:
            return
        if self.net.active:          # no console commands (god, noclip, bots ...) in online games
            self.con_print("The console is disabled in online games.", (255, 100, 100))
            return
        self.con_hist.append(line)
        if len(self.con_hist) > 40:
            self.con_hist = self.con_hist[-40:]
        self.con_hist_i = -1
        self.con_print("] " + line, (255, 255, 255))
        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]

        def arg(i, default=None):
            return args[i] if i < len(args) else default

        if cmd in ("help", "commands", "?"):
            help_lines = [
                "=== Dev console (offline) ===",
                "bot_add [ct|t]          spawn a bot in front of you (it stands still)",
                "bot_add_ct / bot_add_t  spawn a bot for BUM / FAD",
                "bot_kick [all|name]     kick bots (default: all)",
                "bot_kick_all            kick every bot",
                "kill / killme           suicide",
                "bot_kill                kill all bots (they respawn)",
                "restart / mp_restartgame  restart the match",
                "god                     toggle god mode (no damage)",
                "noclip                  toggle noclip fly",
                "clear                   clear console log",
                "fps / cl_showfps 0/1    toggle FPS counter",
                "cl_viewbob <0-2>        viewmodel lag when moving mouse",
                "sv_showhitboxes 0/1     draw collision (cyan) + bullet (yellow) + pistol head (red) boxes",
                "quit / exit             close the game",
                "day / night             toggle day/night atmosphere",
                "thirdperson [0/1]       camera behind your player (firstperson to go back)",
                "",
                "Player collision: bodies block each other (no pushing/tossing).",
            ]
            for h in help_lines:
                self.con_print(h, (180, 200, 220))
        elif cmd == "clear":
            self.con_log.clear()
        elif cmd in ("bot_add", "bot", "bot_place", "bot_static", "npc_create"):
            team = None
            a0 = (arg(0) or "").lower()
            if a0 in ("ct", "black", "0"):
                team = 0
            elif a0 in ("t", "yellow", "1"):
                team = 1
            self.con_add_bot(team)
        elif cmd in ("bot_add_ct", "bot_add_black"):
            self.con_add_bot(0)
        elif cmd in ("bot_add_t", "bot_add_yellow"):
            self.con_add_bot(1)
        elif cmd in ("bot_kick", "kick"):
            a0 = (arg(0) or "all").lower()
            if a0 in ("all", "*"):
                self.con_kick_bots(True)
            else:
                self.con_kick_bots(False, name=" ".join(args) if args else None)
        elif cmd in ("bot_kick_all", "kickall"):
            self.con_kick_bots(True)
        elif cmd in ("bot_kill", "bot_slay"):
            n = 0
            for e in self.ents:
                if not e.isP and not e.remote and e.alive:
                    e.hp = 0
                    self.kill(e, self.P if self.P else e)
                    n += 1
            self.con_print("Killed %d bot(s)." % n, (66, 165, 245))
        elif cmd in ("kill", "killme", "suicide"):
            if self.P and self.P.alive:
                self.P.hp = 0
                self.kill(self.P, self.P)
                self.con_print("You died.", (66, 165, 245))
        elif cmd in ("restart", "mp_restartgame", "retry"):
            self.reset_game()
            self.con_print("Match restarted.", (157, 211, 103))
        elif cmd == "god":
            self.P.god = not getattr(self.P, "god", False)
            self.con_print("god %s" % ("ON" if self.P.god else "OFF"), (66, 165, 245))
        elif cmd == "noclip":
            self.P.noclip = not getattr(self.P, "noclip", False)
            self.con_print("noclip %s" % ("ON" if self.P.noclip else "OFF"), (66, 165, 245))
        elif cmd in ("fps", "cl_showfps"):
            if args:
                self.show_fps = arg(0) not in ("0", "off", "false")
            else:
                self.show_fps = not self.show_fps
            self.con_print("FPS counter %s" % ("ON" if self.show_fps else "OFF"), (157, 211, 103))
        elif cmd in ("cl_viewbob", "viewbob", "cl_bob"):
            # viewmodel lag when moving the mouse: 0=off .. 2=strong
            if not args:
                self.con_print("cl_viewbob = %.2f  (gun lag on mouse look: 0=off, 0.5=default, 2=strong)" % self.viewbob, (200, 200, 200))
            else:
                a0 = arg(0).lower()
                if a0 in ("on", "true"):
                    self.viewbob = 1.0
                elif a0 in ("off", "false"):
                    self.viewbob = 0.0
                else:
                    try:
                        self.viewbob = max(0.0, min(2.0, float(a0)))
                    except ValueError:
                        self.con_print("Usage: cl_viewbob <0-2>", (255, 100, 100))
                        return
                self.con_print("cl_viewbob = %.2f  (mouse-look lag)" % self.viewbob, (157, 211, 103))
        elif cmd in ("quit", "exit", "disconnect_and_quit"):
            self.con_print("Quitting ...", (255, 180, 80))
            self.quit_req = True
        elif cmd in ("sv_showhitboxes", "hitboxes", "showhitboxes", "cl_showhitboxes", "hitbox", "showhitbox"):
            if args:
                self.show_hitboxes = arg(0) not in ("0", "off", "false")
            else:
                self.show_hitboxes = not self.show_hitboxes
            self.con_print("sv_showhitboxes %s  (cyan=body collision, yellow=body hitbox, red=head hitbox)"
                           % ("1" if self.show_hitboxes else "0"), (157, 211, 103))
        elif cmd in ("thirdperson", "cl_thirdperson", "tp", "firstperson"):
            if cmd == "firstperson":
                on = False
            elif args:
                on = arg(0) not in ("0", "off", "false")
            else:
                on = not self.thirdperson
            self.set_thirdperson(on)
            self.con_print("thirdperson %s" % ("ON" if on else "OFF"), (157, 211, 103))
        elif cmd == "day":
            FX.day = True
            self.con_print("day mode", (255, 255, 200))
        elif cmd == "night":
            FX.day = False
            self.con_print("night mode", (150, 150, 180))
        elif cmd in ("echo", "say"):
            self.con_print(" ".join(args), (200, 200, 200))
        else:
            self.con_print("Unknown command: %s  (type help)" % cmd, (255, 100, 100))

    # ==========================================================================
    # online glue (host / join / sync)
    # ==========================================================================

    def map_sig(self):
        b = self.files.get("map_raw")
        return hashlib.sha1(b).hexdigest()[:10] if b else "none"

    def mk_remote(self, pid, p):
        e = Ent(p["tm"], False, p["n"])
        e.remote = True
        e.nid = pid
        self.build_entity_model(e)
        self.spawn(e)
        self.ents.append(e)
        self.remotes[pid] = e
        return e

    def net_host(self, room):
        self.my_team = 0
        if not self.net.host(room, self.player_name, self.map_sig(), 0):
            return False
        self.reset_game()
        return True

    def net_join(self, room, rid=None):
        """room: a room-browser entry (dict), or a list of (ip, port) addresses to try."""
        self.join_msg = ""
        if isinstance(room, dict):
            ok, self.join_msg = self.use_map_sig(str(room.get("map", "none")))
            if not ok:
                return False
            return self.net.join_room(room, self.player_name, self.map_sig())
        return self.net.join(room, rid, self.player_name, self.map_sig())

    def net_set_team(self, tm):
        """Switch to team tm (0 = BUM, 1 = FAD) while on a server; everyone is told."""
        tm = int(tm) & 1
        if not self.net.active or not self.P or self.P.team == tm:
            return
        self.net.my_team = tm
        self.my_team = tm
        self.P.team = tm
        self.spawn(self.P)
        self.net.send({"t": "team", "tm": tm})

    def net_leave(self, status=None):
        self.net.leave(status)
        self.my_team = 0
        self.reset_game()

    def ent_by_id(self, pid):
        return self.P if pid == self.net.my_id else self.remotes.get(pid)

    def net_update(self, dt):
        net = self.net
        for m in net.poll(time.time()):
            try:
                self.net_msg(m)
            except Exception as ex:
                print("net message error:", ex)
        if net.active and self.P:
            net.t_st += dt
            if net.t_st >= 1.0 / (20 if net.relayed else NET_STATE_HZ):
                net.t_st = 0.0
                P = self.P
                net.send({"t": "st", "s": [
                    round(P.x, 1), round(P.y, 1), round(P.z, 1),
                    round(P.vx), round(P.vy), round(P.vz),
                    round(P.yaw, 3), round(P.pitch, 3), P.w, round(P.cr, 2),
                    1 if P.sl > 0 else 0, 1 if P.alive else 0, 1 if P.reloadT > 0 else 0]}, droppable=True)

    def net_msg(self, m):
        net = self.net
        t, f = m.get("t"), m.get("f")
        if t == "welcome":
            self.my_team = net.my_team
            self.reset_game()
            self.score = [int(x) for x in m.get("sc", [0, 0])][:2] or [0, 0]
            self.net_joined = True
        elif t in ("failed", "closed"):
            if t == "closed":
                self.reset_game()
            self.net_notice = net.status or m.get("why", "")
        elif t == "join":
            pid = int(m["id"])
            if pid not in self.remotes:
                self.mk_remote(pid, net.players.get(pid, {"n": m.get("n", "?"), "tm": m.get("tm", 0)}))
        elif t == "leave":
            e = self.remotes.pop(int(m.get("id", -1)), None)
            if e in self.ents:
                self.ents.remove(e)
        elif t == "st":
            e = self.remotes.get(f)
            s = m.get("s")
            if not e or not s or len(s) < 13:
                return
            x, y, z, vx, vy, vz, yaw, pt, w, cr, sl, al, rl = s[:13]
            if al and not e.alive:
                e.alive = 1
                e.hp = 100
                e.anim = fresh_anim()
                e.tx = None
            if not e.alive:
                return
            if e.tx is None or abs(x - e.x) > 300 or abs(y - e.y) > 300 or abs(z - e.z) > 300:
                e.x, e.y, e.z = x, y, z
                e.yaw = yaw
                e.pitch = pt
            e.tx, e.ty, e.tz = x, y, z
            e.vx, e.vy, e.vz = vx, vy, vz
            e.tyaw, e.tpitch = yaw, pt
            e.w = max(0, min(len(CFG["weapons"]) - 1, int(w)))
            e.cr = cr
            e.sl = 1.0 if sl else 0.0
            if rl and e.reloadT <= 0:
                e.reloadT = 1.6
        elif t == "shot":
            e = self.remotes.get(f)
            if not e or not e.alive:
                return
            e.w = max(0, min(len(CFG["weapons"]) - 1, int(m.get("w", e.w))))
            w = CFG["weapons"][e.w]
            o, b = m.get("o"), m.get("e")
            if not o or not b:
                return
            e.shootT = .15
            if not w.melee:
                self.tracers.append({"a": (o[0], o[1], o[2] - 2), "b": tuple(b), "al": .5, "t": .022})
            if not w.melee or w.snd:
                g, pan = self.spatial(o[0], o[1], o[2], .55)
                snd = w.snd
                if g > .004:
                    self.audio.play(g, snd, pan)
        elif t == "team":
            try:
                pid, tm = int(f), int(m.get("tm", 0)) & 1
            except (TypeError, ValueError):
                return
            if pid in net.players:
                net.players[pid]["tm"] = tm
            e = self.remotes.get(pid)
            if e and e.team != tm:
                e.team = tm
                self.build_entity_model(e)
                self.spawn(e)
        elif t == "hit":
            P = self.P
            if P.alive and not self.over and not getattr(P, "god", False):
                damage = max(0.0, float(m.get("d", 0)))
                P.hp -= damage
                if damage > 0:
                    P.regen_delay = 5.0
                    P.took_damage_this_frame = True
                if P.hp <= 0:
                    k = self.remotes.get(f) or P
                    ow = k.w
                    k.w = max(0, min(len(CFG["weapons"]) - 1, int(m.get("w", k.w))))
                    self.kill(P, k)
                    k.w = ow
        elif t == "kill":
            k, v = self.ent_by_id(m.get("k")), self.ent_by_id(m.get("v"))
            if k and v and v.alive:
                ow = k.w
                k.w = max(0, min(len(CFG["weapons"]) - 1, int(m.get("w", k.w))))
                self.kill(v, k)
                k.w = ow

    def remote_tick(self, e, dt):
        if not e.alive or e.tx is None:
            return
        k = min(1.0, dt * 16)
        e.x += (e.tx - e.x) * k
        e.y += (e.ty - e.y) * k
        e.z += (e.tz - e.z) * k
        if abs(e.tz - e.z) < 1.5:
            e.z = e.tz          # snap: never hover a hair above the floor
        e.yaw += math.atan2(math.sin(e.tyaw - e.yaw), math.cos(e.tyaw - e.yaw)) * min(1.0, dt * 20)
        e.pitch += (e.tpitch - e.pitch) * min(1.0, dt * 20)

    # ==========================================================================
    # physics and movement
    # ==========================================================================

    def airborne(self, e):
        """True when the entity really is in the air. Checks a few units below the feet (a hair above
        the floor still counts as standing) and, for online players, the position the network says
        they are at, not the smoothed one that trails behind it by a fraction of a unit."""
        solid = self.world.solid
        if e.remote and e.tx is not None:
            x, y, z = e.tx, e.ty, e.tz
        else:
            x, y, z = e.x, e.y, e.z
        return not (solid(x, y, z - 1) or solid(x, y, z - 3))

    def phys(self, e, dt, vx, vy, jump):
        world = self.world
        gr = world.solid(e.x, e.y, e.z - 1)
        bl = 0
        if jump and gr:
            e.vz = JUMPV
        elif not gr:
            e.vz -= SV_GRAVITY * dt
        elif e.vz < 0:
            e.vz = 0
        stick = gr and e.vz <= 0  # walking on the ground (not leaving it by jumping)
        nx = e.x + vx * dt
        if not world.solid(nx, e.y, e.z):
            e.x = nx
        else:
            h = self.step_h(nx, e.y, e.z) if stick else 0
            if h:
                e.x = nx
                e.z += h
            elif vx:
                bl = 1
        ny = e.y + vy * dt
        if not world.solid(e.x, ny, e.z):
            e.y = ny
        else:
            h = self.step_h(e.x, ny, e.z) if stick else 0
            if h:
                e.y = ny
                e.z += h
            elif vy:
                bl = 1
        if stick and not world.solid(e.x, e.y, e.z - 1):
            # walked off a slope / small drop: stay glued to the ground instead of
            # going airborne (which killed friction and made slopes bounce)
            for d in range(2, STEP_H + 1):
                if world.solid(e.x, e.y, e.z - d):
                    e.z -= d - 1
                    break
        nz = e.z + e.vz * dt
        if world.solid(e.x, e.y, nz):
            if e.vz < 0:
                z = e.z
                while z - 1 > nz and not world.solid(e.x, e.y, z - 1):
                    z -= 1
                e.z = z
            e.vz = 0
        else:
            e.z = nz
        return bl

    def step_h(self, x, y, z, fx=None, fy=None):
        """Smallest height (1..STEP_H) we must rise to stand at (x, y); 0 if blocked.
        Rising only as much as needed makes slopes and stairs smooth. Steep surfaces
        (too steep to walk) are never stepped onto - those you slide / surf on."""
        world = self.world
        solid = world.solid
        for h in range(1, STEP_H + 1):
            if fx is not None and solid(fx, fy, z + h):
                return 0  # ceiling above where we stand
            if not solid(x, y, z + h):
                n = world.normal_at(x, y, z + h, True)
                if n is not None and n[2] < SURF_NZ:
                    return 0
                return h
        return 0

    @staticmethod
    def clip_vel(v, n):
        b = v[0] * n[0] + v[1] * n[1] + v[2] * n[2]
        if b >= 0:
            return v
        o = (v[0] - n[0] * b, v[1] - n[1] * b, v[2] - n[2] * b)
        a = o[0] * n[0] + o[1] * n[1] + o[2] * n[2]
        if a < 0:
            o = (o[0] - n[0] * a, o[1] - n[1] * a, o[2] - n[2] * a)
        return o

    def floor_n(self, e):
        """Normal of the up-facing surface right under the feet, None when airborne."""
        world = self.world
        if not world.solid(e.x, e.y, e.z - 1):
            return None
        return world.normal_at(e.x, e.y, e.z, True) or (0.0, 0.0, 1.0)

    def walkable(self, e):
        """On ground gentle enough to walk on (steeper surfaces count as air: you surf).
        Also true when standing on another player's head."""
        if e.vz > 0:
            return False
        n = self.floor_n(e)
        if n is not None and n[2] >= SURF_NZ:
            return True
        # standing on another living entity's head?
        # use standing half-height for the feet test so crouch/uncrouch doesn't drop walkable
        ra, _ = self.ent_hull(e)
        ha_stand = HIT_HALF_H * (PLAYER_SCALE if not e.isP else 1.0)
        feet = e.z - ha_stand
        # also accept crouched feet (higher) so crouching on a head still counts
        _, ha_now = self.ent_hull(e)
        feet_now = e.z - ha_now
        for o in self.ents:
            if o is e or not o.alive or o.remote:
                continue
            rb, hb = self.ent_hull(o)
            head = o.z + hb
            near = abs(feet - head) <= 18 or abs(feet_now - head) <= 18
            if not near:
                continue
            if math.hypot(e.x - o.x, e.y - o.y) <= (ra + rb) * 1.05:
                return True
        return False

    def ent_hull(self, e):
        """Horizontal radius and vertical half-height of the player collision hull (game units)."""
        scale = PLAYER_SCALE if not e.isP else 1.0
        sl = (getattr(e, "sl", 0) or 0) > 0 or (getattr(e, "cr", 0) or 0) > 0.5
        hh = (HIT_HALF_H_LOW if sl else HIT_HALF_H) * scale
        rw = HIT_HALF_W * scale * 1.15   # a bit wider than the bullet hitbox so bodies block
        return rw, hh

    def resolve_player_collisions(self, dt):
        """Solid player hulls: bodies block each other (no shove), and you can stand on heads.
        Crouch / uncrouch keeps feet planted on a head instead of shoving you sideways."""
        alive = [e for e in self.ents if e.alive and not e.remote]
        n = len(alive)
        for i in range(n):
            a = alive[i]
            ra, ha = self.ent_hull(a)
            for j in range(i + 1, n):
                b = alive[j]
                rb, hb = self.ent_hull(b)
                dx = b.x - a.x
                dy = b.y - a.y
                dist = math.hypot(dx, dy)
                min_d = ra + rb
                dxy_ok = dist < min_d * 1.08 or dist < 1e-4

                def try_stand(upper, lower, uh, lh):
                    """Snap upper onto lower's head. Generous Z so crouch height changes stay on top."""
                    if not dxy_ok and math.hypot(upper.x - lower.x, upper.y - lower.y) > (self.ent_hull(upper)[0] + self.ent_hull(lower)[0]) * 1.05:
                        return False
                    head = lower.z + lh
                    # feet using current hull
                    feet = upper.z - uh
                    # also check as if standing full height (uncrouch expands downward)
                    uh_stand = HIT_HALF_H * (PLAYER_SCALE if not upper.isP else 1.0)
                    feet_stand = upper.z - uh_stand
                    # accept a wide band: any feet near the head counts as "on top"
                    on = (head - 16 <= feet <= head + 20) or (head - 16 <= feet_stand <= head + 20)
                    if not on:
                        return False
                    if upper.vz > 60:
                        return False
                    # keep centre so feet sit just on the head (use current half-height)
                    upper.z = head + uh + 0.5
                    upper.vz = 0.0
                    upper.walk = True
                    return True

                stacked = False
                if try_stand(a, b, ha, hb):
                    stacked = True
                elif try_stand(b, a, hb, ha):
                    stacked = True
                if stacked:
                    continue

                # if one is clearly above the other, never apply horizontal push
                # (stops crouch/uncrouch from ejecting you off a head)
                if a.z - ha >= b.z + hb - 20 or b.z - hb >= a.z + ha - 20:
                    continue

                za0, za1 = a.z - ha, a.z + ha
                zb0, zb1 = b.z - hb, b.z + hb
                if za1 < zb0 or zb1 < za0:
                    continue
                if dist >= min_d or dist < 1e-4:
                    continue
                nx, ny = dx / dist, dy / dist
                overlap = min_d - dist
                if a.isP and not b.isP:
                    a.x -= nx * overlap
                    a.y -= ny * overlap
                    vn = a.vx * nx + a.vy * ny
                    if vn > 0:
                        a.vx -= nx * vn
                        a.vy -= ny * vn
                elif b.isP and not a.isP:
                    b.x += nx * overlap
                    b.y += ny * overlap
                    vn = b.vx * (-nx) + b.vy * (-ny)
                    if vn > 0:
                        b.vx -= (-nx) * vn
                        b.vy -= (-ny) * vn
                else:
                    a.x -= nx * overlap * 0.5
                    a.y -= ny * overlap * 0.5
                    b.x += nx * overlap * 0.5
                    b.y += ny * overlap * 0.5


    # ==========================================================================
    # sMove movement tech (1:1 from sm_movement.lua)
    # ==========================================================================

    def _trace_down(self, e, dist):
        """True if solid within `dist` units below feet (hull-style point sample)."""
        solid = self.world.solid
        x, y, z = e.x, e.y, e.z
        steps = max(2, int(dist))
        for i in range(1, steps + 1):
            if solid(x, y, z - i):
                return True
        return False

    def _gap_ahead(self, e, ahead, down):
        """True if there is NO ground ahead (sMove edgebug check: ahead trace misses)."""
        solid = self.world.solid
        spd = math.hypot(e.vx, e.vy)
        if spd < 5:
            return False
        fx, fy = e.vx / spd, e.vy / spd
        ax, ay = e.x + fx * ahead, e.y + fy * ahead
        for i in range(1, int(down) + 1):
            if solid(ax, ay, e.z - i):
                return False
        return True

    def _wall_dir(self, e, dist=None):
        """Unit horizontal direction pointing INTO the wall(s) within `dist` of you, or None.
        Samples sit above the centre, clear of the floor and of walkable steps (e.z is the body
        centre), so only a real wall ever counts."""
        dist = PX_WALL_DIST if dist is None else dist
        solid = self.world.solid
        sx = sy = 0.0
        for k in range(8):
            a = k * math.pi / 4
            ca, sa = math.cos(a), math.sin(a)
            for dz in (20, 30):
                if solid(e.x + ca * dist, e.y + sa * dist, e.z + dz):
                    sx += ca
                    sy += sa
                    break
        n = math.hypot(sx, sy)
        if n < 1e-3:
            return None
        return sx / n, sy / n

    def _near_wall(self, e, dist=None):
        return self._wall_dir(e, dist) is not None

    def ladder_tick(self, e, f, s, jump, wx, wy):
        """Source / CS:GO ladder movement (CGameMovement::LadderMove).
        Grab: moving toward a ladder (hull moved 2 units along the wish direction touches it) while holding a move key.
        On it: no gravity, no friction; the keys give +-156 u/s each in the VIEW direction, split along the ladder:
        the part going into the ladder turns into climbing (so W looks-up = climb, W looking steeply down = descend),
        the rest slides you sideways along it. Jump = leap off at 270 u/s along the ladder normal.
        Leaving the top / bottom of the zone just drops you back into normal movement with the speed you had.
        Returns 0 = not on a ladder, 1 = climbing (velocity set), 2 = jumped off (velocity set)"""
        world = self.world
        if not getattr(world, "LAD", None):
            e.ladder = None
            return 0
        lad = e.ladder
        if lad is not None:                          # already on it: keep using the ladder's own direction
            n = world.ladder_at(e.x, e.y, e.z, -lad[0], -lad[1], LADDER_REACH)
        elif f or s:                                 # otherwise: the direction we are trying to move
            n = world.ladder_at(e.x, e.y, e.z, wx, wy, LADDER_REACH)
        else:
            return 0                                 # not trying to move: no ladder behaviour
        if n is None:
            e.ladder = None
            return 0
        e.ladder = n
        if jump:                                     # leap off: velocity = normal * 270, gravity back on
            e.ladder = None
            e.vx, e.vy, e.vz = n[0] * LADDER_JUMP, n[1] * LADDER_JUMP, 0.0
            return 2
        sp = LADDER_SPEED * LADDER_SCALE
        if f or s:
            c, sn = math.cos(e.yaw), math.sin(e.yaw)
            cp, sp_ = math.cos(e.pitch), math.sin(e.pitch)
            fw, rt = f * sp, s * sp                  # forward / right speed from the keys (not normalised, like Source)
            vx = c * cp * fw + sn * rt               # velocity wanted, along the full 3D view direction
            vy = sn * cp * fw - c * rt
            vz = sp_ * fw
            nrm = vx * n[0] + vy * n[1]              # part pushing into (-) / away from (+) the ladder
            vx -= n[0] * nrm                         # lateral: what is left in the ladder plane ...
            vy -= n[1] * nrm
            vz -= nrm                                # ... plus the push into the ladder turned into "up"
            if nrm > 0 and world.solid(e.x, e.y, e.z - 1):
                vx += n[0] * sp                      # on the floor and walking away from it: step off
                vy += n[1] * sp
            e.vx, e.vy, e.vz = vx, vy, vz
        else:
            e.vx = e.vy = e.vz = 0.0                 # hang on the ladder
        return 1

    def smove_jumpbug(self, e, jump):
        """smFakeJB: airborne, near ground (10u), jump held -> vz = 300."""
        if not jump:
            return False
        if e.walk:
            return False
        last = getattr(e, "jb_tick", -999)
        # simple tick counter via land_ticks style
        tick = getattr(e, "_tick", 0)
        if tick < last + JB_COOLDOWN_TICKS:
            return False
        if not self._trace_down(e, JB_TRACE):
            return False
        e.vz = JB_VZ
        e.jb_tick = tick
        e.jumpbug = True
        return True

    def smove_edgebug(self, e):
        """smFakeEB: airborne, near floor, gap ahead -> damp fall + forward boost."""
        if e.walk:
            return False
        now = time.time()
        if now < getattr(e, "eb_cd", 0.0):
            return False
        spd = math.hypot(e.vx, e.vy)
        if spd < 5:
            return False
        if not self._trace_down(e, EB_TRACE_DOWN):
            return False
        if not self._gap_ahead(e, EB_AHEAD, EB_AHEAD_DOWN):
            return False
        e.eb_cd = now + EB_COOLDOWN
        if e.vz < 0:
            e.vz *= EB_VZ_MUL
        fx, fy = e.vx / spd, e.vy / spd
        e.vx += fx * EB_BOOST
        e.vy += fy * EB_BOOST
        e.edgebug = True
        return True

    def smove_pixelsurf(self, e, yaw, wish=None):
        """smFakePX: airborne + near wall -> project velocity onto look forward, vz=0."""
        if e.walk:
            e.pxsurf = False
            return False
        if not self.world.in_px_zone(e.x, e.y, e.z):      # only inside a pixel surf zone (map builder tool 8)
            e.pxsurf = False
            return False
        wd = self._wall_dir(e)
        if wd is None:
            e.pxsurf = False
            return False
        # you must be holding a movement key into the wall for pixel surf to work
        if wish is None or (wish[0] * wd[0] + wish[1] * wd[1]) < 0.3:
            e.pxsurf = False
            return False
        if abs(e.vx) + abs(e.vy) <= 0:
            e.pxsurf = False
            return False
        # Pixel surf keeps your speed: no gravity, and the view only steers the direction
        # (it never scales the speed down). Looking away from your motion just keeps your heading.
        c, s = math.cos(yaw), math.sin(yaw)
        hs = math.hypot(e.vx, e.vy)
        if e.vx * c + e.vy * s > 0:
            e.vx = c * hs
            e.vy = s * hs
        e.vz = 0.0
        e.pxsurf = True
        e.edgebug = False
        return True

    def pm_move(self, e, dt):
        """Source-style player move: gravity already applied in caller, then swept collision
        that slides along blocking planes. Gentle slopes/stairs are stepped; steep surfaces
        (surf) keep full clipped velocity for pixel surfing. Edgebug: vertical velocity is
        cancelled by a plane while still airborne — horizontal speed is preserved."""
        world = self.world
        solid = world.solid
        walk = e.walk
        e.steep = False
        e.edgebug = False
        prev_vz = e.vz
        if walk:
            e.jc_used = 0.0
            e.land_ticks = 0
            if e.vz < 0:
                e.vz = 0.0
        else:
            e.land_ticks = min(999, getattr(e, "land_ticks", 99) + 1)
            if not getattr(e, "pxsurf", False) and not getattr(e, "ladder", None):   # pixel surfing / ladder: no gravity
                e.vz -= SV_GRAVITY * dt
        ov = (e.vx, e.vy, e.vz)
        sp = math.sqrt(ov[0] * ov[0] + ov[1] * ov[1] + ov[2] * ov[2])
        if sp > MAXV:  # sv_maxvelocity
            k = MAXV / sp
            e.vx, e.vy, e.vz = ov = (ov[0] * k, ov[1] * k, ov[2] * k)
        planes = []
        hit_any = False
        t = dt
        for _ in range(4):
            if t <= 1e-7:
                break
            if solid(e.x, e.y, e.z):  # stuck: pop up
                for h in range(1, 129):
                    if not solid(e.x, e.y, e.z + h):
                        e.z += h
                        break
                else:
                    e.vx = e.vy = e.vz = 0.0
                    break
            dx, dy, dz = e.vx * t, e.vy * t, e.vz * t
            if dx == 0 and dy == 0 and dz == 0:
                break
            nx, ny, nz = e.x + dx, e.y + dy, e.z + dz
            if not solid(nx, ny, nz):
                e.x, e.y, e.z = nx, ny, nz
                break
            hit_any = True
            lo, hi = 0.0, 1.0
            for _i in range(10):
                mid = (lo + hi) * .5
                if solid(e.x + dx * mid, e.y + dy * mid, e.z + dz * mid):
                    hi = mid
                else:
                    lo = mid
            e.x += dx * lo
            e.y += dy * lo
            e.z += dz * lo
            t *= 1 - lo
            if walk:  # stairs / gentle slopes
                tx, ty = e.x + e.vx * t, e.y + e.vy * t
                h = self.step_h(tx, ty, e.z, e.x, e.y)
                if h:
                    e.x, e.y, e.z = tx, ty, e.z + h
                    break
            elif getattr(e, "jc", False):
                # jump-crouch: crouch in air pulls feet up (ledge mount)
                used = getattr(e, "jc_used", 0.0)
                # floor right under the feet = a landing, not a ledge: never "mount" it
                # (that kept you hovering above the floor, with no friction, after crouch-landing)
                _on_floor = e.vz <= 0 and (solid(e.x, e.y, e.z - 1) or solid(e.x, e.y, e.z - 3))
                if used < STEP_H and not _on_floor:
                    tx, ty = e.x + e.vx * t, e.y + e.vy * t
                    h = self.step_h(tx, ty, e.z, e.x, e.y)
                    if h and used + h <= STEP_H:
                        e.x, e.y, e.z = tx, ty, e.z + h
                        e.jc_used = used + h
                        break
            n = world.normal_at(e.x, e.y, e.z, False, (dx, dy, dz))
            if n is None:
                e.vx = e.vy = e.vz = 0.0
                break
            if walk and 0 < n[2] < SURF_NZ:
                # ground: steep slope acts like a wall (slide along base, never up)
                hl = math.hypot(n[0], n[1])
                if hl > 1e-6:
                    n = (n[0] / hl, n[1] / hl, 0.0)
            planes.append(n)
            v = None
            for i, pl in enumerate(planes):
                v2 = self.clip_vel(ov, pl)
                if all(v2[0] * q[0] + v2[1] * q[1] + v2[2] * q[2] >= -.01
                       for j, q in enumerate(planes) if j != i):
                    v = v2
                    break
            if v is None:
                if len(planes) == 2:  # crease: slide along intersection
                    a, b = planes
                    d = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
                    l = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
                    if l > 1e-6:
                        k = (d[0] * ov[0] + d[1] * ov[1] + d[2] * ov[2]) / (l * l)
                        v = (d[0] * k, d[1] * k, d[2] * k)
                if v is None:
                    v = (0.0, 0.0, 0.0)
                    e.vx = e.vy = e.vz = 0.0
                    break
            # CS:GO surf / pixel surf: keep full clipped velocity on non-walkable slopes
            if not walk and len(planes) >= 1:
                for pl in planes:
                    if .02 < pl[2] < SURF_NZ:
                        e.steep = True
                        break
            if v[0] * ov[0] + v[1] * ov[1] + v[2] * ov[2] <= 0:
                e.vx = e.vy = e.vz = 0.0
                break
            e.vx, e.vy, e.vz = v

        # --- Edgebug (CS:GO / KZ) ---
        # Falling, hit a surface that cancelled vertical speed, but not on walkable ground:
        # horizontal velocity is kept, vz ~ 0, you stay airborne.
        if (not walk and hit_any and prev_vz < -EDGEBUG_VZ
                and abs(e.vz) < EDGEBUG_VZ and not self.walkable(e)):
            e.vz = 0.0
            e.edgebug = True
            # optional tiny height stick so you don't immediately re-intersect
            if solid(e.x, e.y, e.z - 0.5) and not solid(e.x, e.y, e.z + 1.0):
                e.z += 0.25

        if STICK_TO_GROUND and walk and e.vz <= 0 and not solid(e.x, e.y, e.z - 1):
            for d in range(2, STEP_H + 1):
                if solid(e.x, e.y, e.z - d):
                    zz = e.z - (d - 1)
                    n = world.normal_at(e.x, e.y, zz, True)
                    if n is None or n[2] >= SURF_NZ:
                        e.z = zz
                    break

    @staticmethod
    def accel(e, wx, wy, ws, a, dt, cap):
        """Source sv_accelerate / sv_airaccelerate."""
        if not wx and not wy:
            return
        wsp = min(ws, cap)
        add = wsp - (e.vx * wx + e.vy * wy)
        if add <= 0:
            return
        as_ = a * ws * dt
        if as_ > add:
            as_ = add
        e.vx += as_ * wx
        e.vy += as_ * wy

    def pmove(self, e, dt, wx, wy, jump):
        """Ground/air accelerate + jump (sMove: jump power 275, no bhop speed clamp)."""
        gr = e.walk
        e.jumpbug = False
        if jump and gr:
            e.vz = JUMPV
            gr = False
            e.walk = False
            # CS:GO bunnyhop prevention: horizontal speed capped at 1.1x max speed on take-off
            if BHCAP:
                hs = math.hypot(e.vx, e.vy)
                lim = MAXS * BHCAP
                if hs > lim:
                    e.vx *= lim / hs
                    e.vy *= lim / hs
            if e.isP:
                self.sc_on_jump(e)
        if gr:
            sp = math.hypot(e.vx, e.vy)
            if sp > .1:
                ns = max(0.0, sp - max(sp, STOPS) * FRIC * dt) / sp
                e.vx *= ns
                e.vy *= ns
            else:
                e.vx = e.vy = 0.0
            self.accel(e, wx, wy, MAXS, ACC, dt, MAXS)
        else:
            # CS:GO: sv_airaccelerate 12, wish-speed capped at 30 while airborne
            self.accel(e, wx, wy, MAXS, AIRACC, dt, AIRCAP)

    def ptick(self, e, dt, f, s, n, c, sn):
        world = self.world
        e.was_walk = e.walk
        e.walk = self.walkable(e)
        # Jump: key press OR holding the key (sMove autohop - hold to keep bunny hopping)
        jump = e.jreq
        e.jreq = 0
        if e.isP and self.keys.get(self.binds["jump"]):
            jump = 1
        e.sl = 0.0   # slide removed
        mv = f or s
        wx = (c * f + sn * s) / n if mv else 0
        wy = (sn * f - c * s) / n if mv else 0
        # ladders first (Source: MOVETYPE_LADDER replaces the walk move: no friction, no acceleration, no gravity)
        lad = self.ladder_tick(e, f, s, jump, wx, wy)
        if lad:
            e.walk = False
            e.jumpbug = e.edgebug = e.pxsurf = False
            self._px_snd = 0.0
            self.pm_move(e, dt)
            e._tick = getattr(e, "_tick", 0) + 1
            return
        jmp = jump
        # sMove autohop: while holding jump, only apply jump impulse on ground
        # (jump button is cleared mid-air so the next land can re-trigger)
        if jump and not e.walk:
            jmp = 0

        # crouch held on the ground: cap speed BEFORE the jump/friction, so landing with
        # crouch (or hopping while holding crouch) can't carry speed and slide along
        crouch_held = e.cr > .5 or (e.isP and bool(self.keys.get(self.binds["crouch"])))
        if crouch_held and e.walk:
            _fn0 = self.floor_n(e)
            if _fn0 is None or _fn0[2] >= SURF_NZ:
                sp = math.hypot(e.vx, e.vy)
                cap = MAXS * DUCKSPD
                if sp > cap:
                    e.vx *= cap / sp
                    e.vy *= cap / sp
        self.pmove(e, dt, wx, wy, jmp)
        _fn = self.floor_n(e) if e.cr > .5 else None
        _surf = _fn is not None and _fn[2] < SURF_NZ
        if e.cr > .5 and not _surf and world.solid(e.x, e.y, e.z - 1):
            sp = math.hypot(e.vx, e.vy)
            cap = MAXS * DUCKSPD
            if sp > cap:
                e.vx *= cap / sp
                e.vy *= cap / sp
        _air_pre = not e.walk                  # in the air before this move (fall damage)
        _vz_pre = e.vz
        self.pm_move(e, dt)
        # crouch held and on the floor after the move (also the landing tick): no fast floor running
        if e.isP and self.keys.get(self.binds["crouch"]) and e.vz <= 0 and self.walkable(e):
            _fn1 = self.floor_n(e)
            if _fn1 is None or _fn1[2] >= SURF_NZ:
                sp = math.hypot(e.vx, e.vy)
                cap = MAXS * DUCKSPD
                if sp > cap:
                    e.vx *= cap / sp
                    e.vy *= cap / sp

        # --- sMove tech (smFakeJB / smFakeEB / smFakePX) ---
        e._tick = getattr(e, "_tick", 0) + 1
        e.edgebug = False
        e.pxsurf = False
        K, B = self.keys, self.binds
        want_px = bool(e.isP and K.get(B["pixelsurf"]) and K.get(B["crouch"]))   # needs crouch held too
        want_jb = bool(e.isP and K.get(B["jumpbug"]))
        want_eb = bool(e.isP and K.get(B["edgebug"]))
        # sMove tech only runs while its key is held (like +smPX / +smJB / +smEB)
        if want_px and self.smove_pixelsurf(e, e.yaw, (wx, wy) if mv else None):
            # pixelsurf: the hit sound repeats for as long as you keep surfing
            self._px_snd = getattr(self, "_px_snd", 0.0) - dt
            if self._px_snd <= 0:
                self._px_snd = self.move_hit_interval()
                self.play_move_hit()
            if e.isP:
                self.sc_on_pixelsurf(e)
        else:
            self._px_snd = 0.0
            if e.isP:
                self.sc_px_t0 = None
            if want_jb and self.smove_jumpbug(e, want_jb):
                self.play_move_hit()
                self.sc_on_trick("jb", SC_JB)
            elif want_eb and self.smove_edgebug(e):
                self.play_move_hit()
                self.sc_on_trick("eb", SC_EB)
                self.sc_chain = 0

        # --- CS:GO fall damage ---
        # Landing faster than 580 u/s hurts: (speed - 580) * 100 / (1024 - 580), so 1024 u/s kills.
        # A jumpbug (or edgebug / pixelsurf) never really lands, so it takes no damage.
        if (e.isP and _air_pre and e.alive and not e.jumpbug and not e.edgebug and not e.pxsurf
                and e.vz <= 0 and self.walkable(e)):
            fs = -(_vz_pre - SV_GRAVITY * dt)           # fall speed at impact (after this tick's gravity)
            if fs > FALL_SAFE_SPEED and not getattr(e, "god", False):
                dmg = (fs - FALL_SAFE_SPEED) * 100.0 / (FALL_FATAL_SPEED - FALL_SAFE_SPEED)
                e.hp -= dmg * 2.0                         # the player has double health (HUD shows 100)
                e.regen_delay = 5.0
                e.took_damage_this_frame = True
                self.shake = min(1.0, self.shake + min(0.6, dmg / 100.0 + 0.15))
                if e.hp <= 0:
                    self.kill(e, e)

    # --------------------------------------------------------------------------
    # sMove score system
    # --------------------------------------------------------------------------

    def sc_add_held(self, n):
        self.sc_held += n

    def sc_keep_alive(self):
        self.sc_timer = SC_TIMER_LEN

    def sc_on_trick(self, label, pts):
        """jumpbug / edgebug: points go into the held pool, the timer restarts, the label flashes cyan."""
        if not self.show_score:
            return
        self.sc_add_held(pts)
        self.sc_keep_alive()
        self.sc_trick = label
        self.sc_flash_ind = 0.2

    def sc_on_jump(self, e):
        """Take-off from the ground. Above 250 u/s each chained hop is a bhop; the 2nd and later score."""
        if not self.show_score:
            return
        hs = math.hypot(e.vx, e.vy)
        if hs > SC_BHOP_MIN_SPEED:
            self.sc_chain += 1
            if self.sc_chain > 1:
                self.sc_keep_alive()
                self.sc_add_held(SC_BHOP)
                self.sc_trick = "bhop x%d" % (self.sc_chain - 1)
                self.sc_jvel = int(hs)
                self.sc_flash_ind = 0.2

    def sc_on_pixelsurf(self, e):
        """Every tick of a pixelsurf keeps the timer up; moving faster than 150 u/s also adds a point."""
        if not self.show_score:
            return
        now = time.time()
        if self.sc_px_t0 is None:
            self.sc_px_t0 = now
        self.sc_keep_alive()
        self.sc_trick = "px %ss" % self.sc_fmt(round(now - self.sc_px_t0, 2))
        self.sc_chain = 0
        if math.hypot(e.vx, e.vy) > SC_PX_MIN_SPEED:
            self.sc_add_held(SC_PX)

    def sc_on_kill(self, v):
        """Bank everything you are holding plus the kill bonus."""
        if not self.show_score:
            return
        pts = self.sc_held + SC_KILL
        self.sc_total += pts
        self.sc_kill_pts = pts
        self.sc_kill_name = v.name or "UNKNOWN"
        self.sc_kill_t = 2.0
        self.sc_flash_score = 0.3
        self.sc_held = 0
        self.sc_timer = 0.0
        snd = self._kill_sound()
        if snd is not None:
            self.audio.play(1.0, snd, 0.0)

    def sc_reset_combo(self):
        self.sc_held = 0
        self.sc_timer = 0.0
        self.sc_chain = 0

    def sc_update(self, dt):
        P = self.P
        if not self.show_score:               # score system switched off in settings: nothing runs, nothing shows
            self.sc_reset_combo()
            self.sc_kill_t = self.sc_flash_score = self.sc_flash_ind = 0.0
            self.sc_px_t0 = None
            return
        if self.sc_timer > 0 and P.alive:
            self.sc_timer = max(0.0, self.sc_timer - dt)
            if self.sc_timer <= 0:
                self.sc_held = 0              # ran out of time: the held points are lost
                self.sc_chain = 0
        elif not P.alive:
            self.sc_reset_combo()
        if self.sc_chain and math.hypot(P.vx, P.vy) < SC_BHOP_MIN_SPEED:
            self.sc_chain = 0                 # slowed down below 250: the bhop chain is over
        self.sc_flash_score = max(0.0, self.sc_flash_score - dt)
        self.sc_flash_ind = max(0.0, self.sc_flash_ind - dt)
        self.sc_kill_t = max(0.0, self.sc_kill_t - dt)

    @staticmethod
    def sc_fmt(v):
        return ("%.2f" % v).rstrip("0").rstrip(".")

    def _kill_sound(self):
        if not hasattr(self, "_ks_snd"):
            self._ks_snd = None
            try:
                self._ks_snd = pygame.mixer.Sound(str(Path(__file__).parent / "assets" / "killsound.wav"))
            except Exception as ex:
                print("kill sound failed:", ex)
        return self._ks_snd

    # ==========================================================================
    # combat: shooting, bots, kills, shove, weapon switching
    # ==========================================================================

    def los(self, a, b):
        dx, dy, dz = b.x - a.x, b.y - a.y, b.z - a.z
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        t = 24
        while t < d:
            if self.world.solid(a.x + dx * t / d, a.y + dy * t / d, a.z + 17 + dz * t / d):
                return 0
            t += 24
        return 1

    def kill(self, v, k):
        v.alive = 0
        v.resp = 3
        v.deaths += 1
        k.kills += 1
        self.score[k.team] += 1
        w = CFG["weapons"][k.w]
        self.feed.insert(0, {
            "killer": k.name or "UNKNOWN", "victim": v.name or "UNKNOWN",
            "weapon": ("KNIFE" if w.melee else w.n) if w else "RIFLE",
            "kl": bool(k.isP), "vl": bool(v.isP), "kt": k.team, "vt": v.team,
        })
        self.feed = self.feed[:5]
        v.anim = fresh_anim()
        if k.isP and v is not k:
            self.hm, self.hm_kill = HM_TIME, True     # red hit marker on every kill (bots, online players, knife)
            self.streak += 1
            self.sc_on_kill(v)
        if v.isP:
            self.streak = 0
            self.sc_reset_combo()
            self.death_killer = k.name or "UNKNOWN"
            if self.P.zoom:
                self.P.zoom = 0
        if v.isP and self.net.active:
            self.net.send({"t": "kill", "k": self.net.my_id if k.isP else k.nid,
                           "v": self.net.my_id, "w": k.w})
        if self.score[k.team] >= CFG["frags"]:
            self.over = 6.0

    def fire(self, e, extra=0.0):
        w = CFG["weapons"][e.w]
        if e.cool > 0 or not e.alive:
            return
        if not w.melee and e.ammo[e.w] <= 0:
            if not getattr(self, "_manual_reload", False):
                e.rl_prev = 0            # empty mag auto-reload: cancelling leaves you with an empty mag
            e.ammo[e.w] = w.mag
            dur = 1.6
            if e.isP and w.vm and hasattr(w.vm, "want_empty"):
                w.vm.want_empty = not getattr(self, "_manual_reload", False)
            if e.isP and w.vm and w.vm.find("reload"):
                # the reload lasts as long as the weapon's reload animation, so a shot can never
                # cut it off halfway (that made the hands snap / bug out)
                d = self.vm_play(w.vm, "reload")
                if d > 0.3:
                    dur = min(d, 6.0)
            if w.pellets and int(w.pellets) > 1:
                # shotgun: the whole reload takes as long as the AWP's, so the animation is slowed to fit
                tgt = self.awp_reload_len() * SHOTGUN_RELOAD_SCALE
                ch = self.vm_reload_chain(w.vm, min(8, max(1, int(w.mag or 1) - int(getattr(e, "rl_prev", 0) or 0)))) \
                    if (e.isP and w.vm) else None
                if ch:
                    # play start -> insert(s) -> end so the reload (and the pump) actually completes
                    self.vmA["s"], self.vmA["t"], self.vmA["loop"], self.vmA["kind"] = ch[0][0], 0.0, False, "reload"
                    self.vmA["hold"] = False
                    self.vmA["chain"] = [c_[0] for c_ in ch[1:]]
                    dur = sum(c_[1] for c_ in ch)
                if e.isP and w.vm and w.vm.find("reload") and dur > 0.3:
                    self.vmA["spd"] = dur / tgt
                dur = tgt
            e.cool = dur
            e.reloadT = dur
            e.zoom = 0   # scope out when a reload starts
            return
        if not w.melee:
            e.ammo[e.w] -= 1
        e.cool = 60 / w.rpm
        if e.isP and not w.melee:
            _n = str(w.n or "").lower()
            _snip = ("awp" in _n or "scout" in _n or (not w.auto and not w.pellets and not w.head_dmg and 0 < (w.mag or 0) <= 10))
            self.shake = min(1.0, self.shake + (0.9 if _snip else 0.28))
        if e.isP and w.vm:
            self.vm_play(w.vm, "slash" if w.melee else "shoot")
            if not w.melee:
                self._vm_fit_cycle(e, w, w.vm)
        pp = e.pitch + e.pun_p      # aim + current recoil punch: bullets (and tracers) follow the recoil
        yy = e.yaw + e.pun_y
        cp = math.cos(pp)
        sp = (w.spr or 0) + extra
        sniper = ("awp" in str(w.n or "").lower() or "scout" in str(w.n or "").lower()
                  or (not w.melee and not w.auto and not w.pellets and not w.head_dmg and 0 < (w.mag or 0) <= 10))
        if e.isP and sniper:
            # CS:GO style AWP: pin-point when scoped and standing still, wild when unscoped,
            # in the air, and a bit off while moving
            spd = math.hypot(e.vx, e.vy)
            if not e.zoom or not e.walk:
                sp = .11          # unscoped / in the air: shots scatter widely
            elif spd > 40:
                sp = .03 + min(spd, 250.0) / 250.0 * .04
            else:
                sp = 0.0
        e.shootT = .15
        if e.isP and not w.melee:
            if w.auto:
                # CS:GO style spray: every bullet moves the aim along a fixed pattern
                p0 = spray_pattern(e.spray)
                e.spray = min(30.0, e.spray + 1.0)
                p1 = spray_pattern(e.spray)
                # the recoil moves your real aim and stays there (no snap back when you stop shooting)
                e.pend_p += p1[0] - p0[0]
                e.pend_y += p1[1] - p0[1]
                e.since = 0.0
                e.sprayw = True
            else:
                kick = w.recoil if w.recoil is not None else 0.05
                e.pun_p = min(0.3, e.pun_p + kick)
                e.pun_y += (random.random() - .5) * kick * .8
                e.sprayw = False
        if e.isP:
            # shoot from the exact eye position you are looking from (the camera is interpolated
            # between physics ticks, so e.x / e.y / e.z alone can be a hair behind it)
            al = min(1.0, e.acc / TICK)
            o = [e.px + (e.x - e.px) * al, e.py + (e.y - e.py) * al,
                 e.pz + (e.z - e.pz) * al + 17 + e.so]
        else:
            o = [e.x, e.y, e.z + 17 + e.so]
        pellets = max(1, int(w.pellets or 1)) if not w.melee else 1
        rem_dmg = {}
        bt = tw = 0.0
        for _pi in range(pellets):
            d = [cp * math.cos(yy) + (random.random() - .5) * sp * 2,
                 cp * math.sin(yy) + (random.random() - .5) * sp * 2,
                 math.sin(pp) + (random.random() - .5) * sp * 2]
            m = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2)
            d = [x / m for x in d]
            rg = w.range or 3000
            tw = rg
            step = 4.0 if e.isP else 16.0   # bots use a coarser (much cheaper) ray
            t = 1.0
            solid = self.world.bullet_solid
            while t < rg:
                if solid(o[0] + d[0] * t, o[1] + d[1] * t, o[2] + d[2] * t):
                    lo2, hi2 = max(0.0, t - step), t      # refine: find the exact wall distance
                    for _r in range(5):
                        mid = (lo2 + hi2) * .5
                        if solid(o[0] + d[0] * mid, o[1] + d[1] * mid, o[2] + d[2] * mid):
                            hi2 = mid
                        else:
                            lo2 = mid
                    tw = hi2
                    break
                t += step
            best, bt = None, tw
            best_head = False
            for v in self.ents:
                if not v.alive or v.team == e.team:
                    continue
                for is_head, (bcx, bcy, bcz, bhx, bhy, bhz) in zip((False, True), self.target_shape(v)):
                    p = (bcx, bcy, bcz)
                    h = (bhx, bhy, bhz)
                    a, b = 0.0, 1e9
                    for k in range(3):
                        if abs(d[k]) < 1e-6:
                            if o[k] < p[k] - h[k] or o[k] > p[k] + h[k]:
                                b = -1
                        else:
                            t1 = (p[k] - h[k] - o[k]) / d[k]
                            t2 = (p[k] + h[k] - o[k]) / d[k]
                            a = max(a, min(t1, t2))
                            b = min(b, max(t1, t2))
                    if a <= b and b > 0 and a < bt:
                        best, bt, best_head = v, a, is_head
            if best:
                killed_ = False
                dmg = w.dmg
                if w.head_dmg and best_head:
                    dmg = w.head_dmg          # headshot
                if pellets > 1:
                    dmg *= max(.2, 1.0 - bt / float(rg))      # shotgun pellets lose damage with distance
                if best.remote:
                    rem_dmg[best] = rem_dmg.get(best, 0.0) + dmg    # one message per victim, not one per pellet
                else:
                    if not getattr(best, "god", False):
                        best.hp -= dmg * (1 if e.isP else .7)
                        if best.hp <= 0:
                            killed_ = True
                            self.kill(best, e)
                if e.isP:
                    self.hm, self.hm_kill = HM_TIME, killed_
            if not w.melee:
                tl = min(bt, 115 if e.isP else 85)
                # your own tracer starts a little ahead of the eye, right on the aim line,
                # so it comes out of the crosshair instead of the bottom of the screen
                ts = min(40.0, tl * .5) if e.isP else 0.0
                # nudged a little to the right and down so the line isn't hidden behind the crosshair
                ox = math.sin(yy) * 4.0 if e.isP else 0.0
                oy = -math.cos(yy) * 4.0 if e.isP else 0.0
                self.tracers.append({
                    "a": (o[0] + d[0] * ts + ox, o[1] + d[1] * ts + oy,
                          o[2] + d[2] * ts - (3.0 if e.isP else 2.0)),
                    "b": (o[0] + d[0] * tl, o[1] + d[1] * tl, o[2] + d[2] * tl),
                    "al": (.62 if e.isP else .32) * (.55 if pellets > 1 else 1), "t": .04 if e.isP else .022,
                })
        for _v, _dm in rem_dmg.items():
            self.net.send({"t": "hit", "to": _v.nid, "d": int(round(_dm)), "w": e.w})  # the victim applies it
        if e.isP and self.net.active:
            tl2 = min(bt, 85)
            self.net.send({"t": "shot", "w": e.w,
                           "o": [round(o[0], 1), round(o[1], 1), round(o[2], 1)],
                           "e": [round(o[0] + d[0] * tl2, 1), round(o[1] + d[1] * tl2, 1), round(o[2] + d[2] * tl2, 1)]}, droppable=True)
        if not w.melee or w.snd:
            if e.isP:
                g, pan = .42, 0.0
            else:
                g, pan = self.spatial(e.x, e.y, e.z + 17, .55)
            snd = w.snd
            if g > .004:
                self.audio.play(g, snd, pan)
        if e.isP and e.w == 0 and e.zoom:
            e.zoom = 0  # AWP: scope out after each shot

    def dummy_tick(self, e, dt):
        """Bots are stand-still dummies: no AI, but still physical (they fall, and a shove / toss
        slides them until the horizontal speed is damped away)."""
        e.vx *= max(0.0, 1.0 - 6.0 * dt)
        e.vy *= max(0.0, 1.0 - 6.0 * dt)
        if abs(e.vx) < 8:
            e.vx = 0.0
        if abs(e.vy) < 8:
            e.vy = 0.0
        self.phys(e, dt, e.vx, e.vy, False)

    def target_shape(self, v):
        """Hitboxes of a player / bot that follow its real (posed, shrunk) model:
        returns (body, head), each (cx, cy, cz, hx, hy, hz) in world units.
        The body box covers the feet up to the neck, the head box sits on the model's head."""
        sl = (getattr(v, "sl", 0) or 0) > 0 or (getattr(v, "cr", 0) or 0) > .5
        vs = PLAYER_SCALE if not v.isP else 1.0
        pm = getattr(v, "pm", None)
        vtx = getattr(pm, "vtx", None) if pm is not None else None
        gy = None
        if vtx is not None:
            gy = v.anim.get("gyaw") if getattr(v, "anim", None) else None
            key = (v.x, v.y, v.z, gy, v.yaw, getattr(v, "pm_dz", 0.0))
            c = getattr(v, "_shape_c", None)
            if c is not None and c[0] is vtx and c[1] == key:
                return c[2]
        hw = HIT_HALF_W * vs
        if vtx is not None and len(vtx):
            zmin, zmax = float(vtx[:, 2].min()), float(vtx[:, 2].max())
            H = max(1e-3, zmax - zmin)
            zcut = zmax - HEAD_FRAC * H
            m_top = vtx[:, 2] >= zcut
            top = vtx[m_top]
            low = vtx[~m_top]
            ang = math.radians(gy if gy is not None else math.degrees(v.yaw))
            ca, sa = math.cos(ang), math.sin(ang)

            def world(lx, ly, lz):
                return (v.x + vs * (ca * lx - sa * ly), v.y + vs * (sa * lx + ca * ly),
                        v.z + v.pm_dz + vs * lz)
            if len(top):
                hx_l, hy_l = float(top[:, 0].mean()), float(top[:, 1].mean())
                ext = max(float(top[:, 0].max() - top[:, 0].min()), float(top[:, 1].max() - top[:, 1].min())) * .5
            else:
                hx_l = hy_l = 0.0
                ext = HEAD_MAX_R
            hr = max(HEAD_MIN_R, min(HEAD_MAX_R, ext)) * vs
            bx_l, by_l = (float(low[:, 0].mean()), float(low[:, 1].mean())) if len(low) else (0.0, 0.0)
            hxw, hyw, _ = world(hx_l, hy_l, 0.0)
            bxw, byw, _ = world(bx_l, by_l, 0.0)
            # keep the body box near the root so a stray arm / weapon cannot drag it away
            bxw = v.x + max(-12.0, min(12.0, bxw - v.x))
            byw = v.y + max(-12.0, min(12.0, byw - v.y))
            z0 = v.z + v.pm_dz + vs * zmin
            z1 = v.z + v.pm_dz + vs * zcut
            z2 = v.z + v.pm_dz + vs * zmax
            res = ((bxw, byw, (z0 + z1) * .5, hw, hw, (z1 - z0) * .5),
                   (hxw, hyw, (z1 + z2) * .5, hr, hr, (z2 - z1) * .5))
            v._shape_c = (vtx, key, res)
            return res
        # no player model (box bots / yourself): fixed box, feet kept in place
        feet = v.z - (16 if sl else 0) - (21 if sl else 35)
        H = 2 * (HIT_HALF_H_LOW if sl else HIT_HALF_H) * vs
        zcut = feet + H * (1 - HEAD_FRAC)
        hr = max(HEAD_MIN_R, min(HEAD_MAX_R, hw * .8))
        return ((v.x, v.y, (feet + zcut) * .5, hw, hw, (zcut - feet) * .5),
                (v.x, v.y, (zcut + feet + H) * .5, hr, hr, (feet + H - zcut) * .5))

    def knife_index(self):
        return next((i for i, w in enumerate(CFG["weapons"]) if w.melee), 0)

    def select_weapon(self, i):
        """Key 1 = your primary gun, key 2 = knife. You only ever carry one gun plus the knife."""
        P = self.P
        if P and i in (0, 1):
            P.w = self.primary if i == 0 else self.knife_index()
            P.zoom = 0

    def set_primary(self, i):
        """Choose the one gun you carry (Loadout tab). Swaps straight to it if you are holding a gun."""
        if not (0 <= i < len(CFG["weapons"])) or CFG["weapons"][i].melee:
            return
        self.primary = i
        P = self.P
        if P and not CFG["weapons"][P.w].melee and P.w != i:
            P.w = i
            P.zoom = 0

    def reload_pressed(self):
        P = self.P
        if P and not CFG["weapons"][P.w].melee:
            if P.reloadT > 0 or P.ammo[P.w] >= CFG["weapons"][P.w].mag:
                return  # already reloading / full: pressing R must not restart the animation
            P.rl_prev = P.ammo[P.w]      # so a melee can cancel the reload and give the rounds back
            P.ammo[P.w] = 0
            P.cool = 0
            self._manual_reload = True
            try:
                self.fire(P)
            finally:
                self._manual_reload = False

    # ==========================================================================
    # view-model playback and weapon animation sounds
    # ==========================================================================

    def _vm_moving(self):
        P = self.P
        # moving = any real motion: running, sliding, or in the air (jumping / falling / air-strafing)
        return bool(P.alive and (math.hypot(P.vx, P.vy) > 40 or (not P.walk and abs(P.vz) > 40)))

    def awp_reload_len(self):
        """Seconds the AWP takes to reload (its reload animation, or 3.7 s like CS:GO when it has no model)."""
        for w in CFG["weapons"]:
            if str(w.n or "").lower() == "awp":
                try:
                    if w.vm:
                        old = getattr(w.vm, "want_empty", False)
                        w.vm.want_empty = False
                        c = w.vm.find("reload")
                        w.vm.want_empty = old
                        if c:
                            sq = w.vm.seqs[c[0]]
                            d = sq["n"] / max(1.0, sq["fps"])
                            if d > 0.3:
                                return min(d, 6.0)
                except Exception:
                    pass
        return 3.7

    def vm_reload_chain(self, vm, n_ins):
        """Shell-by-shell shotgun reloads are split into reload_start / reload_insert / reload_end sequences.
        Returns [(seq, seconds), ...] to play back to back (start, insert x n_ins, end), or None for a one-clip reload."""
        try:
            S = vm.seqs
            ok = [i for i in range(len(S)) if S[i].get("desc") is not None and not (S[i]["flags"] & 0x4)]

            def get(rx):
                return [i for i in ok if re.search(rx, S[i]["label"].lower())]
            st = get(r"reload.*start|start.*reload|^(reload_)?begin")
            ins = get(r"reload.*(insert|loop)|(insert|loop).*reload|^insert")
            en = get(r"reload.*(end|finish|pump)|(end|finish|pump).*reload|^pump")
            if not ins:
                return None
            ids = st[:1] + [ins[0]] * max(1, int(n_ins)) + en[:1]
            return [(i, S[i]["n"] / max(1.0, S[i]["fps"])) for i in ids]
        except Exception:
            return None

    # Source view-models carry sound events (AE_CL_PLAYSOUND) on their sequences, e.g. "Weapon_AWP.Clipout" at 12 %
    # of the reload. L4D2 packs keep them on the matching "<seq>_layer" sequence. Each event name is turned into a
    # sound file of the weapon pack (via its game_sounds script, else by file name) and played when the animation
    # reaches that point. Models without usable events get the usual draw / clip-out / clip-in / bolt timing.
    _EV_GROUPS = (
        ("draw", "deploy", "pullout", "pullup", "ready", "select", "raise", "equip", "takeout", "pickup", "unholster"),
        ("clipout", "magout", "magazineout", "magrelease", "clipoff", "magdrop", "clipeject", "ejectclip", "magremove"),
        ("clipin", "magin", "magazinein", "magslap", "clipon", "clipinsert", "insertclip", "magload"),
        ("boltforward", "boltrelease", "slideforward", "slidestop", "boltpush", "cockforward", "boltclose", "slidelock"),
        ("bolt", "boltback", "boltpull", "slideback", "slidepull", "cock", "pullback", "cockback", "charge", "boltopen"),
        ("pump", "pumpback", "pumpforward", "pumpend", "pumpaction"),
        ("insert", "shellinsert", "shell", "loadshell", "bulletin", "bulletinsert"),
    )

    def _ev_canon(self, act):
        k = re.sub(r"\d+$", "", key_norm(act))
        for g in self._EV_GROUPS:
            if k in g:
                return g[0]
        return k

    def _game_sound_map(self, arch):
        """{sound script name: wave path} from the scripts/game_sounds*.txt files inside a pack (cached)."""
        cache = self.__dict__.setdefault("_gs_map", {})
        k = id(arch)
        if k not in cache:
            m = {}
            for n in list(getattr(arch, "files", {})):
                low = n.replace("\\", "/").lower()
                if "scripts/" in low and low.endswith(".txt") and "sound" in low.rsplit("/", 1)[-1]:
                    try:
                        kv = parse_kv(arch.read(n).decode("utf-8", "ignore"))
                    except Exception:
                        continue
                    for name, ent in kv.items():
                        if not isinstance(ent, dict):
                            continue
                        wv = ent.get("wave")
                        if wv is None and isinstance(ent.get("rndwave"), dict):
                            wv = ent["rndwave"].get("wave")
                        if isinstance(wv, str) and wv:
                            m[name.lower()] = re.sub(r"^[\)\(\^#\*<>@~\?\$`]+", "", wv).replace("\\", "/").lower()
            if getattr(arch, "is_gma", False):         # Garry's Mod: sound.Add{ name = "...", sound = "..." } in lua
                try:
                    for nm_, wv_ in arch.lua_sound_defs().items():
                        m.setdefault(nm_, wv_)
                except Exception as ex:
                    print("gma sound.Add scan failed:", ex)
            cache[k] = m
        return cache[k]

    def _load_pack_snd(self, arch, n):
        ck = self.__dict__.setdefault("_pack_snd_obj", {})
        key = (id(arch), n)
        if key not in ck:
            try:
                ck[key] = pygame.mixer.Sound(io.BytesIO(arch.read(n)))
            except Exception as ex:
                print("cannot load pack sound", n, ex)
                ck[key] = None
        return ck[key]

    def wpn_ev_sound(self, w, evname):
        """pygame Sound for a weapon animation event name ('Weapon_AWP.Clipout') or a bare action ('draw'), or None."""
        spec = self.asset["mdl"].get(getattr(w, "_m", None))
        arch = getattr(spec, "arch", None)
        if arch is not None and not hasattr(arch, "read"):
            arch = None
        ck = self.__dict__.setdefault("_ev_snd", {})
        key = (id(arch), str(w.n), str(evname).lower())
        if key in ck:
            return ck[key]
        res = None
        try:
            res = self._resolve_ev_sound(w, arch, str(evname))
        except Exception as ex:
            print("weapon event sound failed:", evname, ex)
        ck[key] = res
        return res

    def _resolve_ev_sound(self, w, arch, name):
        files = self.pack_sound_files(arch) if arch is not None else []
        # 1) the pack's own sound script (Weapon_AWP.Clipout -> weapons/awp/gunother/awp_clipout.wav)
        if arch is not None:
            wave = self._game_sound_map(arch).get(name.lower())
            if wave:
                low = {n.replace("\\", "/").lower(): n for n in files}
                hit = low.get("sound/" + wave) or next((o for l, o in low.items() if l.endswith("/" + wave)), None)
                if hit:
                    return self._load_pack_snd(arch, hit)
        # 2) by file name: weapon prefix + action ('awp' + 'clipout' -> awp_clipout.wav)
        pre, _, act = name.rpartition(".")
        pre = key_norm(re.sub(r"^weapon_?", "", pre.lower()))
        ak = self._ev_canon(act or name)
        ak_raw = re.sub(r"\d+$", "", key_norm(act or name))
        if not ak:
            return None
        base = re.sub(r"\.mdl$", "", str(getattr(w, "_m", "") or "").lower())
        base = re.sub(r"\s*\(.*\)", "", re.sub(r"^v_", "", base))
        toks = {t for t in (pre, key_norm(base), key_norm(str(w.n or "")),
                            key_norm(re.sub(r"^(rif|snip|smg|pist|shot|mach|pistol|rifle|sniper)_", "", base))) if len(t) >= 3}
        mates = [m for m, sp in self.asset["mdl"].items()
                 if arch is not None and getattr(sp, "arch", None) is arch and not _is_arms_mdl(m)]
        cands = [(n.replace("\\", "/").lower(), (lambda n=n: self._load_pack_snd(arch, n))) for n in files]
        cands += [(n.lower(), (lambda s=sn: s)) for n, sn in self.asset["wav"].items()]
        best = None
        for low, getter in cands:
            stem = re.sub(r"\.\w+$", "", low.rsplit("/", 1)[-1])
            sk = key_norm(stem)
            pk = key_norm(low)
            rest = sk
            for t in sorted(toks, key=len, reverse=True):
                if sk.startswith(t) and len(sk) > len(t):
                    rest = sk[len(t):]
                    break
            rest_raw = re.sub(r"\d+$", "", rest)
            rc = self._ev_canon(rest_raw)
            toks_ = [x for x in re.split(r"[^a-z0-9]+", stem) if x and x not in ("reload", "weapon", "wpn", "sfx", "snd")]
            joined_ = {"".join(toks_[i:i + n_]) for n_ in (1, 2) for i in range(len(toks_))}
            joined_ = {re.sub(r"\d+$", "", j_) for j_ in joined_}
            if rest_raw == ak_raw:
                sc = 100
            elif rc == ak:
                sc = 80
            elif any(self._ev_canon(j_) == ak and j_ for j_ in joined_ if len(j_) >= 3):
                sc = 60
            elif len(ak_raw) >= 4 and (ak_raw in rest_raw or (len(rest_raw) >= 4 and rest_raw in ak_raw)) and \
                    self._ev_canon(rest_raw) == rest_raw:
                sc = 30
            else:
                continue
            if any(t in pk for t in toks):
                sc += 10
            elif len(mates) > 1:
                continue            # several weapons in this pack: only take sounds that name this one
            if re.search(r"incendiary|silenc|suppress|distant|/far|explo", low):
                sc -= 8
            if best is None or sc > best[0]:
                best = (sc, getter)
        return best[1]() if best else None

    def _seq_events(self, vm, si):
        """Sound events of a sequence: its own, plus those of its '<label>_layer' sequence (L4D2 packs)."""
        S = vm.seqs
        s = S[si]
        ev = list(s.get("events") or [])
        lab = s["label"].lower()
        for t in S:
            if t is not s and t["label"].lower() in (lab + "_layer", lab + "layer"):
                ev += list(t.get("events") or [])
        return sorted(set(ev))

    def _default_sound_plan(self, kind, label):
        lab = (label or "").lower()
        if kind == "draw":
            return [(0.0, "draw")]
        if kind == "reload":
            if re.search(r"insert|loop", lab):
                return [(0.3, "insert")]
            if re.search(r"end|finish|pump", lab):
                return [(0.3, "pump")]
            if re.search(r"start|begin", lab):
                return []
            return [(0.22, "clipout"), (0.58, "clipin"), (0.84, "boltforward")]
        return []

    _LUA_KIND = {
        "draw": re.compile(r"draw|deploy|equip|takeout|pullout|ready|raise|unholster"),
        "reload": re.compile(r"reload|mag|clip|insert|chamber"),
        "cycle": re.compile(r"bolt|cock|cycle|pump|rechamber|charge|pullback|slide"),
    }

    def _lua_sound_plan(self, vm, w, si, kind):
        """Garry's Mod weapons start their sounds from lua (TFA EventTable, ArcCW SoundTable, CW2 Sounds ...), at times
        in seconds, not from events inside the model. Read those tables of the addon: [(cycle, Sound, action)]."""
        rx = self._LUA_KIND.get(kind)
        spec = self.asset["mdl"].get(getattr(w, "_m", None))
        arch = getattr(spec, "arch", None)
        if rx is None or arch is None or not getattr(arch, "is_gma", False):
            return []
        try:
            tl = arch.swep_sound_timeline(spec.mdl)
        except Exception as ex:
            print("gma sound tables failed:", ex)
            return []
        sq = vm.seqs[si]
        dur = max(0.05, float(sq["n"]) / max(1.0, float(sq["fps"])))
        lab = sq["label"].lower()
        hits = [(k, t, p) for k, t, p in tl if rx.search(k)]
        if kind == "reload" and hits:
            emp = [h for h in hits if "empty" in h[0] or "dry" in h[0] or "empty" in h[2]]
            full = [h for h in hits if h not in emp]
            hits = (emp if (re.search(r"empty|dry", lab) or getattr(vm, "want_empty", False)) and emp else full) or hits
        # tables named after the sequence ("animations.reload_empty") belong to that sequence only
        named = [h for h in hits if lab and re.search(r"(^|[.\[_])" + re.escape(lab) + r"($|[.\]_])", h[0])]
        hits = named or hits
        out = []
        for k, t, p in sorted(hits, key=lambda h: h[1]):
            snd = self._load_pack_snd(arch, p)
            if snd is not None:
                out.append((min(1.0, t / dur), snd, self._ev_canon(re.sub(r"\.\w+$", "", p.rsplit("/", 1)[-1]))))
        return out

    def vm_seq_sounds(self, vm, w, si, kind):
        """[(cycle, Sound, action), ...] for one view-model sequence, sorted by cycle."""
        ck = self.__dict__.setdefault("_vm_evs", {})
        key = (id(vm), id(w), si, kind)
        if key in ck:
            return ck[key]
        out = []
        try:
            for cyc, nm in self._seq_events(vm, si):
                snd = self.wpn_ev_sound(w, nm)
                if snd is not None:
                    out.append((float(cyc), snd, self._ev_canon(nm.rpartition(".")[2] or nm)))
            if not out:
                out += self._lua_sound_plan(vm, w, si, kind)
            have = {o[2] for o in out}
            if kind == "draw" and "draw" not in have:       # the deploy sound is not always an event of the draw clip
                sd = self.wpn_ev_sound(w, "draw")
                if sd is not None:
                    out.append((0.0, sd, "draw"))
            elif kind == "reload" and not out:               # no usable events: spread the usual reload sounds out
                for cyc, act in self._default_sound_plan(kind, vm.seqs[si]["label"]):
                    sd = self.wpn_ev_sound(w, act)
                    if sd is not None:
                        out.append((cyc, sd, act))
        except Exception as ex:
            print("view-model sound events failed:", ex)
        out.sort(key=lambda o: o[0])
        ck[key] = out
        return out

    def vm_events_tick(self, vm, w, A, s):
        """Play the sound events the current view-model animation passed since the last frame."""
        if A.get("ev_pid") != A.get("pid"):
            A["ev_pid"] = A.get("pid")
            A["ev_c"] = -1.0
        cur = min(1.0, A["t"] / max(1.0, float(s["n"])))
        prev = A["ev_c"]
        if cur <= prev:
            return
        for cyc, snd, _a in self.vm_seq_sounds(vm, w, A["s"], A.get("kind", "")):
            if prev < cyc <= cur:
                self.audio.play(VM_EVENT_GAIN, snd, 0.0)
        A["ev_c"] = cur

    def wpn_sound_plan(self, w, kind):
        """[(cycle 0..1, Sound, action)] for another player's / bot's weapon (kind = 'reload' or 'draw')."""
        vm = getattr(w, "vm", None)
        try:
            c = vm.find(kind) if vm else None
            if c:
                return self.vm_seq_sounds(vm, w, c[0], kind)
        except Exception:
            pass
        out = []
        for cyc, act in self._default_sound_plan(kind, ""):
            sd = self.wpn_ev_sound(w, act)
            if sd is not None:
                out.append((cyc, sd, act))
        return out

    def remote_wpn_sounds(self, e, A, wpn, reload):
        """Spatial pull-out / reload sounds for the other players and the bots."""
        if e.isP or wpn is None or not e.alive:
            return
        try:
            if A.get("snd_w") is not None and A.get("snd_w") != e.w:
                sd = self.wpn_sound_plan(wpn, "draw")
                for _c, snd, _a in sd[:1]:
                    g, pan = self.spatial(e.x, e.y, e.z + 17, .4)
                    if g > .004:
                        self.audio.play(g, snd, pan)
            A["snd_w"] = e.w
            if reload is None:
                A["snd_rl"] = -1.0
                return
            prev = A.get("snd_rl", -1.0)
            if reload > prev:
                for cyc, snd, _a in self.wpn_sound_plan(wpn, "reload"):
                    if prev < cyc <= reload:
                        g, pan = self.spatial(e.x, e.y, e.z + 17, .4)
                        if g > .004:
                            self.audio.play(g, snd, pan)
                A["snd_rl"] = reload
        except Exception as ex:
            print("remote weapon sound failed:", ex)

    def vm_play(self, vm, kind):
        self.vmA["chain"] = None
        self.vmA["cut"] = None
        self.vmA["pid"] = self.vmA.get("pid", 0) + 1     # new playback: its sound events start over
        c = vm.find(kind)
        if not c and kind == "slash":
            kind = "shoot"
            c = vm.find("shoot")
        if not c:
            kind = "idle"
            c = vm.find("idle")
        vm.begin_fade(0.07)
        self.vmA["s"] = random.choice(c) if c else 0
        self.vmA["t"] = 0.0
        self.vmA["loop"] = kind in ("idle", "run")
        self.vmA["kind"] = kind
        self.vmA["spd"] = 1.0
        # standing still: hold the first frame of the idle sequence instead of playing it
        self.vmA["hold"] = bool(kind == "idle" and not VM_IDLE_ANIM and not self._vm_moving())
        if c and 0 <= self.vmA["s"] < len(vm.seqs):
            sq = vm.seqs[self.vmA["s"]]
            return sq["n"] / max(1.0, sq["fps"])   # how long the chosen animation takes (seconds)
        return 0.0

    VM_CYCLE_MAX_COOL = 1.6     # seconds: longest the shot + bolt-cycle may keep a bolt-action GMod weapon busy
    VM_SHOT_PART = 0.25         # seconds of the shot clip (the recoil kick) played before the cock-back starts

    @staticmethod
    def _vm_is_bolt_gma(vm):
        """Garry's Mod view-model of a bolt-action weapon (decided by the addon's own weapon script)."""
        try:
            d = vm.d
            arch = getattr(d, "arch", None)
            return bool(getattr(arch, "is_gma", False) and arch.is_bolt_action(getattr(d, "name", "")))
        except Exception:
            return False

    def _vm_fit_cycle(self, e, w, vm):
        """GMod bolt-action / pump weapons: the shot clip AND the cock-back clip that follows it have to fit between
        two shots. Without this the next shot (after 60 / rpm seconds) restarted the shot clip before it ended, so the
        cock-back never got its turn. The cooldown is stretched to the two clips (capped at VM_CYCLE_MAX_COOL) and, when
        even that is shorter, both clips are sped up to fit."""
        A = self.vmA
        A["cspd"] = 1.0
        try:
            if w.auto or e.ammo[e.w] <= 0:
                return
            if not self._vm_is_bolt_gma(vm):
                return
            c = vm.find("cycle")
            if not c or not (0 <= A["s"] < len(vm.seqs)) or not (0 <= c[0] < len(vm.seqs)):
                return
            sh, cy = vm.seqs[A["s"]], vm.seqs[c[0]]
            t_shot = min(self.VM_SHOT_PART, sh["n"] / max(1.0, sh["fps"]))
            t_cyc = cy["n"] / max(1.0, cy["fps"])
            want = min(max(e.cool, t_shot + t_cyc), self.VM_CYCLE_MAX_COOL)
            e.cool = want
            A["cut"] = t_shot * sh["fps"]                       # frame where the cock-back takes over
            A["cspd"] = max(1.0, t_cyc / max(0.2, want - t_shot))   # cock-back clip speed
        except Exception as ex:
            print("bolt-cycle timing skipped:", ex)

    def _vm_cycle_after_shot(self, vm, A):
        """Garry's Mod bolt-action / pump / manual weapons keep the 'cock back' as its own sequence after the shot
        (bolt, cock, cycle, pump ...). Play it once after the shot clip, as long as there is a round left."""
        try:
            if not self._vm_is_bolt_gma(vm):
                return False
            w = CFG["weapons"][A["w"]]
            if w.melee or w.auto or self.P.ammo[A["w"]] <= 0:
                return False
            c = vm.find("cycle")
            if not c:
                return False
            A["s"], A["t"], A["loop"], A["kind"], A["hold"], A["spd"] = c[0], 0.0, False, "cycle", False, A.get("cspd", 1.0)
            A["pid"] = A.get("pid", 0) + 1
            vm.begin_fade(0.03)
            return True
        except Exception as ex:
            print("cycle animation skipped:", ex)
            return False

    def vm_tick(self, vm, dt):
        A = self.vmA
        if A["vm"] is not vm or A["w"] != self.P.w:
            A["vm"] = vm
            A["w"] = self.P.w
            self.vm_play(vm, "draw")
        vm.tick(dt)
        moving = self._vm_moving()
        if A["loop"] and A.get("kind") in ("idle", "run"):
            want = "run" if (moving and vm.find("run")) else "idle"
            if A.get("kind") != want:
                self.vm_play(vm, want)
        if not (0 <= A["s"] < len(vm.seqs)):
            return
        s = vm.seqs[A["s"]]
        still_idle = bool(A["loop"] and A.get("kind") == "idle" and not VM_IDLE_ANIM and not moving)
        if A["loop"] and A.get("kind") == "idle" and not VM_IDLE_ANIM and moving:
            A["hold"] = False            # moving again: let the animation play
        if still_idle and A.get("hold"):
            A["t"] = 0.0
            vm.pose(A["s"], 0.0, loop=True)   # held still pose
            return
        if still_idle:
            # just stopped: blend smoothly into the still pose (no fast playback)
            vm.begin_fade(0.3)
            A["hold"] = True
            A["t"] = 0.0
            vm.pose(A["s"], 0.0, loop=True)
            return
        A["t"] += dt * s["fps"] * A.get("spd", 1.0)
        if not A["loop"]:
            try:
                self.vm_events_tick(vm, CFG["weapons"][A["w"]], A, s)
            except Exception as ex:
                print("view-model sound event failed:", ex)
        f = A["t"]
        if A.get("cut") and A.get("kind") == "shoot" and not A["loop"] and f >= A["cut"]:
            A["cut"] = None
            if self._vm_cycle_after_shot(vm, A):
                return
        if f >= s["n"]:
            if A["loop"]:
                A["t"] = f = f % s["n"]
            else:
                nxt = A.get("chain")
                if nxt:
                    # next piece of a chained reload (start -> insert -> end), keeping the slowed-down speed
                    A["s"] = nxt.pop(0)
                    A["t"] = 0.0
                    A["pid"] = A.get("pid", 0) + 1
                    vm.begin_fade(0.03)
                    return
                if A.get("kind") == "shoot" and self._vm_cycle_after_shot(vm, A):
                    return
                self.vm_play(vm, "idle")
                return
        # 1/8-frame steps; SrcInst.pose also rate-limits skinning to ~90 Hz
        ff = round(f * 8) / 8
        if ff >= s["n"]:
            ff = 0.0 if A["loop"] else s["n"] - 1.0
        vm.pose(A["s"], ff, loop=A["loop"])

    def update_entity_anim(self, e, dt):
        """Simple state animation: death, shoot, slide, crouch, run, idle."""
        pm = e.pm
        if not pm:
            return

        def align_model_to_ground():
            if not self.airborne(e):
                z = pm.min_z()
                if math.isfinite(z):
                    e.pm_dz = -(self.world.feet_offset + z)

        A = e.anim
        if "state" not in A:
            A.update({k: v for k, v in fresh_anim().items() if k not in A})
        d = pm.d
        is_src = getattr(pm, "is_src_player", False)
        if not is_src:
            pm.tick(dt)
        wpn = CFG["weapons"][e.w] if 0 <= e.w < len(CFG["weapons"]) else None
        spd = math.hypot(e.vx, e.vy)
        in_air = self.airborne(e)
        crouch = e.cr > 0.5 or e.sl > 0
        A["gyaw"] = math.degrees(e.yaw)
        A["twist"] = 0.0

        # Death is one clip, played once and held on its last frame.
        if not e.alive:
            if is_src:
                if not A["dead"]:
                    A["dead"] = True
                    A["dt"] = 0.0
                    A["ddir"] = random.choice((-1.0, 1.0))
                    # fall style follows what the player was doing: sliding / crouching, else a random stand fall
                    A["dstyle"] = 4 if e.sl > 0 else (3 if e.cr > 0.5 else random.choice((0, 0, 1, 2)))
                A["dt"] += dt
                pm.animate_dead(A["dt"], A["ddir"], A.get("dstyle", 0))
            A["dead"] = True
            align_model_to_ground()
            return
        if A["dead"]:
            A["dead"] = False
            A["state"] = ""

        # Only Source player models are supported (posed procedurally).
        if is_src:
            self.update_src_player(e, pm, A, dt, spd, in_air, crouch, wpn)
            align_model_to_ground()

        # Non-Source models: leave T-pose / last pose (CS 1.6 path removed)

    def update_src_player(self, e, pm, A, dt, spd, in_air, crouch, wpn):
        """Drive states for Source / L4D2 player models (sequence clips or procedural IK)."""
        if e.reloadT > 0 and not e.isP:      # your own reload timer already runs in Game.update
            e.reloadT = max(0.0, e.reloadT - dt)
        melee = bool(wpn and wpn.melee)
        kick = max(0.0, min(1.0, e.shootT / 0.15)) if e.shootT > 0 else 0.0     # shot pulse (gun recoil / knife slash)
        e.shootT = max(0.0, e.shootT - dt)
        if e.reloadT > 0:
            A["rl_max"] = max(A.get("rl_max", 0.0), e.reloadT)
            reload = 1.0 - e.reloadT / max(A["rl_max"], 1e-3)
        else:
            A["rl_max"] = 0.0
            reload = None
        if e.sl > 0:
            state = "slide"
        elif crouch:
            state = "crouch"
        elif in_air:
            state = "jump"
        elif spd > 8:  # low threshold so bots/players start the run cycle quickly
            state = "run"
        else:
            state = "idle"
        A["state"] = state
        self.remote_wpn_sounds(e, A, wpn, reload)
        hold = None
        if HELD_WPN and wpn is not None and not melee:
            hp = held_points(wpn)
            if hp is not None:
                d_ = hp[1] - hp[0]
                hold = (float(d_[0]), float(d_[2]))     # how far forward / up the left hand is from the right hand
        pm.animate(dt, state, spd, e.pitch, kick, float(e.vz), melee, reload, hold)

    # ==========================================================================
    # mods: weapons, arms, player models, archives
    # ==========================================================================

    def _move_hit_sound(self):
        """mod/sound/movehit.(mp3|wav|ogg): played on every jumpbug / edgebug and repeated while pixelsurfing."""
        if not hasattr(self, "_mh_snd"):
            self._mh_snd = None
            try:
                for d in mod_folders("sound", "sounds"):
                    f = next((f for f in sorted(d.rglob("*")) if f.is_file() and f.stem.lower() == "movehit"
                              and f.suffix.lower() in (".mp3", ".wav", ".ogg")), None)
                    if f is not None:
                        self._mh_snd = pygame.mixer.Sound(str(f))
                        break
                else:
                    print("movehit sound not found: put movehit.mp3 in mod/sound")
            except Exception as ex:
                print("movehit sound failed:", ex)
        return self._mh_snd

    def play_move_hit(self):
        snd = self._move_hit_sound()
        if snd is not None:
            self.audio.play(1.0, snd, 0.0)

    def move_hit_interval(self):
        snd = self._move_hit_sound()
        try:
            return max(0.08, snd.get_length()) if snd is not None else 0.2
        except Exception:
            return 0.2

    def set_thirdperson(self, on):
        self.thirdperson = bool(on)
        P = self.P
        if P and not on:
            P.pm = None                 # free the model, it is only needed in third person
            P._pm_tried = False
            P._pm_team = None

    def toggle_thirdperson(self):
        self.set_thirdperson(not self.thirdperson)

    def key_aliases(self, raw):
        """The key itself plus the primary key of every action this key is a second bind for."""
        out = [raw]
        for a, k in self.binds2.items():
            p = self.binds.get(a)
            if k == raw and p and p not in out:
                out.append(p)
        return out

    def key_held(self, name):
        """True while `name` or a second key bound to the same action is physically held."""
        return any(name in self.key_aliases(p) for p in self.held_raw)

    def bind_assets(self):
        asset = self.asset
        msg = []
        names = [n for n in asset["mdl"] if not re.match(r"^[pw]_", n)]
        for i, w in enumerate(CFG["weapons"]):
            w.vm = None
            w.wvm = None
            pick = self.wm_pick.get(i)
            if pick not in asset["mdl"] or (pick and not self.mdl_ok_for_weapon(pick, w)):
                pick = None
                if i in self.wm_pick and self.wm_pick.get(i) not in (None, pick):
                    # drop invalid folder pick
                    if self.wm_pick.get(i) not in asset["mdl"] or not self.mdl_ok_for_weapon(self.wm_pick[i], w):
                        self.wm_pick.pop(i, None)
            allowed = [m for m in names if self.mdl_ok_for_weapon(m, w)]
            n = pick or (str(w.model).lower() if w.model else None)
            if n and n not in asset["mdl"]:
                n = None
            if n and not self.mdl_ok_for_weapon(n, w):
                n = None
            if n not in asset["mdl"] and not self.manual_mods:
                wk = key_norm(w.n)
                n = next((m for m in allowed if key_norm(re.sub(r"^v_", "", m)) == wk), None) or \
                    next((m for m in allowed if wk in key_norm(m)), None) or \
                    (w.match and next((m for m in allowed if re.search(w.match, m.lower())), None))
            if n and not w.model and not pick and bool(w.melee) != bool(re.search("knife", n)):
                n = None
            if n and not self.mdl_ok_for_weapon(n, w):
                n = None
            w._m = n if (n and n in asset["mdl"]) else None
        for w in CFG["weapons"]:
            if not w._m:
                continue
            try:
                w.vm = make_vm(asset["mdl"][w._m], asset["pack"].get(key_norm(w._m)), self.src_arms, not self.arms_off)
                o = w.ofs or [0, 0, 0]
                w.vm_ofs = (o[0], o[1], o[2])
                w.vm_flip = bool(w.flip) if w.flip is not None else w.vm.is_mirrored()
                try:   # the same weapon without any arms, for the players / bots that hold it
                    w.wvm = SrcInst(asset["mdl"][w._m].load(), None)
                except Exception as ex:
                    print("held-weapon model failed:", ex)
                    w.wvm = None
                msg.append(f"{w.n} <- {w._m}")
            except Exception as ex:
                msg.append(f"{w._m}: {ex}")
        wn = list(asset["wav"].keys())
        bad = re.compile(r"clip|bolt|draw|reload|slide|deploy|insert|empty|pull|release|cock|rattle")
        for i, w in enumerate(CFG["weapons"]):
            w.snd = None
            n = str(w.sound).lower() if w.sound else None
            if n not in asset["wav"] and not self.manual_mods:
                pk = self.wm_pick.get(i)
                stem = key_norm(re.sub(r"^v_", "", pk)) if pk in asset["mdl"] else None
                n = (stem and next((m for m in wn if not bad.search(m) and key_norm(m).startswith(stem)), None)) or \
                    (w.melee and next((m for m in wn if re.match(r"^knife.*(slash|miss)", m)), None)) or \
                    next((m for m in wn if not bad.search(m) and key_norm(m).startswith(key_norm(w.n))), None)
            if n and n in asset["wav"]:
                w.snd = asset["wav"][n]
                msg.append(f"{w.n} sound: {n}")
        # sounds shipped inside the weapon pack (.vpk / .zip / folder): use them for the weapon they belong to
        for w in CFG["weapons"]:
            if w.snd is None and w._m:
                try:
                    sn = self.pack_weapon_sound(w)
                except Exception as ex:
                    print("pack sound failed:", ex)
                    sn = None
                if sn is not None:
                    w.snd = sn
                    msg.append(f"{w.n} sound: from the pack")
        self.vm_msg = (" Weapons: " + ", ".join(msg) + ".") if msg else ""

    _SND_BAD = re.compile(r"clip|bolt|draw|reload|slide|deploy|insert|empty|pull|release|cock|rattle|pump|magin|magout|"
                          r"mag_|zoom|holster|ready|select|click|safety|shell|brass|impact|ricochet|bullet|"
                          r"step|foot|jump|land|death|pain|glass|flesh|wood|concrete|nearby")

    _SND_FIRE = re.compile(r"fire|shoot|shot|attack|primary|gunfire|slash|swing|miss|stab|(^|[^a-z])1$")

    def pack_sound_files(self, arch):
        """Audio files (.wav/.ogg/.mp3) inside a Source pack, cached."""
        cache = self.__dict__.setdefault("_pack_snd", {})
        k = id(arch)
        if k not in cache:
            cache[k] = [n for n in getattr(arch, "files", {})
                        if n.replace("\\", "/").lower().endswith((".wav", ".ogg", ".mp3"))
                        and (n.lower().startswith("sound/") or "/sound/" in n.lower())]
        return cache[k]

    def pack_weapon_sound(self, w):
        """Best firing sound for weapon w from the pack its view-model came from (or None)."""
        spec = self.asset["mdl"].get(w._m)
        arch = getattr(spec, "arch", None)
        if arch is None or not hasattr(arch, "read"):
            return None
        files = self.pack_sound_files(arch)
        if not files:
            return None
        base = re.sub(r"\.mdl$", "", str(w._m).lower())
        base = re.sub(r"\s*\(.*\)", "", base)
        base = re.sub(r"^v_", "", base)
        toks = {key_norm(base), key_norm(re.sub(r"^(rif|snip|smg|pist|shot|mach|pistol|rifle|sniper)_", "", base))}
        toks = {t for t in toks if len(t) >= 3}
        mates = [m for m, sp in self.asset["mdl"].items() if getattr(sp, "arch", None) is arch and not _is_arms_mdl(m)]
        scored = []
        for n in files:
            low = n.replace("\\", "/").lower()
            nm = low.rsplit("/", 1)[-1]
            stem = re.sub(r"\.\w+$", "", nm)
            if self._SND_BAD.search(stem) and not (w.melee and re.search(r"slash|swing|miss|stab", stem)):
                continue
            sc = 0
            pk = key_norm(low)
            if any(t in pk for t in toks):
                sc += 10
            elif len(mates) > 1:
                continue            # several weapons in this pack: only take sounds that name this one
            if self._SND_FIRE.search(stem):
                sc += 5
            if re.search(r"gunfire|/fire", low):
                sc += 5
            if re.search(r"incendiary|silenc|suppress|distant|/far|explo|alt", low):
                sc -= 8
            if w.melee and not re.search(r"slash|swing|miss|stab|knife", low):
                continue
            if not w.melee and re.search(r"knife|melee", low):
                continue
            scored.append((-sc, low, n))
        if not scored:
            return None
        scored.sort()
        n = scored[0][2]
        ck = self.__dict__.setdefault("_pack_snd_obj", {})
        key = (id(arch), n)
        if key not in ck:
            try:
                ck[key] = pygame.mixer.Sound(io.BytesIO(arch.read(n)))
            except Exception as ex:
                print("cannot load pack sound", n, ex)
                ck[key] = None
        return ck[key]

    def weapon_mod_folder(self, path):
        """Sub-folder under mod/weapon/ that this file lives in (e.g. 'ak-47'), or '' if none."""
        hint = getattr(self, "_load_folder_hint", None)
        if hint is not None:
            return hint
        try:
            path = Path(path).resolve()
        except Exception:
            path = Path(path)
        for base in mod_folders("weapon"):
            try:
                rel = path.relative_to(Path(base).resolve())
            except Exception:
                # also try without resolve (Windows junction / case quirks)
                try:
                    rel = Path(path).relative_to(base)
                except Exception:
                    continue
            if len(rel.parts) >= 2:
                return rel.parts[0].lower()
            # file sits directly in mod/weapon/ — no sub-folder
            return ""
        # last resort: look for .../weapon/<folder>/... in the path
        parts = [x.lower() for x in Path(path).parts]
        for i, part in enumerate(parts):
            if part in ("weapon", "weapons") and i + 1 < len(parts):
                nxt = parts[i + 1]
                if nxt and not nxt.endswith((".mdl", ".vpk", ".gma", ".bin", ".zip", ".7z", ".rar", ".part")):
                    return nxt
        return ""

    # mod/weapon/<folder>/ names that belong to each weapon (compared with key_norm, so "AK-47" == "ak47")
    WEAPON_FOLDER_ALIASES = {
        "ak47": {"ak47", "ak", "ak74", "akm", "kalash", "kalashnikov"},
        "awp": {"awp", "snip", "sniper", "scout", "ssg08", "ssg", "g3sg1", "g3sg", "awm", "barrett", "m24"},
        "knife": {"knife", "knives", "bayonet", "karambit", "melee", "blade", "dagger", "kunai"},
        "shotgun": {"shotgun", "shotguns", "nova", "xm1014", "xm", "mag7", "sawedoff", "sawed", "m3", "pump", "shotty"},
        "pistol": {"pistol", "pistols", "handgun", "glock", "usp", "p228", "p250", "fiveseven", "deagle",
                   "desserteagle", "deserteagle", "elite", "tec9", "revolver", "magnum"},
    }

    def weapon_kind(self, w):
        """Folder-group key for a weapon: 'ak47', 'awp', 'knife', 'shotgun', 'pistol' (or its own normalised name)."""
        if getattr(w, "melee", 0):
            return "knife"
        wn = key_norm(w.n or "")
        for kind, aliases in self.WEAPON_FOLDER_ALIASES.items():
            if wn == kind or wn in aliases:
                return kind
        return wn

    def folder_owner(self, folder):
        """Which weapon group a mod/weapon/<folder>/ belongs to, or None if the folder name is not a known weapon."""
        f = key_norm(folder or "")
        if not f:
            return None
        groups = {k: set(v) | {k} for k, v in self.WEAPON_FOLDER_ALIASES.items()}
        for w in CFG["weapons"]:           # custom / extra weapons: their own name is a folder name too
            k = self.weapon_kind(w)
            groups.setdefault(k, {k}).add(k)
        for k, aliases in groups.items():  # exact match first ("ak-47", "AWP", "knife" ...)
            if f in aliases:
                return k
        for k, aliases in groups.items():  # then "ak47 skins", "awp_dragonlore", "pistol-pack" ...
            if any(len(a) >= 3 and (f.startswith(a) or f.endswith(a)) for a in aliases):
                return k
        return None

    def weapon_accepts_folder(self, w, folder):
        """True if models from this mod/weapon/<folder>/ belong on weapon w.
        mod/weapon/ak-47/ -> only AK-47, mod/weapon/awp/ -> only AWP, and so on.
        Loose files (no sub-folder) and sub-folders with an unrecognised name stay available to every weapon."""
        if not folder:
            return True
        owner = self.folder_owner(folder)
        if owner is None:
            return True
        return owner == self.weapon_kind(w)

    def _recover_mdl_folder(self, name):
        """If mdl_folder was missing, try to recover it from the Source archive path."""
        folders = self.asset.setdefault("mdl_folder", {})
        if name in folders and folders[name]:
            return folders[name]
        spec = self.asset["mdl"].get(name)
        path = None
        if isinstance(spec, SrcSpec):
            arch = spec.arch
            if hasattr(arch, "path") and arch.path:
                path = arch.path
            elif hasattr(arch, "roots") and arch.roots:
                path = arch.roots[0]
            elif hasattr(arch, "paths") and arch.paths:
                # LooseSrcFiles: any known path
                try:
                    path = next(iter(arch.paths.values()))
                except Exception:
                    path = None
        if path is not None:
            # temporarily clear hint so we resolve from the real path
            prev = getattr(self, "_load_folder_hint", None)
            self._load_folder_hint = None
            try:
                folder = self.weapon_mod_folder(path)
            finally:
                self._load_folder_hint = prev
            if folder:
                folders[name] = folder
                return folder
        return folders.get(name, "")

    def mdl_ok_for_weapon(self, name, w):
        """A model is usable on weapon w only if it sits in that weapon's mod/weapon/<folder>/."""
        folder = self.asset.get("mdl_folder", {}).get(name, "")
        if not folder:
            folder = self._recover_mdl_folder(name)
        return self.weapon_accepts_folder(w, folder)

    def weapon_mods(self, slot=None):
        """Loaded weapon view-models for the Skins tab.
        If slot is given, only models from that weapon's folder (mod/weapon/<name>/) are listed."""
        names = [n for n in self.asset["mdl"]
                 if not re.match(r"^[pw]_", n) and not _is_arms_mdl(n) and n not in self.arms_all]
        if slot is not None and 0 <= slot < len(CFG["weapons"]):
            w = CFG["weapons"][slot]
            names = [n for n in names if self.mdl_ok_for_weapon(n, w)]
        return sorted(names)

    def load_src_vpk(self, path):
        """Any Source .vpk or Garry's Mod .gma: view-models (L4D2 models/v_models/v_*.mdl, GMod v_* / c_* and the
        SWEP.ViewModel of the addon's weapon scripts), players, arms/hands.
        Only models, textures and animations are used - weapon stats stay the game's own.
        Incomplete downloads (.vpk.part) are accepted for whatever entries are fully present."""
        path = Path(path)
        gma = is_gma_path(path)
        arch = GmaArchive(path) if gma else VpkArchive(path)
        names = src_scan(arch)
        pls = src_players(arch)
        arms = src_arms_all(arch)
        if not names and not pls and not arms:
            hint = ""
            if getattr(arch, "incomplete", False):
                hint = " VPK looks truncated (re-download the full file)."
            elif gma and arch.has_maps():
                hint = " This addon is a map - open it in saw_map_editor.py instead."
            elif any(n.endswith(".mdl") for n in arch.files):
                hint = " Found .mdl files but none are usable view-models (need matching .vvd/.vtx)."
            return f"{path.name}: no weapon, player or arms/hands model inside.{hint}"
        tag = path.stem[:18]
        # strip browser temp suffixes from the display tag
        tag = re.sub(r"\.(part|crdownload|tmp)$", "", tag, flags=re.I)[:18]
        folder = self.weapon_mod_folder(path)
        bases = {}
        for nm in names:
            bases[nm.rsplit("/", 1)[-1]] = bases.get(nm.rsplit("/", 1)[-1], 0) + 1
        for nm in names:
            key = nm.rsplit("/", 1)[-1]
            if gma and bases[key] > 1 and "/" in nm:      # same file name in two folders of one addon: keep both
                key = nm.rsplit("/", 2)[-2] + "_" + key
            old = self.asset["mdl"].get(key)
            if old is not None and not (isinstance(old, SrcSpec) and old.arch is arch and old.mdl == nm):
                key = re.sub(r"\.mdl$", "", key) + " (" + tag + ").mdl"
            self.asset["mdl"][key] = SrcSpec(arch, nm)
            self.asset.setdefault("mdl_folder", {})[key] = folder
        for nm in pls:
            self.add_src_player(nm.rsplit("/", 1)[-1], SrcSpec(arch, nm), tag)
        self._new_pl += len(pls)
        for nm in arms:
            # only register arms whose mesh is complete
            if hasattr(arch, "complete") and not arch.complete(nm):
                continue
            self.add_arms(nm.rsplit("/", 1)[-1], SrcSpec(arch, nm), tag)
        parts = []
        if names:
            l4d = sum(1 for n in names if "/v_models/" in n.replace("\\", "/").lower())
            if l4d == len(names):
                parts.append(f"{len(names)} L4D2 view-model(s)")
            elif l4d:
                parts.append(f"{len(names)} view-model(s) ({l4d} L4D2)")
            else:
                parts.append(f"{len(names)} view-model(s)")
        if pls:
            n_l4d = sum(1 for n in pls if re.search(r"/(infected|survivors?)/", n.replace("\\", "/").lower()))
            if n_l4d == len(pls):
                parts.append(f"{len(pls)} L4D2 player model(s)")
            elif n_l4d:
                parts.append(f"{len(pls)} player model(s) ({n_l4d} L4D2)")
            else:
                parts.append(f"{len(pls)} player model(s)")
        n_arms = sum(1 for n in arms if not hasattr(arch, "complete") or arch.complete(n))
        if n_arms:
            parts.append(f"{n_arms} arms/hands model(s)")
        msg = f"{path.name}: {' + '.join(parts)} - pick them in Skins."
        if getattr(arch, "incomplete", False):
            msg += " (package incomplete — re-download the full .vpk for missing models/textures)"
        if gma:
            msg = msg.replace(" - pick them in Skins.", " (Garry's Mod addon) - pick them in Skins.")
        return msg

    def add_src_player(self, name, spec, tag=""):
        """Register a Source (L4D2) player model in asset['player']; returns its key."""
        key = name
        old = self.asset["player"].get(key)
        if old is not None and not (isinstance(old, SrcSpec) and old.arch is spec.arch and old.mdl == spec.mdl):
            key = re.sub(r"\.mdl$", "", name) + " (" + (tag or "mod")[:14] + ").mdl"
        self.asset["player"][key] = spec
        self.pm_cache.pop(key, None)
        for tk, sv in self.pm_saved.items():
            if sv == key and not self.pm_cfg[tk]:
                self.pm_cfg[tk] = key
        return key

    def add_arms(self, name, spec, tag=""):
        """Register an arms/hands/gloves model. name is the file basename; tag disambiguates packs."""
        key = name
        old = self.arms_all.get(key)
        if old is not None and not (isinstance(old, SrcSpec) and old.arch is spec.arch and old.mdl == spec.mdl):
            key = re.sub(r"\.mdl$", "", name) + " (" + (tag or "mod")[:14] + ").mdl"
        self.arms_all[key] = spec
        if self.arms_off:
            return key
        if (self.arms_want and key == self.arms_want) or (self.arms_want and name == self.arms_want) \
                or (not self.arms_want and self.arms_pick is None and not self.manual_mods):
            self.arms_pick, self.src_arms = key, spec
        return key

    def load_arms_folder(self):
        """Scan arms/ folders and load every arms/hands pack found.

        Looks in <script_dir>/mod/arms/ (or <cwd>/mod/arms/). Never creates folders.
        Accepts .vpk, .vpk.part, .zip, .7z, .rar, and loose .mdl (+ companions).
        """
        uniq_roots = mod_folders("arms")
        if not uniq_roots:
            return "No mod/arms folder found."

        exts = (".vpk", ".gma", ".bin", ".zip", ".7z", ".rar", ".mdl")
        files = []
        for root in uniq_roots:
            for f in sorted(root.rglob("*")):
                if not f.is_file():
                    continue
                n = f.name.lower()
                if f.suffix.lower() == ".bin" and not is_gma_path(f):
                    continue
                if f.suffix.lower() in exts or n.endswith(".vpk.part"):
                    files.append(f)

        if not files:
            return "mod/arms is empty — drop .vpk / .mdl packs into it."

        before = set(self.arms_all.keys())
        # Load each file; VPKs/archives go through normal Source loader
        status_parts = []
        for f in files:
            try:
                st = self.load_files([f], "weapon")
                if st:
                    status_parts.append(st)
            except Exception as ex:
                status_parts.append(f"{f.name}: {ex}")

        # Second pass: any VPK in arms/ that registered only as a weapon but looks like hands
        for f in files:
            n = f.name.lower()
            if not (n.endswith(".vpk") or n.endswith(".vpk.part")):
                continue
            try:
                arch = VpkArchive(f)
            except Exception:
                continue
            for nm in src_arms_all(arch):
                if hasattr(arch, "complete") and not arch.complete(nm):
                    continue
                key = nm.rsplit("/", 1)[-1]
                if key not in self.arms_all:
                    self.add_arms(key, SrcSpec(arch, nm), f.stem[:18])
            # Aggressive: in arms/ folder, any v_*.mdl with hand/arm bones counts as arms
            for nm in arch.files:
                low = nm.replace("\\", "/").lower()
                if not low.endswith(".mdl"):
                    continue
                if _is_arms_mdl(nm):
                    continue  # already handled
                b = low.rsplit("/", 1)[-1]
                if re.match(r"^w_", b) or "/w_models/" in low:
                    continue
                try:
                    buf = arch.read(nm, 0, min(65536, arch.size(nm)))
                except Exception:
                    continue
                if _mdl_looks_like_arms(buf) or re.search(r"arms?|hands?|glove|sleeve", b):
                    if hasattr(arch, "complete") and not arch.complete(nm):
                        continue
                    self.add_arms(b, SrcSpec(arch, nm), f.stem[:18])

        after = set(self.arms_all.keys())
        added = sorted(after - before)

        # Auto-equip default if nothing selected
        if self.arms_all and self.arms_pick is None and not self.arms_off and not self.manual_mods:
            want = self.arms_want
            if want and want in self.arms_all:
                self.set_arms(want)
            else:
                prefer = ("v_arms_zoey.mdl", "v_arms_bill.mdl", "v_arms_francis.mdl",
                          "v_arms_louis.mdl", "v_arms_coach_new.mdl", "v_arms_gambler_new.mdl",
                          "v_arms_mechanic_new.mdl", "v_arms_producer_new.mdl",
                          "t_arms.mdl", "c_arms.mdl", "v_arms.mdl")
                pick = next((n for n in prefer if n in self.arms_all), None)
                if pick is None:
                    pick = sorted(self.arms_all.keys())[0]
                self.set_arms(pick)
        elif self.arms_want and self.arms_want in self.arms_all and self.arms_pick != self.arms_want:
            self.set_arms(self.arms_want)

        roots_s = ", ".join(str(r) for r in uniq_roots)
        if added:
            msg = f"Arms folder ({roots_s}): {len(added)} model(s) — {', '.join(added[:6])}{'…' if len(added) > 6 else ''}."
        else:
            msg = f"Arms folder ({roots_s}): scanned {len(files)} file(s), {len(self.arms_all)} arms total."
        if self.arms_pick:
            msg += f" Equipped: {self.arms_pick}."
        return msg

    def set_arms(self, name):
        """Equip an arms model (name from arms_all) or None = no arms."""
        self.arms_pick = name if name in self.arms_all else None
        self.arms_want = self.arms_pick
        self.arms_off = self.arms_pick is None
        self.src_arms = self.arms_all.get(self.arms_pick)
        try:
            self.bind_assets()
        except Exception as ex:
            print("set_arms: rebuilding weapons failed:", ex)
            self.src_arms = None
            self.bind_assets()

    def load_src_archive(self, path):
        """.zip / .7z / .rar with weapon, arms or player model files (models, materials, or a .vpk inside).
        Only the needed parts are unpacked (once) into the temp folder, then loaded like loose files."""
        import tempfile
        path = Path(path)
        out = Path(tempfile.gettempdir()) / "akb_arena_src" / (re.sub(r"[^A-Za-z0-9_.-]", "_", path.stem) + "_%d" % path.stat().st_size)

        def want(nm):
            nm = nm.replace("\\", "/").lower()
            parts = nm.split("/")
            return nm.endswith((".vpk", ".gma", ".bin")) or (not nm.endswith("/") and ("models" in parts[:-1] or "materials" in parts[:-1] or "sound" in parts[:-1]))
        if not (out / ".done").exists():
            out.mkdir(parents=True, exist_ok=True)
            if path.suffix.lower() == ".rar":
                extract_rar(path, out)
            elif path.suffix.lower() == ".7z":
                try:
                    import py7zr
                except Exception:
                    import subprocess
                    subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "py7zr"])
                    try:
                        import importlib
                        importlib.invalidate_caches()
                        import py7zr
                    except Exception:
                        return f"{path.name}: .7z needs py7zr. Run: pip install py7zr  (or unzip it yourself)"
                with py7zr.SevenZipFile(str(path)) as z:
                    names = [n for n in z.getnames() if want(n)]
                    z.extract(path=str(out), targets=names)
            else:
                import zipfile
                with zipfile.ZipFile(str(path)) as z:
                    for i in z.infolist():
                        if not i.is_dir() and want(i.filename):
                            z.extract(i, str(out))
            (out / ".done").write_text("ok")
        files = [f for f in out.rglob("*") if f.is_file() and f.suffix.lower() in (".mdl", ".vpk", ".gma", ".bin")
                 and f.name.lower().count(".mdl") <= 1 and (f.suffix.lower() != ".bin" or is_gma_path(f))]
        if not files:
            return f"{path.name}: no weapon, arms or player model inside."
        # keep the mod/weapon/<folder> tag from the archive's original path
        prev = getattr(self, "_load_folder_hint", None)
        folder = ""
        for base in mod_folders("weapon"):
            try:
                rel = path.resolve().relative_to(base.resolve())
                if len(rel.parts) >= 2:
                    folder = rel.parts[0].lower()
                break
            except Exception:
                continue
        self._load_folder_hint = folder
        try:
            return self.load_files(files, "weapon") or f"{path.name}: loaded."
        finally:
            self._load_folder_hint = prev

    def set_weapon_pick(self, slot, name):
        if name:
            if 0 <= slot < len(CFG["weapons"]) and not self.mdl_ok_for_weapon(name, CFG["weapons"][slot]):
                self.vm_msg = f"{name} is not for {CFG['weapons'][slot].n} (wrong folder)."
                return
            self.wm_pick[slot] = name
        else:
            self.wm_pick.pop(slot, None)
        self.bind_assets()

    def load_files(self, paths, mode):
        """Returns a status string."""
        status = None
        map_chg = wpn_chg = cfg_chg = pm_chg = False
        for p in sorted(paths, key=lambda q: len(Path(q).parts)):
            p = Path(p)
            n = p.name.lower()
            if n.endswith(".fadmap"):
                status = self.load_map(p)
                continue
            # L4D2 weapon packs (.vpk, or browser partial downloads .vpk.part)
            if n.endswith((".vpk", ".gma")) or (n.endswith(".bin") and is_gma_path(p)) or n.endswith(".vpk.part") or re.search(r"\.vpk\.(part|crdownload|tmp)$", n):
                try:
                    st = self.load_src_vpk(p)
                    status = st or status
                    wpn_chg = True
                    if self._new_pl:
                        pm_chg, self._new_pl = True, 0
                except Exception as ex:
                    status = f"Cannot open {p.name}: {ex}"
                continue
            if n.endswith((".zip", ".7z", ".rar")):  # archive with L4D2 weapons, arms and / or player models
                try:
                    st = self.load_src_archive(p)
                    status = st or status
                    wpn_chg = True
                except Exception as ex:
                    status = f"Cannot open {p.name}: {ex}"
                continue
            if n.endswith((".vvd", ".vtx", ".vtf", ".vmt", ".ani")):
                # Companion mesh files (picked up next to their .mdl); .ani ignored for now
                continue
            try:
                data = p.read_bytes()
            except Exception as ex:
                return f"Cannot read {p.name}: {ex}"
            if n.endswith(".mdl"):
                srcv = len(data) > 250 and data[:4] == b"IDST" and 44 <= struct.unpack_from("<i", data, 4)[0] <= 49
                if mode == "player" or (srcv and is_src_player(src_rel(p))):
                    pm_chg = True
                else:
                    wpn_chg = True
            elif n.endswith(".json"):
                wpn_chg = cfg_chg = True
            elif n.endswith(".wav"):
                wpn_chg = True
            if n.endswith(".json"):
                try:
                    j = json.loads(data.decode("utf-8"))
                    for i, wd in enumerate(j.get("weapons", [])):
                        while len(CFG["weapons"]) <= i:
                            CFG["weapons"].append(Wpn(n="Gun", dmg=30, rpm=300, mag=20, spr=.02, auto=1))
                        CFG["weapons"][i].__dict__.update(wd)
                    if j.get("frags"):
                        CFG["frags"] = int(j["frags"])
                except Exception as ex:
                    status = "Bad JSON: " + str(ex)
            elif n.endswith(".wav"):
                try:
                    self.asset["wav"][n] = pygame.mixer.Sound(io.BytesIO(data))
                except Exception:
                    pass
            elif n.endswith(".mdl"):
                if len(data) > 250 and data[:4] == b"IDST" and 44 <= struct.unpack_from("<i", data, 4)[0] <= 49:
                    # L4D2 .mdl (its .vvd/.vtx/.vtf/.vmt are found in the same mod folder)
                    root = src_root(p)
                    arch = self.src_dirs.get(root)
                    if arch is None:
                        arch = self.src_dirs[root] = DirSrcFiles(root)
                    rel = src_rel(p)
                    spec = SrcSpec(arch, rel)
                    if is_src_player(rel) or (mode == "player" and not re.search(r"arms|hands|ragdoll|^[vwp]_", n)):
                        self.add_src_player(n, spec, root.name or "mod")     # player model, posed in code
                        continue
                    if mode == "player":
                        continue
                    if re.search(r"_zoom|_scope|display|dropped|^w_", n):
                        continue
                    key = n
                    old = self.asset["mdl"].get(key)
                    if old is not None and not (isinstance(old, SrcSpec) and old.arch is arch and old.mdl == rel):
                        key = re.sub(r"\.mdl$", "", n) + " (" + (root.name or "mod")[:14] + ").mdl"
                    # Any arms/hands/gloves mesh (by name, path, or bone layout)
                    if _is_arms_mdl(rel) or _is_arms_mdl(n) or _mdl_looks_like_arms(data):
                        self.add_arms(key, spec, root.name or "mod")
                    else:
                        self.asset["mdl"][key] = spec
                        self.asset.setdefault("mdl_folder", {})[key] = self.weapon_mod_folder(p)
                    continue
                # Reject GoldSrc / CS 1.6 (MDL v10) and unknown formats
                status = f"{n}: only Source engine models (MDL v44-49) are supported."
                continue
        if pm_chg and not map_chg and not wpn_chg:
            self.reset_game()
            return f"{len(self.asset['player'])} player model(s) loaded. Pick them below."
        if wpn_chg:
            self.bind_assets()
            if not map_chg:
                if cfg_chg or pm_chg:
                    self.reset_game()
                nsrc = sum(isinstance(v, SrcSpec) for v in self.asset["mdl"].values())
                if not self.vm_msg and nsrc:
                    return status or f"{nsrc} Source view-model(s) loaded - pick them in Skins."
                return (self.vm_msg or "No weapon files matched.").strip() if not status else status
        return status or ""

    # ==========================================================================
    # maps
    # ==========================================================================

    def load_map(self, path):
        """Load a .fadmap made with the FAD-MOD Map Builder. Returns a status string."""
        path = Path(path)
        if self.net.active:
            return "Leave the online server before loading a map."
        try:
            raw = path.read_bytes()
            world = FadMapWorld(json.loads(raw.decode("utf-8")))
        except Exception as ex:
            return f"Cannot load map {path.name}: {ex}"
        self.files["map_raw"] = raw          # used for the online map signature
        self.set_world(world)
        self.remember_map(raw)
        extra = ", Source map" if getattr(world, "_bsp", None) else ""
        return f"Map loaded: {world.name} ({len(world.B)} boxes{extra})."

    # --- online: find a map by its signature so joining a custom-map server just works ---
    @staticmethod
    def _map_dirs():
        base = Path(__file__).resolve().parent
        dirs = list(mod_folders("maps", "map"))
        dirs.append(base / "mod" / "maps")
        dirs.append(base / "assets" / "maps")
        dirs.append(base / "mod")
        out, seen = [], set()
        for d in dirs:
            try:
                k = str(d.resolve())
                if d.is_dir() and k not in seen:
                    seen.add(k)
                    out.append(d)
            except Exception:
                pass
        return out

    def remember_map(self, raw):
        """Keep a copy of every map you load in mod/maps (named by its signature) so it can be re-found."""
        try:
            sig = hashlib.sha1(raw).hexdigest()[:10]
            d = Path(__file__).resolve().parent / "mod" / "maps"
            if not d.is_dir():          # never create folders: only keep a copy if mod/maps already exists
                return
            p = d / (sig + ".fadmap")
            if not p.exists():
                p.write_bytes(raw)
        except Exception:
            pass

    def find_map_by_sig(self, sig):
        """Path of a local .fadmap whose signature matches `sig`, or None."""
        for d in self._map_dirs():
            try:
                files = list(d.rglob("*.fadmap"))
            except Exception:
                continue
            for p in files:
                try:
                    if hashlib.sha1(p.read_bytes()).hexdigest()[:10] == sig:
                        return p
                except Exception:
                    pass
        return None

    def use_map_sig(self, sig):
        """Make our world match the server's map. Returns (ok, message)."""
        if sig == self.map_sig():
            return True, ""
        if self.net.active:
            self.net.leave()          # maps can only change while offline
        if sig in ("none", "arena"):      # "arena" = servers from older versions that used the built-in map
            return False, "This server has no map loaded that you can use."
        p = self.find_map_by_sig(sig)
        if p is None:
            return False, ("You don't have this server's map (custom_%s). Get the .fadmap from the host "
                           "and load it (or drop it in mod/maps), then join again." % sig[:5])
        msg = self.load_map(p)
        if self.map_sig() != sig:
            return False, "Could not load the server's map: " + msg
        return True, "Found the server's map (%s) - loaded it." % p.name

    # ==========================================================================
    # per-frame update
    # ==========================================================================

    def update(self, dt):
        P = self.P
        P.took_damage_this_frame = False
        if self.con_open and self.net.active:    # joined an online game with the console open: close it
            self.con_toggle()
        self.net_update(dt)
        P = self.P
        K = self.keys
        B = self.binds
        # Regenerate health after 5 seconds without taking damage. A low-health
        # player returns to full in about 4 seconds; near-full health recovers in 2.
        if P.alive and not self.over and P.hp < 200 and not P.took_damage_this_frame:
            if P.regen_delay > 0:
                P.regen_delay = max(0.0, P.regen_delay - dt)
            else:
                hp_ratio = max(0.0, min(1.0, P.hp / 200.0))
                full_heal_time = 4.0 - 2.0 * hp_ratio
                P.hp = min(200.0, P.hp + (200.0 - P.hp) / max(2.0, full_heal_time) * dt)
        if (self.lock or self.net.active) and not self.over:   # online: the world never pauses
            for e in self.ents:
                e.cool -= dt
                if e.remote:
                    self.remote_tick(e, dt)
                    continue
                if not e.alive:
                    e.resp -= dt
                    if e.resp <= 0:
                        self.spawn(e)
                    continue
                vz_ = getattr(self.world, "void_z", None)
                if vz_ is not None and e.z < vz_:      # fell off the map
                    self.spawn(e)
                    continue
                if e.isP:
                    if getattr(e, "noclip", False):
                        # fly through walls: WASD + jump/crouch
                        f = (1 if K.get(B["forward"]) else 0) - (1 if K.get(B["backward"]) else 0)
                        s = (1 if K.get(B["right"]) else 0) - (1 if K.get(B["left"]) else 0)
                        up = (1 if K.get(B["jump"]) else 0) - (1 if K.get(B["crouch"]) else 0)
                        n = math.hypot(f, s) or 1
                        c, sn = math.cos(e.yaw), math.sin(e.yaw)
                        cp, sp_ = math.cos(e.pitch), math.sin(e.pitch)
                        spd = 450.0
                        e.px, e.py, e.pz = e.x, e.y, e.z
                        e.x += ((c * cp) * f + sn * s) / n * spd * dt if (f or s) else 0
                        e.y += ((sn * cp) * f - c * s) / n * spd * dt if (f or s) else 0
                        e.z += (sp_ * f / n * spd + up * spd) * dt
                        e.vx = e.vy = e.vz = 0.0
                        e.ladder = None
                        e.walk = True
                        e.so = 0.0
                        if K.get("mouse1") and (CFG["weapons"][e.w].auto or not e.held):
                            self.fire(e)
                            e.held = 1
                        if not K.get("mouse1"):
                            e.held = 0
                    else:
                        f = (1 if K.get(B["forward"]) else 0) - (1 if K.get(B["backward"]) else 0)
                        s = (1 if K.get(B["right"]) else 0) - (1 if K.get(B["left"]) else 0)
                        n = math.hypot(f, s) or 1
                        c, sn = math.cos(e.yaw), math.sin(e.yaw)
                        e.walk = self.walkable(e)
                        gr = e.walk
                        e.sl = 0.0
                        e.aS = 0
                        e.jc = bool(K.get(B["crouch"]))     # crouch held (jump-crouch: feet pull up in the air)
                        e.acc += dt
                        while e.acc >= TICK:
                            e.acc -= TICK
                            e.px, e.py, e.pz = e.x, e.y, e.z
                            self.ptick(e, TICK, f, s, n, c, sn)
                        crouch = bool(K.get(B["crouch"])) and e.sl <= 0
                        e.cr += ((1.0 if crouch else 0.0) - e.cr) * min(1.0, dt * 14)
                        e.slk += ((1.0 if e.sl > 0 else 0.0) - e.slk) * min(1.0, dt * (9 if e.sl > 0 else 6))
                        cdrop = 11 * e.cr
                        e.so = -(cdrop + (16 - cdrop) * e.slk)
                        if K.get("mouse1") and (CFG["weapons"][e.w].auto or not e.held):
                            self.fire(e)
                            e.held = 1
                        if not K.get("mouse1"):
                            e.held = 0
                else:
                    self.dummy_tick(e, dt)
            # CS-style player/bot solid hulls + run-into toss
            self.resolve_player_collisions(dt)
        elif self.over:
            self.over -= dt
            if self.over <= 0:
                self.over = 0.0
                self.reset_game()
                P = self.P

        zoomed = bool(P.zoom and P.w == 0)
        target = 20.0 if zoomed else self.fov_cfg            # world: follows the FOV slider
        vm_target = 20.0 if zoomed else 75.0                 # viewmodel: fixed, ignores the slider
        if abs(self.fov - target) > .1:
            self.fov += (target - self.fov) * min(1, dt * 20)
        else:
            self.fov = target
        if abs(self.vm_fov - vm_target) > .1:
            self.vm_fov += (vm_target - self.vm_fov) * min(1, dt * 20)
        else:
            self.vm_fov = vm_target

        # viewmodel mouse lag: gun lags behind look direction, then springs back
        _vb = max(0.0, min(2.0, float(self.viewbob)))
        if self._vm_lyaw is None:
            self._vm_lyaw, self._vm_lpitch = P.yaw, P.pitch
        dyaw = P.yaw - self._vm_lyaw
        # wrap yaw delta into -pi..pi
        dyaw = (dyaw + math.pi) % (2 * math.pi) - math.pi
        dpitch = P.pitch - self._vm_lpitch
        self._vm_lyaw, self._vm_lpitch = P.yaw, P.pitch
        # add lag from mouse motion (gun moves opposite to look) — light touch
        self.vm_lag_y += dyaw * 3.5 * _vb
        self.vm_lag_p += dpitch * 2.5 * _vb
        # clamp so it doesn't fly off screen
        self.vm_lag_y = max(-2.2, min(2.2, self.vm_lag_y))
        self.vm_lag_p = max(-1.4, min(1.4, self.vm_lag_p))
        # spring back to centre (snappier recovery)
        k = min(1.0, dt * 16.0)
        self.vm_lag_y *= (1.0 - k)
        self.vm_lag_p *= (1.0 - k)
        if abs(self.vm_lag_y) < 0.01:
            self.vm_lag_y = 0.0
        if abs(self.vm_lag_p) < 0.01:
            self.vm_lag_p = 0.0

        if P.sprayw:
            P.since += dt
            if P.since > 0.12:
                P.spray = max(0.0, P.spray - dt * 18)      # spray pattern resets when you stop shooting
            k = min(1.0, dt * 25)
            dp, dy = P.pend_p * k, P.pend_y * k
            P.pitch = max(-1.5, min(1.5, P.pitch + dp))
            P.yaw += dy
            P.pend_p -= dp
            P.pend_y -= dy
            if P.spray <= 0 and abs(P.pend_p) < 1e-5 and abs(P.pend_y) < 1e-5:
                P.sprayw = False
        else:
            k = math.exp(-dt * 3.5)       # single-shot recoil settles back down
            P.pun_p *= k
            P.pun_y *= k
        if P.reloadT > 0:
            P.reloadT -= dt
            P.zoom = 0   # reloading always scopes out
        for e in self.ents:
            if e.pm:
                # posing a model is the heaviest per-bot cost: close bots update often, far ones rarely
                e.aacc += dt
                d2 = (e.x - P.x) ** 2 + (e.y - P.y) ** 2
                step = ANIM_STEP   # every player model is posed at ANIM_HZ, however far away
                if e.aacc >= step:
                    self.update_entity_anim(e, e.aacc)
                    e.aacc = 0.0
            spd = math.hypot(e.vx, e.vy)
            ground = e.walk if e.isP else not self.airborne(e)
            in_air = e.alive and not ground
            run = e.alive and e.sl <= 0 and spd > 10 and ground
            if in_air:
                e.pvz = e.vz
            elif e.was_air and e.alive:
                e.land = min(1.0, abs(e.pvz) / 450)   # landing impact
            e.was_air = in_air
            e.air += ((1.0 if in_air else 0.0) - e.air) * min(1.0, dt * 14)
            e.land = max(0.0, e.land - dt * 4)
            if run:
                e.ph += dt * spd * 0.03
            e.bob += ((min(1.0, spd / MAXS) if run else 0.0) - e.bob) * min(1.0, dt * 10)
            if not run and e.bob < 0.02:
                e.bob = 0.0
        for t in self.tracers[:]:
            t["t"] -= dt
            if t["t"] <= 0:
                self.tracers.remove(t)
        self.hm = max(0.0, self.hm - dt)
        self.sc_update(dt)
        for c_ in self.chat:
            c_["t"] -= dt
        self.chat = [c_ for c_ in self.chat if c_["t"] > 0]
        self.flash = max(0.0, self.flash - dt * 2.5)
        self.shake = max(0.0, self.shake - dt * SHAKE_DECAY * max(0.2, self.shake))
        cvm = CFG["weapons"][P.w].vm
        if cvm and P.alive and not (P.zoom and P.w == 0):
            self.vm_tick(cvm, dt)

