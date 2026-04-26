from __future__ import annotations

from pathlib import Path

from auto_uv.models import AutoUvProbeSummary
from auto_uv2 import (
    AutoUv2OverclockBudget,
    AutoUv2SweepState,
    apply_probe_decision,
    choose_next_candidate,
    classify_probe_result,
    decide_efficiency_stop,
    predict_clock_floor_miss,
)


def _plan() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": 2000 + index * 30,
            "target_mhz": 2000 + index * 30,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(range(800, 1025, 25))
    ]


def _probe(voltage_mv: int, clock_mhz: float) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(voltage_mv),
        lock_clock_mhz=int(round(clock_mhz)),
        live_voltage_before_mv=None,
        live_voltage_after_mv=int(voltage_mv),
        avg_voltage_mv=float(voltage_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=200.0,
        max_power_w=200.0,
        avg_temperature_c=60.0,
        max_temperature_c=60.0,
        avg_fan_speed_pct=40.0,
        max_fan_speed_pct=40.0,
        avg_core_clock_mhz=float(clock_mhz),
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=float(clock_mhz) / 200.0,
        watts_per_mhz=200.0 / float(clock_mhz),
        used_companion_load=False,
        result_reason="passed",
        log_path=Path("synthetic.log"),
    )


def test_auto_uv2_candidate_follows_source_curve_downward() -> None:
    choice = choose_next_candidate(
        _plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2240,
            candidate_voltage_mv=900,
            budget=AutoUv2OverclockBudget(limit_pct=0.0),
        ),
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2240.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2240.0,
    )

    assert choice is not None
    assert choice.candidate.target_clock_mhz == 2120
    assert choice.phase == "medium"


def test_auto_uv2_predicts_floor_miss_from_recent_slope() -> None:
    reason = predict_clock_floor_miss(
        [_probe(1000, 2400.0), _probe(950, 2250.0)],
        candidate_voltage_mv=900,
        initial_core_clock_mhz=2400.0,
        min_core_clock_pct=90.0,
    )

    assert reason == "predicted=2100.0MHz floor=2160.0MHz"


def test_auto_uv2_preemptive_overclock_spends_budget_once() -> None:
    choice = choose_next_candidate(
        _plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=950,
            stable_target_mhz=2240,
            candidate_voltage_mv=900,
            budget=AutoUv2OverclockBudget(used_pct=0.0, limit_pct=4.0),
        ),
        start_voltage_mv=1000,
        stable_history=[_probe(1000, 2400.0), _probe(950, 2250.0)],
        initial_core_clock_mhz=2400.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2240.0,
    )

    assert choice is not None
    assert choice.candidate.target_clock_mhz > 2120
    assert choice.state.overclock_count == 1
    assert choice.state.budget.used_pct > 0.0
    assert "overclocking-budget=" in choice.candidate.label


def test_auto_uv2_efficiency_stop_waits_for_budget_to_be_spent() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=12.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=2,
        required_extra_confirmations=1,
        pending_previous_curve=True,
        budget=AutoUv2OverclockBudget(used_pct=2.0, limit_pct=5.0),
        power_up_efficiency_down=False,
        efficiency_delta_pct=-0.1,
    )

    assert not decision.should_stop
    assert decision.reason == "budget still available"


def test_auto_uv2_efficiency_stop_finishes_after_confirmed_wall() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=12.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=2,
        required_extra_confirmations=1,
        pending_previous_curve=True,
        budget=AutoUv2OverclockBudget(used_pct=5.0, limit_pct=5.0),
        power_up_efficiency_down=False,
        efficiency_delta_pct=-0.1,
    )

    assert decision.should_stop
    assert decision.confirmations == 1
    assert not decision.use_current_curve


def test_auto_uv2_probe_decision_tries_overclock_for_low_clock_failure() -> None:
    decision = classify_probe_result(
        probe_success=False,
        probe_failure_reason="telemetry-live-core_clock current=2100MHz floor=2160MHz",
        evaluation_error=None,
        budget=AutoUv2OverclockBudget(used_pct=1.0, limit_pct=5.0),
        candidate_used_overclock=False,
    )

    assert decision.action == "try-overclock"
    assert not decision.should_back_off_overclock


def test_auto_uv2_probe_decision_recovers_upward_after_hard_failure() -> None:
    decision = classify_probe_result(
        probe_success=False,
        probe_failure_reason="timedemo-live-stall",
        evaluation_error=None,
        budget=AutoUv2OverclockBudget(used_pct=1.0, limit_pct=5.0),
        candidate_used_overclock=True,
    )

    assert decision.action == "recover-upward"
    assert decision.should_back_off_overclock


def test_auto_uv2_probe_decision_accepts_lowest_floor_miss_after_budget() -> None:
    decision = classify_probe_result(
        probe_success=True,
        probe_failure_reason=None,
        evaluation_error="core_clock-regression current=2140MHz floor=2160MHz",
        budget=AutoUv2OverclockBudget(used_pct=5.0, limit_pct=5.0),
        candidate_used_overclock=True,
    )

    assert decision.action == "accept-lowest-floor-miss"
    assert not decision.should_back_off_overclock


def test_auto_uv2_sweep_update_accepts_and_moves_to_next_voltage() -> None:
    state = AutoUv2SweepState(
        stable_voltage_mv=1000,
        stable_target_mhz=2240,
        candidate_voltage_mv=975,
        budget=AutoUv2OverclockBudget(limit_pct=0.0),
    )
    choice = choose_next_candidate(
        _plan(),
        state=state,
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2240.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2240.0,
    )
    assert choice is not None

    update = apply_probe_decision(
        _plan(),
        state=state,
        decision=classify_probe_result(
            probe_success=True,
            probe_failure_reason=None,
            evaluation_error=None,
            budget=state.budget,
            candidate_used_overclock=False,
        ),
        candidate=choice.candidate,
        probe=_probe(975, 2200.0),
        start_voltage_mv=1000,
        reference_actual_voltage_mv=975.0,
        preserve_vanilla_below_mv=None,
        min_search_voltage_mv=900,
    )

    assert update.state.stable_voltage_mv == 975
    assert update.state.candidate_voltage_mv is not None
    assert 900 <= update.state.candidate_voltage_mv < 975
    assert update.write_latest_verified
    assert not update.stop


def test_auto_uv2_sweep_update_stops_on_lowest_floor_miss() -> None:
    state = AutoUv2SweepState(
        stable_voltage_mv=950,
        stable_target_mhz=2240,
        candidate_voltage_mv=925,
        budget=AutoUv2OverclockBudget(used_pct=5.0, limit_pct=5.0),
    )
    choice = choose_next_candidate(
        _plan(),
        state=state,
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2240.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2240.0,
    )
    assert choice is not None

    update = apply_probe_decision(
        _plan(),
        state=state,
        decision=classify_probe_result(
            probe_success=True,
            probe_failure_reason=None,
            evaluation_error="core_clock-regression current=2000MHz floor=2016MHz",
            budget=state.budget,
            candidate_used_overclock=True,
        ),
        candidate=choice.candidate,
        probe=_probe(925, 2000.0),
        start_voltage_mv=1000,
        reference_actual_voltage_mv=925.0,
        preserve_vanilla_below_mv=None,
        min_search_voltage_mv=900,
    )

    assert update.stop
    assert update.state.stable_voltage_mv == 925
