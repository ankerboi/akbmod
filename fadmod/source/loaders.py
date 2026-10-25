"""Source engine asset loaders: loose files, VPK scanning, view-models, player models."""
import math
import os
import re
import struct
import time
import zipfile
import tempfile
import shutil
from pathlib import Path

import numpy as np
from OpenGL import GL as gl

from ..config import *
from ..glutil import make_texture, tex2d
from .vpk import VpkArchive
from .gma import GmaArchive
from .vtf import parse_kv, load_vtf
from .model import SrcModel, _find_vpk_model, _quat_mat3, _angle_quat
from .model import HIT_HALF_W, HIT_HALF_H, HEAD_FRAC, HEAD_MIN_R, HEAD_MAX_R, HIT_HALF_H_LOW
from .model import VM_EVENT_GAIN, SHOTGUN_RELOAD_SCALE, PLAYER_SCALE

class LooseSrcFiles:
    """Loose .mdl/.vvd/.vtx/.vtf/.vmt files dropped in the weapons folder, found by file name."""

    def __init__(self):
        self.paths = {}

    def add(self, path):
        self.paths[Path(path).name.lower()] = Path(path)

    def _key(self, name):
        return name.lower().replace("\\", "/").rsplit("/", 1)[-1]

    def has(self, name):
        return self._key(name) in self.paths

    def read(self, name, start=0, length=None):
        with open(self.paths[self._key(name)], "rb") as f:
            f.seek(start)
            return f.read() if length is None else f.read(length)

    @property
    def files(self):
        return self.paths


class DirSrcFiles:
    """A folder with Source files (models/..., materials/...). Also looks for materials in the
    folders above it, so variant folders like V_Models/CSS/models/... find the mod's materials."""

    def __init__(self, root):
        root = Path(root)
        self.roots = [root]
        a = root
        for _ in range(4):
            if a.parent == a:
                break
            a = a.parent
            try:
                if any(c.is_dir() and c.name.lower() == "materials" for c in a.iterdir()):
                    self.roots.append(a)
            except Exception:
                pass
        self.files = {}
        for r in self.roots:
            try:
                subs = [c for c in r.iterdir() if c.is_dir() and c.name.lower() in ("models", "materials", "sound")]
            except Exception:
                subs = []
            for sub in subs:
                for dp, _dn, fs in os.walk(sub):
                    for f in fs:
                        q = Path(dp) / f
                        self.files.setdefault(q.relative_to(r).as_posix().lower(), q)

    def _k(self, name):
        return name.lower().replace("\\", "/")

    def has(self, name):
        return self._k(name) in self.files

    def size(self, name):
        return self.files[self._k(name)].stat().st_size

    def read(self, name, start=0, length=None):
        with open(self.files[self._k(name)], "rb") as f:
            f.seek(start)
            return f.read() if length is None else f.read(length)


def src_root(p):
    """Folder that holds models/ and materials/ for a loose model file."""
    p = Path(p)
    for i, part in enumerate(p.parts[:-1]):
        if part.lower() == "models":
            return Path(*p.parts[:i]) if i else p.parent
    return p.parent


def src_rel(p):
    """Path of a loose model file relative to the folder that holds its models/ folder."""
    p = Path(p)
    try:
        return p.resolve().relative_to(src_root(p).resolve()).as_posix().lower()
    except Exception:
        return p.name.lower()


def _is_arms_mdl(nm):
    """True for a separate arm / hand / glove / sleeve view-model in any Source pack.

    Matches common L4D2 arms-pack naming:
      v_arms_zoey.mdl, v_glove_*.mdl, arms.mdl, hands.mdl
      models/weapons/arms/..., models/.../hands/...
    Pure gun view-models (v_ak47, v_knife_..., w_*, ...) are rejected.
    """
    rel = nm.replace("\\", "/").lower()
    b = rel.rsplit("/", 1)[-1]
    if not b.endswith(".mdl"):
        return False
    stem = b[:-4]
    # world / dropped / display models are never equippable arms
    if re.match(r"^[wp]_", stem) or re.search(r"(^|_)(dropped|display|world|ragdoll|anim)(_|$)", stem):
        return False
    # path folders that only hold arm / hand meshes
    if re.search(r"/(arms?|hands?|sleeves?|gloves?)/", rel):
        return True
    # filename keywords
    if re.search(r"(^|_)(arms?|hands?|sleeves?|gloves?|fingers?)(_|$)", stem):
        return True
    if re.search(r"(arms?|hands?|sleeves?|gloves?)", stem):
        return True
    # pure weapon view-models (v_ak47, v_m4a1, ...) – not arms
    if re.match(r"^v_", stem):
        return False
    return False


def _mdl_looks_like_arms(buf):
    """Fallback: Source MDL with mostly arm/hand bones and no full body → treat as arms pack."""
    try:
        if len(buf) < 220 or buf[:4] != b"IDST":
            return False
        ver = struct.unpack_from("<i", buf, 4)[0]
        if not 44 <= ver <= 49:
            return False
        I = lambda o: struct.unpack_from("<i", buf, o)[0]
        nb, bo = I(156), I(160)
        if nb <= 0 or nb > 256 or bo <= 0 or bo + nb * 216 > len(buf):
            return False
        arm = body = 0
        for i in range(nb):
            p = bo + i * 216
            no = I(p)
            if no <= 0 or p + no >= len(buf):
                continue
            end = buf.find(b"\0", p + no)
            if end < 0:
                end = min(len(buf), p + no + 64)
            name = buf[p + no:end].decode("latin1", "replace").lower()
            if re.search(r"hand|finger|forearm|upperarm|wrist|ulna|clavicle|shoulder|thumb", name):
                arm += 1
            if re.search(r"pelvis|thigh|calf|foot|toe|head|neck|spine|hip", name):
                body += 1
        return arm >= 8 and body <= 2
    except Exception:
        return False


def src_arms(arch):
    """First arm / hand model inside a Source archive (legacy single-pick helper)."""
    all_ = src_arms_all(arch)
    return all_[0] if all_ else None


def src_arms_all(arch):
    """Every arm / hand / glove model inside any Source archive (.vpk / folder / zip)."""
    found = [nm for nm in arch.files if _is_arms_mdl(nm)]
    if found:
        return sorted(found)
    # Ambiguous filenames: peek at MDL bone layout for leftover .mdl files
    extra = []
    for nm in arch.files:
        low = nm.replace("\\", "/").lower()
        if not low.endswith(".mdl"):
            continue
        b = low.rsplit("/", 1)[-1]
        if re.match(r"^[wp]_", b) or re.search(r"player|ragdoll|dropped|display", low):
            continue
        try:
            sz = arch.size(nm) if hasattr(arch, "size") else 65536
            buf = arch.read(nm, 0, min(65536, sz))
        except Exception:
            continue
        if _mdl_looks_like_arms(buf):
            extra.append(nm)
    return sorted(set(found) | set(extra))


class SrcSpec:
    def __init__(self, arch, mdl):
        self.arch, self.mdl, self._data = arch, mdl, None

    def __len__(self):
        try:
            return self.arch.size(self.mdl) if hasattr(self.arch, "size") else 1
        except Exception:
            return 1

    def load(self):
        if self._data is None:
            self._data = SrcData(self.arch, self.mdl)
        return self._data


_ARM_BONE_RX = re.compile(r"bip01_[lr]_(hand|forearm|upperarm|finger)|^j_(wrist|elbow|shoulder|index|mid|pinky|ring|thumb)", re.I)


def _mesh_on_arm_bones(srcm):
    """True when the model's own mesh is skinned to arm / hand / finger bones (hands built into the view-model)."""
    try:
        arm = np.array([bool(_ARM_BONE_RX.search(b["name"])) for b in srcm.bones])
        dom = np.minimum(srcm.v_bi[:, 0], len(arm) - 1)
        return int(arm[dom].sum()) > 100
    except Exception:
        return False


def _norm_tex(t):
    """A $basetexture / material value -> 'models/x/y' (no 'materials/' prefix, no extension, forward slashes)."""
    t = str(t or "").replace("\\", "/").lower().strip()
    t = re.sub(r"^/+", "", t)
    t = re.sub(r"^materials/", "", t)
    return re.sub(r"\.(vtf|vmt)$", "", t)


_STEM_IDX = {}


def _find_by_stem(arch, name, ext):
    """'materials/<name><ext>' inside the archive: the exact path first, else the one file that has the same
    file name (addons whose cdmaterials / $basetexture folder differs from where the file really is)."""
    exact = "materials/" + name + ext
    if arch.has(exact):
        return exact
    key = (id(arch), ext)
    if key not in _STEM_IDX:
        idx = {}
        for n in getattr(arch, "files", {}):
            low = str(n).replace("\\", "/").lower()
            if low.endswith(ext) and "materials/" in low:
                idx.setdefault(low.rsplit("/", 1)[-1][:-len(ext)], []).append(low)
        _STEM_IDX[key] = idx
    hits = _STEM_IDX[key].get(name.rsplit("/", 1)[-1], [])
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:       # several: the one whose folder matches the most of the wanted path
        want = name.split("/")
        sc = lambda h: sum(1 for a, b in zip(reversed(h[:-len(ext)].split("/")), reversed(want)) if a == b)
        best = max(hits, key=sc)
        return best if sc(best) >= 2 else None
    return None


def _find_any(arch, name, ext):
    """(archive, path) of a material file: in 'arch' itself first, then in every other opened Garry's Mod addon
    (exact path, or the one file of that name). (None, None) when nobody has it."""
    hit = _find_by_stem(arch, name, ext)
    if hit:
        return arch, hit
    try:
        from .gma import OPEN_ARCHIVES
        for other in list(OPEN_ARCHIVES):
            if other is arch:
                continue
            hit = _find_by_stem(other, name, ext)
            if hit:
                return other, hit
    except Exception:
        pass
    return None, None


def _vmt_root(arch, vmt, depth=0):
    """(parameters dict, shader name) of a .vmt. 'patch' materials (include + insert / replace) are resolved
    against the material they include, so $basetexture etc. are found."""
    kv = parse_kv(arch.read(vmt).decode("utf-8", "replace"))
    shader = next(iter(kv.keys()), "")
    root = next((v for v in kv.values() if isinstance(v, dict)), {})
    if shader == "patch" and depth < 4:
        inc = _norm_tex(root.get("include"))
        inc_vmt = _find_by_stem(arch, inc, ".vmt") if inc else None
        base, base_shader = ({}, "")
        if inc_vmt:
            try:
                base, base_shader = _vmt_root(arch, inc_vmt, depth + 1)
            except Exception:
                base, base_shader = {}, ""
        merged = dict(base)
        for sect in ("insert", "replace"):
            if isinstance(root.get(sect), dict):
                merged.update(root[sect])
        return merged, base_shader or shader
    return root, shader


