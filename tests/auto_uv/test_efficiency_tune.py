from __future__ import annotations

from auto_uv.auto_uv_scan_settings import AutoUvScanSettings
from auto_uv.efficiency_tune import (
    min_search_voltage_mv,
    voltage_descent_candidate_policy,
)


def test_voltage_floor_uses_explicit_user_value_first() -> None:
    floor = min_search_voltage_mv(
        start_voltage_mv=1025,
        configured_min_voltage_mv=875,
        configured_max_drop_pct=15.0,
        gpu_name="NVIDIA GeForce RTX 5080",
    )

    assert floor == 875


def test_voltage_floor_uses_gpu_table_before_percent_fallback() -> None:
    floor = min_search_voltage_mv(
        start_voltage_mv=1025,
        configured_min_voltage_mv=None,
        configured_max_drop_pct=15.0,
        gpu_name="NVIDIA GeForce RTX 5080",
    )

    assert floor == 850


def test_voltage_floor_uses_configured_percent_when_gpu_unknown() -> None:
    floor = min_search_voltage_mv(
        start_voltage_mv=1025,
        configured_min_voltage_mv=None,
        configured_max_drop_pct=10.0,
        gpu_name="NVIDIA GeForce GTX 1080",
    )

    assert floor == 922


def test_voltage_descent_policy_keeps_clock_and_uses_configured_tail_shape() -> None:
    policy = voltage_descent_candidate_policy(
        settings=AutoUvScanSettings(
            start_voltage_mv=1025,
            min_search_voltage_mv=850,
            baseline_core_clock_mhz=2700.0,
            tail_rise_bins=0,
        ),
        stable_target_mhz=2700,
    )

    assert policy.target_mhz == 2700
    assert policy.tail_rise_bins == 0
