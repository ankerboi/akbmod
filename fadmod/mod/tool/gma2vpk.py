#!/usr/bin/env python3
"""
gma2vpk - convert a Garry's Mod weapon or player-model addon (.gma) into a
Left 4 Dead 2 mod .vpk (addon-style single-file VPK v1 with addoninfo.txt).

Pure standard library. Run with no arguments for the GUI, or use the CLI:

  python gma2vpk.py addon.gma -t melee_crowbar -o out.vpk
  python gma2vpk.py addon.gma --list-targets
  python gma2vpk.py addon.gma --info

  # player models (survivors / special infected / tank / witch / common)
  python gma2vpk.py playermodel.gma --list-players
  python gma2vpk.py playermodel.gma -t nick -o ghostface_nick.vpk
  python gma2vpk.py playermodel.gma -t hunter --include-model models/m_anm.mdl=models/<l4d2 anim mdl>.mdl
  python gma2vpk.py playermodel.gma --list-player-targets

  # everything in the addon in one go (weapons + player models + arms)
  python gma2vpk.py bigpack.gma --kind all --outdir out/
  python gma2vpk.py bigpack.gma --kind all --combine -o everything.vpk
  python gma2vpk.py bigpack.gma --kind all --assign arms_hands=arms_nick

What it does
  * reads the GMA (versions 1-3), drops lua/ and other GMod-only content
  * moves the view model (c_*/v_*) and world model (w_*) plus all companion
    files (.vvd .dx90.vtx .dx80.vtx .sw.vtx .phy .ani) onto the L4D2 weapon
    path you choose, so the game loads them in place of the stock weapon
  * keeps materials/ and sound/ as they are (optional per-file remapping)
  * writes addoninfo.txt (Content_WeaponModel) and packs a VPK v1

Player models
  The addon is scanned for player_manager.AddValidModel(...) /
  list.Set("PlayerOptionsModel", ...) (or any non-c_/v_/w_ model under
  models/player). The model, its companion files and every material it uses
  are moved onto the L4D2 survivor / infected model path you choose
  (--list-player-targets). Lua (bodygroup scripts, cloth physics, ...) cannot
  run in L4D2 and is dropped.

Arms
  c_hands / c_arms models (AddValidHands, or any c_hands*/c_arms* .mdl) are
  converted too: they replace the L4D2 first-person survivor arms
  (models/weapons/arms/v_arms_<survivor>_new.mdl, targets arms_nick,
  arms_rochelle, ...). By default each arms model is paired with the survivor
  its player model replaces. Use --no-arms to skip them.

Everything at once
  --kind all (picked automatically when the addon holds more than one kind)
  converts every weapon, player model and arms model in the file: one VPK each
  (--outdir) or a single combined VPK (--combine).

What it cannot do
  It does not recompile models. GMod models are MDL v48 on the HL2/c_hands
  skeleton; L4D2 expects v49 with its own animation sets. The app warns when
  it sees this. If the model misbehaves in game, decompile and recompile it
  for L4D2 (Crowbar + studiomdl from the L4D2 SDK) and feed the result back
  through this tool.
"""
import argparse
import json
import os
import re
import struct
import sys
import zlib

# --------------------------------------------------------------------------
# GMA reader
# --------------------------------------------------------------------------

class GMA:
    def __init__(self):
        self.version = 0
        self.name = ""
        self.description = ""
        self.author = ""
        self.files = {}  # normalized path -> bytes


def _cstr(buf, pos):
    end = buf.index(b"\0", pos)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def read_gma(path):
    with open(path, "rb") as fh:
        buf = fh.read()
    if buf[:4] != b"GMAD":
        raise ValueError("Not a GMA file (missing GMAD header)")
    g = GMA()
    g.version = buf[4]
    pos = 5 + 8 + 8  # steamid, timestamp
    if g.version > 1:
        while True:  # required content list
            s, pos = _cstr(buf, pos)
            if not s:
                break
    g.name, pos = _cstr(buf, pos)
    g.description, pos = _cstr(buf, pos)
    g.author, pos = _cstr(buf, pos)
    pos += 4  # addon version
    entries = []
    while True:
        num = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        if num == 0:
            break
        p, pos = _cstr(buf, pos)
        size = struct.unpack_from("<q", buf, pos)[0]
        pos += 8
        pos += 4  # crc
        entries.append((p, size))
    for p, size in entries:
        g.files[norm(p)] = buf[pos:pos + size]
        pos += size
    return g


def norm(p):
    return p.replace("\\", "/").strip("/").lower()


# --------------------------------------------------------------------------
# L4D2 target table (view model path, world model path)
# --------------------------------------------------------------------------

def _gun(v, w):
    return ("models/v_models/%s.mdl" % v, "models/w_models/weapons/%s.mdl" % w)


def _melee(n):
    return ("models/weapons/melee/v_%s.mdl" % n, "models/weapons/melee/w_%s.mdl" % n)


TARGETS = {
    "pistol":          _gun("v_pistola", "w_pistol_a"),
    "pistol_magnum":   _gun("v_desert_eagle", "w_desert_eagle"),
    "smg":             _gun("v_smg", "w_smg_uzi"),
    "smg_silenced":    _gun("v_silenced_smg", "w_smg_a"),
    "smg_mp5":         _gun("v_smg_mp5", "w_smg_mp5"),
    "pumpshotgun":     _gun("v_pumpshotgun", "w_shotgun"),
    "shotgun_chrome":  _gun("v_shotgun_chrome", "w_pumpshotgun_a"),
    "autoshotgun":     _gun("v_autoshotgun", "w_autoshot_m4super"),
    "shotgun_spas":    _gun("v_shotgun_spas", "w_shotgun_spas"),
    "rifle":           _gun("v_rifle", "w_rifle_m16a2"),
    "rifle_ak47":      _gun("v_rifle_ak47", "w_rifle_ak47"),
    "rifle_desert":    _gun("v_rifle_desert", "w_desert_rifle"),
    "rifle_sg552":     _gun("v_rifle_sg552", "w_rifle_sg552"),
    "hunting_rifle":   _gun("v_huntingrifle", "w_sniper_mini14"),
    "sniper_military": _gun("v_sniper_military", "w_sniper_military"),
    "sniper_awp":      _gun("v_sniper_awp", "w_sniper_awp"),
    "sniper_scout":    _gun("v_sniper_scout", "w_sniper_scout"),
    "grenade_launcher": _gun("v_grenade_launcher", "w_grenade_launcher"),
    "rifle_m60":       _gun("v_m60", "w_m60"),
    "molotov":         _gun("v_molotov", "w_molotov"),
    "pipe_bomb":       _gun("v_pipebomb", "w_eq_pipebomb"),
    "vomitjar":        _gun("v_bile_flask", "w_eq_bile_flask"),
}
for _m in ("crowbar", "cricket_bat", "baseball_bat", "katana", "machete",
           "fireaxe", "electric_guitar", "frying_pan", "golfclub", "tonfa",
           "knife", "pitchfork", "shovel", "chainsaw"):
    TARGETS["melee_" + _m] = _melee(_m)

MDL_COMPANIONS = (".vvd", ".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx",
                  ".phy", ".ani")
KEEP_PREFIXES = ("models/", "materials/", "sound/", "scripts/", "particles/")


# --------------------------------------------------------------------------
# Model helpers
# --------------------------------------------------------------------------

def mdl_version(data):
    if len(data) >= 8 and data[:4] == b"IDST":
        return struct.unpack_from("<i", data, 4)[0]
    return None


def find_models(files):
    """Return (view_candidates, world_candidates, other_models)."""
    view, world, other = [], [], []
    for p in sorted(files):
        if not p.endswith(".mdl"):
            continue
        base = os.path.basename(p)
        if base.startswith(("c_", "v_")):
            view.append(p)
        elif base.startswith("w_"):
            world.append(p)
        else:
            other.append(p)
    return view, world, other


def model_group(files, mdl_path):
    """mdl + companion files that share its stem."""
    stem = mdl_path[:-4]
    out = [mdl_path]
    for ext in MDL_COMPANIONS:
        if stem + ext in files:
            out.append(stem + ext)
    return out


def guess_target(files):
    view, world, _ = find_models(files)
    names = " ".join(os.path.basename(p) for p in view + world)
    for key in sorted(TARGETS, key=len, reverse=True):
        short = key.replace("melee_", "").replace("pistol_", "")
        if short in names:
            return key
    return None


# --------------------------------------------------------------------------
# Planning the file map
# --------------------------------------------------------------------------

