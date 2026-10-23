"""FAD-MOD configuration, constants, and small pure helpers."""
import math
import re
import sys
from pathlib import Path

# Settings file lives next to the entry-point script / executable
if getattr(sys, "frozen", False):
    SETTINGS_FILE = Path(sys.executable).with_name("settings.json")
else:
    # Prefer the package's parent (project root) so settings.json sits beside main.py
    SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

def _find_sub(parent, names):
    """Existing sub-folder of `parent` whose name matches one of `names` (case-insensitive)."""
    try:
        for d in sorted(parent.iterdir()):
            if d.is_dir() and d.name.lower() in names:
                return d
    except Exception:
        pass
    return None


def mod_folders(*names):
    """Existing folders called one of `names` inside the "mod" folder (next to this script, or in cwd).
    Never creates anything - if the folder is not there, the list is empty."""
    names = tuple(n.lower() for n in names)
    out, seen = [], set()
    # package dir, project root (parent of package), and cwd
    for base in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent, Path.cwd()):
        mroot = _find_sub(base, ("mod",))
        if not mroot:
            continue
        d = _find_sub(mroot, names)
        if d:
            k = str(d.resolve())
            if k not in seen:
                seen.add(k)
                out.append(d)
    return out

DEFAULT_BINDS = {
    "forward": "w", "backward": "s", "left": "a", "right": "d",
    "jump": "space", "reload": "r",
    "jumpbug": "z", "edgebug": "x", "pixelsurf": "c",
    "weapon1": "1", "weapon2": "2",
    "swaphand": "v", "crouch": "left ctrl", "melee": "e",
    "console": "`",
}
DEFAULT_XH = {"size": 6, "thick": 2, "gap": 3, "dot": 0, "outline": 1, "r": 0, "g": 255, "b": 0, "a": 1.0}
BIND_META = [
    ("forward", "Move forward"), ("backward", "Move backward"),
    ("left", "Move left"), ("right", "Move right"),
    ("jump", "Jump (hold = autohop)"), ("reload", "Reload"),
    ("jumpbug", "Jumpbug (hold)"), ("edgebug", "Edgebug (hold)"), ("pixelsurf", "Pixelsurf (hold + crouch)"),
    ("weapon1", "Primary weapon"), ("weapon2", "Knife"),
    ("swaphand", "Switch hand"),
    ("crouch", "Crouch"), ("melee", "Melee attack"),
    ("console", "Developer console"),
]
BASE_SENS = 0.0022
MENU_CONTENT_SCALE = 0.75   # menu pages (everything right of the side menu) are drawn at this fraction of their old size; the side menu keeps its size. 1.0 = old size
STEP_H = 18  # max step / slope rise per move
MAXV = 3500.0  # sv_maxvelocity (CS:GO)
SURF_NZ = 0.7  # surfaces steeper than this (normal.z below it) cannot be walked on - you slide / surf on them
BOT_NAMES = ["Rick", "Sun", "Moon", "Ace", "Blaze", "Nova", "Jinx", "Echo", "Rex", "Luna",
             "Max", "Zed", "Kai", "Axel", "Milo", "Ziggy", "Storm", "Bolt", "Pixel", "Fang"]
VM_NEAR, VM_FAR = 0.1, 1000.0  # viewmodel clip planes

WJR, WJH, WJV, WJMAX = 16, 260, 210, 3   # WJV = wall-jump upward speed (was 290; normal jump is 270)
TICK = 1 / 64

