"""Choose the base-load clock that Auto-UV will flatten to.

The selected MHz is based on sustained Q2RTX load, then snapped down to a base-curve clock step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..auto_uv_types import BaseLoadTarget
from .base_load_telemetry import (
    LoadedTelemetryRules,
    decision_samples,
    derive_active_core_clock_mhz,
    derive_power_saturated_clock_mhz,
    sample_values,
    saturated_tail_samples,
)


LIGHT_LOAD_DIAGNOSTIC_BUSY_UTIL_PCT = 60.0
LIGHT_LOAD_DIAGNOSTIC_POWER_LIMIT_PCT = 50.0
IDLE_SELECTED_GPU_DIAGNOSTIC_MAX_UTIL_PCT = 5.0
IDLE_SELECTED_GPU_DIAGNOSTIC_MAX_POWER_LIMIT_PCT = 15.0
IDLE_SELECTED_GPU_DIAGNOSTIC_MAX_CLOCK_MHZ = 600.0


@dataclass(frozen=True, slots=True)
class CurveTiming:
    clock_step_mhz: int = 15


def choose_base_load_flatten_target(
    base_curve: list[dict],
    telemetry_samples: list[Any],
    *,
    power_limit_w: int | None,
    fallback_clock_mhz: float | None,
    curve: CurveTiming = CurveTiming(),
    rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> BaseLoadTarget:
    """Choose the fixed clock used to flatten the base V/F curve under load."""

    tail = saturated_tail_samples(telemetry_samples, rules=rules)
    saturated_clock_mhz, saturated_count, _saturation_floor_w = (
        derive_power_saturated_clock_mhz(
            tail,
            power_limit_w=power_limit_w,
            rules=rules,
        )
    )
    active_avg_clock_mhz, active_preferred_clock_mhz, active_count, _active_floor_w = (
        derive_active_core_clock_mhz(
            tail,
            power_limit_w=power_limit_w,
            use_power_limit_floor=True,
            rules=rules,
        )
    )
    candidates = [
        value
        for value in (
            saturated_clock_mhz,
            active_avg_clock_mhz,
            active_preferred_clock_mhz,
            fallback_clock_mhz,
        )
        if value is not None
    ]
    if not candidates:
        raise ValueError(
            "baseline probe did not report a loaded core clock; "
            + base_load_telemetry_diagnostic(
                telemetry_samples,
                power_limit_w=power_limit_w,
                rules=rules,
            )
        )

    # Pick the lowest credible loaded clock so the flat curve does not chase boost spikes.
    measured_clock_mhz = min(float(value) for value in candidates)
    target_clock_mhz = choose_sustained_curve_clock(
        base_curve,
        measured_clock_mhz,
        clock_step_mhz=int(curve.clock_step_mhz),
    )
    return BaseLoadTarget(
        measured_clock_mhz=float(measured_clock_mhz),
        target_clock_mhz=int(target_clock_mhz),
        active_avg_clock_mhz=active_avg_clock_mhz,
        active_preferred_clock_mhz=active_preferred_clock_mhz,
        saturated_clock_mhz=saturated_clock_mhz,
        fallback_clock_mhz=fallback_clock_mhz,
        active_sample_count=int(active_count),
        saturated_sample_count=int(saturated_count),
    )


def choose_sustained_curve_clock(
    base_curve: list[dict],
    measured_clock_mhz: float,
    *,
    clock_step_mhz: int = 15,
) -> int:
    available = sorted({int(item["target_mhz"]) for item in base_curve})
    if not available:
        raise ValueError("base V/F curve did not contain any target clocks")
    snapped = snap_clock_at_or_below(
        max(1.0, float(measured_clock_mhz)),
        clock_step_mhz=int(clock_step_mhz),
    )
    return int(min(max(int(snapped), min(available)), max(available)))


def snap_clock_at_or_below(clock_mhz: float, *, clock_step_mhz: int = 15) -> int:
    if float(clock_mhz) <= 0.0:
        raise ValueError("clock must be positive")
    step = max(1, int(clock_step_mhz))
    return max(step, int(float(clock_mhz) // float(step)) * step)


def selected_nvidia_light_load_diagnostic(
    telemetry_samples: list[Any],
    *,
    power_limit_w: int | None,
    rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> str | None:
    """Return a non-fatal diagnostic when a busy selected GPU is far below its limit."""

    if power_limit_w is None or int(power_limit_w) <= 0:
        return None
    samples = decision_samples(list(telemetry_samples or []), rules=rules)
    max_power_w = _max_metric(samples, "power_w")
    max_util_pct = _max_metric(samples, "gpu_util_pct")
    if max_power_w is None or max_util_pct is None:
        return None
    if float(max_util_pct) < LIGHT_LOAD_DIAGNOSTIC_BUSY_UTIL_PCT:
        return None

    low_power_floor_w = float(power_limit_w) * (
        LIGHT_LOAD_DIAGNOSTIC_POWER_LIMIT_PCT / 100.0
    )
    if float(max_power_w) >= float(low_power_floor_w):
        return None

    return (
        "warning selected NVIDIA GPU light-load diagnostic: "
        + base_load_telemetry_diagnostic(
            telemetry_samples,
            power_limit_w=power_limit_w,
            rules=rules,
        )
        + f" low_power_floor={_format_metric(low_power_floor_w, 'W')}"
        + " continuing; laptop power policy can legitimately cap load"
    )


def base_load_telemetry_diagnostic(
    telemetry_samples: list[Any],
    *,
    power_limit_w: int | None,
    rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> str:
    samples = list(telemetry_samples or [])
    decision = decision_samples(samples, rules=rules)
    tail = saturated_tail_samples(samples, rules=rules)
    _saturated_clock_mhz, saturated_count, saturated_floor_w = (
        derive_power_saturated_clock_mhz(
            tail,
            power_limit_w=power_limit_w,
            rules=rules,
        )
    )
    _active_avg_clock_mhz, _active_preferred_clock_mhz, active_count, active_floor_w = (
        derive_active_core_clock_mhz(
            tail,
            power_limit_w=power_limit_w,
            use_power_limit_floor=True,
            rules=rules,
        )
    )
    return (
        f"telemetry samples={len(samples)} decision_samples={len(decision)} "
        f"power_limit={_format_power_limit(power_limit_w)} "
        f"max_power={_format_metric(_max_metric(decision, 'power_w'), 'W')} "
        f"avg_power={_format_metric(_avg_metric(decision, 'power_w'), 'W')} "
        f"max_util={_format_metric(_max_metric(decision, 'gpu_util_pct'), '%')} "
        f"max_clock={_format_metric(_max_metric(decision, 'core_clock_mhz'), 'MHz')} "
        f"avg_clock={_format_metric(_avg_metric(decision, 'core_clock_mhz'), 'MHz')} "
        f"active_samples={int(active_count)} "
        f"active_floor={_format_metric(active_floor_w, 'W')} "
        f"saturated_samples={int(saturated_count)} "
        f"saturated_floor={_format_metric(saturated_floor_w, 'W')}"
        f"{_selected_gpu_idle_hint(decision, power_limit_w=power_limit_w)}"
    )


def _selected_gpu_idle_hint(samples: list[Any], *, power_limit_w: int | None) -> str:
    max_util = _max_metric(samples, "gpu_util_pct")
    max_power = _max_metric(samples, "power_w")
    max_clock = _max_metric(samples, "core_clock_mhz")
    if max_util is None or max_power is None or max_clock is None:
        return ""
    if float(max_util) > IDLE_SELECTED_GPU_DIAGNOSTIC_MAX_UTIL_PCT:
        return ""
    if float(max_clock) > IDLE_SELECTED_GPU_DIAGNOSTIC_MAX_CLOCK_MHZ:
        return ""
    if power_limit_w is not None and int(power_limit_w) > 0:
        idle_power_floor = float(power_limit_w) * (
            IDLE_SELECTED_GPU_DIAGNOSTIC_MAX_POWER_LIMIT_PCT / 100.0
        )
        if float(max_power) > idle_power_floor:
            return ""
    return (
        " hint=selected GPU stayed idle while Q2RTX completed; on multi-GPU "
        "systems Q2RTX may be rendering on the display-attached GPU instead. "
        "Select that GPU with --gpu-index."
    )


def _max_metric(samples: list[Any], field_name: str) -> float | None:
    values = sample_values(samples, field_name)
    return max(values) if values else None


def _avg_metric(samples: list[Any], field_name: str) -> float | None:
    values = sample_values(samples, field_name)
    if not values:
        return None
    return sum(values) / len(values)


def _format_power_limit(power_limit_w: int | None) -> str:
    if power_limit_w is None or int(power_limit_w) <= 0:
        return "n/a"
    return f"{int(power_limit_w)}W"


def _format_metric(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f}{unit}"
