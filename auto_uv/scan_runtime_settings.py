from __future__ import annotations

from dataclasses import dataclass

from stability.q2rtx import Q2RTXStabilityConfig

from .scan_mode.auto_uv_mode import AUTO_UV_MODE_PERFORMANCE, normalize_auto_uv_mode
from .auto_uv_types import AutoUvError
from .auto_uv_user_options import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_METRIC_TUNING,
)


@dataclass(frozen=True, slots=True)
class ScanRuntimeSettings:
    q2rtx_config: Q2RTXStabilityConfig
    auto_uv_mode: str
    final_clock_drop_margin_pct: float
    min_performance_core_clock_pct: float
    preserve_base_below_mv: int | None
    configured_max_drop_pct: float
    final_verification_duration_s: int
    short_probe_base_duration_s: int
    efficiency_stop_streak: int
    min_efficiency_stop_voltage_drop_pct: float
    tail_rise_bins: int
    timedemo_warmup_runs: int


def read_scan_runtime_settings(
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
) -> ScanRuntimeSettings:
    if q2rtx_config.timedemo_loops is None and int(q2rtx_config.duration_s) <= 0:
        raise AutoUvError(
            "auto-UV voltage scan needs either timedemo loops or positive duration"
        )

    auto_uv_mode = normalize_auto_uv_mode(runtime_options.get("auto_uv_mode"))
    final_clock_drop_margin_pct = clock_drop_margin_pct(runtime_options)
    min_performance_core_clock_pct = max(0.0, 100.0 - final_clock_drop_margin_pct)
    preserve_base_below_mv = optional_int(
        runtime_options.get(
            "preserve_base_below_mv",
            runtime_options.get("preserve_vanilla_below_mv"),
        )
    )
    configured_max_drop_pct = max_drop_pct(runtime_options, auto_uv_mode=auto_uv_mode)
    return ScanRuntimeSettings(
        q2rtx_config=q2rtx_config,
        auto_uv_mode=auto_uv_mode,
        final_clock_drop_margin_pct=float(final_clock_drop_margin_pct),
        min_performance_core_clock_pct=float(min_performance_core_clock_pct),
        preserve_base_below_mv=preserve_base_below_mv,
        configured_max_drop_pct=float(configured_max_drop_pct),
        final_verification_duration_s=final_verification_duration_s(runtime_options),
        short_probe_base_duration_s=short_probe_base_duration_s(runtime_options),
        efficiency_stop_streak=efficiency_stop_streak(runtime_options),
        min_efficiency_stop_voltage_drop_pct=min_efficiency_stop_voltage_drop_pct(
            runtime_options
        ),
        tail_rise_bins=tail_rise_bins(runtime_options, auto_uv_mode=auto_uv_mode),
        timedemo_warmup_runs=timedemo_warmup_runs_for_mode(auto_uv_mode),
    )


def clock_drop_margin_pct(runtime_options: dict) -> float:
    value = runtime_options.get("auto_uv_max_clock_drop_pct")
    if value is None:
        value = AUTO_UV_METRIC_TUNING.max_core_clock_drop_pct
    return max(0.0, min(100.0, float(value)))


def max_drop_pct(runtime_options: dict, *, auto_uv_mode: str) -> float:
    _ = auto_uv_mode
    value = runtime_options.get("auto_uv_max_drop_pct")
    if value is None:
        value = AUTO_UV_DEFAULTS.max_drop_pct
    return max(0.0, float(value))


def final_verification_duration_s(runtime_options: dict) -> int:
    value = runtime_options.get(
        "auto_uv_final_seconds",
        AUTO_UV_DEFAULTS.final_duration_s,
    )
    return max(1, int(value or AUTO_UV_DEFAULTS.final_duration_s))


def short_probe_base_duration_s(runtime_options: dict) -> int:
    value = runtime_options.get(
        "auto_uv_short_seconds",
        AUTO_UV_DEFAULTS.probe_duration_s,
    )
    return max(10, min(60, int(value or AUTO_UV_DEFAULTS.probe_duration_s)))


def efficiency_stop_streak(runtime_options: dict) -> int:
    value = runtime_options.get("auto_uv_efficiency_stop_streak")
    if value is None:
        value = AUTO_UV_DEFAULTS.efficiency_stop_streak
    return max(0, int(value))


def min_efficiency_stop_voltage_drop_pct(runtime_options: dict) -> float:
    value = runtime_options.get("auto_uv_min_efficiency_stop_drop_pct")
    if value is None:
        value = AUTO_UV_METRIC_TUNING.min_efficiency_stop_voltage_drop_pct
    return max(0.0, float(value))


def tail_rise_bins(runtime_options: dict, *, auto_uv_mode: str) -> int:
    value = runtime_options.get("auto_uv_tail_rise_bins")
    if value is None:
        value = (
            AUTO_UV_DEFAULTS.performance_tail_rise_bins
            if auto_uv_mode == AUTO_UV_MODE_PERFORMANCE
            else AUTO_UV_DEFAULTS.tail_rise_bins
        )
    return max(0, min(int(AUTO_UV_DEFAULTS.max_tail_rise_bins), int(value)))


def timedemo_warmup_runs_for_mode(auto_uv_mode: str) -> int:
    _ = auto_uv_mode
    return 0


def optional_int(value: object) -> int | None:
    return None if value is None else int(value)
