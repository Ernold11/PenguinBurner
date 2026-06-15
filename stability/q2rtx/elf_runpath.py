"""Make the bundled Q2RTX binary find its OpenSSL 1.1 libs without LD_LIBRARY_PATH.

NVIDIA's Q2RTX Linux build bakes a non-relocatable RUNPATH pointing at their build
machine (``/mnt/q2rtx/.``), so on an end-user system the loader cannot find the
bundled ``libssl.so.1.1`` / ``libcrypto.so.1.1`` and falls back to
``LD_LIBRARY_PATH`` -- which the loader strips when q2rtx is launched through a
capability-carrying gamescope (``AT_SECURE``).

Rewriting that one known RUNPATH string in place to ``$ORIGIN`` makes the loader
look next to the binary, honored even under ``AT_SECURE`` and needing no patchelf.
The replacement is NUL-padded to the original length, so no ``.dynstr`` offsets
move; the matching OpenSSL libs are copied next to the binary by the caller.
"""

from __future__ import annotations

from pathlib import Path

ORIGIN_RUNPATH = "$ORIGIN"
_BUILD_RUNPATH = b"/mnt/q2rtx/.\x00"  # RUNPATH string NVIDIA ships in the q2rtx ELF


def patch_runpath_to_origin(binary_path: Path) -> str | None:
    """Rewrite the binary's vendored RUNPATH to ``$ORIGIN`` in place.

    Returns the previous RUNPATH when it was rewritten, ``"$ORIGIN"`` if it was
    already patched, or None if the known build RUNPATH was not present (the caller
    then keeps relying on ``LD_LIBRARY_PATH``).
    """
    try:
        data = binary_path.read_bytes()
    except OSError:
        return None
    if _BUILD_RUNPATH not in data:
        return ORIGIN_RUNPATH if b"$ORIGIN\x00" in data else None
    # Same-length, NUL-padded replacement keeps every .dynstr offset valid.
    replacement = b"$ORIGIN\x00".ljust(len(_BUILD_RUNPATH), b"\x00")
    try:
        binary_path.write_bytes(data.replace(_BUILD_RUNPATH, replacement, 1))
    except OSError:
        return None
    return _BUILD_RUNPATH[:-1].decode("ascii")
