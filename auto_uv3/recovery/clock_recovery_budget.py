"""Track how much lost clock Auto-UV3 may recover while sweeping lower voltage.

Budget is measured as percent of the configured allowed clock drop, not raw MHz.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


CLOCK_GUARDRAIL_RE = re.compile(
    r"(?:current|predicted)=(?P<current>-?\d+(?:\.\d+)?)MHz"
    r".*?floor=(?P<floor>-?\d+(?:\.\d+)?)MHz"
)


@dataclass(frozen=True, slots=True)
class ClockRecoveryBudgetRules:
    clock_step_mhz: int = 15
    minimum_budget_step_fraction: float = 0.10


def max_clock_drop_pct_for_min_core_clock(min_core_clock_pct: float) -> float:
    return max(0.0, 100.0 - float(min_core_clock_pct))


def recovery_budget_pct_for_target(
    *,
    measured_target_mhz: int,
    recovered_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
) -> float:
    measured = float(measured_target_mhz)
    baseline = float(baseline_clock_mhz or measured)
    target = float(recovered_target_mhz)
    lost_clock_mhz = max(0.0, baseline - measured)
    if lost_clock_mhz <= 0.0 or target <= measured:
        return 0.0
    recovered_fraction = max(0.0, (target - measured) / lost_clock_mhz)
    return recovered_fraction * max(0.0, float(max_clock_drop_pct))


def recovery_budget_snap_tolerance_pct(
    *,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
    rules: ClockRecoveryBudgetRules = ClockRecoveryBudgetRules(),
) -> float:
    lost_clock_mhz = max(
        0.0,
        float(baseline_clock_mhz or measured_target_mhz) - float(measured_target_mhz),
    )
    if lost_clock_mhz <= 0.0:
        return 0.0
    return (
        float(rules.clock_step_mhz)
        / lost_clock_mhz
        * max(0.0, float(max_clock_drop_pct))
    )


def charged_recovery_budget_pct(
    *,
    consumed_pct: float,
    requested_used_pct: float,
    limit_pct: float,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
    rules: ClockRecoveryBudgetRules = ClockRecoveryBudgetRules(),
) -> float | None:
    limit = max(0.0, float(limit_pct))
    tolerance = recovery_budget_snap_tolerance_pct(
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
        rules=rules,
    )
    if float(consumed_pct) > limit + tolerance + 1e-9:
        return None
    return min(limit, max(float(requested_used_pct), float(consumed_pct)))


def charge_recovery_budget_for_target(
    *,
    measured_target_mhz: int,
    recovered_target_mhz: int,
    requested_used_pct: float,
    limit_pct: float,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
) -> float | None:
    consumed_pct = recovery_budget_pct_for_target(
        measured_target_mhz=int(measured_target_mhz),
        recovered_target_mhz=int(recovered_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    return charged_recovery_budget_pct(
        consumed_pct=float(consumed_pct),
        requested_used_pct=float(requested_used_pct),
        limit_pct=float(limit_pct),
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )


def next_recovery_budget_used_pct(
    *,
    current_used_pct: float,
    limit_pct: float,
    reason: str | None,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
    rules: ClockRecoveryBudgetRules = ClockRecoveryBudgetRules(),
) -> float | None:
    current_used = max(0.0, float(current_used_pct))
    limit = max(0.0, float(limit_pct))
    if current_used >= limit:
        return None

    measured = int(measured_target_mhz)
    baseline = float(baseline_clock_mhz or measured)
    lost_clock_mhz = max(0.0, baseline - float(measured))
    max_drop_pct = max(0.0, float(max_clock_drop_pct))
    if lost_clock_mhz <= 0.0 or max_drop_pct <= 0.0:
        return None

    requested_mhz = requested_recovery_mhz(
        reason=reason,
        measured_target_mhz=measured,
        baseline_clock_mhz=baseline,
        rules=rules,
    )
    requested_pct = requested_mhz / lost_clock_mhz * max_drop_pct
    minimum_step_pct = max_drop_pct * max(0.0, float(rules.minimum_budget_step_fraction))
    next_used = current_used + max(requested_pct, minimum_step_pct)
    return min(limit, next_used)


def requested_recovery_mhz(
    *,
    reason: str | None,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    rules: ClockRecoveryBudgetRules = ClockRecoveryBudgetRules(),
) -> float:
    lost_clock_mhz = max(
        0.0,
        float(baseline_clock_mhz or measured_target_mhz) - float(measured_target_mhz),
    )
    fallback_mhz = max(float(rules.clock_step_mhz), lost_clock_mhz * 0.10)
    match = CLOCK_GUARDRAIL_RE.search(str(reason or ""))
    if match is None:
        return fallback_mhz
    observed_clock_mhz = float(match.group("current"))
    floor_clock_mhz = float(match.group("floor"))
    shortfall_mhz = max(0.0, floor_clock_mhz - observed_clock_mhz)
    return max(fallback_mhz, shortfall_mhz + float(rules.clock_step_mhz))