class SrcData:
    """Source model + its textures as GL textures (needs the GL context)."""

    _tcache = {}

    def __init__(self, arch, mdl_name):
        self.arch = arch
        self.model = srcm = SrcModel(*_find_vpk_model(arch, mdl_name))
        try:
            srcm.attach_includes(arch, mdl_name)
        except Exception as ex:
            print("include animations failed:", ex)
        self.name = mdl_name
        self.seqs = srcm.seqs
        # Garry's Mod weapons whose view-model is only the chassis: barrel, magazine, stock, bolt, scope ... are
        # separate models in the addon and get bone-merged on top; their bodygroup choices (hide the iron sights
        # behind a scope ...) are applied to this model
        self.extras = []
        sel = {}
        fn = getattr(arch, "default_attachments", None)
        if fn is not None:
            try:
                for nm, bg in fn(mdl_name):
                    try:
                        self.extras.append(SrcData(arch, nm))
                        sel.update(bg)
                    except Exception as ex:
                        print("attachment failed:", nm, ex)
            except Exception as ex:
                print("attachments skipped:", ex)
        if sel:
            try:
                srcm.parts = srcm.parts_for(sel)
            except Exception as ex:
                print("bodygroups skipped:", ex)
        self.parts = []
        for p in srcm.parts:
            tri = np.ascontiguousarray(p["tri"], np.uint32)
            try:
                _tn = srcm.tex_names[srcm.skin[p["mat"]]]
            except Exception:
                _tn = ""
            _arm = bool(re.search(r"(?<![a-z])(hands?|sleeves?|gloves?|fingers?|palms?|arms?)(?![a-z])|_arms?$", _tn, re.I))
            self.parts.append({"idx": tri, "mat": self._material(p["mat"]), "arm": _arm})
        # CS:S style view-models have the hands built in: no separate arms model then
        # (whole words only: a "handguard" / "handle" texture is part of the gun, not a hand)
        self.has_hands = (any(re.search(r"(?<![a-z])(hands?|sleeves?|gloves?|fingers?|palms?)(?![a-z])|^arms?$|_arms?$", t, re.I)
                              for t in srcm.tex_names)
                          or any(re.search(r"/(hands|arms|sleeves|gloves)/", c) for c in srcm.cd)
                          or _mesh_on_arm_bones(srcm))
        self.uv = np.ascontiguousarray(srcm.v_uv, np.float32)

    _warned = set()

    def _warn_tex(self, nm, why, tried, shader):
        """One line per missing texture, so it is clear WHY a model came out grey (texture in another addon, RT scope ...)."""
        k = (id(self.arch), nm, why)
        if k in SrcData._warned:
            return
        SrcData._warned.add(k)
        req = list(getattr(self.arch, "required", []) or [])
        print("[texture] %s: material '%s' - %s (shader %s; looked for: %s)%s"
              % (self.name, nm, why, shader or "?", ", ".join(dict.fromkeys(tried)) or "-",
                 ("; this addon lists required addons: " + ", ".join(req)) if req else ""))

    @staticmethod
    def _glass_tex():
        """4x4 dark see-through texture: scope lenses / glass use render targets or refraction that we cannot draw."""
        key = ("glass",)
        if key not in SrcData._tcache:
            rgba = np.full((4, 4, 4), (14, 20, 24, 70), np.uint8)
            SrcData._tcache[key] = make_texture(rgba, 4, 4, mip=False)
        return SrcData._tcache[key]

    @staticmethod
    def _solid_tex(rgb):
        """1x1 solid colour GL texture (used when a Source pack ships mesh only, no .vtf)."""
        key = ("solid", tuple(round(c, 4) for c in rgb))
        if key not in SrcData._tcache:
            r, g, b = [max(0, min(255, int(c * 255))) for c in rgb]
            rgba = np.full((4, 4, 4), (r, g, b, 255), np.uint8)
            SrcData._tcache[key] = make_texture(rgba, 4, 4, mip=False)
        return SrcData._tcache[key]

    def _material(self, mat):
        arch = self.arch
        info = {"t": None, "alphatest": False, "blend": False, "add": False, "tint": None}
        nm0 = (self.model.tex_names[self.model.skin[mat]] if 0 <= mat < len(self.model.skin)
               and self.model.skin[mat] < len(self.model.tex_names) else "")
        # Guess a readable fill from the material / path name (L4D2 arms packs often have no .vtf)
        is_arms_path = any(re.search(r"/(arms|hands|sleeves|gloves)/", c, re.I) for c in (self.model.cd or []))
        is_arms_name = bool(re.search(r"arms|hands|sleeve|glove|finger|palm", nm0, re.I))
        if re.search(r"hand|finger|palm|skin", nm0, re.I):
            info["fill"] = (.82, .64, .52)           # skin
        elif re.search(r"sleeve|arm|glove|shirt|jacket|anarchist|bill|zoey|louis|francis|coach|gambler|mechanic|producer", nm0, re.I):
            info["fill"] = (.35, .38, .42)           # fabric / sleeve (lighter so it shows on dark fog)
        elif re.search(r"scope|optic|sight|reflex|holo|rail|mount", nm0, re.I):
            info["fill"] = (.09, .09, .10)           # optic body with no texture: dark anodised metal, not flat grey
        elif is_arms_path or is_arms_name:
            info["fill"] = (.55, .48, .40)           # generic arms mesh: warm mid-tone
        else:
            info["fill"] = (.55, .55, .58)
        tried, shader_seen = [], ""
        for cand in self.model.material_paths(mat):
            cand = _norm_tex(cand)
            v_arch, vmt = _find_any(arch, cand, ".vmt")
            root, shader = {}, ""
            if vmt:
                try:
                    root, shader = _vmt_root(v_arch, vmt)
                except Exception:
                    root, shader = {}, ""
            shader_seen = shader or shader_seen
            bt = _norm_tex(root.get("$basetexture"))
            if str(root.get("$alphatest", "0")) not in ("0", ""):
                info["alphatest"] = True
            if str(root.get("$translucent", "0")) not in ("0", ""):
                info["blend"] = True
            if str(root.get("$additive", "0")) not in ("0", ""):
                info["add"] = True
            # scope lenses / glass draw a render target (_rt_*) or refract the screen: nothing we can load
            if vmt and (bt.startswith("_") or shader == "refract" or "$refracttint" in root):
                info["glass"] = True
            # $color / $color2 tint when present (helps when texture is missing)
            for ck in ("$color2", "$color"):
                if ck in root:
                    try:
                        cols = [float(x) for x in re.findall(r"-?[0-9.]+", str(root[ck]))[:3]]
                        if len(cols) == 3:
                            # Source colours are often 0-1, sometimes 0-255
                            if max(cols) > 1.5:
                                cols = [c / 255.0 for c in cols]
                            info["fill"] = tuple(max(0.0, min(1.0, c)) for c in cols)
                    except Exception:
                        pass
            # a material with only a .vtf (the .vmt lives in another addon) still works: try the name itself too
            for tp in ([bt] if bt and not bt.startswith("_") else []) + [cand]:
                tried.append(tp)
                t_arch, vtf = _find_any(arch, tp, ".vtf")
                if not vtf:
                    continue
                key = (id(t_arch), vtf)
                if key not in SrcData._tcache:
                    try:
                        rgba, real = load_vtf(t_arch, vtf, 1024)
                        rgba = np.array(rgba)
                        if not (info["alphatest"] or info["blend"] or info["add"]):
                            rgba[:, :, 3] = 255     # alpha is usually a shine mask, not see-through
                        SrcData._tcache[key] = make_texture(rgba, rgba.shape[1], rgba.shape[0])
                    except Exception as ex:
                        print("Source texture failed:", vtf, ex)
                        SrcData._tcache[key] = None
                info["t"] = SrcData._tcache[key]
                if info["t"]:
                    return info
        if not info["t"] and re.search(r"lens|glass|stencil|reticle|_rt\b|ads_scope", nm0, re.I):
            info["glass"] = True        # scope lens / reticle mask whose material lives in another addon
        if info.get("glass") and not info["t"]:
            info["blend"], info["add"], info["alphatest"] = True, False, False
            info["t"] = SrcData._glass_tex()
            self._warn_tex(nm0, "render-target / refraction material, drawn as dark glass", tried, shader_seen)
            return info
        self._warn_tex(nm0, "no .vtf found", tried, shader_seen)
        # No .vtf in the pack (common for L4D2 arms-only VPKs): bake a solid texture from the fill
        # so the mesh is visible in both the game viewmodel and the Skins preview.
        if info.get("fill") and not info["t"]:
            info["t"] = SrcData._solid_tex(info["fill"])
        return info


# viewmodel (weapon + arms) is re-skinned on the CPU at most this many times per second; lower = more fps while
# walking / animating, higher = smoother weapon motion
VM_SKIN_HZ = 75.0   # weapon + arms animation update rate (fixed)


class _MergedPart:
    """An extra model (a weapon attachment) bone-merged onto the weapon skeleton by bone name: a bone with the
    weapon's name follows the weapon's animation, any other bone hangs off its parent at its own bind pose."""

    def __init__(self, mm, data):
        self.d = data
        am = data.model
        wi = {b["name"].lower(): i for i, b in enumerate(mm.bones)}
        self.map = [wi.get(b["name"].lower(), -1) for b in am.bones]
        self.p2b = np.stack([b["p2b"] for b in am.bones]).astype(np.float32)
        Px = np.ascontiguousarray(am.v_pos, dtype=np.float32)
        bi = [np.minimum(am.v_bi[:, k], am.nb - 1).astype(np.intp) for k in range(3)]
        ws = [np.where(am.v_nb > k, am.v_w[:, k], 0.0).astype(np.float32) for k in range(3)]
        tot = np.maximum(ws[0] + ws[1] + ws[2], 1e-6)
        self.plan = SrcInst._make_skin_plan(Px, bi, [np.ascontiguousarray(w_ / tot, dtype=np.float32) for w_ in ws])
        self.lbind = []
        for b in am.bones:
            Lb = np.eye(4, dtype=np.float32)
            Lb[:3, :3] = _quat_mat3(b["quat"])
            Lb[:3, 3] = b["pos"]
            self.lbind.append(Lb)
        self.vtx = None

    def build(self, W):
        am = self.d.model
        WA = np.empty((am.nb, 4, 4), dtype=np.float32)
        for b in range(am.nb):
            wb = self.map[b]
            if wb >= 0:
                WA[b] = W[wb]
            else:
                par = am.bones[b]["par"]
                WA[b] = WA[par] @ self.lbind[b] if 0 <= par < b else self.lbind[b]
        v = SrcInst._skin_fast(np.matmul(WA, self.p2b)[:, :3, :], self.plan)
        if np.isfinite(v).all() and np.abs(v).max() < 1e4:
            self.vtx = np.ascontiguousarray(v, dtype=np.float32)