def build_plan(gma, target, view_src=None, world_src=None):
    """Return (plan dict src->dest, warnings)."""
    files = gma.files
    warnings = []
    plan = {}
    vdest, wdest = TARGETS[target]
    view, world, other = find_models(files)
    view_src = norm(view_src) if view_src else (view[0] if view else None)
    world_src = norm(world_src) if world_src else (world[0] if world else None)
    if not view_src and other:
        view_src = other[0]
        warnings.append("No c_/v_ model found; using %s as the view model." % view_src)
    if len(view) > 1 and not view_src:
        warnings.append("Several view models found; pick one.")

    for src, dest in ((view_src, vdest), (world_src, wdest)):
        if not src:
            continue
        if src not in files:
            raise ValueError("Model not in addon: %s" % src)
        d_stem = dest[:-4]
        for part in model_group(files, src):
            suffix = part[len(src) - 4:]  # '.mdl', '.vvd', '.dx90.vtx', ...
            plan[part] = d_stem + suffix
        ver = mdl_version(files[src])
        if ver is not None and ver != 49:
            warnings.append(
                "%s is MDL v%d (L4D2 uses v49). It may load, but animations "
                "on a GMod c_hands skeleton usually do not match L4D2." %
                (os.path.basename(src), ver))
    if not view_src:
        warnings.append("No view model in this addon - only the world model will be replaced.")
    if not world_src:
        warnings.append("No world model in this addon - only the view model will be replaced.")

    skipped_models = [m for m in view + world + other
                      if m not in plan and m not in (view_src, world_src)]
    for m in skipped_models:
        warnings.append("Extra model not used: %s" % m)

    for p in sorted(files):
        if p in plan or p.endswith((".mdl", ".vvd", ".phy", ".ani")) or p.endswith(".vtx"):
            continue
        if p.startswith(KEEP_PREFIXES):
            plan[p] = p
    dropped = [p for p in files if p not in plan and not p.endswith(
        (".mdl", ".vvd", ".phy", ".ani", ".vtx"))]
    if dropped:
        warnings.append("Dropped %d non-L4D2 file(s) (e.g. lua/): %s" %
                        (len(dropped), ", ".join(sorted(dropped)[:3])))
    if any(p.startswith("sound/") for p in plan):
        warnings.append(
            "Sounds are kept at their GMod paths. L4D2 plays weapon sounds "
            "through soundscripts, so they only play in game if you remap "
            "them onto the stock L4D2 wav paths (edit the destination).")
    return plan, warnings


# --------------------------------------------------------------------------
# VPK v1 writer
# --------------------------------------------------------------------------

def make_addoninfo(title, author, desc, keys=("Content_WeaponModel", "addonContent_Weapon")):
    t = '"AddonInfo"\r\n{\r\n'
    t += '\t"addonSteamAppID"\t"550"\r\n'
    t += '\t"addonTitle"\t\t"%s"\r\n' % title.replace('"', "'")
    t += '\t"addonVersion"\t\t"1.0"\r\n'
    t += '\t"addonAuthor"\t\t"%s"\r\n' % author.replace('"', "'")
    t += '\t"addonAuthorSteamID"\t"%s"\r\n\r\n' % author.replace('"', "'")
    for k in keys:
        t += '\t"%s"\t"1"\r\n' % k
    t += '\r\n'
    t += '\t"addonDescription"\t"%s"\r\n}\r\n' % desc.replace('"', "'")
    return t.encode("utf-8")


def write_vpk(path, entries):
    """entries: dict dest_path -> bytes"""
    tree = {}
    for p, data in entries.items():
        d, fn = os.path.split(p)
        if "." in fn:
            name, ext = fn.split(".", 1) if fn.count(".") == 1 else (fn.rsplit(".", 1)[0], fn.rsplit(".", 1)[1])
        else:
            name, ext = fn, " "
        tree.setdefault(ext, {}).setdefault(d or " ", {})[name] = data

    tree_bytes = bytearray()
    data_bytes = bytearray()

    def cs(s):
        tree_bytes.extend(s.encode("utf-8") + b"\0")

    for ext in sorted(tree):
        cs(ext)
        for d in sorted(tree[ext]):
            cs(d)
            for name in sorted(tree[ext][d]):
                data = tree[ext][d][name]
                cs(name)
                tree_bytes.extend(struct.pack(
                    "<IHHIIH", zlib.crc32(data) & 0xFFFFFFFF, 0, 0x7FFF,
                    len(data_bytes), len(data), 0xFFFF))
                data_bytes.extend(data)
            tree_bytes.extend(b"\0")
        tree_bytes.extend(b"\0")
    tree_bytes.extend(b"\0")

    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x55AA1234, 1, len(tree_bytes)))
        fh.write(tree_bytes)
        fh.write(data_bytes)


def read_vpk_listing(path):
    """Small VPK v1 reader (for verification)."""
    with open(path, "rb") as fh:
        buf = fh.read()
    sig, ver, tsize = struct.unpack_from("<III", buf, 0)
    assert sig == 0x55AA1234 and ver == 1
    base = 12 + tsize
    pos, out = 12, {}
    while True:
        ext, pos = _cstr(buf, pos)
        if not ext:
            break
        while True:
            d, pos = _cstr(buf, pos)
            if not d:
                break
            while True:
                name, pos = _cstr(buf, pos)
                if not name:
                    break
                crc, pl, ai, off, ln, _t = struct.unpack_from("<IHHIIH", buf, pos)
                pos += 18 + pl
                full = ("" if d == " " else d + "/") + name + ("" if ext == " " else "." + ext)
                data = buf[base + off:base + off + ln]
                assert zlib.crc32(data) & 0xFFFFFFFF == crc, "CRC mismatch " + full
                out[full] = data
    return out


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------

def convert_pairs(gma, out_path, pairs, title=None, author=None, desc=None,
                  content_keys=None, overrides=None):
    """pairs: iterable of (src_in_gma, dest_in_vpk). The same src may appear
    several times (shared parts); two different srcs may not share a dest."""
    entries, origin = {}, {}
    for src, dest in pairs:
        dest = norm(dest)
        if dest in origin and origin[dest] != src:
            raise ValueError("Two different files map to %s" % dest)
        origin[dest] = src
        entries[dest] = (overrides or {}).get(src, gma.files[src])
    meta = {}
    try:
        meta = json.loads(gma.description)
    except Exception:
        pass
    entries["addoninfo.txt"] = make_addoninfo(
        title or gma.name or "GMA conversion",
        author or (gma.author if gma.author != "Author Name" else "Unknown"),
        desc or (meta.get("title") if isinstance(meta, dict) else None) or
        "Converted from a Garry's Mod addon",
        content_keys or ("Content_WeaponModel", "addonContent_Weapon"))
    write_vpk(out_path, entries)
    return entries


def convert(gma, out_path, plan, title=None, author=None, desc=None):
    return convert_pairs(gma, out_path, plan.items(), title, author, desc)


# --------------------------------------------------------------------------
# Weapon packs (many SWEPs in one GMA)
# --------------------------------------------------------------------------

class Swep:
    def __init__(self, name):
        self.name = name
        self.title = name
        self.view = None
        self.world = None


def parse_sweps(gma):
    """Find every SWEP in lua/weapons and its ViewModel / WorldModel."""
    sweps = {}
    for p in sorted(gma.files):
        if not (p.startswith("lua/weapons/") and p.endswith(".lua")):
            continue
        rel = p[len("lua/weapons/"):]
        name = rel.split("/")[0]
        if name.endswith(".lua"):
            name = name[:-4]
        txt = gma.files[p].decode("utf-8", "replace")
        sw = sweps.setdefault(name, Swep(name))
        # also matches  SWEP.ViewModel = Model( "models/..." )  and single quotes
        for attr, pat in (("view", r'\.ViewModel\s*=\s*(?:Model\s*\(\s*)?["\']([^"\']+)["\']'),
                          ("world", r'\.WorldModel\s*=\s*(?:Model\s*\(\s*)?["\']([^"\']+)["\']')):
            m = re.search(pat, txt)
            if m and norm(m.group(1)) in gma.files and getattr(sw, attr) is None:
                setattr(sw, attr, norm(m.group(1)))
        m = re.search(r'\.PrintName\s*=\s*"([^"]+)"', txt)
        if m:
            sw.title = m.group(1)
    return [s for s in sweps.values() if s.view or s.world]


