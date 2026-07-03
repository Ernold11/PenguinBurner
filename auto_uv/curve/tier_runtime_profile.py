"""Final Auto-UV profile shaping for sustained board-power-heavy loads."""

from __future__ import annotations

from auto_uv.domain.types import VfCurveCandidate
from auto_uv.scan_mode.uv_limits import uv_limit_clock_drop_pct_for_gpu

from .vf_curve_flattening import (
    FlatteningRules,
    build_flattened_plan,
    snap_target_clock,
)


_TIER_TAIL_RISE_BINS = {
    "efficiency": 0,
    "balanced": 4,
    "performance": 6,
}


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
    """Lower the final saved profile proportionally for the selected tier.

    The voltage scan finds a stable candidate first. This post-scan shape then
    scales that candidate's lock clock down by the GPU family's preset ratio and
    picks the lowest already-shaped voltage bin that can carry the new clock.
    Final verification still tests the returned profile before it is saved.
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
    shaped_clock_mhz = _proportional_clock_target(
        reference_clock_mhz,
        clock_drop_pct=float(drop_pct),
        rules=rules,
    )
    if shaped_clock_mhz >= reference_clock_mhz:
        return selected_candidate, int(tail_rise_bins)

    shaped_voltage_mv = _lowest_existing_voltage_for_clock(
        selected_candidate.flattened_plan,
        max_voltage_mv=int(selected_candidate.voltage_mv),
        target_clock_mhz=int(shaped_clock_mhz),
    )
    if shaped_voltage_mv is None:
        return selected_candidate, int(tail_rise_bins)

    shaped_tail_rise_bins = tier_tail_rise_bins(
        tier,
        fallback_tail_rise_bins=int(tail_rise_bins),
    )
    shaped_plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(shaped_clock_mhz),
        candidate_voltage_mv=int(shaped_voltage_mv),
        tail_rise_bins=int(shaped_tail_rise_bins),
        rules=rules,
    )
    metadata = dict(selected_candidate.metadata or {})
    metadata.update(
        {
            "profile_runtime_shape": "tier-runtime-shape",
            "profile_runtime_shape_tier": tier,
            "profile_runtime_shape_gpu_name": str(gpu_name or ""),
            "profile_runtime_shape_clock_drop_pct": float(drop_pct),
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
            voltage_mv=int(shaped_voltage_mv),
            target_mhz=int(shaped_clock_mhz),
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


def _lowest_existing_voltage_for_clock(
    plan: list[dict],
    *,
    max_voltage_mv: int,
    target_clock_mhz: int,
) -> int | None:
    eligible = [
        int(point["voltage_mv"])
        for point in plan
        if int(point.get("voltage_mv", 0)) <= int(max_voltage_mv)
        and int(point.get("target_mhz", 0)) >= int(target_clock_mhz)
        and not bool(point.get("preserve_base"))
    ]
    if not eligible:
        return None
    return int(min(eligible))
