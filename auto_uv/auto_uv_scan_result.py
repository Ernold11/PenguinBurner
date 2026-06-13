"""Build the Auto-UV scan result returned to callers.

It compares the final verified probe with the initial loaded baseline probe.
"""

from __future__ import annotations

from .auto_uv_types import AutoUvProbeSummary, AutoUvVoltageScanResult


def build_voltage_scan_result(
    *,
    final_voltage_mv: int,
    final_lock_clock_mhz: int,
    initial_probe: AutoUvProbeSummary | None,
    probe_history: list[AutoUvProbeSummary],
    final_probe: AutoUvProbeSummary | None,
) -> AutoUvVoltageScanResult:
    baseline_probe = initial_probe
    return AutoUvVoltageScanResult(
        success=True,
        final_voltage_mv=int(final_voltage_mv),
        lock_clock_mhz=int(final_lock_clock_mhz),
        stop_reason="candidate-sweep-complete",
        failed_candidate_voltage_mv=None,
        probes=probe_history,
        baseline_core_clock_mhz=probe_float(baseline_probe, "avg_core_clock_mhz"),
        baseline_power_w=probe_float(baseline_probe, "avg_power_w"),
        baseline_temperature_c=probe_float(baseline_probe, "avg_temperature_c"),
        baseline_fan_speed_pct=probe_float(baseline_probe, "avg_fan_speed_pct"),
        baseline_efficiency_mhz_per_w=probe_float(
            baseline_probe,
            "efficiency_mhz_per_w",
        ),
        final_core_clock_mhz=probe_float(final_probe, "avg_core_clock_mhz"),
        final_power_w=probe_float(final_probe, "avg_power_w"),
        final_temperature_c=probe_float(final_probe, "avg_temperature_c"),
        final_fan_speed_pct=probe_float(final_probe, "avg_fan_speed_pct"),
        final_efficiency_mhz_per_w=probe_float(final_probe, "efficiency_mhz_per_w"),
        core_clock_drop_mhz=delta(
            probe_float(baseline_probe, "avg_core_clock_mhz"),
            probe_float(final_probe, "avg_core_clock_mhz"),
        ),
        core_clock_drop_pct=drop_pct(
            probe_float(baseline_probe, "avg_core_clock_mhz"),
            probe_float(final_probe, "avg_core_clock_mhz"),
        ),
        power_saved_w=delta(
            probe_float(baseline_probe, "avg_power_w"),
            probe_float(final_probe, "avg_power_w"),
        ),
        power_saved_pct=drop_pct(
            probe_float(baseline_probe, "avg_power_w"),
            probe_float(final_probe, "avg_power_w"),
        ),
    )


def probe_float(probe: AutoUvProbeSummary | None, field_name: str) -> float | None:
    if probe is None:
        return None
    value = getattr(probe, field_name)
    return float(value) if value is not None else None


def delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return float(before) - float(after)


def drop_pct(before: float | None, after: float | None) -> float | None:
    if before in (None, 0.0) or after is None:
        return None
    return (float(before) - float(after)) / float(before) * 100.0
