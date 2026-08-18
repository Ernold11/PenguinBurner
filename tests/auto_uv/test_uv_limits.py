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
    assert ceiling.clock_mhz == 2950
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
    assert performance == pytest.approx(6.349206349206349)
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
    # 80% stored, less the fixed 12% efficiency reduction: 80 * 0.88 = 70.4.
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5060", profile_id="efficiency"
    ) == pytest.approx(70.4)


def test_power_limit_pct_reduces_savings_tiers_and_keeps_performance_full() -> None:
    # Blackwell: efficiency cap is the stored per-family value LESS the fixed
    # 12% efficiency reduction (88 * 0.88 = 77.44); balanced sits halfway
    # between that and full power ((77.44 + 100) / 2 = 88.72); performance keeps
    # the stock budget.
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="efficiency"
    ) == pytest.approx(77.44)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="balanced"
    ) == pytest.approx(88.72)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="performance"
    ) == pytest.approx(100.0)


def test_rtx_5090_power_limit_percentages_and_575w_tier_caps() -> None:
    from auto_uv.main_loop import adaptive_tier_power_limit_w

    gpu_name = "NVIDIA GeForce RTX 5090"
    percentages = {
        tier: uv_limit_power_limit_pct_for_gpu(gpu_name, profile_id=tier)
        for tier in ("efficiency", "balanced", "performance")
    }

    assert percentages == pytest.approx(
        {"efficiency": 74.8, "balanced": 87.4, "performance": 100.0}
    )
    assert {
        tier: adaptive_tier_power_limit_w(
            power_limit_pct=percentage,
            baseline_power_limit_w=575,
            scan_request_w=None,
            balanced_pct=percentages["balanced"],
        )
        for tier, percentage in percentages.items()
    } == {"efficiency": 430, "balanced": 503, "performance": 575}


def test_power_limit_pct_ampere_efficiency_takes_fixed_reduction() -> None:
    # 80% stored - 12% reduction = 70.4; balanced halfway to full = 85.2.
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="efficiency"
    ) == pytest.approx(70.4)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="balanced"
    ) == pytest.approx(85.2)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="performance"
    ) == pytest.approx(100.0)


def test_power_limit_pct_ada_stays_full_power_on_every_tier() -> None:
    # Ada is deliberately uncapped (stored efficiency == full power), so the
    # fixed reduction does not apply: you cannot lower a limit that is not
    # there. Every tier stays at full power.
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
