from __future__ import annotations

from auto_uv.base_uv_loop import BaseUvLoopIO
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.efficiency_uv_loop import run_efficiency_uv_loop
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome

from auto_uv_test_data import base_curve


def _passing_outcome(raw_probe: object | None = None) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=True,
            failure_kind=FailureKind.NONE,
            severity=FailureSeverity.PASS,
            reason="stable",
        ),
        measured_core_clock_mhz=None,
        measured_voltage_mv=None,
        raw_probe=raw_probe,
        raw_result=None,
    )


def _initial_candidate(curve: list[dict], *, voltage_mv: int, lock_mhz: int) -> VfCurveCandidate:
    return VfCurveCandidate(
        label="baseline",
        voltage_mv=voltage_mv,
        target_mhz=lock_mhz,
        flattened_plan=build_flattened_plan(
            curve,
            lock_clock_mhz=lock_mhz,
            candidate_voltage_mv=voltage_mv,
            tail_rise_bins=0,
        ),
        metadata={"tail_rise_bins": 0},
    )


def _all_pass_io() -> BaseUvLoopIO:
    return BaseUvLoopIO(
        probe_candidate=lambda _candidate: _passing_outcome(),
        write_verified_candidate=lambda _candidate, _outcome: None,
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )


def _settings(*, auto_uv_mode: str = "efficiency", min_search_voltage_mv: int) -> AutoUvScanSettings:
    return AutoUvScanSettings(
        start_voltage_mv=1000,
        min_search_voltage_mv=min_search_voltage_mv,
        baseline_core_clock_mhz=None,
        auto_uv_mode=auto_uv_mode,
        min_core_clock_pct=85.0,
        reference_actual_voltage_mv=None,
        efficiency_stop_streak=0,
        min_efficiency_stop_voltage_drop_pct=100.0,
        tail_rise_bins=0,
    )


def _tail_targets_above_lock(candidate: VfCurveCandidate) -> list[int]:
    return [
        int(point["target_mhz"])
        for point in candidate.flattened_plan
        if int(point["voltage_mv"]) > int(candidate.voltage_mv)
        and not point.get("preserve_base")
    ]


def _recoverable_low_clock_outcome() -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=False,
            failure_kind=FailureKind.LOW_CLOCK,
            severity=FailureSeverity.RECOVERABLE,
            reason="average busy core clock below floor",
        ),
        measured_core_clock_mhz=None,
        measured_voltage_mv=None,
        raw_probe=None,
        raw_result=None,
    )