AUTO_TARGETS = ["rifle_ak47", "rifle", "rifle_desert", "rifle_sg552", "smg",
                "smg_silenced", "smg_mp5", "pumpshotgun", "shotgun_chrome",
                "autoshotgun", "shotgun_spas", "hunting_rifle",
                "sniper_military", "sniper_awp", "sniper_scout",
                "grenade_launcher", "rifle_m60", "pistol", "pistol_magnum"]


# weapon name contains -> best matching L4D2 weapon slot
AUTO_HINTS = [("awp", "sniper_awp"), ("ssg08", "sniper_scout"), ("g3sg1", "sniper_military"),
              ("scar20", "hunting_rifle"), ("ak47", "rifle_ak47"), ("m4a4", "rifle"),
              ("m4a1", "rifle_desert"), ("sg553", "rifle_sg552"), ("nova", "pumpshotgun"),
              ("xm1014", "autoshotgun"), ("sawedoff", "shotgun_chrome"), ("mag7", "shotgun_spas"),
              ("mp5", "smg_mp5"), ("mp7", "smg_silenced"), ("mac10", "smg"),
              ("deagle", "pistol_magnum"), ("p2000", "pistol"), ("m249", "rifle_m60"),
              ("negev", "grenade_launcher")]


# weapon name contains -> the L4D2 slots of the same kind
AUTO_KINDS = [("pist", ["pistol", "pistol_magnum"]),
              ("smg", ["smg", "smg_silenced", "smg_mp5"]),
              ("rif", ["rifle", "rifle_ak47", "rifle_desert", "rifle_sg552"]),
              ("shot", ["pumpshotgun", "shotgun_chrome", "autoshotgun", "shotgun_spas"]),
              ("snip", ["sniper_awp", "sniper_scout", "sniper_military", "hunting_rifle"]),
              ("mach", ["rifle_m60", "grenade_launcher"])]


def _kind_slots(name):
    n = name.lower()
    for k, slots in AUTO_KINDS:
        if "_" + k in n or n.startswith(k):
            return slots
    return AUTO_TARGETS


def auto_assign(sweps):
    """swep name -> L4D2 target. Every weapon gets one.
    1) a weapon whose name matches a stock gun gets that slot,
    2) the rest take a free slot of the same kind (pistol -> pistol, ...),
    3) when a kind runs out, the weapon shares a slot with another one. Sharing
       is fine with one VPK per weapon, but not inside one combined VPK.
    Plain versions come before scopeless / dual variants."""
    order = sorted(sweps, key=lambda s: (("scopeless" in s.name) or s.name.endswith("_dual"), s.name))
    out, used = {}, set()
    for s in order:
        for h, t in AUTO_HINTS:
            if h in s.name and t not in used:
                out[s.name] = t
                used.add(t)
                break
    for s in order:
        if s.name in out:
            continue
        base = s.name.replace("_scopeless", "").replace("_dual", "")
        if base != s.name and base in out:        # variant: same slot as its plain weapon
            out[s.name] = out[base]
            continue
        slots = _kind_slots(s.name)
        free = [t for t in slots if t not in used] or [t for t in AUTO_TARGETS if t not in used]
        t = free[0] if free else slots[0]
        out[s.name] = t
        used.add(t)
    return {s.name: out[s.name] for s in sweps}


def mdl_materials(data):
    """(cdmaterials list, texture names) from an MDL v44-49 header."""
    def s_at(off):
        end = data.index(b"\0", off)
        return data[off:end].decode("utf-8", "replace")
    try:
        ntex, tidx = struct.unpack_from("<ii", data, 204)
        ncd, cidx = struct.unpack_from("<ii", data, 212)
        cds = []
        for i in range(ncd):
            off = struct.unpack_from("<i", data, cidx + 4 * i)[0]
            cds.append(norm(s_at(off)))
        texs = []
        for i in range(ntex):
            base = tidx + 64 * i
            texs.append(norm(s_at(base + struct.unpack_from("<i", data, base)[0])))
        return cds, texs
    except Exception:
        return [], []


def model_materials(files, mdl_paths):
    """Material files (vmt + the vtf/vmt they reference) a set of models uses."""
    found, todo = set(), []
    for mp in mdl_paths:
        cds, texs = mdl_materials(files[mp])
        for cd in cds or [""]:
            for t in texs:
                v = ("materials/%s/%s.vmt" % (cd, t)).replace("//", "/")
                if v in files:
                    todo.append(v)
    while todo:
        v = todo.pop()
        if v in found:
            continue
        found.add(v)
        txt = files[v].decode("utf-8", "replace")
        for ref in re.findall(r'"([^"]+)"', txt):
            r = norm(ref)
            for cand in ("materials/%s.vtf" % r, "materials/%s.vmt" % r):
                if cand in files and cand not in found:
                    if cand.endswith(".vmt"):
                        todo.append(cand)
                    else:
                        found.add(cand)
    return found


def swep_pairs(gma, swep, target, with_sounds=False):
    """(src, dest) pairs for one SWEP replacing one L4D2 weapon."""
    files = gma.files
    vdest, wdest = TARGETS[target]
    pairs, warns, models = [], [], []
    for src, dest in ((swep.view, vdest), (swep.world, wdest)):
        if not src:
            continue
        models.append(src)
        for part in model_group(files, src):
            pairs.append((part, dest[:-4] + part[len(src) - 4:]))
        ver = mdl_version(files[src])
        if ver is not None and ver != 49:
            warns.append("%s: %s is MDL v%d" % (swep.name, os.path.basename(src), ver))
    mats = model_materials(files, models)
    pairs += [(m, m) for m in sorted(mats)]
    if not mats:
        warns.append("%s: could not resolve materials from the model" % swep.name)
    if with_sounds:
        pairs += [(p, p) for p in sorted(files) if p.startswith("sound/")]
    return pairs, warns


def safe(n):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", n)


# --------------------------------------------------------------------------
# Player models (survivors / infected)
# --------------------------------------------------------------------------

def _surv(n):
    return "models/survivors/survivor_%s.mdl" % n


def _inf(n):
    return "models/infected/%s.mdl" % n


PLAYER_TARGETS = {
    # survivors (L4D2 cast)
    "nick":      _surv("gambler"),
    "rochelle":  _surv("producer"),
    "coach":     _surv("coach"),
    "ellis":     _surv("mechanic"),
    # survivors (L4D1 cast, present in L4D2)
    "bill":      _surv("namvet"),
    "zoey":      _surv("teenangst"),
    "francis":   _surv("biker"),
    "louis":     _surv("manager"),
    # special infected / bosses
    "smoker":    _inf("smoker"),
    "boomer":    _inf("boomer"),
    "boomette":  _inf("boomette"),
    "hunter":    _inf("hunter"),
    "spitter":   _inf("spitter"),
    "jockey":    _inf("jockey"),
    "charger":   _inf("charger"),
    "tank":      _inf("hulk"),
    "witch":     _inf("witch"),
    "witch_bride": _inf("witch_bride"),
    # common infected
    "common_male":   _inf("common_male_tshirt_cargos"),
    "common_suit":   _inf("common_male_suit"),
    "common_female": _inf("common_female_tshirt_skirt"),
}

def _arms(n):
    return "models/weapons/arms/v_arms_%s_new.mdl" % n


# first-person survivor arms (the "hands" a c_hands model replaces)
ARMS_TARGETS = {
    "arms_nick": _arms("gambler"), "arms_rochelle": _arms("producer"),
    "arms_coach": _arms("coach"), "arms_ellis": _arms("mechanic"),
    "arms_bill": _arms("bill"), "arms_zoey": _arms("zoey"),
    "arms_francis": _arms("francis"), "arms_louis": _arms("louis"),
}

AUTO_PLAYER_TARGETS = ["nick", "rochelle", "coach", "ellis", "bill", "zoey",
                       "francis", "louis"]

CONTENT_KEYS = {
    "survivor": ("addonContent_Survivor",),
    "boss": ("addonContent_BossInfected",),
    "common": ("addonContent_CommonInfected",),
}


def player_dest(target):
    """Destination .mdl for a PLAYER_TARGETS key or a custom .mdl path."""
    if target in PLAYER_TARGETS:
        return PLAYER_TARGETS[target]
    if target in ARMS_TARGETS:
        return ARMS_TARGETS[target]
    t = norm(target)
    if t.endswith(".mdl") and t.startswith("models/"):
        return t
    raise ValueError("Unknown player target %r (see --list-player-targets, or "
                     "give a full models/....mdl path)" % target)


def player_content_key(dest):
    if dest.startswith(("models/survivors/", "models/weapons/arms/")):
        return "survivor"
    if "/common_" in dest:
        return "common"
    return "boss"


