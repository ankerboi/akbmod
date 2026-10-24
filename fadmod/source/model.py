"""Source studio model (MDL + VVD + VTX) loader and animation decoder."""
import math
import struct
import numpy as np

from .vtf import *  # not strictly needed but parse_kv lives there

def _quat_mat3(q):
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _angle_quat(ax, ay, az):
    sy, cy = math.sin(az * .5), math.cos(az * .5)
    sp, cp = math.sin(ay * .5), math.cos(ay * .5)
    sr, cr = math.sin(ax * .5), math.cos(ax * .5)
    return (sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy, cr * cp * cy + sr * sp * sy)


HIT_HALF_W = 12.0     # hitbox half-width (was 19); smaller = narrower hitbox
HIT_HALF_H = 29.0     # hitbox half-height standing (was 35)
HEAD_FRAC = 0.20       # the top 20% of the model's height is the head (own hitbox, placed on the model's real head)
HEAD_MIN_R, HEAD_MAX_R = 4.5, 9.0   # head box half-width limits (units, before PLAYER_SCALE)
HIT_HALF_H_LOW = 17.0 # half-height while sliding / crouching (was 21)
VM_EVENT_GAIN = 0.55          # volume of the weapon animation-event sounds (reload / pull-out / bolt ...)
SHOTGUN_RELOAD_SCALE = 0.75   # shotgun reload time as a fraction of the AWP reload (1.0 = same as AWP, lower = faster)
PLAYER_SCALE = 0.85   # visual + hitbox size of other players (1.0 = original); lower = smaller


