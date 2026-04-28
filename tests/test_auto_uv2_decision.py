from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary
from auto_uv import (
    AutoUv2OverclockBudget,
    AutoUv2SweepHooks,
    AutoUv2SweepState,
    apply_probe_decision,
    choose_next_candidate,
    classify_probe_result,
    decide_efficiency_stop,
    make_overclock_attempt,
    predict_clock_floor_miss,
)
from auto_uv.candidate_decision import (
    next_overclock_budget_used_pct,
    overclock_recovery_target_mhz,
)
from auto_uv.performance import AutoUvPerformanceBehavior
from auto_uv.sweep import static_probe_result
from auto_uv.sweep_behavior import AutoUvAcceptedCandidateContext


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


def _wide_plan() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": 2200 + index * 10,
            "target_mhz": 2200 + index * 10,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(range(800, 1300, 5))
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


def _candidate(voltage_mv: int, clock_mhz: int) -> AutoUvCurveCandidate:
    return AutoUvCurveCandidate(
        label=f"{int(voltage_mv)}mV@{int(clock_mhz)}MHz",
        candidate_voltage_mv=int(voltage_mv),
        target_clock_mhz=int(clock_mhz),
        plan=_wide_plan(),
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


def test_auto_uv2_candidate_uses_last_measured_clock_after_stable_probe() -> None:
    choice = choose_next_candidate(
        _wide_plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=950,
            stable_target_mhz=2520,
            stable_measured_target_mhz=2450,
            candidate_voltage_mv=900,
            budget=AutoUv2OverclockBudget(limit_pct=0.0),
        ),
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2735.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2735.0,
    )

    assert choice is not None
    assert choice.candidate.target_clock_mhz == 2450


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


def test_auto_uv2_candidate_does_not_pin_future_steps_to_last_overclock() -> None:
    choice = choose_next_candidate(
        _plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=950,
            stable_target_mhz=2240,
            candidate_voltage_mv=900,
            budget=AutoUv2OverclockBudget(used_pct=1.0, limit_pct=4.0),
            last_overclock_target_mhz=2300,
        ),
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2240.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2240.0,
    )

    assert choice is not None
    assert choice.candidate.target_clock_mhz == 2120


def test_auto_uv2_candidate_applies_persistent_recovery_budget() -> None:
    choice = choose_next_candidate(
        _plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=950,
            stable_target_mhz=2240,
            candidate_voltage_mv=900,
            budget=AutoUv2OverclockBudget(used_pct=1.5, limit_pct=4.0),
            persistent_overclock_pct=1.5,
        ),
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2240.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2240.0,
    )

    assert choice is not None
    assert choice.candidate.target_clock_mhz == 2145
    assert choice.state.overclock_count == 0
    assert choice.state.persistent_overclock_pct == 1.5


def test_auto_uv2_full_budget_recovers_half_of_lost_clock_to_baseline() -> None:
    target_mhz = overclock_recovery_target_mhz(
        _wide_plan(),
        measured_target_mhz=2450,
        baseline_clock_mhz=2735.0,
        max_clock_drop_pct=10.0,
        budget_used_pct=5.0,
        cap_clock_mhz=2735.0,
    )

    assert target_mhz == 2595


def test_auto_uv2_125_percent_budget_can_target_above_baseline() -> None:
    target_mhz = overclock_recovery_target_mhz(
        _wide_plan(),
        measured_target_mhz=2450,
        baseline_clock_mhz=2735.0,
        max_clock_drop_pct=10.0,
        budget_used_pct=12.5,
        cap_clock_mhz=2735.0,
    )

    assert target_mhz > 2735
    assert target_mhz <= 2735 + ((2735 - 2450) * 0.25)


def test_auto_uv2_recovery_budget_advances_by_recovery_fraction() -> None:
    next_used_pct = next_overclock_budget_used_pct(
        current_used_pct=0.0,
        limit_pct=5.0,
        reason="telemetry-live-core_clock current=2450MHz floor=2460MHz",
        measured_target_mhz=2450,
        baseline_clock_mhz=2735.0,
        max_clock_drop_pct=10.0,
    )

    assert next_used_pct == pytest.approx(1.0)


