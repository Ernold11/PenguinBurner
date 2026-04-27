from __future__ import annotations

from pathlib import Path

import pytest

from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary
from auto_uv import AutoUv2OverclockBudget, AutoUv2SweepHooks, AutoUv2SweepState, run_sweep
from auto_uv.live_sweep import _candidate_log_context
from auto_uv.sweep import static_probe_result


def _plan() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": 2000 + index * 30,
            "target_mhz": 2000 + index * 30,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(range(900, 1025, 25))
    ]


def _probe(voltage_mv: int, clock_mhz: float = 2200.0) -> AutoUvProbeSummary:
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


def _candidate(voltage_mv: int = 1000, target_mhz: int = 2120) -> AutoUvCurveCandidate:
    return AutoUvCurveCandidate(
        label="stable",
        candidate_voltage_mv=int(voltage_mv),
        target_clock_mhz=int(target_mhz),
        plan=_plan(),
    )


def test_auto_uv2_live_log_context_uses_candidate_budget() -> None:
    candidate = AutoUvCurveCandidate(
        label="voltage=950mV low-clock-recovery overclocking-budget=2.25/6.00%",
        candidate_voltage_mv=950,
        target_clock_mhz=2100,
        plan=_plan(),
    )

    assert _candidate_log_context(candidate) == "overclocking-budget=2.25/6.00%"


def test_auto_uv2_sweep_accepts_until_no_lower_voltage() -> None:
    writes: list[int] = []

    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda candidate: (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(True),
        ),
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _probe, _reason: (None, None, None),
        write_latest_verified=lambda candidate, _probe: writes.append(
            int(candidate.candidate_voltage_mv)
        ),
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=0.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=950,
        hooks=hooks,
    )

    assert result.state.stable_voltage_mv < 1000
    assert writes
    assert result.stop_reason in {"no lower voltage bin", "probe passed"}


def test_auto_uv2_sweep_recovers_upward_after_probe_failure() -> None:
    recovery_candidate = AutoUvCurveCandidate(
        label="recovered",
        candidate_voltage_mv=1000,
        target_clock_mhz=2120,
        plan=_plan(),
    )
    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda candidate: (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(False, "timedemo-live-stall"),
        ),
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (
            recovery_candidate,
            _probe(1000, 2120.0),
            static_probe_result(True),
        ),
        write_latest_verified=lambda _candidate, _probe: None,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=0.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=950,
        hooks=hooks,
    )

    assert any(event.name == "recover" for event in result.events)
    assert result.state.stable_voltage_mv == 1000


def test_auto_uv2_sweep_uses_overclock_after_low_clock_failure() -> None:
    writes: list[int] = []
    table_calls: list[tuple[str, int, int | None]] = []

    def _probe_candidate(candidate):
        if "low-clock-recovery" in candidate.label:
            return (
                _probe(candidate.candidate_voltage_mv, 2000.0),
                static_probe_result(True),
            )
        return (
            _probe(candidate.candidate_voltage_mv, 2000.0),
            static_probe_result(
                False,
                "telemetry-live-core_clock current=2000MHz floor=2050MHz",
            ),
        )

    hooks = AutoUv2SweepHooks(
        probe_candidate=_probe_candidate,
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda candidate, _probe: writes.append(
            int(candidate.target_clock_mhz)
        ),
        log_probe_result=lambda _attempt, decision, _reason, probe, previous: table_calls.append(
            (
                str(decision),
                int(probe.candidate_voltage_mv),
                int(previous.candidate_voltage_mv) if previous is not None else None,
            )
        ),
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=5.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=975,
        hooks=hooks,
    )

    assert any(event.name == "overclock" for event in result.events)
    assert result.state.overclock_count == 1
    assert writes
    assert result.state.stable_target_mhz == writes[0]
    assert result.state.stable_target_mhz > int(result.stable_probe.avg_core_clock_mhz or 0)
    assert [call[0] for call in table_calls] == ["try-overclock", "accept"]
    assert all(call[2] == 1000 for call in table_calls)


