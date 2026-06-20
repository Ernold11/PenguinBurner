from __future__ import annotations

from .rules import _float_or_none


def profile_verification_metrics_from_result(result) -> dict:
    metrics = {}
    benchmark_summary = getattr(result, "benchmark_summary", None)
    if benchmark_summary is not None:
        if isinstance(benchmark_summary, dict):
            avg_fps = _float_or_none(benchmark_summary.get("fps_avg"))
        else:
            avg_fps = _float_or_none(getattr(benchmark_summary, "fps_avg", None))
        if avg_fps is not None and avg_fps > 0.0:
            metrics["avg_fps"] = avg_fps

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
