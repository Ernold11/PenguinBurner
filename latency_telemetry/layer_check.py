from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

LATENCY_LAYER_NAME = "VK_LAYER_PENGUINBURNER_latency"
DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS = (
    "PENGUIN_BURNER_LATENCY_LAYER=1 "
    "VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency %command%"
)


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

    check_env = dict(os.environ if env is None else env)
    check_env["PENGUIN_BURNER_LATENCY_LAYER"] = "1"

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
        lines.extend(
            [
                "Build-tree check example:",
                "  VK_ADD_IMPLICIT_LAYER_PATH=/path/to/native/build "
                "PENGUIN_BURNER_LATENCY_LAYER=1 vulkaninfo --summary",
            ]
        )

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("Layer warnings:")
        lines.extend(f"  {line}" for line in list(warnings)[:5])

    return "\n".join(lines)
