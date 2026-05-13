"""Classify one raw Q2RTX/CUDA probe as stable, recoverable, or critical.

The voltage sweep consumes this typed decision instead of parsing process output itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..auto_uv_types import FailureKind, FailureSeverity, StableRunDecision
from ..curve.base_load_telemetry import derive_active_power_floor_w
from ..shared.probe_data_fields import percent as _percent
from ..shared.probe_data_fields import read_field


@dataclass(frozen=True, slots=True)
class StabilityThresholds:
    min_average_fps_pct: float = 90.0
    min_single_run_fps_pct: float = 80.0
    min_power_pct: float = 50.0
    min_core_clock_pct: float = 90.0
    clock_tolerance_mhz: float = 5.0
    busy_gpu_util_pct: float = 60.0


FATAL_REASON_PREFIXES = (
    "fatal-cuda-output",
    "fatal-q2rtx-output",
    "nvidia-xid-detected",
    "q2rtx-selected-nvidia-gpu-idle",
    "timedemo-timeout",
    "timedemo-metrics-invalid",
    "timedemo-metrics-missing",
    "timedemo-nonzero-exit",
    "timedemo-unexpected-frame-count",
    "timedemo-frame-count-drift",
)

LOW_CLOCK_PREFIXES = (
    "telemetry-live-core_clock",
    "telemetry-live-core_clock-avg",
    "core_clock-regression",
)


def evaluate_stable_run(
    result: Any,
    *,
    baseline_frames: int | None,
    baseline_fps: float | None,
    baseline_power_w: float | None,
    baseline_core_clock_mhz: float | None,
    power_limit_w: int | None,
    cuda_required: bool,
    companion_result: Any | None = None,
    fatal_output_found: bool = False,
    xid_found: bool = False,
    stop_requested: bool = False,
    thresholds: StabilityThresholds = StabilityThresholds(),
) -> StableRunDecision:
    """Return the strict pass/fail decision for one Auto-UV stability probe."""

    if stop_requested:
        return _fail(
            FailureKind.USER_STOP,
            FailureSeverity.RECOVERABLE,
            "user stop requested",
        )
    if xid_found:
        return _fail(
            FailureKind.NVIDIA_XID,
            FailureSeverity.CRITICAL,
            "NVIDIA Xid detected after probe launch",
        )
    if fatal_output_found:
        return _fail(
            FailureKind.FATAL_OUTPUT,
            FailureSeverity.CRITICAL,
            "fatal output pattern detected",
        )

    reason = str(read_field(result, "reason") or "")
    log_path = _path_or_none(read_field(result, "log_path"))

    # Q2RTX success alone is not enough, but Q2RTX failure always invalidates the probe.
    if not bool(read_field(result, "success")):
        return classify_failed_result(reason, log_path=log_path)

    # Timedemo metrics prove the FPS result; missing or invalid metrics fail closed.
    timedemo_runs = list(read_field(result, "timedemo_runs") or [])
    timedemo_decision = evaluate_timedemo_runs(
        timedemo_runs,
        baseline_frames=baseline_frames,
        baseline_fps=baseline_fps,
        thresholds=thresholds,
        log_path=log_path,
    )
    if not timedemo_decision.passed:
        return timedemo_decision

    # Telemetry proves Q2RTX was under real load and held the requested clock.
    telemetry_decision = evaluate_loaded_telemetry(
        list(read_field(result, "telemetry_samples") or []),
        baseline_power_w=baseline_power_w,
        baseline_core_clock_mhz=baseline_core_clock_mhz,
        power_limit_w=power_limit_w,
        thresholds=thresholds,
        log_path=log_path,
    )
    if not telemetry_decision.passed:
        return telemetry_decision

    # CUDA is part of the workload contract when enabled; a CUDA failure fails the point.
    if cuda_required:
        cuda_decision = evaluate_cuda_companion(
            companion_result,
            log_path=log_path,
        )
        if not cuda_decision.passed:
            return cuda_decision

    return StableRunDecision(
        passed=True,
        failure_kind=FailureKind.NONE,
        severity=FailureSeverity.PASS,
        reason="stable run",
        evidence={
            "timedemo_runs": len(timedemo_runs),
            "telemetry_samples": len(
                list(read_field(result, "telemetry_samples") or [])
            ),
            "cuda_required": bool(cuda_required),
        },
        log_path=log_path,
    )


def classify_failed_result(
    reason: str,
    *,
    log_path: Path | None,
) -> StableRunDecision:
    text = str(reason or "")
    if text.startswith(LOW_CLOCK_PREFIXES):
        return _fail(
            FailureKind.LOW_CLOCK,
            FailureSeverity.RECOVERABLE,
            text or "loaded core clock below floor",
            log_path=log_path,
        )
    if text.startswith(FATAL_REASON_PREFIXES):
        kind = (
            FailureKind.NVIDIA_XID
            if text.startswith("nvidia-xid")
            else FailureKind.Q2RTX_FAILED
        )
        return _fail(
            kind,
            FailureSeverity.CRITICAL,
            text or "critical Q2RTX failure",
            log_path=log_path,
        )
    if text.startswith("cuda"):
        return _fail(
            FailureKind.CUDA_FAILED,
            FailureSeverity.CRITICAL,
            text,
            log_path=log_path,
        )
    return _fail(
        FailureKind.Q2RTX_FAILED,
        FailureSeverity.RECOVERABLE,
        text or "Q2RTX probe failed",
        log_path=log_path,
    )


def evaluate_timedemo_runs(
    timedemo_runs: list[Any],
    *,
    baseline_frames: int | None,
    baseline_fps: float | None,
    thresholds: StabilityThresholds,
    log_path: Path | None,
) -> StableRunDecision:
    if not timedemo_runs:
        return _fail(
            FailureKind.METRICS_MISSING,
            FailureSeverity.CRITICAL,
            "timedemo metrics missing",
            log_path=log_path,
        )
    average_fps_floor = (
        float(baseline_fps) * _percent(thresholds.min_average_fps_pct)
        if baseline_fps is not None
        else None
    )
    single_run_fps_floor = (
        float(baseline_fps) * _percent(thresholds.min_single_run_fps_pct)
        if baseline_fps is not None
        else None
    )
    # One slow timedemo loop is noise; sustained average loss is a real regression.
    fps_values: list[float] = []
    for run_number, run in enumerate(timedemo_runs, start=1):
        frames = read_field(run, "frames")
        seconds = read_field(run, "seconds")
        fps = read_field(run, "fps")
        if frames is None or seconds is None or fps is None:
            return _fail(
                FailureKind.METRICS_INVALID,
                FailureSeverity.CRITICAL,
                "timedemo run has missing frames/seconds/fps",
                evidence={"run": _run_evidence(run)},
                log_path=log_path,
            )
        if int(frames) <= 0 or float(seconds) <= 0.0 or float(fps) <= 0.0:
            return _fail(
                FailureKind.METRICS_INVALID,
                FailureSeverity.CRITICAL,
                "timedemo run has non-positive metrics",
                evidence={"run": _run_evidence(run)},
                log_path=log_path,
            )
        if baseline_frames is not None and int(frames) != int(baseline_frames):
            return _fail(
                FailureKind.FRAME_COUNT_REGRESSION,
                FailureSeverity.CRITICAL,
                "timedemo frame count changed",
                evidence={
                    "current_frames": int(frames),
                    "baseline_frames": int(baseline_frames),
                },
                log_path=log_path,
            )
        fps_values.append(float(fps))
        if (
            single_run_fps_floor is not None
            and float(fps) < float(single_run_fps_floor)
        ):
            run_index = read_field(run, "run_index") or run_number
            return _fail(
                FailureKind.FPS_REGRESSION,
                FailureSeverity.RECOVERABLE,
                (
                    f"timedemo single-run FPS below floor "
                    f"current={float(fps):.2f} "
                    f"floor={float(single_run_fps_floor):.2f} run={int(run_index)}"
                ),
                evidence={
                    "current_fps": float(fps),
                    "floor_fps": float(single_run_fps_floor),
                    "run_index": int(run_index),
                },
                log_path=log_path,
            )
    average_fps = sum(fps_values) / len(fps_values)
    if average_fps_floor is not None and average_fps < float(average_fps_floor):
        return _fail(
            FailureKind.FPS_REGRESSION,
            FailureSeverity.RECOVERABLE,
            (
                f"timedemo average FPS below floor current={average_fps:.2f} "
                f"floor={float(average_fps_floor):.2f} runs={len(fps_values)}"
            ),
            evidence={
                "average_fps": float(average_fps),
                "floor_fps": float(average_fps_floor),
                "runs": len(fps_values),
            },
            log_path=log_path,
        )
    return _pass("timedemo metrics stable", log_path=log_path)


def evaluate_loaded_telemetry(
    telemetry_samples: list[Any],
    *,
    baseline_power_w: float | None,
    baseline_core_clock_mhz: float | None,
    power_limit_w: int | None,
    thresholds: StabilityThresholds,
    log_path: Path | None,
) -> StableRunDecision:
    if not telemetry_samples:
        return _fail(
            FailureKind.LOAD_LOST,
            FailureSeverity.CRITICAL,
            "telemetry samples missing",
            log_path=log_path,
        )
    power_floor_w = (
        float(baseline_power_w) * _percent(thresholds.min_power_pct)
        if baseline_power_w is not None
        else None
    )
    if power_floor_w is None:
        power_floor_w = derive_active_power_floor_w(
            telemetry_samples,
            power_limit_w=power_limit_w,
            use_power_limit_floor=power_limit_w is not None,
        )
    busy_samples = [
        sample
        for sample in telemetry_samples
        if sample_is_busy(
            sample,
            busy_power_floor_w=power_floor_w,
            busy_gpu_util_pct=float(thresholds.busy_gpu_util_pct),
        )
    ]
    if not busy_samples:
        return _fail(
            FailureKind.LOAD_LOST,
            FailureSeverity.CRITICAL,
            "no busy telemetry samples",
            evidence={"power_floor_w": power_floor_w},
            log_path=log_path,
        )
    if baseline_core_clock_mhz is not None:
        floor_mhz = float(baseline_core_clock_mhz) * _percent(
            thresholds.min_core_clock_pct
        )
        clocks = [
            float(read_field(sample, "core_clock_mhz"))
            for sample in busy_samples
            if read_field(sample, "core_clock_mhz") is not None
        ]
        if not clocks:
            return _fail(
                FailureKind.LOW_CLOCK,
                FailureSeverity.RECOVERABLE,
                "busy core-clock telemetry missing",
                log_path=log_path,
            )
        avg_clock_mhz = sum(clocks) / len(clocks)
        if avg_clock_mhz < floor_mhz - float(thresholds.clock_tolerance_mhz):
            return _fail(
                FailureKind.LOW_CLOCK,
                FailureSeverity.RECOVERABLE,
                "average busy core clock below floor",
                evidence={
                    "avg_core_clock_mhz": avg_clock_mhz,
                    "floor_mhz": floor_mhz,
                    "busy_samples": len(busy_samples),
                },
                log_path=log_path,
            )
    return _pass("loaded telemetry stable", log_path=log_path)


def evaluate_cuda_companion(
    companion_result: Any | None,
    *,
    log_path: Path | None,
) -> StableRunDecision:
    if companion_result is None:
        return _fail(
            FailureKind.CUDA_FAILED,
            FailureSeverity.CRITICAL,
            "CUDA companion result missing",
            log_path=log_path,
        )
    if not bool(read_field(companion_result, "success")):
        return _fail(
            FailureKind.CUDA_FAILED,
            FailureSeverity.CRITICAL,
            str(read_field(companion_result, "reason") or "CUDA companion failed"),
            log_path=log_path,
        )
    return _pass("CUDA companion stable", log_path=log_path)


def sample_is_busy(
    sample: Any,
    *,
    busy_power_floor_w: float | None,
    busy_gpu_util_pct: float,
) -> bool:
    gpu_util_pct = read_field(sample, "gpu_util_pct")
    if gpu_util_pct is not None and float(gpu_util_pct) >= float(busy_gpu_util_pct):
        return True
    power_w = read_field(sample, "power_w")
    return (
        power_w is not None
        and busy_power_floor_w is not None
        and float(power_w) >= float(busy_power_floor_w)
    )


def _pass(reason: str, *, log_path: Path | None) -> StableRunDecision:
    return StableRunDecision(
        passed=True,
        failure_kind=FailureKind.NONE,
        severity=FailureSeverity.PASS,
        reason=str(reason),
        log_path=log_path,
    )


def _fail(
    kind: FailureKind,
    severity: FailureSeverity,
    reason: str,
    *,
    evidence: dict[str, Any] | None = None,
    log_path: Path | None = None,
) -> StableRunDecision:
    return StableRunDecision(
        passed=False,
        failure_kind=kind,
        severity=severity,
        reason=str(reason),
        evidence=dict(evidence or {}),
        log_path=log_path,
    )


def _run_evidence(run: Any) -> dict[str, Any]:
    return {
        "frames": read_field(run, "frames"),
        "seconds": read_field(run, "seconds"),
        "fps": read_field(run, "fps"),
        "run_index": read_field(run, "run_index"),
    }


def _path_or_none(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value)
