"""The shipped-curve contract: only scan-validated points leave the scan."""

from __future__ import annotations

from types import SimpleNamespace

from auto_uv.curve.shipped_plan import (
    restore_stock_below_validated_floor,
    validated_floor_voltage_mv,
)


def _point(voltage_mv, base_mhz, target_mhz, preserve_base=False):
    point = {
        "voltage_mv": voltage_mv,
        "base_mhz": base_mhz,
        "target_mhz": target_mhz,
        "new_offset_mhz": target_mhz - base_mhz,
    }
    if preserve_base:
        point["preserve_base"] = True
    return point


def test_below_floor_bins_ship_stock() -> None:
    plan = [
        _point(450, 180, 180, preserve_base=True),
        # Scan-machinery debris: the baseline flatten floor at unprobed bins.
        _point(825, 2047, 2415),
        _point(845, 2130, 2415),
        # Validated region: floor rung and lock.
        _point(850, 2160, 2639),
        _point(925, 2475, 2920),
        # Rising tail above the lock stays untouched.
        _point(940, 2500, 2950),
    ]

    shipped = restore_stock_below_validated_floor(plan, floor_voltage_mv=850)

    by_voltage = {point["voltage_mv"]: point for point in shipped}
    assert by_voltage[825]["target_mhz"] == 2047
    assert by_voltage[825]["new_offset_mhz"] == 0
    assert by_voltage[845]["target_mhz"] == 2130
    assert by_voltage[845]["new_offset_mhz"] == 0
    # At and above the floor the validated points are untouched.
    assert by_voltage[850]["target_mhz"] == 2639
    assert by_voltage[925]["target_mhz"] == 2920
    assert by_voltage[940]["target_mhz"] == 2950
    # preserve_base bins are never rewritten (they are already stock).
    assert by_voltage[450]["target_mhz"] == 180
    # The input plan is not mutated.
    assert plan[1]["target_mhz"] == 2415


def test_validated_floor_is_deepest_passed_probe() -> None:
    history = [
        SimpleNamespace(candidate_voltage_mv=915),
        SimpleNamespace(candidate_voltage_mv=850),
        SimpleNamespace(candidate_voltage_mv=870),
    ]
    assert validated_floor_voltage_mv(history, fallback_voltage_mv=925) == 850
    # Without history the final candidate voltage is the only proven point.
    assert validated_floor_voltage_mv([], fallback_voltage_mv=925) == 925
    assert validated_floor_voltage_mv(None, fallback_voltage_mv=870) == 870
