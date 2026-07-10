"""Build the optional CUDA companion command used beside Q2RTX."""

from __future__ import annotations

from pathlib import Path
import sys

import stability.cuda_bruteforce as cuda_bruteforce


def cuda_bruteforce_companion_command(
    *,
    gpu_index: int,
    duration_s: int,
) -> tuple[str, ...]:
    return (
        str(sys.executable),
        str(cuda_bruteforce_script_path()),
        "--gpu-index",
        str(int(gpu_index)),
        "--duration-seconds",
        str(max(1, int(duration_s))),
    )


def cuda_bruteforce_script_path() -> Path:
    return Path(cuda_bruteforce.__file__).resolve()
