from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path

from penguin_burner_paths import default_user_config_dir

from .models import AutoUvProbeSummary
from .tuning import AUTO_UV_FAN_TUNING


@dataclass(frozen=True, slots=True)
class AutoUvFanTuningResult:
    path: Path
    payload: dict

    @property
    def blocked(self) -> bool:
        return bool(self.payload.get("fan_curve_blocked"))

    @property
    def block_reason(self) -> str | None:
        reason = self.payload.get("block_reason")
        return str(reason) if reason else None

    @property
    def curve(self) -> list[list[float]]:
        fan = self.payload.get("fan")
        if not isinstance(fan, dict):
            return []
        return [list(point) for point in fan["curve"]]


def _finite(value: float | int | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _mean(values: list[float | int | None]) -> float | None:
    finite_values = [
        float(value)
        for value in (_finite(item) for item in values)
        if value is not None
    ]
    if not finite_values:
        return None
    return sum(finite_values) / float(len(finite_values))


def _max(values: list[float | int | None]) -> float | None:
    finite_values = [
        float(value)
        for value in (_finite(item) for item in values)
        if value is not None
    ]
    if not finite_values:
        return None
    return max(finite_values)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(float(lower), min(float(value), float(upper)))


def _monotonic_curve(points: list[tuple[float, float]]) -> list[list[float]]:
    normalized: list[list[float]] = []
    last_temp: float | None = None
    last_speed = 0.0
    for temp_c, speed_pct in points:
        temp = round(float(temp_c), 1)
        speed = round(_clamp(float(speed_pct), 0.0, 100.0), 1)
        if last_temp is not None and temp <= last_temp:
            temp = round(last_temp + 1.0, 1)
        speed = max(speed, last_speed)
        normalized.append([temp, speed])
        last_temp = temp
        last_speed = speed
    return normalized


def _blocked_payload(
    *,
    telemetry: dict,
    reason: str,
    loaded_temp_c: float | None,
    limit_temp_c: float,
) -> dict:
    generated_at = datetime.now().astimezone().isoformat()
    payload = {
        "source": "auto-uv",
        "format_version": 1,
        "generated_at": generated_at,
        "fan_curve_blocked": True,
        "block_reason": reason,
        "max_stock_curve_load_temperature_c": float(limit_temp_c),
        "telemetry": telemetry,
    }
    if loaded_temp_c is not None:
        payload["loaded_temperature_c"] = float(loaded_temp_c)
    return payload


def _probe_telemetry_payload(
    *,
    final_probe: AutoUvProbeSummary | None,
    probes: list[AutoUvProbeSummary],
) -> dict:
    all_probes = [probe for probe in probes if probe is not None]
    if final_probe is not None and final_probe not in all_probes:
        all_probes.append(final_probe)
    return {
        "final": {
            "avg_power_w": _finite(final_probe.avg_power_w if final_probe else None),
            "max_power_w": _finite(final_probe.max_power_w if final_probe else None),
            "avg_temperature_c": _finite(
                final_probe.avg_temperature_c if final_probe else None
            ),
            "max_temperature_c": _finite(
                final_probe.max_temperature_c if final_probe else None
            ),
            "avg_fan_speed_pct": _finite(
                final_probe.avg_fan_speed_pct if final_probe else None
            ),
            "max_fan_speed_pct": _finite(
                final_probe.max_fan_speed_pct if final_probe else None
            ),
        },
        "scan": {
            "max_temperature_c": _max(
                [probe.max_temperature_c for probe in all_probes]
            ),
            "avg_temperature_c": _mean(
                [probe.avg_temperature_c for probe in all_probes]
            ),
            "max_fan_speed_pct": _max(
                [probe.max_fan_speed_pct for probe in all_probes]
            ),
            "avg_fan_speed_pct": _mean(
                [probe.avg_fan_speed_pct for probe in all_probes]
            ),
            "max_power_w": _max([probe.max_power_w for probe in all_probes]),
            "avg_power_w": _mean([probe.avg_power_w for probe in all_probes]),
            "probe_count": len(all_probes),
        },
    }


def build_auto_uv_fan_payload(
    *,
    final_probe: AutoUvProbeSummary | None,
    probes: list[AutoUvProbeSummary],
) -> dict | None:
    telemetry = _probe_telemetry_payload(final_probe=final_probe, probes=probes)
    final_telemetry = telemetry["final"]
    scan_telemetry = telemetry["scan"]
    loaded_temp_c = (
        _finite(final_telemetry["max_temperature_c"])
        or _finite(final_telemetry["avg_temperature_c"])
        or _finite(scan_telemetry["max_temperature_c"])
        or _finite(scan_telemetry["avg_temperature_c"])
    )
    max_stock_load_temp_c = float(AUTO_UV_FAN_TUNING.max_stock_curve_load_temp_c)
    if loaded_temp_c is None:
        return _blocked_payload(
            telemetry=telemetry,
            reason="missing-final-load-temperature",
            loaded_temp_c=None,
            limit_temp_c=max_stock_load_temp_c,
        )
    if loaded_temp_c > max_stock_load_temp_c:
        return _blocked_payload(
            telemetry=telemetry,
            reason="stock-load-temperature-too-high",
            loaded_temp_c=float(loaded_temp_c),
            limit_temp_c=max_stock_load_temp_c,
        )

    observed_fan_pct = (
        _finite(final_telemetry["avg_fan_speed_pct"])
        or _finite(final_telemetry["max_fan_speed_pct"])
        or _finite(scan_telemetry["avg_fan_speed_pct"])
        or _finite(scan_telemetry["max_fan_speed_pct"])
    )
    if observed_fan_pct is None:
        observed_fan_pct = AUTO_UV_FAN_TUNING.fallback_speed_pct

    zero_rpm_temp_c = float(AUTO_UV_FAN_TUNING.zero_rpm_until_temp_c)
    active_temp_c = float(AUTO_UV_FAN_TUNING.minimum_active_temp_c)
    min_active_speed_pct = float(AUTO_UV_FAN_TUNING.minimum_active_speed_pct)
    emergency_temp_c = float(AUTO_UV_FAN_TUNING.emergency_temp_c)
    emergency_speed_pct = float(AUTO_UV_FAN_TUNING.emergency_min_speed_pct)
    full_speed_temp_c = float(AUTO_UV_FAN_TUNING.full_speed_temp_c)
    full_speed_pct = float(AUTO_UV_FAN_TUNING.full_speed_pct)
    max_curve_points = max(5, int(AUTO_UV_FAN_TUNING.max_curve_points))

    safe_curve_temp_c = _clamp(
        max_stock_load_temp_c,
        active_temp_c + 1.0,
        emergency_temp_c - 1.0,
    )
    thermal_span_c = max(1.0, emergency_temp_c - active_temp_c)
    safe_position = _clamp(
        (safe_curve_temp_c - active_temp_c) / thermal_span_c,
        0.0,
        1.0,
    )
    cooling_headroom_c = max(0.0, max_stock_load_temp_c - float(loaded_temp_c))
    headroom_speed_reduction_pct = min(
        float(AUTO_UV_FAN_TUNING.cooling_headroom_max_speed_reduction_pct),
        cooling_headroom_c
        * float(AUTO_UV_FAN_TUNING.cooling_headroom_speed_reduction_pct_per_c),
    )
    exponential_power_bonus = min(
        float(AUTO_UV_FAN_TUNING.cooling_headroom_max_exponential_power_bonus),
        cooling_headroom_c
        * float(AUTO_UV_FAN_TUNING.cooling_headroom_exponential_power_per_c),
    )
    effective_exponential_power = max(
        1.0,
        float(AUTO_UV_FAN_TUNING.exponential_power) + exponential_power_bonus,
    )
    thermal_speed_floor_pct = min_active_speed_pct + (
        (emergency_speed_pct - min_active_speed_pct)
        * (safe_position**effective_exponential_power)
    )
    relaxed_observed_fan_pct = max(
        min_active_speed_pct,
        float(observed_fan_pct) - headroom_speed_reduction_pct,
    )
    safe_anchor_speed_pct = _clamp(
        max(relaxed_observed_fan_pct, thermal_speed_floor_pct, min_active_speed_pct),
        min_active_speed_pct,
        AUTO_UV_FAN_TUNING.load_anchor_max_speed_pct,
    )

    curve_points: list[tuple[float, float]] = [
        (zero_rpm_temp_c, 0.0),
        (active_temp_c, min_active_speed_pct),
    ]
    max_exponential_points = max(1, max_curve_points - 4)
    exponential_points = min(
        max(1, int(AUTO_UV_FAN_TUNING.exponential_points)),
        max_exponential_points,
    )
    for step in range(1, exponential_points + 1):
        position = float(step) / float(exponential_points)
        temp_c = active_temp_c + ((safe_curve_temp_c - active_temp_c) * position)
        speed_pct = min_active_speed_pct + (
            (safe_anchor_speed_pct - min_active_speed_pct)
            * (position**effective_exponential_power)
        )
        curve_points.append((temp_c, speed_pct))
    curve_points.extend(
        [
            (emergency_temp_c, max(emergency_speed_pct, safe_anchor_speed_pct)),
            (full_speed_temp_c, full_speed_pct),
        ]
    )

    curve = _monotonic_curve(curve_points)

    generated_at = datetime.now().astimezone().isoformat()
    fan_config = {
        "poll_interval_s": AUTO_UV_FAN_TUNING.poll_interval_s,
        "hysteresis_c": AUTO_UV_FAN_TUNING.hysteresis_c,
        "mode": "linear",
        "min_fan_speed_pct": AUTO_UV_FAN_TUNING.min_speed_pct,
        "max_fan_speed_pct": AUTO_UV_FAN_TUNING.max_speed_pct,
        "max_step_up_pct_per_s": AUTO_UV_FAN_TUNING.max_step_up_pct_per_s,
        "max_step_down_pct_per_s": AUTO_UV_FAN_TUNING.max_step_down_pct_per_s,
        "manual_enable_temp_c": AUTO_UV_FAN_TUNING.minimum_active_temp_c,
        "auto_restore_temp_c": AUTO_UV_FAN_TUNING.zero_rpm_until_temp_c,
        "emergency_auto_override_temp_c": AUTO_UV_FAN_TUNING.hardware_auto_override_temp_c,
        "emergency_auto_resume_temp_c": AUTO_UV_FAN_TUNING.emergency_resume_temp_c,
        "force_update_every_poll": False,
        "curve_source": "auto-uv",
        "curve_source_generated_at": generated_at,
        "curve_source_target_load_temp_c": float(safe_curve_temp_c),
        "curve": curve,
    }
    return {
        "source": "auto-uv",
        "format_version": 1,
        "generated_at": generated_at,
        "zero_rpm_until_temperature_c": float(zero_rpm_temp_c),
        "minimum_active_temperature_c": float(active_temp_c),
        "minimum_active_fan_speed_pct": float(min_active_speed_pct),
        "max_stock_curve_load_temperature_c": float(max_stock_load_temp_c),
        "cooling_headroom_c": float(cooling_headroom_c),
        "cooling_headroom_speed_reduction_pct": float(headroom_speed_reduction_pct),
        "cooling_headroom_exponential_power_bonus": float(exponential_power_bonus),
        "effective_exponential_power": float(effective_exponential_power),
        "max_curve_points": int(max_curve_points),
        "safe_curve_temperature_c": float(safe_curve_temp_c),
        "load_anchor_temperature_c": float(safe_curve_temp_c),
        "load_anchor_fan_speed_pct": float(safe_anchor_speed_pct),
        "observed_fan_speed_before_headroom_pct": float(observed_fan_pct),
        "emergency_temperature_c": float(emergency_temp_c),
        "emergency_min_fan_speed_pct": float(emergency_speed_pct),
        "full_speed_temperature_c": float(full_speed_temp_c),
        "loaded_temperature_c": float(loaded_temp_c),
        "observed_fan_speed_pct": float(observed_fan_pct),
        "fan": fan_config,
        "telemetry": telemetry,
    }


def write_auto_uv_fan_payload(
    *,
    final_probe: AutoUvProbeSummary | None,
    probes: list[AutoUvProbeSummary],
) -> AutoUvFanTuningResult | None:
    payload = build_auto_uv_fan_payload(final_probe=final_probe, probes=probes)
    if payload is None:
        return None

    output_dir = default_user_config_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "auto-uv-fan-curve.json"
    output_path.write_text(json.dumps(payload, indent=2) + "\n")
    return AutoUvFanTuningResult(path=output_path, payload=payload)