class PlayerModel:
    def __init__(self, path, title=None):
        self.path = path
        self.title = title or os.path.basename(path)[:-4]


def _lua_vars(txt):
    return {m.group(1): m.group(2)
            for m in re.finditer(r'([A-Za-z_]\w*)\s*=\s*"([^"]+\.mdl)"', txt, re.I)}


def _lua_arg(arg, vars_):
    arg = arg.strip()
    if arg[:1] in "\"'":
        return arg.strip("\"'")
    return vars_.get(arg)


def parse_playermodels(gma, strict=False):
    """Return (player_models, hands_paths) found in the addon.
    strict=True: only models registered from lua (no plain file-scan fallback)."""
    files = gma.files
    found, hands = {}, set()
    for p in sorted(files):
        if not (p.startswith("lua/") and p.endswith(".lua")):
            continue
        txt = files[p].decode("utf-8", "replace")
        vars_ = _lua_vars(txt)
        for m in re.finditer(r'AddValidModel\(\s*"([^"]*)"\s*,\s*([^,)]+)', txt):
            path = _lua_arg(m.group(2), vars_)
            if path and norm(path) in files:
                found.setdefault(norm(path), m.group(1))
        for m in re.finditer(r'list\.Set\(\s*"PlayerOptionsModel"\s*,\s*"([^"]*)"\s*,\s*([^)]+)\)', txt):
            path = _lua_arg(m.group(2), vars_)
            if path and norm(path) in files:
                found.setdefault(norm(path), m.group(1))
        for m in re.finditer(r'AddValidHands\(\s*"[^"]*"\s*,\s*([^,)]+)', txt):
            path = _lua_arg(m.group(1), vars_)
            if path:
                hands.add(norm(path))
    def is_hands(path):
        b = os.path.basename(path)
        return path in hands or b.startswith(("c_hands", "c_arms", "hands", "arms"))
    for p in list(found):
        if is_hands(p):
            hands.add(p)
            del found[p]
    if not found and not strict:  # fall back to a plain file scan
        for p in sorted(files):
            b = os.path.basename(p)
            if p.endswith(".mdl") and not is_hands(p) and not b.startswith(("v_", "c_", "w_")):
                found[p] = None
    models = [PlayerModel(p, t) for p, t in found.items()]
    hands |= {p for p in files if p.endswith(".mdl") and is_hands(p)}
    return models, hands


def hand_models(gma, hands):
    """PlayerModel list for the hands / arms models that really are in the addon."""
    return [PlayerModel(p) for p in sorted(hands) if p in gma.files and p.endswith(".mdl")]


def auto_assign_arms(hmodels, passign=None):
    """arms model path -> arms target. Arms follow the survivors the player
    models replace; leftovers take the next free survivor."""
    pref = []
    for t in (passign or {}).values():
        if t and ("arms_" + t) in ARMS_TARGETS and ("arms_" + t) not in pref:
            pref.append("arms_" + t)
    for n in AUTO_PLAYER_TARGETS:
        if ("arms_" + n) not in pref:
            pref.append("arms_" + n)
    return {m.path: (pref[i] if i < len(pref) else None) for i, m in enumerate(hmodels)}


def auto_assign_players(models):
    return {m.path: (AUTO_PLAYER_TARGETS[i] if i < len(AUTO_PLAYER_TARGETS) else None)
            for i, m in enumerate(models)}


def mdl_info(data):
    """Bones / sequences / $includemodel list from an MDL v44-49 header."""
    v = mdl_version(data)
    if v is None or not (44 <= v <= 49) or len(data) < 344:
        return None
    nb = struct.unpack_from("<i", data, 156)[0]
    nseq = struct.unpack_from("<i", data, 188)[0]
    ninc, iinc = struct.unpack_from("<ii", data, 336)
    includes = []
    try:
        for i in range(max(0, ninc)):
            base = iinc + 8 * i
            ni = struct.unpack_from("<i", data, base + 4)[0]
            end = data.index(b"\0", base + ni)
            includes.append(norm(data[base + ni:end].decode("utf-8", "replace")))
    except Exception:
        pass
    return {"version": v, "bones": nb, "sequences": nseq, "includes": includes}


def patch_includemodels(data, mapping):
    """Rewrite $includemodel paths. New strings are appended to the file and the
    entry's name index re-pointed, so the new path may be any length.
    Returns (new_bytes, [(old, new), ...])."""
    info = mdl_info(data)
    if not info or not info["includes"]:
        return data, []
    buf = bytearray(data)
    ninc, iinc = struct.unpack_from("<ii", buf, 336)
    changed = []
    for i in range(ninc):
        base = iinc + 8 * i
        ni = struct.unpack_from("<i", buf, base + 4)[0]
        end = buf.index(b"\0", base + ni)
        old = buf[base + ni:end].decode("utf-8", "replace")
        new = mapping.get(norm(old))
        if not new:
            continue
        while len(buf) % 4:
            buf.append(0)
        struct.pack_into("<i", buf, base + 4, len(buf) - base)
        buf.extend(new.encode("utf-8") + b"\0")
        changed.append((old, new))
    if changed:
        while len(buf) % 4:
            buf.append(0)
        struct.pack_into("<i", buf, 76, len(buf))  # studiohdr.length
    return bytes(buf), changed


def player_pairs(gma, src, target, include_map=None):
    """(pairs, warnings, overrides) for one player model replacing one L4D2 model."""
    files = gma.files
    dest = player_dest(target)
    name = os.path.basename(src)
    pairs, warns, overrides = [], [], {}
    for part in model_group(files, src):
        pairs.append((part, dest[:-4] + part[len(src) - 4:]))
    if src not in files:
        raise ValueError("Model not in addon: %s" % src)
    if include_map:
        data, changed = patch_includemodels(files[src], {norm(k): v for k, v in include_map.items()})
        if changed:
            overrides[src] = data
            for o, n in changed:
                warns.append("%s: $includemodel %s -> %s" % (name, o, n))
    info = mdl_info(overrides.get(src, files[src]))
    if info:
        if info["version"] != 49:
            warns.append("%s: MDL v%d (L4D2 uses v49)." % (name, info["version"]))
        if info["bones"] > 256:
            warns.append("%s: %d bones, over the Source limit of 256." % (name, info["bones"]))
        for inc in info["includes"]:
            if inc not in files and inc not in {norm(v) for v in (include_map or {}).values()}:
                warns.append(
                    "%s includes %s for its animations. L4D2 does not ship that "
                    "file, so the model will T-pose unless you point it at an "
                    "L4D2 animation model (--include-model) or recompile it." % (name, inc))
    mats = model_materials(files, [src])
    pairs += [(m, m) for m in sorted(mats)]
    if not mats:
        warns.append("%s: could not resolve materials from the model" % name)
    return pairs, warns, overrides


def parse_include_map(items):
    out = {}
    for it in items:
        if "=" not in it:
            raise ValueError("--include-model expects OLD=NEW, got %r" % it)
        o, n = it.split("=", 1)
        out[norm(o)] = n.strip().replace("\\", "/")
    return out


# --------------------------------------------------------------------------
# Everything in one go (weapons + player models + arms)
# --------------------------------------------------------------------------

class Item:
    def __init__(self, label, stem, pairs, over, keys, title, warns):
        self.label, self.stem, self.pairs, self.over = label, stem, pairs, over
        self.keys, self.title, self.warns = keys, title, warns   # keys None = weapon keys


def build_items(g, weapons=(), players=(), arms=(), inc=None, sounds=False):
    """weapons: [(Swep, target)], players / arms: [(PlayerModel, target)]."""
    items = []
    for s, t in weapons:
        p, w = swep_pairs(g, s, t, sounds)
        items.append(Item("weapon %s -> %s" % (s.name, t), "%s__%s" % (safe(s.name), t),
                          p, {}, None, s.title, w))
    for kind, lst in (("player", players), ("arms", arms)):
        for m, t in lst:
            p, w, o = player_pairs(g, m.path, t, inc)
            keys = list(CONTENT_KEYS[player_content_key(player_dest(t))])
            items.append(Item("%s %s -> %s" % (kind, os.path.basename(m.path), t),
                              "%s__%s" % (safe(os.path.basename(m.path)[:-4]), t),
                              p, o, keys, m.title, w))
    return items


