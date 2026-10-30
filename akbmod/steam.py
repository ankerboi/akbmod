"""Steam identity for AKB-MOD (Spacewar app 480).

1) Preferred: steam_api64.dll → SteamAPI_Init (shows as Playing Spacewar, full API).
2) Fallback: read the Steam install (registry / standard paths) for the logged-in
   account's persona name, and download the avatar from Steam Community XML.
   Works without shipping steam_api64.dll.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

APP_ID = 480  # Spacewar
MAGIC = "AKBARENA3"
MAX_PLAYERS = 8


@dataclass
class RoomInfo:
    lobby: int
    name: str
    host_name: str
    host: int
    players: int
    max: int
    map: str = ""


@dataclass
class FriendInfo:
    id: int
    name: str
    online: bool
    in_game: bool


class SteamEvent:
    def __init__(self, kind: str, **kw):
        self.kind = kind
        self.__dict__.update(kw)


# --------------------------------------------------------------------------------------
# paths / app id
# --------------------------------------------------------------------------------------

def _write_appid(folder: Optional[Path] = None) -> List[str]:
    written = []
    roots = []
    if folder:
        roots.append(Path(folder))
    roots.append(Path.cwd())
    try:
        roots.append(Path(sys.argv[0]).resolve().parent)
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    seen = set()
    for root in roots:
        try:
            root = root.resolve()
        except Exception:
            continue
        if root in seen:
            continue
        seen.add(root)
        try:
            p = root / "steam_appid.txt"
            p.write_text("%d\n" % APP_ID)
            written.append(str(p))
        except OSError:
            continue
    return written


def _steam_roots() -> List[Path]:
    roots: List[Path] = []
    seen = set()

    def add(p):
        try:
            p = Path(p).expanduser().resolve()
        except Exception:
            return
        if p in seen or not p.is_dir():
            return
        seen.add(p)
        roots.append(p)

    for key in ("STEAM_PATH", "STEAM_ROOT", "SteamPath", "STEAM_DIR"):
        v = os.environ.get(key)
        if v:
            add(v)

    home = Path.home()
    if sys.platform == "win32":
        try:
            import winreg
            for hive, sub in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        val, _ = winreg.QueryValueEx(k, "SteamPath")
                        if val:
                            add(val)
                except OSError:
                    pass
        except Exception:
            pass
        pf = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        pf64 = os.environ.get("ProgramFiles") or r"C:\Program Files"
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        for base in (pf, pf64, local):
            add(Path(base) / "Steam")
    elif sys.platform == "darwin":
        add(home / "Library/Application Support/Steam")
    else:
        for p in (
            home / ".steam/steam",
            home / ".steam/root",
            home / ".local/share/Steam",
            home / ".var/app/com.valvesoftware.Steam/data/Steam",
        ):
            add(p)

    for root in list(roots):
        for vdf in (
            root / "steamapps" / "libraryfolders.vdf",
            root / "config" / "libraryfolders.vdf",
        ):
            if not vdf.is_file():
                continue
            try:
                data = vdf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in data.splitlines():
                if "path" not in line.lower():
                    continue
                for part in line.split('"'):
                    part = part.strip().replace("\\\\", "\\")
                    if len(part) >= 2 and (part[1] == ":" or part.startswith("/")):
                        if Path(part).is_dir():
                            add(part)
    return roots


def _find_steam_lib() -> Optional[str]:
    env = os.environ.get("STEAM_API_LIB") or os.environ.get("STEAMWORKS_LIB")
    if env and Path(env).is_file():
        return str(Path(env).resolve())

    if sys.platform == "win32":
        names = ("steam_api64.dll", "steam_api.dll")
        subdirs = (
            "",
            "redistributable_bin/win64",
            "redistributable_bin",
            "bin/win64",
            "steamapps/common/Spacewar",
            "steamapps/common/SpaceWar",
            "steamapps/common/Steamworks SDK Redist/redistributable_bin/win64",
        )
    elif sys.platform == "darwin":
        names = ("libsteam_api.dylib",)
        subdirs = ("", "redistributable_bin/osx", "steamapps/common/Spacewar")
    else:
        names = ("libsteam_api.so", "libsteam_api.so.1")
        subdirs = (
            "",
            "redistributable_bin/linux64",
            "steamapps/common/Spacewar",
            "steamapps/common/Steamworks SDK Redist/redistributable_bin/linux64",
        )

    candidates: List[Path] = []
    local_dirs = [Path.cwd(), Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent]
    if getattr(sys, "frozen", False):
        local_dirs.insert(0, Path(sys.executable).resolve().parent)
    try:
        local_dirs.append(Path(sys.argv[0]).resolve().parent)
    except Exception:
        pass
    for d in local_dirs:
        for n in names:
            candidates.append(d / n)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            for n in names:
                candidates.append(Path(entry) / n)
    for root in _steam_roots():
        for sub in subdirs:
            base = root / sub if sub else root
            for n in names:
                candidates.append(base / n)
        common = root / "steamapps" / "common"
        if common.is_dir():
            try:
                for child in common.iterdir():
                    if not child.is_dir():
                        continue
                    for n in names:
                        candidates.append(child / n)
                        candidates.append(child / "bin" / n)
                        candidates.append(child / "redistributable_bin" / "win64" / n)
                        candidates.append(child / "redistributable_bin" / "linux64" / n)
            except OSError:
                pass

    checked = set()
    for p in candidates:
        try:
            key = str(p.resolve()) if p.exists() else str(p)
        except OSError:
            key = str(p)
        if key in checked:
            continue
        checked.add(key)
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    return None


def _bind(lib, name, restype=None, argtypes=None):
    try:
        fn = getattr(lib, name)
    except AttributeError:
        return None
    if restype is not None:
        fn.restype = restype
    if argtypes is not None:
        fn.argtypes = argtypes
    return fn


# --------------------------------------------------------------------------------------
# Local Steam install fallback (no dll)
# --------------------------------------------------------------------------------------

def _parse_loginusers(
    path: Path,
    prefer_account: Optional[str] = None,
    prefer_id64: Optional[int] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """Return (steam_id64, persona) for the *currently logged-in* user.

    Priority:
      1. prefer_id64 (from registry ActiveUser)
      2. prefer_account (AutoLoginUser / AccountName match)
      3. MostRecent == "1"
      4. highest Timestamp
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None

    users = []  # list of dicts
    # Line-oriented brace parse so nested tables inside a user block still work
    current_id = None
    depth = 0
    block_lines: List[str] = []
    for line in raw.splitlines():
        s = line.strip()
        # new user id at depth 1 under "users"
        id_m = re.match(r'"(\d{17})"\s*$', s)
        if id_m and depth <= 1:
            current_id = int(id_m.group(1))
            block_lines = []
        if "{" in s:
            depth += s.count("{")
        if current_id is not None and depth >= 2:
            block_lines.append(s)
        if "}" in s:
            depth -= s.count("}")
            if current_id is not None and depth <= 1:
                block = "\n".join(block_lines)
                persona = None
                account = None
                recent = 0
                ts = 0
                m = re.search(r'"PersonaName"\s*"([^"]*)"', block)
                if m:
                    persona = m.group(1)
                m = re.search(r'"AccountName"\s*"([^"]*)"', block)
                if m:
                    account = m.group(1)
                m = re.search(r'"MostRecent"\s*"(\d+)"', block)
                if m:
                    recent = int(m.group(1))
                m = re.search(r'"Timestamp"\s*"(\d+)"', block)
                if m:
                    ts = int(m.group(1))
                if persona or account:
                    users.append({
                        "id": current_id,
                        "persona": persona or account or "Player",
                        "account": (account or "").lower(),
                        "recent": recent,
                        "ts": ts,
                    })
                current_id = None
                block_lines = []

    if not users:
        m = re.search(r'"PersonaName"\s*"([^"]+)"', raw)
        id_m = re.search(r'"(\d{17})"', raw)
        if m and id_m:
            return int(id_m.group(1)), m.group(1)
        return None, None

    # 1) exact steam id from registry
    if prefer_id64:
        for u in users:
            if u["id"] == prefer_id64:
                return u["id"], u["persona"]

    # 2) AutoLoginUser account name
    if prefer_account:
        acc = prefer_account.lower().strip()
        for u in users:
            if u["account"] == acc:
                return u["id"], u["persona"]

    # 3) MostRecent = 1 (the account Steam considers active)
    for u in users:
        if u["recent"] == 1:
            return u["id"], u["persona"]

    # 4) newest Timestamp
    users.sort(key=lambda u: u["ts"], reverse=True)
    u = users[0]
    return u["id"], u["persona"]


