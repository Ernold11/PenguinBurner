"""Build long stability-test configs for profile verification and Auto-UV final checks.

The helper splits one user duration between Q2RTX benchmark and CUDA companion load.
"""

from __future__ import annotations

from dataclasses import replace

from .constants import DEFAULT_SINGLE_PASS_TIMEOUT_S
from .models import Q2RTXStabilityConfig
from auto_uv.auto_uv_user_options import AUTO_UV_PROBE_TUNING
from auto_uv.q2rtx.q2rtx_cuda_probe_config import cuda_bruteforce_companion_command

LONG_STABILITY_CUDA_RATIO_REFERENCE_S = 30


def long_stability_workload_durations(
    total_duration_s: int,
) -> tuple[int, int]:
    total_s = max(1, int(total_duration_s))
    return _q2rtx_cuda_duration_s(int(total_s))


def _q2rtx_cuda_duration_s(total_duration_s: int) -> tuple[int, int]:
    total_s = max(2, int(total_duration_s))
    cuda_ratio = float(AUTO_UV_PROBE_TUNING.long_cuda_duration_s) / float(
        LONG_STABILITY_CUDA_RATIO_REFERENCE_S * 2
    )
    cuda_s = max(1, int(round(float(total_s) * cuda_ratio)))
    return max(1, int(total_s) - int(cuda_s)), int(cuda_s)


def build_long_stability_test_config(
    config: Q2RTXStabilityConfig,
    *,
    total_duration_s: int,
) -> Q2RTXStabilityConfig:
    q2rtx_s, cuda_s = long_stability_workload_durations(int(total_duration_s))
    companion = cuda_bruteforce_companion_command(
        gpu_index=int(config.gpu_index),
        duration_s=int(cuda_s),
    )
    configured = replace(
        config,
        duration_s=int(q2rtx_s),
        companion_command=companion,
        single_pass_timeout_s=max(
            float(DEFAULT_SINGLE_PASS_TIMEOUT_S),
            float(max(1, int(total_duration_s)))
            + AUTO_UV_PROBE_TUNING.long_timeout_buffer_s,
        ),
    )
    return configured
