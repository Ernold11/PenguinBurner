"""Retry final verification with a budgeted clock recovery when only the clock floor failed.

This module uses the same budget units as the lower-voltage sweep: percent of the allowed clock drop.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..auto_uv_types import FailureKind, StableRunDecision, VfCurveCandidate
from ..recovery.clock_recovery_budget import (
    charge_recovery_budget_for_target,
    next_recovery_budget_used_pct,
)
from ..recovery.clock_recovery_target import choose_clock_recovery_target_mhz
from ..curve.vf_curve_flattening import build_flattened_plan
from ..q2rtx.probe_runtime_guardrails import final_failure_can_accept_budget_curve


@dataclass(frozen=True, slots=True)
class FinalClockRecoveryAttempt:
    candidate: VfCurveCandidate
    budget_used_pct: float
    marker_details: dict


def final_failure_allows_clock_recovery(
    decision: StableRunDecision,
    *,
    raw_reason: str,
) -> bool:
    return decision.failure_kind is FailureKind.LOW_CLOCK or final_failure_can_accept_budget_curve(
        raw_reason
    )


def build_final_clock_recovery_candidate(
    base_curve: list[dict],
    *,
    voltage_mv: int,
    previous_target_mhz: int,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
    current_budget_used_pct: float,
    budget_limit_pct: float,
    clock_cap_mhz: float | None,
    reason: str,
) -> FinalClockRecoveryAttempt | None:
    next_used_pct = next_recovery_budget_used_pct(
        current_used_pct=float(current_budget_used_pct),
        limit_pct=float(budget_limit_pct),
        reason=str(reason),
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if next_used_pct is None:
        return None
    target_mhz = choose_clock_recovery_target_mhz(
        base_curve,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
        budget_used_pct=float(next_used_pct),
        cap_clock_mhz=clock_cap_mhz,
        minimum_target_mhz=int(previous_target_mhz) + 15,
    )
    charged_pct = charge_recovery_budget_for_target(
        measured_target_mhz=int(measured_target_mhz),
        recovered_target_mhz=int(target_mhz),
        requested_used_pct=float(next_used_pct),
        limit_pct=float(budget_limit_pct),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if charged_pct is None or int(target_mhz) <= int(previous_target_mhz):
        return None
    return FinalClockRecoveryAttempt(
        candidate=VfCurveCandidate(
            label=(
                f"final-clock-recovery {int(voltage_mv)}mV "
                f"recovery-budget={float(charged_pct):.2f}/"
                f"{float(budget_limit_pct):.2f}%"
            ),
            voltage_mv=int(voltage_mv),
            target_mhz=int(target_mhz),
            flattened_plan=build_flattened_plan(
                base_curve,
                lock_clock_mhz=int(target_mhz),
                candidate_voltage_mv=int(voltage_mv),
            ),
        ),
        budget_used_pct=float(charged_pct),
        marker_details=clock_recovery_marker_details(
            previous_target_mhz=int(previous_target_mhz),
            recovered_target_mhz=int(target_mhz),
            budget_used_before_pct=float(current_budget_used_pct),
            budget_used_after_pct=float(charged_pct),
            budget_limit_pct=float(budget_limit_pct),
            reason=str(reason),
        ),
    )


def clock_recovery_marker_details(
    *,
    previous_target_mhz: int,
    recovered_target_mhz: int,
    budget_used_before_pct: float,
    budget_used_after_pct: float,
    budget_limit_pct: float,
    reason: str,
) -> dict:
    return {
        "previous_target_clock_mhz": int(previous_target_mhz),
        "bumped_target_clock_mhz": int(recovered_target_mhz),
        "clock_bump_budget_used_before_pct": float(budget_used_before_pct),
        "clock_bump_budget_used_after_pct": float(budget_used_after_pct),
        "clock_bump_budget_limit_pct": float(budget_limit_pct),
        "reason": str(reason),
    }


def format_clock_recovery_budget(*, used_pct: float, limit_pct: float) -> str:
    return f"recovery-budget={max(0.0, float(used_pct)):.2f}/{max(0.0, float(limit_pct)):.2f}%"
