from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from common.penguin_burner_paths import claim_desktop_user_ownership
from common.subprocess_locale import stable_subprocess_env

from .constants import OPENSSL_111_REQUIRED_LIBS
from .elf_runpath import ORIGIN_RUNPATH, patch_runpath_to_origin
from .models import StabilityTestError
from .openssl_compat import _ensure_openssl_111_compat_libs
from .progress import DependencyProgressCallback, _emit_dependency_progress


def _detect_missing_shared_libraries(
    executable_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    ldd_path = shutil.which("ldd")
    if not ldd_path:
        return []
    try:
        result = subprocess.run(
            [ldd_path, str(executable_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(env),
        )
    except OSError:
        return []

    missing: list[str] = []
    for line in result.stdout.splitlines():
        if "=> not found" not in line:
            continue
        name = line.split("=>", 1)[0].strip()
        if name:
            missing.append(name)
    return missing


def _prepend_library_path(env: dict[str, str], lib_dir: Path) -> dict[str, str]:
    updated = dict(env)
    existing = updated.get("LD_LIBRARY_PATH", "").strip()
    if existing:
        updated["LD_LIBRARY_PATH"] = f"{lib_dir}:{existing}"
    else:
        updated["LD_LIBRARY_PATH"] = str(lib_dir)
    return updated


def _colocate_runpath_libs(
    executable_path: Path,
    compat_lib_dir: Path,
) -> str | None:
    """Stage the OpenSSL 1.1 libs next to the binary and point its RUNPATH there.

    NVIDIA's Q2RTX records a non-relocatable RUNPATH (``/mnt/q2rtx/.``), so the
    loader can only find libssl.so.1.1 via ``LD_LIBRARY_PATH`` -- which is stripped
    when q2rtx is launched through a capability-carrying gamescope (AT_SECURE).
    Copying the libs beside the binary and rewriting RUNPATH to ``$ORIGIN`` makes
    the dependency resolve from the ELF itself, which survives that launch path.

    Returns the previous RUNPATH when it was actually rewritten, else None.
    """
    binary_dir = executable_path.parent
    for name in OPENSSL_111_REQUIRED_LIBS:
        source = compat_lib_dir / name
        if not source.is_file():
            return None
        destination = binary_dir / name
        try:
            if (
                destination.is_file()
                and destination.stat().st_size == source.stat().st_size
            ):
                continue  # already staged this run; avoid a redundant copy
            shutil.copy2(source, destination)  # follows symlinks -> real lib
            claim_desktop_user_ownership(destination)
        except OSError:
            return None
    previous_runpath = patch_runpath_to_origin(executable_path)
    if previous_runpath in (None, ORIGIN_RUNPATH):
        return None
    return previous_runpath


def _prepare_q2rtx_runtime_env(
    executable_path: Path,
    *,
    show_progress: bool,
    progress_callback: DependencyProgressCallback | None = None,
    progress_start_pct: float = 85.0,
    progress_end_pct: float = 98.0,
) -> tuple[dict[str, str], Path | None]:
    env = dict(os.environ)
    env.setdefault("SDL_VIDEO_ALLOW_SCREENSAVER", "1")

    missing = _detect_missing_shared_libraries(executable_path, env=env)
    remaining_missing = set(missing) - set(OPENSSL_111_REQUIRED_LIBS)
    if remaining_missing:
        raise StabilityTestError(
            "Q2RTX is missing required shared libraries: "
            + ", ".join(sorted(remaining_missing))
        )
    openssl_missing = bool(set(OPENSSL_111_REQUIRED_LIBS) & set(missing))

    # Always bundle our own OpenSSL 1.1 next to the binary and point RUNPATH at
    # $ORIGIN, so q2rtx resolves libssl/libcrypto from its own directory on every
    # launch -- independent of the system libraries and of the ldd probe, which
    # lies for privilege-dropped, gamescope-wrapped launches (AT_SECURE).
    try:
        compat_lib_dir = _ensure_openssl_111_compat_libs(
            show_progress=show_progress,
            progress_callback=progress_callback,
            progress_start_pct=progress_start_pct,
            progress_end_pct=progress_end_pct,
        )
    except StabilityTestError:
        if openssl_missing:
            raise
        # System already provides OpenSSL 1.1 and we could not stage our own copy;
        # the binary still resolves the libraries from the system.
        _emit_dependency_progress(
            progress_callback,
            progress_end_pct,
            "Q2RTX runtime libraries are available",
            executable=str(executable_path),
        )
        return env, None

    previous_runpath = _colocate_runpath_libs(executable_path, compat_lib_dir)
    if previous_runpath is not None and show_progress:
        print(
            f"Q2RTX RUNPATH set to {ORIGIN_RUNPATH} (was {previous_runpath}); "
            f"bundled OpenSSL 1.1 libs next to {executable_path.name}",
            flush=True,
        )
    env = _prepend_library_path(env, compat_lib_dir)
    compat_root = compat_lib_dir.parent
    compat_conf = compat_root / "ssl" / "openssl11.cnf"
    compat_engines = compat_root / "engines-1.1"
    if compat_conf.is_file():
        env["OPENSSL_CONF"] = str(compat_conf)
    if compat_engines.is_dir():
        env["OPENSSL_ENGINES"] = str(compat_engines)
    missing_after = _detect_missing_shared_libraries(executable_path, env=env)
    if missing_after:
        raise StabilityTestError(
            "Q2RTX is still missing shared libraries after installing compatibility libs: "
            + ", ".join(sorted(missing_after))
        )
    return env, compat_lib_dir
