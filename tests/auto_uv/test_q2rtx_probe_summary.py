from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_uv.persistence.verified_candidate_result_file import probe_metrics
from auto_uv.q2rtx.q2rtx_probe_summary import (
    summarize_loaded_perf_cap_reason,
    summarize_q2rtx_cuda_probe,
)
from auto_uv.ui.probe_summary_ui_payload import probe_summary_ui_payload


def test_probe_summary_records_loaded_median_and_p90_diagnostics() -> None:
    telemetry_samples = [
        {
            "elapsed_s": 1.0,
            "power_w": 35.0,
            "core_clock_mhz": 1100.0,
            "voltage_mv": 750.0,
        },
        {
            "elapsed_s": 6.0,
            "power_w": 100.0,
            "core_clock_mhz": 2100.0,
            "voltage_mv": 820.0,
            "perf_cap_reason": "none",
        },
        {
            "elapsed_s": 7.0,
            "power_w": 150.0,
            "core_clock_mhz": 2300.0,
            "voltage_mv": 850.0,
            "perf_cap_reason": "sw-power",
        },
        {
            "elapsed_s": 8.0,
            "power_w": 180.0,
            "core_clock_mhz": 2400.0,
            "voltage_mv": 860.0,
            "perf_cap_reason": "sw-power+hw-thermal",
        },
        {
            "elapsed_s": 9.0,
            "power_w": 200.0,
            "core_clock_mhz": 2450.0,
            "voltage_mv": 870.0,
        },
        {
            "elapsed_s": 10.0,
            "power_w": 160.0,
            "core_clock_mhz": 2350.0,
            "voltage_mv": 855.0,
        },
    ]
    result = SimpleNamespace(
        timedemo_runs=[SimpleNamespace(frames=1000, seconds=10.0, fps=100.0)],
        telemetry_samples=telemetry_samples,
        companion_telemetry_samples=[],
        telemetry_summary=lambda: {},
        reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )

    summary = summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=870,
        lock_clock_mhz=2400,
        live_voltage_before_mv=870,
        live_voltage_after_mv=868,
        used_companion_load=False,
        power_limit_w=None,
        result=result,
    )

    assert summary.avg_power_w == pytest.approx(172.5)
    assert summary.avg_core_clock_mhz == pytest.approx(2375.0)
    assert summary.avg_voltage_mv == pytest.approx(858.75)
    assert summary.loaded_median_core_clock_mhz == 2400.0
    assert summary.loaded_p90_core_clock_mhz == 2450.0
    assert summary.loaded_median_voltage_mv == 860.0
    assert summary.loaded_qualified_sample_count == 4
    assert summary.observed_vdroop_mv == 10.0
    assert summary.perf_cap_reason == "sw-power+hw-thermal"

    payload = probe_summary_ui_payload(summary, stage="probe")
    metrics = probe_metrics(summary)
    assert payload["loaded_median_core_clock_mhz"] == 2400.0
    assert payload["perf_cap_reason"] == "sw-power+hw-thermal"
    assert payload["observed_vdroop_mv"] == 10.0
    assert metrics["loaded_median_voltage_mv"] == 860.0
    assert metrics["loaded_qualified_sample_count"] == 4
    assert metrics["perf_cap_reason"] == "sw-power+hw-thermal"


def test_loaded_perf_cap_reason_suppresses_idle_as_none() -> None:
    loaded_samples = [
        {"elapsed_s": 6.0, "power_w": 220.0, "perf_cap_reason": "idle"},
        {"elapsed_s": 7.0, "power_w": 230.0, "perf_cap_reason": "idle"},
    ]

    assert (
        summarize_loaded_perf_cap_reason(
            loaded_samples,
            [],
            power_limit_w=None,
            use_power_limit_floor=False,
        )
        == "none"
    )