def test_auto_uv2_persistent_budget_does_not_regress_active_clock() -> None:
    choice = choose_next_candidate(
        _wide_plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=950,
            stable_target_mhz=2520,
            stable_measured_target_mhz=2450,
            candidate_voltage_mv=900,
            budget=AutoUv2OverclockBudget(used_pct=5.0, limit_pct=5.0),
            persistent_overclock_pct=5.0,
        ),
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2735.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2735.0,
    )

    assert choice is not None
    assert choice.candidate.target_clock_mhz > 2520


def test_auto_uv2_first_full_budget_candidate_fixes_future_targets() -> None:
    first = choose_next_candidate(
        _wide_plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=950,
            stable_target_mhz=2520,
            stable_measured_target_mhz=2450,
            candidate_voltage_mv=900,
            budget=AutoUv2OverclockBudget(used_pct=5.0, limit_pct=5.0),
            persistent_overclock_pct=5.0,
        ),
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2735.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2735.0,
    )
    assert first is not None
    assert first.state.full_budget_target_mhz == first.candidate.target_clock_mhz

    second = choose_next_candidate(
        _wide_plan(),
        state=replace(
            first.state,
            stable_voltage_mv=900,
            stable_target_mhz=int(first.candidate.target_clock_mhz),
            stable_measured_target_mhz=2400,
            candidate_voltage_mv=885,
            pending_measured_target_mhz=None,
        ),
        start_voltage_mv=1000,
        stable_history=[],
        initial_core_clock_mhz=2735.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2735.0,
    )

    assert second is not None
    assert second.candidate.target_clock_mhz == first.candidate.target_clock_mhz
    assert second.state.full_budget_target_mhz == first.candidate.target_clock_mhz


def test_auto_uv2_overclock_attempt_records_first_full_budget_target() -> None:
    failed_candidate = AutoUvCurveCandidate(
        label="voltage=900mV phase=medium",
        candidate_voltage_mv=900,
        target_clock_mhz=2520,
        plan=_wide_plan(),
    )

    attempt = make_overclock_attempt(
        _wide_plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=950,
            stable_target_mhz=2520,
            stable_measured_target_mhz=2450,
            candidate_voltage_mv=900,
            budget=AutoUv2OverclockBudget(used_pct=4.0, limit_pct=5.0),
        ),
        failed_candidate=failed_candidate,
        reason="telemetry-live-core_clock current=2400MHz floor=2600MHz",
        cap_clock_mhz=2735.0,
        baseline_clock_mhz=2735.0,
        max_clock_drop_pct=10.0,
    )

    assert attempt is not None
    assert attempt.state.budget.used_pct == pytest.approx(5.0)
    assert attempt.state.full_budget_target_mhz == attempt.candidate.target_clock_mhz


def test_performance_overclock_attempt_caps_one_step_budget() -> None:
    failed_candidate = AutoUvCurveCandidate(
        label="voltage=1010mV phase=coarse",
        candidate_voltage_mv=1010,
        target_clock_mhz=2625,
        plan=_wide_plan(),
    )

    attempt = AutoUvPerformanceBehavior(
        max_overclock_step_pct=2.0
    )._make_performance_overclock_attempt(
        _wide_plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=1010,
            stable_target_mhz=2625,
            stable_measured_target_mhz=2625,
            candidate_voltage_mv=990,
            budget=AutoUv2OverclockBudget(used_pct=0.0, limit_pct=11.5),
        ),
        failed_candidate=failed_candidate,
        reason="performance-fps current=2572.5MHz floor=2693.0MHz",
        cap_clock_mhz=2748.0,
        baseline_clock_mhz=2748.0,
        max_clock_drop_pct=10.0,
    )

    assert attempt is not None
    assert attempt.candidate.target_clock_mhz > 2625
    assert attempt.candidate.target_clock_mhz < 2745
    assert attempt.state.budget.used_pct < 4.0
    assert attempt.state.budget.limit_pct == pytest.approx(11.5)


