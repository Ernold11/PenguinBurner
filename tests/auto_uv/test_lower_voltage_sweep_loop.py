from __future__ import annotations

from auto_uv.auto_uv_scan_settings import AutoUvScanSettings
from auto_uv.auto_uv_types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.lower_voltage_sweep_loop import (
    LowerVoltageSweepHooks,
    run_lower_voltage_sweep_loop,
)
from auto_uv.voltage_sweep_state import VoltageProbeOutcome
from auto_uv_test_data import base_curve, probe_summary


def _passed_outcome(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=True,
            failure_kind=FailureKind.NONE,
            severity=FailureSeverity.PASS,
            reason="stable run",
        ),
        measured_core_clock_mhz=float(candidate.target_mhz),
        measured_voltage_mv=float(candidate.voltage_mv),
        raw_probe=probe_summary(
            candidate.voltage_mv,
            clock_mhz=float(candidate.target_mhz),
        ),
    )


def test_lower_voltage_sweep_loop_accepts_next_lower_voltage_through_hooks() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[int] = []
    written: list[int] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(int(candidate.voltage_mv))
        return _passed_outcome(candidate)

    hooks = LowerVoltageSweepHooks(
        probe_candidate=probe,
        write_verified_candidate=lambda candidate, _outcome: written.append(
            int(candidate.voltage_mv)
        ),
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2160.0,
            reference_actual_voltage_mv=1000.0,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=hooks,
    )

    assert probed == [950]
    assert written == [950]
    assert result.stable_candidate.voltage_mv == 950
    assert result.state.next_voltage_mv is None


def test_performance_mode_lower_sweep_uses_plain_lower_voltage_probe() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[tuple[int, int, str]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(
            (
                int(candidate.voltage_mv),
                int(candidate.target_mhz),
                candidate.label,
            )
        )
        return _passed_outcome(candidate)

    hooks = LowerVoltageSweepHooks(
        probe_candidate=probe,
        write_verified_candidate=lambda _candidate, _outcome: None,
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2160.0,
            auto_uv_mode="performance",
            reference_actual_voltage_mv=1000.0,
            tail_rise_bins=6,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=hooks,
    )

    assert len(probed) == 1
    assert probed[0][0] == 950
    assert "oc-budget" not in probed[0][2]


def test_low_clock_failure_stops_without_recovery_probe() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[int] = []
    unsafe: list[int] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(int(candidate.voltage_mv))
        return VoltageProbeOutcome(
            decision=StableRunDecision(
                passed=False,
                failure_kind=FailureKind.LOW_CLOCK,
                severity=FailureSeverity.RECOVERABLE,
                reason="telemetry-live-core_clock current=1900MHz floor=2000MHz",
            ),
            measured_core_clock_mhz=float(candidate.target_mhz - 120),
            measured_voltage_mv=float(candidate.voltage_mv),
            raw_probe=probe_summary(
                candidate.voltage_mv,
                clock_mhz=float(candidate.target_mhz - 120),
            ),
        )

    hooks = LowerVoltageSweepHooks(
        probe_candidate=probe,
        write_verified_candidate=lambda _candidate, _outcome: None,
        mark_unsafe_candidate=lambda candidate, _outcome: unsafe.append(
            int(candidate.voltage_mv)
        ),
    )
    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2160.0,
            auto_uv_mode="performance",
            reference_actual_voltage_mv=1000.0,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=hooks,
    )

    assert probed == [950]
    assert unsafe == [950]
    assert result.stable_candidate.voltage_mv == 1000
    assert [event.name for event in result.events] == ["stop"]