def write_items(g, items, out=None, outdir=None, combine=False, title=None, author=None,
                desc=None, say=print):
    """One VPK per item (into outdir) or one combined VPK (out). Returns written paths."""
    written = []
    if combine or len(items) == 1:
        pairs, over, keys = [], {}, []
        for it in items:
            pairs += it.pairs
            over.update(it.over)
            for k in (it.keys or ("Content_WeaponModel", "addonContent_Weapon")):
                if k not in keys:
                    keys.append(k)
        base = os.path.splitext(os.path.basename(g.path))[0] if getattr(g, "path", None) else "addon"
        out = out or base + "_l4d2.vpk"
        convert_pairs(g, out, pairs, title, author, desc, keys, over)
        read_vpk_listing(out)
        say("Wrote %s (%d item%s)" % (out, len(items), "" if len(items) == 1 else "s"))
        for it in items:
            say("  " + it.label)
        written.append(out)
    else:
        outdir = outdir or "out_l4d2"
        os.makedirs(outdir, exist_ok=True)
        for it in items:
            f = os.path.join(outdir, it.stem + ".vpk")
            convert_pairs(g, f, it.pairs, "%s (%s)" % (it.title, g.name), author, desc,
                          it.keys, it.over)
            read_vpk_listing(f)
            say("  %-44s %s" % (it.label, f))
            written.append(f)
        say("Wrote %d VPKs into %s/" % (len(items), outdir))
    return written


def _match_model(models, name):
    n = norm(name)
    for m in models:
        b = os.path.basename(m.path)
        if m.path == n or b == n or b[:-4] == n or b == n + ".mdl":
            return m
    return None


def run_all(g, a):
    sweps = parse_sweps(g)
    pmodels, hset = parse_playermodels(g, strict=bool(sweps))
    hmodels = [] if a.no_arms else hand_models(g, hset)
    try:
        inc = parse_include_map(a.include_model)
    except ValueError as e:
        print(e)
        return 2
    wasg = auto_assign(sweps)
    if a.map_file:
        with open(a.map_file) as fh:
            wasg.update(json.load(fh))
    passg = auto_assign_players(pmodels)
    aasg = auto_assign_arms(hmodels, passg)
    for it in a.assign:
        n, t = it.split("=", 1)
        if n in wasg:
            wasg[n] = t
        elif _match_model(pmodels, n):
            passg[_match_model(pmodels, n).path] = t
        elif _match_model(hmodels, n):
            aasg[_match_model(hmodels, n).path] = t
        else:
            print("Unknown name in --assign:", n)
            return 2
    if a.model:
        pm = _match_model(pmodels, a.model)
        pmodels = [pm] if pm else []
    if a.write_map:
        with open(a.write_map, "w") as fh:
            json.dump(wasg, fh, indent=2)
        print("Wrote assignment file", a.write_map)
        return 0
    if not (sweps or pmodels or hmodels):
        print("Nothing to convert: no weapons, player models or arms found.")
        return 2
    used = {}

    def take(kind, name, t, dest):
        if dest in used:
            print("Target %s used by both %s and %s - change one." % (t, used[dest], name))
            return False
        used[dest] = name
        return True

    wch, pch, ach = [], [], []
    for s in sweps:
        t = wasg.get(s.name)
        if not t:
            print("SKIP weapon %s (no target left)" % s.name)
            continue
        if t not in TARGETS:
            print("Unknown weapon target %r for %s" % (t, s.name))
            return 2
        if a.combine and not take("weapon", s.name, t, "w:" + t):
            return 2
        wch.append((s, t))
    for lst, asg, out in ((pmodels, passg, pch), (hmodels, aasg, ach)):
        for m in lst:
            t = asg.get(m.path)
            if not t:
                print("SKIP %s (no target left)" % m.path)
                continue
            try:
                d = player_dest(t)
            except ValueError as e:
                print(e)
                return 2
            if not take("model", os.path.basename(m.path), t, d):
                return 2
            out.append((m, t))
    items = build_items(g, wch, pch, ach, inc, a.sounds)
    base = os.path.splitext(os.path.basename(a.gma))[0]
    g.path = a.gma
    write_items(g, items, a.out or (base + "_l4d2.vpk"), a.outdir or (base + "_l4d2"),
                a.combine, a.title, a.author, a.desc)
    print("Converted: %d weapon(s), %d player model(s), %d arms model(s)" % (len(wch), len(pch), len(ach)))
    if any(p.startswith("lua/") for p in g.files):
        print("NOTE: lua/ dropped - GMod scripts do not run in L4D2.")
    seen = set()
    for it in items:
        for w in it.warns:
            k = w.split(": ", 1)[-1]
            if "MDL v48" in k:
                if "v48" in seen:
                    continue
                seen.add("v48")
                w = "models are MDL v48 (L4D2 uses v49) - may need recompiling for L4D2"
            print("WARNING:", w)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run_pack(g, a):
    sweps = parse_sweps(g)
    assign = auto_assign(sweps)
    if a.map_file:
        with open(a.map_file) as fh:
            assign.update(json.load(fh))
    for it in a.assign:
        n, t = it.split("=", 1)
        assign[n] = t
    if a.write_map:
        with open(a.write_map, "w") as fh:
            json.dump(assign, fh, indent=2)
        print("Wrote assignment file", a.write_map, "- edit it, then pass --map-file")
        return 0
    chosen = []
    for s in sweps:
        t = assign.get(s.name)
        if not t:
            print("SKIP %s (no target left)" % s.name)
            continue
        if t not in TARGETS:
            print("Unknown target %r for %s" % (t, s.name))
            return 2
        chosen.append((s, t))
    used = {}
    for s, t in chosen:
        if t in used and a.combine:
            print("Target %s used by both %s and %s - change one (or drop --combine)." % (t, used[t], s.name))
            return 2
        used[t] = s.name
    base = os.path.splitext(os.path.basename(a.gma))[0]
    if a.combine:
        pairs, warns = [], []
        for s, t in chosen:
            p, w = swep_pairs(g, s, t, a.sounds)
            pairs += p
            warns += w
        out = a.out or base + "_l4d2.vpk"
        convert_pairs(g, out, pairs, a.title, a.author, a.desc)
        print("Wrote %s (%d weapons)" % (out, len(chosen)))
        for s, t in chosen:
            print("  %-22s -> %s" % (s.name, t))
    else:
        outdir = a.outdir or base + "_l4d2"
        os.makedirs(outdir, exist_ok=True)
        warns = []
        for s, t in chosen:
            p, w = swep_pairs(g, s, t, a.sounds)
            warns += w
            out = os.path.join(outdir, "%s__%s.vpk" % (safe(s.name), t))
            convert_pairs(g, out, p, "%s (%s)" % (s.title, g.name), a.author, a.desc)
            print("  %-22s -> %-18s %s" % (s.name, t, out))
        print("Wrote %d VPKs into %s/" % (len(chosen), outdir))
    seen = set()
    for w in warns:
        k = w.split(": ", 1)[-1]
        if "MDL v48" in k and "v48" in seen:
            continue
        if "MDL v48" in k:
            seen.add("v48")
            w = "models are MDL v48 (L4D2 uses v49) - may need recompiling for L4D2"
        print("WARNING:", w)
    return 0