class SrcModel:
    """A Source studio model (MDL v44-49 + VVD + VTX): bodygroup 0 geometry, bones, sequences."""

    def __init__(self, mdl, vvd, vtx):
        b = self.b = mdl
        if b[:4] != b"IDST":
            raise ValueError("not a Source MDL")
        self.version = struct.unpack_from("<i", b, 4)[0]
        if not 44 <= self.version <= 49:
            raise ValueError("unsupported Source MDL version %d" % self.version)
        I = lambda o: struct.unpack_from("<i", b, o)[0]
        F = lambda o: struct.unpack_from("<f", b, o)[0]
        S = lambda o: struct.unpack_from("<h", b, o)[0]
        cs = lambda o: b[o:b.index(b"\0", o)].decode("latin1")
        self.name = b[12:76].split(b"\0")[0].decode("latin1")
        self._parse_bones()
        # ---- textures / skins
        self.tex_names = [cs(I(208) + i * 64 + I(I(208) + i * 64)) for i in range(I(204))]
        self.cd = [cs(I(I(216) + i * 4)).replace("\\", "/").lower() for i in range(I(212))]
        nref, nfam, si = I(220), I(224), I(228)
        self.skin = [S(si + j * 2) for j in range(nref)] if nfam else list(range(nref))
        # ---- vertices (VVD)
        if vvd[:4] != b"IDSV":
            raise ValueError("bad VVD")
        nfix = struct.unpack_from("<i", vvd, 48)[0]
        fixo = struct.unpack_from("<i", vvd, 52)[0]
        vdo = struct.unpack_from("<i", vvd, 56)[0]
        tdo = struct.unpack_from("<i", vvd, 60)[0]
        nlod0 = struct.unpack_from("<i", vvd, 16)[0]
        raw_n = (tdo - vdo) // 48 if tdo > vdo else (len(vvd) - vdo) // 48
        dt = np.dtype([("w", "<f4", 3), ("bi", "u1", 3), ("nbw", "u1"), ("p", "<f4", 3),
                       ("n", "<f4", 3), ("uv", "<f4", 2)])
        raw = np.frombuffer(vvd, dt, raw_n, vdo)
        if nfix:
            fx = np.frombuffer(vvd, "<i4", nfix * 3, fixo).reshape(nfix, 3)
            raw = np.concatenate([raw[s:s + n] for lod, s, n in fx])
        V = raw[:max(nlod0, 1)] if not nfix else raw
        self.v_pos = np.ascontiguousarray(V["p"], np.float64)
        self.v_uv = np.ascontiguousarray(V["uv"], np.float32)
        self.v_w = np.ascontiguousarray(V["w"], np.float32)
        self.v_bi = np.ascontiguousarray(V["bi"], np.int64)
        self.v_nb = np.ascontiguousarray(V["nbw"], np.int64)
        # ---- meshes (VTX strips via _build_parts)
        nbody, bpo = I(232), I(236)
        self.parts = self._build_parts(vtx, nbody, bpo)
        self._pt = (vtx, nbody, bpo)
        self._parse_seqs()
        self.includes = []

    def _parse_bones(self):
        b = self.b
        I = lambda o: struct.unpack_from("<i", b, o)[0]
        F = lambda o: struct.unpack_from("<f", b, o)[0]
        S = lambda o: struct.unpack_from("<h", b, o)[0]
        cs = lambda o: b[o:b.index(b"\0", o)].decode("latin1")
        nb, bo = I(156), I(160)
        self.bones = []
        for i in range(nb):
            p = bo + i * 216
            self.bones.append({
                "name": cs(p + I(p)), "par": I(p + 4),
                "pos": np.array(struct.unpack_from("<3f", b, p + 32)),
                "quat": struct.unpack_from("<4f", b, p + 44),
                "rot": struct.unpack_from("<3f", b, p + 60),
                "ps": struct.unpack_from("<3f", b, p + 72),
                "rs": struct.unpack_from("<3f", b, p + 84),
                "p2b": np.vstack([np.array(struct.unpack_from("<12f", b, p + 96)).reshape(3, 4), [0, 0, 0, 1]]),
            })
        self.nb = nb

    def _parse_seqs(self):
        b = self.b
        I = lambda o: struct.unpack_from("<i", b, o)[0]
        F = lambda o: struct.unpack_from("<f", b, o)[0]
        S = lambda o: struct.unpack_from("<h", b, o)[0]
        cs = lambda o: b[o:b.index(b"\0", o)].decode("latin1")
        self.sounds_ev = {}
        na, ao = I(180), I(184)
        self.anims = []
        for i in range(na):
            p = ao + i * 100
            self.anims.append({"name": cs(p + I(p + 4)), "fps": F(p + 8) or 30.0, "flags": I(p + 12),
                               "n": max(1, I(p + 16)), "p": p, "block": I(p + 52), "idx": I(p + 56),
                               "sect": I(p + 80), "sfr": I(p + 84)})
        ns, so = I(188), I(192)
        self.seqs = []
        for i in range(ns):
            p = so + i * 212
            nbl = I(p + 56)
            ai = I(p + 60)
            descs = [S(p + ai + k * 2) for k in range(max(1, nbl))]
            d = self.anims[descs[0]] if 0 <= descs[0] < na else None
            evs = []
            for e in range(I(p + 24)):
                q = p + I(p + 28) + e * 80
                ev_id, ev_type = I(q + 4), I(q + 8)
                # old style events carry a number (5004 / 5005 = play sound); new style events (type bit 0x400,
                # what GMod / Source 2013 models usually have) carry the event NAME at szeventindex instead
                ev_name = ""
                if ev_type & 0x400:
                    try:
                        so_ = I(q + 76)
                        ev_name = cs(q + so_) if so_ else ""
                    except Exception:
                        ev_name = ""
                if ev_id in (5004, 5005) or ev_name.upper().startswith(("AE_CL_PLAYSOUND", "AE_CL_PLAY_SOUND", "CL_EVENT_SOUND")):
                    opt = b[q + 12:q + 76].split(b"\0")[0].decode("latin1").strip()
                    if opt:
                        evs.append((F(q), opt))
            try:
                _ao = I(p + 8)
                _act = cs(p + _ao) if _ao else ""
            except Exception:
                _act = ""
            self.seqs.append({"label": cs(p + I(p + 4)), "flags": I(p + 12), "act": _act,
                              "fps": d["fps"] if d else 30.0, "n": d["n"] if d else 1,
                              "grp": 0, "anim": 1 if d else -1, "desc": d, "events": sorted(evs),
                              "loop": bool(I(p + 12) & 1), "descs": descs})
        # External .ani anim blocks (HL2 / L4D2 shared anims: male_shared.ani, …)
        self.anim_blocks = [(0, 0)]
        self.ani_data = None  # bytes of the companion .ani when present
        try:
            nblk, blk_ix = I(352), I(356)
            if 0 < nblk < 512 and 0 < blk_ix < len(b) - 8:
                self.anim_blocks = [struct.unpack_from("<ii", b, blk_ix + i * 8) for i in range(nblk)]
        except Exception:
            pass
        self._cache = {}

    def attach_includes(self, vpk, mdl_name, _depth=0):
        """L4D2 view-models keep their animations in another .mdl ($includemodel, e.g.
        v_knife_t.mdl -> v_knife_anim.mdl). Add those sequences to this model."""
        b = self.b
        try:
            ni, io = struct.unpack_from("<ii", b, 336)
        except Exception:
            return
        if not 0 < ni < 64 or not 0 < io < len(b):
            return
        folder = mdl_name.replace("\\", "/").rsplit("/", 1)[0] if "/" in mdl_name else ""
        for k in range(ni):
            try:
                p = io + k * 8
                lo = struct.unpack_from("<i", b, p + 4)[0]
                nm = b[p + lo:b.index(b"\0", p + lo)].decode("latin1").replace("\\", "/").lower()
            except Exception:
                continue
            cands = [nm, folder.lower() + "/" + nm.rsplit("/", 1)[-1]]
            hit = next((c for c in cands if vpk.has(c)), None)
            if not hit or hit.lower() == mdl_name.lower():
                continue
            try:
                am = SrcModel.__new__(SrcModel)
                am.b = vpk.read(hit)
                if am.b[:4] != b"IDST":
                    continue
                am.version = struct.unpack_from("<i", am.b, 4)[0]
                am._parse_bones()
                am._parse_seqs()
                am._cache = {}
                if _depth < 3:
                    am.attach_includes(vpk, hit, _depth + 1)
            except Exception as ex:
                print("include model failed:", nm, ex)
                continue
            names = {bn["name"].lower(): i for i, bn in enumerate(am.bones)}
            remap = [names.get(bn["name"].lower(), -1) for bn in self.bones]
            have = {sq["label"].lower() for sq in self.seqs}
            for sq in am.seqs:
                d = sq.get("desc")
                if d is None or sq["label"].lower() in have:
                    continue
                if "src" in d:
                    continue
                d["raw"] = dict(d)
                d["src"], d["remap"] = am, remap
                sq2 = dict(sq)
                self.seqs.append(sq2)
            self.includes.append(am)

    def body_info(self):
        """[(bodygroup name, number of choices)] - choice 0 is what the game shows by default."""
        b = self.b
        _vtx, nbody, bpo = self._pt
        out = []
        for bi in range(nbody):
            p = bpo + bi * 16
            sn, nmod = struct.unpack_from("<ii", b, p)
            try:
                nm = b[p + sn:b.index(b"\0", p + sn)].decode("latin1")
            except Exception:
                nm = ""
            out.append((nm.lower(), nmod))
        return out

    def parts_for(self, sel):
        """Mesh parts with other bodygroup choices: sel = {bodygroup name: choice} (clamped to the last choice)."""
        vtx, nbody, bpo = self._pt
        by = {}
        for bi, (nm, nmod) in enumerate(self.body_info()):
            if nm in sel and nmod > 0:
                c = int(sel[nm])
                if c < 0:      # "hide it": the choice of this bodygroup that draws the fewest triangles
                    sizes = [sum(len(x["tri"]) for x in self._build_parts(vtx, nbody, bpo, {**by, bi: k}))
                             for k in range(nmod)]
                    c = sizes.index(min(sizes))
                by[bi] = max(0, min(c, nmod - 1))
        return self._build_parts(vtx, nbody, bpo, by)

    def _build_parts(self, vtx, nbody, bpo, sel=None):
        b = self.b
        I = lambda o: struct.unpack_from("<i", b, o)[0]
        sv = struct.unpack_from
        vh_bpo = sv("<i", vtx, 32)[0]
        parts = {}
        for bi in range(nbody):
            p = bpo + bi * 16
            nmod, mi = I(p + 4), I(p + 12)
            if nmod <= 0:
                continue
            km = (sel or {}).get(bi, 0)
            if not 0 <= km < nmod:
                km = 0
            mp = p + mi + km * 148            # mstudiomodel_t is 148 bytes
            nmesh, mo, nvert, vidx = I(mp + 72), I(mp + 76), I(mp + 80), I(mp + 84)
            if nmesh <= 0 or nvert <= 0:
                continue
            vstart = vidx // 48
            vbp = vh_bpo + bi * 8
            vnm, vmo = sv("<ii", vtx, vbp)
            vm = vbp + vmo + km * 8           # one 8-byte VTX model header per choice
            vnl, vlo = sv("<ii", vtx, vm)
            vl = vm + vlo
            vnmesh, vmeo = sv("<ii", vtx, vl)
            for k in range(min(nmesh, vnmesh)):
                q = mp + mo + k * 116
                mat, voff = I(q), I(q + 12)
                vme = vl + vmeo + k * 9
                nsg, sgo = sv("<ii", vtx, vme)
                tris = []
                for g in range(nsg):
                    sg = vme + sgo + g * 25
                    nv_, vo_, ni_, io_, ns_, so_ = sv("<6i", vtx, sg)
                    vt = np.frombuffer(vtx, np.dtype([("bwi", "u1", 3), ("nb", "u1"), ("orig", "<u2"),
                                                       ("bid", "i1", 3)]), nv_, sg + vo_)
                    ix = np.frombuffer(vtx, "<u2", ni_, sg + io_).astype(np.int64)
                    glob = vt["orig"].astype(np.int64) + voff + vstart
                    for s in range(ns_):
                        st = sg + so_ + s * 27
                        sni, sio, _snv, _svo, _snb, sfl = sv("<iiiihB", vtx, st)
                        sidx = ix[sio:sio + sni]
                        if sfl & 2:
                            for t in range(len(sidx) - 2):
                                a, b2, c = sidx[t:t + 3]
                                tris.append(glob[[a, c, b2]] if t & 1 else glob[[a, b2, c]])
                        else:
                            n3 = len(sidx) // 3 * 3
                            if n3:
                                tris.append(glob[sidx[:n3]].reshape(-1, 3))
                if tris:
                    t = np.concatenate([np.asarray(x).reshape(-1, 3) for x in tris])
                    key = (bi, k)
                    parts[key] = {"mat": mat, "tri": t.reshape(-1)}
        return [parts[k] for k in sorted(parts)]

    # ---- material helper: skin table + cdtextures -> list of candidate material paths
    def material_paths(self, mat):
        ti = self.skin[mat] if 0 <= mat < len(self.skin) else mat
        nm = self.tex_names[ti] if 0 <= ti < len(self.tex_names) else ""
        return [(cd.lstrip("/") + nm).lower() for cd in (self.cd or [""])] + [nm.lower()]

    # ---- animation ---------------------------------------------------------------------------
    @staticmethod
    def _rle(buf, base, k, scale):
        """mstudioanimvalue_t run-length decode at integer frame k -> value*scale."""
        p = base
        g = 0
        while True:
            valid, total = buf[p], buf[p + 1]
            if total == 0 or g > 4096:
                return 0.0
            if total > k:
                break
            k -= total
            p += (valid + 1) * 2
            g += 1
        if valid > k:
            return struct.unpack_from("<h", buf, p + 2 + k * 2)[0] * scale
        return struct.unpack_from("<h", buf, p + valid * 2)[0] * scale

    def _anim_base(self, d, frame):
        """Return (buffer, base_offset, frame_within_section) for an anim desc."""
        block, idx = d["block"], d["idx"]
        fr = frame
        # Sectioned anims: pick the section that holds this frame
        if d["sect"] and d["sfr"]:
            n, sfr = d["n"], d["sfr"]
            if n > sfr and frame == n - 1:
                si, fr = n // sfr + 1, 0
            else:
                si = frame // sfr
                fr = frame - si * sfr
            sp = d["p"] + d["sect"] + si * 8
            block, idx = struct.unpack_from("<ii", self.b, sp)
        if block == 0:
            if idx == 0:
                return None, 0, fr
            return self.b, d["p"] + idx, fr
        # External .ani block
        if not self.ani_data or block < 0 or block >= len(self.anim_blocks):
            return None, 0, fr
        ds, de = self.anim_blocks[block]
        off = ds + idx
        if off < 0 or off >= len(self.ani_data):
            return None, 0, fr
        return self.ani_data, off, fr

    @staticmethod
    def _quat48(buf, o):
        x, y, z = struct.unpack_from("<HHH", buf, o)
        wneg = z >> 15
        qx = (x - 32768) / 32768.0
        qy = (y - 32768) / 32768.0
        qz = ((z & 0x7FFF) - 16384) / 16384.0
        qw = math.sqrt(max(0.0, 1 - qx * qx - qy * qy - qz * qz))
        return (qx, qy, qz, -qw if wneg else qw)

    def _decode_frameanim(self, buf, base, frame, pos, quat, delta):
        """STUDIO_FRAMEANIM (0x40): constants + per-frame bone packed data."""
        nb = self.nb
        if base + 24 > len(buf):
            return pos, quat
        c_off, f_off, f_len = struct.unpack_from("<iii", buf, base)
        bone_flags = buf[base + 24: base + 24 + nb]
        # constants stream
        cp = base + c_off if c_off else None
        # frame stream
        fp = base + f_off + frame * f_len if f_off else None
        if fp is not None and (fp < 0 or fp + f_len > len(buf)):
            return pos, quat
        for bone in range(min(nb, len(bone_flags))):
            fl = bone_flags[bone]
            B = self.bones[bone]
            if fl == 0:
                continue
            # constants first
            if cp is not None:
                if fl & 0x20:  # CONST_POS2: float32 x3
                    pos[bone] = struct.unpack_from("<3f", buf, cp)
                    cp += 12
                elif fl & 0x01:  # CONST_POS: Vector48
                    pos[bone] = np.frombuffer(buf, "<f2", 3, cp).astype(np.float64)
                    cp += 6
                if fl & 0x40:  # CONST_ROT2
                    quat[bone] = self._quat48(buf, cp)
                    cp += 6
                elif fl & 0x02:  # CONST_ROT
                    quat[bone] = self._quat48(buf, cp)
                    cp += 6
            # per-frame data
            if fp is not None:
                if fl & 0x10:  # ANIM_POS2: float32 x3
                    pos[bone] = struct.unpack_from("<3f", buf, fp)
                    fp += 12
                elif fl & 0x04:  # ANIM_POS: Vector48
                    pos[bone] = np.frombuffer(buf, "<f2", 3, fp).astype(np.float64)
                    fp += 6
                if fl & 0x80:  # ANIM_ROT2
                    quat[bone] = self._quat48(buf, fp)
                    fp += 6
                elif fl & 0x08:  # ANIM_ROT
                    quat[bone] = self._quat48(buf, fp)
                    fp += 6
            if delta and not (fl & (0x01 | 0x04 | 0x10 | 0x20)):
                pass  # keep zero pos
            elif not delta and not (fl & (0x01 | 0x04 | 0x10 | 0x20)):
                pos[bone] = B["pos"]
        return pos, quat

    def decode_frame(self, d, frame):
        """Local pose of every bone for integer frame of anim desc d: (pos[nb,3], quat[nb,4])."""
        key = (id(d), frame)
        c = self._cache.get(key)
        if c is not None:
            return c
        if d.get("src") is not None:      # animation lives in an included .mdl: decode there, map bones by name
            sm = d["src"]
            p2, q2 = sm.decode_frame(d["raw"], frame)
            pos = np.array([bn["pos"] for bn in self.bones])
            quat = np.array([bn["quat"] for bn in self.bones], np.float64)
            if d["flags"] & 0x4:
                pos[:] = 0
                quat[:] = (0, 0, 0, 1)
            for i, j in enumerate(d["remap"]):
                if j >= 0:
                    pos[i], quat[i] = p2[j], q2[j]
            self._cache[key] = (pos, quat)
            return pos, quat
        b = self.b
        nb = self.nb
        pos = np.array([bn["pos"] for bn in self.bones])
        quat = np.array([bn["quat"] for bn in self.bones], np.float64)
        delta = bool(d["flags"] & 0x4)
        if delta:
            pos[:] = 0
            quat[:] = (0, 0, 0, 1)
        buf, base, fr = self._anim_base(d, frame)
        if buf is None:
            self._cache[key] = (pos, quat)
            return pos, quat
        # FRAMEANIM (flag 0x40): newer by-frame encoding used by HL2/L4D2 shared anims
        if d["flags"] & 0x40:
            pos, quat = self._decode_frameanim(buf, base, fr, pos, quat, delta)
            self._cache[key] = (pos, quat)
            return pos, quat
        # Classic RLE bone stream
        p = base
        guard = 0
        while guard < 4096:
            guard += 1
            if p + 4 > len(buf):
                break
            bone, flags, nxt = buf[p], buf[p + 1], struct.unpack_from("<h", buf, p + 2)[0]
            if bone < nb:
                B = self.bones[bone]
                q0 = p + 4
                if flags & 0x2:        # RAWROT: Quaternion48
                    quat[bone] = self._quat48(buf, q0)
                    q0 += 6
                elif flags & 0x20:     # RAWROT2: Quaternion64
                    v = struct.unpack_from("<Q", buf, q0)[0]
                    qx = ((v & 0x1FFFFF) - 1048576) / 1048576.5
                    qy = (((v >> 21) & 0x1FFFFF) - 1048576) / 1048576.5
                    qz = (((v >> 42) & 0x1FFFFF) - 1048576) / 1048576.5
                    qw = math.sqrt(max(0.0, 1 - qx * qx - qy * qy - qz * qz))
                    quat[bone] = (qx, qy, qz, -qw if (v >> 63) else qw)
                    q0 += 8
                rot_ptr = None
                if flags & 0x8:
                    rot_ptr = q0
                    q0 += 6
                pos_ptr = None
                if flags & 0x4:
                    pos_ptr = q0
                    q0 += 6
                if rot_ptr is not None:
                    offs = struct.unpack_from("<3h", buf, rot_ptr)
                    ang = [0.0, 0.0, 0.0]
                    for j in range(3):
                        if offs[j]:
                            ang[j] = self._rle(buf, rot_ptr + offs[j], fr, B["rs"][j])
                        if not delta:
                            ang[j] += B["rot"][j]
                    quat[bone] = _angle_quat(*ang)
                if flags & 0x1:        # RAWPOS: Vector48
                    pos[bone] = np.frombuffer(buf, "<f2", 3, q0).astype(np.float64)
                elif pos_ptr is not None:
                    offs = struct.unpack_from("<3h", buf, pos_ptr)
                    for j in range(3):
                        v = self._rle(buf, pos_ptr + offs[j], fr, B["ps"][j]) if offs[j] else 0.0
                        pos[bone][j] = v + (0.0 if delta else B["pos"][j])
            if nxt == 0:
                break
            p += nxt
        self._cache[key] = (pos, quat)
        if len(self._cache) > 12000:
            # drop oldest half rather than wiping everything mid-shoot (avoids decode spikes)
            for k in list(self._cache.keys())[:6000]:
                del self._cache[k]
        return pos, quat

def _find_vpk_model(vpk, mdl_name):
    base = mdl_name[:-4]
    vtx = None
    for ext in (".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx"):
        if vpk.has(base + ext):
            vtx = base + ext
            break
    if not vtx or not vpk.has(base + ".vvd"):
        raise ValueError("missing .vvd/.vtx for " + mdl_name)
    return vpk.read(mdl_name), vpk.read(base + ".vvd"), vpk.read(vtx)
