from __future__ import annotations

from collections.abc import Callable
import re

from .models import AutoUvProbeSummary

AutoUvEventCallback = Callable[[str, dict], None]
_OVERCLOCK_BUDGET_RE = re.compile(
    r"\boverclocking-budget=(?P<used>[0-9]+(?:\.[0-9]+)?)/"
    r"(?P<limit>[0-9]+(?:\.[0-9]+)?)%"
)


def emit_event(
    callback: AutoUvEventCallback | None,
    event: str,
    **payload,
) -> None:
    if callback is None:
        return
    callback(str(event), _without_none_values(payload))


def _without_none_values(value):
    if isinstance(value, dict):
        return {
            key: _without_none_values(inner)
            for key, inner in value.items()
            if inner is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            _without_none_values(inner)
            for inner in value
            if inner is not None
        ]
    return value


def plan_event_points(plan: list[dict]) -> list[dict]:
    points = []
    for item in sorted(plan, key=lambda value: int(value["voltage_mv"])):
        points.append(
            {
                "voltage_mv": int(item["voltage_mv"]),
                "clock_mhz": int(item["target_mhz"]),
                "base_mhz": int(item["base_mhz"]),
                "offset_mhz": int(item["target_mhz"]) - int(item["base_mhz"]),
            }
        )
    return points


def probe_event_payload(
    probe: AutoUvProbeSummary,
    *,
    stage: str,
    decision: str = "",
    reason: str = "",
) -> dict:
    return {
        "stage": str(stage),
        "voltage_mv": int(probe.candidate_voltage_mv),
        "clock_mhz": int(probe.lock_clock_mhz),
        "measured_clock_mhz": _rounded(probe.avg_core_clock_mhz),
        "avg_core_clock_mhz": _rounded(probe.avg_core_clock_mhz),
        "avg_voltage_mv": _rounded(probe.avg_voltage_mv),
        "q2rtx_measured_clock_mhz": _rounded(probe.q2rtx_avg_core_clock_mhz),
        "q2rtx_measured_voltage_mv": _rounded(probe.q2rtx_avg_voltage_mv),
        "cuda_measured_clock_mhz": _rounded(probe.cuda_avg_core_clock_mhz),
        "cuda_measured_voltage_mv": _rounded(probe.cuda_avg_voltage_mv),
        "used_companion_load": bool(probe.used_companion_load),
        "fps": _rounded(probe.avg_fps),
        "power_w": _rounded(probe.avg_power_w),
        "temp_c": _rounded(probe.avg_temperature_c),
        "fan_pct": _rounded(probe.avg_fan_speed_pct),
        "efficiency_fps_per_w": _rounded(probe.efficiency_fps_per_w),
        "efficiency_mhz_per_w": _rounded(probe.efficiency_mhz_per_w),
        "decision": str(decision),
        "reason": str(reason),
        "log_path": str(probe.log_path),
    }


def overclock_budget_event_payload(
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


def overclock_budget_payload_from_label(
    label: str,
    *,
    max_clock_drop_pct: float | int | None = None,
) -> dict:
    match = _OVERCLOCK_BUDGET_RE.search(str(label))
    if match is None:
        return {}
    return overclock_budget_event_payload(
        used_pct=float(match.group("used")),
        limit_pct=float(match.group("limit")),
        max_clock_drop_pct=max_clock_drop_pct,
    )


def _rounded(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)
