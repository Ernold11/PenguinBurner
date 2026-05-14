from __future__ import annotations

import pytest

from auto_uv.recovery.clock_recovery_budget import next_recovery_budget_used_pct
from auto_uv.recovery.clock_recovery_target import choose_clock_recovery_target_mhz
from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    compare_temperature_normalized_fps_per_w,
    decide_efficiency_stop,
)
from auto_uv.scan_mode.performance_fps_score_policy import performance_score_from_values
from auto_uv_test_data import probe_summary, wide_base_curve


def test_clock_recovery_target_recovers_half_the_lost_clock() -> None:
    target_mhz = choose_clock_recovery_target_mhz(
        wide_base_curve(),
        measured_target_mhz=2450,
        baseline_clock_mhz=2735.0,
        max_clock_drop_pct=10.0,
        budget_used_pct=5.0,
        cap_clock_mhz=2735.0,
    )

    assert target_mhz == 2595


def test_clock_recovery_budget_advances_by_guardrail_shortfall() -> None:
    next_used_pct = next_recovery_budget_used_pct(
        current_used_pct=0.0,
        limit_pct=5.0,
        reason="telemetry-live-core_clock current=2450MHz floor=2460MHz",
        measured_target_mhz=2450,
        baseline_clock_mhz=2735.0,
        max_clock_drop_pct=10.0,
    )

    assert next_used_pct == pytest.approx(1.0)


def test_clock_recovery_can_target_above_baseline_when_budget_exceeds_full_drop() -> None:
    target_mhz = choose_clock_recovery_target_mhz(
        wide_base_curve(),
        measured_target_mhz=2450,
        baseline_clock_mhz=2735.0,
        max_clock_drop_pct=10.0,
        budget_used_pct=12.5,
        cap_clock_mhz=2735.0,
    )

    assert target_mhz > 2735
    assert target_mhz <= 2735 + ((2735 - 2450) * 0.25)


def test_efficiency_policy_temperature_normalizes_fps_per_w() -> None:
    delta = compare_temperature_normalized_fps_per_w(
        probe_summary(
            1000,
            clock_mhz=2200.0,
            fps=100.0,
            power_w=200.0,
            temperature_c=60.0,
        ),
        probe_summary(
            950,
            clock_mhz=2200.0,
            fps=100.0,
            power_w=190.0,
            temperature_c=65.0,
        ),
    )

    assert delta["improved"] is True
    assert delta["measured_voltage_close_to_requested"] is True


def test_efficiency_stop_waits_for_budget_before_reverting_to_previous_curve() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=12.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=3,
        required_extra_confirmations=1,
        pending_previous_curve=True,
        budget_spent_or_disabled=True,
        power_up_efficiency_down=True,
        efficiency_delta_pct=-0.2,
    )

    assert decision.should_stop
    assert not decision.use_current_curve
    assert decision.reason == "fps-per-watt wall reached"


def test_performance_score_penalizes_fps_loss_harder_than_efficiency_gain() -> None:
    score = performance_score_from_values(
        fps=95.0,
        baseline_fps=100.0,
        fps_per_w=0.70,
        baseline_fps_per_w=0.50,
    )

    assert score is not None
    assert score < 100.0
