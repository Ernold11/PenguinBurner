"""Final Auto-UV curve-tail shaping for sustained board-power-heavy loads."""

from __future__ import annotations

from auto_uv.domain.types import VfCurveCandidate
from auto_uv.scan_mode.uv_limits import uv_limit_clock_drop_pct_for_gpu

from .vf_curve_flattening import (
    FlatteningRules,
    snap_target_clock,
)


_TIER_TAIL_RISE_BINS = {
    "efficiency": 0,
    "balanced": 4,
    "performance": 6,
}

_SUSTAINED_LOAD_DROP_WINDOW_MV = 65


def build_tier_runtime_profile_candidate(
    base_curve: list[dict],
    *,
    selected_candidate: VfCurveCandidate,
    profile_tier: object | None,
    gpu_name: object | None,
    tail_rise_bins: int,
    clock_drop_pct: float | None = None,
    rules: FlatteningRules = FlatteningRules(),
) -> tuple[VfCurveCandidate, int]:
    """Shape the final saved profile without changing the scan-selected lock.

    The voltage scan finds a stable candidate first. This post-scan shape then
    keeps that lock point intact and pulls the upper voltage tail down toward the
    tier's proportional sustained-load clock. Final verification still tests the
    returned profile before it is saved.
    """

    tier = _normalized_tier(profile_tier)
    drop_pct = clock_drop_pct
    if drop_pct is None:
        drop_pct = uv_limit_clock_drop_pct_for_gpu(gpu_name, profile_id=tier)
    if drop_pct is None or float(drop_pct) <= 0.0:
        return selected_candidate, int(tail_rise_bins)
    if _is_tier_runtime_shaped(selected_candidate):
        return selected_candidate, int(tail_rise_bins)

    reference_clock_mhz = int(selected_candidate.target_mhz)
    sustained_clock_mhz = _proportional_clock_target(
        reference_clock_mhz,
        clock_drop_pct=float(drop_pct),
        rules=rules,
    )
    if sustained_clock_mhz >= reference_clock_mhz:
        return selected_candidate, int(tail_rise_bins)

    shaped_tail_rise_bins = tier_tail_rise_bins(
        tier,
        fallback_tail_rise_bins=int(tail_rise_bins),
    )
    shaped_plan, anchor_voltage_mv = _shape_upper_curve_for_sustained_load(
        selected_candidate.flattened_plan,
        lock_voltage_mv=int(selected_candidate.voltage_mv),
        lock_clock_mhz=int(selected_candidate.target_mhz),
        sustained_clock_mhz=int(sustained_clock_mhz),
        drop_window_mv=int(_SUSTAINED_LOAD_DROP_WINDOW_MV),
        rules=rules,
    )
    metadata = dict(selected_candidate.metadata or {})
    metadata.update(
        {
            "profile_runtime_shape": "tier-runtime-shape",
            "profile_runtime_shape_tier": tier,
            "profile_runtime_shape_gpu_name": str(gpu_name or ""),
            "profile_runtime_shape_clock_drop_pct": float(drop_pct),
            "profile_runtime_shape_sustained_clock_mhz": int(sustained_clock_mhz),
            "profile_runtime_shape_anchor_voltage_mv": int(anchor_voltage_mv),
            "profile_runtime_shape_reference_clock_mhz": int(reference_clock_mhz),
            "profile_runtime_shape_reference_voltage_mv": int(
                selected_candidate.voltage_mv
            ),
            "profile_runtime_shape_reference_tail_rise_bins": int(tail_rise_bins),
            "tail_rise_bins": int(shaped_tail_rise_bins),
        }
    )
    return (
        VfCurveCandidate(
            label=f"{selected_candidate.label}-tier-runtime-shape",
            voltage_mv=int(selected_candidate.voltage_mv),
            target_mhz=int(selected_candidate.target_mhz),
            flattened_plan=shaped_plan,
            metadata=metadata,
        ),
        int(shaped_tail_rise_bins),
    )


