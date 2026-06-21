from __future__ import annotations

from auto_uv.efficiency_tune.voltage_floor import min_search_voltage_mv


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