def test_auto_uv2_sweep_treats_failed_probe_fps_collapse_as_hard_regression() -> None:
    baseline_probe = _probe(1000, 2120.0)
    baseline_probe.avg_fps = 150.0
    failed_probe = _probe(975, 2050.0)
    failed_probe.avg_fps = 70.0

    def _evaluate(probe, history):
        baseline = history[0].avg_fps if history else None
        if baseline is not None and probe.avg_fps is not None and probe.avg_fps < baseline * 0.9:
            return (
                f"fps-regression current={probe.avg_fps:.1f} "
                f"baseline={baseline:.1f} floor={baseline * 0.9:.1f}"
            )
        return ""

    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda _candidate: (
            failed_probe,
            static_probe_result(
                False,
                "telemetry-live-core_clock current=2050MHz floor=2100MHz",
            ),
        ),
        evaluate_probe=_evaluate,
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=5.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=baseline_probe,
        stable_history=[baseline_probe],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=975,
        hooks=hooks,
    )

    assert any(
        event.name == "decision" and event.message == "recover-upward"
        for event in result.events
    )
    assert not any(event.name == "overclock" for event in result.events)
    assert result.state.overclock_count == 0


def test_auto_uv2_sweep_normalizes_accepted_candidate() -> None:
    def _normalize(candidate, probe):
        return AutoUvCurveCandidate(
            label=f"{candidate.label} normalized",
            candidate_voltage_mv=int(candidate.candidate_voltage_mv),
            target_clock_mhz=int(probe.avg_core_clock_mhz or candidate.target_clock_mhz),
            plan=candidate.plan,
        )

    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda candidate: (
            _probe(candidate.candidate_voltage_mv, 2060.0),
            static_probe_result(True),
        ),
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
        normalize_accepted_candidate=_normalize,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=0.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=975,
        hooks=hooks,
    )

    assert result.stable_candidate.target_clock_mhz == 2060
    assert result.state.stable_target_mhz == 2060


def test_auto_uv2_sweep_keeps_accepted_overclock_active_and_tracks_measured_target() -> None:
    def _probe_candidate(candidate):
        if "low-clock-recovery" in candidate.label:
            return (
                _probe(candidate.candidate_voltage_mv, 2050.0),
                static_probe_result(True),
            )
        return (
            _probe(candidate.candidate_voltage_mv, 2000.0),
            static_probe_result(
                False,
                "telemetry-live-core_clock current=2000MHz floor=2050MHz",
            ),
        )

    def _normalize(candidate, probe):
        return AutoUvCurveCandidate(
            label=f"{candidate.label} normalized",
            candidate_voltage_mv=int(candidate.candidate_voltage_mv),
            target_clock_mhz=int(probe.avg_core_clock_mhz or candidate.target_clock_mhz),
            plan=candidate.plan,
        )

    hooks = AutoUv2SweepHooks(
        probe_candidate=_probe_candidate,
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
        normalize_accepted_candidate=_normalize,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=5.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=975,
        hooks=hooks,
    )

    assert any(event.name == "overclock" for event in result.events)
    assert result.stable_candidate.target_clock_mhz == 2115
    assert result.state.stable_target_mhz == 2115
    assert result.state.stable_measured_target_mhz == 2050
    assert result.state.persistent_overclock_pct == pytest.approx(5.0)
    assert result.state.budget.used_pct == pytest.approx(5.0)


def test_auto_uv2_sweep_backs_off_overclock_after_hard_failure() -> None:
    def _probe_candidate(candidate):
        if "low-clock-recovery" in candidate.label:
            return (
                _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
                static_probe_result(False, "timedemo-live-stall"),
            )
        return (
            _probe(candidate.candidate_voltage_mv, 2000.0),
            static_probe_result(
                False,
                "telemetry-live-core_clock current=2000MHz floor=2050MHz",
            ),
        )

    hooks = AutoUv2SweepHooks(
        probe_candidate=_probe_candidate,
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=5.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=950,
        hooks=hooks,
    )

    assert any(event.name == "overclock-backoff" for event in result.events)
    assert result.state.last_overclock_target_mhz is not None
    assert result.state.last_overclock_target_mhz < 2120