# CS 1.6 style player animation tuning --------------------------------------
FADE_TIME = 0.2      # sequence cross-fade (seconds), same feel as the GoldSrc 0.2s sequence blend
VM_BOB_SIDE = 0.45    # first-person gun sway left/right while running. Scaled by cl_viewbob
VM_BOB_UP = 0.55      # first-person gun up/down bounce while running. Scaled by cl_viewbob
VM_LAND = 1.0         # gun dip when landing (was 2.2)
VM_IDLE_ANIM = False    # False = the weapon holds a still pose when you stand still; its animation only plays while you move. True = always play the idle animation
SHAKE = 0.6            # screen shake when you shoot: 0 = off, 0.3 = light, 0.6 = medium, 1 = strong
SHAKE_DECAY = 7.0      # how fast the shake dies down (higher = shorter)
# weapon held by other players / bots (the weapon model only, no arms) ------------
HELD_WPN = True                     # False = players / bots hold nothing visible
HELD_WPN_ANCHOR = (0.28, 0.50, 0.40)   # point of the gun held by the RIGHT hand (fractions of its length / width / height): the grip
HELD_WPN_FORE = (0.62, 0.50, 0.55)     # point of the gun held by the LEFT hand: the foregrip / handguard
HELD_WPN_LEVEL = 0.0                   # 0..1: how level the gun is kept (0 = muzzle follows the hands exactly, 1 = perfectly level)
HELD_KNIFE_ANCHOR = (0.15, 0.50, 0.50) # point of the knife inside the fist. Low x = the hand holds the back end, so the whole knife points out in front
HELD_KNIFE_FLIP = False                # True = turn the knife around (use if the blade points back at the body instead of out in front)
HELD_KNIFE_REACH = 17.0                # how far in front of the chest the knife hand is held (was 10)
HELD_KNIFE_SIDE = 8.0                  # how far out to the right (was 6)
HELD_KNIFE_DROP = 6.0                  # how far below the chest (was 8)
# Per-weapon hand positions on the gun, matched on the weapon name (first match wins):
#   (name regex, right-hand point, left-hand point), each = (along the gun 0..1 from the butt, across, height 0..1 from the bottom)
# The arms are posed to reach exactly these two points, so move a hand by editing its numbers here.
HELD_ANCHORS = [
    (r"awp|scout|sniper", (0.24, 0.50, 0.28), (0.39, 0.50, 0.34)),   # hands on the stock (grip + forestock)
    (r"(^|[^a-z])ak",     (0.28, 0.50, 0.40), (0.42, 0.50, 0.29)),   # left hand lower, under the handguard
    (r"nova|xm1014|mag7|sawed|shotgun|(^|[^a-z])m3", (0.26, 0.50, 0.36), (0.50, 0.50, 0.30)),   # shotgun: grip + pump forend
]
HELD_WPN_OFS = (0.0, 0.0, 0.0)         # extra shift of the gun in the hand: forward, left, up (game units)
ANIM_HZ = 60           # player / bot model animation rate at ANY distance (was 50 / 30 / 15 by distance)
ANIM_STEP = 1.0 / ANIM_HZ

# L4D2 style melee shove (E key) ---------------------------------------------
SHOVE_TIME = 0.55      # total length of the shove animation + cooldown (seconds)
SHOVE_HIT_AT = 0.28    # fraction of the animation where the hit lands (the strike peak)
SHOVE_RANGE = 78.0     # reach in game units
SHOVE_ARC = 55.0       # half-angle of the hit cone (degrees) - hits everything inside it, like L4D2
SHOVE_DMG = 50         # damage to enemies hit (100 HP = dies in 2 shoves)
SHOVE_SHAKE = 0.4      # screen shake on impact
SHOVE_ROT = 1          # if the gun swings the wrong way (up/down or left/right), flip to -1
SHOVE_BODY = True      # other players / bots see a two-hand push when you shove (False = off)
SHOVE_BODY_LEAN = 0.10 # how far the body leans into the strike (radians at the peak)
SHOVE_BODY_SCALE = 3.0 # size of the gun swing seen on other players (first person values x this)
# (fwd, left, up, yaw, pitch-up, roll) keyframes: p = 0..1 through the animation
SHOVE_KEYS = [
    (0.00,  0.0,  0.0,  0.0,   0.0,   0.0,   0.0),
    (0.14, -2.2, -1.2, -0.6, -14.0,  20.0,  12.0),   # wind-up: pull the gun back to the hip, muzzle up
    (0.28,  3.2,  1.4,  0.2,  16.0,  -8.0, -14.0),   # strike: gun thrust forward and swept across
    (0.38,  3.6,  1.6,  0.3,  18.0,  -9.0, -15.0),   # impact hold
    (0.70,  0.8,  0.3,  0.0,   4.0,  -2.0,  -3.0),   # drag back
    (1.00,  0.0,  0.0,  0.0,   0.0,   0.0,   0.0),
]


def shove_pose(p):
    """Interpolated (fwd, left, up, yaw, pitch_up, roll) of the shove at progress p (0..1)."""
    p = max(0.0, min(1.0, p))
    k = SHOVE_KEYS
    for a, b in zip(k, k[1:]):
        if p <= b[0]:
            t = (p - a[0]) / max(1e-6, b[0] - a[0])
            t = t * t * (3 - 2 * t)
            return tuple(a[i] + (b[i] - a[i]) * t for i in range(1, 7))
    return tuple(k[-1][1:])


