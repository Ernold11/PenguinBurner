from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .curve.base_vf_curve_voltage_bins import base_target_clock_at_voltage
from .shared.probe_data_fields import percent, read_field


@dataclass(frozen=True, slots=True)
class LowerVoltageProbeTargetRules:
    coarse_voltage_pct: float = 94.0
    medium_voltage_pct: float = 88.0


def lower_voltage_phase(
    *,
    start_voltage_mv: int,
    candidate_voltage_mv: int,
    rules: LowerVoltageProbeTargetRules = LowerVoltageProbeTargetRules(),
) -> str:
    ratio = (
        float(candidate_voltage_mv) / float(start_voltage_mv)
        if int(start_voltage_mv) > 0
        else 1.0
    )
    if ratio > percent(rules.coarse_voltage_pct):
        return "coarse"
    if ratio > percent(rules.medium_voltage_pct):
        return "medium"
    return "fine"


def base_curve_target_for_lower_voltage(
    base_curve: list[dict],
    *,
    candidate_voltage_mv: int,
    stable_target_mhz: int,
    stable_measured_target_mhz: int | None,
) -> int:
    if stable_measured_target_mhz is not None:
        return int(stable_measured_target_mhz)
    base_target_mhz = base_target_clock_at_voltage(
        base_curve,
        voltage_mv=int(candidate_voltage_mv),
        fallback_mhz=int(stable_target_mhz),
    )
    # Lower voltage naturally descends with the base curve as bins get colder.
    return min(int(stable_target_mhz), int(base_target_mhz))


def predict_clock_at_lower_voltage(
    stable_history: list[Any],
    *,
    candidate_voltage_mv: int,
) -> float | None:
    points = [
        (float(read_field(probe, "candidate_voltage_mv")), float(clock_mhz))
        for probe in stable_history[-4:]
        for clock_mhz in [read_field(probe, "avg_core_clock_mhz")]
        if clock_mhz is not None and read_field(probe, "candidate_voltage_mv") is not None
    ]
    if len(points) < 2:
        return None

    first_voltage_mv, first_clock_mhz = points[0]
    last_voltage_mv, last_clock_mhz = points[-1]
    voltage_span_mv = last_voltage_mv - first_voltage_mv
    if abs(voltage_span_mv) <= 0.0:
        return None
    clock_per_mv = (last_clock_mhz - first_clock_mhz) / voltage_span_mv
    return last_clock_mhz + (float(candidate_voltage_mv) - last_voltage_mv) * clock_per_mv


def lower_voltage_clock_floor_miss_reason(
    stable_history: list[Any],
    *,
    candidate_voltage_mv: int,
    baseline_core_clock_mhz: float | None,
    min_core_clock_pct: float,
) -> str | None:
    if baseline_core_clock_mhz is None:
        return None
    predicted_mhz = predict_clock_at_lower_voltage(
        stable_history,
        candidate_voltage_mv=int(candidate_voltage_mv),
    )
    if predicted_mhz is None:
        return None
    floor_mhz = float(baseline_core_clock_mhz) * percent(float(min_core_clock_pct))
    if predicted_mhz >= floor_mhz:
        return None
    return f"predicted={predicted_mhz:.1f}MHz floor={floor_mhz:.1f}MHz"
