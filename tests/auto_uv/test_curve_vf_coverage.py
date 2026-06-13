from __future__ import annotations

from pathlib import Path

import pytest

from auto_uv.auto_uv_types import AutoUvError, AutoUvProbeSummary
from auto_uv.curve.base_vf_curve import (
    read_base_vf_points,
    write_base_vf_point,
    write_base_vf_points,
)
from auto_uv.curve.base_vf_curve_validation import validate_base_vf_curve
from auto_uv.curve.base_vf_curve_voltage_bins import (
    base_target_clock_at_voltage,
    higher_editable_voltage_bins,
    lock_voltage_for_target_clock,
    lower_editable_voltage_bins,
    nearest_editable_voltage_bin,
    next_higher_editable_voltage_bin,
)
from auto_uv.curve.measured_probe_lock_clock import lock_clock_from_probe_loaded_clock

from auto_uv_test_data import base_curve


def _all_preserved_curve() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": 800 + index * 25,
            "base_mhz": 2000 + index * 30,
            "target_mhz": 2000 + index * 30,
            "new_offset_mhz": 0,
            "preserve_base": True,
        }
        for index in range(3)
    ]


def _probe_summary(avg_core_clock_mhz: float | None) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=950,
        lock_clock_mhz=2550,
        live_voltage_before_mv=950,
        live_voltage_after_mv=950,
        avg_voltage_mv=950.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=62.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=35.0,
        avg_core_clock_mhz=avg_core_clock_mhz,
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=10.0,
        watts_per_mhz=0.1,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
    )


# --- base_vf_curve.py ---------------------------------------------------------


def test_write_base_vf_point_roundtrips_typed_fields() -> None:
    points = read_base_vf_points(base_curve(800, 875, 25, 2000, 30))
    point = points[0]

    item = write_base_vf_point(point)

    assert item["index"] == 0
    assert item["voltage_mv"] == 800
    assert item["base_mhz"] == 2000
    assert item["target_mhz"] == 2000
    # new_offset_mhz is derived from target - base.
    assert item["new_offset_mhz"] == 0
    # No preserve flag present in source and point is not preserved.
    assert "preserve_base" not in item


def test_write_base_vf_point_emits_offset_and_preserve_flag() -> None:
    [point] = read_base_vf_points(
        [
            {
                "index": 4,
                "voltage_mv": 900,
                "base_mhz": 2100,
                "target_mhz": 2250,
                "preserve_base": True,
            }
        ]
    )

    item = write_base_vf_point(point)

    assert item["new_offset_mhz"] == 150  # 2250 - 2100
    assert item["preserve_base"] is True
    # The legacy alias must be dropped on write.
    assert "preserve_vanilla" not in item


def test_write_base_vf_point_drops_preserve_vanilla_alias() -> None:
    [point] = read_base_vf_points(
        [
            {
                "index": 1,
                "voltage_mv": 825,
                "base_mhz": 2000,
                "target_mhz": 2000,
                "preserve_vanilla": True,
            }
        ]
    )

    item = write_base_vf_point(point)

    assert "preserve_vanilla" not in item
    # preserve_base is truthy because it derived from the legacy alias.
    assert item["preserve_base"] is True


def test_write_base_vf_points_preserves_order_and_count() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)
    points = read_base_vf_points(curve)

    written = write_base_vf_points(points)

    assert [item["voltage_mv"] for item in written] == [
        item["voltage_mv"] for item in curve
    ]
    assert len(written) == len(curve)


# --- base_vf_curve_validation.py ----------------------------------------------


def test_validate_rejects_too_few_editable_points() -> None:
    curve = base_curve(800, 850, 25, 2000, 30)  # only 2 points

    with pytest.raises(ValueError, match="at least 3 editable"):
        validate_base_vf_curve(curve)


