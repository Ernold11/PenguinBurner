from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from subprocess_locale import stable_subprocess_env

from .constants import OPENSSL_111_REQUIRED_LIBS
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
    if not missing:
        _emit_dependency_progress(
            progress_callback,
            progress_end_pct,
            "Q2RTX runtime libraries are available",
            executable=str(executable_path),
        )
        return env, None

    missing_set = set(missing)
    openssl_missing = set(OPENSSL_111_REQUIRED_LIBS) & missing_set
    remaining_missing = missing_set - set(OPENSSL_111_REQUIRED_LIBS)
    if remaining_missing:
        raise StabilityTestError(
            "Q2RTX is missing required shared libraries: "
            + ", ".join(sorted(remaining_missing))
        )
    if not openssl_missing:
        raise StabilityTestError(
            "Q2RTX is missing required shared libraries: "
            + ", ".join(sorted(missing_set))
        )

    compat_lib_dir = _ensure_openssl_111_compat_libs(
        show_progress=show_progress,
        progress_callback=progress_callback,
        progress_start_pct=progress_start_pct,
        progress_end_pct=progress_end_pct,
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