def run_players(g, a):
    models, hands = parse_playermodels(g)
    if not models:
        print("No player model found in this addon.")
        return 2
    try:
        inc = parse_include_map(a.include_model)
    except ValueError as e:
        print(e)
        return 2
    if a.model:
        want = norm(a.model)
        pick = [m for m in models if m.path == want or os.path.basename(m.path) == want
                or os.path.basename(m.path)[:-4] == want]
        if not pick:
            if want in g.files and want.endswith(".mdl"):
                pick = [PlayerModel(want)]
            else:
                print("Model %r not found. Candidates: %s" % (a.model, ", ".join(m.path for m in models)))
                return 2
        models = pick
    if len(models) == 1:
        assign = {models[0].path: a.target or "nick"}
    else:
        if a.target:
            print("%d player models found - use --assign MODEL=TARGET (or --model) instead of -t." % len(models))
            return 2
        assign = auto_assign_players(models)
        for it in a.assign:
            n, t = it.split("=", 1)
            n = norm(n)
            hit = [m.path for m in models if m.path == n or os.path.basename(m.path) in (n, n + ".mdl")]
            if not hit:
                print("Unknown model in --assign:", n)
                return 2
            assign[hit[0]] = t
    chosen, used = [], {}
    for m in models:
        t = assign.get(m.path)
        if not t:
            print("SKIP %s (no target left)" % m.path)
            continue
        try:
            d = player_dest(t)
        except ValueError as e:
            print(e)
            return 2
        if d in used:
            print("Target %s used by both %s and %s - change one." % (t, used[d], m.path))
            return 2
        used[d] = m.path
        chosen.append((m, t))
    base = os.path.splitext(os.path.basename(a.gma))[0]
    warns = []

    def build(sel):
        pairs, over, keys = [], {}, []
        for m, t in sel:
            p, w, o = player_pairs(g, m.path, t, inc)
            pairs += p
            warns.extend(w)
            over.update(o)
            k = CONTENT_KEYS[player_content_key(player_dest(t))]
            keys += [x for x in k if x not in keys]
        return pairs, over, keys

    if a.combine or len(chosen) == 1:
        pairs, over, keys = build(chosen)
        out = a.out or base + "_l4d2.vpk"
        convert_pairs(g, out, pairs, a.title, a.author, a.desc, keys, over)
        print("Wrote %s (%d player model%s)" % (out, len(chosen), "" if len(chosen) == 1 else "s"))
        for m, t in chosen:
            print("  %-34s -> %-12s %s" % (os.path.basename(m.path), t, player_dest(t)))
    else:
        outdir = a.outdir or base + "_l4d2"
        os.makedirs(outdir, exist_ok=True)
        for m, t in chosen:
            pairs, over, keys = build([(m, t)])
            out = os.path.join(outdir, "%s__%s.vpk" % (safe(os.path.basename(m.path)[:-4]), t))
            convert_pairs(g, out, pairs, "%s (%s)" % (m.title, g.name), a.author, a.desc, keys, over)
            print("  %-34s -> %-12s %s" % (os.path.basename(m.path), t, out))
        print("Wrote %d VPKs into %s/" % (len(chosen), outdir))
    if hands:
        print("NOTE: skipped hands/arms model(s): %s - use --kind all to convert them to L4D2 survivor arms" % ", ".join(sorted(hands)))
    if any(p.startswith("lua/") for p in g.files):
        print("NOTE: lua/ dropped - GMod scripts (bodygroup logic, cloth physics, ...) do not run in L4D2.")
    for w in dict.fromkeys(warns):
        print("WARNING:", w)
    return 0


