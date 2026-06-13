from __future__ import annotations

from ..shared.probe_data_fields import read_field


def probe_summary_ui_payload(
    probe,
    *,
    stage: str,
    decision: str = "",
    reason: str = "",
) -> dict:
    return {
        "stage": str(stage),
        "voltage_mv": int(read_field(probe, "candidate_voltage_mv")),
        "clock_mhz": int(read_field(probe, "lock_clock_mhz")),
        "measured_clock_mhz": _rounded(read_field(probe, "avg_core_clock_mhz")),
        "avg_core_clock_mhz": _rounded(read_field(probe, "avg_core_clock_mhz")),
        "avg_voltage_mv": _rounded(read_field(probe, "avg_voltage_mv")),
        "loaded_median_core_clock_mhz": _rounded(
            read_field(probe, "loaded_median_core_clock_mhz")
        ),
        "loaded_p90_core_clock_mhz": _rounded(
            read_field(probe, "loaded_p90_core_clock_mhz")
        ),
        "loaded_median_voltage_mv": _rounded(
            read_field(probe, "loaded_median_voltage_mv")
        ),
        "loaded_qualified_sample_count": int(
            read_field(probe, "loaded_qualified_sample_count") or 0
        ),
        "observed_vdroop_mv": _rounded(read_field(probe, "observed_vdroop_mv")),
        "q2rtx_measured_clock_mhz": _rounded(
            read_field(probe, "q2rtx_avg_core_clock_mhz")
        ),
        "q2rtx_measured_voltage_mv": _rounded(read_field(probe, "q2rtx_avg_voltage_mv")),
        "cuda_measured_clock_mhz": _rounded(read_field(probe, "cuda_avg_core_clock_mhz")),
        "cuda_measured_voltage_mv": _rounded(read_field(probe, "cuda_avg_voltage_mv")),
        "used_companion_load": bool(read_field(probe, "used_companion_load")),
        "fps": _rounded(read_field(probe, "avg_fps")),
        "fps_stddev": _rounded(read_field(probe, "fps_stddev")),
        "fps_variance_pct": _rounded(read_field(probe, "fps_variance_pct")),
        "power_w": _rounded(read_field(probe, "avg_power_w")),
        "temp_c": _rounded(read_field(probe, "avg_temperature_c")),
        "fan_pct": _rounded(read_field(probe, "avg_fan_speed_pct")),
        "perf_cap_reason": _string_or_empty(read_field(probe, "perf_cap_reason")),
        "efficiency_fps_per_w": _rounded(read_field(probe, "efficiency_fps_per_w")),
        "efficiency_mhz_per_w": _rounded(read_field(probe, "efficiency_mhz_per_w")),
        "decision": str(decision),
        "reason": str(reason),
        "log_path": str(read_field(probe, "log_path")),
    }


def _rounded(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _string_or_empty(value) -> str:
    if value in (None, ""):
        return ""
    return str(value)
