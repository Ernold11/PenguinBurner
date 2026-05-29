from __future__ import annotations

import pytest

from auto_uv.curve.base_load_flatten_target import (
    choose_base_load_flatten_target,
    selected_nvidia_light_load_diagnostic,
)
from auto_uv.curve.vf_curve_flattening import (
    build_flatten_target_for_plan,
    build_flattened_plan,
)
from auto_uv_test_data import base_curve, rtx_5080_20260524_high_oc_base_curve


def _by_voltage(plan: list[dict]) -> dict[int, dict]:
    return {int(point["voltage_mv"]): point for point in plan}


def _target_gaps(
    by_voltage: dict[int, dict],
    *,
    start_mv: int = 835,
    end_mv: int = 940,
) -> list[int]:
    voltages = [
        voltage_mv
        for voltage_mv in sorted(by_voltage)
        if start_mv <= voltage_mv <= end_mv
    ]
    return [
        by_voltage[right]["target_mhz"] - by_voltage[left]["target_mhz"]
        for left, right in zip(voltages, voltages[1:])
    ]


def _tail_voltages(by_voltage: dict[int, dict], candidate_voltage_mv: int) -> list[int]:
    return [
        voltage_mv
        for voltage_mv in sorted(by_voltage)
        if voltage_mv >= int(candidate_voltage_mv)
    ]


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


def test_baseline_target_failure_includes_telemetry_diagnostic() -> None:
    curve = base_curve(850, 1000, 25, 1770, 15)
    telemetry = [
        {
            "elapsed_s": 6.0,
            "power_w": 4.5,
            "gpu_util_pct": 0.0,
            "core_clock_mhz": 210.0,
        },
        {
            "elapsed_s": 7.0,
            "power_w": 5.0,
            "gpu_util_pct": 0.0,
            "core_clock_mhz": 210.0,
        },
    ]

    with pytest.raises(ValueError) as exc_info:
        choose_base_load_flatten_target(
            curve,
            telemetry,
            power_limit_w=110,
            fallback_clock_mhz=None,
        )

    message = str(exc_info.value)
    assert "baseline probe did not report a loaded core clock" in message
    assert "power_limit=110W" in message
    assert "max_power=5.0W" in message
    assert "max_util=0.0%" in message
    assert "max_clock=210.0MHz" in message
    assert "active_samples=0" in message
    assert "display-attached GPU" in message
    assert "--gpu-index" in message


def test_selected_nvidia_light_load_diagnostic_is_non_fatal_and_specific() -> None:
    diagnostic = selected_nvidia_light_load_diagnostic(
        [
            {
                "elapsed_s": 6.0,
                "power_w": 30.0,
                "gpu_util_pct": 97.0,
                "core_clock_mhz": 1320.0,
            },
            {
                "elapsed_s": 7.0,
                "power_w": 31.0,
                "gpu_util_pct": 98.0,
                "core_clock_mhz": 1335.0,
            },
        ],
        power_limit_w=110,
    )

    assert diagnostic is not None
    assert diagnostic.startswith("warning selected NVIDIA GPU light-load diagnostic")
    assert "power_limit=110W" in diagnostic
    assert "max_power=31.0W" in diagnostic
    assert "max_util=98.0%" in diagnostic
    assert "low_power_floor=55.0W" in diagnostic
    assert "continuing" in diagnostic


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


def test_flattened_plan_can_add_soft_rising_tail() -> None:
    curve = base_curve(825, 1000, 25, 1900, 30)

    plan = build_flattened_plan(
        curve,
        lock_clock_mhz=2000,
        candidate_voltage_mv=900,
        tail_rise_bins=2,
    )

    by_voltage = {point["voltage_mv"]: point for point in plan}
    assert by_voltage[900]["target_mhz"] == 2000
    assert by_voltage[925]["target_mhz"] == 2015
    assert by_voltage[950]["target_mhz"] == 2030
    assert by_voltage[975]["target_mhz"] == 2030

    target = build_flatten_target_for_plan(
        curve,
        plan,
        lock_clock_mhz=2000,
        lock_voltage_mv=900,
        tail_rise_bins=2,
    )
    assert target["ceiling_clock_mhz"] == 2030
    assert target["tail_rise_bins"] == 2


