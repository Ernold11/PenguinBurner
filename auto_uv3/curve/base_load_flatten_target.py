"""Choose the base-load clock that Auto-UV will flatten to.

The selected MHz is based on sustained Q2RTX load, then snapped down to a base-curve clock step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..auto_uv_types import BaseLoadTarget
from .base_load_telemetry import (
    LoadedTelemetryRules,
    derive_active_core_clock_mhz,
    derive_power_saturated_clock_mhz,
    saturated_tail_samples,
)


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
        raise ValueError("baseline probe did not report a loaded core clock")

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
