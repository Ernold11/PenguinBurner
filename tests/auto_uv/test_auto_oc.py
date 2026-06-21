from __future__ import annotations

from pathlib import Path

from auto_uv.auto_oc.ladder import AutoOcStep, build_auto_oc_ladder
from auto_uv.auto_oc.scoring import auto_oc_probe_key
from auto_uv.auto_oc.search import AutoOcAttempt, run_auto_oc_candidate_search
from auto_uv.auto_uv_types import (
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.curve.performance_sweep_profile import (
    build_performance_sweep_profile_candidate,
)
from auto_uv.voltage_sweep_state import VoltageProbeOutcome
from auto_uv_test_data import base_curve


def _probe(
    voltage_mv: int,
    clock_mhz: int,
    *,
    fps: float = 100.0,
    q2rtx_clock_mhz: float | None = None,
) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(voltage_mv),
        lock_clock_mhz=int(clock_mhz),
        live_voltage_before_mv=int(voltage_mv),
        live_voltage_after_mv=int(voltage_mv),
        avg_voltage_mv=float(voltage_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=float(fps),
        min_fps=float(fps),
        max_fps=float(fps),
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=float(q2rtx_clock_mhz or clock_mhz),
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=10.0,
        watts_per_mhz=0.1,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
        q2rtx_avg_core_clock_mhz=float(q2rtx_clock_mhz or clock_mhz),
        loaded_p90_core_clock_mhz=float(q2rtx_clock_mhz or clock_mhz),
    )


def _passed_outcome(probe: AutoUvProbeSummary) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            True,
            FailureKind.NONE,
            FailureSeverity.PASS,
            "stable run",
        ),
        measured_core_clock_mhz=probe.avg_core_clock_mhz,
        measured_voltage_mv=probe.avg_voltage_mv,
        raw_probe=probe,
        raw_result=object(),
    )


def _failed_outcome(probe: AutoUvProbeSummary) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            False,
            FailureKind.LOW_CLOCK,
            FailureSeverity.RECOVERABLE,
            "average busy core clock below floor",
        ),
        measured_core_clock_mhz=probe.avg_core_clock_mhz,
        measured_voltage_mv=probe.avg_voltage_mv,
        raw_probe=probe,
        raw_result=object(),
    )


def test_auto_oc_ladder_interpolates_to_endpoint_without_exceeding_caps() -> None:
    curve = base_curve(875, 955, 5, 2400, 15)

    ladder = build_auto_oc_ladder(
        curve,
        start_voltage_mv=900,
        start_clock_mhz=2670,
        endpoint_voltage_mv=950,
        endpoint_clock_mhz=2745,
        max_steps=10,
    )

    assert 0 < len(ladder) <= 10
    assert (ladder[-1].voltage_mv, ladder[-1].target_mhz) == (950, 2745)
    assert all(step.voltage_mv <= 950 for step in ladder)
    assert all(step.target_mhz <= 2745 for step in ladder)
    assert all(step.target_mhz % 15 == 0 for step in ladder)
    assert len({(step.voltage_mv, step.target_mhz) for step in ladder}) == len(ladder)


def test_auto_oc_probe_key_uses_q2rtx_clock_before_fps() -> None:
    lower_fps_higher_clock = _probe(950, 2745, fps=80.0, q2rtx_clock_mhz=2735.0)
    higher_fps_lower_clock = _probe(925, 2670, fps=120.0, q2rtx_clock_mhz=2680.0)

    assert auto_oc_probe_key(
        lower_fps_higher_clock,
        voltage_mv=950,
        step_index=2,
    ) > auto_oc_probe_key(
        higher_fps_lower_clock,
        voltage_mv=925,
        step_index=1,
    )


def test_auto_oc_search_keeps_exploring_after_measured_clock_regression() -> None:
    curve = base_curve(875, 955, 5, 2400, 15)
    start = VfCurveCandidate("start", 925, 2670, curve)
    probes: list[AutoUvProbeSummary] = []
    tried: list[tuple[int, int]] = []

    class FakeRunner:
        def probe_candidate(self, candidate, **kwargs):
            assert kwargs["phase_label"] == "candidate"
            assert kwargs["enforce_target_core_clock_floor"] is False
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            if len(tried) == 1:
                return _failed_outcome(
                    _probe(
                        candidate.voltage_mv,
                        candidate.target_mhz,
                        q2rtx_clock_mhz=2660.0,
                    )
                )
            return _passed_outcome(
                _probe(
                    candidate.voltage_mv,
                    candidate.target_mhz,
                    fps=70.0,
                    q2rtx_clock_mhz=2735.0,
                )
            )

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(925, 2670, q2rtx_clock_mhz=2670.0),
        runner=FakeRunner(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=probes,
        log=lambda _message: None,
        tail_rise_bins=0,
        max_interpolation_steps=2,
        target_voltage_mv=950,
        target_clock_mhz=2745,
        measured_baseline_clock_mhz=2670,
    )

    assert tried == [(935, 2715), (950, 2745)]
    assert (result.selected_candidate.voltage_mv, result.selected_candidate.target_mhz) == (
        950,
        2745,
    )
    assert [attempt.candidate.metadata["auto_oc_applied_mhz"] for attempt in result.attempts] == [
        45,
        75,
    ]
    assert {attempt.candidate.metadata["auto_oc_limit_mhz"] for attempt in result.attempts} == {
        75,
    }


def test_performance_sweep_profile_uses_passed_auto_oc_anchors_below_selection() -> None:
    curve = base_curve(800, 1000, 25, 1000, 50)
    selected = build_flattened_voltage_probe_curve(
        curve,
        candidate_voltage_mv=950,
        target_clock_mhz=1450,
        label="performance-oc",
        tail_rise_bins=0,
    )
    attempts = [
        AutoOcAttempt(
            step=AutoOcStep(1, 850, 1300, 0.25),
            candidate=build_flattened_voltage_probe_curve(
                curve,
                candidate_voltage_mv=850,
                target_clock_mhz=1300,
                label="performance-oc 1",
            ),
            outcome=_passed_outcome(_probe(850, 1300)),
        ),
        AutoOcAttempt(
            step=AutoOcStep(2, 900, 1400, 0.50),
            candidate=build_flattened_voltage_probe_curve(
                curve,
                candidate_voltage_mv=900,
                target_clock_mhz=1400,
                label="performance-oc 2",
            ),
            outcome=_passed_outcome(_probe(900, 1400)),
        ),
        AutoOcAttempt(
            step=AutoOcStep(3, 950, 1450, 1.0),
            candidate=selected,
            outcome=_passed_outcome(_probe(950, 1450)),
        ),
    ]

    sweep = build_performance_sweep_profile_candidate(
        curve,
        selected_candidate=selected,
        stable_history=[_probe(800, 1250)],
        auto_oc_attempts=attempts,
    )

    by_voltage = {int(point["voltage_mv"]): point for point in sweep.flattened_plan}
    assert sweep.metadata["profile_curve"] == "performance-sweep"
    assert by_voltage[800]["target_mhz"] == 1250
    assert by_voltage[850]["target_mhz"] == 1300
    assert by_voltage[875]["target_mhz"] == 1350
    assert by_voltage[900]["target_mhz"] == 1400
    assert by_voltage[925]["target_mhz"] == 1425
    assert by_voltage[950]["target_mhz"] == 1450
