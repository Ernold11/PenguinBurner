from __future__ import annotations

import pytest

from auto_uv.scan_mode.uv_limits import (
    uv_limit_clock_drop_pct_for_gpu,
    uv_limit_profile_target_for_gpu,
    uv_limit_voltage_floor_target_for_gpu,
    voltage_drop_pct,
)


def test_5080_voltage_table_exposes_efficiency_floor_and_performance_ceiling() -> None:
    floor = uv_limit_voltage_floor_target_for_gpu("NVIDIA GeForce RTX 5080")
    ceiling = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 5080", "performance")

    assert floor is not None
    assert ceiling is not None
    assert floor.gpu_family == "RTX 5080"
    assert floor.voltage_mv == 850
    assert floor.clock_mhz == 2800
    assert ceiling.voltage_mv == 925
    assert ceiling.clock_mhz == 2980
    assert voltage_drop_pct(start_voltage_mv=1000, floor_voltage_mv=850) == pytest.approx(
        15.0
    )


def test_unlisted_gpu_has_no_voltage_table_match() -> None:
    assert uv_limit_voltage_floor_target_for_gpu("NVIDIA GeForce GTX 1080") is None
    assert uv_limit_profile_target_for_gpu("NVIDIA GeForce GTX 1080", "performance") is None
    assert uv_limit_clock_drop_pct_for_gpu("NVIDIA GeForce GTX 1080") is None


def test_clock_drop_uses_preset_aware_gpu_table_ratio() -> None:
    efficiency = uv_limit_clock_drop_pct_for_gpu("NVIDIA GeForce RTX 5080")
    performance = uv_limit_clock_drop_pct_for_gpu(
        "NVIDIA GeForce RTX 5080",
        profile_id="performance",
    )
    assert efficiency is not None and performance is not None
    assert efficiency == pytest.approx(11.111111111111116)
    assert performance == pytest.approx(5.3968253968254)
    # Balanced is a savings-biased blend (0.6 efficiency / 0.4 performance) of
    # the two presets, so it stays centered-but-deeper on every GPU instead of
    # collapsing toward a neighbour when the clock geometry is tight.
    assert uv_limit_clock_drop_pct_for_gpu(
        "NVIDIA GeForce RTX 5080",
        profile_id="balanced",
    ) == pytest.approx(efficiency * 0.6 + performance * 0.4)
    assert uv_limit_clock_drop_pct_for_gpu(
        "NVIDIA GeForce RTX 5090"
    ) == pytest.approx(12.903225806451612)


def test_target_matching_keeps_ti_super_before_base_4070() -> None:
    target = uv_limit_profile_target_for_gpu(
        "NVIDIA GeForce RTX 4070 Ti SUPER",
        "performance",
    )

    assert target is not None
    assert target.gpu_family == "RTX 4070 Ti Super"
    assert target.clock_mhz == 2730


def test_4070_ti_performance_target_matches_reference_table() -> None:
    target = uv_limit_profile_target_for_gpu(
        "NVIDIA GeForce RTX 4070 Ti",
        "performance",
    )

    assert target is not None
    assert target.gpu_family == "RTX 4070 Ti"
    assert target.voltage_mv == 950
    assert target.clock_mhz == 2685


def test_3080_uses_ampere_table_values() -> None:
    floor = uv_limit_voltage_floor_target_for_gpu("NVIDIA GeForce RTX 3080")
    ceiling = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 3080", "performance")

    assert floor is not None
    assert ceiling is not None
    assert floor.gpu_family == "RTX 3080"
    assert floor.voltage_mv == 800
    assert floor.clock_mhz == 1750
    assert ceiling.voltage_mv == 900
    assert ceiling.clock_mhz == 1950


def test_3080_12gb_matches_before_base_3080() -> None:
    target = uv_limit_profile_target_for_gpu(
        "NVIDIA GeForce RTX 3080 12GB",
        "performance",
    )

    assert target is not None
    assert target.gpu_family == "RTX 3080 12GB"
    assert target.voltage_mv == 900
    assert target.clock_mhz == 1920
    assert (
        uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 3080 12GB", "max")
        is None
    )
