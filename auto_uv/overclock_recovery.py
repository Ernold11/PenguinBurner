from __future__ import annotations

from dataclasses import dataclass, replace

from auto_uv.clock_bump import (
    _make_clock_bump_candidate as _make_overclock_candidate,
)
from auto_uv.models import AutoUvCurveCandidate

from .candidate_decision import (
    AutoUv2SweepState,
    _fix_full_budget_target,
    charged_overclock_budget_pct,
    next_overclock_budget_used_pct,
    overclock_budget_pct_for_target,
    overclock_recovery_target_mhz,
)
from .tuning import AUTO_UV_CURVE_TUNING


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
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
) -> AutoUv2OverclockAttempt | None:
    if state.budget.spent_or_disabled:
        return None
    # Recovery only moves upward from the highest active target.
    old_target_mhz = max(
        int(failed_candidate.target_clock_mhz),
        int(state.last_overclock_target_mhz or 0),
    )
    measured_target_mhz = (
        int(state.pending_measured_target_mhz)
        if state.pending_measured_target_mhz is not None
        else (
            int(state.stable_measured_target_mhz)
            if state.stable_measured_target_mhz is not None
            else int(failed_candidate.target_clock_mhz)
        )
    )
    next_used_pct = next_overclock_budget_used_pct(
        current_used_pct=float(state.budget.used_pct),
        limit_pct=float(state.budget.limit_pct),
        reason=str(reason),
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if next_used_pct is None:
        return None
    new_target_mhz = overclock_recovery_target_mhz(
        source_plan,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
        budget_used_pct=float(next_used_pct),
        cap_clock_mhz=float(cap_clock_mhz),
        minimum_target_mhz=int(old_target_mhz)
        + max(1, int(AUTO_UV_CURVE_TUNING.clock_step_mhz)),
    )
    consumed_pct = overclock_budget_pct_for_target(
        measured_target_mhz=int(measured_target_mhz),
        overclock_target_mhz=int(new_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    charged_pct = charged_overclock_budget_pct(
        consumed_pct=float(consumed_pct),
        requested_used_pct=float(next_used_pct),
        limit_pct=float(state.budget.limit_pct),
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if int(new_target_mhz) <= int(old_target_mhz) or charged_pct is None:
        return None

    next_budget = state.budget.with_measured_used_pct(float(charged_pct))
    next_state = replace(
        state,
        budget=next_budget,
        last_overclock_target_mhz=int(new_target_mhz),
        overclock_count=int(state.overclock_count) + 1,
    )
    new_target_mhz, next_state = _fix_full_budget_target(
        next_state,
        int(new_target_mhz),
    )
    next_state = replace(next_state, last_overclock_target_mhz=int(new_target_mhz))
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