@pytest.mark.parametrize(
    (
        "lock_clock_mhz",
        "tail_rise_bins",
        "expected_tail",
        "tail_ceiling_mhz",
    ),
    (
        (2730, 4, [2730, 2745, 2760, 2775, 2790], 2790),
        (2550, 0, [2550, 2550, 2550, 2550, 2550], 2550),
    ),
)
def test_flattened_plan_tail_shape_is_the_only_performance_curve_difference(
    lock_clock_mhz: int,
    tail_rise_bins: int,
    expected_tail: list[int],
    tail_ceiling_mhz: int,
) -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()

    for candidate_voltage_mv in (875, 900):
        plan = build_flattened_plan(
            curve,
            lock_clock_mhz=lock_clock_mhz,
            candidate_voltage_mv=candidate_voltage_mv,
            tail_rise_bins=tail_rise_bins,
        )
        by_voltage = _by_voltage(plan)
        tail_voltages = _tail_voltages(by_voltage, candidate_voltage_mv)
        high_load_voltages = [v for v in sorted(by_voltage) if 835 <= v <= 940]

        assert by_voltage[candidate_voltage_mv]["target_mhz"] == lock_clock_mhz
        assert [
            by_voltage[voltage_mv]["target_mhz"] for voltage_mv in tail_voltages[:5]
        ] == expected_tail
        assert max(
            by_voltage[voltage_mv]["target_mhz"] for voltage_mv in tail_voltages
        ) == tail_ceiling_mhz
        assert max(_target_gaps(by_voltage)) <= 150
        assert all(
            by_voltage[voltage_mv]["target_mhz"] <= tail_ceiling_mhz
            for voltage_mv in high_load_voltages
        )
        below_lock_voltages = [
            voltage_mv
            for voltage_mv in high_load_voltages
            if voltage_mv < candidate_voltage_mv
        ]
        if lock_clock_mhz == 2550:
            assert by_voltage[below_lock_voltages[-1]]["target_mhz"] <= 2535


def test_flattened_plan_keeps_legacy_pre_lock_ramp_by_default() -> None:
    curve = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": base_mhz,
            "target_mhz": base_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, base_mhz) in enumerate(
            [
                (860, 2182),
                (865, 2212),
                (870, 2235),
                (875, 2265),
                (885, 2295),
                (890, 2317),
                (895, 2347),
                (900, 2370),
            ]
        )
    ]

    plan = build_flattened_plan(
        curve,
        lock_clock_mhz=2910,
        candidate_voltage_mv=900,
    )

    by_voltage = {point["voltage_mv"]: point for point in plan}
    assert by_voltage[885]["target_mhz"] == 2685
    assert by_voltage[900]["target_mhz"] - by_voltage[885]["target_mhz"] == 225


def test_rising_tail_does_not_wait_for_base_curve_to_catch_up() -> None:
    curve = base_curve(800, 1050, 25, 1900, 15)

    plan = build_flattened_plan(
        curve,
        lock_clock_mhz=2000,
        candidate_voltage_mv=850,
        tail_rise_bins=4,
    )

    by_voltage = {point["voltage_mv"]: point for point in plan}
    assert by_voltage[850]["target_mhz"] == 2000
    assert by_voltage[875]["target_mhz"] == 2015
    assert by_voltage[900]["target_mhz"] == 2030
    assert by_voltage[925]["target_mhz"] == 2045
    assert by_voltage[950]["target_mhz"] == 2060
    assert by_voltage[975]["target_mhz"] == 2060


def test_flattened_plan_rejects_non_editable_voltage() -> None:
    curve = base_curve(825, 950, 25, 1900, 30)
    curve[1]["preserve_base"] = True

    with pytest.raises(ValueError, match="not an editable VF bin"):
        build_flattened_plan(
            curve,
            lock_clock_mhz=2070,
            candidate_voltage_mv=850,
        )