class SrcInst:
    """Per-weapon pose state. Same interface the game uses for GoldSrc models (MdlInst)."""
    is_source = True

    def __init__(self, data, arms=None):
        self.d = data
        self.a = arms           # SrcData of the arms model (bone-merged onto the weapon skeleton), or None
        self.m = data.model
        self.seqs = data.seqs
        self.last = None
        self._pose_t = 0.0
        self.bt = self.bdur = 0.0
        self.snap = self.snap_cur = None
        self.vtx = None
        self._pos_src = self._pos_val = None
        mm = self.m
        self._p2b = np.stack([b["p2b"] for b in mm.bones]).astype(np.float32)
        self._P = np.concatenate([mm.v_pos, np.ones((len(mm.v_pos), 1))], 1).astype(np.float32)
        nbn = mm.nb
        # bone parents as a flat int array (Source MDLs are parent-before-child)
        self._par = np.array([b["par"] for b in mm.bones], dtype=np.int32)
        self._bi = [np.minimum(mm.v_bi[:, k], nbn - 1).astype(np.intp) for k in range(3)]
        ws = [np.where(mm.v_nb > k, mm.v_w[:, k], 0.0).astype(np.float32) for k in range(3)]
        tot = np.maximum(ws[0] + ws[1] + ws[2], 1e-6)
        self._wk = [np.ascontiguousarray(w_ / tot, dtype=np.float32) for w_ in ws]
        # pre-split rest positions for faster skinning (xyz only; w=1 applied as translation)
        self._Px = np.ascontiguousarray(mm.v_pos, dtype=np.float32)
        self._splan = self._make_skin_plan(self._Px, self._bi, self._wk)
        depth = np.zeros(nbn, dtype=np.int32)
        for b_ in range(nbn):
            p_ = int(self._par[b_])
            depth[b_] = depth[p_] + 1 if 0 <= p_ < b_ else 0
        self._levels = [np.where(depth == d_)[0] for d_ in range(1, int(depth.max()) + 1)]   # depth 0 = roots (W = L)
        self.base_seq = 0
        self.want_empty = False
        self.avtx = None
        self._aret = {}         # arms bone -> hand / finger retarget data (see _init_arms_retarget)
        if arms is not None:
            am = arms.model
            wi = {b["name"].lower(): i for i, b in enumerate(mm.bones)}
            # Hands must follow the weapon mod's OWN animation. Exact bone name first; when the weapon names its
            # arm bones differently (CS:GO / Black Ops ports: "v_weapon.Bip01_L_Hand" vs "ValveBiped.Bip01_L_Hand"),
            # match on the name without its prefix so those hand bones still drive the arms.
            def _bn(n):
                return re.sub(r"[\s.]+", "_", n.lower().split(".")[-1].strip())
            wn = {}
            for i, b in enumerate(mm.bones):
                wn.setdefault(_bn(b["name"]), i)
            self._amap = [wi.get(b["name"].lower(), wn.get(_bn(b["name"]), -1)) for b in am.bones]
            self._ap2b = np.stack([b["p2b"] for b in am.bones]).astype(np.float32)
            self._aP = np.concatenate([am.v_pos, np.ones((len(am.v_pos), 1))], 1).astype(np.float32)
            self._aPx = np.ascontiguousarray(am.v_pos, dtype=np.float32)
            self._abi = [np.minimum(am.v_bi[:, k], am.nb - 1).astype(np.intp) for k in range(3)]
            aw = [np.where(am.v_nb > k, am.v_w[:, k], 0.0).astype(np.float32) for k in range(3)]
            at = np.maximum(aw[0] + aw[1] + aw[2], 1e-6)
            self._awk = [np.ascontiguousarray(x / at, dtype=np.float32) for x in aw]
            self._asplan = self._make_skin_plan(self._aPx, self._abi, self._awk)
            self._albind = []
            for b in am.bones:
                Lb = np.eye(4, dtype=np.float32)
                Lb[:3, :3] = _quat_mat3(b["quat"])
                Lb[:3, 3] = b["pos"]
                self._albind.append(Lb)
            try:
                self._init_arms_retarget(mm, am)
            except Exception as ex:
                print("Arms finger retarget skipped:", ex)
                self._aret = {}
        self._ex = []
        for ed in getattr(data, "extras", []):
            try:
                self._ex.append(_MergedPart(mm, ed))
            except Exception as ex:
                print("attachment skipped:", ex)
        idle = self.find("idle")
        self.pose(idle[0] if idle else 0, 0)

    # ---- sequence lookup (L4D2 v_ model names) ----
    def find(self, kind):
        r = self._find_label(kind)
        if r or kind == "idle":
            return r
        r = self._find_alt(kind)
        if not r and kind == "draw":     # only an additive *_layer draw exists (L4D2): play it on top of the idle pose
            r = [i for i, s in enumerate(self.seqs) if s["desc"] is not None and (s["flags"] & 0x4)
                 and re.search(r"ACT_VM_(DRAW|DEPLOY|PULLOUT)$", str(s.get("act") or ""), re.I)
                 and not re.search(r"item|helping", s["label"], re.I)][:1]
        return r

    def _find_alt(self, kind):
        """Fallback for packs whose sequences carry a weapon prefix (awm_fire, awm_reload, sniper_draw ...)
        or custom labels: match the sequence's ACT_VM_* activity name first, then a prefix-tolerant label.
        Skips the *_layer / *_raw helper sequences and additive (delta) sequences - those are not base clips."""
        S = self.seqs

        def usable(i):
            s = S[i]
            if s["desc"] is None or (s["flags"] & 0x4):
                return False
            lab = s["label"].lower()
            if re.search(r"(^|_)(layer|raw)$|^look_poses$|^@", lab):
                return False
            return not str(s.get("act") or "").upper().endswith("_LAYER")
        ok = [i for i in range(len(S)) if usable(i)]

        def by_act(rx, bad=None):
            return [i for i in ok if re.search(rx, str(S[i].get("act") or ""), re.I)
                    and not (bad and re.search(bad, str(S[i].get("act") or ""), re.I))]

        def by_lab(rx, bad=None):
            return [i for i in ok if re.search(rx, S[i]["label"].lower())
                    and not (bad and re.search(bad, S[i]["label"].lower()))]
        if kind == "draw":
            return (by_act(r"^ACT_VM_(DEPLOY|DRAW|PULLOUT|RAISE|DRAW_FIRST)$")
                    or by_lab(r"(^|_)(draw|deploy|pullout|ready|raise)$", r"first"))[:1]
        if kind == "shoot":
            return (by_act(r"^ACT_VM_(PRIMARYATTACK|FIRE|SHOOT)$")
                    or by_act(r"^ACT_VM_PRIMARYATTACK", r"LAST|EMPTY|DRY|SILENC|SECONDARY")
                    or by_lab(r"(^|_)(fire|shoot|attack|primary)\d*$", r"last|empty|dry|alt|secondary|zoom"))[:1]
        if kind == "reload":
            if self.want_empty:
                hit = by_act(r"^ACT_VM_RELOAD_EMPTY") or by_lab(r"(^|_)reload_?empty")
            else:
                hit = []
            hit = (hit or by_act(r"^ACT_VM_RELOAD$") or by_act(r"^ACT_VM_RELOAD", r"EMPTY|START|END|INSERT|LOOP")
                   or by_lab(r"(^|_)reload\d*$", r"empty|dry|start|end|insert|loop"))
            return hit[:1]
        if kind == "cycle":
            return (by_act(r"^ACT_VM_(PULLBACK|RECHAMBER)") or [])[:1]
        if kind == "slash":
            return (by_act(r"^ACT_VM_(MISS|HIT)CENTER") or by_lab(r"(^|_)(slash|stab|swing)"))[:1]
        return []

    def _pick_run(self, ok, lab):
        """The looping run / sprint clip. GMod weapons ship sprint_in -> sprint_idle (or sprint_loop) -> sprint_out
        (ACT_VM_SPRINT_ENTER / _IDLE / _LEAVE). Taking the first 'sprint*' label gave the 1-frame-long entry clip,
        which looped over and over: the run animation started and stopped again and again."""
        S = self.seqs
        trans = re.compile(r"(^|_)(in|out|enter|exit|leave|start|end|begin|stop|trans\w*|to|from)\d*($|_)")
        bad = re.compile(r"draw|deploy|pullout|raise|ready|fire|shoot|reload|stab|slash|swing|inspect|attack|shov|melee")
        mv = re.compile(r"run|sprint|walk|jog|move")
        best, best_sc = [], 0.0
        for i in ok:
            l, act = lab[i], str(S[i].get("act") or "").upper()
            is_mv_act = bool(re.search(r"SPRINT|RUN|WALK", act))
            if not (mv.search(l) or is_mv_act) or bad.search(l):
                continue
            if re.search(r"(ENTER|LEAVE|_IN|_OUT)$", act) or trans.search(l):
                continue                                  # entry / exit pieces are not the cycle
            sc = 1.0
            if re.search(r"SPRINT_IDLE$", act):
                sc += 6
            elif "SPRINT" in act:
                sc += 4
            elif is_mv_act:
                sc += 2
            if S[i].get("loop"):
                sc += 3
            if re.search(r"idle|loop|cycle", l):
                sc += 1.5
            sc += 2.0 if l.startswith("sprint") else (1.0 if l.startswith("run") else (0.5 if l.startswith("walk") else 0.0))
            sc += min(S[i].get("n", 1), 60) / 120.0       # a real cycle is longer than a blend-in
            if sc > best_sc:
                best, best_sc = [i], sc
        return best

    def _find_label(self, kind):
        S = self.seqs
        lab = [s["label"].lower() for s in S]
        ok = [i for i, s in enumerate(S) if s["desc"] is not None and not (s["flags"] & 0x4)]

        def pick(*rxs, bad=None):
            for rx in rxs:
                hit = [i for i in ok if re.search(rx, lab[i]) and not (bad and re.search(bad, lab[i]))]
                if hit:
                    return hit
            return []
        _mv = r"run|sprint|walk|jog|move|strafe"
        if kind == "idle":
            return (pick(r"^idle$", r"^idle(?!_active)", r"idle", bad=_mv)
                    or pick(r".", bad=_mv + r"|draw|deploy|pullout|raise|ready|fire|shoot|attack|reload|slash|stab|swing|inspect|look")[:1]
                    or pick(r".", bad=_mv)[:1] or pick(r".")[:1])
        if kind == "run":
            return self._pick_run(ok, lab)
        if kind == "draw":
            return pick(r"^(draw|deploy|pullout|ready)$", r"^raise$", r"^(draw|deploy|pullout|raise)(?!_first)",
                        r"^raise") 
        if kind == "shoot":
            return pick(r"^(fire|shoot|attack|primary)\d*$", r"^(fire|shoot|attack)", r"slash|stab|swing|hit",
                        bad=r"last|empty|dry|miss|alt|secondary|zoom")
        if kind == "slash":
            return pick(r"^midslash", r"slash", r"stab_?miss", r"^stab", r"swing", bad=r"back")
        if kind == "cycle":      # bolt / cock / pump after the shot (own sequence in GMod weapons)
            return pick(r"^(cock|bolt|cycle|rechamber|charge|pump)(_?back|_?cycle|_?action|_?open)?\d*$",
                        r"^(cock|bolt|cycle|rechamber|charge)", r"(^|_)(cock|bolt_?(back|cycle|action)|cycle|rechamber)(_|\d|$)",
                        bad=r"reload|empty|fire|shoot|attack|idle|draw|deploy|insert|start|end|inspect|sprint|run|walk|ads|iron|sight|zoom|dry")[:1]
        if kind == "reload":
            if self.want_empty:
                hit = pick(r"^reload_?empty", r"^reload")
            else:
                hit = pick(r"^reload$", r"^reload\d*$", bad=r"empty|dry|start|end|insert|loop")
                hit = hit or pick(r"^reload", bad=r"empty")
            return hit[:1]
        return []    # run / walk / jump / die are player-model things

    def is_mirrored(self):
        return False

    # ---- pose ----
    def _frame(self, si, f, loop):
        m = self.m
        s = self.seqs[si] if 0 <= si < len(self.seqs) else None
        if s is None or s["desc"] is None:
            return (np.array([b["pos"] for b in m.bones]), np.array([b["quat"] for b in m.bones], np.float64))
        n = s["n"]
        f0 = max(0, min(int(f), n - 1))
        fr = f - int(f)
        f1 = f0 + 1
        if f1 >= n:
            f1 = 0 if loop else n - 1
        p0, q0 = m.decode_frame(s["desc"], f0)
        if f1 == f0 or fr <= 1e-4:
            p, q = p0, q0
        else:
            p1, q1 = m.decode_frame(s["desc"], f1)
            dot = (q0 * q1).sum(1, keepdims=True)
            q1 = np.where(dot < 0, -q1, q1)
            q = q0 + (q1 - q0) * fr
            q = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-9)
            p = p0 + (p1 - p0) * fr
        if s["flags"] & 0x4:     # additive (delta) layer: add it on top of the idle pose
            idl = self.find("idle")
            bi = idl[0] if idl and not (self.seqs[idl[0]]["flags"] & 0x4) else -1
            if bi >= 0:
                bp, bq = m.decode_frame(self.seqs[bi]["desc"], 0)
                x1, y1, z1, w1 = bq[:, 0], bq[:, 1], bq[:, 2], bq[:, 3]
                x2, y2, z2, w2 = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
                q = np.stack([w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                              w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                              w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
                              w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2], 1)
                q = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-9)
                p = bp + p
        return p, q

    def begin_fade(self, dur=FADE_TIME):
        if self.snap_cur is not None:
            self.snap = self.snap_cur
            self.bt = self.bdur = dur

    def tick(self, dt):
        if self.bt > 0:
            self.bt = max(0.0, self.bt - dt)

    def pose(self, si, f, base=-1, bf=0, loop=False, force=False):
        # Fine quantize (1/8 frame) for smooth viewmodel motion, but rate-limit the
        # expensive CPU skin to ~120 Hz so high game FPS does not reskin every tick.
        # force=True: menu previews — no rate limit, no leftover in-game fade blend.
        if force:
            self.bt = self.bdur = 0.0
            self.snap = None
        fq = round(f * 8)
        fade_q = 0 if force else (round(self.bt * 48) if self.bt > 0 else 0)
        key = (si, fq, loop, fade_q)
        if key == self.last and not force:
            return
        now = time.perf_counter()
        if (not force and VM_SKIN_HZ > 0 and self.last is not None and self.last[0] == si
                and (now - getattr(self, "_pose_t", 0.0)) < (1.0 / VM_SKIN_HZ)):
            return  # keep previous mesh; self.last stays so a newer frame retries next tick
        self.last = key
        self._pose_t = now
        try:
            pos, quat = self._frame(si, fq / 8.0, loop)
        except Exception as ex:      # a broken / unreadable animation must never take the game down
            print("Animation decode failed (seq %s), holding rest pose:" % si, ex)
            pos, quat = self._frame(-1, 0.0, False)
        if (not force) and self.bt > 0 and self.snap is not None and self.snap[0].shape == pos.shape:
            w = 1.0 - self.bt / max(1e-4, self.bdur)
            w = w * w * (3 - 2 * w)
            sp, sq = self.snap
            dot = (quat * sq).sum(1, keepdims=True)
            q2 = np.where(dot < 0, -quat, quat)
            q = sq + (q2 - sq) * w
            quat = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-9)
            pos = sp + (pos - sp) * w
        self.snap_cur = (pos, quat)
        try:
            self._build(pos, quat)
        except Exception as ex:
            print("Pose build failed, hiding arms and retrying:", ex)
            self.a, self.avtx = None, None
            self._build(pos, quat)

    @staticmethod
    def _make_skin_plan(Px, bi, wk):
        """Split the vertices into rigid ones (one bone: most of a gun) and blended ones (2-3 bones) once,
        and lay the data out as contiguous rows so every frame is a few big vector operations."""
        nv = len(Px)
        Pt = np.ascontiguousarray(np.concatenate([Px, np.ones((nv, 1), np.float32)], 1).astype(np.float32).T)  # (4, nv)
        rigid = (wk[1] + wk[2]) < 1e-4
        ri = np.where(rigid)[0]
        si = np.where(~rigid)[0]
        return {
            "nv": nv, "ri": ri, "si": si,
            "rb": bi[0][ri], "rP": np.ascontiguousarray(Pt[:, ri]),
            "sb": [b[si] for b in bi], "sw": [np.ascontiguousarray(w[si]) for w in wk],
            "sP": np.ascontiguousarray(Pt[:, si]),
        }

    @staticmethod
    def _apply_rows(B, P):
        """B=(12,n) blended bone matrices (row-major 3x4), P=(4,n) rest positions -> (3,n) skinned positions."""
        out = np.empty((3, P.shape[1]), np.float32)
        for i in range(3):
            c = i * 4
            o = out[i]
            np.multiply(B[c], P[0], out=o)
            o += B[c + 1] * P[1]
            o += B[c + 2] * P[2]
            o += B[c + 3]
        return out

    @staticmethod
    def _skin_fast(M, pl):
        """Same result as _skin_verts, about 2-3x cheaper: M=(nb,3,4)."""
        Mt = np.ascontiguousarray(np.asarray(M, np.float32).reshape(len(M), 12).T)      # (12, nb)
        out = np.empty((pl["nv"], 3), np.float32)
        if len(pl["ri"]):
            out[pl["ri"]] = SrcInst._apply_rows(np.take(Mt, pl["rb"], axis=1), pl["rP"]).T
        if len(pl["si"]):
            B = np.take(Mt, pl["sb"][0], axis=1)
            B *= pl["sw"][0]
            tmp = np.take(Mt, pl["sb"][1], axis=1)
            tmp *= pl["sw"][1]
            B += tmp
            np.take(Mt, pl["sb"][2], axis=1, out=tmp)
            tmp *= pl["sw"][2]
            B += tmp
            out[pl["si"]] = SrcInst._apply_rows(B, pl["sP"]).T
        return out

    @staticmethod
    def _skin_verts(M, Px, bi, wk):
        """Linear-blend skinning: M=(nb,3,4), Px=(nv,3). Fast path over 3 bone weights."""
        out = np.zeros((Px.shape[0], 3), np.float32)
        for k in range(3):
            w = wk[k]
            if not w.any():
                continue
            Mk = M[bi[k]]                                          # (nv,3,4)
            # R @ v + t  without building homogeneous coords
            out += w[:, None] * (np.einsum("nij,nj->ni", Mk[:, :, :3], Px) + Mk[:, :, 3])
        return out

    def _build(self, pos, quat):
        m = self.m
        nb = m.nb
        quat = np.asarray(quat, dtype=np.float32)
        pos = np.asarray(pos, dtype=np.float32)
        x, y, z, w = quat[:, 0], quat[:, 1], quat[:, 2], quat[:, 3]
        L = np.zeros((nb, 4, 4), dtype=np.float32)
        L[:, 0, 0] = 1 - 2 * (y * y + z * z); L[:, 0, 1] = 2 * (x * y - z * w); L[:, 0, 2] = 2 * (x * z + y * w)
        L[:, 1, 0] = 2 * (x * y + z * w); L[:, 1, 1] = 1 - 2 * (x * x + z * z); L[:, 1, 2] = 2 * (y * z - x * w)
        L[:, 2, 0] = 2 * (x * z - y * w); L[:, 2, 1] = 2 * (y * z + x * w); L[:, 2, 2] = 1 - 2 * (x * x + y * y)
        L[:, :3, 3] = pos
        L[:, 3, 3] = 1
        W = L.copy()
        par = self._par
        for lv in self._levels:                  # all bones of one depth at once (a few steps, not one per bone)
            W[lv] = np.matmul(W[par[lv]], L[lv])
        M = np.matmul(W, self._p2b)[:, :3, :]                      # (nb,3,4) float32
        self.vtx = self._skin_fast(M, self._splan)
        for e in self._ex:
            try:
                e.build(W)
            except Exception as ex:
                print("attachment pose failed, hiding it:", ex)
                e.vtx = None
        if self.a is not None:
            try:
                self._build_arms(W)
            except Exception as ex:
                print("Arms pose failed, hiding arms:", ex)
                self.a, self.avtx = None, None

    _ARM_HAND_RX = re.compile(r"_([lr])_(hand|finger\d*)$", re.I)

    @staticmethod
    def _swing(a, b):
        """Smallest rotation taking direction a onto direction b."""
        a = a / (np.linalg.norm(a) or 1.0)
        b = b / (np.linalg.norm(b) or 1.0)
        v = np.cross(a, b)
        c = float(np.dot(a, b))
        s2 = float(np.dot(v, v))
        if s2 < 1e-12:
            return np.eye(3)
        K = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
        return np.eye(3) + K + K @ K * ((1.0 - c) / s2)

    def _init_arms_retarget(self, mm, am):
        """Hand / finger fix for arms packs whose skeleton differs from the weapon's.

        Plain bone-merge (arms bone = weapon bone of the same name) is only right when both models share the
        exact same bind pose. Converted weapons (Black Ops style rigs: j_index_le_1 ... bones, with
        ValveBiped.Bip01_*_Finger* helpers parented under them) have other bone axes AND another rest curl,
        so the arms mesh was skinned onto the wrong axes and the hand came out deformed.
        Per hand: (1) rigidly align the arms hand to the weapon's hand, (2) turn every arms finger segment so it
        points the way the weapon's own segment points in its bind pose, (3) every frame apply the weapon bone's
        pose change on top. The arms keep their own bone lengths, so the mesh is never stretched."""
        self._aret = {}

        def bind_world(m):
            out = np.zeros((m.nb, 4, 4), dtype=np.float64)
            for i, b in enumerate(m.bones):
                P = np.array(b["p2b"], dtype=np.float64)
                if P.shape[0] == 3:
                    P = np.vstack([P, [0.0, 0.0, 0.0, 1.0]])
                out[i] = np.linalg.inv(P)
            return out
        pairs = {"l": [], "r": []}
        for i, b in enumerate(am.bones):
            mt = self._ARM_HAND_RX.search(b["name"])
            if mt and self._amap[i] >= 0:
                pairs[mt.group(1).lower()].append((i, self._amap[i]))
        if not any(len(v) >= 4 for v in pairs.values()):
            return
        BA, BW = bind_world(am), bind_world(mm)
        for side, pr in pairs.items():
            if len(pr) < 4:
                continue
            A = np.array([BA[i][:3, 3] for i, _ in pr])
            B = np.array([BW[j][:3, 3] for _, j in pr])
            ca, cb = A.mean(0), B.mean(0)
            U, _, Vt = np.linalg.svd((A - ca).T @ (B - cb))
            dd = 1.0 if np.linalg.det(Vt.T @ U.T) >= 0 else -1.0
            R = Vt.T @ np.diag([1.0, 1.0, dd]) @ U.T
            res = np.linalg.norm((A - ca) @ R.T + cb - B, axis=1)
            if res.max() > 2.5:              # the two hands are not the same shape: keep the plain merge
                continue
            worst = 0.0
            for i, j in pr:
                c = (np.trace(BW[j][:3, :3].T @ (R @ BA[i][:3, :3])) - 1.0) / 2.0
                worst = max(worst, math.acos(max(-1.0, min(1.0, c))))
            if worst < math.radians(6.0):    # identical bind pose (normal L4D2 pack): plain merge is exact
                continue
            mapped = dict(pr)
            S = {}
            for i, j in pr:                  # arms bones come parent-before-child
                par = am.bones[i]["par"]
                kids = [c for c in mapped if am.bones[c]["par"] == i]
                if kids:
                    Ao = np.array([R @ (BA[c][:3, 3] - BA[i][:3, 3]) for c in kids])
                    Bo = np.array([BW[mapped[c]][:3, 3] - BW[j][:3, 3] for c in kids])
                    if len(kids) >= 3:        # the hand: full fit (palm roll too) over its finger roots
                        U2, _, Vt2 = np.linalg.svd(Ao.T @ Bo)
                        d2 = 1.0 if np.linalg.det(Vt2.T @ U2.T) >= 0 else -1.0
                        Si = Vt2.T @ np.diag([1.0, 1.0, d2]) @ U2.T
                    else:
                        Si = self._swing(Ao.mean(0), Bo.mean(0))
                else:
                    Si = S.get(par, np.eye(3))
                S[i] = Si
                has_par = par in mapped
                off = (R @ (BA[i][:3, 3] - BA[par][:3, 3])) if has_par else np.zeros(3)
                self._aret[i] = (j, par if has_par else -1,
                                 (Si @ R @ BA[i][:3, :3]).astype(np.float32),
                                 BW[j][:3, :3].T.astype(np.float32),
                                 Si.astype(np.float32), off.astype(np.float32))
            print("Arms: %s hand re-targeted onto the weapon's finger bones (%d bones, bind pose differs by up to %d deg)"
                  % (side.upper(), len(pr), round(math.degrees(worst))))

    def _build_arms(self, W):
        if self.a is not None:
            am = self.a.model
            WA = np.empty((am.nb, 4, 4), dtype=np.float32)
            RdS = {}
            for b in range(am.nb):
                wb = self._amap[b]
                rt = self._aret.get(b)
                if rt is not None:                # hand / finger: weapon pose change on the re-aimed arms bind
                    j, par, Rin, RwT, Sb, off = rt
                    Rd = W[j][:3, :3] @ RwT
                    M = np.eye(4, dtype=np.float32)
                    M[:3, :3] = Rd @ Rin
                    if par >= 0:                  # keep the arms' own bone length: hang off the parent bone
                        M[:3, 3] = WA[par][:3, 3] + RdS[par] @ off
                    else:
                        M[:3, 3] = W[j][:3, 3]
                    RdS[b] = Rd @ Sb
                    WA[b] = M
                elif wb >= 0:                     # same bone name as the weapon: follow the weapon animation
                    WA[b] = W[wb]
                else:
                    par = am.bones[b]["par"]
                    WA[b] = WA[par] @ self._albind[b] if 0 <= par < b else self._albind[b]
            MA = np.matmul(WA, self._ap2b)[:, :3, :]
            av = self._skin_fast(MA, self._asplan)
            if not np.isfinite(av).all() or np.abs(av).max() > 1e4:
                # a retargeted hand went bad on this frame (reload poses bend the wrists a lot): keep the
                # last good arms mesh instead of feeding NaN / huge vertices to OpenGL
                if self.avtx is None:
                    raise ValueError("arms skinning produced invalid vertices")
                return
            self.avtx = np.ascontiguousarray(av, dtype=np.float32)

    @property
    def pos(self):
        """Vertices without far-away parked parts (spare magazine ...): used for preview framing."""
        v = self.vtx
        if v is None:
            return [None]
        if self._pos_src is not v:
            med = np.median(v, axis=0)
            keep = (np.abs(v - med) < 30).all(axis=1)
            self._pos_src, self._pos_val = v, [np.ascontiguousarray(v[keep] if keep.sum() > 50 else v)]
        return self._pos_val

    @pos.setter
    def pos(self, _):
        pass

    def min_z(self):
        return float(self.vtx[:, 2].min()) if self.vtx is not None else 0.0

    def draw(self):
        if self.vtx is None:
            return
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        try:
            self._draw_set(self.d, self.vtx)
            for e in self._ex:
                if e.vtx is not None:
                    self._draw_set(e.d, e.vtx)
        finally:
            gl.glDisable(gl.GL_ALPHA_TEST)
        if self.a is not None and self.avtx is not None:
            try:
                self._draw_set(self.a, self.avtx)
            except Exception as ex:
                print("Arms draw failed, hiding arms:", ex)
                self.a, self.avtx = None, None
                gl.glDisable(gl.GL_ALPHA_TEST)
                gl.glDisable(gl.GL_BLEND)
                gl.glDepthMask(gl.GL_TRUE)
        tex2d(True)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)

    def draw_world(self):
        """The weapon alone (no arms pack, no built-in hands): what a player / bot holds."""
        if self.vtx is None:
            return
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        try:
            self._draw_set(self.d, self.vtx, True)
            for e in self._ex:
                if e.vtx is not None:
                    self._draw_set(e.d, e.vtx, True)
        finally:
            gl.glDisable(gl.GL_ALPHA_TEST)
        tex2d(True)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)

    @staticmethod
    def _draw_set(data, v, skip_arm=False):
        gl.glVertexPointer(3, gl.GL_FLOAT, 0, v)
        gl.glTexCoordPointer(2, gl.GL_FLOAT, 0, data.uv)
        gl.glColor4f(1, 1, 1, 1)
        order = sorted(data.parts, key=lambda p: (p["mat"]["add"] or p["mat"]["blend"]))
        for p in order:
            if skip_arm and p.get("arm"):
                continue
            mt = p["mat"]
            if mt["t"]:
                gl.glBindTexture(gl.GL_TEXTURE_2D, mt["t"])
                tex2d(True)
            else:
                tex2d(False)
                gl.glColor4f(*mt.get("fill", (.47, .47, .47)), 1)
            if mt["alphatest"]:
                gl.glEnable(gl.GL_ALPHA_TEST)
                gl.glAlphaFunc(gl.GL_GREATER, .5)
            if mt["add"] or mt["blend"]:
                gl.glEnable(gl.GL_BLEND)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE if mt["add"] else gl.GL_ONE_MINUS_SRC_ALPHA)
                gl.glDepthMask(gl.GL_FALSE)
            gl.glDrawElements(gl.GL_TRIANGLES, len(p["idx"]), gl.GL_UNSIGNED_INT, p["idx"])
            gl.glDisable(gl.GL_ALPHA_TEST)
            if mt["add"] or mt["blend"]:
                gl.glDepthMask(gl.GL_TRUE)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                gl.glDisable(gl.GL_BLEND)
            gl.glColor4f(1, 1, 1, 1)