def test_performance_default_overclock_attempt_spends_near_half_drop_budget() -> None:
    failed_candidate = AutoUvCurveCandidate(
        label="voltage=1010mV phase=coarse",
        candidate_voltage_mv=1010,
        target_clock_mhz=2625,
        plan=_wide_plan(),
    )

    attempt = AutoUvPerformanceBehavior()._make_performance_overclock_attempt(
        _wide_plan(),
        state=AutoUv2SweepState(
            stable_voltage_mv=1010,
            stable_target_mhz=2625,
            stable_measured_target_mhz=2625,
            candidate_voltage_mv=990,
            budget=AutoUv2OverclockBudget(used_pct=0.0, limit_pct=11.5),
        ),
        failed_candidate=failed_candidate,
        reason="performance-fps current=2572.5MHz floor=2693.0MHz",
        cap_clock_mhz=2748.0,
        baseline_clock_mhz=2748.0,
        max_clock_drop_pct=10.0,
    )

    assert attempt is not None
    assert attempt.candidate.target_clock_mhz == 2685
    assert attempt.state.budget.used_pct == pytest.approx(4.8780487805)
    assert attempt.state.budget.limit_pct == pytest.approx(11.5)


def test_performance_overclock_hard_failure_stops_before_lower_voltage() -> None:
    probed_points: list[tuple[int, int]] = []

    def probe_candidate(candidate: AutoUvCurveCandidate):
        probed_points.append(
            (int(candidate.candidate_voltage_mv), int(candidate.target_clock_mhz))
        )
        return (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(False, "fatal-q2rtx-output"),
        )

    hooks = AutoUv2SweepHooks(
        probe_candidate=probe_candidate,
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
        base_probe=_probe(1240, 2757.0),
    )
    behavior = AutoUvPerformanceBehavior()
    context = AutoUvAcceptedCandidateContext(
        source_plan=_wide_plan(),
        hooks=hooks,
        state=AutoUv2SweepState(
            stable_voltage_mv=885,
            stable_target_mhz=2895,
            stable_measured_target_mhz=2715,
            candidate_voltage_mv=875,
            budget=AutoUv2OverclockBudget(used_pct=12.85, limit_pct=17.5),
            persistent_overclock_pct=12.85,
        ),
        previous_stable_candidate=_candidate(895, 2880),
        previous_stable_probe=_probe(895, 2709.0),
        stable_candidate=_candidate(885, 2895),
        stable_probe=_probe(885, 2715.0),
        probed_candidate=_candidate(885, 2895),
        stable_history=[_probe(895, 2709.0), _probe(885, 2715.0)],
        probe_history=[],
        start_voltage_mv=1025,
        reference_actual_voltage_mv=875.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=850,
        measured_clock_cap_mhz=3180.0,
        initial_core_clock_mhz=3180.0,
        min_core_clock_pct=90.0,
        attempt_index=12,
    )

    result, score, events = behavior._try_performance_overclock(
        context,
        current_score=100.0,
        base_probe=_probe(1240, 2757.0),
    )

    assert probed_points
    assert result is not None
    assert score is None
    assert result.should_stop
    assert result.state.candidate_voltage_mv is None
    assert result.state.stable_voltage_mv > 885
    assert any("refusing lower voltage" in event.message for event in events)


