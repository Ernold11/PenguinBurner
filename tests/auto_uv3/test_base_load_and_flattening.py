from __future__ import annotations

import pytest

from auto_uv3.curve.base_load_flatten_target import choose_base_load_flatten_target
from auto_uv3.curve.vf_curve_flattening import build_flattened_plan
from auto_uv3_test_data import base_curve


def test_baseline_target_uses_loaded_samples_and_snaps_down() -> None:
    curve = base_curve(850, 1000, 25, 1770, 15)
    telemetry = [
        {"elapsed_s": 1.0, "power_w": 35.0, "core_clock_mhz": 1200.0},
        {"elapsed_s": 6.0, "power_w": 190.0, "core_clock_mhz": 1848.0},
        {"elapsed_s": 7.0, "power_w": 196.0, "core_clock_mhz": 1842.0},
        {"elapsed_s": 8.0, "power_w": 200.0, "core_clock_mhz": 1837.0},
    ]

    target = choose_base_load_flatten_target(
        curve,
        telemetry,
        power_limit_w=200,
        fallback_clock_mhz=1905.0,
    )

    assert target.measured_clock_mhz == pytest.approx(1839.5)
    assert target.target_clock_mhz == 1830
    assert target.saturated_sample_count == 2
    assert target.active_sample_count == 3


def test_flattened_plan_builds_plateau_and_preserves_base_points() -> None:
    curve = base_curve(825, 950, 25, 1900, 30)
    curve[0]["preserve_base"] = True

    plan = build_flattened_plan(
        curve,
        lock_clock_mhz=2070,
        candidate_voltage_mv=900,
        below_lock_gap_mhz=30,
    )

    by_voltage = {point["voltage_mv"]: point for point in plan}
    assert by_voltage[825]["target_mhz"] == by_voltage[825]["base_mhz"]
    assert by_voltage[900]["target_mhz"] == 2070
    assert by_voltage[925]["target_mhz"] == 2070
    assert by_voltage[875]["target_mhz"] <= 2040
    assert by_voltage[875]["new_offset_mhz"] == (
        by_voltage[875]["target_mhz"] - by_voltage[875]["base_mhz"]
    )


def test_flattened_plan_rejects_non_editable_voltage() -> None:
    curve = base_curve(825, 950, 25, 1900, 30)
    curve[1]["preserve_base"] = True

    with pytest.raises(ValueError, match="not an editable VF bin"):
        build_flattened_plan(
            curve,
            lock_clock_mhz=2070,
            candidate_voltage_mv=850,
        )
