"""Garry's Mod addon (.gma) reader.

A .gma is a plain archive ("GMAD" header, a file table, then every file's bytes one after the other).
This class has the same small interface as VpkArchive (files / has / size / read / complete), so the
Source loaders treat a .gma exactly like a .vpk: weapon view-models, player models, arms, sounds and
materials are read straight out of the addon, lazily, without unpacking anything.

Workshop downloads from Steam are LZMA-compressed .gma files; those are unpacked once into the temp folder.
"""
import hashlib
import lzma
import os
import re
import struct
import tempfile
import weakref
from pathlib import Path

GMA_MAGIC = b"GMAD"

# Every addon that has been opened. Garry's Mod weapon packs split their content over several addons (the Alpha 50
# scope model is in the sniper addon, its textures in the base addon), so a texture that is not inside the addon
# being loaded is looked up in the others (see loaders._find_any).
OPEN_ARCHIVES = weakref.WeakSet()


def is_gma_file(path):
    """True for a .gma, or for any file (e.g. a Workshop legacy .bin) that holds a .gma, plain or LZMA-compressed.
    Decided by the file's first bytes, never by its name."""
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return False
    if head[:4] == GMA_MAGIC:
        return True
    if len(head) < 14 or head[0] >= 225:                # LZMA 'alone' header: props byte, dict size, length
        return False
    try:
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(head, max_length=4) == GMA_MAGIC
    except (lzma.LZMAError, EOFError):
        return False


def is_gma_path(path):
    p = Path(path)
    sfx = p.suffix.lower()
    return sfx == ".gma" or (sfx == ".bin" and is_gma_file(p))


def _cstr(buf, p):
    e = buf.index(b"\0", p)
    return buf[p:e].decode("utf-8", "replace"), e + 1


def _decompress_to_temp(path):
    """Steam Workshop .gma files are LZMA ('alone' format). Unpack once, return the temp file path."""
    st = Path(path).stat()
    key = hashlib.md5(("%s|%d|%d" % (Path(path).resolve(), st.st_size, int(st.st_mtime))).encode()).hexdigest()[:16]
    out = Path(tempfile.gettempdir()) / "akb_arena_src" / ("gma_" + key + ".gma")
    if out.exists() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "rb") as f:
        raw = f.read()
    try:
        try:
            data = lzma.decompress(raw, format=lzma.FORMAT_ALONE)
        except lzma.LZMAError:
            # some tools write a stream with a bogus length field: decode as far as it goes
            data = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(raw)
    except lzma.LZMAError:
        raise ValueError("not a Garry's Mod addon (.gma)")
    if data[:4] != GMA_MAGIC:
        raise ValueError("not a Garry's Mod addon (.gma)")
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return out


