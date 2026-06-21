"""Build the long Q2RTX+CUDA workload used only for final verification.

The total user-selected duration is split between Q2RTX benchmark and CUDA companion load.
"""

from __future__ import annotations

from dataclasses import replace

from auto_uv.stability.q2rtx import DEFAULT_SINGLE_PASS_TIMEOUT_S, Q2RTXStabilityConfig

from ..auto_uv_user_options import AUTO_UV_PROBE_TUNING
from ..q2rtx.q2rtx_cuda_probe_config import cuda_bruteforce_companion_command

FINAL_VERIFICATION_CUDA_RATIO_REFERENCE_S = 30


def final_q2rtx_cuda_duration_s(total_duration_s: int) -> tuple[int, int]:
    total_s = max(2, int(total_duration_s))
    cuda_ratio = float(AUTO_UV_PROBE_TUNING.long_cuda_duration_s) / float(
        FINAL_VERIFICATION_CUDA_RATIO_REFERENCE_S * 2
    )
    cuda_s = max(1, int(round(float(total_s) * cuda_ratio)))
    return max(1, int(total_s) - int(cuda_s)), int(cuda_s)


def final_q2rtx_cuda_probe_config(
    config: Q2RTXStabilityConfig,
    *,
    total_duration_s: int,
) -> Q2RTXStabilityConfig:
    q2rtx_s, cuda_s = final_q2rtx_cuda_duration_s(int(total_duration_s))
    return replace(
        config,
        duration_s=int(q2rtx_s),
        companion_command=cuda_bruteforce_companion_command(
            gpu_index=int(config.gpu_index),
            duration_s=int(cuda_s),
        ),
        single_pass_timeout_s=max(
            float(DEFAULT_SINGLE_PASS_TIMEOUT_S),
            float(total_duration_s) + AUTO_UV_PROBE_TUNING.long_timeout_buffer_s,
        ),
    )