def test_first_pass_stops_at_clock_floor_and_tail_tune_descends_through() -> None:
    # The confirmed two-pass design: pass 1 (flat tail) stops at the first
    # recoverable low-clock probe instead of sweeping on with a sagging clock;
    # the tail-tune pass then owns the deep voltage region, where the raised
    # tail must hold the clock and low-clock bins are skipped, not fatal.
    curve = base_curve()
    probed: list[tuple[int, int]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        voltage_mv = int(candidate.voltage_mv)
        tail_bins = int(dict(candidate.metadata or {}).get("tail_rise_bins") or 0)
        probed.append((voltage_mv, tail_bins))
        if tail_bins == 0:
            floor_mv = 950  # flat curve sags below the clock floor under 950mV
        else:
            floor_mv = 850  # the raised tail holds the clock down to 850mV
        if voltage_mv >= floor_mv:
            return _passing_outcome()
        return _recoverable_low_clock_outcome()

    result = run_efficiency_uv_loop(
        curve,
        settings=_settings(min_search_voltage_mv=825),
        initial_stable_candidate=_initial_candidate(curve, voltage_mv=1000, lock_mhz=2240),
        io=BaseUvLoopIO(
            probe_candidate=probe,
            write_verified_candidate=lambda _candidate, _outcome: None,
            mark_unsafe_candidate=lambda _candidate, _outcome: None,
        ),
        min_search_voltage_mv=825,
        initial_tail_rise_bins=0,
        log=lambda _message: None,
    )

    flat_probes = [(v, bins) for v, bins in probed if bins == 0]
    tail_probes = [(v, bins) for v, bins in probed if bins == 2]

    # Pass 1 stopped at its first low-clock probe: exactly one flat probe below
    # the flat floor, and no flat probes after the tail-tune pass began.
    flat_low_clock = [v for v, _bins in flat_probes if v < 950]
    assert len(flat_low_clock) == 1
    assert probed.index((flat_low_clock[0], 0)) == len(flat_probes) - 1

    # The deep region was probed only with the raised tail, past where the
    # flat pass stopped, and the low-clock bins below 850 were skipped without
    # ending the sweep.
    assert tail_probes
    assert min(v for v, _bins in tail_probes) < min(v for v, _bins in flat_probes)
    assert any(v < 850 for v, _bins in tail_probes)

    candidate = result.stable_candidate
    assert candidate.voltage_mv == 850
    assert int(candidate.metadata["tail_rise_bins"]) == 2


def test_floor_reached_first_pass_still_ships_raised_tail() -> None:
    curve = base_curve()
    result = run_efficiency_uv_loop(
        curve,
        settings=_settings(min_search_voltage_mv=900),
        initial_stable_candidate=_initial_candidate(curve, voltage_mv=1000, lock_mhz=2240),
        io=_all_pass_io(),
        min_search_voltage_mv=900,
        initial_tail_rise_bins=0,
        log=lambda _message: None,
    )

    candidate = result.stable_candidate
    # The sweep descended to the floor voltage in pass 1, so tail tune never
    # ran; the final candidate must still carry the efficiency +2 rising tail.
    assert candidate.voltage_mv == 900
    assert int(candidate.metadata["tail_rise_bins"]) == 2
    lock_clock = int(candidate.target_mhz)
    tail = _tail_targets_above_lock(candidate)
    assert max(tail) == lock_clock + 30
    assert any(target > lock_clock for target in tail)


def test_tail_tune_pass_result_keeps_raised_tail_metadata() -> None:
    curve = base_curve()
    result = run_efficiency_uv_loop(
        curve,
        settings=_settings(min_search_voltage_mv=950),
        initial_stable_candidate=_initial_candidate(curve, voltage_mv=1000, lock_mhz=2240),
        io=_all_pass_io(),
        min_search_voltage_mv=825,
        initial_tail_rise_bins=0,
        log=lambda _message: None,
    )

    candidate = result.stable_candidate
    # Pass 1 stopped at 950 (its own floor); the tail-tune pass continued to
    # the efficiency floor with the raised tail built into every candidate.
    assert candidate.voltage_mv == 825
    assert int(candidate.metadata["tail_rise_bins"]) == 2
    tail = _tail_targets_above_lock(candidate)
    assert max(tail) == int(candidate.target_mhz) + 30


def test_raised_tail_rebuild_is_persisted_through_sweep_io() -> None:
    curve = base_curve()
    written: list[VfCurveCandidate] = []
    probe_marker = object()
    io = BaseUvLoopIO(
        probe_candidate=lambda _candidate: _passing_outcome(raw_probe=probe_marker),
        write_verified_candidate=lambda candidate, _outcome: written.append(candidate),
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_efficiency_uv_loop(
        curve,
        settings=_settings(min_search_voltage_mv=900),
        initial_stable_candidate=_initial_candidate(curve, voltage_mv=1000, lock_mhz=2240),
        io=io,
        min_search_voltage_mv=900,
        initial_tail_rise_bins=0,
        log=lambda _message: None,
    )

    # Crash recovery and the final-choice UI restore from the persisted
    # verified-candidate records, so the raised-tail rebuild must be written
    # back through the sweep IO, not only returned in memory.
    assert written
    assert written[-1] is result.stable_candidate
    assert int(written[-1].metadata["tail_rise_bins"]) == 2


def test_non_efficiency_mode_returns_first_pass_unchanged() -> None:
    curve = base_curve()
    result = run_efficiency_uv_loop(
        curve,
        settings=_settings(auto_uv_mode="balanced", min_search_voltage_mv=900),
        initial_stable_candidate=_initial_candidate(curve, voltage_mv=1000, lock_mhz=2240),
        io=_all_pass_io(),
        min_search_voltage_mv=900,
        initial_tail_rise_bins=0,
        log=lambda _message: None,
    )

    candidate = result.stable_candidate
    assert int(candidate.metadata.get("tail_rise_bins", 0)) == 0
    tail = _tail_targets_above_lock(candidate)
    assert all(target == int(candidate.target_mhz) for target in tail)
