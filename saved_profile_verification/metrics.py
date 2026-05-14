from __future__ import annotations

from .rules import _float_or_none


def profile_verification_metrics_from_result(result) -> dict:
    metrics = {}
    timedemo_runs = list(getattr(result, "timedemo_runs", []) or [])
    fps_values = [
        fps
        for fps in (_float_or_none(getattr(run, "fps", None)) for run in timedemo_runs)
        if fps is not None and fps > 0.0
    ]
    if fps_values:
        metrics["avg_fps"] = sum(fps_values) / len(fps_values)

    summary = _telemetry_summary(result)
    field_map = {
        "core_clock_avg": "avg_core_clock_mhz",
        "power_avg": "avg_power_w",
        "power_max": "max_power_w",
        "voltage_avg": "avg_voltage_mv",
        "voltage_max": "max_voltage_mv",
        "temperature_avg": "avg_temperature_c",
        "temperature_max": "max_temperature_c",
        "fan_avg": "avg_fan_speed_pct",
        "fan_max": "max_fan_speed_pct",
    }
    for source_key, target_key in field_map.items():
        value = _float_or_none(summary.get(source_key))
        if value is not None:
            metrics[target_key] = value

    avg_fps = _float_or_none(metrics.get("avg_fps"))
    avg_power_w = _float_or_none(metrics.get("avg_power_w"))
    avg_core_clock_mhz = _float_or_none(metrics.get("avg_core_clock_mhz"))
    if avg_fps is not None and avg_power_w is not None and avg_power_w > 0.0:
        metrics["efficiency_fps_per_w"] = avg_fps / avg_power_w
    if (
        avg_core_clock_mhz is not None
        and avg_power_w is not None
        and avg_power_w > 0.0
    ):
        metrics["efficiency_mhz_per_w"] = avg_core_clock_mhz / avg_power_w
    if (
        avg_power_w is not None
        and avg_core_clock_mhz is not None
        and avg_core_clock_mhz > 0.0
    ):
        metrics["watts_per_mhz"] = avg_power_w / avg_core_clock_mhz
    return metrics


def _telemetry_summary(result) -> dict:
    telemetry_summary = getattr(result, "telemetry_summary", None)
    if not callable(telemetry_summary):
        return {}
    try:
        summary = telemetry_summary()
    except Exception:
        return {}
    return summary if isinstance(summary, dict) else {}