# --------------------------------------------------------------------------
# Source engine (L4D2) PLAYER models
#   These packs usually hold only the mesh + textures (the animations live in other game files),
#   so the body is posed here in code: walk / run cycle, crouch, slide, jump, aim pitch, torso
#   twist, two-handed weapon hold (arm IK), shooting kick, reload and a death fall.
#   Everything is done in model space with one cumulative rotation per bone (D) about the bone's
#   rest joint, so it does not matter how the model's bone axes are oriented.
# --------------------------------------------------------------------------
def _cross3(a, b):
    return np.array((a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]))


def _rot_axis(ax, ang):
    x, y, z = float(ax[0]), float(ax[1]), float(ax[2])
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / n, y / n, z / n
    c, s = math.cos(ang), math.sin(ang)
    t = 1 - c
    return np.array(((c + x * x * t, x * y * t - z * s, x * z * t + y * s),
                     (y * x * t + z * s, c + y * y * t, y * z * t - x * s),
                     (z * x * t - y * s, z * y * t + x * s, c + z * z * t)))


def _arc(a, b):
    """Rotation matrix that turns vector a into vector b (shortest way)."""
    na = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) or 1.0
    nb_ = math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2]) or 1.0
    a = (a[0] / na, a[1] / na, a[2] / na)
    b = (b[0] / nb_, b[1] / nb_, b[2] / nb_)
    c = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    if c > 0.999999:
        return np.eye(3)
    if c < -0.999999:
        ax = _cross3(a, (1.0, 0.0, 0.0))
        if abs(ax).sum() < 1e-3:
            ax = _cross3(a, (0.0, 1.0, 0.0))
        return _rot_axis(ax, math.pi)
    v0, v1, v2 = a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]
    vx = np.array(((0.0, -v2, v1), (v2, 0.0, -v0), (-v1, v0, 0.0)))
    return np.eye(3) + vx + (vx @ vx) * (1.0 / (1.0 + c))


