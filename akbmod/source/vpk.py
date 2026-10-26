"""Valve VPK archive reader."""
import re
import struct
from pathlib import Path

class VpkArchive:
    """Reads a Valve .vpk (single file, or *_dir.vpk with *_000.vpk archives). Lazy: files are
    read from disk only when asked for, so a 165 MB package costs no memory.

    Accepts incomplete downloads (.vpk.part): the directory tree is still parsed and any entry
    whose payload is fully present on disk can be read; truncated entries raise a clear error.
    """

    def __init__(self, path):
        self.path = str(path)
        self.files = {}
        self._fh = {}
        self._disk_size = Path(path).stat().st_size
        with open(self.path, "rb") as f:
            sig, ver, tree = struct.unpack("<III", f.read(12))
            if sig != 0x55AA1234 or ver not in (1, 2):
                raise ValueError("not a VPK file")
            hdr = 28 if ver == 2 else 12
            f.seek(hdr)
            b = f.read(tree)
        self.base = hdr + tree
        p = 0

        def cs():
            nonlocal p
            e = b.index(b"\0", p)
            s = b[p:e].decode("utf-8", "replace")
            p = e + 1
            return s
        while True:
            ext = cs()
            if not ext:
                break
            while True:
                d = cs()
                if not d:
                    break
                while True:
                    n = cs()
                    if not n:
                        break
                    _crc, pre, arch, off, ln, _t = struct.unpack_from("<IHHIIH", b, p)
                    p += 18
                    preload = b[p:p + pre]
                    p += pre
                    nm = (d + "/" if d != " " else "") + n + ("." + ext if ext != " " else "")
                    self.files[nm.replace("\\", "/").lower()] = (arch, off, ln, preload)
        # Flag truncated packages (common with browser .vpk.part downloads)
        self.incomplete = False
        for arch, off, ln, pre in self.files.values():
            if arch == 0x7FFF and ln and (self.base + off + ln) > self._disk_size:
                self.incomplete = True
                break

    def complete(self, name):
        """True when the full payload for this entry is present on disk."""
        arch, off, ln, pre = self.files[name.lower().replace("\\", "/")]
        if arch != 0x7FFF:
            return True  # multi-file VPK: assume paired _000.vpk is intact
        return (self.base + off + ln) <= self._disk_size

    def _open(self, arch):
        if arch not in self._fh:
            if arch == 0x7FFF:
                self._fh[arch] = open(self.path, "rb")
            else:
                self._fh[arch] = open(re.sub(r"_dir\.vpk$", "_%03d.vpk" % arch, self.path), "rb")
        return self._fh[arch]

    def has(self, name):
        return name.lower().replace("\\", "/") in self.files

    def size(self, name):
        e = self.files[name.lower().replace("\\", "/")]
        return len(e[3]) + e[2]

    def read(self, name, start=0, length=None):
        """Bytes [start, start+length) of the file (whole file by default)."""
        key = name.lower().replace("\\", "/")
        arch, off, ln, pre = self.files[key]
        total = len(pre) + ln
        if length is None:
            length = total - start
        out = b""
        if start < len(pre):
            out = pre[start:start + length]
            start2, length2 = 0, length - len(out)
        else:
            start2, length2 = start - len(pre), length
        if length2 > 0 and ln:
            if arch == 0x7FFF and (self.base + off + start2 + length2) > self._disk_size:
                raise ValueError(
                    "%s is incomplete in this VPK (download truncated at %d bytes; need ~%d). "
                    "Re-download the full .vpk." % (key, self._disk_size, self.base + off + ln))
            fh = self._open(arch)
            fh.seek((self.base if arch == 0x7FFF else 0) + off + start2)
            chunk = fh.read(min(length2, ln - start2))
            if len(chunk) < length2:
                raise ValueError("%s truncated in VPK (got %d of %d bytes)" % (key, len(chunk), length2))
            out += chunk
        return out

    def close(self):
        for f in self._fh.values():
            f.close()
        self._fh = {}

