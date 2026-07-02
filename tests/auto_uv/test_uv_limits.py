from __future__ import annotations

import pytest

from auto_uv.scan_mode.uv_limits import (
    uv_limit_clock_drop_pct_for_gpu,
    uv_limit_power_limit_pct_for_gpu,
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


def test_rtx_5060_shares_the_5060_ti_vf_targets() -> None:
    # The lower-TBP GB206 cut reuses the 5060 Ti ladder and relies on the
    # efficiency power cap to stay inside its envelope.
    for profile in ("efficiency", "balanced", "performance"):
        base = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 5060", profile)
        ti = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 5060 Ti", profile)
        assert base is not None and ti is not None
        assert base.gpu_family == "RTX 5060"
        assert (base.voltage_mv, base.clock_mhz) == (ti.voltage_mv, ti.clock_mhz)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5060", profile_id="efficiency"
    ) == pytest.approx(80.0)


def test_power_limit_pct_reduces_savings_tiers_and_keeps_performance_full() -> None:
    # Blackwell: efficiency cap is the stored per-family value, balanced sits at
    # the shared middle cap, performance keeps the full board power budget.
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="efficiency"
    ) == pytest.approx(88.0)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="balanced"
    ) == pytest.approx(90.0)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="performance"
    ) == pytest.approx(100.0)


def test_power_limit_pct_ampere_uses_80_90_100_ladder() -> None:
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="efficiency"
    ) == pytest.approx(80.0)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="balanced"
    ) == pytest.approx(90.0)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="performance"
    ) == pytest.approx(100.0)


def test_power_limit_pct_ada_stays_full_power_on_every_tier() -> None:
    # Ada runs uncapped efficiency, so balanced must not drop to the middle cap.
    for profile in ("efficiency", "balanced", "performance"):
        assert uv_limit_power_limit_pct_for_gpu(
            "NVIDIA GeForce RTX 4090", profile_id=profile
        ) == pytest.approx(100.0)


def test_power_limit_pct_unlisted_gpu_returns_none() -> None:
    assert uv_limit_power_limit_pct_for_gpu("NVIDIA GeForce GTX 1080") is None


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
