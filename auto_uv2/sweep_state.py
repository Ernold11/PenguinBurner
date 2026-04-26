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
    preserve_vanilla_below_mv: int | None,
    min_search_voltage_mv: int,
    failed_floor_voltage_mv: int | None = None,
) -> int | None:
    return _next_search_candidate_voltage_mv(
        plan=source_plan,
        start_voltage_mv=int(start_voltage_mv),
        stable_voltage_mv=int(stable_voltage_mv),
        reference_actual_voltage_mv=reference_actual_voltage_mv,
        preserve_vanilla_below_mv=preserve_vanilla_below_mv,
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
    preserve_vanilla_below_mv: int | None,
    min_search_voltage_mv: int,
    recovered_voltage_mv: int | None = None,
    recovered_target_mhz: int | None = None,
) -> AutoUv2SweepUpdate:
    if decision.action in {"accept", "accept-lowest-floor-miss"}:
        next_voltage_mv = None
        stop = decision.action == "accept-lowest-floor-miss"
        if not stop:
            # The v1 voltage picker may skip bins using measured voltage.
            next_voltage_mv = _next_voltage(
                source_plan,
                start_voltage_mv=int(start_voltage_mv),
                stable_voltage_mv=int(candidate.candidate_voltage_mv),
                reference_actual_voltage_mv=reference_actual_voltage_mv,
                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
                min_search_voltage_mv=int(min_search_voltage_mv),
            )
            stop = next_voltage_mv is None
        return AutoUv2SweepUpdate(
            state=replace(
                state,
                stable_voltage_mv=int(candidate.candidate_voltage_mv),
                stable_target_mhz=int(candidate.target_clock_mhz),
                candidate_voltage_mv=next_voltage_mv,
            ),
            stop=stop,
            write_latest_verified=not bool(probe.used_companion_load),
            reason=decision.reason,
        )

    if decision.action == "recover-upward" and recovered_voltage_mv is not None:
        # Recovery becomes stable while the failed voltage caps descent.
        next_voltage_mv = _next_voltage(
            source_plan,
            start_voltage_mv=int(start_voltage_mv),
            stable_voltage_mv=int(recovered_voltage_mv),
            reference_actual_voltage_mv=reference_actual_voltage_mv,
            preserve_vanilla_below_mv=preserve_vanilla_below_mv,
            min_search_voltage_mv=int(min_search_voltage_mv),
            failed_floor_voltage_mv=int(candidate.candidate_voltage_mv),
        )
        return AutoUv2SweepUpdate(
            state=replace(
                state,
                stable_voltage_mv=int(recovered_voltage_mv),
                stable_target_mhz=int(recovered_target_mhz or candidate.target_clock_mhz),
                candidate_voltage_mv=next_voltage_mv,
            ),
            stop=next_voltage_mv is None,
            write_latest_verified=not bool(probe.used_companion_load),
            reason=decision.reason,
        )

    return AutoUv2SweepUpdate(
        state=state,
        stop=True,
        write_latest_verified=False,
        reason=decision.reason,
    )
