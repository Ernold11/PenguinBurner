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


def _base_probe_duration_s(base_duration_s: int | None = None) -> int:
    if base_duration_s is None:
        return int(AUTO_UV_DEFAULTS.probe_duration_s)
    return max(10, int(base_duration_s))


def _tiered_probe_duration_s(
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
    base_duration_s: int | None = None,
) -> int:
    base_probe_duration_s = _base_probe_duration_s(base_duration_s)
    try:
        initial_voltage = int(initial_target_voltage_mv)
        candidate_voltage = int(candidate_voltage_mv)
    except (TypeError, ValueError):
        return int(base_probe_duration_s)
    if initial_voltage <= 0:
        return int(base_probe_duration_s)

    voltage_ratio = float(candidate_voltage) / float(initial_voltage)
    if voltage_ratio >= _percent(AUTO_UV_PROBE_TUNING.high_voltage_pct):
        multiplier = 1
    elif voltage_ratio >= _percent(AUTO_UV_PROBE_TUNING.medium_voltage_pct):
        multiplier = 2
    else:
        multiplier = 3
    return max(1, int(base_probe_duration_s) * int(multiplier))


def _tiered_cuda_duration_s(base_duration_s: int | None = None) -> int:
    base_probe_duration_s = _base_probe_duration_s(base_duration_s)
    return max(
        1,
        int(
            math.ceil(
                float(base_probe_duration_s)
                * float(AUTO_UV_PROBE_TUNING.tiered_cuda_duration_s)
                / float(AUTO_UV_DEFAULTS.probe_duration_s)
            )
        ),
    )


def _budget_tiered_probe_durations(
    target_duration_s: int,
    *,
    cuda_duration_s: int | None = None,
) -> tuple[int, int]:
    total_budget_s = max(1, int(target_duration_s))
    cuda_budget_s = (
        int(AUTO_UV_PROBE_TUNING.tiered_cuda_duration_s)
        if cuda_duration_s is None
        else max(1, int(cuda_duration_s))
    )
    cuda_duration_s = min(
        int(cuda_budget_s),
        max(1, total_budget_s - 1),
    )
    q2rtx_duration_s = max(1, total_budget_s - cuda_duration_s)
    return int(q2rtx_duration_s), int(cuda_duration_s)


def _stability_probe_config_for_voltage_band(
    config: Q2RTXStabilityConfig,
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
    base_duration_s: int | None = None,
) -> Q2RTXStabilityConfig:
    base_probe_duration_s = _base_probe_duration_s(base_duration_s)
    target_duration_s = _tiered_probe_duration_s(
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        candidate_voltage_mv=int(candidate_voltage_mv),
        base_duration_s=int(base_probe_duration_s),
    )
    q2rtx_duration_s = int(target_duration_s)
    cuda_duration_s = _tiered_cuda_duration_s(int(base_probe_duration_s))
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
        AUTO_UV_DEFAULTS.probe_duration_s * 2
    )
    cuda_duration_s = max(1, int(round(float(total_budget_s) * float(cuda_ratio))))
    q2rtx_duration_s = max(1, int(total_budget_s) - int(cuda_duration_s))
    return int(q2rtx_duration_s), int(cuda_duration_s)


def long_stability_workload_durations(
    total_duration_s: int,
    *,
    include_q2rtx: bool = True,
    include_cuda: bool = True,
) -> tuple[int, int]:
    if not include_q2rtx and not include_cuda:
        raise ValueError("at least one stability workload must be enabled")
    total_duration_s = max(1, int(total_duration_s))
    if include_q2rtx and include_cuda:
        return _budget_final_probe_durations(int(total_duration_s))
    if include_q2rtx:
        return int(total_duration_s), 0
    return 0, int(total_duration_s)


def build_long_stability_test_config(
    config: Q2RTXStabilityConfig,
    *,
    total_duration_s: int,
    include_q2rtx: bool = True,
    include_cuda: bool = True,
) -> Q2RTXStabilityConfig:
    if not include_q2rtx and not include_cuda:
        raise ValueError("at least one stability workload must be enabled")
    if include_q2rtx and not include_cuda:
        q2rtx_duration_s, _cuda_duration_s = long_stability_workload_durations(
            int(total_duration_s),
            include_q2rtx=True,
            include_cuda=False,
        )
        return _normalize_probe_config(
            replace(
                config,
                timedemo_loops=None,
                duration_s=int(q2rtx_duration_s),
                companion_command=None,
                single_pass_timeout_s=max(
                    float(config.single_pass_timeout_s),
                    float(q2rtx_duration_s)
                    + AUTO_UV_PROBE_TUNING.long_timeout_buffer_s,
                ),
            )
        )
    if include_cuda and not include_q2rtx:
        _q2rtx_duration_s, cuda_duration_s = long_stability_workload_durations(
            int(total_duration_s),
            include_q2rtx=False,
            include_cuda=True,
        )
        return replace(
            config,
            timedemo_loops=None,
            duration_s=int(cuda_duration_s),
            companion_command=_cuda_bruteforce_companion_command(
                gpu_index=int(config.gpu_index),
                duration_s=int(cuda_duration_s),
            ),
            single_pass_timeout_s=max(
                float(config.single_pass_timeout_s),
                float(cuda_duration_s) + AUTO_UV_PROBE_TUNING.long_timeout_buffer_s,
            ),
        )
    q2rtx_duration_s, cuda_duration_s = long_stability_workload_durations(
        int(total_duration_s),
        include_q2rtx=True,
        include_cuda=True,
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
