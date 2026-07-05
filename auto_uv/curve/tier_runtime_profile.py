"""Final Auto-UV below-lock curve shaping for sustained board-power-heavy loads.

The scan proves one lock point. Under a sustained power-heavy load
(FurMark-class) the power limiter walks the card DOWN the curve into
below-lock bins, and stock below-lock bins carry no undervolt at all, so the
throttled clocks pay full stock voltage. This shape re-applies the lock's
proven voltage offset to every bin below the lock: each bin gets the stock
clock found `offset` millivolts above it, with the offset scaled by the bin's
own voltage so the implied per-millivolt undervolt never exceeds what the
scan verified at the lock. The shape only ever RAISES a bin above the scan
plan's own value, stays a full clock step below the lock clock, and bottoms
out at the tier's allowed clock drop; below that floor the shift tapers to
zero across one ramp window so the curve rejoins the stock knee smoothly
instead of stepping off a cliff. Everything at or above the lock (including
any rising tail) is untouched.
"""

from __future__ import annotations

from auto_uv.domain.types import VfCurveCandidate
from auto_uv.curve.base_vf_curve import editable_base_vf_points
from auto_uv.scan_mode.uv_limits import uv_limit_clock_drop_pct_for_gpu

from .vf_curve_flattening import (
    FlatteningRules,
    snap_target_clock,
)


