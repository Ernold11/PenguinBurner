"""Performance Auto-UV preset entry point.

Performance runs the shared base undervolt sweep first. After the base stable
candidate is known, the existing Auto-OC ladder can search higher clock targets
before final verification.
"""

from __future__ import annotations

from typing import Callable

from auto_uv.domain.console_log import log_phase
from auto_uv.domain.types import AutoUvProbeSummary, VfCurveCandidate
from auto_uv.base_uv_loop import BaseUvLoopIO, run_base_uv_loop
from auto_uv.curve.performance_sweep_profile import (
    build_performance_sweep_profile_candidate,
)
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.probes.runner import AutoUvProbeRunner
from auto_uv.run.voltage_sweep_state import LowerVoltageSweepResult, VoltageProbeOutcome
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_PERFORMANCE


def run_performance_uv_loop(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    io: BaseUvLoopIO,
    unsafe_entries: list[dict] | None = None,
    initial_stable_outcome: VoltageProbeOutcome | None = None,
) -> LowerVoltageSweepResult:
    """Run the Performance preset's base undervolt pass.

    The high-clock Auto-OC ladder runs later, after the base undervolt candidate
    is known and before final verification.
    """

    return run_base_uv_loop(
        base_curve,
        settings=settings,
        initial_stable_candidate=initial_stable_candidate,
        io=io,
        unsafe_entries=unsafe_entries,
        initial_stable_outcome=initial_stable_outcome,
    )


def run_auto_oc_candidate_search(**kwargs):
    from auto_uv.auto_oc.search import run_auto_oc_candidate_search as run_search

    return run_search(**kwargs)


def select_performance_auto_oc_candidate(
    base_curve: list[dict],
    *,
    auto_uv_mode: str,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    stable_history: list[AutoUvProbeSummary] | None,
    runner: AutoUvProbeRunner,
    gpu_name: object | None,
    clock_ceiling,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tail_rise_bins: int = 0,
    target_voltage_mv: int | None = None,
    target_clock_mhz: int | None = None,
    measured_baseline_clock_mhz: float | int | None = None,
) -> tuple[list[dict], int, int, AutoUvProbeSummary | None, dict]:
    if str(auto_uv_mode) != AUTO_UV_MODE_PERFORMANCE:
        return (
            stable_plan,
            int(stable_voltage_mv),
            int(stable_lock_clock_mhz),
            stable_probe,
            {},
        )
    start_candidate = VfCurveCandidate(
        label="performance-auto-oc-start",
        voltage_mv=int(stable_voltage_mv),
        target_mhz=int(stable_lock_clock_mhz),
        flattened_plan=stable_plan,
    )
    result = run_auto_oc_candidate_search(
        base_curve=base_curve,
        start_candidate=start_candidate,
        start_probe=stable_probe,
        runner=runner,
        gpu_name=gpu_name,
        clock_ceiling=clock_ceiling,
        probe_history=probe_history,
        log=log,
        tail_rise_bins=int(tail_rise_bins),
        target_voltage_mv=target_voltage_mv,
        target_clock_mhz=target_clock_mhz,
        measured_baseline_clock_mhz=measured_baseline_clock_mhz,
    )
    if stable_history is not None:
        for attempt in getattr(result, "attempts", ()) or ():
            if (
                attempt.outcome.decision.passed
                and attempt.outcome.raw_probe is not None
            ):
                stable_history.append(attempt.outcome.raw_probe)
    selected = build_performance_sweep_profile_candidate(
        base_curve,
        selected_candidate=result.selected_candidate,
        stable_history=stable_history,
        auto_oc_attempts=getattr(result, "attempts", ()) or (),
    )
    if selected.metadata.get("profile_curve") == "performance-sweep":
        log_phase(
            log,
            "auto-oc",
            "profile-curve=performance-sweep "
            f"start={int(selected.metadata['profile_curve_start_voltage_mv'])}mV "
            f"anchors={int(selected.metadata['profile_curve_anchor_count'])}",
        )
    selected_changed = (
        int(selected.voltage_mv) != int(stable_voltage_mv)
        or int(selected.target_mhz) != int(stable_lock_clock_mhz)
    )
    auto_oc_metadata = performance_auto_oc_progress_metadata(
        endpoint=getattr(result, "endpoint", None),
        measured_baseline_clock_mhz=measured_baseline_clock_mhz,
        selected_clock_mhz=int(selected.target_mhz),
    )
    return (
        selected.flattened_plan,
        int(selected.voltage_mv),
        int(selected.target_mhz),
        None if selected_changed else stable_probe,
        auto_oc_metadata,
    )


def performance_auto_oc_progress_metadata(
    *,
    endpoint,
    measured_baseline_clock_mhz: float | int | None,
    selected_clock_mhz: int,
) -> dict:
    """Auto-OC offset metadata relative to the measured baseline clock."""
    if endpoint is None or measured_baseline_clock_mhz is None:
        return {}
    baseline_clock = float(measured_baseline_clock_mhz)
    endpoint_clock = int(endpoint.clock_mhz)
    limit_mhz = int(round(float(endpoint_clock) - baseline_clock))
    applied_mhz = int(round(float(selected_clock_mhz) - baseline_clock))
    return {
        "auto_oc": True,
        "auto_oc_baseline_clock_mhz": round(baseline_clock, 2),
        "auto_oc_target_clock_mhz": endpoint_clock,
        "auto_oc_applied_mhz": applied_mhz,
        "auto_oc_limit_mhz": limit_mhz,
    }
