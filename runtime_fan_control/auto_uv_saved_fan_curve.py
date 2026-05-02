"""Load the fan curve saved by Auto-UV final verification.

The loader rejects missing, blocked, or thermally unsafe curves before runtime fan control can use them.
"""

from __future__ import annotations

import json

from auto_uv3.auto_uv_user_options import AUTO_UV_FAN_TUNING
from penguin_burner_errors import FanCurveBlockedError, NvmlError
from penguin_burner_paths import default_user_config_dir

from .fan_curve_runtime_rules import speed_for_temp, validate_curve


def validate_auto_uv_fan_curve_safety(curve, path) -> None:
    max_points = int(AUTO_UV_FAN_TUNING.max_curve_points)
    if len(curve) > max_points:
        raise FanCurveBlockedError(
            f"auto-UV fan curve has too many points: {len(curve)} > {max_points}: {path}"
        )

    zero_temp_c = float(AUTO_UV_FAN_TUNING.zero_rpm_until_temp_c)
    active_temp_c = float(AUTO_UV_FAN_TUNING.minimum_active_temp_c)
    active_speed_pct = float(AUTO_UV_FAN_TUNING.minimum_active_speed_pct)
    safe_temp_c = float(AUTO_UV_FAN_TUNING.max_base_curve_load_temp_c)
    emergency_temp_c = float(AUTO_UV_FAN_TUNING.emergency_temp_c)
    emergency_speed_pct = float(AUTO_UV_FAN_TUNING.emergency_min_speed_pct)
    full_speed_temp_c = float(AUTO_UV_FAN_TUNING.full_speed_temp_c)
    full_speed_pct = float(AUTO_UV_FAN_TUNING.full_speed_pct)
    hardware_override_temp_c = float(AUTO_UV_FAN_TUNING.hardware_auto_override_temp_c)

    def require_speed(temp_c, minimum_speed_pct, label):
        speed_pct = float(speed_for_temp(temp_c, curve, mode="linear"))
        if speed_pct + 0.01 < float(minimum_speed_pct):
            raise FanCurveBlockedError(
                f"auto-UV fan curve unsafe at {label}: "
                f"{speed_pct:.1f}% < {float(minimum_speed_pct):.1f}%: {path}"
            )

    zero_speed_pct = float(speed_for_temp(zero_temp_c, curve, mode="linear"))
    if zero_speed_pct > 0.01:
        raise FanCurveBlockedError(
            f"auto-UV fan curve unsafe at zero-rpm point: "
            f"{zero_temp_c:.1f}C is {zero_speed_pct:.1f}% instead of 0%: {path}"
        )
    require_speed(active_temp_c, active_speed_pct, "active minimum")
    require_speed(safe_temp_c, active_speed_pct, "safe load target")
    require_speed(emergency_temp_c, emergency_speed_pct, "emergency")
    require_speed(full_speed_temp_c, full_speed_pct, "full speed")
    if hardware_override_temp_c <= full_speed_temp_c:
        raise FanCurveBlockedError(
            "auto-UV fan curve hardware-auto override must be above the "
            f"{full_speed_temp_c:.1f}C full-speed point"
        )


def load_auto_uv_fan_curve(current_fan_config):
    path = default_user_config_dir() / "auto-uv-fan-curve.json"
    if not path.is_file():
        return None

    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise NvmlError(f"auto-UV fan curve payload is invalid: {path}")
    max_base_load_temp_c = float(AUTO_UV_FAN_TUNING.max_base_curve_load_temp_c)
    loaded_temp_c = payload.get("loaded_temperature_c")
    if payload.get("fan_curve_blocked"):
        reason = str(payload.get("block_reason") or "unknown")
        try:
            temp_text = (
                "n/a" if loaded_temp_c is None else f"{float(loaded_temp_c):.1f}C"
            )
        except (TypeError, ValueError):
            temp_text = "invalid"
        raise FanCurveBlockedError(
            "auto-UV fan curve is blocked: "
            f"reason={reason} loaded-temp={temp_text} "
            f"limit={max_base_load_temp_c:.1f}C"
        )
    if loaded_temp_c is not None:
        try:
            loaded_temp = float(loaded_temp_c)
        except (TypeError, ValueError) as exc:
            raise NvmlError(
                f"auto-UV fan curve loaded temperature is invalid: {path}"
            ) from exc
        if loaded_temp > max_base_load_temp_c:
            raise FanCurveBlockedError(
                "auto-UV fan curve rejected: "
                f"saved final load temperature {loaded_temp:.1f}C is above "
                f"the {max_base_load_temp_c:.1f}C safety limit"
            )
    else:
        raise FanCurveBlockedError(
            "auto-UV fan curve rejected: missing saved final load temperature"
        )
    raw_fan = payload.get("fan")
    if not isinstance(raw_fan, dict):
        raise NvmlError(f"auto-UV fan curve has no fan section: {path}")
    raw_curve = raw_fan.get("curve")
    if not isinstance(raw_curve, list) or not raw_curve:
        raise NvmlError(f"auto-UV fan curve has no curve points: {path}")

    curve = []
    for raw_point in raw_curve:
        if not isinstance(raw_point, list) or len(raw_point) != 2:
            raise NvmlError(f"auto-UV fan curve point is invalid: {path}: {raw_point}")
        try:
            curve.append([float(raw_point[0]), float(raw_point[1])])
        except (TypeError, ValueError) as exc:
            raise NvmlError(
                f"auto-UV fan curve point is invalid: {path}: {raw_point}"
            ) from exc
    validate_curve(curve)
    validate_auto_uv_fan_curve_safety(curve, path)

    fan_config = dict(current_fan_config)
    fan_config.update(
        {
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
            "curve": curve,
            "curve_source": "auto-uv",
            "curve_source_path": str(path),
            "curve_source_loaded_temperature_c": payload.get("loaded_temperature_c"),
            "curve_source_observed_fan_speed_pct": payload.get(
                "observed_fan_speed_pct"
            ),
            "curve_source_generated_at": raw_fan.get("curve_source_generated_at"),
            "curve_source_target_load_temp_c": raw_fan.get(
                "curve_source_target_load_temp_c"
            ),
        }
    )
    return {
        "path": path,
        "payload": payload,
        "fan_config": fan_config,
    }