def tier_tail_rise_bins(
    profile_tier: object | None,
    *,
    fallback_tail_rise_bins: int,
) -> int:
    tier = _normalized_tier(profile_tier)
    return int(_TIER_TAIL_RISE_BINS.get(tier, int(fallback_tail_rise_bins)))


def _normalized_tier(profile_tier: object | None) -> str:
    tier = str(profile_tier or "efficiency").strip().lower()
    if tier in _TIER_TAIL_RISE_BINS:
        return tier
    return "efficiency"


def _is_tier_runtime_shaped(candidate: VfCurveCandidate) -> bool:
    metadata = dict(candidate.metadata or {})
    return str(metadata.get("profile_runtime_shape") or "") == "tier-runtime-shape"


def _proportional_clock_target(
    reference_clock_mhz: int,
    *,
    clock_drop_pct: float,
    rules: FlatteningRules,
) -> int:
    multiplier = 1.0 - max(0.0, float(clock_drop_pct)) / 100.0
    return snap_target_clock(
        int(round(float(reference_clock_mhz) * multiplier)),
        rules=rules,
    )


def _shape_upper_curve_for_sustained_load(
    plan: list[dict],
    *,
    lock_voltage_mv: int,
    lock_clock_mhz: int,
    sustained_clock_mhz: int,
    drop_window_mv: int,
    rules: FlatteningRules,
) -> tuple[list[dict], int]:
    editable_upper_voltages = [
        int(point["voltage_mv"])
        for point in plan
        if int(point.get("voltage_mv", 0)) > int(lock_voltage_mv)
        and not bool(point.get("preserve_base"))
    ]
    requested_anchor_voltage_mv = int(lock_voltage_mv) + max(1, int(drop_window_mv))
    anchor_voltage_mv = _nearest_voltage_at_or_above(
        editable_upper_voltages,
        requested_voltage_mv=int(requested_anchor_voltage_mv),
    )
    if anchor_voltage_mv is None:
        anchor_voltage_mv = _nearest_voltage_at_or_above(
            [int(point["voltage_mv"]) for point in plan],
            requested_voltage_mv=int(lock_voltage_mv),
        )
    if anchor_voltage_mv is None:
        anchor_voltage_mv = int(lock_voltage_mv)

    shaped_plan = []
    ramp_span_mv = max(1, int(anchor_voltage_mv) - int(lock_voltage_mv))
    for source_point in plan:
        point = dict(source_point)
        voltage_mv = int(point["voltage_mv"])
        base_mhz = int(point["base_mhz"])
        if point.get("preserve_base") or voltage_mv < int(lock_voltage_mv):
            target_mhz = int(point["target_mhz"])
        elif voltage_mv == int(lock_voltage_mv):
            target_mhz = int(lock_clock_mhz)
        elif voltage_mv >= int(anchor_voltage_mv):
            target_mhz = int(sustained_clock_mhz)
        else:
            fraction = (
                float(voltage_mv) - float(lock_voltage_mv)
            ) / float(ramp_span_mv)
            target_mhz = snap_target_clock(
                int(
                    round(
                        float(lock_clock_mhz)
                        + (
                            (float(sustained_clock_mhz) - float(lock_clock_mhz))
                            * fraction
                        )
                    )
                ),
                rules=rules,
            )
            target_mhz = max(
                min(int(lock_clock_mhz), int(sustained_clock_mhz)),
                min(max(int(lock_clock_mhz), int(sustained_clock_mhz)), target_mhz),
            )
        point["target_mhz"] = int(target_mhz)
        point["new_offset_mhz"] = int(target_mhz) - int(base_mhz)
        shaped_plan.append(point)
    return shaped_plan, int(anchor_voltage_mv)


def _nearest_voltage_at_or_above(
    voltages: list[int],
    *,
    requested_voltage_mv: int,
) -> int | None:
    candidates = [
        int(voltage)
        for voltage in voltages
        if int(voltage) >= requested_voltage_mv
    ]
    if candidates:
        return min(candidates)
    if voltages:
        return max(int(voltage) for voltage in voltages)
    return None
