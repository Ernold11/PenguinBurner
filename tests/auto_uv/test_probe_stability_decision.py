from __future__ import annotations

from pathlib import Path

from auto_uv.domain.types import FailureKind, FailureSeverity
from stability.q2rtx.cuda_companion import (
    cuda_bruteforce_companion_command,
)
from auto_uv.probes.stability_decision import (
    classify_failed_result,
    evaluate_cuda_companion,
    evaluate_loaded_telemetry,
    evaluate_stable_run,
    sample_is_busy,
)
from auto_uv.probes.stability_decision import StabilityThresholds
from auto_uv_test_data import stable_probe_result


def test_cuda_companion_command_points_to_repo_stability_script() -> None:
    command = cuda_bruteforce_companion_command(gpu_index=0, duration_s=5)

    assert command[1].endswith("/stability/cuda_bruteforce.py")
    assert "/auto_uv/stability/" not in command[1]
    assert Path(command[1]).is_file()


def test_stability_pass_requires_benchmark_telemetry_and_cuda_when_enabled() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=True,
        companion_result={"success": True},
    )

    assert decision.passed
    assert decision.failure_kind is FailureKind.NONE


def test_stability_fails_closed_when_benchmark_summary_is_missing() -> None:
    result = stable_probe_result()
    result.pop("benchmark_summary")

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.METRICS_MISSING
    assert decision.severity is FailureSeverity.CRITICAL


def test_stability_uses_benchmark_summary_without_frame_count_check() -> None:
    result = stable_probe_result(frames=1234)
    result["benchmark_summary"] = {
        "render_frames": 1234,
        "demo_frames": 631,
        "measured_s": 30.0,
        "fps_avg": 100.0,
        "fps_min": 92.0,
        "fps_max": 108.0,
        "loops": 4,
    }
    result["benchmark_telemetry_samples"] = result["telemetry_samples"]

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert decision.passed
    assert decision.reason == "stable run"


def test_stability_reports_benchmark_average_fps_regression() -> None:
    result = stable_probe_result()
    result["benchmark_summary"] = {
        "render_frames": 1234,
        "measured_s": 30.0,
        "fps_avg": 89.0,
        "fps_min": 80.0,
        "fps_max": 95.0,
        "loops": 4,
    }
    result["benchmark_telemetry_samples"] = result["telemetry_samples"]

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.FPS_REGRESSION
    assert decision.reason == "benchmark average FPS below floor current=89.00 floor=90.00"


def test_stability_fails_benchmark_summary_with_invalid_metrics() -> None:
    result = stable_probe_result()
    result["benchmark_summary"] = {
        "render_frames": 0,
        "measured_s": 30.0,
        "fps_avg": 100.0,
    }

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        baseline_core_clock_mhz=2100.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.METRICS_INVALID
    assert decision.severity is FailureSeverity.CRITICAL

def test_stability_treats_loaded_low_clock_as_recoverable() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(clock_mhz=1750.0),
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
    result["reason"] = "benchmark-timeout"
    result["log_path"] = "/tmp/probe.log"

    decision = evaluate_stable_run(
        result,
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


# --- power-cap-aware clock floor (power-bound regime, e.g. RTX 5090) ---


def _busy_sample(
    clock_mhz: float,
    *,
    power_w: float = 450.0,
    perf_cap_reason: str | None = None,
) -> dict:
    return {
        "elapsed_s": 6.0,
        "power_w": power_w,
        "core_clock_mhz": clock_mhz,
        "gpu_util_pct": 99.0,
        "perf_cap_reason": perf_cap_reason,
    }


def test_low_clock_passes_when_busy_samples_report_power_cap() -> None:
    # The field failure shape: avg 2111 vs a 94%-of-2595 floor, driver naming
    # sw-power on the busy window while average draw sits below the limit
    # (per-frame spikes hit the cap, the average droops).
    decision = evaluate_loaded_telemetry(
        [_busy_sample(2111.0, power_w=453.0, perf_cap_reason="sw-power")] * 4,
        baseline_power_w=553.0,
        baseline_core_clock_mhz=2595.0,
        power_limit_w=517,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert decision.passed
    assert "power-walled but stable" in decision.reason


def test_low_clock_passes_when_average_power_pins_the_limit() -> None:
    # No perf-cap telemetry at all; saturation proven by avg power within the
    # 2% headroom of the applied limit.
    decision = evaluate_loaded_telemetry(
        [_busy_sample(2111.0, power_w=512.0)] * 4,
        baseline_power_w=553.0,
        baseline_core_clock_mhz=2595.0,
        power_limit_w=517,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert decision.passed
    assert "power-walled but stable" in decision.reason


def test_low_clock_still_fails_for_reliability_demotion() -> None:
    # Same clock shortfall, but power is off the cap and the driver names a
    # reliability cap: silent V/F demotion must keep failing (S7).
    decision = evaluate_loaded_telemetry(
        [_busy_sample(2111.0, power_w=430.0, perf_cap_reason="sw-reliability")] * 4,
        baseline_power_w=553.0,
        baseline_core_clock_mhz=2595.0,
        power_limit_w=517,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK
    assert decision.reason == "average busy core clock below floor"


def test_power_cap_exemption_disarms_when_cap_stops_binding() -> None:
    # Once an undervolt frees power headroom (power off the limit, driver
    # reports no cap), a still-low clock is real instability again.
    decision = evaluate_loaded_telemetry(
        [_busy_sample(2111.0, power_w=400.0, perf_cap_reason="none")] * 4,
        baseline_power_w=553.0,
        baseline_core_clock_mhz=2595.0,
        power_limit_w=517,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK


def test_minority_power_cap_samples_do_not_exempt_low_clock() -> None:
    samples = [
        _busy_sample(2111.0, power_w=430.0, perf_cap_reason="sw-power"),
        *[_busy_sample(2111.0, power_w=430.0, perf_cap_reason="none")] * 3,
    ]
    decision = evaluate_loaded_telemetry(
        samples,
        baseline_power_w=553.0,
        baseline_core_clock_mhz=2595.0,
        power_limit_w=517,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK


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