def _sstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / ((b - a) or 1e-9)))
    return t * t * (3 - 2 * t)


class _NoSeqs:
    """What update_entity_anim expects in pm.d: a source player model has no sequences."""
    seqs = []

    def pick(self, *pats):
        return -1


class SrcPlayerInst:
    """Procedurally animated Source player model. Vertices come out in game axes
    (x = forward, y = left, z = up), feet at z = 0 when standing."""
    is_source = True
    is_src_player = True
    _I3 = np.eye(3)

    def __init__(self, data):
        self.data = data
        m = self.m = data.model
        self.d = _NoSeqs()
        # Real sequence list from the MDL (used when the pack ships multi-frame clips)
        self.seqs = list(getattr(m, "seqs", []) or [])
        self.base_seq = 0
        self.last = None
        nb = m.nb
        self.par = [b["par"] for b in m.bones]
        # ---- rest pose in model space (world rotation + joint position of every bone)
        Wr, Wp = [None] * nb, [None] * nb
        for i, b in enumerate(m.bones):
            R = _quat_mat3(b["quat"])
            p = self.par[i]
            if p >= 0:
                Wr[i] = Wr[p] @ R
                Wp[i] = Wp[p] + Wr[p] @ np.asarray(b["pos"], np.float64)
            else:
                Wr[i] = R
                Wp[i] = np.asarray(b["pos"], np.float64).copy()
        self.Wr, self.Wp = Wr, Wp
        # ---- bones by role
        ix = {}
        for i, b in enumerate(m.bones):
            # ValveBiped.Bip01_Head1 / bip01_head / ValveBiped.Head — normalise to short role name
            raw = b["name"].lower()
            k = re.sub(r"^.*bip01_", "", raw)
            k = re.sub(r"^valvebiped\.", "", k)
            ix.setdefault(k, i)
            # aliases used by some L4D2 / GMod packs
            if k in ("head", "head1", "head2"):
                ix.setdefault("head1", i)
            if k in ("neck", "neck1"):
                ix.setdefault("neck1", i)
        self.ix = ix
        # L4D2 hunters/survivors use spine / spine1 / spine2 / spine4 (no spine3)
        self.spine = [ix[k] for k in ("spine", "spine1", "spine2", "spine3", "spine4") if k in ix]
        need = ("pelvis", "l_thigh", "r_thigh", "l_calf", "r_calf", "l_foot", "r_foot",
                "l_upperarm", "r_upperarm", "l_forearm", "r_forearm", "l_hand", "r_hand")
        has_head = "head1" in ix or "head" in ix
        self.can = bool(self.spine) and has_head and all(k in ix for k in need)
        if "head1" not in ix and "head" in ix:
            ix["head1"] = ix["head"]
        # ---- model axes: left from the hips, forward = left x up (right handed)
        U = np.array([0.0, 0.0, 1.0])
        F, L = np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])
        if self.can:
            l = Wp[ix["l_thigh"]] - Wp[ix["r_thigh"]]
            l[2] = 0.0
            if np.linalg.norm(l) > 1e-3:
                L = l / np.linalg.norm(l)
                F = _cross3(L, U)
            toes = [(ix["l_toe0"], ix["l_foot"]), (ix["r_toe0"], ix["r_foot"])] if "l_toe0" in ix and "r_toe0" in ix else []
            if toes and sum(float(np.dot(Wp[t] - Wp[f], F)) for t, f in toes) < 0:
                F, L = -F, -L          # toes point the other way: face them
        self.F, self.L, self.U = F, L, U
        self.Rg = np.array([F, L, U])      # model -> game axes
        self._init_fingers()
        # ---- skinning: one entry per (vertex, bone) influence, sorted by bone
        nbn = nb
        nv = len(m.v_pos)
        ev, eb, ew = [], [], []
        for k in range(3):
            w = np.where(m.v_nb > k, m.v_w[:, k], 0.0).astype(np.float64)
            keep = w > 1e-5
            ev.append(np.nonzero(keep)[0])
            eb.append(np.minimum(m.v_bi[keep, k], nbn - 1))
            ew.append(w[keep])
        ev, eb, ew = np.concatenate(ev), np.concatenate(eb), np.concatenate(ew)
        tot = np.zeros(nv)
        np.add.at(tot, ev, ew)
        ew = ew / np.maximum(tot[ev], 1e-9)
        ev, eb, ew = self._skirt_damp(ev, eb, ew)          # robes / long coats: stop the legs dragging the cloth
        order = np.argsort(eb, kind="stable")
        self.ev, self.eb, self.ew = ev[order], eb[order], ew[order]
        Wp_a = np.array(Wp)
        self.er = (m.v_pos[self.ev] - Wp_a[self.eb])            # vertex in the joint's rest frame (model axes)
        self.nv = nv
        bs = np.searchsorted(self.eb, np.arange(nb + 1))
        self._act = [(b, int(bs[b]), int(bs[b + 1])) for b in range(nb) if bs[b + 1] > bs[b]]
        self.er32 = np.ascontiguousarray(self.er, np.float32)
        self.ew32 = np.ascontiguousarray(self.ew, np.float32)[:, None]
        self._o = np.empty((len(self.eb), 3), np.float32)
        self.ph = 0.0
        self._snap = None
        self._simple_legs = [(0.0, 0.03, 0.0), (0.0, 0.03, 0.0)]
        self._simple_lean = 0.0
        self.leg = 36.0
        self._seg = (17.0, 17.0)
        if self.can:
            t_ = float(np.linalg.norm(Wp[ix["l_calf"]] - Wp[ix["l_thigh"]]))
            c_ = float(np.linalg.norm(Wp[ix["l_foot"]] - Wp[ix["l_calf"]]))
            if t_ > 1.0 and c_ > 1.0:
                self._seg = (t_, c_)
                self.leg = t_ + c_
        self.vtx = None
        self.grip_g = None      # (right-hand pos, forward, left, up) in game axes, set by _solve
        self._key = None
        self.rest_min = float(m.v_pos[:, 2].min())
        self._skin_rest()
        self.rest_min = float(self.vtx[:, 2].min())

    # ---- loose cloth (robes, dresses, long coats) -------------------------------------------------------
    SKIRT_KEEP = 0.30        # share of the leg swing that far-from-the-leg cloth still follows (1 = no damping)

    def _skirt_damp(self, ev, eb, ew):
        """Cloth hanging well away from the legs is skinned to thigh / calf / foot bones on many player models.
        Swinging those bones at run speed shears it into a long sheet behind the player. Here the leg weight of
        such vertices is moved to the pelvis, smoothly with the distance from the leg, so the hem sways with the
        hips instead. Models whose cloth hugs the legs (trousers) are left alone."""
        ix, Wp, m = self.ix, self.Wp, self.m
        if not self.can or "pelvis" not in ix:
            return ev, eb, ew
        legb = [ix[pre + k] for pre in ("l_", "r_") for k in ("thigh", "calf", "foot", "toe0") if pre + k in ix]
        segs = []
        for pre in ("l_", "r_"):
            ch = [ix[pre + k] for k in ("thigh", "calf", "foot", "toe0") if pre + k in ix]
            segs += [(Wp[a], Wp[b]) for a, b in zip(ch, ch[1:])]
        idx = np.nonzero(np.isin(eb, legb))[0]
        if not len(idx) or not segs:
            return ev, eb, ew
        P = m.v_pos[ev[idx]]
        dist = np.full(len(idx), 1e9)
        for a, b in segs:
            ab = b - a
            t = np.clip(((P - a) @ ab) / max(float(ab @ ab), 1e-9), 0.0, 1.0)
            dist = np.minimum(dist, np.linalg.norm(P - (a + t[:, None] * ab), axis=1))
        leg = float(np.linalg.norm(Wp[ix["l_calf"]] - Wp[ix["l_thigh"]]) + np.linalg.norm(Wp[ix["l_foot"]] - Wp[ix["l_calf"]]))
        r0, r1 = 0.08 * leg, 0.20 * leg
        wl = ew[idx]
        far = float((wl * (dist > r1)).sum() / max(float(wl.sum()), 1e-9))
        if far < 0.12:                       # trousers / tight clothing: nothing to do
            return ev, eb, ew
        t = np.clip((dist - r0) / (r1 - r0), 0.0, 1.0)
        t = t * t * (3.0 - 2.0 * t)
        k = 1.0 - (1.0 - self.SKIRT_KEEP) * t
        moved = wl * (1.0 - k)
        ew = ew.copy()
        ew[idx] = wl * k
        keep = moved > 1e-4
        pel = np.full(int(keep.sum()), ix["pelvis"], dtype=eb.dtype)
        print("Player model: loose cloth detected (%.0f%% of the leg weights), leg swing damped on it" % (far * 100))
        self._skirt_v = np.unique(ev[idx][keep])      # the hem rests on the floor instead of sinking through it
        return (np.concatenate([ev, ev[idx][keep]]), np.concatenate([eb, pel]),
                np.concatenate([ew, moved[keep]]))

    # ---- hands: finger curl + palm direction ---------------------------------------------------------------
    GRIP_CURL = ((0.75, 1.05, 0.70), (0.35, 0.40, 0.30))      # fingers 1-4 (3 joints) / thumb, radians

    def _init_fingers(self):
        """Curl axis of every finger chain and the palm direction, read from the model's own rest pose
        (a relaxed hand is slightly curled, so the bend direction of each chain tells which way to close it)."""
        ix, Wp = self.ix, self.Wp
        self.fing = {}        # side -> [(bone, axis)] for the 4 fingers, [(bone, axis)] for the thumb
        self.palm0 = {}       # side -> unit palm direction (model axes, rest pose)
        for pre in ("l_", "r_"):
            try:
                chains = []
                for f in range(5):
                    ch = [ix["%sfinger%s" % (pre, n)] for n in (f, "%d1" % f, "%d2" % f)]
                    chains.append(ch)
            except KeyError:
                continue
            axes, dirs = [], []
            for ch in chains:
                d1, d2 = Wp[ch[1]] - Wp[ch[0]], Wp[ch[2]] - Wp[ch[1]]
                c = _cross3(d1, d2)
                n = float(np.linalg.norm(c))
                tip = Wp[ch[2]] - Wp[ch[0]]
                axes.append(c / n if n > 0.03 else None)
                dirs.append(tip / (np.linalg.norm(tip) or 1.0))
            good = [a for a in axes[1:] if a is not None]
            if not good:
                continue
            ax = sum(good)
            ax = ax / (np.linalg.norm(ax) or 1.0)
            th = axes[0] if axes[0] is not None else ax
            self.fing[pre] = ([(b, ax) for ch in chains[1:] for b in ch],
                              [(b, th) for b in chains[0]])
            dm = sum(dirs[1:])
            dm = dm / (np.linalg.norm(dm) or 1.0)
            pv = _cross3(ax, dm)               # the way a closing finger moves = palm side
            self.palm0[pre] = pv / (np.linalg.norm(pv) or 1.0)

    def _finger_pose(self, E, curl):
        """Close both hands by 'curl' (0 = rest pose, 1 = full grip)."""
        if curl <= 0.0 or not self.fing:
            return
        for pre, (fs, th) in self.fing.items():
            for j, (b, ax) in enumerate(fs):
                E[b] = _rot_axis(ax, self.GRIP_CURL[0][j % 3] * curl)
            for j, (b, ax) in enumerate(th):
                E[b] = _rot_axis(ax, self.GRIP_CURL[1][j % 3] * curl)

    # ---- same small interface the game uses for GoldSrc models ----
    def find(self, kind):
        return []

    def is_mirrored(self):
        return False

    def tick(self, dt):
        pass

    def begin_fade(self, dur=0.2):
        pass

    def pose(self, *a, **k):
        pass

    @property
    def pos(self):
        return [self.vtx]

    @pos.setter
    def pos(self, _):
        pass

    def min_z(self):
        return float(self.vtx[:, 2].min()) if self.vtx is not None else 0.0

    def draw(self):
        if self.vtx is None:
            return
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        SrcInst._draw_set(self.data, self.vtx)
        tex2d(True)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)

    # ---- rotations in the model's own forward / left / up frame ----
    def _RL(self, a):      # + = lean forward / swing a hanging limb backward
        return _rot_axis(self.L, a)

    def _RU(self, a):      # + = turn left
        return _rot_axis(self.U, a)

    def _RF(self, a):      # + = swing a hanging limb out to the left
        return _rot_axis(self.F, a)

    # ---- two-bone arm IK: returns the new cumulative rotations of upper arm and forearm ----
    def _arm(self, ua, fa, Dp, S, T, pole):
        Wp = self.Wp
        u = float(np.linalg.norm(Wp[fa] - Wp[ua]))
        hand = self.ix["l_hand"] if ua == self.ix["l_upperarm"] else self.ix["r_hand"]
        f = float(np.linalg.norm(Wp[hand] - Wp[fa]))
        v = T - S
        d = float(np.linalg.norm(v)) or 1e-6
        dirv = v / d
        d = max(abs(u - f) + 0.5, min(u + f - 0.05, d))
        a = (u * u - f * f + d * d) / (2 * d)
        h = math.sqrt(max(u * u - a * a, 0.0))
        pv = pole - dirv * float(np.dot(pole, dirv))
        n = float(np.linalg.norm(pv))
        pv = pv / n if n > 1e-6 else _cross3(dirv, self.L)
        elbow = S + dirv * a + pv * h
        hand_p = S + dirv * d
        r_ua = (Wp[fa] - Wp[ua])
        r_fa = (Wp[hand] - Wp[fa])
        Dua = _arc(Dp @ r_ua, elbow - S) @ Dp
        Dfa = _arc(Dua @ r_fa, hand_p - elbow) @ Dua
        return Dua, Dfa

    # ---- public: alive pose ----
    @staticmethod
    def _mix(a, b, t):
        return tuple(x + (y - x) * t for x, y in zip(a, b))

    # ---- simple, smooth procedural player animation: idle / walk / run / crouch / slide / jump / shoot ----
    @staticmethod
    def _sm(cur, tgt, rate, dt):
        """Frame-rate independent smoothing toward a target."""
        return cur + (tgt - cur) * (1.0 - math.exp(-rate * dt))

    def _leg(self, a, k, p=None):
        """(hip, knee, ankle) for one leg; the ankle keeps the foot about flat unless p is given."""
        return (a, k, -0.12 * (a - k) if p is None else p)

    def animate(self, dt, state, spd=0.0, pitch=0.0, kick=0.0, vz=0.0, melee=False, reload=None, hold=None):
        """state: idle | run | crouch | slide | jump.  spd = ground speed, vz = vertical speed,
        kick = 0..1 shot pulse (recoil for guns, slash for a knife), reload = None or 0..1 progress.
        Every pose weight is smoothed, so walking into a crouch, a jump or a slide never pops."""
        if not self.can:
            self._skin_rest()
            return
        dt = max(0.0, min(float(dt), 0.1))
        S = getattr(self, "_an", None)
        if S is None:
            S = self._an = dict(t=0.0, ph=0.0, g=0.0, run=0.0, cr=0.0, air=0.0, sl=0.0, up=0.0, land=0.0,
                                k=0.0, fz=0.0)
        S["t"] += dt
        # ---- target weights from the game state ----
        in_air = state == "jump"
        g_t = _sstep(6.0, 90.0, spd) if not in_air else 0.0           # walking gait strength
        run_t = _sstep(110.0, 230.0, spd)                              # walk -> run (longer, bigger strides)
        cr_t = 1.0 if state == "crouch" else 0.0
        sl_t = 1.0 if state == "slide" else 0.0
        was_air = S["air"]
        S["g"] = self._sm(S["g"], g_t, 11.0, dt)
        S["run"] = self._sm(S["run"], run_t, 7.0, dt)
        S["cr"] = self._sm(S["cr"], cr_t, 12.0, dt)
        S["sl"] = self._sm(S["sl"], sl_t, 16.0, dt)
        S["air"] = self._sm(S["air"], 1.0 if in_air else 0.0, 16.0 if in_air else 20.0, dt)
        S["up"] = self._sm(S["up"], max(-1.0, min(1.0, vz / 260.0)), 10.0, dt)   # +1 rising .. -1 falling
        if was_air > 0.5 and S["air"] <= 0.5 and not in_air:
            S["land"] = 1.0                                            # touched down: knees absorb it
        S["land"] = max(0.0, S["land"] - dt * 5.0)
        # shot pulse: snaps in, eases out
        S["k"] = self._sm(S["k"], kick, 45.0 if kick > S["k"] else 14.0, dt)
        g, run, cr, air, sl, up = S["g"], S["run"], S["cr"], S["air"], S["sl"], S["up"]
        land = S["land"]
        # ---- gait phase: stride length grows with the leg, so the feet match the ground speed ----
        stride = max(30.0, self.leg * (2.3 + 0.5 * run) * (1.0 - 0.35 * cr))
        S["ph"] = (S["ph"] + dt * min(3.8, spd / stride)) % 1.0
        p = S["ph"] * 2.0 * math.pi
        ground = max(0.0, 1.0 - air) * (1.0 - sl)
        cw = min(1.0, cr + 0.5 * land)                                 # crouch weight (landing dips a bit)
        legs = []
        for side in (0, 1):
            th = p + side * math.pi
            amp = (0.26 + 0.40 * run) * g * (1.0 - 0.5 * cw) * ground
            a = amp * math.sin(th)
            k = (0.06 + g * (0.10 + (0.50 + 0.85 * run) * max(0.0, math.cos(th - 0.4)) ** 1.4)
                 * (1.0 - 0.35 * cw)) * ground
            # standing -> crouching: thighs forward, knees folded (feet stay under the hips via squat)
            a += 0.95 * cw
            k += 1.70 * cw * (1.0 - 0.15 * g)
            ground_leg = self._leg(a, k)
            # in the air: rising = knees tucked (lead leg forward when running), falling = legs reach down
            rj = _sstep(40.0, 160.0, spd)
            if side == 0:
                rise = self._mix((0.60, 1.10), (0.95, 1.55), rj)
                fall = (0.30, 0.35)
            else:
                rise = self._mix((0.45, 0.90), (-0.15, 0.95), rj)
                fall = (-0.05, 0.25)
            tuck = self._mix(fall, rise, _sstep(-0.3, 0.4, up))
            air_leg = self._leg(tuck[0], tuck[1])
            leg = self._mix(ground_leg, air_leg, air)
            # sliding: lead leg straight out, the other knee folded under
            sl_leg = (1.30, 0.05, -0.25) if side == 0 else (0.55, 2.0, 0.2)
            legs.append(self._mix(leg, sl_leg, sl))
        # ---- upper body ----
        breath = 0.012 * math.sin(S["t"] * 1.7) * (1.0 - g)
        lean = breath + 0.05 * g + 0.13 * run * g + 0.30 * cw + 0.08 * air * (1.0 - sl) - 0.5 * sl
        lean -= 0.07 * S["k"]                                          # the shot rocks the torso back
        sway = g * ground
        twist = 0.06 * run * sway * math.sin(p)
        hip_yaw = -0.09 * (0.4 + 0.6 * run) * sway * math.sin(p)
        arm_drop = 0.5 * run * g * ground * (0.5 + 0.5 * math.cos(2.0 * p)) + 0.3 * sl
        squat = max(cw, 0.6 * sl)
        rl = None if reload is None else max(0.0, min(1.0, float(reload)))
        # ---- skip the solve if nothing visible changed ----
        flat = [x for lg in legs for x in lg]
        key = tuple(round(x, 3) for x in flat + [lean, twist, hip_yaw, pitch, S["k"], air, arm_drop, squat,
                                                  -1.0 if rl is None else rl]) + (bool(melee), None if hold is None else (round(hold[0], 2), round(hold[1], 2)))
        self._snap = (legs, lean)
        if key == self._key:
            return
        self._key = key
        self._solve(legs, lean, twist, pitch, S["k"], rl, melee, ground=True, hip_yaw=hip_yaw,
                    air_arms=air * (1.0 - sl), arm_drop=arm_drop, squat=squat, hold=hold)

    # ---- core solver ----
    def _solve(self, legs, lean, twist, pitch, kick, reload, melee, ground=True, root=None, extra=None,
               hip_yaw=0.0, air_arms=0.0, arm_drop=0.0, squat=0.0, ik=None, arm_local=None, hold=None):
        m, ix, Wp = self.m, self.ix, self.Wp
        nb = m.nb
        F, L, U = self.F, self.L, self.U
        I3 = self._I3
        E = {}
        self.grip_g = None
        _grip = None
        # spine: lean + share of the aim pitch + twist (the hips' own yaw is counter-rotated so the shoulders stay square)
        sp = self.spine
        w = np.array([1.0, 1.2, 1.5, 1.5][-len(sp):] if len(sp) <= 4 else [1.0] * len(sp))
        w = w / w.sum()
        pit_sp = pitch * 0.45
        tw = np.array([0.15] + [0.25] * (len(sp) - 1)) if len(sp) > 1 else np.array([1.0])
        tw = tw / tw.sum()
        tw_eff = twist - hip_yaw * 0.85
        for j, bi in enumerate(sp):
            E[bi] = self._RU(tw_eff * tw[j]) @ self._RL((lean - pit_sp) * w[j])
        if hip_yaw and "pelvis" in ix:
            E[ix["pelvis"]] = self._RU(hip_yaw)
        if "neck1" in ix:
            E[ix["neck1"]] = self._RL(-pitch * 0.15 - lean * 0.30)
        E[ix["head1"]] = self._RL(-pitch * 0.40 - lean * 0.55)       # the head keeps looking ahead
        # legs: hip angle, knee bend, ankle (flat on the floor unless a plantar-flex is given)
        for side, pre in ((0, "l_"), (1, "r_")):
            lg = legs[side]
            a, k = lg[0], lg[1]
            foot = (a - k) * 0.88 if len(lg) < 3 else (a - k) + lg[2]
            E[ix[pre + "thigh"]] = self._RL(-a)
            E[ix[pre + "calf"]] = self._RL(k)
            E[ix[pre + "foot"]] = self._RL(foot)
        self._finger_pose(E, 1.0)
        if extra:
            for bi, r in extra.items():
                E[bi] = r @ E[bi] if bi in E else r
        # ---------- forward kinematics (arms solved with IK on the way) ----------
        D, Pn = [None] * nb, [None] * nb
        ovr = {}
        chest = sp[-1]
        l_ua, r_ua = ix["l_upperarm"], ix["r_upperarm"]
        l_fa, r_fa = ix["l_forearm"], ix["r_forearm"]
        use_ik = ik if ik is not None else (extra is None)
        for i in range(nb):
            p = self.par[i]
            Dp = D[p] if p >= 0 else I3
            if p >= 0:
                Pn[i] = Pn[p] + Dp @ (Wp[i] - Wp[p])
            elif root is not None:
                Rr, piv, sh = root[0], root[1], root[2]
                pre_s = root[3] if len(root) > 3 else 0.0
                Pn[i] = piv + Rr @ (Wp[i] + pre_s - piv) + sh
                Dp = Rr
            else:
                Pn[i] = Wp[i].copy()
            if i in ovr:
                D[i] = ovr[i]
            elif p < 0 and root is not None:
                D[i] = root[0] @ E[i] if i in E else root[0]
            else:
                D[i] = Dp @ E[i] if i in E else Dp
            if use_ik and i in (l_ua, r_ua):
                left = (i == l_ua)
                Dc = D[chest]
                C = Pn[chest]
                if arm_local is not None:
                    hl = arm_local[0] if left else arm_local[1]
                    T = C + Dc @ (F * hl[0] + L * hl[1] + U * hl[2])
                    pl_ = arm_local[2]
                    pole = Dc @ (F * pl_[0] + L * (pl_[1] if left else -pl_[1]) + U * pl_[2])
                else:
                    Fh = Dc @ F
                    Fh = np.array([Fh[0], Fh[1], 0.0])
                    Fh = Fh / (np.linalg.norm(Fh) or 1.0)
                    Fa = Fh * math.cos(pitch) + U * math.sin(pitch)
                    Ua = U - Fa * float(np.dot(U, Fa))
                    Ua = Ua / (np.linalg.norm(Ua) or 1.0)
                    La = _cross3(Ua, Fa)
                    back = 1.6 * kick
                    up = 0.9 * kick
                    if melee:
                        s_ = 1.0 - max(0.0, min(1.0, kick))          # 0 at the start of a slash .. 1 at the end
                        sw = math.sin(s_ * math.pi) if kick > 0 else 0.0
                        Tr = C + Fa * (HELD_KNIFE_REACH + 8 * sw) - La * (HELD_KNIFE_SIDE - 9 * s_ * (kick > 0)) - Ua * (HELD_KNIFE_DROP - 8 * sw)
                        Tl = C + Fa * 4 + La * 5 - Ua * 17
                    else:
                        if hold is not None:
                            # hands go exactly where the held gun wants them: right hand on the grip,
                            # left hand on the foregrip (hold = forward / up distance between the two hands)
                            sep = max(4.0, min(float(hold[0]), 26.0))
                            Tr = C + Fa * (max(1.0, 21.0 - sep) - back) - La * 3.0 - Ua * (12.0 - up)
                            Tl = Tr + Fa * sep + Ua * float(hold[1])
                        else:
                            Tr = C + Fa * (8.0 - back) - La * 3.0 - Ua * (12.0 - up)
                            Tl = C + Fa * (21.0 - back) + La * 1.0 - Ua * (7.0 - up)
                        if reload is not None:
                            down = _sstep(0.0, 0.22, reload) - _sstep(0.68, 0.92, reload)
                            slap = 1.5 * math.sin(max(0.0, min(1.0, (reload - 0.4) / 0.2)) * math.pi)
                            pouch = C + Fa * 6 + La * 5 - Ua * (19.0 + slap)
                            Tl = Tl + (pouch - Tl) * down
                            Tr = Tr + (C + Fa * 7 - La * 3 - Ua * 14 - Tr) * down * 0.5
                    if air_arms > 0:                                  # arms lift and open for balance
                        Tr = Tr + U * (3.5 * air_arms) - La * (3.0 * air_arms)
                        Tl = Tl + U * (3.5 * air_arms) + La * (3.0 * air_arms)
                    if arm_drop > 0:
                        Tr = Tr - U * (2.5 * arm_drop)
                        Tl = Tl - U * (2.5 * arm_drop)
                    T = Tl if left else Tr
                    if not left:
                        _grip = (np.array(Tr, np.float64), np.array(Fa), np.array(La), np.array(Ua),
                                 np.array(Tl, np.float64), bool((not melee) and reload is None))
                    pole = -Ua * 1.0 + (La if left else -La) * 0.55
                fa = l_fa if left else r_fa
                Dua, Dfa = self._arm(i, fa, Dp, Pn[i], T, pole)
                pre_ = "l_" if left else "r_"
                if arm_local is None and pre_ in self.palm0:
                    # the IK only aims the arm; roll the forearm so the palm faces where a grip needs it
                    # (right hand: palm to the left, onto the grip; left hand: palm up and a little inward)
                    want = (Ua * 0.8 - La * 0.5) if left else La
                    fd = Dfa @ (Wp[ix[pre_ + "hand"]] - Wp[fa])
                    fd = fd / (np.linalg.norm(fd) or 1.0)
                    pcur = Dfa @ self.palm0[pre_]
                    pp = pcur - fd * float(np.dot(pcur, fd))
                    tp = want - fd * float(np.dot(want, fd))
                    if np.linalg.norm(pp) > 1e-3 and np.linalg.norm(tp) > 1e-3:
                        ang = math.atan2(float(np.dot(fd, _cross3(pp, tp))), float(np.dot(pp, tp)))
                        Dfa = _rot_axis(fd, max(-2.2, min(2.2, ang))) @ Dfa
                D[i] = Dua
                ovr[fa] = Dfa
        # ground lock: keep the lowest foot on the floor (and, squatting, the feet under the hips)
        shift = np.zeros(3)
        if ground and root is None:
            fz = min(Pn[ix[k]][2] for k in ("l_foot", "r_foot", "l_toe0", "r_toe0") if k in ix)
            rz = min(Wp[ix[k]][2] for k in ("l_foot", "r_foot", "l_toe0", "r_toe0") if k in ix)
            shift[2] = rz - fz
            if squat > 0:
                mf = 0.5 * (Pn[ix["l_foot"]] + Pn[ix["r_foot"]])
                rmf = 0.5 * (Wp[ix["l_foot"]] + Wp[ix["r_foot"]])
                shift = shift - F * float(np.dot(mf - rmf, F)) * min(1.0, squat) * 0.9
        if _grip is not None:
            Rg = self.Rg
            self.grip_g = (Rg @ (_grip[0] + shift), Rg @ _grip[1], Rg @ _grip[2], Rg @ _grip[3],
                           Rg @ (_grip[4] + shift), _grip[5])
        self._skin(D, Pn, shift)

    def _skin_rest(self):
        nb = self.m.nb
        D = [self._I3] * nb
        self._skin(D, [np.asarray(w) for w in self.Wp], np.zeros(3))

    def _skin(self, D, Pn, shift):
        """Linear-blend skinning: every (vertex, bone) influence is moved with one small matmul per
        bone, then the weighted results are summed per vertex."""
        Dt = np.ascontiguousarray(np.stack(D).transpose(0, 2, 1), np.float32)     # (nb,3,3), transposed
        T = (np.asarray(Pn) + shift).astype(np.float32)
        o = self._o
        er = self.er32
        for b, s0, e0 in self._act:
            seg = o[s0:e0]
            np.dot(er[s0:e0], Dt[b], out=seg)
            seg += T[b]
        o *= self.ew32
        ev, nv = self.ev, self.nv
        out = np.empty((nv, 3), np.float32)
        for c in range(3):
            out[:, c] = np.bincount(ev, weights=o[:, c], minlength=nv)
        self.vtx = np.ascontiguousarray(out @ self.Rg.T.astype(np.float32))
        sv = getattr(self, "_skirt_v", None)
        if sv is not None and len(sv):
            floor = np.float32(getattr(self, "rest_min", 0.0))
            self.vtx[sv, 2] = np.maximum(self.vtx[sv, 2], floor)

    # ---- public: death ----
    # One keyframe track per fall style. phi / roll / yaw tip the whole body about a point on the floor,
    # lean / twist / head bend the spine, la.. are (hip, knee, ankle) per leg and lh.. / rh.. are hand targets
    # in the chest's own frame (forward, left, up), so the arms are solved by IK whatever the rest pose is.
    _DK = ("phi", "roll", "yaw", "lean", "twist", "head", "la", "lk", "lp", "ra", "rk", "rp",
           "lhf", "lhl", "lhu", "rhf", "rhl", "rhu")
    _DK0 = dict(phi=0.0, roll=0.0, yaw=0.0, lean=0.0, twist=0.0, head=0.0,
                la=0.0, lk=0.03, lp=0.0, ra=0.0, rk=0.03, rp=0.0,
                lhf=21.0, lhl=1.0, lhu=-7.0, rhf=8.0, rhl=-3.0, rhu=-12.0)      # weapon hold
    _DSPEC = {
        # 0: shot in the chest - jolts back, knees give, sits down and falls on the back
        0: (-6.0, [
            (0.00, {}),
            (0.07, dict(lean=-0.30, head=-0.45, BL=(0.10, 0.32, 0.0), BH=(4, 18, 9))),
            (0.22, dict(lean=-0.10, head=-0.25, LL=(0.55, 1.0, 0.0), RL=(0.35, 0.8, 0.0), BH=(1, 20, 6))),
            (0.42, dict(phi=-0.35, lean=0.05, head=-0.10, LL=(0.85, 1.6, 0.0), RL=(0.70, 1.3, 0.0), BH=(-3, 19, 6))),
            (0.60, dict(phi=-1.00, lean=0.0, head=0.05, LL=(0.80, 1.4, 0.0), RL=(0.60, 1.2, 0.0), BH=(-4, 18, 8))),
            (0.74, dict(phi=-1.50, lean=-0.05, head=-0.35, LL=(0.50, 1.0, 0.0), RL=(0.30, 0.8, 0.0), BH=(0, 21, -1))),
            (0.88, dict(phi=-1.40, head=0.0, BH=(0, 21, 3))),
            (1.15, dict(phi=-1.50, head=0.10, twist=0.15, LL=(0.30, 0.60, 0.3), RL=(0.02, 0.12, 0.35), BH=(-2, 20, -3))),
        ]),
        # 1: shot in the back - arches, knees buckle, kneels, then folds forward onto the chest
        1: (5.0, [
            (0.00, {}),
            (0.08, dict(lean=-0.30, head=-0.40, BL=(0.05, 0.25, 0.0), BH=(-8, 15, 6))),
            (0.24, dict(phi=0.10, lean=0.15, head=0.0, LL=(0.70, 1.35, 0.0), RL=(0.45, 1.0, 0.0), BH=(3, 9, -14))),
            (0.46, dict(phi=0.30, lean=0.35, head=0.3, LL=(0.20, 2.2, 0.0), RL=(0.05, 2.0, 0.0), BH=(9, 8, -12))),
            (0.68, dict(phi=0.95, lean=0.30, head=0.1, LL=(0.05, 1.6, 0.0), RL=(-0.05, 1.2, 0.0), BH=(16, 8, -6))),
            (0.86, dict(phi=1.50, lean=0.10, head=-0.25, twist=0.25, LL=(-0.05, 0.5, 0.0), RL=(0.10, 0.2, 0.0), BH=(14, 12, 0))),
            (1.05, dict(phi=1.42, head=-0.10)),
            (1.40, dict(phi=1.50, head=-0.15, twist=0.3, LL=(-0.10, 0.15, 0.5), RL=(0.25, 0.85, 0.4), LH=(10, 17, -2), RH=(4, -15, -2))),
        ]),
        # 2: spins away from the hit and crumples onto one side (mirrored by the fall direction)
        2: (0.0, [
            (0.00, {}),
            (0.08, dict(twist=0.35, lean=-0.2, head=-0.3, yaw=0.25, BL=(0.10, 0.32, 0.0), LH=(2, 17, 7), RH=(-6, -14, 6))),
            (0.26, dict(yaw=0.55, roll=-0.15, lean=0.2, twist=0.2, LL=(0.65, 1.3, 0.0), RL=(0.35, 0.95, 0.0), LH=(6, 14, -4), RH=(8, -6, -8))),
            (0.48, dict(yaw=0.85, roll=0.55, phi=0.2, lean=0.25, LL=(0.80, 1.5, 0.0), RL=(0.5, 1.2, 0.0), LH=(6, 12, -8), RH=(4, -10, -14))),
            (0.68, dict(yaw=1.0, roll=1.40, phi=0.25, lean=0.2, LL=(0.9, 1.5, 0.0), RL=(0.5, 1.1, 0.0), LH=(12, 5, -8), RH=(6, -8, -10))),
            (0.82, dict(roll=1.50, head=-0.15)),
            (1.00, dict(roll=1.40, yaw=0.95)),
            (1.30, dict(roll=1.50, yaw=0.95, head=0.1, LL=(0.75, 1.35, 0.2), RL=(0.3, 0.9, 0.0), LH=(10, 12, -10), RH=(4, -6, -6))),
        ]),
        # 3: shot while crouching - tips over from the squat
        3: (3.0, [
            (0.00, dict(lean=0.30, BL=(0.95, 1.70, 0.0))),
            (0.08, dict(lean=0.05, head=-0.3, BH=(4, 16, 8))),
            (0.28, dict(phi=0.25, roll=0.2, lean=0.3, head=0.0, LL=(1.0, 1.9, 0.0), RL=(0.8, 1.6, 0.0), BH=(6, 10, -14))),
            (0.52, dict(phi=0.7, roll=0.8, yaw=0.3, lean=0.3, LL=(0.8, 1.6, 0.0), RL=(0.5, 1.2, 0.0), LH=(8, 12, -10), RH=(8, -8, -12))),
            (0.72, dict(phi=1.1, roll=1.35, yaw=0.5, LL=(0.6, 1.2, 0.0), RL=(0.3, 0.8, 0.0))),
            (1.00, dict(phi=1.15, roll=1.45, yaw=0.5, head=0.1, LL=(0.5, 1.0, 0.2), RL=(0.2, 0.6, 0.3), LH=(10, 12, -8), RH=(2, -8, -4))),
        ]),
        # 4: shot mid-slide - slides out flat on the back
        4: (-8.0, [
            (0.00, dict(lean=-0.50, LL=(1.30, 0.05, -0.25), RL=(0.55, 2.0, 0.2))),
            (0.10, dict(lean=-0.70, head=-0.4, BH=(4, 18, 9))),
            (0.35, dict(phi=-0.7, lean=-0.3, LL=(1.2, 0.2, 0.0), RL=(0.9, 1.3, 0.0), BH=(-2, 19, 5))),
            (0.65, dict(phi=-1.35, lean=-0.1, head=-0.2, LL=(0.35, 0.1, 0.2), RL=(0.15, 0.35, 0.3), BH=(0, 21, -2))),
            (0.80, dict(phi=-1.28, head=0.0)),
            (1.10, dict(phi=-1.35, head=0.1, twist=0.1, BH=(-2, 20, -3))),
        ]),
    }
    _DCACHE = {}

    @classmethod
    def _dead_track(cls, style, mirror):
        ck = (style, mirror)
        if ck in cls._DCACHE:
            return cls._DCACHE[ck]
        poff, spec = cls._DSPEC.get(style, cls._DSPEC[0])
        cur = dict(cls._DK0)
        T, V = [], []
        for t, kw in spec:
            kw = dict(kw)
            if "BL" in kw:
                kw["LL"] = kw["RL"] = kw.pop("BL")
            if "BH" in kw:
                f, l, u = kw.pop("BH")
                kw["LH"], kw["RH"] = (f, l, u), (f, -l, u)
            for nm, pre in (("LL", "l"), ("RL", "r")):
                if nm in kw:
                    kw[pre + "a"], kw[pre + "k"], kw[pre + "p"] = kw.pop(nm)
            for nm, pre in (("LH", "lh"), ("RH", "rh")):
                if nm in kw:
                    kw[pre + "f"], kw[pre + "l"], kw[pre + "u"] = kw.pop(nm)
            cur.update(kw)
            T.append(t)
            V.append([cur[k] for k in cls._DK])
        T, V = np.array(T, np.float64), np.array(V, np.float64)
        if mirror < 0:
            o = V.copy()
            o[:, 1], o[:, 2], o[:, 4] = -V[:, 1], -V[:, 2], -V[:, 4]
            o[:, 6:9], o[:, 9:12] = V[:, 9:12], V[:, 6:9]
            o[:, 12], o[:, 13], o[:, 14] = V[:, 15], -V[:, 16], V[:, 17]
            o[:, 15], o[:, 16], o[:, 17] = V[:, 12], -V[:, 13], V[:, 14]
            V = o
        cls._DCACHE[ck] = (T, V, poff, float(T[-1]))
        return cls._DCACHE[ck]

    @staticmethod
    def _kf_eval(T, V, t):
        """Cubic Hermite through the keys with finite-difference tangents: no stop-and-go between keys."""
        n = len(T)
        if t <= T[0]:
            return V[0]
        if t >= T[-1]:
            return V[-1]
        i = int(np.searchsorted(T, t, side="right")) - 1
        h = T[i + 1] - T[i]
        s = (t - T[i]) / h

        def tan(j):
            if j <= 0 or j >= n - 1:
                return np.zeros(V.shape[1])
            return (V[j + 1] - V[j - 1]) / (T[j + 1] - T[j - 1])
        m0, m1 = tan(i) * h, tan(i + 1) * h
        s2, s3 = s * s, s * s * s
        return (2 * s3 - 3 * s2 + 1) * V[i] + (s3 - 2 * s2 + s) * m0 + (-2 * s3 + 3 * s2) * V[i + 1] + (s3 - s2) * m1

    def animate_dead(self, t, ddir=1.0, style=0):
        """t = seconds since death, style 0..4 (back / forward / spin-to-the-side / from a crouch / from a slide),
        ddir = +-1 mirrors the side falls. Holds the last frame (the corpse)."""
        if not self.can:
            return
        mirror = 1.0 if ddir >= 0 else -1.0
        T, V, poff, dur = self._dead_track(int(style), mirror if style in (2, 3) else 1.0)
        tt = min(max(t, 0.0), dur)
        key = ("d", round(tt * 50), int(style), mirror)
        if key == self._key:
            return
        self._key = key
        v = self._kf_eval(T, V, tt)
        phi, roll, yaw, lean, twist, head = (float(x) for x in v[:6])
        legs = [tuple(float(x) for x in v[6:9]), tuple(float(x) for x in v[9:12])]
        # blend out of the last living pose so the fall starts from whatever the player was doing
        snap = getattr(self, "_snap", None)
        b = 1.0 - _sstep(0.0, 0.14, tt)
        if snap is not None and b > 0:
            legs = [self._mix(legs[i], snap[0][i], b) for i in (0, 1)]
            lean = lean + (snap[1] - lean) * b
        ix, Wp = self.ix, self.Wp
        t_, c_ = self._seg
        reach = max(t_ * math.cos(g[0]) + c_ * math.cos(g[0] - g[1]) for g in legs)
        drop = max(0.0, t_ + c_ - reach)                    # how far folded legs let the pelvis sink
        floor = min(Wp[ix[k]][2] for k in ("l_foot", "r_foot", "l_toe0", "r_toe0") if k in ix)
        piv = np.array([Wp[0][0], Wp[0][1], floor]) + self.F * poff
        Rr = self._RU(yaw) @ self._RF(roll) @ self._RL(phi)
        hl = (float(v[12]), float(v[13]), float(v[14]))
        hr = (float(v[15]), float(v[16]), float(v[17]))
        self._solve(legs, lean, twist, 0.0, 0.0, None, False, ground=False,
                    root=(Rr, piv, np.zeros(3), -self.U * drop),
                    extra={ix["head1"]: self._RL(head)}, ik=True, arm_local=(hl, hr, (0.0, 0.6, -1.0)))
        vt = self.vtx
        vt[:, 2] -= (vt[:, 2].min() - self.rest_min)