_TIERS = ("efficiency", "balanced", "performance")


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
    keeps the lock point and the upper curve intact and raises the below-lock
    bins toward the lock, so power-limited descents shed voltage together with
    clock instead of falling back to the stock V/F. Final verification still
    tests the returned profile before it is saved.
    """

    tier = _normalized_tier(profile_tier)
    drop_pct = clock_drop_pct
    if drop_pct is None:
        drop_pct = uv_limit_clock_drop_pct_for_gpu(gpu_name, profile_id=tier)
    if drop_pct is None or float(drop_pct) <= 0.0:
        return selected_candidate, int(tail_rise_bins)
    if _is_tier_runtime_shaped(selected_candidate):
        return selected_candidate, int(tail_rise_bins)
    # An Auto-OC / performance-sweep plan already encodes probed below-lock
    # anchors; overwriting them with a synthetic ramp would trade verified
    # points for extrapolated ones.
    if bool(dict(selected_candidate.metadata or {}).get("auto_oc")):
        return selected_candidate, int(tail_rise_bins)

    reference_clock_mhz = int(selected_candidate.target_mhz)
    sustained_clock_mhz = _proportional_clock_target(
        reference_clock_mhz,
        clock_drop_pct=float(drop_pct),
        rules=rules,
    )
    if sustained_clock_mhz >= reference_clock_mhz:
        return selected_candidate, int(tail_rise_bins)

    shaped = _shape_below_lock_for_sustained_load(
        selected_candidate.flattened_plan,
        base_curve,
        lock_voltage_mv=int(selected_candidate.voltage_mv),
        lock_clock_mhz=int(selected_candidate.target_mhz),
        sustained_clock_mhz=int(sustained_clock_mhz),
        rules=rules,
    )
    if shaped is None:
        return selected_candidate, int(tail_rise_bins)
    shaped_plan, anchor_voltage_mv = shaped

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
        int(tail_rise_bins),
    )


def _normalized_tier(profile_tier: object | None) -> str:
    tier = str(profile_tier or "efficiency").strip().lower()
    if tier in _TIERS:
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


def _shape_below_lock_for_sustained_load(
    plan: list[dict],
    base_curve: list[dict],
    *,
    lock_voltage_mv: int,
    lock_clock_mhz: int,
    sustained_clock_mhz: int,
    rules: FlatteningRules,
) -> tuple[list[dict], int] | None:
    editable_below_voltages = sorted(
        int(point.voltage_mv)
        for point in editable_base_vf_points(plan)
        if int(point.voltage_mv) < int(lock_voltage_mv)
    )
    if not editable_below_voltages:
        return None
    # The below-lock plateau gap from the scan plan is preserved: no shaped
    # bin may reach the lock clock at a voltage the scan never proved.
    ceiling_clock_mhz = int(lock_clock_mhz) - int(rules.clock_step_mhz)
    if int(sustained_clock_mhz) >= ceiling_clock_mhz:
        return None

    stock_lock_voltage_mv = _stock_voltage_for_clock(base_curve, int(lock_clock_mhz))
    if stock_lock_voltage_mv is None:
        # The lock sits above every stock clock (e.g. an overclocked lock);
        # there is no proven-offset geometry to extend, so the profile is
        # left unshaped rather than extrapolated.
        return None
    lock_offset_mv = int(stock_lock_voltage_mv) - int(lock_voltage_mv)
    if lock_offset_mv <= 0:
        return None

    full_shift_targets: dict[int, int] = {}
    for voltage_mv in editable_below_voltages:
        shifted_clock_mhz = _stock_clock_for_voltage(
            base_curve,
            voltage_mv + _proven_shift_mv(
                voltage_mv,
                lock_offset_mv=lock_offset_mv,
                lock_voltage_mv=int(lock_voltage_mv),
            ),
        )
        if shifted_clock_mhz is None:
            continue
        full_shift_targets[voltage_mv] = min(
            int(ceiling_clock_mhz),
            snap_target_clock(int(shifted_clock_mhz), rules=rules),
        )
    anchored_voltages = [
        voltage_mv
        for voltage_mv in editable_below_voltages
        if full_shift_targets.get(voltage_mv, 0) >= int(sustained_clock_mhz)
    ]
    if not anchored_voltages:
        return None
    anchor_voltage_mv = anchored_voltages[0]

    targets = {
        voltage_mv: full_shift_targets[voltage_mv]
        for voltage_mv in anchored_voltages
    }
    # Below the tier's sustained floor the proven shift tapers linearly to
    # zero across one ramp window, so the shaped region rejoins the stock
    # knee smoothly instead of dropping the whole offset in a single bin.
    blend_window_mv = max(1, int(rules.flatten_ramp_window_mv))
    for voltage_mv in editable_below_voltages:
        distance_mv = int(anchor_voltage_mv) - voltage_mv
        if distance_mv <= 0 or distance_mv >= blend_window_mv:
            continue
        blend_fraction = 1.0 - (float(distance_mv) / float(blend_window_mv))
        blended_shift_mv = int(
            round(
                blend_fraction
                * _proven_shift_mv(
                    voltage_mv,
                    lock_offset_mv=lock_offset_mv,
                    lock_voltage_mv=int(lock_voltage_mv),
                )
            )
        )
        blended_clock_mhz = _stock_clock_for_voltage(
            base_curve,
            voltage_mv + blended_shift_mv,
        )
        if blended_clock_mhz is None:
            continue
        targets[voltage_mv] = min(
            int(ceiling_clock_mhz),
            snap_target_clock(int(blended_clock_mhz), rules=rules),
        )

    shaped_plan = []
    raised_any_bin = False
    for source_point in plan:
        target_mhz = targets.get(int(source_point["voltage_mv"]))
        # Only ever raise a bin: the scan plan's own value is the floor, so a
        # lock that ratcheted below the stock geometry keeps its verified
        # (monotonic) below-lock shape instead of gaining unproven points.
        if target_mhz is None or target_mhz <= int(source_point["target_mhz"]):
            shaped_plan.append(source_point)
            continue
        point = dict(source_point)
        point["target_mhz"] = int(target_mhz)
        point["new_offset_mhz"] = int(target_mhz) - int(point["base_mhz"])
        shaped_plan.append(point)
        raised_any_bin = True
    if not raised_any_bin:
        return None
    return shaped_plan, int(anchor_voltage_mv)


def _proven_shift_mv(
    voltage_mv: int,
    *,
    lock_offset_mv: int,
    lock_voltage_mv: int,
) -> int:
    """The lock's proven voltage offset scaled by the bin's own voltage.

    Scaling keeps the implied per-millivolt undervolt at or below the ratio
    the scan verified at the lock.
    """

    return int(
        round(
            float(lock_offset_mv) * float(voltage_mv) / float(lock_voltage_mv)
        )
    )


def _stock_clock_for_voltage(base_curve: list[dict], voltage_mv: int) -> int | None:
    candidates = [
        int(point.base_mhz)
        for point in editable_base_vf_points(base_curve)
        if int(point.voltage_mv) <= int(voltage_mv)
    ]
    if not candidates:
        return None
    return max(candidates)


def _stock_voltage_for_clock(base_curve: list[dict], clock_mhz: int) -> int | None:
    candidates = [
        int(point.voltage_mv)
        for point in editable_base_vf_points(base_curve)
        if int(point.base_mhz) >= int(clock_mhz)
    ]
    if not candidates:
        return None
    return min(candidates)
