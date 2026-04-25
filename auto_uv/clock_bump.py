from __future__ import annotations

from .curve_planning import _choose_strictly_higher_clock_target, _make_curve_candidate
from .models import AutoUvCurveCandidate


def _next_clock_bump_target_mhz(
    plan: list[dict],
    *,
    current_clock_mhz: int,
    cap_clock_mhz: float,
) -> int | None:
    return _choose_strictly_higher_clock_target(
        plan,
        current_clock_mhz=int(current_clock_mhz),
        desired_clock_mhz=float(current_clock_mhz) * 1.02,
        cap_clock_mhz=float(cap_clock_mhz),
    )


def _make_clock_bump_candidate(
    source_plan: list[dict],
    *,
    candidate_voltage_mv: int,
    target_clock_mhz: int,
    reason_label: str,
) -> AutoUvCurveCandidate:
    return _make_curve_candidate(
        source_plan,
        candidate_voltage_mv=int(candidate_voltage_mv),
        target_clock_mhz=int(target_clock_mhz),
        label=f"voltage={int(candidate_voltage_mv)}mV {reason_label} +2.0%",
    )


def _clock_bump_marker_details(
    *,
    attempt: int,
    limit: int,
    previous_target_clock_mhz: int,
    bumped_target_clock_mhz: int,
    reason: str | None = None,
) -> dict:
    details = {
        "clock_bump_attempt": int(attempt),
        "clock_bump_limit": int(limit),
        "previous_target_clock_mhz": int(previous_target_clock_mhz),
        "bumped_target_clock_mhz": int(bumped_target_clock_mhz),
        "bump_pct": 2.0,
    }
    if reason is not None:
        details["reason"] = str(reason)
    return details
