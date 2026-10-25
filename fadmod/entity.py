"""Player / bot entity."""
import math
import random

from .config import *

class Ent:
    def __init__(self, team, is_p, name):
        self.team = team
        self.isP = is_p
        self.name = name
        self.hp = 100
        self.regen_delay = 5.0  # seconds without taking damage before health regeneration
        self.took_damage_this_frame = False
        self.alive = 1
        self.x = self.y = self.z = 0.0
        self.vx = self.vy = self.vz = 0.0
        self.px = self.py = self.pz = 0.0
        self.yaw = self.pitch = 0.0
        self.w = 0
        self.ammo = [w.mag for w in CFG["weapons"]]
        self.cool = 0.0
        self.resp = 0.0
        self.aacc = 0.0                   # animation time accumulator (animations are throttled)
        self.pun_p = self.pun_y = 0.0     # recoil punch (radians) added to the view / bullet direction
        self.pend_p = self.pend_y = 0.0   # spray recoil still to be applied to the aim
        self.spray = 0.0                  # bullets fired in the current spray
        self.since = 9.0                  # seconds since the last spray shot
        self.sprayw = False               # recoil currently follows the spray pattern
        self.zoom = 0
        self.acc = 0.0
        self.jreq = 0
        self.sl = 0.0
        self.scd = 0.0
        self.so = 0.0
        self.slk = 0.0   # eased slide pose 0..1 (camera drop, lean, viewmodel)
        self.cr = 0.0
        self.wj = 0
        self.wjt = 99.0           # seconds since the last wall kick (animation)
        self.wjn = (0.0, 0.0)     # normal of the wall it kicked off (points away from the wall)
        self.cHeld = False
        self.aS = 0
        self.sdx = self.sdy = 0.0
        self.sdyaw = None
        self.held = 0
        self.kills = 0
        self.deaths = 0
        self.walk = False  # standing on walkable ground
        self.steep = False  # touching a too-steep slope (you just slide down it)
        self.edgebug = False  # last move cancelled vertical velocity while airborne (CS edgebug)
        self.jumpbug = False  # jumped on the landing tick (CS jumpbug)
        self.was_walk = False  # walkable state from previous physics tick
        self.land_ticks = 99
        self.pxsurf = False
        self.ladder = None   # (nx, ny) normal of the ladder being climbed, None = not on a ladder
        self.jb_tick = -999
        self.eb_cd = 0.0
        self._tick = 0
        self.shootT = 0.0
        self.reloadT = 0.0
        self.remote = False     # online player driven by network state
        self.home = None        # (x, y, z, yaw, weapon) for bots added with bot_add: they respawn there, not at the map spawn points
        self.nid = -1
        self.tx = self.ty = self.tz = None
        self.tyaw = self.tpitch = 0.0
        self.ph = 0.0   # run-cycle phase
        self.bob = 0.0  # run-cycle strength 0..1
        self.air = 0.0  # jump pose strength 0..1
        self.land = 0.0  # landing squash 0..1
        self.pvz = 0.0
        self.was_air = False
        self.pm = None
        self.pm_dz = 0.0
        self.anim = fresh_anim()

