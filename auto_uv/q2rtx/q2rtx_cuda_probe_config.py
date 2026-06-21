from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import sys

import stability.cuda_bruteforce as cuda_bruteforce
from stability.q2rtx.constants import DEFAULT_SINGLE_PASS_TIMEOUT_S
from stability.q2rtx.models import Q2RTXStabilityConfig

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS, AUTO_UV_PROBE_TUNING
from ..shared.probe_data_fields import percent

REFERENCE_DISCOVERY_Q2RTX_DURATION_MULTIPLIER = 2
DEEP_VOLTAGE_Q2RTX_DURATION_MULTIPLIER = 2.5
DEEP_VOLTAGE_CUDA_DURATION_MULTIPLIER = 2


def short_q2rtx_probe_config(
    config: Q2RTXStabilityConfig,
    *,
    target_duration_s: int,
) -> Q2RTXStabilityConfig:
    probe_duration_s = max(1, int(target_duration_s))
    return replace(
        config,
        duration_s=int(probe_duration_s),
        single_pass_timeout_s=max(
            float(DEFAULT_SINGLE_PASS_TIMEOUT_S),
            float(probe_duration_s) + AUTO_UV_PROBE_TUNING.short_timeout_buffer_s,
        ),
    )


def reference_discovery_q2rtx_probe_config(
    config: Q2RTXStabilityConfig,
    *,
    base_duration_s: int | None = None,
) -> Q2RTXStabilityConfig:
    return replace(
        short_q2rtx_probe_config(
            config,
            target_duration_s=reference_discovery_q2rtx_duration_s(
                base_duration_s
            ),
        ),
        companion_command=None,
    )


def reference_discovery_q2rtx_duration_s(base_duration_s: int | None = None) -> int:
    return (
        base_q2rtx_probe_duration_s(base_duration_s)
        * REFERENCE_DISCOVERY_Q2RTX_DURATION_MULTIPLIER
    )


def q2rtx_only_probe_config_for_voltage_band(
    config: Q2RTXStabilityConfig,
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
    base_duration_s: int | None = None,
) -> Q2RTXStabilityConfig:
    q2rtx_duration_s = tiered_q2rtx_probe_duration_s(
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        candidate_voltage_mv=int(candidate_voltage_mv),
        base_duration_s=base_duration_s,
    )
    return replace(
        short_q2rtx_probe_config(
            config,
            target_duration_s=int(q2rtx_duration_s),
        ),
        companion_command=None,
    )


def q2rtx_cuda_probe_config_for_voltage_band(
    config: Q2RTXStabilityConfig,
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
    base_duration_s: int | None = None,
) -> Q2RTXStabilityConfig:
    q2rtx_duration_s = tiered_q2rtx_probe_duration_s(
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        candidate_voltage_mv=int(candidate_voltage_mv),
        base_duration_s=base_duration_s,
    )
    probe_config = short_q2rtx_probe_config(
        config,
        target_duration_s=int(q2rtx_duration_s),
    )
    if not cuda_companion_enabled_for_voltage_band(
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        candidate_voltage_mv=int(candidate_voltage_mv),
    ):
        return replace(probe_config, companion_command=None)
    cuda_duration_s = tiered_cuda_probe_duration_s(
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        candidate_voltage_mv=int(candidate_voltage_mv),
        base_duration_s=base_duration_s,
    )
    return replace(
        probe_config,
        companion_command=cuda_bruteforce_companion_command(
            gpu_index=int(config.gpu_index),
            duration_s=int(cuda_duration_s),
        ),
    )


def cuda_companion_enabled_for_voltage_band(
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
) -> bool:
    try:
        initial_voltage = int(initial_target_voltage_mv)
        candidate_voltage = int(candidate_voltage_mv)
    except (TypeError, ValueError):
        return True
    if initial_voltage <= 0:
        return True
    voltage_ratio = float(candidate_voltage) / float(initial_voltage)
    return voltage_ratio < percent(AUTO_UV_PROBE_TUNING.high_voltage_pct)


def tiered_q2rtx_probe_duration_s(
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
    base_duration_s: int | None = None,
) -> int:
    base_probe_duration_s = base_q2rtx_probe_duration_s(base_duration_s)
    try:
        initial_voltage = int(initial_target_voltage_mv)
        candidate_voltage = int(candidate_voltage_mv)
    except (TypeError, ValueError):
        return int(base_probe_duration_s)
    if initial_voltage <= 0:
        return int(base_probe_duration_s)

    voltage_ratio = float(candidate_voltage) / float(initial_voltage)
    if voltage_ratio >= percent(AUTO_UV_PROBE_TUNING.high_voltage_pct):
        return int(base_probe_duration_s)
    elif voltage_ratio >= percent(AUTO_UV_PROBE_TUNING.medium_voltage_pct):
        return int(base_probe_duration_s) * 2
    else:
        return max(
            1,
            int(
                math.ceil(
                    float(base_probe_duration_s)
                    * float(DEEP_VOLTAGE_Q2RTX_DURATION_MULTIPLIER)
                )
            ),
        )


def tiered_cuda_probe_duration_s(
    *,
    initial_target_voltage_mv: int | None = None,
    candidate_voltage_mv: int | None = None,
    base_duration_s: int | None = None,
) -> int:
    base_probe_duration_s = base_q2rtx_probe_duration_s(base_duration_s)
    cuda_duration_s = max(
        1,
        int(
            math.ceil(
                float(base_probe_duration_s)
                * float(AUTO_UV_PROBE_TUNING.tiered_cuda_duration_s)
                / float(AUTO_UV_DEFAULTS.probe_duration_s)
            )
        ),
    )
    try:
        initial_voltage = int(initial_target_voltage_mv)
        candidate_voltage = int(candidate_voltage_mv)
    except (TypeError, ValueError):
        return int(cuda_duration_s)
    if initial_voltage <= 0:
        return int(cuda_duration_s)
    voltage_ratio = float(candidate_voltage) / float(initial_voltage)
    if voltage_ratio < percent(AUTO_UV_PROBE_TUNING.medium_voltage_pct):
        return max(
            1,
            int(cuda_duration_s) * int(DEEP_VOLTAGE_CUDA_DURATION_MULTIPLIER),
        )
    return int(cuda_duration_s)


def base_q2rtx_probe_duration_s(base_duration_s: int | None = None) -> int:
    if base_duration_s is None:
        return int(AUTO_UV_DEFAULTS.probe_duration_s)
    return max(10, int(base_duration_s))


def cuda_bruteforce_companion_command(
    *,
    gpu_index: int,
    duration_s: int,
) -> tuple[str, ...]:
    script_path = cuda_bruteforce_script_path()
    return (
        str(sys.executable),
        str(script_path),
        "--gpu-index",
        str(int(gpu_index)),
        "--duration-seconds",
        str(max(1, int(duration_s))),
    )


def cuda_bruteforce_script_path() -> Path:
    return Path(cuda_bruteforce.__file__).resolve()
