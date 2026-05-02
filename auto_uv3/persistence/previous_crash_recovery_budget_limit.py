"""Limit clock-recovery budget after a previous recovery attempt crashed.

The next run must not spend beyond the budget already proven unsafe by the crash marker.
"""

from __future__ import annotations


def recovery_budget_limit_after_crash_cache(
    unsafe_entries: list[dict],
    configured_budget_pct: float,
) -> float:
    effective_budget_pct = max(0.0, float(configured_budget_pct))
    for entry in unsafe_entries:
        if str(entry.get("reason", "")) != "previous-run-abruptly-ended":
            continue
        if str(entry.get("phase", "")) not in {
            "candidate-recovery",
            "final-recovery",
        }:
            continue
        marker_details = marker_details_from_unsafe_entry(entry)
        if not marker_details:
            continue
        used_before_pct = clock_recovery_budget_before_crash(marker_details)
        if used_before_pct is None:
            continue
        effective_budget_pct = min(
            effective_budget_pct,
            max(0.0, float(used_before_pct)),
        )
    return float(effective_budget_pct)


def marker_details_from_unsafe_entry(entry: dict) -> dict:
    details = entry.get("details")
    if not isinstance(details, dict):
        return {}
    marker_details = details.get("marker_details")
    return marker_details if isinstance(marker_details, dict) else {}


def clock_recovery_budget_before_crash(marker_details: dict) -> float | None:
    try:
        return float(marker_details["clock_bump_budget_used_before_pct"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        crashed_attempt = int(marker_details["clock_bump_attempt"])
    except (KeyError, TypeError, ValueError):
        return None
    return 0.0 if crashed_attempt <= 1 else None
