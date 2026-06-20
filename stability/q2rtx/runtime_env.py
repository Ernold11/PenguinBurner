from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from common.subprocess_locale import stable_subprocess_env

from .models import StabilityTestError
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


def _prepare_q2rtx_runtime_env(
    executable_path: Path,
    *,
    show_progress: bool,
    progress_callback: DependencyProgressCallback | None = None,
    progress_start_pct: float = 85.0,
    progress_end_pct: float = 98.0,
) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("SDL_VIDEO_ALLOW_SCREENSAVER", "1")

    missing = _detect_missing_shared_libraries(executable_path, env=env)
    if missing:
        raise StabilityTestError(
            "Q2RTX is missing required shared libraries: "
            + ", ".join(sorted(set(missing)))
        )
    _emit_dependency_progress(
        progress_callback,
        progress_end_pct,
        "Q2RTX runtime libraries are available",
        executable=str(executable_path),
    )
    return env
