"""Run nvidia-smi commands with stable text decoding.

The wrapper centralizes subprocess error handling for runtime GPU policy setup.
"""

from __future__ import annotations

import subprocess

from common.penguin_burner_errors import NvmlError
from common.subprocess_locale import stable_subprocess_env


def run_nvidia_smi_command(args, *, executable):
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=stable_subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise NvmlError(f"{executable} not found") from exc

    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0:
        raise NvmlError(
            f"nvidia-smi {' '.join(args)} failed: {output or result.returncode}"
        )
    return output


def apply_gpu_base_policy(
    gpu_index,
    enable_persistence_mode,
    power_limit_w,
    *,
    run_nvidia_smi_fn,
    log,
):
    if enable_persistence_mode:
        output = run_nvidia_smi_fn(["-pm", "1"])
        if output:
            log(output)

    if power_limit_w is not None:
        output = run_nvidia_smi_fn(["-i", str(gpu_index), "-pl", str(power_limit_w)])
        if output:
            log(output)
