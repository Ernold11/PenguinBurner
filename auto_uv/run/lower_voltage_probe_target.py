from __future__ import annotations

from dataclasses import dataclass
import math

from auto_uv.curve.base_vf_curve_voltage_bins import base_target_clock_at_voltage
from auto_uv.shared.probe_data_fields import percent


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
    baseline_core_clock_mhz: float | None = None,
    min_core_clock_pct: float | None = None,
) -> int:
    if stable_measured_target_mhz is not None:
        target_mhz = int(stable_measured_target_mhz)
    else:
        base_target_mhz = base_target_clock_at_voltage(
            base_curve,
            voltage_mv=int(candidate_voltage_mv),
            fallback_mhz=int(stable_target_mhz),
        )
        # Lower voltage naturally descends with the base curve as bins get colder.
        target_mhz = min(int(stable_target_mhz), int(base_target_mhz))

    floor_mhz = scan_clock_floor_mhz(
        baseline_core_clock_mhz=baseline_core_clock_mhz,
        min_core_clock_pct=min_core_clock_pct,
    )
    if floor_mhz is not None:
        target_mhz = max(int(target_mhz), int(floor_mhz))
    return int(target_mhz)


def scan_clock_floor_mhz(
    *,
    baseline_core_clock_mhz: float | None,
    min_core_clock_pct: float | None,
    clock_step_mhz: int = 15,
) -> int | None:
    if baseline_core_clock_mhz is None or min_core_clock_pct is None:
        return None
    baseline = float(baseline_core_clock_mhz)
    if baseline <= 0.0:
        return None
    pct = max(0.0, min(100.0, float(min_core_clock_pct)))
    requested = baseline * percent(pct)
    step = max(1, int(clock_step_mhz))
    return int(math.ceil(requested / float(step)) * step)