def test_auto_uv2_sweep_rolls_back_fixed_full_budget_target_after_hard_failure() -> None:
    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda candidate: (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(False, "timedemo-live-stall"),
        ),
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            stable_measured_target_mhz=2000,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(used_pct=5.0, limit_pct=5.0),
            persistent_overclock_pct=5.0,
            full_budget_target_mhz=2120,
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=950,
        hooks=hooks,
        max_attempts=1,
    )

    assert any(event.name == "overclock-backoff" for event in result.events)
    assert result.state.full_budget_target_mhz is not None
    assert result.state.full_budget_target_mhz < 2120


def test_auto_uv2_sweep_stops_at_confirmed_efficiency_wall() -> None:
    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda candidate: (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(True),
        ),
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
        efficiency_delta=lambda _previous, _candidate: {
            "improved": False,
            "measured_voltage_close_to_requested": True,
            "delta_pct": -0.25,
        },
        power_up_efficiency_down=lambda _previous, _candidate, _delta: False,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=0.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=900,
        hooks=hooks,
        efficiency_stop_streak=1,
        min_efficiency_stop_voltage_drop_pct=0.0,
    )

    assert any(event.message == "fps-per-watt wall reached" for event in result.events)
    assert result.state.stable_voltage_mv == 1000


def test_auto_uv2_sweep_tries_efficiency_wall_overclock_before_stopping() -> None:
    deltas = iter(
        [
            {
                "improved": False,
                "measured_voltage_close_to_requested": True,
                "delta_pct": -0.25,
            },
            {
                "improved": True,
                "measured_voltage_close_to_requested": True,
                "delta_pct": 1.0,
            },
        ]
    )

    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda candidate: (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(True),
        ),
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
        efficiency_delta=lambda _previous, _candidate: next(deltas),
        power_up_efficiency_down=lambda _previous, _candidate, _delta: False,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=5.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=975,
        hooks=hooks,
        efficiency_stop_streak=1,
        min_efficiency_stop_voltage_drop_pct=0.0,
    )

    assert any(event.name == "efficiency-overclock" for event in result.events)
    assert any(event.message == "accepted" for event in result.events)
    assert result.state.overclock_count == 1


def test_auto_uv2_sweep_waits_for_min_voltage_drop_before_efficiency_overclock() -> None:
    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda candidate: (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(True),
        ),
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _failed_probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
        efficiency_delta=lambda _previous, _candidate: {
            "improved": False,
            "measured_voltage_close_to_requested": True,
            "delta_pct": -0.25,
        },
        power_up_efficiency_down=lambda _previous, _candidate, _delta: False,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=5.0),
        ),
        stable_candidate=_candidate(),
        stable_probe=_probe(1000, 2120.0),
        stable_history=[_probe(1000, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=1000.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=975,
        hooks=hooks,
        efficiency_stop_streak=1,
        min_efficiency_stop_voltage_drop_pct=10.0,
    )

    assert not any(event.name == "efficiency-overclock" for event in result.events)
    assert result.state.overclock_count == 0


def test_auto_uv2_sweep_stops_when_no_lower_voltage_remains() -> None:
    hooks = AutoUv2SweepHooks(
        probe_candidate=lambda candidate: (
            _probe(candidate.candidate_voltage_mv, candidate.target_clock_mhz),
            static_probe_result(True),
        ),
        evaluate_probe=lambda _probe, _history: "",
        recover_upward=lambda _candidate, _probe, _reason: (None, None, None),
        write_latest_verified=lambda _candidate, _probe: None,
    )

    result = run_sweep(
        _plan(),
        initial_state=AutoUv2SweepState(
            stable_voltage_mv=975,
            stable_target_mhz=2120,
            candidate_voltage_mv=975,
            budget=AutoUv2OverclockBudget(limit_pct=0.0),
        ),
        stable_candidate=_candidate(975, 2120),
        stable_probe=_probe(975, 2120.0),
        stable_history=[_probe(975, 2120.0)],
        probe_history=[],
        start_voltage_mv=1000,
        initial_core_clock_mhz=2120.0,
        min_core_clock_pct=90.0,
        measured_clock_cap_mhz=2120.0,
        reference_actual_voltage_mv=975.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=950,
        hooks=hooks,
        max_attempts=3,
    )

    assert result.state.candidate_voltage_mv is None
    assert result.state.stable_voltage_mv == 950
