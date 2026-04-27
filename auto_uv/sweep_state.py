from __future__ import annotations

from dataclasses import dataclass, replace

from auto_uv.curve_planning import _next_search_candidate_voltage_mv
from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary

from .candidate_decision import AutoUv2SweepState
from .probe_decision import AutoUv2ProbeDecision


@dataclass(frozen=True, slots=True)
class AutoUv2SweepUpdate:
    state: AutoUv2SweepState
    stop: bool
    write_latest_verified: bool
    reason: str


def _next_voltage(
    source_plan: list[dict],
    *,
    start_voltage_mv: int,
    stable_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
    preserve_base_below_mv: int | None,
    min_search_voltage_mv: int,
    failed_floor_voltage_mv: int | None = None,
) -> int | None:
    return _next_search_candidate_voltage_mv(
        plan=source_plan,
        start_voltage_mv=int(start_voltage_mv),
        stable_voltage_mv=int(stable_voltage_mv),
        reference_actual_voltage_mv=reference_actual_voltage_mv,
        preserve_base_below_mv=preserve_base_below_mv,
        min_search_voltage_mv=int(min_search_voltage_mv),
        failed_floor_voltage_mv=failed_floor_voltage_mv,
    )


def apply_probe_decision(
    source_plan: list[dict],
    *,
    state: AutoUv2SweepState,
    decision: AutoUv2ProbeDecision,
    candidate: AutoUvCurveCandidate,
    probe: AutoUvProbeSummary,
    start_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
    preserve_base_below_mv: int | None,
    min_search_voltage_mv: int,
    recovered_voltage_mv: int | None = None,
    recovered_target_mhz: int | None = None,
    probed_candidate: AutoUvCurveCandidate | None = None,
    candidate_used_new_overclock: bool = False,
    measured_target_mhz: int | None = None,
) -> AutoUv2SweepUpdate:
    accepted_measured_target_mhz = (
        int(measured_target_mhz)
        if measured_target_mhz is not None
        else (
            int(state.pending_measured_target_mhz)
            if state.pending_measured_target_mhz is not None
            else int(candidate.target_clock_mhz)
        )
    )
    next_persistent_overclock_pct = float(state.persistent_overclock_pct)
    next_budget = state.budget
    if bool(candidate_used_new_overclock):
        next_persistent_overclock_pct = min(
            float(state.budget.limit_pct),
            max(
                float(next_persistent_overclock_pct),
                float(state.budget.used_pct),
            ),
        )
        next_budget = state.budget.with_measured_used_pct(
            float(next_persistent_overclock_pct)
        )

    if decision.action in {"accept", "accept-lowest-floor-miss"}:
        next_voltage_mv = None
        stop = decision.action == "accept-lowest-floor-miss"
        if not stop:
            # The v1 voltage picker may skip bins using measured voltage.
            next_reference_actual_voltage_mv = (
                float(probe.avg_voltage_mv)
                if probe.avg_voltage_mv is not None
                else reference_actual_voltage_mv
            )
            next_voltage_mv = _next_voltage(
                source_plan,
                start_voltage_mv=int(start_voltage_mv),
                stable_voltage_mv=int(candidate.candidate_voltage_mv),
                reference_actual_voltage_mv=next_reference_actual_voltage_mv,
                preserve_base_below_mv=preserve_base_below_mv,
                min_search_voltage_mv=int(min_search_voltage_mv),
            )
            stop = next_voltage_mv is None
        return AutoUv2SweepUpdate(
            state=replace(
                state,
                stable_voltage_mv=int(candidate.candidate_voltage_mv),
                stable_target_mhz=int(candidate.target_clock_mhz),
                stable_measured_target_mhz=int(accepted_measured_target_mhz),
                candidate_voltage_mv=next_voltage_mv,
                budget=next_budget,
                last_overclock_target_mhz=None,
                persistent_overclock_pct=float(next_persistent_overclock_pct),
                pending_measured_target_mhz=None,
            ),
            stop=stop,
            write_latest_verified=True,
            reason=decision.reason,
        )

    if decision.action == "recover-upward" and recovered_voltage_mv is not None:
        # Recovery becomes stable while the failed voltage caps descent.
        next_reference_actual_voltage_mv = (
            float(probe.avg_voltage_mv)
            if probe.avg_voltage_mv is not None
            else reference_actual_voltage_mv
        )
        next_voltage_mv = _next_voltage(
            source_plan,
            start_voltage_mv=int(start_voltage_mv),
            stable_voltage_mv=int(recovered_voltage_mv),
            reference_actual_voltage_mv=next_reference_actual_voltage_mv,
            preserve_base_below_mv=preserve_base_below_mv,
            min_search_voltage_mv=int(min_search_voltage_mv),
            failed_floor_voltage_mv=int(candidate.candidate_voltage_mv),
        )
        return AutoUv2SweepUpdate(
            state=replace(
                state,
                stable_voltage_mv=int(recovered_voltage_mv),
                stable_target_mhz=int(recovered_target_mhz or candidate.target_clock_mhz),
                stable_measured_target_mhz=int(accepted_measured_target_mhz),
                candidate_voltage_mv=next_voltage_mv,
                budget=next_budget,
                last_overclock_target_mhz=None,
                persistent_overclock_pct=float(next_persistent_overclock_pct),
                pending_measured_target_mhz=None,
            ),
            stop=next_voltage_mv is None,
            write_latest_verified=True,
            reason=decision.reason,
        )

    return AutoUv2SweepUpdate(
        state=state,
        stop=True,
        write_latest_verified=False,
        reason=decision.reason,
    )
