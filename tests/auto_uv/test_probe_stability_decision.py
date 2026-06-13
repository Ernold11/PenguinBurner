from __future__ import annotations

from pathlib import Path

from auto_uv.auto_uv_types import FailureKind, FailureSeverity
from auto_uv.q2rtx.q2rtx_cuda_probe_config import (
    cuda_bruteforce_companion_command,
)
from auto_uv.q2rtx.probe_stability_decision import (
    classify_failed_result,
    evaluate_cuda_companion,
    evaluate_loaded_telemetry,
    evaluate_stable_run,
    evaluate_timedemo_runs,
    sample_is_busy,
)
from auto_uv.q2rtx.probe_stability_decision import StabilityThresholds
from auto_uv_test_data import stable_probe_result


def test_cuda_companion_command_points_to_repo_stability_script() -> None:
    command = cuda_bruteforce_companion_command(gpu_index=0, duration_s=5)

    assert command[1].endswith("/stability/cuda_bruteforce.py")
    assert "/auto_uv/stability/" not in command[1]
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
        stable_probe_result(clock_mhz=1750.0),
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


# --- coverage: short-circuit guards in evaluate_stable_run ---


def test_stability_user_stop_is_recoverable() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
        stop_requested=True,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.USER_STOP
    assert decision.severity is FailureSeverity.RECOVERABLE
    assert decision.reason == "user stop requested"


def test_stability_fatal_output_is_critical() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_frames=1000,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
        fatal_output_found=True,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.FATAL_OUTPUT
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.reason == "fatal output pattern detected"


def test_stability_failed_q2rtx_result_is_classified() -> None:
    result = stable_probe_result()
    result["success"] = False
    result["reason"] = "timedemo-timeout"
    result["log_path"] = "/tmp/probe.log"

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
    assert decision.failure_kind is FailureKind.Q2RTX_FAILED
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.log_path == Path("/tmp/probe.log")


# --- coverage: classify_failed_result branches ---


def test_classify_low_clock_prefix_is_recoverable_low_clock() -> None:
    decision = classify_failed_result(
        "telemetry-live-core_clock-avg below floor", log_path=None
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK
    assert decision.severity is FailureSeverity.RECOVERABLE
    assert decision.reason == "telemetry-live-core_clock-avg below floor"


def test_classify_xid_prefix_is_critical_nvidia_xid() -> None:
    decision = classify_failed_result("nvidia-xid-detected", log_path=None)

    assert not decision.passed
    assert decision.failure_kind is FailureKind.NVIDIA_XID
    assert decision.severity is FailureSeverity.CRITICAL


def test_classify_fatal_prefix_is_critical_q2rtx() -> None:
    decision = classify_failed_result("fatal-cuda-output", log_path=None)

    assert not decision.passed
    assert decision.failure_kind is FailureKind.Q2RTX_FAILED
    assert decision.severity is FailureSeverity.CRITICAL


def test_classify_cuda_prefix_is_critical_cuda_failed() -> None:
    decision = classify_failed_result("cuda kernel launch failed", log_path=None)

    assert not decision.passed
    assert decision.failure_kind is FailureKind.CUDA_FAILED
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.reason == "cuda kernel launch failed"


def test_classify_unknown_reason_is_recoverable_q2rtx() -> None:
    decision = classify_failed_result("", log_path=None)

    assert not decision.passed
    assert decision.failure_kind is FailureKind.Q2RTX_FAILED
    assert decision.severity is FailureSeverity.RECOVERABLE
    assert decision.reason == "Q2RTX probe failed"


# --- coverage: evaluate_timedemo_runs invalid-metric branches ---


def test_timedemo_run_missing_field_is_metrics_invalid() -> None:
    decision = evaluate_timedemo_runs(
        [{"frames": 1000, "seconds": None, "fps": 100.0, "run_index": 2}],
        baseline_frames=None,
        baseline_fps=None,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.METRICS_INVALID
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.evidence["run"]["run_index"] == 2


def test_timedemo_run_non_positive_metric_is_metrics_invalid() -> None:
    decision = evaluate_timedemo_runs(
        [{"frames": 1000, "seconds": 0.0, "fps": 100.0}],
        baseline_frames=None,
        baseline_fps=None,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.METRICS_INVALID
    assert decision.reason == "timedemo run has non-positive metrics"


# --- coverage: evaluate_loaded_telemetry failure branches ---


def test_telemetry_samples_missing_is_load_lost() -> None:
    decision = evaluate_loaded_telemetry(
        [],
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOAD_LOST
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.reason == "telemetry samples missing"


def test_telemetry_no_busy_samples_is_load_lost() -> None:
    # Idle: util far below 60%, power far below the 50%-of-baseline floor (90 W).
    decision = evaluate_loaded_telemetry(
        [{"elapsed_s": 6.0, "power_w": 10.0, "core_clock_mhz": 2100.0, "gpu_util_pct": 5.0}],
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOAD_LOST
    assert decision.reason == "no busy telemetry samples"
    assert decision.evidence["power_floor_w"] == 90.0


def test_telemetry_busy_but_missing_core_clock_is_low_clock() -> None:
    # Busy via gpu_util, but no core_clock telemetry on the busy sample.
    decision = evaluate_loaded_telemetry(
        [{"elapsed_s": 6.0, "power_w": 180.0, "core_clock_mhz": None, "gpu_util_pct": 99.0}],
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK
    assert decision.severity is FailureSeverity.RECOVERABLE
    assert decision.reason == "busy core-clock telemetry missing"


def test_telemetry_derives_power_floor_when_baseline_power_absent() -> None:
    # baseline_power_w=None forces the derive_active_power_floor_w fallback path.
    # power_limit_w=200 -> floor uses the power-limit floor; sample stays busy via util.
    decision = evaluate_loaded_telemetry(
        [{"elapsed_s": 6.0, "power_w": 180.0, "core_clock_mhz": 2100.0, "gpu_util_pct": 99.0}],
        baseline_power_w=None,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=200,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert decision.passed
    assert decision.reason == "loaded telemetry stable"


# --- coverage: evaluate_cuda_companion failure branch ---


def test_cuda_companion_unsuccessful_uses_its_reason() -> None:
    decision = evaluate_cuda_companion(
        {"success": False, "reason": "cuda OOM"}, log_path=None
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.CUDA_FAILED
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.reason == "cuda OOM"


# --- coverage: sample_is_busy power-floor path ---


def test_sample_is_busy_via_power_when_util_below_threshold() -> None:
    # gpu_util below busy threshold falls through to the power check.
    sample = {"gpu_util_pct": 10.0, "power_w": 150.0}

    assert sample_is_busy(sample, busy_power_floor_w=100.0, busy_gpu_util_pct=60.0)


def test_sample_is_busy_false_when_power_below_floor_and_util_low() -> None:
    sample = {"gpu_util_pct": 10.0, "power_w": 50.0}

    assert not sample_is_busy(sample, busy_power_floor_w=100.0, busy_gpu_util_pct=60.0)