def make_vm(buf, pack=None, arms=None, css_hands=False):
    """View-model from a Source Spec (L4D2 layout).
    arms: optional SrcSpec of an arms model, bone-merged onto the weapon skeleton.
    css_hands is ignored (CS:S fallback hands were removed)."""
    if not isinstance(buf, SrcSpec):
        raise ValueError("Only Source engine view-models are supported — not CS 1.6 / GoldSrc")
    a = None
    if arms is not None:
        try:
            a = arms.load()
        except Exception as ex:
            print("Arms model failed:", ex)
    data = buf.load()
    if data.has_hands:
        a = None  # weapon already has hands built in
    if a is not None:
        try:
            return SrcInst(data, a)
        except Exception as ex:
            print("Arms do not fit this weapon, using it without arms:", ex)
    return SrcInst(data, None)


def extract_rar(path, out):
    """.rar has no pure-Python reader, so use whatever extractor the PC has: 7-Zip, unrar / WinRAR,
    unar or tar (Windows 10+ ships a tar that reads .rar)."""
    import shutil
    import subprocess
    path, out = str(path), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cmds = []
    for exe in ("7z", "7zz", "7za"):
        w = shutil.which(exe)
        if w:
            cmds.append([w, "x", "-y", "-o" + str(out), path])
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        w = os.path.join(base, "7-Zip", "7z.exe")
        if os.path.exists(w):
            cmds.append([w, "x", "-y", "-o" + str(out), path])
        for nm in ("UnRAR.exe", "WinRAR.exe"):
            w = os.path.join(base, "WinRAR", nm)
            if os.path.exists(w):
                cmds.append([w, "x", "-y", path, str(out) + os.sep])
    w = shutil.which("unrar")
    if w:
        cmds.append([w, "x", "-y", path, str(out) + os.sep])
    w = shutil.which("unar")
    if w:
        cmds.append([w, "-o", str(out), "-f", path])
    for exe in ("bsdtar", "tar"):
        w = shutil.which(exe)
        if w:
            cmds.append([w, "-xf", path, "-C", str(out)])
    def good():
        files = [f for f in out.rglob("*") if f.is_file() and f.suffix.lower() in (".mdl", ".vvd", ".vtx", ".vtf", ".vmt")]
        return any(f.suffix.lower() == ".mdl" for f in files) and all(f.stat().st_size > 0 for f in files)

    def fresh():
        shutil.rmtree(out, ignore_errors=True)       # old 7-Zip builds leave empty files for RAR5: start clean
        out.mkdir(parents=True, exist_ok=True)
    for cmd in cmds:
        fresh()
        try:
            subprocess.run(cmd, capture_output=True, timeout=900)
        except Exception:
            continue
        if good():
            return
    try:                                              # python-libarchive-c (needs the libarchive library)
        import libarchive
        fresh()
        with libarchive.file_reader(path) as arc:
            for ent in arc:
                if ent.isdir:
                    continue
                dst = out / Path(ent.pathname.replace("\\", "/"))
                dst.parent.mkdir(parents=True, exist_ok=True)
                with open(dst, "wb") as fh:
                    for blk in ent.get_blocks():
                        fh.write(blk)
        if good():
            return
    except Exception:
        pass
    raise RuntimeError("cannot unpack .rar - install 7-Zip (or WinRAR), or unpack it yourself and drop the folder / .zip in")


