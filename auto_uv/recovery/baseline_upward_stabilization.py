"""Recover when the first flattened base-load curve is not stable.

It keeps the chosen load clock and walks upward through real base V/F voltage bins until a probe passes.
"""

from __future__ import annotations

from typing import Callable

from ..auto_uv_console_log import log_benchmark, log_phase
from ..auto_uv_types import AutoUvProbeSummary, VfCurveCandidate
from ..curve.base_vf_curve_voltage_bins import higher_editable_voltage_bins
from ..curve.rising_tail import tail_ceiling_clock_mhz
from ..q2rtx.q2rtx_cuda_probe_runner import Q2RtxCudaProbeRunner
from ..curve.vf_curve_flattening import build_flattened_plan
from ..voltage_sweep_state import VoltageProbeOutcome


def find_upward_stable_baseline_candidate(
    base_curve: list[dict],
    *,
    failed_candidate: VfCurveCandidate,
    minimum_candidate_voltage_mv: int | None,
    runner: Q2RtxCudaProbeRunner,
    clock_ceiling,
    probe_history: list[AutoUvProbeSummary],
    discovery_summary: AutoUvProbeSummary,
    log: Callable[[str], None],
    tail_rise_bins: int = 0,
) -> tuple[VfCurveCandidate | None, VoltageProbeOutcome | None]:
    recovery_floor_mv = int(minimum_candidate_voltage_mv or failed_candidate.voltage_mv)
    upward_bins = [
        int(value)
        for value in higher_editable_voltage_bins(base_curve, recovery_floor_mv - 1)
        if int(value) >= int(recovery_floor_mv)
    ]
    for recovery_voltage_mv in upward_bins:
        plan = build_flattened_plan(
            base_curve,
            lock_clock_mhz=int(failed_candidate.target_mhz),
            candidate_voltage_mv=int(recovery_voltage_mv),
            tail_rise_bins=int(tail_rise_bins),
        )
        candidate = VfCurveCandidate(
            label="baseline-upward-stabilized",
            voltage_mv=int(recovery_voltage_mv),
            target_mhz=int(failed_candidate.target_mhz),
            flattened_plan=plan,
        )
        log_phase(
            log,
            "stabilize",
            f"try={candidate.voltage_mv}mV@{candidate.target_mhz}MHz",
        )
        if clock_ceiling is not None:
            clock_ceiling.retarget(
                lock_clock_mhz=int(candidate.target_mhz),
                lock_voltage_mv=int(candidate.voltage_mv),
                ceiling_clock_mhz=tail_ceiling_clock_mhz(
                    plan,
                    fallback_clock_mhz=int(candidate.target_mhz),
                    lock_voltage_mv=int(candidate.voltage_mv),
                ),
            )
            log_phase(log, "ceiling", clock_ceiling.describe())
        outcome = runner.probe_candidate(
            candidate,
            stable_history=[],
            phase_label="stabilize",
            enforce_target_core_clock_floor=True,
            summarize_saturated_tail=False,
            use_power_limit_floor=False,
            use_companion_load=False,
        )
        if outcome.raw_probe is not None:
            probe_history.append(outcome.raw_probe)
            log_benchmark(
                log,
                phase="stabilize",
                probe=outcome.raw_probe,
                reference_probe=discovery_summary,
                reference_label="initial",
            )
        if outcome.decision.passed:
            return candidate, outcome
        log_phase(log, "stabilize", f"rejected {outcome.decision.reason}")
    return None, None