# sMove (GMod) movement 1:1 — from sm_movement.lua + init.lua convars
#   sv_airaccelerate 1000, sv_accelerate 10, sv_gravity 800, sv_maxvelocity 10000
#   sv_sticktoground 0, jump power 275, run/walk 250
#   Fake jumpbug: vz=300 when near ground; edgebug: vz*0.1 + forward*40; pixelsurf: project vel, vz=0 near wall
# CS:GO defaults: sv_accelerate 5.5, sv_airaccelerate 12 (air wish-speed cap 30), jump impulse 301.993,
# friction 5.2, stopspeed 80, maxspeed 250, duck speed 0.34, bhop prevention 1.1x (sv_enablebunnyhopping 0)
MAXS, FRIC, STOPS, ACC, AIRACC, AIRCAP, JUMPV, BHCAP = 250, 5.2, 80, 5.5, 12.0, 30.0, 301.993, 1.1
DUCKSPD = 0.34
FALL_SAFE_SPEED, FALL_FATAL_SPEED = 580.0, 1024.0   # CS:GO: no damage below 580 u/s, dead at 1024
SV_GRAVITY = 800.0
STICK_TO_GROUND = False     # sv_sticktoground 0
# Jumpbug (smFakeJB): trace 10u down hits while airborne + jump held -> vz = 300
JB_TRACE = 10.0
JB_VZ = 300.0
JB_COOLDOWN_TICKS = 5
# Edgebug (smFakeEB): near floor (20u), gap ahead (45u fwd, 70u down miss), not on ground
EB_TRACE_DOWN = 20.0
EB_AHEAD = 45.0
EB_AHEAD_DOWN = 70.0
EB_VZ_MUL = 0.1             # falling vz *= 0.1
EB_BOOST = 40.0             # + forward * 40
EB_COOLDOWN = 0.15
# Pixel surf (smFakePX): near wall within 20u, airborne, project velocity onto look yaw, vz=0
PX_WALL_DIST = 8.0
EDGEBUG_VZ = 40.0           # residual |vz| after clip for passive edgebug detect
JUMPBUG_WINDOW = 1


def fresh_anim():
    """Per-entity animation state (Source players + legacy fields kept for compatibility)."""
    return {"seq": -1, "time": 0.0, "loop": True, "kind": "",
            "init": False, "gyaw": None, "twist": 0.0,
            "gs": -1, "gt": 0.0, "gl": True, "gk": "",
            "us": -1, "ut": 0.0, "ul": True, "uk": "", "ukey": None, "pst": 0.0,
            "dead": False, "ds": -1, "dt": 0.0, "air": False,
            "state": "", "state_time": 0.0}


class Wpn:
    """Weapon definition; missing attributes read as None."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, k):
        if k.startswith("__"):
            raise AttributeError(k)
        return None


def default_cfg():
    return {
        "weapons": [
            Wpn(n="AWP", cls="Sniper rifle", dmg=50, head_dmg=1000, rpm=45, mag=5, spr=.00035, auto=0,
                match=r"awp|snip|hunting|scout|sg55|g3sg|dragunov|svd|barrett|awm|(^|[^a-z])m24|m82"),
            Wpn(n="AK-47", cls="Assault rifle", dmg=28, rpm=600, mag=30, spr=.012, auto=1),
            Wpn(n="Knife", dmg=1000, rpm=120, mag=0, spr=0, auto=1, melee=1, range=64),
            # pump shotgun: `pellets` rays per shot, each doing `dmg` (damage fades with distance up to `range`);
            # `match` = file-name pattern used to auto-pick a shotgun view-model from your weapon packs
            Wpn(n="Shotgun", cls="Pump shotgun", dmg=16, rpm=70, mag=8, spr=.045, auto=0, pellets=9, range=1400, recoil=.14,
                match=r"nova|xm1014|mag7|sawed|shotgun|(^|[^a-z])m3"),
            # semi-auto pistol: slow fire. `head_dmg` = damage of a hit in the head (kills in one shot);
            # body hits do `dmg` = 25, so 4 body shots kill a 100 hp target
            Wpn(n="Pistol", cls="Pistol", dmg=25, head_dmg=1000, rpm=130, mag=5, spr=.004, auto=0, recoil=.07,
                match=r"pistol|glock|usp|p228|p250|fiveseven|five-seven|deagle|desert_?eagle|magnum|elite|tec9|revolver"),
        ],
        "frags": 30,
    }


CFG = default_cfg()


def key_norm(s):
    s = s.lower()
    s = re.sub(r"\.\w+$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)




# Team colours / names (UI + scoreboard)
TEAM_CT = (0, 0, 0)        # CT = black
TEAM_T = (255, 214, 0)     # T = yellow team
TEAM_NAMES = ("BUM", "FAD")     # team 0 (black) = BUM, team 1 (yellow) = FAD

RENDER_W, RENDER_H = 1280, 720    # default render resolution (Settings > Video)
