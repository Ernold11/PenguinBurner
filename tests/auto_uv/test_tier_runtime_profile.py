from __future__ import annotations

from auto_uv.curve.rising_tail import tail_ceiling_clock_mhz
from auto_uv.curve.tier_runtime_profile import (
    build_tier_runtime_profile_candidate,
)
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.types import VfCurveCandidate

from auto_uv_test_data import rtx_5080_20260524_high_oc_base_curve


def _candidate_from_5080_curve() -> VfCurveCandidate:
    curve = rtx_5080_20260524_high_oc_base_curve()
    plan = build_flattened_plan(
        curve,
        lock_clock_mhz=2280,
        candidate_voltage_mv=825,
        tail_rise_bins=2,
    )
    return VfCurveCandidate(
        label="stable",
        voltage_mv=825,
        target_mhz=2280,
        flattened_plan=plan,
        metadata={"tail_rise_bins": 2},
    )


def _candidate_from_5080_efficiency_scan_result() -> VfCurveCandidate:
    curve = rtx_5080_20260524_high_oc_base_curve()
    plan = build_flattened_plan(
        curve,
        lock_clock_mhz=2235,
        candidate_voltage_mv=850,
        tail_rise_bins=2,
    )
    return VfCurveCandidate(
        label="stable",
        voltage_mv=850,
        target_mhz=2235,
        flattened_plan=plan,
        metadata={"tail_rise_bins": 2},
    )


def _by_voltage(plan: list[dict]) -> dict[int, dict]:
    return {int(point["voltage_mv"]): point for point in plan}


def test_efficiency_shape_keeps_lock_and_ramps_below_lock() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    selected = _candidate_from_5080_efficiency_scan_result()

    shaped, tail_rise_bins = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="efficiency",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=2,
    )

    by_voltage = _by_voltage(shaped.flattened_plan)

    assert shaped.voltage_mv == 850
    assert shaped.target_mhz == 2235
    assert tail_rise_bins == 2
    # Lock and the rising tail above it stay untouched.
    assert by_voltage[850]["target_mhz"] == 2235
    assert by_voltage[860]["target_mhz"] == 2250
    assert by_voltage[865]["target_mhz"] == 2265
    assert by_voltage[915]["target_mhz"] == 2265
    assert by_voltage[1240]["target_mhz"] == 2265
    assert tail_ceiling_clock_mhz(
        shaped.flattened_plan,
        fallback_clock_mhz=shaped.target_mhz,
        lock_voltage_mv=shaped.voltage_mv,
    ) == 2265
    # Below the lock the curve ramps down to the sustained clock at the anchor,
    # then returns to stock; no shaped bin drops below the sustained clock and
    # bins keep the scan plan's own value when it already exceeds the ramp.
    assert by_voltage[845]["target_mhz"] == 2220
    assert by_voltage[840]["target_mhz"] == 2205
    assert by_voltage[820]["target_mhz"] == 2085
    assert by_voltage[800]["target_mhz"] == 1980
    assert by_voltage[795]["target_mhz"] == 1732
    assert by_voltage[760]["target_mhz"] == 892
    # No below-lock bin ever reaches the lock clock itself.
    assert all(
        int(point["target_mhz"]) < 2235
        for point in shaped.flattened_plan
        if int(point["voltage_mv"]) < 850
    )
    shaped_region = [
        int(point["target_mhz"])
        for point in shaped.flattened_plan
        if 800 <= int(point["voltage_mv"]) <= 850
    ]
    assert min(shaped_region) == 1980
    assert shaped.metadata["profile_runtime_shape"] == "tier-runtime-shape"
    assert shaped.metadata["profile_runtime_shape_reference_clock_mhz"] == 2235
    assert shaped.metadata["profile_runtime_shape_sustained_clock_mhz"] == 1980
    assert shaped.metadata["profile_runtime_shape_anchor_voltage_mv"] == 800


