"""Build UI fields for the clock-recovery budget bar.

The UI stores this as overclock-budget fields because old profiles already use that name.
"""

from __future__ import annotations


def clock_recovery_budget_ui_payload(
    *,
    used_pct: float | int | None,
    limit_pct: float | int | None,
    max_clock_drop_pct: float | int | None = None,
) -> dict:
    used = _rounded(used_pct)
    limit = _rounded(limit_pct)
    if used is None or limit is None:
        return {}
    ratio = 0.0 if float(limit) <= 0.0 else max(0.0, float(used) / float(limit))
    payload = {
        "overclock_budget_used_pct": used,
        "overclock_budget_limit_pct": limit,
        "overclock_budget_used_ratio": round(ratio, 4),
    }
    max_drop = _rounded(max_clock_drop_pct)
    if max_drop is not None and float(max_drop) > 0.0:
        payload.update(
            {
                "overclock_budget_clock_drop_pct": max_drop,
                "overclock_budget_used_of_clock_drop_pct": _rounded(
                    float(used) / float(max_drop) * 100.0
                ),
                "overclock_budget_limit_of_clock_drop_pct": _rounded(
                    float(limit) / float(max_drop) * 100.0
                ),
            }
        )
    return payload


def _rounded(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)