def is_src_player(rel):
    """True for a full-body Source character model usable as CT/T.

    Paths recognised:
      models/survivors/...              (L4D / L4D2 survivors)
      models/infected/...               (L4D / L4D2 specials & commons)
      models/player/..., models/humans/... (other Source characters in user mods)
    Skips arms/hands, view-models, world models, ragdoll helpers, and anim-only stubs.
    """
    rel = rel.replace("\\", "/").lower()
    nm = rel.rsplit("/", 1)[-1]
    if not nm.endswith(".mdl"):
        return False
    # view / world / arms models are never third-person players
    if re.match(r"^[vwp]_", nm) or re.search(r"(^|_)(arms?|hands?|sleeves?|gloves?|ragdoll|_anim|physics|gib)(_|\.|$)", nm):
        return False
    if re.search(r"/(arms?|hands?|sleeves?|gloves?|weapons?|v_models|w_models)/", rel):
        return False
    # Known character folders
    if re.search(r"(^|/)(player|survivors?|infected|humans?|npc|characters?)/", rel):
        return True
    # Filename hints used by many L4D2 workshop packs
    if re.search(r"(survivor|infected|hunter|smoker|boomer|tank|spitter|jockey|charger|witch|common_)", nm):
        return True
    return False


def src_players(arch):
    """Character models (L4D2 survivor/infected, other Source characters) inside a Source archive."""
    out = []
    gpm = set()
    if getattr(arch, "is_gma", False):
        try:
            gpm = set(arch.player_model_paths())   # player models the Garry's Mod addon registers in its lua
        except Exception:
            gpm = set()
    for nm in arch.files:
        if not (is_src_player(nm) or nm.replace("\\", "/").lower() in gpm):
            continue
        # Arms / hands / gloves are never player models, even when the addon registers them with
        # player_manager.AddValidModel (c_hands style addons do that): keep them out of the player list.
        if _is_arms_mdl(nm):
            continue
        # Skip truncated VPK entries
        if hasattr(arch, "complete") and not arch.complete(nm):
            continue
        low = nm.replace("\\", "/").lower()
        base = low[:-4]
        if hasattr(arch, "has"):
            has_vvd = arch.has(base + ".vvd")
            has_vtx = any(arch.has(base + e) for e in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx"))
            if not (has_vvd and has_vtx):
                continue
            if hasattr(arch, "complete"):
                if not arch.complete(base + ".vvd"):
                    continue
                if not any(arch.complete(base + e) for e in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx") if arch.has(base + e)):
                    continue
        out.append(nm)
    return sorted(out)


def src_scan(arch):
    """Weapon view-models inside a Source archive.

    Recognises:
      models/v_models/v_*.mdl         (L4D / L4D2)
      models/v_models/weapons/v_*.mdl
      any other path ending in v_*.mdl that is not arms/hands/world/display
    World models (w_*) and incomplete VPK entries are skipped.
    """
    out = []
    gma = getattr(arch, "is_gma", False)
    gvm = set()
    if gma:
        try:
            gvm = set(arch.viewmodel_paths())      # SWEP.ViewModel = "..." in lua/weapons/*.lua
        except Exception:
            gvm = set()
    for nm in arch.files:
        low = nm.replace("\\", "/").lower()
        if not low.endswith(".mdl"):
            continue
        b = low.rsplit("/", 1)[-1]
        # L4D2 world models live under w_models/ — never use them as viewmodels
        if "/w_models/" in low or re.match(r"^w_", b) or re.match(r"^p_", b):
            continue
        # decompile / source-asset copies (not loadable game models)
        if re.search(r"(^|/)(source files|decompiled[^/]*|sources?)/", low):
            continue
        if re.search(r"display|dropped|arms|sleeve|glove|hands|_zoom|_scope|ragdoll", low):
            continue
        # view-model naming: v_*.mdl (any folder); Garry's Mod also uses c_*.mdl (hands come from the arms model)
        # and whatever the addon's weapon script names as SWEP.ViewModel
        if not (re.match(r"^v_[^/]*\.mdl$", b) or (gma and (re.match(r"^(c|vm)_[^/]*\.mdl$", b) or low in gvm))):
            continue
        # Skip truncated downloads so Skins only lists usable models
        if hasattr(arch, "complete") and not arch.complete(nm):
            continue
        # Prefer entries that still have their .vvd / .vtx on disk
        base = low[:-4]
        has_mesh = True
        if hasattr(arch, "has"):
            has_vvd = arch.has(base + ".vvd")
            has_vtx = any(arch.has(base + e) for e in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx"))
            has_mesh = has_vvd and has_vtx
            if has_mesh and hasattr(arch, "complete"):
                if not arch.complete(base + ".vvd"):
                    has_mesh = False
                if has_mesh and not any(arch.complete(base + e) for e in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx") if arch.has(base + e)):
                    has_mesh = False
        if not has_mesh:
            continue
        out.append(nm)
    return sorted(out)

