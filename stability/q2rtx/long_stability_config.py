"""Build long stability-test configs for profile verification and Auto-UV final checks.

The helper splits one user duration between Q2RTX timedemo and CUDA companion load.
"""

from __future__ import annotations

from dataclasses import replace

from .models import Q2RTXStabilityConfig
from auto_uv3.auto_uv_user_options import AUTO_UV_PROBE_TUNING
from auto_uv3.q2rtx.q2rtx_cuda_probe_config import cuda_bruteforce_companion_command

LONG_STABILITY_CUDA_RATIO_REFERENCE_S = 30


def long_stability_workload_durations(
    total_duration_s: int,
    *,
    include_q2rtx: bool = True,
    include_cuda: bool = True,
) -> tuple[int, int]:
    if not include_q2rtx and not include_cuda:
        raise ValueError("at least one stability workload must be enabled")
    total_s = max(1, int(total_duration_s))
    if include_q2rtx and include_cuda:
        return _q2rtx_cuda_duration_s(int(total_s))
    if include_q2rtx:
        return int(total_s), 0
    return 0, int(total_s)


def _q2rtx_cuda_duration_s(total_duration_s: int) -> tuple[int, int]:
    total_s = max(2, int(total_duration_s))
    cuda_ratio = float(AUTO_UV_PROBE_TUNING.tiered_cuda_duration_s) / float(
        LONG_STABILITY_CUDA_RATIO_REFERENCE_S * 2
    )
    cuda_s = max(1, int(round(float(total_s) * cuda_ratio)))
    return max(1, int(total_s) - int(cuda_s)), int(cuda_s)


def build_long_stability_test_config(
    config: Q2RTXStabilityConfig,
    *,
    total_duration_s: int,
    include_q2rtx: bool = True,
    include_cuda: bool = True,
) -> Q2RTXStabilityConfig:
    if not include_q2rtx and not include_cuda:
        raise ValueError("at least one stability workload must be enabled")
    q2rtx_s, cuda_s = long_stability_workload_durations(
        int(total_duration_s),
        include_q2rtx=bool(include_q2rtx),
        include_cuda=bool(include_cuda),
    )
    companion = (
        cuda_bruteforce_companion_command(
            gpu_index=int(config.gpu_index),
            duration_s=int(cuda_s),
        )
        if include_cuda
        else None
    )
    configured = replace(
        config,
        timedemo_loops=None,
        duration_s=int(q2rtx_s if include_q2rtx else cuda_s),
        companion_command=companion,
        single_pass_timeout_s=max(
            float(config.single_pass_timeout_s),
            float(max(1, int(total_duration_s)))
            + AUTO_UV_PROBE_TUNING.long_timeout_buffer_s,
        ),
    )
    return configured
