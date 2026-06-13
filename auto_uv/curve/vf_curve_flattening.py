from __future__ import annotations

from dataclasses import dataclass

from .rising_tail import (
    normalize_tail_rise_bins,
    rising_tail_targets,
    tail_ceiling_clock_mhz,
)


@dataclass(frozen=True, slots=True)
class FlatteningRules:
    clock_step_mhz: int = 15
    flatten_ramp_window_mv: int = 40


def build_flatten_target(
    base_curve: list[dict],
    *,
    lock_clock_mhz: int,
    lock_voltage_mv: int,
    ceiling_clock_mhz: int | None = None,
    tail_rise_bins: int = 0,
) -> dict:
    voltages = sorted({int(point["voltage_mv"]) for point in base_curve})
    end_voltage_mv = voltages[-1] if voltages else int(lock_voltage_mv)
    tail_point_count = sum(
        1 for point in base_curve if int(point["voltage_mv"]) >= int(lock_voltage_mv)
    )
    target = {
        "baseline_measurement": "q2rtx-loaded-telemetry",
        "lock_clock_mhz": int(lock_clock_mhz),
        "lock_voltage_mv": int(lock_voltage_mv),
        "end_voltage_mv": int(end_voltage_mv),
        "tail_point_count": int(tail_point_count),
        "tail_rise_bins": normalize_tail_rise_bins(tail_rise_bins),
    }
    if ceiling_clock_mhz is not None and int(ceiling_clock_mhz) > int(lock_clock_mhz):
        target["ceiling_clock_mhz"] = int(ceiling_clock_mhz)
    return target


def build_flatten_target_for_plan(
    base_curve: list[dict],
    plan: list[dict],
    *,
    lock_clock_mhz: int,
    lock_voltage_mv: int,
    tail_rise_bins: int = 0,
) -> dict:
    return build_flatten_target(
        base_curve,
        lock_clock_mhz=int(lock_clock_mhz),
        lock_voltage_mv=int(lock_voltage_mv),
        ceiling_clock_mhz=tail_ceiling_clock_mhz(
            plan,
            fallback_clock_mhz=int(lock_clock_mhz),
            lock_voltage_mv=int(lock_voltage_mv),
        ),
        tail_rise_bins=int(tail_rise_bins),
    )


def build_flattened_plan(
    base_curve: list[dict],
    *,
    lock_clock_mhz: int,
    candidate_voltage_mv: int,
    below_lock_gap_mhz: int | None = None,
    tail_rise_bins: int = 0,
    rules: FlatteningRules = FlatteningRules(),
) -> list[dict]:
    editable_voltages = {
        int(point["voltage_mv"])
        for point in base_curve
        if not point.get("preserve_base")
    }
    if int(candidate_voltage_mv) not in editable_voltages:
        raise ValueError(f"{int(candidate_voltage_mv)}mV is not an editable VF bin")

    flattened_clock_mhz = int(lock_clock_mhz)
    below_gap = (
        int(below_lock_gap_mhz)
        if below_lock_gap_mhz is not None
        else int(rules.clock_step_mhz)
    )
    below_plateau_cap_mhz = max(
        int(rules.clock_step_mhz),
        int(flattened_clock_mhz) - max(int(rules.clock_step_mhz), int(below_gap)),
    )
    min_voltage_mv = min(editable_voltages)
    ramp_start_voltage_mv = max(
        int(min_voltage_mv),
        int(candidate_voltage_mv) - int(rules.flatten_ramp_window_mv),
    )
    ramp_span_mv = max(1, int(candidate_voltage_mv) - int(ramp_start_voltage_mv))
    tail_targets = rising_tail_targets(
        base_curve,
        lock_clock_mhz=int(flattened_clock_mhz),
        candidate_voltage_mv=int(candidate_voltage_mv),
        tail_rise_bins=int(tail_rise_bins),
        clock_step_mhz=int(rules.clock_step_mhz),
    )
    plan = []
    for base_point in base_curve:
        point = dict(base_point)
        voltage_mv = int(point["voltage_mv"])
        base_mhz = int(point["base_mhz"])
        original_target_mhz = int(point["target_mhz"])
        if point.get("preserve_base"):
            target_mhz = int(base_mhz)
        elif voltage_mv >= int(candidate_voltage_mv):
            target_mhz = int(tail_targets.get(voltage_mv, flattened_clock_mhz))
        elif voltage_mv <= int(ramp_start_voltage_mv):
            target_mhz = int(original_target_mhz)
        else:
            fraction = max(
                0.0,
                min(
                    1.0,
                    (float(voltage_mv) - float(ramp_start_voltage_mv))
                    / float(ramp_span_mv),
                ),
            )
            interpolated_mhz = original_target_mhz + (
                (float(flattened_clock_mhz) - float(original_target_mhz)) * fraction
            )
            target_mhz = min(
                int(flattened_clock_mhz),
                max(
                    int(original_target_mhz),
                    snap_target_clock(int(round(interpolated_mhz)), rules=rules),
                ),
            )
        if not point.get("preserve_base") and voltage_mv < int(candidate_voltage_mv):
            target_mhz = min(int(target_mhz), int(below_plateau_cap_mhz))
        point["target_mhz"] = int(target_mhz)
        point["new_offset_mhz"] = int(target_mhz) - int(base_mhz)
        plan.append(point)
    return plan


def snap_target_clock(requested_clock_mhz: int, *, rules: FlatteningRules) -> int:
    requested = int(requested_clock_mhz)
    if requested <= 0:
        raise ValueError("requested target clock must be positive")
    step = max(1, int(rules.clock_step_mhz))
    return int(round(float(requested) / float(step)) * step)