def test_balanced_and_performance_keep_tails_and_ramp_proportionally() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    selected = _candidate_from_5080_curve()

    balanced, balanced_tail = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="balanced",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=4,
    )
    performance, performance_tail = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="performance",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=6,
    )

    balanced_by_voltage = _by_voltage(balanced.flattened_plan)
    performance_by_voltage = _by_voltage(performance.flattened_plan)

    # The lock point and the configured tier tail bins pass through unchanged.
    assert (balanced.voltage_mv, balanced.target_mhz, balanced_tail) == (825, 2280, 4)
    assert (performance.voltage_mv, performance.target_mhz, performance_tail) == (
        825,
        2280,
        6,
    )
    # The rising tail above the lock is preserved, not inverted.
    assert balanced_by_voltage[890]["target_mhz"] == 2310
    assert performance_by_voltage[890]["target_mhz"] == 2310
    # Below the lock, balanced derates deeper than performance and both bottom
    # out at their tier's sustained clock at a lower voltage than the lock.
    assert balanced.metadata["profile_runtime_shape_anchor_voltage_mv"] == 790
    assert balanced.metadata["profile_runtime_shape_sustained_clock_mhz"] == 2085
    assert balanced_by_voltage[790]["target_mhz"] == 2085
    assert performance.metadata["profile_runtime_shape_anchor_voltage_mv"] == 800
    assert performance.metadata["profile_runtime_shape_sustained_clock_mhz"] == 2160
    assert performance_by_voltage[800]["target_mhz"] == 2160
    assert balanced_by_voltage[810]["target_mhz"] < performance_by_voltage[810][
        "target_mhz"
    ] < selected.target_mhz


def test_unknown_gpu_keeps_selected_candidate_unchanged() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    selected = _candidate_from_5080_curve()

    shaped, tail_rise_bins = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="efficiency",
        gpu_name="NVIDIA GeForce GTX 1080",
        tail_rise_bins=2,
    )

    assert shaped is selected
    assert tail_rise_bins == 2


def test_configured_drop_pct_shapes_unlisted_gpu() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    selected = _candidate_from_5080_curve()

    shaped, tail_rise_bins = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="efficiency",
        gpu_name="NVIDIA GeForce GTX 1080",
        tail_rise_bins=2,
        clock_drop_pct=11.111111111111116,
    )

    by_voltage = _by_voltage(shaped.flattened_plan)

    assert shaped is not selected
    assert shaped.voltage_mv == 825
    assert shaped.target_mhz == 2280
    assert tail_rise_bins == 2
    assert shaped.metadata["profile_runtime_shape_anchor_voltage_mv"] == 775
    assert by_voltage[775]["target_mhz"] == 2025
    assert by_voltage[785]["target_mhz"] == 2070
    assert by_voltage[890]["target_mhz"] == 2310


def test_auto_oc_candidate_is_not_reshaped() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    base = _candidate_from_5080_curve()
    selected = VfCurveCandidate(
        label=base.label,
        voltage_mv=base.voltage_mv,
        target_mhz=base.target_mhz,
        flattened_plan=base.flattened_plan,
        metadata={**base.metadata, "auto_oc": True},
    )

    shaped, tail_rise_bins = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="performance",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=6,
    )

    # Auto-OC / performance-sweep plans carry probed below-lock anchors and
    # must not be overwritten with a synthetic ramp.
    assert shaped is selected
    assert tail_rise_bins == 6


def test_ratcheted_lock_below_stock_keeps_scan_plan_and_monotonicity() -> None:
    # A power-capped sweep can ratchet the lock clock far below the stock
    # clock of nearby bins; the shape must never raise a below-lock bin above
    # the scan plan's capped values (which would break monotonicity).
    curve = rtx_5080_20260524_high_oc_base_curve()
    plan = build_flattened_plan(
        curve,
        lock_clock_mhz=2000,
        candidate_voltage_mv=900,
        tail_rise_bins=0,
    )
    selected = VfCurveCandidate(
        label="ratcheted",
        voltage_mv=900,
        target_mhz=2000,
        flattened_plan=plan,
        metadata={"tail_rise_bins": 0},
    )

    shaped, _ = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="efficiency",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=0,
    )

    ordered = sorted(
        shaped.flattened_plan, key=lambda point: int(point["voltage_mv"])
    )
    targets = [int(point["target_mhz"]) for point in ordered]
    assert targets == sorted(targets)
    assert all(
        int(point["target_mhz"]) < 2000
        for point in ordered
        if int(point["voltage_mv"]) < 900
    )


def test_curve_stays_monotonic_after_shaping() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    selected = _candidate_from_5080_efficiency_scan_result()

    shaped, _ = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="efficiency",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=2,
    )

    ordered = sorted(shaped.flattened_plan, key=lambda point: int(point["voltage_mv"]))
    targets = [int(point["target_mhz"]) for point in ordered]
    assert targets == sorted(targets)
    # The reshaped below-lock region never drops below the stock curve; above
    # the lock the undervolt cap intentionally sits below stock.
    for point in ordered:
        if int(point["voltage_mv"]) <= int(shaped.voltage_mv):
            assert int(point["target_mhz"]) >= int(point["base_mhz"])
