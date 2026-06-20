from __future__ import annotations

from dataclasses import dataclass

from stability.q2rtx import Q2RTXStabilityConfig

from .scan_mode.auto_uv_mode import AUTO_UV_MODE_PERFORMANCE, normalize_auto_uv_mode
from .auto_uv_types import AutoUvError
from .auto_uv_user_options import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_METRIC_TUNING,
)
from .scan_mode.uv_limits import uv_limit_eco_to_max_clock_drop_pct_for_gpu


@dataclass(frozen=True, slots=True)
class ScanRuntimeSettings:
    q2rtx_config: Q2RTXStabilityConfig
    auto_uv_mode: str
    final_clock_drop_margin_pct: float
    min_performance_core_clock_pct: float
    configured_min_voltage_mv: int | None
    configured_max_drop_pct: float
    final_verification_duration_s: int
    short_probe_base_duration_s: int
    efficiency_stop_streak: int
    derive_efficiency_stop_streak: bool
    min_efficiency_stop_voltage_drop_pct: float
    tail_rise_bins: int


def read_scan_runtime_settings(
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
    gpu_name: object | None = None,
) -> ScanRuntimeSettings:
    if int(q2rtx_config.duration_s) <= 0:
        raise AutoUvError("auto-UV voltage scan needs positive benchmark duration")

    auto_uv_mode = normalize_auto_uv_mode(runtime_options.get("auto_uv_mode"))
    final_clock_drop_margin_pct = clock_drop_margin_pct(
        runtime_options,
        gpu_name=gpu_name,
    )
    min_performance_core_clock_pct = max(0.0, 100.0 - final_clock_drop_margin_pct)
    configured_max_drop_pct = max_drop_pct(runtime_options)
    return ScanRuntimeSettings(
        q2rtx_config=q2rtx_config,
        auto_uv_mode=auto_uv_mode,
        final_clock_drop_margin_pct=float(final_clock_drop_margin_pct),
        min_performance_core_clock_pct=float(min_performance_core_clock_pct),
        configured_min_voltage_mv=optional_int(
            runtime_options.get("auto_uv_min_voltage_mv")
        ),
        configured_max_drop_pct=float(configured_max_drop_pct),
        final_verification_duration_s=final_verification_duration_s(runtime_options),
        short_probe_base_duration_s=short_probe_base_duration_s(runtime_options),
        efficiency_stop_streak=efficiency_stop_streak(runtime_options),
        derive_efficiency_stop_streak=derive_efficiency_stop_streak(runtime_options),
        min_efficiency_stop_voltage_drop_pct=min_efficiency_stop_voltage_drop_pct(
            runtime_options
        ),
        tail_rise_bins=tail_rise_bins(runtime_options, auto_uv_mode=auto_uv_mode),
    )


def clock_drop_margin_pct(
    runtime_options: dict,
    *,
    gpu_name: object | None = None,
) -> float:
    value = runtime_options.get("auto_uv_max_clock_drop_pct")
    if value is None:
        value = uv_limit_eco_to_max_clock_drop_pct_for_gpu(gpu_name)
    if value is None:
        value = AUTO_UV_DEFAULTS.max_core_clock_drop_pct
    return max(0.0, min(100.0, float(value)))


def max_drop_pct(runtime_options: dict) -> float:
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


def derive_efficiency_stop_streak(runtime_options: dict) -> bool:
    if bool(runtime_options.get("auto_uv_efficiency_stop_streak_explicit")):
        return False
    value = runtime_options.get("auto_uv_efficiency_stop_streak")
    if value is None:
        return True
    try:
        return int(value) == int(AUTO_UV_DEFAULTS.efficiency_stop_streak)
    except (TypeError, ValueError):
        return True


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


def optional_int(value: object) -> int | None:
    return None if value is None else int(value)
