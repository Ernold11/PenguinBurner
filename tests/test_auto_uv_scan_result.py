"""Tests for build_voltage_scan_result: the Auto-UV result the main loop returns.

These exercise the baseline-vs-final delta math (clock/power drop, savings) and
the None-handling when a baseline or final probe is missing.
"""

from __future__ import annotations

from pathlib import Path

from auto_uv.domain.scan_result import build_voltage_scan_result
from auto_uv.domain.types import AutoUvProbeSummary


def _probe(
    *,
    core_clock_mhz: float | None,
    power_w: float | None,
    efficiency_mhz_per_w: float | None,
) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=900,
        lock_clock_mhz=2500,
        live_voltage_before_mv=900,
        live_voltage_after_mv=900,
        avg_voltage_mv=900.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=98.0,
        max_fps=102.0,
        avg_power_w=power_w,
        max_power_w=power_w,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=40.0,
        avg_core_clock_mhz=core_clock_mhz,
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=efficiency_mhz_per_w,
        watts_per_mhz=0.1,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/probe.log"),
    )


def test_build_scan_result_computes_drops_and_savings() -> None:
    baseline = _probe(core_clock_mhz=2600.0, power_w=250.0, efficiency_mhz_per_w=10.4)
    final = _probe(core_clock_mhz=2500.0, power_w=200.0, efficiency_mhz_per_w=12.5)

    result = build_voltage_scan_result(
        final_voltage_mv=900,
        final_lock_clock_mhz=2500,
        initial_probe=baseline,
        probe_history=[baseline, final],
        final_probe=final,
    )

    assert result.success is True
    assert result.final_voltage_mv == 900
    assert result.lock_clock_mhz == 2500
    assert result.stop_reason == "candidate-sweep-complete"
    assert result.probes == [baseline, final]
    # 2600 -> 2500 is a 100 MHz / ~3.85% clock drop.
    assert result.core_clock_drop_mhz == 100.0
    assert result.core_clock_drop_pct == (100.0 / 2600.0) * 100.0
    # 250 W -> 200 W is 50 W / 20% saved.
    assert result.power_saved_w == 50.0
    assert result.power_saved_pct == 20.0
    assert result.final_efficiency_mhz_per_w == 12.5
    assert result.baseline_efficiency_mhz_per_w == 10.4


def test_build_scan_result_handles_missing_probes() -> None:
    result = build_voltage_scan_result(
        final_voltage_mv=950,
        final_lock_clock_mhz=2550,
        initial_probe=None,
        probe_history=[],
        final_probe=None,
    )

    assert result.success is True
    # With no probes the deltas and percentages stay None rather than raising.
    assert result.core_clock_drop_mhz is None
    assert result.core_clock_drop_pct is None
    assert result.power_saved_w is None
    assert result.power_saved_pct is None
    assert result.baseline_core_clock_mhz is None
    assert result.final_power_w is None


def test_build_scan_result_drop_pct_is_none_when_baseline_zero() -> None:
    baseline = _probe(core_clock_mhz=0.0, power_w=0.0, efficiency_mhz_per_w=None)
    final = _probe(core_clock_mhz=2500.0, power_w=200.0, efficiency_mhz_per_w=12.5)

    result = build_voltage_scan_result(
        final_voltage_mv=900,
        final_lock_clock_mhz=2500,
        initial_probe=baseline,
        probe_history=[baseline, final],
        final_probe=final,
    )

    # drop_pct guards against a zero baseline to avoid dividing by zero.
    assert result.core_clock_drop_pct is None
    assert result.power_saved_pct is None
    # The absolute delta is still reported.
    assert result.core_clock_drop_mhz == -2500.0
