"""Deploy the PenguinBurner NVAPI latency shim into a Proton prefix.

The shim is a drop-in proxy ``nvapi64.dll`` (see ``native/nvapi_shim/``). When
the in-game latency flag is on, the launcher fronts the prefix's system32
``nvapi64.dll`` with it: the real dxvk-nvapi is copied aside to ``nvapi64-pb.dll``
and our shim takes the ``nvapi64.dll`` name. The shim forwards every call to that
sidecar and taps the Reflex latency markers, emitting them to stderr in the
format ``telemetry/nvapi_marker_bridge.py`` parses -- which the launcher already
routes to the marker FIFO. That replaces enabling dxvk-nvapi trace/marker-log:
same marker stream, no fork, and it works under frame generation (the tap is
above vkd3d's owner-gate).

Why system32 and not the game directory: it is fully generic. Every process that
loads ``nvapi64.dll`` -- bootstrapper launchers, UE shipping exes, Streamline's
``sl.interposer`` (which resolves nvapi64 from system32) -- picks up the shim
with zero per-game or per-engine path logic. Re-applied every launch, so a Proton
prefix re-sync (which restores the stock nvapi64.dll) simply self-heals on the
next start.

Falling back is always safe: when this returns ``None`` the launcher keeps the
existing dxvk-nvapi marker-log / trace path.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

# Recognises our own DLL; the banner string is compiled into the shim (see
# native/nvapi_shim/src/nvapi_shim.cpp).
SHIM_NEEDLE = b"[pb-nvapi-shim]"
SHIM_DLL_NAME = "nvapi64.dll"
# The real dxvk-nvapi is parked under this name; the shim forwards to it.
REAL_SIDECAR_NAME = "nvapi64-pb.dll"

# Override the directory (or direct file path) the shim artifact is loaded from.
NVAPI_SHIM_DIR_ENV = "PENGUIN_BURNER_NVAPI_SHIM_DIR"
# Force the shim off even when the in-game latency flag is on (falls back to the
# dxvk-nvapi marker-log / trace path).
NVAPI_SHIM_DISABLE_ENV = "PENGUIN_BURNER_NVAPI_SHIM_DISABLE"

_OVERLAY_ROOT = Path(__file__).resolve().parent
# Packaged location (populated by the build) and the source-checkout build dir.
_PACKAGED_SHIM_DLL = _OVERLAY_ROOT / "nvapi_shim" / SHIM_DLL_NAME
_SOURCE_SHIM_DLL = _OVERLAY_ROOT / "native" / "nvapi_shim" / "build" / SHIM_DLL_NAME

_TRUTHY = {"1", "true", "yes", "on"}


def nvapi_shim_artifact(env: dict[str, str] | None = None) -> Path | None:
    """Return the path to the built shim nvapi64.dll, or None if unavailable."""
    source = os.environ if env is None else env
    candidates: list[Path] = []
    explicit = str(source.get(NVAPI_SHIM_DIR_ENV) or "").strip()
    if explicit:
        base = Path(explicit).expanduser()
        candidates.append(base / SHIM_DLL_NAME)
        candidates.append(base)  # allow pointing straight at the file
    candidates.append(_PACKAGED_SHIM_DLL)
    candidates.append(_SOURCE_SHIM_DLL)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def prefix_system32(env: dict[str, str]) -> Path | None:
    """The running prefix's system32 directory, if it exists."""
    data_path = str(env.get("STEAM_COMPAT_DATA_PATH") or "").strip()
    if not data_path:
        return None
    system32 = (
        Path(data_path).expanduser() / "pfx" / "drive_c" / "windows" / "system32"
    )
    try:
        return system32 if system32.is_dir() else None
    except OSError:
        return None


def _file_contains(path: Path, needle: bytes) -> bool:
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if needle in chunk:
                    return True
    except OSError:
        return False
    return False


def _files_identical(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                chunk_a = a.read(1024 * 1024)
                chunk_b = b.read(1024 * 1024)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except OSError:
        return False


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".pb-tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def deploy_nvapi_shim(env: dict[str, str]) -> Path | None:
    """Front the prefix's system32 nvapi64.dll with the shim. Return its path, or None.

    Idempotent and self-healing:
    - stock nvapi64.dll present -> park it as nvapi64-pb.dll, install the shim;
    - shim already installed -> refresh it if the build changed;
    - prefix re-sync restored the stock DLL -> re-park and re-install next launch.

    Returns None (launcher falls back to dxvk-nvapi marker-log / trace) when the
    shim is disabled or unbuilt, there is no prefix, or system32 is not writable.
    """
    if str(env.get(NVAPI_SHIM_DISABLE_ENV) or "").strip().lower() in _TRUTHY:
        return None

    artifact = nvapi_shim_artifact(env)
    if artifact is None:
        return None

    system32 = prefix_system32(env)
    if system32 is None:
        return None

    nvapi = system32 / SHIM_DLL_NAME
    sidecar = system32 / REAL_SIDECAR_NAME

    try:
        if not nvapi.is_file():
            return None  # nothing to forward to / prefix not ready yet

        if _file_contains(nvapi, SHIM_NEEDLE):
            # Already our shim. Refresh it if the build changed; the sidecar (real
            # dxvk-nvapi) is left as-is.
            if not sidecar.is_file():
                _log(f"nvapi shim: {sidecar.name} missing -- shim has no forward target")
                return None
            if not _files_identical(nvapi, artifact):
                _atomic_copy(artifact, nvapi)
                _log(f"nvapi shim: updated {nvapi}")
            return nvapi

        # nvapi64.dll is the stock dxvk-nvapi (fresh install or post re-sync):
        # park it under the sidecar name, then front it with the shim.
        _atomic_copy(nvapi, sidecar)
        _atomic_copy(artifact, nvapi)
        _log(f"nvapi shim: installed {nvapi} (real -> {sidecar.name})")
        return nvapi
    except OSError as error:
        _log(f"nvapi shim: deploy failed in {system32}: {error}")
        return None


def _log(message: str) -> None:
    # Wrapper stderr here is the inherited console / proton log (the marker FIFO
    # redirect happens later), so this is a human diagnostic, not marker data.
    print(message, file=sys.stderr)
