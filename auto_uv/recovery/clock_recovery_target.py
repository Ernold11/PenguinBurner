from __future__ import annotations

from dataclasses import dataclass

from ..curve.base_load_flatten_target import choose_sustained_curve_clock


@dataclass(frozen=True, slots=True)
class ClockRecoveryTargetRules:
    clock_step_mhz: int = 15


def choose_clock_recovery_target_mhz(
    base_curve: list[dict],
    *,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
    budget_used_pct: float,
    cap_clock_mhz: float | None = None,
    minimum_target_mhz: int | None = None,
    rules: ClockRecoveryTargetRules = ClockRecoveryTargetRules(),
) -> int:
    measured_target = int(measured_target_mhz)
    baseline_clock = float(baseline_clock_mhz or measured_target)
    if baseline_clock <= float(measured_target):
        return measured_target

    fraction = recovery_fraction(
        budget_used_pct=float(budget_used_pct),
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if fraction <= 0.0:
        return measured_target

    desired_clock_mhz = float(measured_target) + (
        (baseline_clock - float(measured_target)) * fraction
    )
    cap_clock = recovery_cap_clock_mhz(
        desired_clock_mhz=desired_clock_mhz,
        baseline_clock_mhz=baseline_clock,
        fraction=fraction,
        cap_clock_mhz=cap_clock_mhz,
    )
    if cap_clock <= float(measured_target):
        return measured_target

    target_mhz = choose_clock_target_at_or_above(
        base_curve,
        desired_clock_mhz=min(desired_clock_mhz, cap_clock),
        cap_clock_mhz=cap_clock,
        rules=rules,
    )
    if minimum_target_mhz is not None and int(minimum_target_mhz) > int(target_mhz):
        target_mhz = int(minimum_target_mhz)
        while float(target_mhz) > cap_clock:
            target_mhz -= int(rules.clock_step_mhz)
    return max(measured_target, int(target_mhz))


def recovery_fraction(*, budget_used_pct: float, max_clock_drop_pct: float) -> float:
    max_drop_pct = max(0.0, float(max_clock_drop_pct))
    if max_drop_pct <= 0.0:
        return 0.0
    return max(0.0, float(budget_used_pct) / max_drop_pct)


def recovery_cap_clock_mhz(
    *,
    desired_clock_mhz: float,
    baseline_clock_mhz: float,
    fraction: float,
    cap_clock_mhz: float | None,
) -> float:
    if float(fraction) <= 1.0:
        cap_clock = float(baseline_clock_mhz)
        if cap_clock_mhz is not None:
            cap_clock = min(cap_clock, float(cap_clock_mhz))
        return cap_clock

    cap_clock = float(desired_clock_mhz)
    if cap_clock_mhz is not None and float(cap_clock_mhz) > float(baseline_clock_mhz):
        cap_clock = min(cap_clock, float(cap_clock_mhz))
    return cap_clock


def choose_clock_target_at_or_above(
    base_curve: list[dict],
    *,
    desired_clock_mhz: float,
    cap_clock_mhz: float,
    rules: ClockRecoveryTargetRules = ClockRecoveryTargetRules(),
) -> int:
    step_mhz = max(1, int(rules.clock_step_mhz))
    cap_target_mhz = choose_sustained_curve_clock(base_curve, float(cap_clock_mhz))
    target_mhz = choose_sustained_curve_clock(base_curve, float(desired_clock_mhz))
    if float(target_mhz) < float(desired_clock_mhz):
        target_mhz += step_mhz
    while int(target_mhz) > int(cap_target_mhz) or float(target_mhz) > cap_clock_mhz:
        target_mhz -= step_mhz
    return max(step_mhz, int(target_mhz))
