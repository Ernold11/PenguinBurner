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


def _by_voltage(plan: list[dict]) -> dict[int, dict]:
    return {int(point["voltage_mv"]): point for point in plan}


def test_efficiency_shape_lowers_final_profile_like_sustained_power_tune() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    selected = _candidate_from_5080_curve()

    shaped, tail_rise_bins = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="efficiency",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=2,
    )

    assert shaped.voltage_mv == 800
    assert shaped.target_mhz == 2025
    assert tail_rise_bins == 0
    assert _by_voltage(shaped.flattened_plan)[800]["target_mhz"] == 2025
    assert tail_ceiling_clock_mhz(
        shaped.flattened_plan,
        fallback_clock_mhz=shaped.target_mhz,
        lock_voltage_mv=shaped.voltage_mv,
    ) == 2025
    assert shaped.metadata["profile_runtime_shape"] == "tier-runtime-shape"
    assert shaped.metadata["profile_runtime_shape_reference_clock_mhz"] == 2280


def test_balanced_and_performance_shapes_keep_proportional_ladder() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    selected = _candidate_from_5080_curve()

    balanced, balanced_tail = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="balanced",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=2,
    )
    performance, performance_tail = build_tier_runtime_profile_candidate(
        curve,
        selected_candidate=selected,
        profile_tier="performance",
        gpu_name="NVIDIA GeForce RTX 5080",
        tail_rise_bins=2,
    )

    assert (balanced.voltage_mv, balanced.target_mhz, balanced_tail) == (810, 2085, 4)
    assert (performance.voltage_mv, performance.target_mhz, performance_tail) == (
        810,
        2160,
        6,
    )
    assert 2025 < balanced.target_mhz < performance.target_mhz < selected.target_mhz


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

    assert shaped is not selected
    assert shaped.voltage_mv == 800
    assert shaped.target_mhz == 2025
    assert tail_rise_bins == 0