def cli(argv):
    ap = argparse.ArgumentParser(description="Convert a GMod weapon .gma to L4D2 weapon .vpk file(s)")
    ap.add_argument("gma")
    ap.add_argument("-o", "--out")
    ap.add_argument("-t", "--target", help="L4D2 weapon to replace (see --list-targets)")
    ap.add_argument("--view", help="path of the view model inside the GMA")
    ap.add_argument("--world", help="path of the world model inside the GMA")
    ap.add_argument("--map", action="append", default=[], metavar="SRC=DEST",
                    help="override destination of a file (repeatable)")
    ap.add_argument("--title"); ap.add_argument("--author"); ap.add_argument("--desc")
    ap.add_argument("--list-targets", action="store_true")
    ap.add_argument("--info", action="store_true", help="list addon contents and exit")
    pl = ap.add_argument_group("player models")
    pl.add_argument("--kind", choices=("auto", "weapon", "player", "all"), default="auto",
                    help="what the addon contains (default: auto-detect; 'all' = weapons + players + arms)")
    pl.add_argument("--no-arms", action="store_true", help="do not convert c_hands / arms models")
    pl.add_argument("--list-player-targets", action="store_true", help="list L4D2 models a player model can replace")
    pl.add_argument("--list-players", action="store_true", help="list player models found and their auto-assigned targets")
    pl.add_argument("--model", help="player model inside the GMA to convert (path or file name)")
    pl.add_argument("--include-model", action="append", default=[], metavar="OLD=NEW",
                    help="rewrite a $includemodel (animation model) path, e.g. models/m_anm.mdl=models/....mdl (repeatable)")
    pk = ap.add_argument_group("weapon packs (several SWEPs in one GMA)")
    pk.add_argument("--list-weapons", action="store_true", help="list SWEPs found and their auto-assigned targets")
    pk.add_argument("--batch", action="store_true", help="convert every weapon (default when the GMA holds several)")
    pk.add_argument("--combine", action="store_true", help="put all weapons in ONE vpk instead of one vpk each")
    pk.add_argument("--outdir", help="output folder for per-weapon vpks")
    pk.add_argument("--assign", action="append", default=[], metavar="SWEP=TARGET", help="choose the L4D2 weapon a SWEP replaces")
    pk.add_argument("--write-map", metavar="FILE", help="save the SWEP->target assignment as JSON and exit")
    pk.add_argument("--map-file", metavar="FILE", help="load assignment JSON written by --write-map")
    pk.add_argument("--sounds", action="store_true", help="also include sounds in pack output")
    a = ap.parse_args(argv)

    if a.list_player_targets:
        for k, d in list(PLAYER_TARGETS.items()) + list(ARMS_TARGETS.items()):
            print("%-14s %s" % (k, d))
        return 0
    if a.list_targets:
        for k, (v, w) in TARGETS.items():
            print("%-18s %s | %s" % (k, v, w))
        return 0
    g = read_gma(a.gma)
    if a.info:
        print("Name: %s\nAuthor: %s\nGMA v%d" % (g.name, g.author, g.version))
        for p, d in sorted(g.files.items()):
            ver = mdl_version(d) if p.endswith(".mdl") else None
            print("%9d  %s%s" % (len(d), p, ("  [MDL v%d]" % ver) if ver else ""))
        print("Suggested target:", guess_target(g.files))
        sw = parse_sweps(g)
        pm, _h = parse_playermodels(g, strict=bool(sw))
        if pm and not sw:
            print("Player model(s):", ", ".join(m.path for m in pm))
        hm = hand_models(g, _h)
        if hm:
            print("Arms model(s):", ", ".join(m.path for m in hm))
        return 0
    sweps = parse_sweps(g)
    pmodels, _hands = parse_playermodels(g, strict=bool(sweps))
    hmodels = [] if a.no_arms else hand_models(g, _hands)
    kind = a.kind
    manual = a.target or a.view or a.world
    if kind == "auto" and not manual:
        nk = bool(sweps) + bool(pmodels) + bool(hmodels)
        if nk >= 2 or (hmodels and nk == 1):
            kind = "all"
    if kind == "auto":
        kind = "player" if (pmodels and not sweps and not a.view and not a.world) else "weapon"
    if a.list_players:
        asg = auto_assign_players(pmodels)
        for m in pmodels:
            print("%-40s %-30s -> %s" % (m.path, m.title, asg[m.path]))
        aa = auto_assign_arms(hmodels, asg)
        for m in hmodels:
            print("%-40s %-30s -> %s   (arms)" % (m.path, m.title, aa[m.path]))
        return 0
    if kind == "all":
        return run_all(g, a)
    if kind == "player":
        return run_players(g, a)
    if a.list_weapons:
        asg = auto_assign(sweps)
        for s in sweps:
            print("%-24s %-28s -> %s" % (s.name, s.title, asg[s.name]))
        return 0
    if a.batch or a.combine or a.write_map or (len(sweps) > 1 and not a.target):
        return run_pack(g, a)
    target = a.target or guess_target(g.files)
    if not target:
        print("Could not guess the target weapon; pass -t (see --list-targets).")
        return 2
    plan, warns = build_plan(g, target, a.view, a.world)
    for m in a.map:
        s, d = m.split("=", 1)
        s = norm(s)
        if s not in g.files:
            print("Unknown source in --map:", s)
            return 2
        plan[s] = norm(d)
    out = a.out or os.path.splitext(a.gma)[0] + "_l4d2.vpk"
    convert(g, out, plan, a.title, a.author, a.desc)
    for w in warns:
        print("WARNING:", w)
    print("Target:", target)
    for s, d in sorted(plan.items()):
        print("  %s -> %s" % (s, d))
    print("Wrote", out)
    return 0


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    root = tk.Tk()
    root.title("GMA -> L4D2 VPK weapon / player model / arms converter")
    root.geometry("960x640")
    st = {"gma": None, "path": "", "sweps": [], "assign": {}, "on": {},
          "pmodels": [], "hands": set(), "passign": {}, "pon": {},
          "hmodels": [], "aassign": {}, "aon": {}}

    top = ttk.Frame(root, padding=6)
    top.pack(fill="x")
    path_var = tk.StringVar()
    ttk.Entry(top, textvariable=path_var).pack(side="left", fill="x", expand=True)
    title_var, author_var = tk.StringVar(), tk.StringVar()
    row2 = ttk.Frame(root, padding=(6, 0))
    row2.pack(fill="x")
    ttk.Label(row2, text="Title:").pack(side="left")
    ttk.Entry(row2, textvariable=title_var, width=34).pack(side="left", padx=6)
    ttk.Label(row2, text="Author:").pack(side="left")
    ttk.Entry(row2, textvariable=author_var, width=18).pack(side="left", padx=6)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=6, pady=6)
    log = tk.Text(root, height=7)
    log.pack(fill="x", padx=6, pady=(0, 6))

    def say(s):
        log.insert("end", s + "\n")
        log.see("end")

    # ---- tab 1: single weapon -------------------------------------------
    t1 = ttk.Frame(nb, padding=4)
    nb.add(t1, text="Single weapon")
    target_var = tk.StringVar()
    r = ttk.Frame(t1)
    r.pack(fill="x")
    ttk.Label(r, text="Replace L4D2 weapon:").pack(side="left")
    cb = ttk.Combobox(r, textvariable=target_var, values=list(TARGETS), width=22, state="readonly")
    cb.pack(side="left", padx=6)
    tree = ttk.Treeview(t1, columns=("src", "dest"), show="headings")
    tree.heading("src", text="In GMA")
    tree.heading("dest", text="In VPK (double-click to edit)")
    tree.column("src", width=420)
    tree.column("dest", width=420)
    tree.pack(fill="both", expand=True, pady=6)

    def refresh():
        g = st["gma"]
        if not g or not target_var.get():
            return
        try:
            plan, warns = build_plan(g, target_var.get())
        except Exception as e:
            say("ERROR: %s" % e)
            return
        tree.delete(*tree.get_children())
        for s, d in sorted(plan.items()):
            tree.insert("", "end", values=(s, d))
        for w in warns:
            say("WARNING: " + w)

    def edit(_e):
        sel = tree.selection()
        if not sel:
            return
        s, d = tree.item(sel[0], "values")
        n = simpledialog.askstring("Destination", "Path inside the VPK for\n%s" % s, initialvalue=d)
        if n:
            tree.item(sel[0], values=(s, norm(n)))

    def convert_single():
        g = st["gma"]
        if not g:
            return
        plan = {tree.item(i, "values")[0]: tree.item(i, "values")[1] for i in tree.get_children()}
        out = filedialog.asksaveasfilename(defaultextension=".vpk", filetypes=[("VPK", "*.vpk")],
                                           initialfile=os.path.splitext(os.path.basename(st["path"]))[0] + ".vpk")
        if not out:
            return
        try:
            convert(g, out, plan, title_var.get(), author_var.get() or "Unknown")
            read_vpk_listing(out)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        say("Wrote %s (%d files)." % (out, len(plan) + 1))

    cb.bind("<<ComboboxSelected>>", lambda e: refresh())
    tree.bind("<Double-1>", edit)
    ttk.Button(t1, text="Convert -> VPK", command=convert_single).pack(anchor="e")

    # ---- tab 2: weapon pack ---------------------------------------------
    t2 = ttk.Frame(nb, padding=4)
    nb.add(t2, text="Weapon pack (all weapons)")
    ptree = ttk.Treeview(t2, columns=("on", "name", "title", "target"), show="headings")
    for c, h, w in (("on", "Convert", 70), ("name", "SWEP", 220), ("title", "Name", 260), ("target", "Replaces L4D2 weapon", 220)):
        ptree.heading(c, text=h)
        ptree.column(c, width=w)
    ptree.pack(fill="both", expand=True, pady=6)
    sounds_var = tk.BooleanVar(value=False)
    ttk.Label(t2, text="Click 'Convert' cell to tick/untick, double-click the target to change it. "
                        "Weapons that share a slot need one VPK each (not combined).").pack(anchor="w")
    ttk.Checkbutton(t2, text="Include sounds", variable=sounds_var).pack(anchor="w")

    def prefresh():
        ptree.delete(*ptree.get_children())
        for s in st["sweps"]:
            ptree.insert("", "end", iid=s.name, values=(
                "yes" if st["on"].get(s.name) else "no", s.name, s.title, st["assign"].get(s.name) or "-"))

    def pclick(e):
        row, col = ptree.identify_row(e.y), ptree.identify_column(e.x)
        if not row:
            return
        if col == "#1":
            st["on"][row] = not st["on"].get(row)
            prefresh()
        elif col == "#4" and e.type == tk.EventType.ButtonPress:
            pass

    def pedit(e):
        row, col = ptree.identify_row(e.y), ptree.identify_column(e.x)
        if not row or col != "#4":
            return
        win = tk.Toplevel(root)
        win.title(row)
        v = tk.StringVar(value=st["assign"].get(row) or "")
        c = ttk.Combobox(win, textvariable=v, values=list(TARGETS), state="readonly", width=28)
        c.pack(padx=10, pady=10)

        def ok():
            st["assign"][row] = v.get()
            win.destroy()
            prefresh()
        ttk.Button(win, text="OK", command=ok).pack(pady=(0, 10))

    ptree.bind("<Button-1>", pclick)
    ptree.bind("<Double-1>", pedit)

    def chosen(unique=True):
        out, used = [], {}
        for s in st["sweps"]:
            if not st["on"].get(s.name):
                continue
            t = st["assign"].get(s.name)
            if not t:
                continue
            if t in used and unique:
                raise ValueError("%s and %s both replace %s" % (used[t], s.name, t))
            used[t] = s.name
            out.append((s, t))
        return out

    def pack_many():
        g = st["gma"]
        try:
            ch = chosen(unique=False)      # one VPK each: two weapons may share a slot
            if not ch:
                return
            d = filedialog.askdirectory(title="Folder for the VPKs")
            if not d:
                return
            for s, t in ch:
                p, w = swep_pairs(g, s, t, sounds_var.get())
                f = os.path.join(d, "%s__%s.vpk" % (safe(s.name), t))
                convert_pairs(g, f, p, "%s (%s)" % (s.title, g.name), author_var.get() or "Unknown")
                say("%s -> %s" % (s.name, f))
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        say("Done: %d VPKs." % len(ch))

    def pack_one():
        g = st["gma"]
        try:
            ch = chosen()
            if not ch:
                return
            f = filedialog.asksaveasfilename(defaultextension=".vpk", filetypes=[("VPK", "*.vpk")])
            if not f:
                return
            pairs = []
            for s, t in ch:
                pairs += swep_pairs(g, s, t, sounds_var.get())[0]
            convert_pairs(g, f, pairs, title_var.get(), author_var.get() or "Unknown")
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        say("Wrote %s with %d weapons." % (f, len(ch)))

    bt = ttk.Frame(t2)
    bt.pack(fill="x")
    ttk.Button(bt, text="Convert ticked -> one VPK each", command=pack_many).pack(side="left", padx=4)
    ttk.Button(bt, text="Convert ticked -> ONE combined VPK", command=pack_one).pack(side="left")

    # ---- tab 3: player models -------------------------------------------
    t3 = ttk.Frame(nb, padding=4)
    nb.add(t3, text="Player models")
    ptree3 = ttk.Treeview(t3, columns=("on", "model", "title", "target"), show="headings")
    for c, h, w in (("on", "Convert", 70), ("model", "Model in GMA", 330), ("title", "Name", 200), ("target", "Replaces L4D2 model", 200)):
        ptree3.heading(c, text=h)
        ptree3.column(c, width=w)
    ptree3.pack(fill="both", expand=True, pady=6)
    ttk.Label(t3, text="Click 'Convert' to tick/untick, double-click the target to change it "
                       "(a full models/....mdl path also works via the CLI).").pack(anchor="w")
    inc_var = tk.StringVar()
    ri = ttk.Frame(t3)
    ri.pack(fill="x", pady=4)
    ttk.Label(ri, text="Rewrite $includemodel (OLD=NEW; OLD=NEW):").pack(side="left")
    ttk.Entry(ri, textvariable=inc_var).pack(side="left", fill="x", expand=True, padx=6)

    def p3refresh():
        ptree3.delete(*ptree3.get_children())
        for m in st["pmodels"]:
            ptree3.insert("", "end", iid=m.path, values=(
                "yes" if st["pon"].get(m.path) else "no", m.path, m.title, st["passign"].get(m.path) or "-"))

    def p3click(e):
        row, col = ptree3.identify_row(e.y), ptree3.identify_column(e.x)
        if row and col == "#1":
            st["pon"][row] = not st["pon"].get(row)
            p3refresh()

    def p3edit(e):
        row, col = ptree3.identify_row(e.y), ptree3.identify_column(e.x)
        if not row or col != "#4":
            return
        win = tk.Toplevel(root)
        win.title(os.path.basename(row))
        v = tk.StringVar(value=st["passign"].get(row) or "")
        c = ttk.Combobox(win, textvariable=v, values=list(PLAYER_TARGETS), state="readonly", width=28)
        c.pack(padx=10, pady=10)

        def ok():
            st["passign"][row] = v.get()
            win.destroy()
            p3refresh()
        ttk.Button(win, text="OK", command=ok).pack(pady=(0, 10))

    ptree3.bind("<Button-1>", p3click)
    ptree3.bind("<Double-1>", p3edit)

    def p3chosen():
        out, used = [], {}
        for m in st["pmodels"]:
            if not st["pon"].get(m.path):
                continue
            t = st["passign"].get(m.path)
            if not t:
                continue
            if t in used:
                raise ValueError("%s and %s both replace %s" % (used[t], os.path.basename(m.path), t))
            used[t] = os.path.basename(m.path)
            out.append((m, t))
        return out

    def p3build(sel):
        inc = parse_include_map([x.strip() for x in inc_var.get().split(";") if x.strip()])
        pairs, over, keys, warns = [], {}, [], []
        for m, t in sel:
            p, w, o = player_pairs(st["gma"], m.path, t, inc)
            pairs += p
            warns += w
            over.update(o)
            keys += [x for x in CONTENT_KEYS[player_content_key(player_dest(t))] if x not in keys]
        for w in dict.fromkeys(warns):
            say("WARNING: " + w)
        return pairs, over, keys

    def p3_many():
        try:
            ch = p3chosen()
            if not ch:
                return
            d = filedialog.askdirectory(title="Folder for the VPKs")
            if not d:
                return
            for m, t in ch:
                pairs, over, keys = p3build([(m, t)])
                f = os.path.join(d, "%s__%s.vpk" % (safe(os.path.basename(m.path)[:-4]), t))
                convert_pairs(st["gma"], f, pairs, "%s (%s)" % (m.title, st["gma"].name),
                              author_var.get() or "Unknown", None, keys, over)
                say("%s -> %s" % (os.path.basename(m.path), f))
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        say("Done: %d VPKs." % len(ch))

    def p3_one():
        try:
            ch = p3chosen()
            if not ch:
                return
            f = filedialog.asksaveasfilename(defaultextension=".vpk", filetypes=[("VPK", "*.vpk")])
            if not f:
                return
            pairs, over, keys = p3build(ch)
            convert_pairs(st["gma"], f, pairs, title_var.get(), author_var.get() or "Unknown", None, keys, over)
            read_vpk_listing(f)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        say("Wrote %s with %d player model(s)." % (f, len(ch)))

    b3 = ttk.Frame(t3)
    b3.pack(fill="x")
    ttk.Button(b3, text="Convert ticked -> one VPK each", command=p3_many).pack(side="left", padx=4)
    ttk.Button(b3, text="Convert ticked -> ONE combined VPK", command=p3_one).pack(side="left")

    # ---- tab 4: arms ------------------------------------------------------
    t4 = ttk.Frame(nb, padding=4)
    nb.add(t4, text="Arms")
    ptree4 = ttk.Treeview(t4, columns=("on", "model", "target"), show="headings")
    for c, h, w in (("on", "Convert", 70), ("model", "Arms / hands model in GMA", 420), ("target", "Replaces L4D2 arms", 240)):
        ptree4.heading(c, text=h)
        ptree4.column(c, width=w)
    ptree4.pack(fill="both", expand=True, pady=6)
    ttk.Label(t4, text="c_hands / arms models become the first-person survivor arms. "
                       "Click 'Convert' to tick/untick, double-click the target to change it.").pack(anchor="w")

    def a4refresh():
        ptree4.delete(*ptree4.get_children())
        for m in st["hmodels"]:
            ptree4.insert("", "end", iid=m.path, values=(
                "yes" if st["aon"].get(m.path) else "no", m.path, st["aassign"].get(m.path) or "-"))

    def a4click(e):
        row, col = ptree4.identify_row(e.y), ptree4.identify_column(e.x)
        if row and col == "#1":
            st["aon"][row] = not st["aon"].get(row)
            a4refresh()

    def a4edit(e):
        row, col = ptree4.identify_row(e.y), ptree4.identify_column(e.x)
        if not row or col != "#3":
            return
        win = tk.Toplevel(root)
        win.title(os.path.basename(row))
        v = tk.StringVar(value=st["aassign"].get(row) or "")
        ttk.Combobox(win, textvariable=v, values=list(ARMS_TARGETS), state="readonly", width=28).pack(padx=10, pady=10)

        def ok():
            st["aassign"][row] = v.get()
            win.destroy()
            a4refresh()
        ttk.Button(win, text="OK", command=ok).pack(pady=(0, 10))

    ptree4.bind("<Button-1>", a4click)
    ptree4.bind("<Double-1>", a4edit)

    def a4chosen():
        out, used = [], {}
        for m in st["hmodels"]:
            if not st["aon"].get(m.path):
                continue
            t = st["aassign"].get(m.path)
            if not t:
                continue
            if t in used:
                raise ValueError("%s and %s both replace %s" % (used[t], os.path.basename(m.path), t))
            used[t] = os.path.basename(m.path)
            out.append((m, t))
        return out

    def convert_everything():
        g = st["gma"]
        if not g:
            return
        try:
            wch, pch, ach = chosen(unique=combine_var.get()), p3chosen(), a4chosen()
            if not (wch or pch or ach):
                say("Nothing ticked.")
                return
            inc = parse_include_map([x.strip() for x in inc_var.get().split(";") if x.strip()])
            items = build_items(g, wch, pch, ach, inc, sounds_var.get())
            g.path = st["path"]
            if combine_var.get():
                f = filedialog.asksaveasfilename(defaultextension=".vpk", filetypes=[("VPK", "*.vpk")],
                                                 initialfile=os.path.splitext(os.path.basename(st["path"]))[0] + "_all.vpk")
                if not f:
                    return
                write_items(g, items, out=f, combine=True, title=title_var.get(),
                            author=author_var.get() or "Unknown", say=say)
            else:
                d = filedialog.askdirectory(title="Folder for the VPKs")
                if not d:
                    return
                write_items(g, items, outdir=d, author=author_var.get() or "Unknown", say=say)
            for w in dict.fromkeys(w for it in items for w in it.warns):
                say("WARNING: " + w)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        say("Done: %d weapon(s), %d player model(s), %d arms model(s)." % (len(wch), len(pch), len(ach)))

    combine_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(row2, text="one combined VPK", variable=combine_var).pack(side="left", padx=10)
    ttk.Button(row2, text="Convert EVERYTHING (ticked in all tabs)", command=convert_everything).pack(side="left")

    # ---- open ------------------------------------------------------------
    def open_gma():
        p = filedialog.askopenfilename(filetypes=[("Garry's Mod addon", "*.gma"), ("All", "*.*")])
        if not p:
            return
        try:
            g = read_gma(p)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        st.update(gma=g, path=p)
        path_var.set(p)
        title_var.set(g.name)
        author_var.set("" if g.author == "Author Name" else g.author)
        st["sweps"] = parse_sweps(g)
        st["assign"] = auto_assign(st["sweps"])
        st["on"] = {s.name: bool(st["assign"][s.name]) for s in st["sweps"]}
        prefresh()
        st["pmodels"], st["hands"] = parse_playermodels(g, strict=bool(st["sweps"]))
        st["passign"] = auto_assign_players(st["pmodels"])
        st["pon"] = {m.path: bool(st["passign"][m.path]) for m in st["pmodels"]}
        p3refresh()
        st["hmodels"] = hand_models(g, st["hands"])
        st["aassign"] = auto_assign_arms(st["hmodels"], st["passign"])
        st["aon"] = {m.path: bool(st["aassign"][m.path]) for m in st["hmodels"]}
        a4refresh()
        target_var.set(guess_target(g.files) or "rifle_ak47")
        refresh()
        log.delete("1.0", "end")
        say("%d weapon(s), %d player model(s), %d arms model(s) found." % (
            len(st["sweps"]), len(st["pmodels"]), len(st["hmodels"])))
        if st["hmodels"] and not st["sweps"] and not st["pmodels"]:
            nb.select(t4)
        elif st["pmodels"] and not st["sweps"]:
            nb.select(t3)
        else:
            nb.select(t2 if len(st["sweps"]) > 1 else t1)

    ttk.Button(top, text="Open GMA...", command=open_gma).pack(side="left", padx=6)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    gui()