def test_performance_voltage_recovery_skips_visited_voltage_target_pairs() -> None:
    probed_points: list[tuple[int, int]] = []

    def probe_candidate(candidate: AutoUvCurveCandidate):
        probed_points.append(
            (int(candidate.candidate_voltage_mv), int(candidate.target_clock_mhz))
        )
        return (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(True),
        )

    hooks = AutoUv2SweepHooks(
        probe_candidate=probe_candidate,
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
    )
    stable_history = [
        _probe(900, 2865.0),
        _probe(895, 2865.0),
        _probe(890, 2865.0),
        _probe(910, 2835.0),
        _probe(885, 2865.0),
    ]
    context = AutoUvAcceptedCandidateContext(
        source_plan=_wide_plan(),
        hooks=hooks,
        state=AutoUv2SweepState(
            stable_voltage_mv=885,
            stable_target_mhz=2865,
            candidate_voltage_mv=None,
            budget=AutoUv2OverclockBudget(used_pct=13.0, limit_pct=13.0),
        ),
        previous_stable_candidate=_candidate(890, 2865),
        previous_stable_probe=_probe(890, 2865.0),
        stable_candidate=_candidate(885, 2865),
        stable_probe=_probe(885, 2865.0),
        probed_candidate=_candidate(885, 2865),
        stable_history=stable_history,
        probe_history=list(stable_history),
        start_voltage_mv=1020,
        reference_actual_voltage_mv=885.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=850,
        measured_clock_cap_mhz=2865.0,
        initial_core_clock_mhz=2730.0,
        min_core_clock_pct=90.0,
        attempt_index=18,
    )

    result, events = AutoUvPerformanceBehavior()._sweep_higher_voltage_recovery(
        context,
        base_probe=_probe(1240, 2730.0),
    )

    assert result is not None
    assert probed_points == [(910, 2865)]
    assert any(event.message == "skip-visited 890mV@2865MHz" for event in events)
    assert any(event.message == "skip-visited 895mV@2865MHz" for event in events)
    assert any(event.message == "skip-visited 900mV@2865MHz" for event in events)


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


def test_auto_uv2_probe_decision_stops_at_critical_failure() -> None:
    decision = classify_probe_result(
        probe_success=False,
        probe_failure_reason="fatal-q2rtx-output",
        evaluation_error=None,
        budget=AutoUv2OverclockBudget(used_pct=10.0, limit_pct=15.0),
        candidate_used_overclock=True,
    )

    assert decision.action == "stop-critical"
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
        preserve_base_below_mv=None,
        min_search_voltage_mv=900,
    )

    assert update.state.stable_voltage_mv == 975
    assert update.state.candidate_voltage_mv is not None
    assert 900 <= update.state.candidate_voltage_mv < 975
    assert update.write_latest_verified
    assert not update.stop


def test_auto_uv2_sweep_update_persists_new_overclock_budget_stage() -> None:
    state = AutoUv2SweepState(
        stable_voltage_mv=950,
        stable_target_mhz=2120,
        candidate_voltage_mv=925,
        budget=AutoUv2OverclockBudget(used_pct=0.7, limit_pct=4.0),
        last_overclock_target_mhz=2150,
    )
    probed_candidate = AutoUvCurveCandidate(
        label="overclock probe",
        candidate_voltage_mv=925,
        target_clock_mhz=2150,
        plan=_plan(),
    )
    normalized_candidate = AutoUvCurveCandidate(
        label="accepted-real-clock",
        candidate_voltage_mv=925,
        target_clock_mhz=2120,
        plan=_plan(),
    )

    update = apply_probe_decision(
        _plan(),
        state=state,
        decision=classify_probe_result(
            probe_success=True,
            probe_failure_reason=None,
            evaluation_error=None,
            budget=state.budget,
            candidate_used_overclock=True,
        ),
        candidate=normalized_candidate,
        probe=_probe(925, 2118.0),
        start_voltage_mv=1000,
        reference_actual_voltage_mv=925.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=900,
        probed_candidate=probed_candidate,
        candidate_used_new_overclock=True,
    )

    assert update.state.last_overclock_target_mhz is None
    assert update.state.persistent_overclock_pct == pytest.approx(0.7)
    assert update.state.budget.used_pct == pytest.approx(0.7)


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
        preserve_base_below_mv=None,
        min_search_voltage_mv=900,
    )

    assert update.stop
    assert update.state.stable_voltage_mv == 925
