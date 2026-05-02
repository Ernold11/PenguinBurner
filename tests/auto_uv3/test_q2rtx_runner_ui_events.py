from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from auto_uv3.auto_uv_types import AutoUvProbeSummary, VfCurveCandidate
from auto_uv3.q2rtx.q2rtx_cuda_probe_runner import Q2RtxCudaProbeRunner
from auto_uv3_test_data import base_curve


def test_probe_runner_emits_candidate_table_start_and_result(monkeypatch) -> None:
    from auto_uv3.q2rtx import q2rtx_cuda_probe_runner as module

    events: list[tuple[str, dict]] = []
    summary = AutoUvProbeSummary(
        candidate_voltage_mv=950,
        lock_clock_mhz=2400,
        live_voltage_before_mv=950,
        live_voltage_after_mv=948,
        avg_voltage_mv=948.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=180.0,
        max_power_w=190.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=2400.0,
        efficiency_fps_per_w=0.56,
        efficiency_mhz_per_w=13.33,
        watts_per_mhz=0.075,
        used_companion_load=False,
        result_reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )
    result = {
        "success": True,
        "timedemo_runs": [{"frames": 1000, "seconds": 10.0, "fps": 100.0}],
        "telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2400.0, "gpu_util_pct": 99.0}
        ],
    }
    monkeypatch.setattr(
        module,
        "q2rtx_cuda_probe_config_for_voltage_band",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        module,
        "probe_voltage_candidate",
        lambda **kwargs: (summary, result),
    )

    runner = Q2RtxCudaProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=object(),
        runtime_default_plan=[],
        power_limit_w=220,
        start_voltage_mv=1000,
        baseline_clock_mhz=None,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=10,
        timedemo_warmup_runs=0,
        log=lambda _message: None,
        event_callback=lambda name, payload: events.append((name, payload)),
    )

    runner.probe_sweep_candidate(
        VfCurveCandidate(
            label="lower-voltage recovery-budget=0.60/1.20%",
            voltage_mv=950,
            target_mhz=2400,
            flattened_plan=base_curve(900, 1000, 25, 2200, 20),
        ),
        stable_history=[],
    )

    assert [name for name, _payload in events] == [
        "candidate_curve",
        "probe_start",
        "probe_result",
    ]
    assert events[1][1]["voltage_mv"] == 950
    assert events[1][1]["overclock_budget_limit_of_clock_drop_pct"] == 12.0
    assert events[2][1]["fps"] == 100.0
    assert events[2][1]["measured_clock_mhz"] == 2400.0
    assert events[2][1]["overclock_budget_used_of_clock_drop_pct"] == 6.0


def test_probe_runner_evaluates_cuda_from_per_voltage_config() -> None:
    summary = AutoUvProbeSummary(
        candidate_voltage_mv=950,
        lock_clock_mhz=2400,
        live_voltage_before_mv=950,
        live_voltage_after_mv=948,
        avg_voltage_mv=948.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=180.0,
        max_power_w=190.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=2400.0,
        efficiency_fps_per_w=0.56,
        efficiency_mhz_per_w=13.33,
        watts_per_mhz=0.075,
        used_companion_load=True,
        result_reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )
    result = {
        "success": True,
        "timedemo_runs": [{"frames": 1000, "seconds": 10.0, "fps": 100.0}],
        "telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2400.0, "gpu_util_pct": 99.0}
        ],
    }
    runner = Q2RtxCudaProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=SimpleNamespace(companion_command=None),
        runtime_default_plan=[],
        power_limit_w=220,
        start_voltage_mv=1000,
        baseline_clock_mhz=None,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=10,
        timedemo_warmup_runs=0,
        log=lambda _message: None,
    )

    outcome = runner.outcome_from_probe_result(
        summary,
        result,
        stable_history=[summary],
        q2rtx_config=SimpleNamespace(companion_command=("cuda",)),
    )

    assert outcome.decision.passed is True
    assert outcome.decision.evidence["cuda_required"] is True