class GmaArchive:
    """Reads a Garry's Mod .gma. files maps lower-case 'models/weapons/v_x.mdl' -> (offset, size)."""

    is_gma = True
    incomplete = False

    def __init__(self, path):
        self.src_path = str(path)
        with open(self.src_path, "rb") as f:
            magic = f.read(4)
        real = Path(path)
        if magic != GMA_MAGIC:
            real = _decompress_to_temp(path)         # raises ValueError if it is not an addon at all
        self.path = str(real)
        self.files = {}
        self.title = self.description = self.author = ""
        self.version = 0
        self.required = []
        self._fh = None
        self._parse()
        OPEN_ARCHIVES.add(self)

    # ---- header + file table ----
    def _parse(self):
        total = Path(self.path).stat().st_size
        want = min(total, 4 << 20)
        while True:
            with open(self.path, "rb") as f:
                b = f.read(want)
            try:
                data_start = self._parse_table(b)
                break
            except (IndexError, ValueError, struct.error):
                if want >= total:
                    raise ValueError("damaged .gma file (file table is cut off)")
                want = min(total, want * 4)
        off = data_start
        for nm, size in self._entries:
            self.files[nm] = (off, size)
            off += size
        del self._entries

    def _parse_table(self, b):
        if b[:4] != GMA_MAGIC:
            raise ValueError("not a Garry's Mod addon (.gma)")
        ver = b[4]
        if ver > 3:
            raise ValueError("unsupported .gma version %d" % ver)
        p = 5 + 8 + 8                                   # version byte, steamid, timestamp
        self.required = []
        if ver > 1:
            while True:
                s, p = _cstr(b, p)
                if not s:
                    break
                self.required.append(s)
        self.title, p = _cstr(b, p)
        self.description, p = _cstr(b, p)
        self.author, p = _cstr(b, p)
        self.version = struct.unpack_from("<i", b, p)[0]
        p += 4
        entries = []
        while True:
            num = struct.unpack_from("<I", b, p)[0]
            p += 4
            if num == 0:
                break
            nm, p = _cstr(b, p)
            size = struct.unpack_from("<q", b, p)[0]
            p += 12                                     # i64 size + u32 crc
            if size < 0:
                raise ValueError("damaged .gma file")
            entries.append((nm.replace("\\", "/").lower().lstrip("/"), size))
        self._entries = entries
        return p

    # ---- VpkArchive-compatible interface ----
    def _k(self, name):
        return name.lower().replace("\\", "/")

    def has(self, name):
        return self._k(name) in self.files

    def size(self, name):
        return self.files[self._k(name)][1]

    def complete(self, name):
        off, ln = self.files[self._k(name)]
        return off + ln <= self._disk_size()

    def _disk_size(self):
        return Path(self.path).stat().st_size

    def read(self, name, start=0, length=None):
        off, ln = self.files[self._k(name)]
        if length is None:
            length = ln - start
        length = max(0, min(length, ln - start))
        if self._fh is None:
            self._fh = open(self.path, "rb")
        self._fh.seek(off + start)
        out = self._fh.read(length)
        if len(out) < length:
            raise ValueError("%s is cut off inside the .gma (incomplete download?)" % name)
        return out

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    # ---- addon metadata used to find the real view-models ----
    def lua_texts(self, prefix="lua/"):
        for nm, (_o, ln) in self.files.items():
            if nm.startswith(prefix) and nm.endswith(".lua") and ln < 2_000_000:
                try:
                    yield nm, self.read(nm).decode("utf-8", "replace")
                except Exception:
                    continue

    _VM_RX = re.compile(r"""SWEP\s*\.\s*(?:ViewModel|VModel|VM|ViewModelPath)\s*=\s*(?:Model\s*\(\s*)?["']([^"']+\.mdl)["']""", re.I)
    _WM_RX = re.compile(r"""SWEP\s*\.\s*WorldModel\s*=\s*(?:Model\s*\(\s*)?["']([^"']+\.mdl)["']""", re.I)
    _PN_RX = re.compile(r"""SWEP\s*\.\s*PrintName\s*=\s*["']([^"']+)["']""", re.I)

    def swep_info(self):
        """[(print_name, view_model_path, world_model_path)] from lua/weapons/*.lua (scripted weapons)."""
        if getattr(self, "_swep", None) is None:
            out = []
            for nm, txt in self.lua_texts("lua/weapons/"):
                vm = self._VM_RX.search(txt)
                if not vm:
                    continue
                wm = self._WM_RX.search(txt)
                pn = self._PN_RX.search(txt)
                out.append((pn.group(1) if pn else "", vm.group(1).replace("\\", "/").lower(),
                            wm.group(1).replace("\\", "/").lower() if wm else ""))
            self._swep = out
        return self._swep

    _BOLT_RX = re.compile(r"""HoldType\s*=\s*["']BoltAction["']|Name\s*=\s*["'][^"']*bolt[^"']*["']|ReloadRechambers\s*=\s*true""", re.I)

    def is_bolt_action(self, vm_path):
        """True when the weapon script that uses this view-model describes a bolt-action rifle (HoldType BoltAction,
        a 'Bolt-Action' fire mode, ReloadRechambers). Only those get the scripted cock-back after every shot."""
        cache = self.__dict__.setdefault("_boltc", {})
        vm_path = str(vm_path).replace("\\", "/").lower()
        if vm_path in cache:
            return cache[vm_path]
        res = False
        for nm, txt in self.lua_texts("lua/weapons/"):
            m = self._VM_RX.search(txt)
            if m and m.group(1).replace("\\", "/").lower() == vm_path:
                folder = nm.rsplit("/", 1)[0] + "/" if nm.count("/") >= 3 else None
                texts = [t for _n, t in self.lua_texts(folder)] if folder else [txt]
                res = any(self._BOLT_RX.search(self._strip_lua_comments(t)) for t in texts)
                break
        cache[vm_path] = res
        return res

    def viewmodel_paths(self):
        """View-model .mdl paths named by the addon's own weapon scripts that really exist in the addon."""
        seen, out = set(), []
        for _pn, vm, _wm in self.swep_info():
            if vm not in seen and vm in self.files:
                seen.add(vm)
                out.append(vm)
        return out

    _PM_RX = re.compile(r"""(?:AddValidModel\s*\(\s*["'][^"']*["']\s*,|PlayerOptionsModel["']\s*,\s*["'][^"']*["']\s*,)\s*["']([^"']+\.mdl)["']""", re.I)

    def player_model_paths(self):
        """Player models the addon registers itself (player_manager.AddValidModel / PlayerOptionsModel) that exist in it."""
        if getattr(self, "_pm", None) is None:
            seen = []
            for _nm, txt in self.lua_texts("lua/"):
                for m in self._PM_RX.finditer(txt):
                    q = m.group(1).replace("\\", "/").lower()
                    if q in self.files and q not in seen:
                        seen.append(q)
            self._pm = seen
        return self._pm

    # ---- default attachments (Modern Warfare style SWEPs: the base view-model is only the chassis) ----
    _ATT_MODEL_RX = re.compile(r"""ATTACHMENT\s*\.\s*Model\s*=\s*(?:Model\s*\(\s*)?["']([^"']+\.mdl)["']""", re.I)
    _CUSTOM_RX = re.compile(r"SWEP\s*\.\s*Customization\s*=\s*\{")

    def _att_index(self):
        if getattr(self, "_attidx", None) is None:
            idx = {}
            for nm in self.files:
                if nm.startswith("lua/") and nm.endswith(".lua"):
                    idx.setdefault(nm.rsplit("/", 1)[-1][:-4], nm)
            self._attidx = idx
        return self._attidx

    @staticmethod
    def _slots(txt):
        """First name of every slot of SWEP.Customization = { {"a","b"}, {"c"} } (comments stripped)."""
        txt = re.sub(r"--\[\[.*?\]\]", "", txt, flags=re.S)
        txt = re.sub(r"--[^\n]*", "", txt)
        m = GmaArchive._CUSTOM_RX.search(txt)
        if not m:
            return []
        depth, i, start, out = 1, m.end(), None, []
        while i < len(txt) and depth:
            c = txt[i]
            if c == "{":
                depth += 1
                if depth == 2:
                    start = i + 1
            elif c == "}":
                depth -= 1
                if depth == 1 and start is not None:
                    names = re.findall(r"""["']([^"']+)["']""", txt[start:i])
                    if names:
                        out.append(names)
                    start = None
            i += 1
        return out

    _BG_RX = re.compile(r"ATTACHMENT\s*\.\s*Bodygroups\s*=\s*\{([^}]*)\}", re.I)
    _BGKV_RX = re.compile(r"""\[\s*["']([^"']+)["']\s*\]\s*=\s*(\d+)""")
    _SIGHT_RX = re.compile(r"att_vm.*(scope|reflex|holo|reddot|fourx|optic)", re.I)

    def _model_by_name(self, name):
        """Attachment model whose file name is the attachment's name (the lua file may not be in this addon)."""
        if getattr(self, "_mdlidx", None) is None:
            idx = {}
            for nm in self.files:
                if nm.endswith(".mdl"):
                    st = nm.rsplit("/", 1)[-1][:-4]
                    if st.endswith("_hide"):
                        continue
                    idx.setdefault(st, nm)
                    idx.setdefault(re.sub(r"_lod\d$", "", st), nm)
            self._mdlidx = idx
        return self._mdlidx.get(name.lower())

    def default_attachments(self, vm_path):
        """Attachments that make up the weapon as it spawns, for a view-model named by a SWEP script:
        [(model path, {bodygroup: choice})]. The first entry of every slot of SWEP.Customization is the default;
        in the sight slot the first entry that has a model in this addon is used (the stock scope is often in
        another addon), and the weapon's own iron sights are hidden behind it."""
        vm_path = vm_path.replace("\\", "/").lower()
        folder = None
        for nm, txt in self.lua_texts("lua/weapons/"):
            m = self._VM_RX.search(txt)
            if m and m.group(1).replace("\\", "/").lower() == vm_path:
                folder = nm.rsplit("/", 1)[0] + "/"
                break
        if folder is None:
            return []
        slots = []
        for nm, txt in self.lua_texts(folder):
            slots = self._slots(txt)
            if slots:
                break
        idx, out = self._att_index(), []

        def resolve(name):
            """(model path, bodygroups) of one attachment name, or None when this addon has no model for it."""
            lua, bg, q = idx.get(name.lower()), {}, None
            if lua:
                try:
                    txt = self.read(lua).decode("utf-8", "replace")
                    m = self._ATT_MODEL_RX.search(txt)
                    if m:
                        q = m.group(1).replace("\\", "/").lower()
                    g = self._BG_RX.search(re.sub(r"--[^\n]*", "", txt))
                    if g:
                        bg = {k.lower(): int(v) for k, v in self._BGKV_RX.findall(g.group(1))}
                except Exception:
                    pass
            if not q:
                q = self._model_by_name(name)
            if q and q in self.files and q != vm_path:
                return q, bg
            return None
        for names in slots:
            if any(self._SIGHT_RX.search(x) for x in names):
                hit = next((r for r in (resolve(x) for x in names if self._SIGHT_RX.search(x)) if r), None)
                if hit:
                    path, bg = hit
                    out.append((path, bg or {"tag_sight": -1}))     # -1 = whichever choice draws the least (no iron sights)
                continue
            r = resolve(names[0])
            if r and r[0] not in [o[0] for o in out]:
                out.append(r)
        return out

    # ---- sounds: Garry's Mod weapons play their sounds from Lua, not (only) from model events ----
    @staticmethod
    def _strip_lua_comments(txt):
        txt = re.sub(r"--\[(=*)\[.*?\]\1\]", "", txt, flags=re.S)
        return re.sub(r"--[^\n]*", "", txt)

    def lua_sound_defs(self):
        """{sound name: audio path} from sound.Add{ name = "X", sound = "a.wav" | { "a.wav", "b.wav" } } in any lua
        file (the first wave of a list). Model events and SWEP tables refer to sounds by these names."""
        if getattr(self, "_sdefs", None) is None:
            out = {}
            rx = re.compile(r"sound\s*\.\s*Add\s*\(?\s*\{", re.I)
            for _nm, txt in self.lua_texts("lua/"):
                if "sound.add" not in txt.lower():
                    continue
                txt = self._strip_lua_comments(txt)
                for m in rx.finditer(txt):
                    depth, i = 1, m.end()
                    while i < len(txt) and depth:
                        depth += (txt[i] == "{") - (txt[i] == "}")
                        i += 1
                    body = txt[m.end():i - 1]
                    n = re.search(r"""\bname\s*=\s*["']([^"']+)["']""", body, re.I)
                    w = re.search(r"""\bsound\s*=\s*(?:\{[^}]*?)?["']([^"']+)["']""", body, re.I)
                    if n and w:
                        out[n.group(1).lower()] = re.sub(r"^[\)\(\^#\*<>@~\?\$`]+", "", w.group(1)).replace("\\", "/").lower()
            self._sdefs = out
        return self._sdefs

    def sound_path(self, ref):
        """A sound reference of a lua table (a file path or a sound.Add name) -> 'sound/....wav' inside this addon, or None."""
        if not ref:
            return None
        r = re.sub(r"^[\)\(\^#\*<>@~\?\$`]+", "", ref.strip()).replace("\\", "/").lower()
        r = self.lua_sound_defs().get(r, r)
        for c in (r, "sound/" + r):
            if c in self.files:
                return c
        tail = "/" + r.lstrip("/")
        return next((n for n in self.files if n.startswith("sound/") and n.endswith(tail)), None)

    _TKEYS = r"(?:time|t|delay|timestamp|at|cycle)"
    _SKEYS = r"(?:s|snd|sound|value|file|path|name|sfx)"

    def swep_sound_timeline(self, vm_path):
        """Timed sounds the weapon's own lua tables ask for: [(action key, seconds, 'sound/..path in the addon'), ...]
        e.g. TFA   EventTable[ACT_VM_RELOAD] = { {time = 0.5, type = "sound", value = "x.wav"} }
             ArcCW Animations["reload"].SoundTable = { {s = "x.wav", t = 0.5} }
             CW2   SWEP.Sounds.base_reload = { {time = 0.5, sound = "CW_X_MAGOUT"} }
        The action key is the path of table keys the entry sits under ("eventtable.act_vm_reload", "animations.reload_empty.soundtable" ...)."""
        vm_path = vm_path.replace("\\", "/").lower()
        cache = self.__dict__.setdefault("_tl", {})
        if vm_path in cache:
            return cache[vm_path]
        texts = []
        for nm, txt in self.lua_texts("lua/weapons/"):
            m = self._VM_RX.search(txt)
            if m and m.group(1).replace("\\", "/").lower() == vm_path:
                if nm.count("/") >= 3:                  # lua/weapons/<folder>/shared.lua: the whole folder is the weapon
                    texts = list(self.lua_texts(nm.rsplit("/", 1)[0] + "/"))
                else:
                    texts = [(nm, txt)]
                break
        out = []
        for _nm, txt in texts:
            out += self._timeline_from_text(self._strip_lua_comments(txt))
        cache[vm_path] = out
        return out

    def _timeline_from_text(self, txt):
        out = []
        key_rx = re.compile(r"""(?:\[\s*["']?([A-Za-z_][\w ]*)["']?\s*\]|([A-Za-z_]\w*))\s*=\s*$""")
        tim = re.compile(r"\b" + self._TKEYS + r"\s*=\s*(-?[0-9]*\.?[0-9]+)", re.I)
        snd = re.compile(r"\b" + self._SKEYS + r"\s*=\s*(?:Sound\s*\(\s*)?[\"']([^\"']+)[\"']", re.I)
        stack = []                      # key of every open table (None for an unnamed one)
        i, n = 0, len(txt)
        while i < n:
            c = txt[i]
            if c == "{":
                km = key_rx.search(txt[max(0, i - 80):i].rstrip())
                key = (km.group(1) or km.group(2)).lower() if km else None
                j = txt.find("}", i)
                inner = txt.find("{", i + 1)
                if j != -1 and (inner == -1 or inner > j):          # leaf table: look for a timed sound in it
                    body = txt[i + 1:j]
                    tm, sm = tim.search(body), snd.search(body)
                    if tm and sm:
                        path = self.sound_path(sm.group(1))
                        if path:
                            act = ".".join([k for k in stack if k and not re.fullmatch(r"\d+", k)]) or (key or "")
                            out.append((act, float(tm.group(1)), path))
                    i = j + 1
                    continue
                stack.append(key)
            elif c == "}":
                if stack:
                    stack.pop()
            i += 1
        return out

    def has_maps(self):
        return any(n.startswith("maps/") and n.endswith(".bsp") for n in self.files)
