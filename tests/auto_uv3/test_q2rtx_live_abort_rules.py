from __future__ import annotations

from auto_uv3.auto_uv_types import TelemetrySample
from auto_uv3.q2rtx.probe_runtime_guardrails import (
    probe_failure_should_mark_voltage_unsafe,
)
from auto_uv3.q2rtx.q2rtx_live_abort_rules import telemetry_live_abort_reason


def _sample(
    elapsed_s: float,
    *,
    gpu_util_pct: float,
    power_w: float,
    core_clock_mhz: float = 210.0,
) -> TelemetrySample:
    return TelemetrySample(
        elapsed_s=float(elapsed_s),
        gpu_util_pct=float(gpu_util_pct),
        power_w=float(power_w),
        core_clock_mhz=float(core_clock_mhz),
    )


def test_telemetry_abort_detects_q2rtx_not_loading_selected_nvidia_gpu() -> None:
    samples = [
        _sample(float(index), gpu_util_pct=0.0, power_w=4.5)
        for index in range(5, 36)
    ]

    reason = telemetry_live_abort_reason(
        {
            "elapsed_s": 35.0,
            "latest_sample": samples[-1],
            "telemetry_samples": samples,
        },
        busy_power_floor_w=None,
        proper_run_power_floor_w=None,
        target_core_clock_floor_mhz=None,
        progress_state={},
    )

    assert reason is not None
    assert reason.startswith("q2rtx-selected-nvidia-gpu-idle")
    assert "may be rendering on another GPU" in reason


def test_telemetry_idle_abort_allows_actual_selected_gpu_load() -> None:
    samples = [
        _sample(float(index), gpu_util_pct=0.0, power_w=4.5)
        for index in range(5, 35)
    ]
    samples.append(
        _sample(35.0, gpu_util_pct=97.0, power_w=95.0, core_clock_mhz=2400.0)
    )

    assert (
        telemetry_live_abort_reason(
            {
                "elapsed_s": 35.0,
                "latest_sample": samples[-1],
                "telemetry_samples": samples,
            },
            busy_power_floor_w=None,
            proper_run_power_floor_w=None,
            target_core_clock_floor_mhz=None,
            progress_state={},
        )
        is None
    )


def test_selected_gpu_idle_failure_does_not_blacklist_voltage() -> None:
    assert (
        probe_failure_should_mark_voltage_unsafe(
            "q2rtx-selected-nvidia-gpu-idle max_util=0.0% max_power=4.5W"
        )
        is False
    )
