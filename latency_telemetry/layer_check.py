from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable

from penguin_burner_overlay.native_layer import LATENCY_LAYER_NAME
from penguin_burner_overlay.native_layer import native_layer_dirs

DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS = "PENGUIN_BURNER %command%"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_LAYER_DIR = _REPO_ROOT / "native" / "latency_layer" / "build"


def check_latency_layer(
    *,
    env: dict[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    vulkaninfo = which("vulkaninfo")
    if not vulkaninfo:
        return {
            "ok": False,
            "layer_name": LATENCY_LAYER_NAME,
            "reason": "vulkaninfo-not-found",
            "launch_options": DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS,
            "returncode": None,
        }

    check_env = _latency_layer_check_env(dict(os.environ if env is None else env))

    try:
        completed = run(
            [vulkaninfo, "--summary"],
            capture_output=True,
            text=True,
            timeout=20,
            env=check_env,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "layer_name": LATENCY_LAYER_NAME,
            "reason": f"vulkaninfo-failed: {exc}",
            "launch_options": DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS,
            "returncode": None,
        }

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = f"{stdout}\n{stderr}"
    layer_listed = LATENCY_LAYER_NAME in combined
    warnings = [
        line
        for line in combined.splitlines()
        if ("WARNING" in line or "ERROR" in line) and "PENGUINBURNER" in line
    ]

    return {
        "ok": layer_listed,
        "layer_name": LATENCY_LAYER_NAME,
        "reason": "" if layer_listed else "layer-not-listed",
        "launch_options": DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS,
        "returncode": completed.returncode,
        "warnings": warnings,
    }


def format_latency_layer_check(result: dict[str, object]) -> str:
    ok = bool(result.get("ok"))
    layer_name = str(result.get("layer_name") or LATENCY_LAYER_NAME)
    reason = str(result.get("reason") or "")
    launch_options = str(
        result.get("launch_options") or DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS
    )

    lines = [
        f"PenguinBurner latency layer: {'found' if ok else 'not found'}",
        f"Layer: {layer_name}",
        "Steam launch options:",
        f"  {launch_options}",
    ]
    if reason and not ok:
        lines.append(f"Reason: {reason}")
    if not ok:
        layer_dirs = native_layer_dirs(source_build_dir=_BUILD_LAYER_DIR)
        layer_dir = layer_dirs[0] if layer_dirs else _BUILD_LAYER_DIR
        lines.extend(
            [
                "Layer check example:",
                f"  VK_ADD_IMPLICIT_LAYER_PATH={layer_dir} "
                "PENGUIN_BURNER_LATENCY_LAYER=1 vulkaninfo --summary",
            ]
        )

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("Layer warnings:")
        lines.extend(f"  {line}" for line in list(warnings)[:5])

    return "\n".join(lines)


def _latency_layer_check_env(env: dict[str, str]) -> dict[str, str]:
    env["PENGUIN_BURNER_LATENCY_LAYER"] = "1"
    layer_dirs = native_layer_dirs(env, source_build_dir=_BUILD_LAYER_DIR)
    if layer_dirs:
        _prepend_env_entries(
            env,
            "VK_ADD_IMPLICIT_LAYER_PATH",
            [str(path) for path in layer_dirs],
            separator=":",
        )
        _prepend_env_entries(
            env,
            "VK_LOADER_LAYERS_ENABLE",
            [LATENCY_LAYER_NAME],
            separator=",",
        )
    return env


def _prepend_env_entries(
    env: dict[str, str],
    key: str,
    entries: list[str],
    *,
    separator: str,
) -> None:
    existing = [item for item in str(env.get(key) or "").split(separator) if item]
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item not in merged:
            merged.append(item)
    if merged:
        env[key] = separator.join(merged)
