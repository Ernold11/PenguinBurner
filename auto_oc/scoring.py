"""Score Auto-OC probes by measured Q2RTX core clock, not FPS."""

from __future__ import annotations

from auto_uv.auto_uv_types import AutoUvProbeSummary


def effective_q2rtx_clock_mhz(probe: AutoUvProbeSummary | None) -> float | None:
    if probe is None:
        return None
    for field_name in (
        "q2rtx_avg_core_clock_mhz",
        "loaded_median_core_clock_mhz",
        "avg_core_clock_mhz",
    ):
        value = getattr(probe, field_name, None)
        if value is not None:
            return float(value)
    return None


def auto_oc_probe_key(
    probe: AutoUvProbeSummary | None,
    *,
    voltage_mv: int,
    step_index: int,
) -> tuple[float, float, float, float, float, int]:
    effective_clock = effective_q2rtx_clock_mhz(probe)
    p90_clock = getattr(probe, "loaded_p90_core_clock_mhz", None) if probe else None
    power_w = getattr(probe, "avg_power_w", None) if probe else None
    temperature_c = getattr(probe, "avg_temperature_c", None) if probe else None
    return (
        _clock_value(effective_clock),
        _clock_value(p90_clock),
        -float(voltage_mv),
        -_cost_value(power_w),
        -_cost_value(temperature_c),
        -int(step_index),
    )


def _clock_value(value: object | None) -> float:
    return -1.0 if value is None else float(value)


def _cost_value(value: object | None) -> float:
    return 1_000_000.0 if value is None else float(value)
