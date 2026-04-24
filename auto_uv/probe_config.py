from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import sys

from stability.q2rtx import (
    DEFAULT_DEMO_NAME,
    KNOWN_TIMEDEMO_RUN_SECONDS_HINTS,
    Q2RTXStabilityConfig,
)

from .tuning import AUTO_UV_DEFAULTS, AUTO_UV_PROBE_TUNING


def _percent(value: float | int) -> float:
    return max(0.0, float(value) / 100.0)


def _short_probe_config(
    config: Q2RTXStabilityConfig,
    *,
    target_duration_s: int,
) -> Q2RTXStabilityConfig:
    probe_duration_s = max(1, int(target_duration_s))
    demo_name = str(config.demo_name).strip().lower()
    hinted_demo_name = (
        "q2demo1" if demo_name == str(DEFAULT_DEMO_NAME).lower() else demo_name
    )
    hinted_seconds = KNOWN_TIMEDEMO_RUN_SECONDS_HINTS.get(hinted_demo_name)
    if hinted_seconds is not None and float(hinted_seconds) > 0.0:
        timedemo_loops = max(
            1, int(math.ceil(probe_duration_s / float(hinted_seconds)))
        )
        return replace(
            config,
            timedemo_loops=int(timedemo_loops),
            duration_s=0,
            single_pass_timeout_s=max(
                float(config.single_pass_timeout_s),
                float(timedemo_loops)
                * float(hinted_seconds)
                * AUTO_UV_PROBE_TUNING.timeout_multiplier
                + AUTO_UV_PROBE_TUNING.short_timeout_buffer_s,
            ),
        )
    return replace(
        config,
        timedemo_loops=None,
        duration_s=int(probe_duration_s),
        single_pass_timeout_s=max(
            float(config.single_pass_timeout_s),
            float(probe_duration_s) + AUTO_UV_PROBE_TUNING.short_timeout_buffer_s,
        ),
    )


def _normalize_probe_config(config: Q2RTXStabilityConfig) -> Q2RTXStabilityConfig:
    if config.timedemo_loops is not None:
        return config
    probe_duration_s = int(config.duration_s)
    if probe_duration_s <= 0:
        return config
    demo_name = str(config.demo_name).strip().lower()
    hinted_demo_name = (
        "q2demo1" if demo_name == str(DEFAULT_DEMO_NAME).lower() else demo_name
    )
    hinted_seconds = KNOWN_TIMEDEMO_RUN_SECONDS_HINTS.get(hinted_demo_name)
    if hinted_seconds is None or float(hinted_seconds) <= 0.0:
        return config
    timedemo_loops = max(
        1, int(math.ceil(float(probe_duration_s) / float(hinted_seconds)))
    )
    return replace(
        config,
        timedemo_loops=int(timedemo_loops),
        duration_s=0,
        single_pass_timeout_s=max(
            float(config.single_pass_timeout_s),
            float(timedemo_loops)
            * float(hinted_seconds)
            * AUTO_UV_PROBE_TUNING.timeout_multiplier
            + AUTO_UV_PROBE_TUNING.short_timeout_buffer_s,
        ),
    )


def _cuda_bruteforce_companion_command(
    *, gpu_index: int, duration_s: int
) -> tuple[str, ...]:
    script_path = (
        Path(__file__).resolve().parent.parent / "stability" / "cuda_bruteforce.py"
    )
    return (
        str(sys.executable),
        str(script_path),
        "--gpu-index",
        str(int(gpu_index)),
        "--duration-seconds",
        str(max(1, int(duration_s))),
    )


def _budget_tiered_probe_durations(target_duration_s: int) -> tuple[int, int]:
    total_budget_s = max(
        1,
        min(int(target_duration_s), int(AUTO_UV_PROBE_TUNING.max_tiered_probe_total_s)),
    )
    cuda_duration_s = min(
        int(AUTO_UV_PROBE_TUNING.tiered_cuda_duration_s),
        max(1, total_budget_s - 1),
    )
    q2rtx_duration_s = max(1, total_budget_s - cuda_duration_s)
    return int(q2rtx_duration_s), int(cuda_duration_s)


def _stability_probe_config_for_voltage_band(
    config: Q2RTXStabilityConfig,
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
) -> Q2RTXStabilityConfig:
    if int(initial_target_voltage_mv) <= 0:
        return _short_probe_config(
            config,
            target_duration_s=AUTO_UV_DEFAULTS.probe_duration_s,
        )
    voltage_ratio = float(candidate_voltage_mv) / float(initial_target_voltage_mv)
    if voltage_ratio >= _percent(AUTO_UV_PROBE_TUNING.high_voltage_pct):
        target_duration_s = AUTO_UV_PROBE_TUNING.high_voltage_duration_s
    elif voltage_ratio >= _percent(AUTO_UV_PROBE_TUNING.medium_voltage_pct):
        target_duration_s = AUTO_UV_PROBE_TUNING.medium_voltage_duration_s
    else:
        target_duration_s = AUTO_UV_PROBE_TUNING.low_voltage_duration_s
    q2rtx_duration_s, cuda_duration_s = _budget_tiered_probe_durations(
        int(target_duration_s)
    )
    probe_config = _short_probe_config(
        config,
        target_duration_s=int(q2rtx_duration_s),
    )
    companion_command = _cuda_bruteforce_companion_command(
        gpu_index=int(config.gpu_index),
        duration_s=int(cuda_duration_s),
    )
    return replace(
        probe_config,
        companion_command=companion_command,
    )


def _budget_final_probe_durations(target_duration_s: int) -> tuple[int, int]:
    total_budget_s = max(2, int(target_duration_s))
    cuda_ratio = float(AUTO_UV_PROBE_TUNING.tiered_cuda_duration_s) / float(
        AUTO_UV_PROBE_TUNING.max_tiered_probe_total_s
    )
    cuda_duration_s = max(1, int(round(float(total_budget_s) * float(cuda_ratio))))
    q2rtx_duration_s = max(1, int(total_budget_s) - int(cuda_duration_s))
    return int(q2rtx_duration_s), int(cuda_duration_s)


def build_long_stability_test_config(
    config: Q2RTXStabilityConfig,
    *,
    total_duration_s: int,
) -> Q2RTXStabilityConfig:
    q2rtx_duration_s, cuda_duration_s = _budget_final_probe_durations(
        int(total_duration_s)
    )
    return _normalize_probe_config(
        replace(
            config,
            timedemo_loops=None,
            duration_s=int(q2rtx_duration_s),
            companion_command=_cuda_bruteforce_companion_command(
                gpu_index=int(config.gpu_index),
                duration_s=int(cuda_duration_s),
            ),
            single_pass_timeout_s=max(
                float(config.single_pass_timeout_s),
                float(total_duration_s) + AUTO_UV_PROBE_TUNING.long_timeout_buffer_s,
            ),
        )
    )