def _registry_active_user() -> Tuple[Optional[int], Optional[str]]:
    """Windows registry: ActiveUser (32-bit account id) + AutoLoginUser name."""
    if sys.platform != "win32":
        return None, None
    prefer_id64 = None
    prefer_account = None
    try:
        import winreg
        # ActiveProcess\ActiveUser = 32-bit Steam account id
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess"
            ) as k:
                try:
                    val, _ = winreg.QueryValueEx(k, "ActiveUser")
                    acct = int(val)
                    if acct > 0:
                        # SteamID64 = account_id + 76561197960265728
                        prefer_id64 = acct + 76561197960265728
                except OSError:
                    pass
        except OSError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                try:
                    val, _ = winreg.QueryValueEx(k, "AutoLoginUser")
                    if val:
                        prefer_account = str(val)
                except OSError:
                    pass
        except OSError:
            pass
    except Exception:
        pass
    return prefer_id64, prefer_account


def _most_recent_userdata(root: Path) -> Optional[int]:
    """Pick userdata/<account32> with the newest localconfig / most recently modified."""
    ud = root / "userdata"
    if not ud.is_dir():
        return None
    best_acct, best_mtime = None, -1.0
    try:
        kids = list(ud.iterdir())
    except OSError:
        return None
    for child in kids:
        if not child.is_dir() or not child.name.isdigit():
            continue
        acct = int(child.name)
        if acct <= 0:
            continue
        # prefer folders that have a recent localconfig or config
        mtime = 0.0
        for rel in (
            child / "config" / "localconfig.vdf",
            child / "7" / "remote" / "sharedconfig.vdf",
            child / "config",
        ):
            try:
                if rel.is_file():
                    mtime = max(mtime, rel.stat().st_mtime)
                elif rel.is_dir():
                    mtime = max(mtime, rel.stat().st_mtime)
            except OSError:
                pass
        try:
            mtime = max(mtime, child.stat().st_mtime)
        except OSError:
            pass
        if mtime > best_mtime:
            best_mtime = mtime
            best_acct = acct
    if best_acct:
        return best_acct + 76561197960265728
    return None


