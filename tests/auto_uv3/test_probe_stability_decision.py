from __future__ import annotations

from pathlib import Path

from auto_uv3.auto_uv_types import FailureKind, FailureSeverity
from auto_uv3.q2rtx.q2rtx_cuda_probe_config import (
    cuda_bruteforce_companion_command,
)
from auto_uv3.q2rtx.probe_stability_decision import evaluate_stable_run
from auto_uv3_test_data import stable_probe_result


def test_cuda_companion_command_points_to_repo_stability_script() -> None:
    command = cuda_bruteforce_companion_command(gpu_index=0, duration_s=5)

    assert command[1].endswith("/stability/cuda_bruteforce.py")
    assert "/auto_uv3/stability/" not in command[1]
    assert Path(command[1]).is_file()


def test_stability_pass_requires_timedemo_telemetry_and_cuda_when_enabled() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=True,
        companion_result={"success": True},
    )

    assert decision.passed
    assert decision.failure_kind is FailureKind.NONE


def test_stability_fails_closed_when_timedemo_metrics_are_missing() -> None:
    result = stable_probe_result()
    result["timedemo_runs"] = []

    decision = evaluate_stable_run(
        result,
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.METRICS_MISSING
    assert decision.severity is FailureSeverity.CRITICAL


def test_stability_treats_frame_count_drift_as_critical() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(frames=999),
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.FRAME_COUNT_REGRESSION
    assert decision.severity is FailureSeverity.CRITICAL


def test_stability_allows_one_slow_timedemo_run_inside_single_run_tolerance() -> None:
    result = stable_probe_result()
    result["timedemo_runs"] = [
        {"frames": 1000, "seconds": 11.24, "fps": 89.0, "run_index": 1},
        {"frames": 1000, "seconds": 10.99, "fps": 91.0, "run_index": 2},
        {"frames": 1000, "seconds": 10.53, "fps": 95.0, "run_index": 3},
    ]

    decision = evaluate_stable_run(
        result,
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert decision.passed


def test_stability_reports_average_timedemo_fps_regression() -> None:
    result = stable_probe_result()
    result["timedemo_runs"] = [
        {"frames": 1000, "seconds": 11.36, "fps": 88.0},
        {"frames": 1000, "seconds": 11.11, "fps": 90.0},
    ]

    decision = evaluate_stable_run(
        result,
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.FPS_REGRESSION
    assert decision.reason == "timedemo average FPS below floor current=89.00 floor=90.00 runs=2"


def test_stability_reports_the_timedemo_run_that_missed_single_run_floor() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(fps=79.0),
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.FPS_REGRESSION
    assert decision.reason == (
        "timedemo single-run FPS below floor current=79.00 floor=80.00 run=1"
    )


def test_stability_treats_loaded_low_clock_as_recoverable() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(clock_mhz=1800.0),
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK
    assert decision.severity is FailureSeverity.RECOVERABLE


def test_stability_fails_when_required_cuda_result_is_missing() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=True,
        companion_result=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.CUDA_FAILED
    assert decision.severity is FailureSeverity.CRITICAL


def test_stability_treats_nvidia_xid_as_critical() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
        xid_found=True,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.NVIDIA_XID
    assert decision.severity is FailureSeverity.CRITICAL
