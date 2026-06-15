"""Make the bundled Q2RTX binary find its OpenSSL 1.1 libs without LD_LIBRARY_PATH.

NVIDIA's Q2RTX Linux build records an absolute, non-relocatable ``DT_RUNPATH`` that
points at their build machine (``/mnt/q2rtx/.``), so on an end-user system the
loader cannot find the bundled ``libssl.so.1.1`` / ``libcrypto.so.1.1`` and falls
back to ``LD_LIBRARY_PATH`` -- which the dynamic loader strips when the binary is
launched through a capability-carrying gamescope (``AT_SECURE``).

Rewriting that RUNPATH string in place to ``$ORIGIN`` makes the loader look next to
the binary, which is honored even under ``AT_SECURE`` and needs no ``patchelf``:
``$ORIGIN`` is shorter than the build path, so it fits in the existing string slot.
The matching OpenSSL libs are copied next to the binary by the caller.
"""

from __future__ import annotations

import struct
from pathlib import Path


_DT_NULL = 0
_DT_RPATH = 15
_DT_RUNPATH = 29
ORIGIN_RUNPATH = "$ORIGIN"


def _find_runpath_string(data: bytes) -> tuple[int, str] | None:
    """Return ``(file_offset, value)`` of the DT_RUNPATH/DT_RPATH string, or None.

    Only 64-bit little-endian ELF files are handled; anything else returns None so
    the caller falls back to ``LD_LIBRARY_PATH``.
    """
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return None
    if data[4] != 2 or data[5] != 1:  # ELFCLASS64, ELFDATA2LSB
        return None
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", data, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", data, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", data, 0x3E)[0]
    if e_shoff == 0 or e_shnum == 0 or e_shentsize < 64 or e_shstrndx >= e_shnum:
        return None
    if e_shoff + e_shnum * e_shentsize > len(data):
        return None

    def section(index: int) -> tuple[int, int, int]:
        base = e_shoff + index * e_shentsize
        sh_name = struct.unpack_from("<I", data, base)[0]
        sh_offset = struct.unpack_from("<Q", data, base + 0x18)[0]
        sh_size = struct.unpack_from("<Q", data, base + 0x20)[0]
        return sh_name, sh_offset, sh_size

    _name, shstr_off, _shstr_size = section(e_shstrndx)

    def section_name(name_off: int) -> str:
        start = shstr_off + name_off
        end = data.find(b"\x00", start)
        if start >= len(data) or end < 0:
            return ""
        return data[start:end].decode("ascii", "replace")

    dynamic: tuple[int, int] | None = None
    dynstr: tuple[int, int] | None = None
    for i in range(e_shnum):
        sh_name, sh_offset, sh_size = section(i)
        name = section_name(sh_name)
        if name == ".dynamic":
            dynamic = (sh_offset, sh_size)
        elif name == ".dynstr":
            dynstr = (sh_offset, sh_size)
    if dynamic is None or dynstr is None:
        return None

    dyn_off, dyn_size = dynamic
    dynstr_off, dynstr_size = dynstr
    # The loader ignores DT_RPATH when DT_RUNPATH is present, so prefer RUNPATH.
    runpath_val: int | None = None
    rpath_val: int | None = None
    for i in range(dyn_size // 16):
        base = dyn_off + i * 16
        if base + 16 > len(data):
            break
        d_tag, d_val = struct.unpack_from("<qQ", data, base)
        if d_tag == _DT_NULL:
            break
        if d_tag == _DT_RUNPATH:
            runpath_val = d_val
        elif d_tag == _DT_RPATH:
            rpath_val = d_val
    chosen = runpath_val if runpath_val is not None else rpath_val
    if chosen is None or chosen >= dynstr_size:
        return None
    str_off = dynstr_off + chosen
    end = data.find(b"\x00", str_off)
    if str_off >= len(data) or end < 0:
        return None
    return str_off, data[str_off:end].decode("ascii", "replace")


def patch_runpath_to_origin(binary_path: Path) -> str | None:
    """Rewrite ``binary_path``'s RUNPATH to ``$ORIGIN`` in place.

    Returns the previous RUNPATH on success, ``"$ORIGIN"`` if it was already set, or
    None if the file could not be safely patched (the caller keeps relying on
    ``LD_LIBRARY_PATH`` in that case).
    """
    try:
        data = bytearray(binary_path.read_bytes())
    except OSError:
        return None
    found = _find_runpath_string(bytes(data))
    if found is None:
        return None
    str_off, current = found
    if current.split(":")[0] == ORIGIN_RUNPATH:
        return ORIGIN_RUNPATH
    region_len = len(current.encode("ascii")) + 1  # include the NUL terminator
    replacement = ORIGIN_RUNPATH.encode("ascii") + b"\x00"
    if len(replacement) > region_len:
        return None  # cannot shrink-fit; growing .dynstr would need patchelf
    data[str_off : str_off + region_len] = replacement + b"\x00" * (
        region_len - len(replacement)
    )
    try:
        binary_path.write_bytes(bytes(data))
    except OSError:
        return None
    return current
