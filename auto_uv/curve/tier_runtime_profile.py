"""Final Auto-UV below-lock curve shaping for sustained board-power-heavy loads.

The scan proves one lock point. Under a sustained power-heavy load
(FurMark-class) the power limiter walks the card DOWN the curve into
below-lock bins, and stock below-lock bins carry no undervolt at all, so the
throttled clocks pay full stock voltage. This shape extends a tapered share of
the lock's proven voltage offset to the bins below the lock: the same
power-limited clock tiers arrive at lower voltage (more watts saved). The ramp
only ever RAISES a bin above the scan plan's own value, stays a full clock
step below the lock clock, and bottoms out at the tier's allowed clock drop;
everything at or above the lock (including any rising tail) is untouched.
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

# The far (anchor) end of the below-lock ramp re-uses this fraction of the
# lock's proven voltage offset. Staying under 1.0 keeps the implied undervolt
# of the unprobed lower bins strictly inside the margin the scan verified at
# the lock, so the ramp is safe to apply without its own Q2RTX/CUDA probe.
_SUSTAINED_ANCHOR_OFFSET_TAPER = 0.7


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

    anchor_voltage_mv = _sustained_anchor_voltage_mv(
        base_curve,
        editable_below_voltages=editable_below_voltages,
        lock_voltage_mv=int(lock_voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        sustained_clock_mhz=int(sustained_clock_mhz),
    )
    if anchor_voltage_mv is None:
        return None

    ramp_span_mv = max(1, int(lock_voltage_mv) - int(anchor_voltage_mv))
    editable_voltages = set(editable_below_voltages)
    shaped_plan = []
    raised_any_bin = False
    for source_point in plan:
        voltage_mv = int(source_point["voltage_mv"])
        if (
            voltage_mv not in editable_voltages
            or voltage_mv < int(anchor_voltage_mv)
        ):
            shaped_plan.append(source_point)
            continue
        fraction = (
            float(voltage_mv) - float(anchor_voltage_mv)
        ) / float(ramp_span_mv)
        ramp_clock_mhz = snap_target_clock(
            int(
                round(
                    float(sustained_clock_mhz)
                    + (
                        (float(lock_clock_mhz) - float(sustained_clock_mhz))
                        * fraction
                    )
                )
            ),
            rules=rules,
        )
        target_mhz = min(int(ceiling_clock_mhz), int(ramp_clock_mhz))
        # Only ever raise a bin: the scan plan's own value is the floor, so a
        # lock that ratcheted below the stock geometry keeps its verified
        # (monotonic) below-lock shape instead of gaining unproven points.
        if target_mhz <= int(source_point["target_mhz"]):
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


def _sustained_anchor_voltage_mv(
    base_curve: list[dict],
    *,
    editable_below_voltages: list[int],
    lock_voltage_mv: int,
    lock_clock_mhz: int,
    sustained_clock_mhz: int,
) -> int | None:
    stock_lock_voltage_mv = _stock_voltage_for_clock(base_curve, int(lock_clock_mhz))
    stock_sustained_voltage_mv = _stock_voltage_for_clock(
        base_curve,
        int(sustained_clock_mhz),
    )
    if stock_lock_voltage_mv is None or stock_sustained_voltage_mv is None:
        # The lock or sustained clock sits above every stock clock (e.g. an
        # overclocked lock); there is no proven-offset geometry to extend, so
        # the profile is left unshaped rather than extrapolated.
        return None
    # The lock proved a voltage offset against the stock curve; the anchor
    # keeps a tapered share of that proven offset at the sustained clock.
    lock_offset_mv = max(0, int(stock_lock_voltage_mv) - int(lock_voltage_mv))
    requested_anchor_mv = int(stock_sustained_voltage_mv) - int(
        round(_SUSTAINED_ANCHOR_OFFSET_TAPER * float(lock_offset_mv))
    )
    at_or_below = [
        voltage_mv
        for voltage_mv in editable_below_voltages
        if voltage_mv <= int(requested_anchor_mv)
    ]
    if at_or_below:
        return at_or_below[-1]
    return editable_below_voltages[0]


def _stock_voltage_for_clock(base_curve: list[dict], clock_mhz: int) -> int | None:
    candidates = [
        int(point.voltage_mv)
        for point in editable_base_vf_points(base_curve)
        if int(point.base_mhz) >= int(clock_mhz)
    ]
    if not candidates:
        return None
    return min(candidates)
