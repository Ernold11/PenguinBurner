from __future__ import annotations

from auto_uv.lower_voltage_probe_target import (
    base_curve_target_for_lower_voltage,
    lower_voltage_clock_floor_miss_reason,
)
from auto_uv.lower_voltage_search import (
    filter_effective_voltage_candidates,
    select_next_lower_voltage,
)
from auto_uv.persistence.unsafe_voltage_cache import (
    unsafe_entry_blocks_voltage_candidate,
    unsafe_min_search_voltage,
)
from auto_uv_test_data import base_curve, probe_summary


def test_lower_voltage_search_keeps_final_low_bin_testable() -> None:
    selected = select_next_lower_voltage(
        base_curve(900, 1025, 25),
        start_voltage_mv=1000,
        stable_voltage_mv=1000,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=900,
    )
    filtered = filter_effective_voltage_candidates(
        [925, 900],
        stable_voltage_mv=1000,
        reference_actual_voltage_mv=1000.0,
    )

    assert selected == 925
    assert filtered[-1] == 900


def test_lower_voltage_probe_target_follows_base_curve_until_measurement_exists() -> None:
    curve = base_curve(800, 1025, 25, 2000, 30)

    assert (
        base_curve_target_for_lower_voltage(
            curve,
            candidate_voltage_mv=900,
            stable_target_mhz=2240,
            stable_measured_target_mhz=None,
        )
        == 2120
    )
    assert (
        base_curve_target_for_lower_voltage(
            curve,
            candidate_voltage_mv=900,
            stable_target_mhz=2240,
            stable_measured_target_mhz=2190,
        )
        == 2190
    )


def test_lower_voltage_probe_target_predicts_clock_floor_miss() -> None:
    reason = lower_voltage_clock_floor_miss_reason(
        [
            probe_summary(1000, clock_mhz=2400.0),
            probe_summary(950, clock_mhz=2250.0),
        ],
        candidate_voltage_mv=900,
        baseline_core_clock_mhz=2400.0,
        min_core_clock_pct=90.0,
    )

    assert reason == "predicted=2100.0MHz floor=2160.0MHz"


def test_unsafe_cache_blocks_only_the_recorded_clock_band_when_clock_aware() -> None:
    entry = {"candidate_voltage_mv": 900, "lock_clock_mhz": 2200}

    assert unsafe_entry_blocks_voltage_candidate(
        entry,
        candidate_voltage_mv=875,
        lock_clock_mhz=2200,
    )
    assert not unsafe_entry_blocks_voltage_candidate(
        entry,
        candidate_voltage_mv=875,
        lock_clock_mhz=2050,
    )


def test_unsafe_cache_raises_min_search_above_legacy_unsafe_floor() -> None:
    unsafe_floor_mv, min_search_mv = unsafe_min_search_voltage(
        base_curve(850, 1025, 25),
        start_voltage_mv=1000,
        unsafe_entries=[{"candidate_voltage_mv": 925, "reason": "timedemo-crash"}],
    )

    assert unsafe_floor_mv == 925
    assert min_search_mv == 950