def _read_local_steam_identity() -> Tuple[Optional[int], Optional[str]]:
    prefer_id64, prefer_account = _registry_active_user()

    # If registry only gave ActiveUser, that is the source of truth while Steam is running
    for root in _steam_roots():
        p = root / "config" / "loginusers.vdf"
        if not p.is_file():
            continue
        sid, name = _parse_loginusers(p, prefer_account=prefer_account, prefer_id64=prefer_id64)
        if sid and name:
            return sid, name

    # userdata last-write fallback (often the account you actually use)
    if not prefer_id64:
        for root in _steam_roots():
            prefer_id64 = _most_recent_userdata(root)
            if prefer_id64:
                break
        if prefer_id64:
            for root in _steam_roots():
                p = root / "config" / "loginusers.vdf"
                if p.is_file():
                    sid, name = _parse_loginusers(p, prefer_id64=prefer_id64)
                    if sid and name:
                        return sid, name

    # last resort: any loginusers parse
    for root in _steam_roots():
        p = root / "config" / "loginusers.vdf"
        if p.is_file():
            sid, name = _parse_loginusers(p)
            if sid or name:
                return sid, name
    return None, None


def _fetch_avatar_url(steam_id: int) -> Optional[Tuple[int, int, bytes]]:
    """Download medium avatar via community XML (no API key)."""
    urls = [
        "https://steamcommunity.com/profiles/%d?xml=1" % steam_id,
        "https://steamcommunity.com/profiles/%d/?xml=1" % steam_id,
    ]
    avatar_url = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AKB-MOD/1.0"})
            with urllib.request.urlopen(req, timeout=4) as r:
                xml = r.read().decode("utf-8", "replace")
            # <avatarMedium><![CDATA[https://...]]></avatarMedium>
            m = re.search(
                r"<avatarMedium>\s*(?:<!\[CDATA\[)?\s*(https?://[^\]<\s]+)",
                xml,
            )
            if not m:
                m = re.search(
                    r"<avatarFull>\s*(?:<!\[CDATA\[)?\s*(https?://[^\]<\s]+)",
                    xml,
                )
            if not m:
                m = re.search(
                    r"<avatarIcon>\s*(?:<!\[CDATA\[)?\s*(https?://[^\]<\s]+)",
                    xml,
                )
            if m:
                avatar_url = m.group(1).strip()
                break
        except Exception:
            continue
    if not avatar_url:
        return None
    try:
        req = urllib.request.Request(avatar_url, headers={"User-Agent": "AKB-MOD/1.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = r.read()
        # decode with pygame if available, else return raw and let caller handle
        try:
            import pygame
            import numpy as np
            from io import BytesIO
            surf = pygame.image.load(BytesIO(data)).convert_alpha()
            w, h = surf.get_size()
            rgba = pygame.image.tostring(surf, "RGBA", False)
            return w, h, rgba
        except Exception:
            return None
    except Exception:
        return None


def _local_avatar_cache(steam_id: int) -> Optional[Tuple[int, int, bytes]]:
    """Try Steam's on-disk avatar cache — only files that clearly belong to this steam id."""
    try:
        import pygame
        from io import BytesIO
    except Exception:
        return None
    acct32 = steam_id & 0xFFFFFFFF
    # Accept only names that contain the full id64 or the exact account32 id
    needles = (str(steam_id), str(acct32))
    for root in _steam_roots():
        folders = [
            root / "config" / "avatarcache",
            root / "userdata" / str(acct32) / "config",
        ]
        for folder in folders:
            if not folder.is_dir():
                continue
            try:
                files = list(folder.iterdir())
            except OSError:
                continue
            for f in files:
                if f.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                    continue
                low = f.stem.lower()
                if not any(n in low for n in needles):
                    continue  # never pick another account's file
                try:
                    data = f.read_bytes()
                    if len(data) < 64:
                        continue
                    surf = pygame.image.load(BytesIO(data)).convert_alpha()
                    w, h = surf.get_size()
                    if w < 16 or h < 16:
                        continue
                    rgba = pygame.image.tostring(surf, "RGBA", False)
                    return w, h, rgba
                except Exception:
                    continue
    return None


# --------------------------------------------------------------------------------------
# Steam class
# --------------------------------------------------------------------------------------

class Steam:
    def __init__(self):
        self.available = False      # True if identity is known (dll OR local fallback)
        self.api_live = False       # True only if SteamAPI_Init succeeded
        self.me = 0
        self.persona = "Player"
        self.lobby: Optional[int] = None
        self._lib = None
        self._error = ""
        self._rooms: List[RoomInfo] = []
        self._events: List[SteamEvent] = []
        self._lock = threading.Lock()
        self._members: set = set()
        self._lib_path: Optional[str] = None
        self._friends = None
        self._utils = None
        self._user = None
        self._avatar_cache: dict = {}
        self._avatar_pending: set = set()
        self.appid_files: List[str] = []
        self._fallback_tried = False

    def connect(self) -> bool:
        self.appid_files = _write_appid()
        for root in _steam_roots()[:1]:
            os.environ.setdefault("SteamPath", str(root))
            break

        # --- try native API first ---
        path = _find_steam_lib()
        self._lib_path = path
        if path:
            try:
                if self._init_api(path):
                    self.api_live = True
                    self.available = True
                    self._error = ""
                    return True
            except Exception as e:
                self._error = str(e)

        # --- fallback: local Steam files + community avatar ---
        sid, name = _read_local_steam_identity()
        if name:
            self.persona = name
        if sid:
            self.me = sid
        if name or sid:
            self.available = True
            self.api_live = False
            self._avatar_cache.clear()
            self._avatar_pending.clear()
            self._error = (
                "Using Steam account from local install (no steam_api64.dll). "
                "Name: %s (id %s). Put steam_api64.dll next to the game to show as Spacewar."
                % (self.persona, self.me or "?")
            )
            print("[steam] active account:", self.persona, self.me)
            return True

        if not self._error:
            self._error = (
                "Steam not found. Start Steam and log in once, or place steam_api64.dll "
                "next to run_akbmod.py."
            )
        self.available = False
        return False

    def _init_api(self, path: str) -> bool:
        if sys.platform == "win32":
            dll_dir = str(Path(path).resolve().parent)
            try:
                os.add_dll_directory(dll_dir)
            except Exception:
                pass
            self._lib = ctypes.WinDLL(path)
        else:
            self._lib = ctypes.CDLL(path)

        init = _bind(self._lib, "SteamAPI_Init", ctypes.c_bool, [])
        if init is None:
            init = _bind(self._lib, "SteamAPI_InitSafe", ctypes.c_bool, [])
        if init is None:
            raise OSError("SteamAPI_Init not exported from %s" % path)

        restart = _bind(self._lib, "SteamAPI_RestartAppIfNecessary", ctypes.c_bool, [ctypes.c_uint32])
        if restart is not None:
            try:
                if restart(APP_ID):
                    os._exit(0)
            except Exception:
                pass

        if not init():
            raise OSError(
                "SteamAPI_Init failed (is Steam running?). appid files: %s"
                % (", ".join(self.appid_files) or "none")
            )

        self._user = self._iface((
            "SteamAPI_SteamUser_v023", "SteamAPI_SteamUser_v022",
            "SteamAPI_SteamUser_v021", "SteamAPI_SteamUser_v020",
        ))
        self._friends = self._iface((
            "SteamAPI_SteamFriends_v017", "SteamAPI_SteamFriends_v016",
            "SteamAPI_SteamFriends_v015",
        ))
        self._utils = self._iface((
            "SteamAPI_SteamUtils_v010", "SteamAPI_SteamUtils_v009",
            "SteamAPI_SteamUtils_v008",
        ))
        self._read_identity()
        return True

    def _iface(self, names: Tuple[str, ...]):
        for name in names:
            fn = _bind(self._lib, name, ctypes.c_void_p, [])
            if not fn:
                continue
            try:
                ptr = fn()
            except Exception:
                continue
            if ptr:
                return ptr
        return None

    def _read_identity(self):
        get_name = _bind(
            self._lib, "SteamAPI_ISteamFriends_GetPersonaName",
            ctypes.c_char_p, [ctypes.c_void_p],
        )
        if get_name and self._friends:
            try:
                raw = get_name(self._friends)
                if raw:
                    self.persona = raw.decode("utf-8", "replace")
            except Exception:
                pass
        get_id = _bind(
            self._lib, "SteamAPI_ISteamUser_GetSteamID",
            ctypes.c_uint64, [ctypes.c_void_p],
        )
        if get_id and self._user:
            try:
                self.me = int(get_id(self._user))
            except Exception:
                pass
        if not self.me or not self.persona or self.persona == "Player":
            sid, name = _read_local_steam_identity()
            if name and (not self.persona or self.persona == "Player"):
                self.persona = name
            if sid and not self.me:
                self.me = sid

    def run_callbacks(self) -> None:
        if self.api_live and self._lib:
            fn = _bind(self._lib, "SteamAPI_RunCallbacks", None, [])
            if fn:
                try:
                    fn()
                except Exception:
                    pass
            for sid in list(self._avatar_pending):
                if self._fetch_avatar_api(sid) is not None:
                    self._avatar_pending.discard(sid)

    def shutdown(self) -> None:
        if self._lib and self.api_live:
            fn = _bind(self._lib, "SteamAPI_Shutdown", None, [])
            if fn:
                try:
                    fn()
                except Exception:
                    pass
        self.api_live = False
        self._lib = None

    def avatar(self, steam_id: Optional[int] = None, *, force: bool = False) -> Optional[Tuple[int, int, bytes]]:
        sid = int(steam_id or self.me or 0)
        if not sid:
            sid2, name = _read_local_steam_identity()
            sid = int(sid2 or 0)
            if sid:
                self.me = sid
            if name:
                self.persona = name
        if not sid:
            return None
        if force and sid in self._avatar_cache:
            del self._avatar_cache[sid]

        if sid in self._avatar_cache:
            return self._avatar_cache[sid]

        # 1) Community XML for *this* steam id (correct picture for the active account)
        got = _fetch_avatar_url(sid)
        if got:
            self._avatar_cache[sid] = got
            print("[steam] avatar from community for", sid)
            return got

        # 2) Native API
        if self.api_live:
            got = self._fetch_avatar_api(sid)
            if got:
                return got

        # 3) Strict local disk cache for this id only
        got = _local_avatar_cache(sid)
        if got:
            self._avatar_cache[sid] = got
            return got
        return None

    def _fetch_avatar_api(self, sid: int) -> Optional[Tuple[int, int, bytes]]:
        if not self._friends or not self._utils or not self._lib:
            return None
        lib = self._lib
        handle = 0
        for fn_name in (
            "SteamAPI_ISteamFriends_GetMediumFriendAvatar",
            "SteamAPI_ISteamFriends_GetLargeFriendAvatar",
            "SteamAPI_ISteamFriends_GetSmallFriendAvatar",
        ):
            fn = _bind(lib, fn_name, ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint64])
            if not fn:
                continue
            try:
                handle = int(fn(self._friends, ctypes.c_uint64(sid)))
            except Exception:
                handle = 0
            if handle > 0:
                break
        if handle <= 0:
            self._avatar_pending.add(sid)
            return None
        get_size = _bind(
            lib, "SteamAPI_ISteamUtils_GetImageSize", ctypes.c_bool,
            [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)],
        )
        get_rgba = _bind(
            lib, "SteamAPI_ISteamUtils_GetImageRGBA", ctypes.c_bool,
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int],
        )
        if not get_size or not get_rgba:
            return None
        w = ctypes.c_uint32(0)
        h = ctypes.c_uint32(0)
        try:
            if not get_size(self._utils, handle, ctypes.byref(w), ctypes.byref(h)):
                self._avatar_pending.add(sid)
                return None
        except Exception:
            return None
        if not w.value or not h.value:
            return None
        buf = (ctypes.c_uint8 * (w.value * h.value * 4))()
        try:
            if not get_rgba(self._utils, handle, buf, w.value * h.value * 4):
                self._avatar_pending.add(sid)
                return None
        except Exception:
            return None
        data = bytes(buf)
        self._avatar_cache[sid] = (int(w.value), int(h.value), data)
        self._avatar_pending.discard(sid)
        return self._avatar_cache[sid]

    @property
    def status_hint(self) -> str:
        if self.available and self.api_live:
            return "Steam: Spacewar (%d) as %s" % (APP_ID, self.persona)
        if self.available:
            return "Steam account: %s (local) — avatar from Steam Community" % self.persona
        return self._error or "Steam not available."

    def friends(self) -> List[FriendInfo]:
        return []

    def refresh_rooms(self) -> None:
        pass

    def create_lobby(self, name: str, host_name: str, map_name: str = "") -> None:
        pass

    def join_lobby(self, lobby_id: int) -> None:
        pass

    def leave_lobby(self) -> None:
        self.lobby = None
        self._members.clear()

    def poll(self) -> List[SteamEvent]:
        with self._lock:
            ev, self._events = self._events, []
        return ev

    def members(self) -> List[int]:
        return list(self._members)


_steam: Optional[Steam] = None


def get_steam() -> Steam:
    global _steam
    if _steam is None:
        _steam = Steam()
    return _steam