def test_validate_rejects_duplicate_editable_voltage_bins() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)
    # Force a duplicate voltage bin while keeping >=3 points.
    curve.append(
        {
            "index": 99,
            "voltage_mv": curve[0]["voltage_mv"],
            "base_mhz": 2500,
            "target_mhz": 2500,
            "new_offset_mhz": 0,
        }
    )

    with pytest.raises(ValueError, match="duplicate editable voltage bins"):
        validate_base_vf_curve(curve)


def test_validate_rejects_invalid_editable_point() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)
    curve[1]["base_mhz"] = 0  # non-positive base clock

    with pytest.raises(ValueError) as excinfo:
        validate_base_vf_curve(curve)

    assert "invalid editable point" in str(excinfo.value)
    assert "base=0MHz" in str(excinfo.value)


def test_validate_accepts_healthy_curve() -> None:
    assert validate_base_vf_curve(base_curve()) is None


# --- base_vf_curve_voltage_bins.py --------------------------------------------


def test_nearest_editable_voltage_bin_raises_without_editable_bins() -> None:
    with pytest.raises(ValueError, match="did not contain editable voltage bins"):
        nearest_editable_voltage_bin(_all_preserved_curve(), 900)


def test_nearest_editable_voltage_bin_picks_closest() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert nearest_editable_voltage_bin(curve, 840) == 850


def test_lower_editable_voltage_bins_respects_min_search_floor() -> None:
    curve = base_curve(800, 1000, 25, 2000, 30)

    bins = lower_editable_voltage_bins(
        curve,
        start_voltage_mv=975,
        preserve_base_below_mv=None,
        min_search_voltage_mv=850,
    )

    # 800 and 825 fall below the min-search floor and are dropped.
    assert bins == [950, 925, 900, 875, 850]


def test_lower_editable_voltage_bins_respects_preserve_base_floor() -> None:
    curve = base_curve(800, 1000, 25, 2000, 30)

    bins = lower_editable_voltage_bins(
        curve,
        start_voltage_mv=975,
        preserve_base_below_mv=875,
        min_search_voltage_mv=None,
    )

    # 800, 825, 850, 875 are at or below the preserve floor and are dropped.
    assert bins == [950, 925, 900]


def test_next_higher_editable_voltage_bin_returns_min_above() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert next_higher_editable_voltage_bin(curve, 825) == 850
    assert higher_editable_voltage_bins(curve, 825) == [850, 875]


def test_next_higher_editable_voltage_bin_none_at_top() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert next_higher_editable_voltage_bin(curve, 875) is None


def test_lock_voltage_for_target_clock_raises_when_unreachable() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    with pytest.raises(AutoUvError, match="never reaches 9999MHz"):
        lock_voltage_for_target_clock(curve, 9999)


def test_lock_voltage_for_target_clock_returns_first_match() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    # target_mhz at 850mV (index 2) is 2060, first to reach 2050.
    assert lock_voltage_for_target_clock(curve, 2050) == 850


def test_base_target_clock_at_voltage_matches_existing_bin() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert base_target_clock_at_voltage(curve, voltage_mv=825, fallback_mhz=0) == 2030


def test_base_target_clock_at_voltage_falls_back_when_missing() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert (
        base_target_clock_at_voltage(curve, voltage_mv=12345, fallback_mhz=1500) == 1500
    )


# --- measured_probe_lock_clock.py ---------------------------------------------


def test_lock_clock_returns_previous_when_no_measured_clock() -> None:
    curve = base_curve()

    result = lock_clock_from_probe_loaded_clock(
        curve,
        probe=_probe_summary(avg_core_clock_mhz=None),
        previous_lock_clock_mhz=2400,
    )

    assert result == 2400


def test_lock_clock_takes_min_of_measured_and_previous() -> None:
    curve = base_curve(800, 1025, 25, 2000, 30)

    # Measured clock snaps well below the previous lock, so it wins.
    result = lock_clock_from_probe_loaded_clock(
        curve,
        probe=_probe_summary(avg_core_clock_mhz=2100.0),
        previous_lock_clock_mhz=2400,
    )

    assert result < 2400
    assert result == min(2400, result)
