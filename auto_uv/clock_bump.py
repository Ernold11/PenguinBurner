from __future__ import annotations

import re

from .tuning import AUTO_UV_CURVE_TUNING, AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO
from .curve_planning import _choose_strictly_higher_clock_target, _make_curve_candidate
from .models import AutoUvCurveCandidate


_CLOCK_GUARDRAIL_RE = re.compile(
    r"(?:current|predicted)=(?P<current>-?\d+(?:\.\d+)?)MHz.*?floor=(?P<floor>-?\d+(?:\.\d+)?)MHz"
)


def _clock_bump_budget_pct(
    *,
    max_clock_drop_pct: float,
    bump_budget_ratio: float,
    max_budget_ratio: float = AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO,
) -> float:
    ratio = max(
        0.0,
        min(float(max_budget_ratio), float(bump_budget_ratio)),
    )
    return max(0.0, float(max_clock_drop_pct)) * float(ratio)


def _clock_bump_needed_pct(
    *,
    current_target_clock_mhz: int,
    reason: str | None,
    fallback_bump_pct: float | None = None,
) -> float:
    current_target = max(1.0, float(current_target_clock_mhz))
    match = _CLOCK_GUARDRAIL_RE.search(str(reason or ""))
    if match is None:
        if fallback_bump_pct is not None:
            return max(0.0, float(fallback_bump_pct))
        return (
            float(AUTO_UV_CURVE_TUNING.clock_step_mhz)
            / float(current_target)
            * 100.0
        )
    observed_clock_mhz = float(match.group("current"))
    floor_clock_mhz = float(match.group("floor"))
    shortfall_mhz = max(0.0, float(floor_clock_mhz) - float(observed_clock_mhz))
    requested_mhz = shortfall_mhz + float(AUTO_UV_CURVE_TUNING.clock_step_mhz)
    return float(requested_mhz) / float(current_target) * 100.0


def _clock_bump_consumed_pct(
    *,
    previous_target_clock_mhz: int,
    bumped_target_clock_mhz: int,
) -> float:
    previous = max(1.0, float(previous_target_clock_mhz))
    delta = max(0.0, float(bumped_target_clock_mhz) - float(previous_target_clock_mhz))
    return float(delta) / float(previous) * 100.0


def _format_clock_bump_budget(*, used_pct: float, limit_pct: float) -> str:
    used = max(0.0, float(used_pct))
    limit = max(0.0, float(limit_pct))
    return f"overclocking-budget={used:.2f}/{limit:.2f}%"


def _next_clock_bump_target_mhz(
    plan: list[dict],
    *,
    current_clock_mhz: int,
    cap_clock_mhz: float,
    remaining_budget_pct: float,
    reason: str | None = None,
    fallback_bump_pct: float | None = None,
) -> int | None:
    if float(remaining_budget_pct) <= 0.0:
        return None
    bump_pct = min(
        float(remaining_budget_pct),
        _clock_bump_needed_pct(
            current_target_clock_mhz=int(current_clock_mhz),
            reason=reason,
            fallback_bump_pct=fallback_bump_pct,
        ),
    )
    if float(bump_pct) <= 0.0:
        return None
    target = _choose_strictly_higher_clock_target(
        plan,
        current_clock_mhz=int(current_clock_mhz),
        desired_clock_mhz=float(current_clock_mhz)
        * (1.0 + max(0.0, float(bump_pct)) / 100.0),
        cap_clock_mhz=float(cap_clock_mhz),
    )
    if target is None:
        return None
    consumed_pct = _clock_bump_consumed_pct(
        previous_target_clock_mhz=int(current_clock_mhz),
        bumped_target_clock_mhz=int(target),
    )
    if float(consumed_pct) > float(remaining_budget_pct) + 1e-9:
        return None
    return int(target)


def _make_clock_bump_candidate(
    source_plan: list[dict],
    *,
    candidate_voltage_mv: int,
    target_clock_mhz: int,
    reason_label: str,
    budget_used_pct: float,
    budget_limit_pct: float,
) -> AutoUvCurveCandidate:
    return _make_curve_candidate(
        source_plan,
        candidate_voltage_mv=int(candidate_voltage_mv),
        target_clock_mhz=int(target_clock_mhz),
        label=(
            f"voltage={int(candidate_voltage_mv)}mV {reason_label} "
            + _format_clock_bump_budget(
                used_pct=float(budget_used_pct),
                limit_pct=float(budget_limit_pct),
            )
        ),
    )


def _clock_bump_marker_details(
    *,
    previous_target_clock_mhz: int,
    bumped_target_clock_mhz: int,
    budget_used_before_pct: float,
    budget_used_after_pct: float,
    budget_limit_pct: float,
    reason: str | None = None,
) -> dict:
    details = {
        "previous_target_clock_mhz": int(previous_target_clock_mhz),
        "bumped_target_clock_mhz": int(bumped_target_clock_mhz),
        "bump_pct": _clock_bump_consumed_pct(
            previous_target_clock_mhz=int(previous_target_clock_mhz),
            bumped_target_clock_mhz=int(bumped_target_clock_mhz),
        ),
        "clock_bump_budget_used_before_pct": float(budget_used_before_pct),
        "clock_bump_budget_used_after_pct": float(budget_used_after_pct),
        "clock_bump_budget_limit_pct": float(budget_limit_pct),
    }
    if reason is not None:
        details["reason"] = str(reason)
    return details
