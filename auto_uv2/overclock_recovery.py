from __future__ import annotations

from dataclasses import dataclass, replace

from auto_uv.clock_bump import (
    _make_clock_bump_candidate as _make_overclock_candidate,
    _next_clock_bump_target_mhz as _next_overclock_target_mhz,
)
from auto_uv.models import AutoUvCurveCandidate

from .candidate_decision import AutoUv2SweepState


@dataclass(frozen=True, slots=True)
class AutoUv2OverclockAttempt:
    candidate: AutoUvCurveCandidate
    state: AutoUv2SweepState
    old_target_mhz: int


def make_overclock_attempt(
    source_plan: list[dict],
    *,
    state: AutoUv2SweepState,
    failed_candidate: AutoUvCurveCandidate,
    reason: str,
    cap_clock_mhz: float,
) -> AutoUv2OverclockAttempt | None:
    if state.budget.spent_or_disabled:
        return None
    # Recovery only moves upward from the highest active target.
    old_target_mhz = max(
        int(failed_candidate.target_clock_mhz),
        int(state.last_overclock_target_mhz or 0),
    )
    new_target_mhz = _next_overclock_target_mhz(
        source_plan,
        current_clock_mhz=int(old_target_mhz),
        cap_clock_mhz=float(cap_clock_mhz),
        remaining_budget_pct=state.budget.remaining_pct,
        reason=str(reason),
    )
    if new_target_mhz is None:
        return None

    # Charge the actual snapped target increase, not the requested estimate.
    next_budget = state.budget.spend(
        old_target_mhz=int(old_target_mhz),
        new_target_mhz=int(new_target_mhz),
    )
    next_state = replace(
        state,
        budget=next_budget,
        last_overclock_target_mhz=int(new_target_mhz),
        overclock_count=int(state.overclock_count) + 1,
    )
    candidate = _make_overclock_candidate(
        source_plan,
        candidate_voltage_mv=int(failed_candidate.candidate_voltage_mv),
        target_clock_mhz=int(new_target_mhz),
        reason_label="low-clock-recovery",
        budget_used_pct=float(next_budget.used_pct),
        budget_limit_pct=float(next_budget.limit_pct),
    )
    return AutoUv2OverclockAttempt(
        candidate=candidate,
        state=next_state,
        old_target_mhz=int(old_target_mhz),
    )


def step_back_overclock_target(
    source_plan: list[dict],
    *,
    current_target_mhz: int,
    last_overclock_target_mhz: int | None,
) -> int | None:
    if last_overclock_target_mhz is None:
        return None
    # Hard failures back off by one real V/F grid target.
    lower_targets = sorted(
        {
            int(point["target_mhz"])
            for point in source_plan
            if int(point["target_mhz"]) < int(current_target_mhz)
        },
        reverse=True,
    )
    if lower_targets:
        return int(lower_targets[0])
    return None
