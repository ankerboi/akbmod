#!/usr/bin/env python3
"""
gma2vpk - convert a Garry's Mod weapon addon (.gma) into a Left 4 Dead 2
weapon-mod .vpk (addon-style single-file VPK v1 with addoninfo.txt).

Pure standard library. Run with no arguments for the GUI, or use the CLI:

  python gma2vpk.py addon.gma -t melee_crowbar -o out.vpk
  python gma2vpk.py addon.gma --list-targets
  python gma2vpk.py addon.gma --info

What it does
  * reads the GMA (versions 1-3), drops lua/ and other GMod-only content
  * moves the view model (c_*/v_*) and world model (w_*) plus all companion
    files (.vvd .dx90.vtx .dx80.vtx .sw.vtx .phy .ani) onto the L4D2 weapon
    path you choose, so the game loads them in place of the stock weapon
  * keeps materials/ and sound/ as they are (optional per-file remapping)
  * writes addoninfo.txt (Content_WeaponModel) and packs a VPK v1

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

def make_addoninfo(title, author, desc):
    t = '"AddonInfo"\r\n{\r\n'
    t += '\t"addonSteamAppID"\t"550"\r\n'
    t += '\t"addonTitle"\t\t"%s"\r\n' % title.replace('"', "'")
    t += '\t"addonVersion"\t\t"1.0"\r\n'
    t += '\t"addonAuthor"\t\t"%s"\r\n' % author.replace('"', "'")
    t += '\t"addonAuthorSteamID"\t"%s"\r\n\r\n' % author.replace('"', "'")
    t += '\t"Content_WeaponModel"\t"1"\r\n\r\n'
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

def convert_pairs(gma, out_path, pairs, title=None, author=None, desc=None):
    """pairs: iterable of (src_in_gma, dest_in_vpk). The same src may appear
    several times (shared parts); two different srcs may not share a dest."""
    entries, origin = {}, {}
    for src, dest in pairs:
        dest = norm(dest)
        if dest in origin and origin[dest] != src:
            raise ValueError("Two different files map to %s" % dest)
        origin[dest] = src
        entries[dest] = gma.files[src]
    meta = {}
    try:
        meta = json.loads(gma.description)
    except Exception:
        pass
    entries["addoninfo.txt"] = make_addoninfo(
        title or gma.name or "GMA conversion",
        author or (gma.author if gma.author != "Author Name" else "Unknown"),
        desc or (meta.get("title") if isinstance(meta, dict) else None) or
        "Converted from a Garry's Mod addon")
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
        for attr, pat in (("view", r'\.ViewModel\s*=\s*"([^"]+)"'),
                          ("world", r'\.WorldModel\s*=\s*"([^"]+)"')):
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
                "grenade_launcher", "rifle_m60", "pistol", "pistol_magnum",
                "molotov", "pipe_bomb", "vomitjar"]


def auto_assign(sweps):
    """swep name -> L4D2 target, one unique target each (None if we run out)."""
    out = {}
    for i, s in enumerate(sweps):
        out[s.name] = AUTO_TARGETS[i] if i < len(AUTO_TARGETS) else None
    return out


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
        if t in used:
            print("Target %s used by both %s and %s - change one." % (t, used[t], s.name))
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
        return 0
    sweps = parse_sweps(g)
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
    root.title("GMA -> L4D2 VPK weapon converter")
    root.geometry("960x640")
    st = {"gma": None, "path": "", "sweps": [], "assign": {}, "on": {}}

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
    ttk.Label(t2, text="Click 'Convert' cell to tick/untick, double-click the target to change it.").pack(anchor="w")
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

    def chosen():
        out, used = [], {}
        for s in st["sweps"]:
            if not st["on"].get(s.name):
                continue
            t = st["assign"].get(s.name)
            if not t:
                continue
            if t in used:
                raise ValueError("%s and %s both replace %s" % (used[t], s.name, t))
            used[t] = s.name
            out.append((s, t))
        return out

    def pack_many():
        g = st["gma"]
        try:
            ch = chosen()
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
        target_var.set(guess_target(g.files) or "rifle_ak47")
        refresh()
        log.delete("1.0", "end")
        say("%d weapon(s) found." % len(st["sweps"]))
        nb.select(t2 if len(st["sweps"]) > 1 else t1)

    ttk.Button(top, text="Open GMA...", command=open_gma).pack(side="left", padx=6)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    gui()
