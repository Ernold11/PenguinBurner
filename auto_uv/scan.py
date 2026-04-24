#!/usr/bin/env python3

from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
import signal
from typing import Callable

from afterburner.vfcurve import describe_afterburner_dynamic_lock
from hidden_nvapi_vf import (
    create_hidden_vf_curve_reader,
    get_hidden_vf_curve_reader_last_error,
)
from hidden_nvml_voltage import create_hidden_voltage_reader
from afterburner.import_vf_curve import apply_plan
from nvml_gpu_policy import NvmlGpuPolicyController
from nvidia_runtime_defaults import (
    build_runtime_default_plan,
    reset_nvidia_runtime_defaults,
)
from stability.q2rtx import (
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    StabilityTestError,
    print_q2rtx_stability_result,
    query_gpu_metrics,
    run_q2rtx_stability_test,
)

from .constants import NVML_SUCCESS
from .models import (
    AutoUvCurveCandidate,
    AutoUvError,
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
)
from .artifacts import (
    _clear_uv_probe_in_progress,
    _consume_interrupted_uv_probe_marker,
    _load_uv_unsafe_voltage_entries,
    _record_unsafe_uv_voltage,
    _write_final_curve_snapshot,
    _write_latest_verified_uv_result,
    _write_uv_probe_in_progress,
    _write_saved_uv_state,
    _write_stable_uv_result,
    _write_uv_result_snapshot,
)
from .curve_planning import (
    _build_descended_plan,
    _build_flatten_target,
    _choose_sustained_clock_target,
    _find_lock_voltage_for_clock,
    _higher_voltage_bins,
    _make_curve_candidate,
    _nearest_voltage_bin,
    _next_higher_voltage_bin,
    _next_search_candidate_voltage_mv,
    _unsafe_min_search_voltage_mv,
    _validate_auto_uv_source_plan,
)
from .fan_tuning import write_auto_uv_fan_payload
from .probe_metrics import (
    _baseline_value,
    _derive_active_core_clock_mhz,
    _derive_loaded_voltage_band_mv,
    _derive_power_saturated_clock_mhz,
    _evaluate_probe,
    _history_average,
    _latest_non_companion_probe,
    _mean,
    _saturated_tail_samples,
    _summarize_probe,
    _temperature_normalized_efficiency_delta,
)
from .probe_config import (
    _budget_final_probe_durations,
    _cuda_bruteforce_companion_command,
    _normalize_probe_config,
    _short_probe_config,
    _stability_probe_config_for_voltage_band,
)
from .tuning import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_METRIC_TUNING,
    AUTO_UV_PROBE_TUNING,
    AUTO_UV_STALL_TUNING,
    AUTO_UV_VOLTAGE_PHASE_TUNING,
)
from .user_output import (
    format_probe_summary as _format_probe_summary,
    format_user_value as _format_user_value,
    log_benchmark as _log_benchmark,
    log_fan_curve_ascii_chart as _log_fan_curve_ascii_chart,
    log_final_summary as _log_final_summary,
    log_phase as _log_phase,
    log_user_candidate_intro as _log_user_candidate_intro,
    log_user_candidate_result as _log_user_candidate_result,
    log_user_readable_final_summary as _log_user_readable_final_summary,
    log_user_stage as _log_user_stage,
    log_vf_ascii_chart as _log_vf_ascii_chart,
    log_vf_point_list as _log_vf_point_list,
)


@dataclass(frozen=True, slots=True)
class _AutoUvScanSettings:
    q2rtx_config: Q2RTXStabilityConfig
    final_clock_drop_margin_pct: float
    min_performance_core_clock_pct: float
    preserve_vanilla_below_mv: int | None
    configured_max_drop_pct: float
    final_verification_duration_s: int
    efficiency_stop_streak: int
    min_efficiency_stop_voltage_drop_pct: float


def _percent(value: float | int) -> float:
    return max(0.0, float(value) / 100.0)


class _NvmlDeviceSession:
    def __init__(self, gpu_index: int):
        self._gpu_index = int(gpu_index)
        self._nvml = ctypes.CDLL("libnvidia-ml.so.1")
        self._device = ctypes.c_void_p()
        self._initialized = False
        self._bind()
        self._initialize()
        self._voltage_reader = create_hidden_voltage_reader(self._nvml)

    def _bind(self) -> None:
        self._nvml.nvmlInit_v2.restype = ctypes.c_int
        self._nvml.nvmlShutdown.restype = ctypes.c_int
        self._nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int

    def _initialize(self) -> None:
        rc = int(self._nvml.nvmlInit_v2())
        if rc != NVML_SUCCESS:
            raise AutoUvError(f"nvmlInit_v2 failed with NVML error {rc}")
        self._initialized = True

        rc = int(
            self._nvml.nvmlDeviceGetHandleByIndex_v2(
                ctypes.c_uint(self._gpu_index),
                ctypes.byref(self._device),
            )
        )
        if rc != NVML_SUCCESS:
            self.close()
            raise AutoUvError(
                f"nvmlDeviceGetHandleByIndex_v2 failed with NVML error {rc}"
            )

    def read_live_voltage_mv(self) -> int | None:
        if self._voltage_reader is None:
            return None
        voltage_uv = self._voltage_reader.read_microvolts(self._device)
        if voltage_uv is None:
            return None
        return int(round(int(voltage_uv) / 1000.0))

    def voltage_reader_available(self) -> bool:
        return self._voltage_reader is not None

    def close(self) -> None:
        if self._initialized:
            self._nvml.nvmlShutdown()
            self._initialized = False


class _ProbeClockCeilingController:
    def __init__(
        self, flatten_target: dict, policy_controller: NvmlGpuPolicyController
    ):
        self._flatten_target = dict(flatten_target)
        self._policy_controller = policy_controller
        self._range_lock: dict | None = None
        self._active = False

    @property
    def target_clock_mhz(self) -> int:
        return int(self._flatten_target["lock_clock_mhz"])

    @property
    def target_voltage_mv(self) -> int | None:
        value = self._flatten_target.get("lock_voltage_mv")
        return int(value) if value is not None else None

    def apply(self) -> dict:
        supported_steps = self._policy_controller.get_supported_core_clock_steps_mhz()
        requested_min_clock_mhz = (
            supported_steps[0] if supported_steps else self.target_clock_mhz
        )
        self._range_lock = self._policy_controller.apply_locked_core_clock_range_mhz(
            requested_min_clock_mhz,
            self.target_clock_mhz,
            prefer_max_not_above=True,
            snap_to_supported=True,
        )
        self._active = True
        return dict(self._range_lock)

    def retarget(
        self, *, lock_clock_mhz: int, lock_voltage_mv: int | None = None
    ) -> dict:
        self._flatten_target["lock_clock_mhz"] = int(lock_clock_mhz)
        if lock_voltage_mv is not None:
            self._flatten_target["lock_voltage_mv"] = int(lock_voltage_mv)
        if self._active:
            self._policy_controller.reset_locked_core_clocks()
            self._active = False
        return self.apply()

    def describe(self) -> str:
        if self._range_lock is None:
            return describe_afterburner_dynamic_lock(self._flatten_target)
        requested_max = int(self._range_lock["requested_max_clock_mhz"])
        applied_max = int(self._range_lock["applied_max_clock_mhz"])
        voltage_mv = self.target_voltage_mv
        ceiling_text = (
            f"{requested_max}MHz"
            if requested_max == applied_max
            else f"{requested_max}->{applied_max}MHz"
        )
        if voltage_mv is not None:
            ceiling_text += f"@{voltage_mv}mV"
        return (
            f"{describe_afterburner_dynamic_lock(self._flatten_target)}, "
            f"ceiling={ceiling_text}"
        )

    def close(self) -> None:
        if not self._active:
            return
        self._policy_controller.reset_locked_core_clocks()
        self._active = False


def _is_power_up_efficiency_down_regression(
    previous_probe: AutoUvProbeSummary | None,
    candidate_probe: AutoUvProbeSummary | None,
    efficiency_delta: dict[str, float | bool | None],
) -> bool:
    if previous_probe is None or candidate_probe is None:
        return False
    measured_voltage_drop_mv = efficiency_delta.get("measured_voltage_drop_mv")
    if measured_voltage_drop_mv is None or float(measured_voltage_drop_mv) <= 0.0:
        return False
    previous_power_w = efficiency_delta.get("previous_power_w")
    candidate_power_w = efficiency_delta.get("candidate_power_w")
    previous_fps_per_w = efficiency_delta.get("previous_fps_per_w")
    candidate_fps_per_w = efficiency_delta.get("candidate_fps_per_w")
    if previous_power_w is None or candidate_power_w is None:
        return False
    if previous_fps_per_w is None or candidate_fps_per_w is None:
        return False
    fps_per_w_delta_pct = (
        (float(candidate_fps_per_w) - float(previous_fps_per_w))
        / float(previous_fps_per_w)
        * 100.0
        if float(previous_fps_per_w) != 0.0
        else 0.0
    )
    return float(candidate_power_w) > float(previous_power_w) and float(
        fps_per_w_delta_pct
    ) <= float(AUTO_UV_METRIC_TUNING.min_temp_normalized_fps_per_w_improvement_pct)


def _probe_stabilization_search(
    *,
    reader,
    plan_source: list[dict],
    failure_voltage_mv: int,
    failure_live_voltage_mv: int | None,
    minimum_candidate_voltage_mv: int | None,
    target_clock_mhz: int,
    q2rtx_config: Q2RTXStabilityConfig,
    stable_history: list[AutoUvProbeSummary],
    nvml_session: _NvmlDeviceSession,
    clock_ceiling: _ProbeClockCeilingController | None,
    log: Callable[[str], None],
    probe_history: list[AutoUvProbeSummary],
    baseline_probe: AutoUvProbeSummary | None,
    initial_target_voltage_mv: int,
    initial_probe_clock_mhz: float | None,
    power_limit_w: int | None,
    min_performance_core_clock_pct: float,
    reset_plan: list[dict] | None = None,
) -> tuple[AutoUvCurveCandidate | None, AutoUvProbeSummary | None, object | None]:
    search_start_mv = (
        int(failure_live_voltage_mv)
        if failure_live_voltage_mv is not None
        else int(failure_voltage_mv)
    )
    recovery_floor_mv = (
        int(minimum_candidate_voltage_mv)
        if minimum_candidate_voltage_mv is not None
        else (
            _next_higher_voltage_bin(plan_source, int(failure_voltage_mv))
            or int(failure_voltage_mv)
        )
    )
    upward_bins = [
        int(value)
        for value in _higher_voltage_bins(plan_source, int(recovery_floor_mv) - 1)
        if int(value) >= int(recovery_floor_mv)
    ]

    for recovery_voltage_mv in upward_bins:
        recovery_candidate = _make_curve_candidate(
            plan_source,
            candidate_voltage_mv=int(recovery_voltage_mv),
            target_clock_mhz=int(target_clock_mhz),
            label="stabilize-upward-search",
        )
        _log_phase(
            log,
            "stabilize",
            f"search-start={search_start_mv}mV floor={recovery_floor_mv}mV try={recovery_candidate.candidate_voltage_mv}mV@{recovery_candidate.target_clock_mhz}MHz",
        )
        _log_vf_ascii_chart(
            log,
            plan=recovery_candidate.plan,
            target_clock_mhz=recovery_candidate.target_clock_mhz,
            candidate_voltage_mv=recovery_candidate.candidate_voltage_mv,
        )
        _log_vf_point_list(
            log,
            plan=recovery_candidate.plan,
            label=(
                f"stabilize target={recovery_candidate.target_clock_mhz}MHz "
                f"voltage={recovery_candidate.candidate_voltage_mv}mV"
            ),
        )
        if clock_ceiling is not None:
            clock_ceiling.retarget(
                lock_clock_mhz=int(recovery_candidate.target_clock_mhz),
                lock_voltage_mv=int(recovery_candidate.candidate_voltage_mv),
            )
            _log_phase(log, "ceiling", clock_ceiling.describe())
        recovery_probe_config = _stability_probe_config_for_voltage_band(
            q2rtx_config,
            initial_target_voltage_mv=int(initial_target_voltage_mv),
            candidate_voltage_mv=int(recovery_candidate.candidate_voltage_mv),
        )
        recovery_summary, recovery_result = _probe_voltage_candidate(
            reader=reader,
            candidate_plan=recovery_candidate.plan,
            candidate_voltage_mv=recovery_candidate.candidate_voltage_mv,
            lock_clock_mhz=recovery_candidate.target_clock_mhz,
            q2rtx_config=recovery_probe_config,
            stable_history=stable_history,
            initial_probe_clock_mhz=initial_probe_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label="stabilize",
            power_limit_w=power_limit_w,
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            reset_plan=reset_plan,
        )
        probe_history.append(recovery_summary)
        _log_benchmark(
            log,
            phase="stabilize",
            probe=recovery_summary,
            reference_probe=baseline_probe,
            reference_label="initial",
        )
        if recovery_result.success:
            recovery_error = _evaluate_probe(
                recovery_summary,
                stable_history=stable_history,
                min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            )
            if not recovery_error:
                return recovery_candidate, recovery_summary, recovery_result
            _log_phase(
                log,
                "stabilize",
                f"rejected {recovery_error} probe={_format_probe_summary(recovery_summary)}",
            )
    return None, None, None


def _describe_guardrails(
    stable_history: list[AutoUvProbeSummary],
    *,
    min_performance_core_clock_pct: float | None = None,
) -> str:
    if not stable_history:
        return "baseline-only"

    baseline_frames = stable_history[0].frames_per_run
    baseline_avg_core_clock = _baseline_value(stable_history, "avg_core_clock_mhz")
    baseline_avg_loop_s = _baseline_value(stable_history, "avg_seconds_per_run")
    if min_performance_core_clock_pct is None:
        min_performance_core_clock_pct = (
            AUTO_UV_METRIC_TUNING.min_performance_core_clock_pct
        )
    parts = []
    if baseline_frames is not None:
        parts.append(f"frames={baseline_frames}")
    if baseline_avg_loop_s is not None:
        parts.append(
            "stall-ceil="
            f"{max(AUTO_UV_STALL_TUNING.timeout_min_s, baseline_avg_loop_s * AUTO_UV_STALL_TUNING.timeout_multiplier):.1f}s"
        )
    if baseline_avg_core_clock is not None:
        parts.append(
            "core_clock-floor="
            f"{baseline_avg_core_clock * _percent(float(min_performance_core_clock_pct)):.1f}MHz"
        )
    return " ".join(parts) if parts else "baseline-only"


def _latest_reference_voltage_mv(
    history: list[AutoUvProbeSummary],
    fallback_voltage_mv: float | None,
) -> float | None:
    probe = _latest_non_companion_probe(history)
    if probe is not None and probe.avg_voltage_mv is not None:
        return float(probe.avg_voltage_mv)
    return fallback_voltage_mv


def _probe_voltage_candidate(
    *,
    reader,
    candidate_plan: list[dict],
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    q2rtx_config: Q2RTXStabilityConfig,
    stable_history: list[AutoUvProbeSummary],
    initial_probe_clock_mhz: float | None,
    nvml_session: _NvmlDeviceSession,
    log: Callable[[str], None],
    phase_label: str,
    log_context: str | None = None,
    power_limit_w: int | None = None,
    enforce_target_core_clock_floor: bool = True,
    show_candidate_target: bool = True,
    summarize_saturated_tail: bool = False,
    use_power_limit_floor: bool = False,
    min_performance_core_clock_pct: float | None = None,
    reset_plan: list[dict] | None = None,
) -> tuple[AutoUvProbeSummary, Q2RTXStabilityResult]:
    frame_reference = (
        int(stable_history[0].frames_per_run)
        if stable_history and stable_history[0].frames_per_run is not None
        else None
    )
    if min_performance_core_clock_pct is None:
        min_performance_core_clock_pct = (
            AUTO_UV_METRIC_TUNING.min_performance_core_clock_pct
        )
    target_core_clock_floor_mhz = (
        float(initial_probe_clock_mhz) * _percent(float(min_performance_core_clock_pct))
        if enforce_target_core_clock_floor and initial_probe_clock_mhz is not None
        else (
            float(lock_clock_mhz) * _percent(float(min_performance_core_clock_pct))
            if enforce_target_core_clock_floor
            else None
        )
    )

    latest_non_companion_probe = _latest_non_companion_probe(stable_history)
    progress_state = {
        "last_completed_runs": 0,
        "last_progress_elapsed_s": 0.0,
        "expected_loop_s": (
            latest_non_companion_probe.avg_seconds_per_run
            if latest_non_companion_probe is not None
            else _history_average(stable_history, "avg_seconds_per_run")
        ),
        "low_core_clock_streak": 0,
    }
    busy_power_floor_w = None
    reference_probe = _latest_non_companion_probe(stable_history)
    if reference_probe is not None and reference_probe.avg_power_w is not None:
        busy_power_floor_w = float(reference_probe.avg_power_w) * _percent(
            AUTO_UV_STALL_TUNING.busy_reference_power_pct
        )
    elif power_limit_w is not None and int(power_limit_w) > 0:
        busy_power_floor_w = float(power_limit_w) * _percent(
            AUTO_UV_STALL_TUNING.busy_power_limit_pct
        )

    def _format_live_status(state: dict) -> str:
        elapsed_s = float(state.get("elapsed_s", 0.0))
        latest_sample = state.get("latest_sample")
        running = str(state.get("running", "")).strip()
        live_voltage_mv = nvml_session.read_live_voltage_mv()

        parts = [f"elapsed={elapsed_s:.1f}s"]
        if show_candidate_target:
            parts.insert(0, f"target={lock_clock_mhz}MHz")
            parts.insert(0, f"candidate={candidate_voltage_mv}mV")
        if running:
            parts.append(f"running={running}")
        if live_voltage_mv is not None:
            parts.append(f"live={live_voltage_mv}mV")
        if latest_sample is not None and latest_sample.power_w is not None:
            parts.append(f"power={float(latest_sample.power_w):.1f}W")
        if latest_sample is not None and latest_sample.core_clock_mhz is not None:
            parts.append(f"core_clock={float(latest_sample.core_clock_mhz):.0f}MHz")
        if latest_sample is not None and latest_sample.temperature_c is not None:
            parts.append(f"temp={float(latest_sample.temperature_c):.0f}C")
        else:
            parts.append("temp=n/a")
        if latest_sample is not None and latest_sample.fan_speed_pct is not None:
            parts.append(f"fan={float(latest_sample.fan_speed_pct):.0f}%")
        else:
            parts.append("fan=n/a")
        if target_core_clock_floor_mhz is not None:
            parts.append(f"target-floor={target_core_clock_floor_mhz:.1f}MHz")
        return " ".join(parts)

    def _progress_callback(state: dict) -> None:
        completed_runs = int(state.get("completed_runs", 0))
        elapsed_s = float(state.get("elapsed_s", 0.0))
        timedemo_runs = list(state.get("timedemo_runs") or [])
        if completed_runs > int(progress_state["last_completed_runs"]):
            progress_state["last_completed_runs"] = completed_runs
            progress_state["last_progress_elapsed_s"] = elapsed_s
            current_loop_s = _mean([float(run.seconds) for run in timedemo_runs])
            if current_loop_s is not None:
                progress_state["expected_loop_s"] = current_loop_s
            last_run = state.get("last_run")
            if last_run is not None:
                latest_sample = state.get("latest_sample")
                sample_parts = []
                if latest_sample is not None and latest_sample.power_w is not None:
                    sample_parts.append(f"power={float(latest_sample.power_w):.1f}W")
                if (
                    latest_sample is not None
                    and latest_sample.temperature_c is not None
                ):
                    sample_parts.append(
                        f"temp={float(latest_sample.temperature_c):.0f}C"
                    )
                else:
                    sample_parts.append("temp=n/a")
                if (
                    latest_sample is not None
                    and latest_sample.fan_speed_pct is not None
                ):
                    sample_parts.append(
                        f"fan={float(latest_sample.fan_speed_pct):.0f}%"
                    )
                else:
                    sample_parts.append("fan=n/a")
                _log_phase(
                    log,
                    f"{phase_label}-pass",
                    (
                        f"{str(log_context).strip()} "
                        if log_context is not None and str(log_context).strip()
                        else ""
                    )
                    + f"run={int(last_run.run_index)} "
                    f"frames={int(last_run.frames)} "
                    f"fps={float(last_run.fps):.1f} "
                    f"seconds={float(last_run.seconds):.2f} " + " ".join(sample_parts),
                )
        live_status = _format_live_status(state)
        if log_context is not None and str(log_context).strip():
            live_status = f"{str(log_context).strip()} {live_status}"
        _log_phase(log, f"{phase_label}-live", live_status)

    def _abort_callback(state: dict) -> str | None:
        fatal_output_matches = list(state.get("fatal_output_matches") or [])
        if fatal_output_matches:
            return "fatal-q2rtx-output"

        expected_frames_per_run = state.get("expected_frames_per_run")
        frame_ref = frame_reference
        if frame_ref is None and expected_frames_per_run is not None:
            frame_ref = int(expected_frames_per_run)

        for run in list(state.get("new_timedemo_runs") or []):
            frames = int(run.frames)
            seconds = float(run.seconds)
            fps = float(run.fps)
            if frames <= 0 or seconds <= 0.0 or fps <= 0.0:
                return "timedemo-metrics-invalid"
            if frame_ref is not None and frames != frame_ref:
                return (
                    f"timedemo-live-frame-count current={frames} "
                    f"expected={frame_ref} run={int(run.run_index)}"
                )

        core_clock_samples = [
            float(sample.core_clock_mhz)
            for sample in list(state.get("telemetry_samples") or [])
            if sample is not None and sample.core_clock_mhz is not None
        ]
        latest_sample = state.get("latest_sample")
        live_core_clock_mhz = (
            float(latest_sample.core_clock_mhz)
            if latest_sample is not None and latest_sample.core_clock_mhz is not None
            else None
        )
        running_avg_core_clock = _mean(core_clock_samples)
        if (
            target_core_clock_floor_mhz is not None
            and live_core_clock_mhz is not None
            and len(core_clock_samples)
            >= AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
        ):
            if live_core_clock_mhz < float(target_core_clock_floor_mhz):
                progress_state["low_core_clock_streak"] = (
                    int(progress_state.get("low_core_clock_streak", 0)) + 1
                )
            else:
                progress_state["low_core_clock_streak"] = 0
            if (
                int(progress_state["low_core_clock_streak"])
                >= AUTO_UV_METRIC_TUNING.target_core_clock_low_streak_samples
            ):
                return (
                    f"telemetry-live-core_clock current={live_core_clock_mhz:.1f}MHz "
                    f"floor={target_core_clock_floor_mhz:.1f}MHz"
                )
        if (
            target_core_clock_floor_mhz is not None
            and running_avg_core_clock is not None
            and len(core_clock_samples)
            >= AUTO_UV_STALL_TUNING.avg_core_clock_abort_min_samples
            and running_avg_core_clock < float(target_core_clock_floor_mhz)
        ):
            return (
                f"telemetry-live-core_clock-avg current={running_avg_core_clock:.1f}MHz "
                f"floor={target_core_clock_floor_mhz:.1f}MHz"
            )

        expected_loop_s = progress_state.get("expected_loop_s")
        if expected_loop_s is not None:
            completed_runs = int(state.get("completed_runs", 0))
            if completed_runs <= 0:
                return None
            elapsed_s = float(state.get("elapsed_s", 0.0))
            last_progress_elapsed_s = float(progress_state["last_progress_elapsed_s"])
            idle_s = elapsed_s - last_progress_elapsed_s
            stall_limit_s = max(
                AUTO_UV_STALL_TUNING.timeout_min_s,
                float(expected_loop_s) * AUTO_UV_STALL_TUNING.timeout_multiplier,
            )
            if idle_s > stall_limit_s:
                latest_sample = state.get("latest_sample")
                live_gpu_util = (
                    getattr(latest_sample, "gpu_util_pct", None)
                    if latest_sample is not None
                    else None
                )
                live_power_w = (
                    getattr(latest_sample, "power_w", None)
                    if latest_sample is not None
                    else None
                )
                gpu_is_busy = (
                    live_gpu_util is not None
                    and float(live_gpu_util) >= AUTO_UV_STALL_TUNING.busy_gpu_util_pct
                ) or (
                    live_power_w is not None
                    and busy_power_floor_w is not None
                    and float(live_power_w) >= float(busy_power_floor_w)
                )
                if gpu_is_busy:
                    return None
                return (
                    f"timedemo-live-stall idle={idle_s:.1f}s "
                    f"stall={stall_limit_s:.1f}s completed={completed_runs}"
                )

    mark_in_progress = str(phase_label) in {"candidate", "final-verify", "stabilize"}

    def _record_probe_unsafe(reason: str, details: dict | None = None) -> None:
        if not mark_in_progress:
            return
        try:
            blacklist_path, _unsafe_entry = _record_unsafe_uv_voltage(
                candidate_voltage_mv=int(candidate_voltage_mv),
                lock_clock_mhz=int(lock_clock_mhz),
                reason=str(reason),
                phase=str(phase_label),
                details=details,
            )
        except Exception as exc:
            _log_phase(
                log,
                "blacklist",
                f"failed-to-record-unsafe-voltage voltage={int(candidate_voltage_mv)}mV "
                f"target={int(lock_clock_mhz)}MHz reason={reason} error={exc}",
            )
            return
        detail_text = ""
        if details and details.get("result_reason"):
            detail_text = f" result={details['result_reason']}"
        elif details and details.get("exception"):
            detail_text = f" exception={details['exception']}"
        _log_phase(
            log,
            "blacklist",
            f"unsafe-voltage-recorded voltage={int(candidate_voltage_mv)}mV "
            f"target={int(lock_clock_mhz)}MHz reason={reason}{detail_text} "
            f"path={blacklist_path}",
        )

    if mark_in_progress:
        _write_uv_probe_in_progress(
            phase=phase_label,
            candidate_voltage_mv=int(candidate_voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
            log_context=log_context,
        )
    try:
        live_voltage_before_mv = nvml_session.read_live_voltage_mv()
        current_sample = query_gpu_metrics(int(q2rtx_config.gpu_index))
        start_message = f"live-voltage-before={live_voltage_before_mv if live_voltage_before_mv is not None else 'n/a'}"
        start_message += (
            f" temp={float(current_sample.temperature_c):.0f}C"
            if current_sample is not None and current_sample.temperature_c is not None
            else " temp=n/a"
        )
        start_message += (
            f" fan={float(current_sample.fan_speed_pct):.0f}%"
            if current_sample is not None and current_sample.fan_speed_pct is not None
            else " fan=n/a"
        )
        if show_candidate_target:
            start_message = (
                f"candidate={candidate_voltage_mv}mV "
                f"target={lock_clock_mhz}MHz " + start_message
            )
        if log_context is not None and str(log_context).strip():
            start_message = f"{str(log_context).strip()} {start_message}"
        _log_phase(log, phase_label, start_message)
        if reset_plan is not None:
            apply_plan(reader, reset_plan)
            reader.refresh_points()
            _log_phase(
                log,
                phase_label,
                "reset runtime-default V/F curve before applying probe curve",
            )
        apply_plan(reader, candidate_plan)
        reader.refresh_points()
        probe_config = replace(
            q2rtx_config,
            progress_callback=_progress_callback,
            abort_callback=_abort_callback,
        )
        try:
            result = run_q2rtx_stability_test(probe_config)
        except StabilityTestError:
            raise
        except Exception as exc:
            _record_probe_unsafe(
                "stability-probe-exception",
                details={
                    "exception": f"{exc.__class__.__name__}: {exc}",
                    "used_companion_load": bool(q2rtx_config.companion_command),
                },
            )
            raise
        if result.success:
            print_q2rtx_stability_result(result)
        else:
            _record_probe_unsafe(
                "stability-probe-failed",
                details={
                    "result_reason": str(result.reason),
                    "workload_kind": str(result.workload_kind),
                    "workload_name": str(result.workload_name),
                    "shutdown_mode": str(result.shutdown_mode),
                    "process_exit_code": (
                        int(result.process_exit_code)
                        if result.process_exit_code is not None
                        else None
                    ),
                    "log_path": str(result.log_path),
                    "used_companion_load": bool(q2rtx_config.companion_command),
                },
            )
            controlled_abort_prefixes = (
                "telemetry-live-core_clock",
                "telemetry-live-core_clock-avg",
                "timedemo-live-frame-count",
            )
            if str(result.reason).startswith(controlled_abort_prefixes):
                requested_target = (
                    f"{result.timedemo_loops_requested} loops"
                    if result.timedemo_loops_requested is not None
                    else f"{result.duration_requested_s}s"
                )
                print("Auto-UV probe: ABORTED", flush=True)
                print(
                    f"Reason: {result.reason} | workload={result.workload_name} ({result.workload_kind}) | "
                    f"requested={requested_target} | observed={result.duration_observed_s:.1f}s",
                    flush=True,
                )
                print(
                    f"Executable: {result.executable_path} | workdir={result.workdir}",
                    flush=True,
                )
                print(f"Log: {result.log_path}", flush=True)
            else:
                print_q2rtx_stability_result(result)
        live_voltage_after_mv = nvml_session.read_live_voltage_mv()
        summary_samples = (
            _saturated_tail_samples(result.telemetry_samples)
            if summarize_saturated_tail
            else None
        )
        if summarize_saturated_tail and summary_samples is not None:
            _log_phase(
                log,
                phase_label,
                f"using saturated telemetry tail samples={len(summary_samples)}/"
                f"{len(result.telemetry_samples)} for baseline measurement",
            )
        summary = _summarize_probe(
            candidate_voltage_mv=candidate_voltage_mv,
            lock_clock_mhz=lock_clock_mhz,
            live_voltage_before_mv=live_voltage_before_mv,
            live_voltage_after_mv=live_voltage_after_mv,
            used_companion_load=bool(q2rtx_config.companion_command),
            power_limit_w=power_limit_w,
            result=result,
            telemetry_samples=summary_samples,
            use_power_limit_floor=use_power_limit_floor,
        )
        return summary, result
    finally:
        if mark_in_progress:
            _clear_uv_probe_in_progress()


def _scan_settings(
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
) -> _AutoUvScanSettings:
    if q2rtx_config.timedemo_loops is None and int(q2rtx_config.duration_s) <= 0:
        raise AutoUvError(
            "auto-UV voltage scan needs either a positive timedemo loop count or a positive duration"
        )

    normalized_q2rtx_config = _normalize_probe_config(q2rtx_config)
    final_clock_drop_margin_pct = runtime_options.get("auto_uv_max_clock_drop_pct")
    if final_clock_drop_margin_pct is None:
        final_clock_drop_margin_pct = AUTO_UV_METRIC_TUNING.max_core_clock_drop_pct
    final_clock_drop_margin_pct = max(
        0.0, min(100.0, float(final_clock_drop_margin_pct))
    )
    min_performance_core_clock_pct = max(
        0.0, 100.0 - float(final_clock_drop_margin_pct)
    )
    preserve_vanilla_below_mv = runtime_options.get("preserve_vanilla_below_mv")
    if preserve_vanilla_below_mv is not None:
        preserve_vanilla_below_mv = int(preserve_vanilla_below_mv)
    configured_max_drop_pct = runtime_options.get("auto_uv_max_drop_pct")
    if configured_max_drop_pct is None:
        configured_max_drop_pct = AUTO_UV_DEFAULTS.max_drop_pct
    configured_max_drop_pct = max(0.0, float(configured_max_drop_pct))
    final_verification_duration_s = int(
        runtime_options.get(
            "auto_uv_final_seconds",
            AUTO_UV_DEFAULTS.final_duration_s,
        )
        or AUTO_UV_DEFAULTS.final_duration_s
    )
    final_verification_duration_s = max(1, int(final_verification_duration_s))
    efficiency_stop_streak = int(
        runtime_options.get(
            "auto_uv_efficiency_stop_streak",
            AUTO_UV_DEFAULTS.efficiency_stop_streak,
        )
        if runtime_options.get("auto_uv_efficiency_stop_streak") is not None
        else AUTO_UV_DEFAULTS.efficiency_stop_streak
    )
    efficiency_stop_streak = max(0, int(efficiency_stop_streak))
    min_efficiency_stop_voltage_drop_pct = max(
        0.0,
        float(
            runtime_options.get(
                "auto_uv_min_efficiency_stop_drop_pct",
                AUTO_UV_METRIC_TUNING.min_efficiency_stop_voltage_drop_pct,
            )
            if runtime_options.get("auto_uv_min_efficiency_stop_drop_pct") is not None
            else AUTO_UV_METRIC_TUNING.min_efficiency_stop_voltage_drop_pct
        ),
    )
    return _AutoUvScanSettings(
        q2rtx_config=normalized_q2rtx_config,
        final_clock_drop_margin_pct=float(final_clock_drop_margin_pct),
        min_performance_core_clock_pct=float(min_performance_core_clock_pct),
        preserve_vanilla_below_mv=preserve_vanilla_below_mv,
        configured_max_drop_pct=float(configured_max_drop_pct),
        final_verification_duration_s=int(final_verification_duration_s),
        efficiency_stop_streak=int(efficiency_stop_streak),
        min_efficiency_stop_voltage_drop_pct=float(
            min_efficiency_stop_voltage_drop_pct
        ),
    )


def _build_voltage_scan_result(
    *,
    final_voltage_mv: int,
    final_lock_clock_mhz: int,
    probe_history: list[AutoUvProbeSummary],
    stable_history: list[AutoUvProbeSummary],
    final_probe: AutoUvProbeSummary | None,
) -> AutoUvVoltageScanResult:
    baseline_probe = stable_history[0] if stable_history else None
    return AutoUvVoltageScanResult(
        success=True,
        final_voltage_mv=int(final_voltage_mv),
        lock_clock_mhz=int(final_lock_clock_mhz),
        stop_reason="candidate-sweep-complete",
        failed_candidate_voltage_mv=None,
        probes=probe_history,
        baseline_core_clock_mhz=(
            float(baseline_probe.avg_core_clock_mhz)
            if baseline_probe is not None
            and baseline_probe.avg_core_clock_mhz is not None
            else None
        ),
        baseline_power_w=(
            float(baseline_probe.avg_power_w)
            if baseline_probe is not None and baseline_probe.avg_power_w is not None
            else None
        ),
        baseline_temperature_c=(
            float(baseline_probe.avg_temperature_c)
            if baseline_probe is not None
            and baseline_probe.avg_temperature_c is not None
            else None
        ),
        baseline_fan_speed_pct=(
            float(baseline_probe.avg_fan_speed_pct)
            if baseline_probe is not None
            and baseline_probe.avg_fan_speed_pct is not None
            else None
        ),
        baseline_efficiency_mhz_per_w=(
            float(baseline_probe.efficiency_mhz_per_w)
            if baseline_probe is not None
            and baseline_probe.efficiency_mhz_per_w is not None
            else None
        ),
        final_core_clock_mhz=(
            float(final_probe.avg_core_clock_mhz)
            if final_probe is not None and final_probe.avg_core_clock_mhz is not None
            else None
        ),
        final_power_w=(
            float(final_probe.avg_power_w)
            if final_probe is not None and final_probe.avg_power_w is not None
            else None
        ),
        final_temperature_c=(
            float(final_probe.avg_temperature_c)
            if final_probe is not None and final_probe.avg_temperature_c is not None
            else None
        ),
        final_fan_speed_pct=(
            float(final_probe.avg_fan_speed_pct)
            if final_probe is not None and final_probe.avg_fan_speed_pct is not None
            else None
        ),
        final_efficiency_mhz_per_w=(
            float(final_probe.efficiency_mhz_per_w)
            if final_probe is not None and final_probe.efficiency_mhz_per_w is not None
            else None
        ),
        core_clock_drop_mhz=(
            float(baseline_probe.avg_core_clock_mhz)
            - float(final_probe.avg_core_clock_mhz)
            if baseline_probe is not None
            and baseline_probe.avg_core_clock_mhz is not None
            and final_probe is not None
            and final_probe.avg_core_clock_mhz is not None
            else None
        ),
        core_clock_drop_pct=(
            (
                (
                    float(baseline_probe.avg_core_clock_mhz)
                    - float(final_probe.avg_core_clock_mhz)
                )
                / float(baseline_probe.avg_core_clock_mhz)
            )
            * 100.0
            if baseline_probe is not None
            and baseline_probe.avg_core_clock_mhz not in (None, 0.0)
            and final_probe is not None
            and final_probe.avg_core_clock_mhz is not None
            else None
        ),
        power_saved_w=(
            float(baseline_probe.avg_power_w) - float(final_probe.avg_power_w)
            if baseline_probe is not None
            and baseline_probe.avg_power_w is not None
            and final_probe is not None
            and final_probe.avg_power_w is not None
            else None
        ),
        power_saved_pct=(
            (
                (float(baseline_probe.avg_power_w) - float(final_probe.avg_power_w))
                / float(baseline_probe.avg_power_w)
            )
            * 100.0
            if baseline_probe is not None
            and baseline_probe.avg_power_w not in (None, 0.0)
            and final_probe is not None
            and final_probe.avg_power_w is not None
            else None
        ),
    )


def _run_candidate_sweep(
    *,
    log,
    reader,
    flattened_plan,
    start_voltage_mv,
    stable_plan,
    stable_voltage_mv,
    stable_lock_clock_mhz,
    stable_probe,
    stable_history,
    probe_history,
    first_candidate_voltage_mv,
    discovery_summary,
    lock_clock_mhz,
    q2rtx_config,
    measured_clock_mhz,
    nvml_session,
    translated_gpu_policy,
    runtime_default_plan,
    clock_ceiling,
    source_result,
    min_performance_core_clock_pct,
    min_search_voltage_mv,
    preserve_vanilla_below_mv,
    min_efficiency_stop_voltage_drop_pct,
    efficiency_stop_streak,
):
    candidate_attempt_count = 0
    failed_candidate_floor_mv = None
    candidate_voltage_mv = int(first_candidate_voltage_mv)
    non_improving_efficiency_streak = 0
    pending_efficiency_stop_curve = None
    while candidate_voltage_mv is not None:
        candidate_attempt_count += 1
        ratio = (
            float(candidate_voltage_mv) / float(start_voltage_mv)
            if int(start_voltage_mv) > 0
            else 1.0
        )
        if ratio > _percent(AUTO_UV_VOLTAGE_PHASE_TUNING.coarse_voltage_pct):
            phase = "coarse"
        elif ratio > _percent(AUTO_UV_VOLTAGE_PHASE_TUNING.medium_voltage_pct):
            phase = "medium"
        else:
            phase = "fine"
        reference_actual_voltage_mv = _latest_reference_voltage_mv(
            stable_history,
            discovery_summary.avg_voltage_mv,
        )
        candidate = _make_curve_candidate(
            flattened_plan,
            candidate_voltage_mv=int(candidate_voltage_mv),
            target_clock_mhz=int(lock_clock_mhz),
            label=f"voltage={candidate_voltage_mv}mV phase={phase}",
        )
        previous_stable_probe_for_iteration = stable_probe
        _log_user_candidate_intro(
            log,
            attempt=candidate_attempt_count,
            stable_voltage_mv=int(stable_voltage_mv),
            stable_lock_clock_mhz=int(stable_lock_clock_mhz),
            candidate_voltage_mv=int(candidate.candidate_voltage_mv),
            candidate_lock_clock_mhz=int(candidate.target_clock_mhz),
            start_voltage_mv=int(start_voltage_mv),
            min_search_voltage_mv=int(min_search_voltage_mv),
            phase=phase,
        )
        _log_phase(
            log,
            "candidate",
            f"{candidate_attempt_count} "
            f"stable={stable_voltage_mv}mV@{stable_lock_clock_mhz}MHz "
            f"try={candidate.candidate_voltage_mv}mV@{candidate.target_clock_mhz}MHz "
            f"step={candidate.candidate_voltage_mv - stable_voltage_mv:+d}mV "
            f"shape={candidate.label} "
            + f"{_describe_guardrails(stable_history, min_performance_core_clock_pct=float(min_performance_core_clock_pct))}",
        )
        _log_vf_ascii_chart(
            log,
            plan=candidate.plan,
            target_clock_mhz=candidate.target_clock_mhz,
            candidate_voltage_mv=candidate.candidate_voltage_mv,
        )
        _log_vf_point_list(
            log,
            plan=candidate.plan,
            label=(
                f"candidate target={candidate.target_clock_mhz}MHz "
                f"voltage={candidate.candidate_voltage_mv}mV"
            ),
        )
        if clock_ceiling is not None:
            clock_ceiling.retarget(
                lock_clock_mhz=int(candidate.target_clock_mhz),
                lock_voltage_mv=int(candidate.candidate_voltage_mv),
            )
            _log_phase(log, "ceiling", clock_ceiling.describe())
        probe_summary, probe_result = _probe_voltage_candidate(
            reader=reader,
            candidate_plan=candidate.plan,
            candidate_voltage_mv=candidate.candidate_voltage_mv,
            lock_clock_mhz=candidate.target_clock_mhz,
            q2rtx_config=_stability_probe_config_for_voltage_band(
                q2rtx_config,
                initial_target_voltage_mv=int(start_voltage_mv),
                candidate_voltage_mv=int(candidate.candidate_voltage_mv),
            ),
            stable_history=stable_history,
            initial_probe_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label="candidate",
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            use_power_limit_floor=(candidate_attempt_count <= 1),
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            reset_plan=runtime_default_plan,
        )
        probe_history.append(probe_summary)
        _log_benchmark(
            log,
            phase="candidate",
            probe=probe_summary,
            reference_probe=discovery_summary,
            reference_label="initial",
        )

        if not probe_result.success:
            failed_voltage_mv = int(candidate.candidate_voltage_mv)
            low_frequency_failure = str(probe_result.reason).startswith(
                ("telemetry-live-core_clock", "telemetry-live-core_clock-avg")
            )
            if low_frequency_failure:
                apply_plan(reader, stable_plan)
                reader.refresh_points()
                restored_live_mv = nvml_session.read_live_voltage_mv()
                _log_phase(
                    log,
                    "final",
                    f"low-frequency fail at {candidate.candidate_voltage_mv}mV; "
                    f"finishing with previous stable {stable_voltage_mv}mV@{stable_lock_clock_mhz}MHz "
                    f"restored-live-voltage={restored_live_mv if restored_live_mv is not None else 'n/a'}",
                )
                _log_user_candidate_result(
                    log,
                    attempt=candidate_attempt_count,
                    decision="STOPPED",
                    reason="This voltage could not hold the target core clock. The previous stable curve was restored.",
                    initial_probe=discovery_summary,
                    previous_probe=previous_stable_probe_for_iteration,
                    candidate_probe=probe_summary,
                    restored_voltage_mv=int(stable_voltage_mv),
                    restored_lock_clock_mhz=int(stable_lock_clock_mhz),
                )
                break
            recovery_candidate, recovery_summary, recovery_result = (
                _probe_stabilization_search(
                    reader=reader,
                    plan_source=source_result["plan"],
                    failure_voltage_mv=failed_voltage_mv,
                    failure_live_voltage_mv=probe_summary.live_voltage_after_mv,
                    minimum_candidate_voltage_mv=_next_higher_voltage_bin(
                        source_result["plan"], int(failed_voltage_mv)
                    ),
                    target_clock_mhz=int(candidate.target_clock_mhz),
                    q2rtx_config=q2rtx_config,
                    stable_history=stable_history,
                    nvml_session=nvml_session,
                    clock_ceiling=clock_ceiling,
                    log=log,
                    probe_history=probe_history,
                    baseline_probe=discovery_summary,
                    initial_target_voltage_mv=int(start_voltage_mv),
                    initial_probe_clock_mhz=measured_clock_mhz,
                    power_limit_w=translated_gpu_policy.get("power_limit_w"),
                    min_performance_core_clock_pct=float(
                        min_performance_core_clock_pct
                    ),
                    reset_plan=runtime_default_plan,
                )
            )
            recovery_accepted = False
            if (
                recovery_candidate is not None
                and recovery_summary is not None
                and recovery_result is not None
            ):
                stable_plan = recovery_candidate.plan
                stable_voltage_mv = int(recovery_candidate.candidate_voltage_mv)
                stable_lock_clock_mhz = int(recovery_candidate.target_clock_mhz)
                stable_probe = recovery_summary
                if not recovery_summary.used_companion_load:
                    stable_history.append(recovery_summary)
                    _write_latest_verified_uv_result(
                        plan=stable_plan,
                        lock_clock_mhz=int(stable_lock_clock_mhz),
                        voltage_mv=int(stable_voltage_mv),
                        probe=stable_probe,
                    )
                _log_phase(
                    log,
                    "retest",
                    "accepted " + _format_probe_summary(recovery_summary),
                )
                recovery_accepted = True

            apply_plan(reader, stable_plan)
            reader.refresh_points()
            restored_live_mv = nvml_session.read_live_voltage_mv()
            should_stop_descent = not recovery_accepted
            _log_phase(
                log,
                "reject",
                f"candidate={candidate.candidate_voltage_mv}mV "
                f"shape={candidate.label} "
                f"reason={probe_result.reason} "
                f"probe={_format_probe_summary(probe_summary)} "
                f"restored={stable_voltage_mv}mV "
                f"restored-live-voltage={restored_live_mv if restored_live_mv is not None else 'n/a'}",
            )
            _log_user_candidate_result(
                log,
                attempt=candidate_attempt_count,
                decision="REJECTED"
                if should_stop_descent
                else "REJECTED, THEN RECOVERED",
                reason=(
                    f"The probe failed: {probe_result.reason}. "
                    + (
                        "No safer replacement was found, so the scan will stop."
                        if should_stop_descent
                        else "A safer stable curve was found and will be used for the next step."
                    )
                ),
                initial_probe=discovery_summary,
                previous_probe=previous_stable_probe_for_iteration,
                candidate_probe=probe_summary,
                restored_voltage_mv=int(stable_voltage_mv),
                restored_lock_clock_mhz=int(stable_lock_clock_mhz),
            )
            if should_stop_descent:
                break
            failed_candidate_floor_mv = int(candidate.candidate_voltage_mv)
            candidate_voltage_mv = _next_search_candidate_voltage_mv(
                plan=flattened_plan,
                start_voltage_mv=int(start_voltage_mv),
                stable_voltage_mv=int(stable_voltage_mv),
                reference_actual_voltage_mv=_latest_reference_voltage_mv(
                    stable_history,
                    reference_actual_voltage_mv,
                ),
                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
                min_search_voltage_mv=min_search_voltage_mv,
                failed_floor_voltage_mv=failed_candidate_floor_mv,
            )
            continue

        evaluation_error = _evaluate_probe(
            probe_summary,
            stable_history=stable_history,
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
        )
        if evaluation_error:
            apply_plan(reader, stable_plan)
            reader.refresh_points()
            restored_live_mv = nvml_session.read_live_voltage_mv()
            _log_phase(
                log,
                "reject",
                f"{evaluation_error} "
                f"candidate={candidate.candidate_voltage_mv}mV "
                f"shape={candidate.label} "
                f"probe={_format_probe_summary(probe_summary)} "
                f"restored={stable_voltage_mv}mV "
                f"restored-live-voltage={restored_live_mv if restored_live_mv is not None else 'n/a'}",
            )
            _log_user_candidate_result(
                log,
                attempt=candidate_attempt_count,
                decision="REJECTED",
                reason=f"This candidate passed the timedemo but failed a guardrail: {evaluation_error}. The previous stable curve was restored.",
                initial_probe=discovery_summary,
                previous_probe=previous_stable_probe_for_iteration,
                candidate_probe=probe_summary,
                restored_voltage_mv=int(stable_voltage_mv),
                restored_lock_clock_mhz=int(stable_lock_clock_mhz),
            )
            failed_candidate_floor_mv = int(candidate.candidate_voltage_mv)
            candidate_voltage_mv = _next_search_candidate_voltage_mv(
                plan=flattened_plan,
                start_voltage_mv=int(start_voltage_mv),
                stable_voltage_mv=int(stable_voltage_mv),
                reference_actual_voltage_mv=reference_actual_voltage_mv,
                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
                min_search_voltage_mv=min_search_voltage_mv,
                failed_floor_voltage_mv=failed_candidate_floor_mv,
            )
            continue

        previous_stable_probe = stable_probe
        efficiency_delta = _temperature_normalized_efficiency_delta(
            previous_stable_probe,
            probe_summary,
        )
        power_up_efficiency_down = _is_power_up_efficiency_down_regression(
            previous_stable_probe,
            probe_summary,
            efficiency_delta,
        )
        stable_plan = candidate.plan
        stable_voltage_mv = int(candidate.candidate_voltage_mv)
        stable_lock_clock_mhz = int(candidate.target_clock_mhz)
        stable_probe = probe_summary
        if not probe_summary.used_companion_load:
            stable_history.append(probe_summary)
            _write_latest_verified_uv_result(
                plan=stable_plan,
                lock_clock_mhz=int(stable_lock_clock_mhz),
                voltage_mv=int(stable_voltage_mv),
                probe=stable_probe,
            )

        efficiency_improved = efficiency_delta.get("improved")
        measured_voltage_close_to_requested = bool(
            efficiency_delta.get("measured_voltage_close_to_requested")
        )
        voltage_drop_from_start_pct = (
            (
                (float(start_voltage_mv) - float(candidate.candidate_voltage_mv))
                / float(start_voltage_mv)
            )
            * 100.0
            if int(start_voltage_mv) > 0
            else 0.0
        )
        efficiency_stop_allowed = float(voltage_drop_from_start_pct) >= float(
            min_efficiency_stop_voltage_drop_pct
        )
        efficiency_stop_candidate = (
            efficiency_stop_streak > 0
            and (efficiency_improved is False or power_up_efficiency_down)
            and measured_voltage_close_to_requested
        )
        if efficiency_stop_candidate:
            non_improving_efficiency_streak += 1
        elif efficiency_improved is True:
            non_improving_efficiency_streak = 0
            pending_efficiency_stop_curve = None
        else:
            non_improving_efficiency_streak = 0

        if (
            efficiency_stop_candidate
            and efficiency_stop_allowed
            and pending_efficiency_stop_curve is not None
            and non_improving_efficiency_streak > efficiency_stop_streak
        ):
            efficiency_confirmations = max(0, non_improving_efficiency_streak - 1)
            delta_pct = efficiency_delta.get("delta_pct")
            use_current_curve = (
                not power_up_efficiency_down
                and delta_pct is not None
                and float(delta_pct) >= 0.0
            )
            if use_current_curve:
                stop_decision = "ACCEPTED, THEN STOPPED"
                stop_reason = (
                    "This candidate passed and still slightly improved "
                    "temperature-normalized FPS per watt, but the gain is now "
                    "below the scan threshold. PenguinBurner is stopping here "
                    "and using this lower-voltage curve."
                )
                stop_previous_probe = previous_stable_probe
            else:
                stable_plan = pending_efficiency_stop_curve["plan"]
                stable_voltage_mv = int(pending_efficiency_stop_curve["voltage_mv"])
                stable_lock_clock_mhz = int(
                    pending_efficiency_stop_curve["lock_clock_mhz"]
                )
                stable_probe = pending_efficiency_stop_curve["probe"]
                stop_decision = "ACCEPTED, THEN STOPPED AT PREVIOUS STEP"
                stop_reason = (
                    "This candidate passed, but temperature-normalized FPS per watt "
                    "failed to improve for a second effective voltage drop. "
                    "PenguinBurner is using the previous stable curve."
                )
                stop_previous_probe = pending_efficiency_stop_curve["probe"]
            _log_phase(
                log,
                "final",
                "temperature-normalized fps_per_w stopped improving "
                f"confirmations={efficiency_confirmations}/{efficiency_stop_streak} "
                f"candidate={candidate.candidate_voltage_mv}mV "
                f"using-current={str(use_current_curve).lower()} "
                f"final={stable_voltage_mv}mV@{stable_lock_clock_mhz}MHz "
                f"delta={efficiency_delta['delta_fps_per_w'] if efficiency_delta['delta_fps_per_w'] is not None else 'n/a'} "
                f"delta_pct={efficiency_delta['delta_pct'] if efficiency_delta['delta_pct'] is not None else 'n/a'} "
                f"requested-voltage-drop={efficiency_delta['requested_voltage_drop_mv'] if efficiency_delta['requested_voltage_drop_mv'] is not None else 'n/a'}mV "
                f"measured-voltage-drop={efficiency_delta['measured_voltage_drop_mv'] if efficiency_delta['measured_voltage_drop_mv'] is not None else 'n/a'}mV "
                f"temp-normalized-at={efficiency_delta['reference_temperature_c'] if efficiency_delta['reference_temperature_c'] is not None else 'n/a'}C",
            )
            _log_user_candidate_result(
                log,
                attempt=candidate_attempt_count,
                decision=stop_decision,
                reason=stop_reason,
                initial_probe=discovery_summary,
                previous_probe=stop_previous_probe,
                candidate_probe=probe_summary,
                restored_voltage_mv=int(stable_voltage_mv),
                restored_lock_clock_mhz=int(stable_lock_clock_mhz),
            )
            break

        if efficiency_improved is True:
            accepted_reason = (
                "This candidate passed stability, clock guardrails, and "
                "temperature-normalized FPS per watt still improved. "
                "PenguinBurner will try the next lower voltage."
            )
        elif efficiency_stop_candidate:
            pending_efficiency_stop_curve = {
                "plan": stable_plan,
                "voltage_mv": int(stable_voltage_mv),
                "lock_clock_mhz": int(stable_lock_clock_mhz),
                "probe": stable_probe,
            }
            stop_floor_text = f"{float(min_efficiency_stop_voltage_drop_pct):.1f}%"
            if power_up_efficiency_down:
                accepted_reason = (
                    "This candidate passed stability and clock guardrails, but "
                    "measured voltage went lower while temperature-normalized power rose "
                    "and FPS per watt fell. PenguinBurner will still probe "
                    f"{efficiency_stop_streak} more lower-voltage step(s) to confirm; "
                    "if those also fail to improve, this curve will be used as final."
                )
            else:
                accepted_reason = (
                    "This candidate passed stability and clock guardrails, but "
                    "temperature-normalized FPS per watt did not improve enough. "
                    f"PenguinBurner will probe {efficiency_stop_streak} more lower-voltage "
                    "step(s) to confirm; if those also fail to improve, this curve will be used as final."
                )
            if not efficiency_stop_allowed:
                accepted_reason += (
                    f" Early FPS/W stopping is disabled until the scan reaches at least "
                    f"{stop_floor_text} below the starting voltage; this step is only "
                    f"{float(voltage_drop_from_start_pct):.1f}% below start."
                )
        elif efficiency_improved is False and not measured_voltage_close_to_requested:
            requested_drop = efficiency_delta.get("requested_voltage_drop_mv")
            measured_drop = efficiency_delta.get("measured_voltage_drop_mv")
            requested_drop_text = (
                f"{float(requested_drop):.1f}mV"
                if requested_drop is not None
                else "not available"
            )
            measured_drop_text = (
                f"{float(measured_drop):.1f}mV"
                if measured_drop is not None
                else "not available"
            )
            accepted_reason = (
                "This candidate passed stability and clock guardrails. "
                "The requested voltage dropped, but measured loaded voltage did not "
                f"follow it closely enough (requested {requested_drop_text}, measured {measured_drop_text}), so the FPS/W stop "
                "rule is ignored for this step and PenguinBurner will try the next lower voltage."
            )
        else:
            accepted_reason = (
                "This candidate passed stability and clock guardrails. "
                "PenguinBurner could not compute temperature-normalized FPS per watt "
                "for this step, so it will try the next lower voltage."
            )
        _log_phase(
            log,
            "accept",
            f"shape={candidate.label} "
            + _format_probe_summary(probe_summary)
            + " "
            + _describe_guardrails(
                stable_history,
                min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            ),
        )
        _log_user_candidate_result(
            log,
            attempt=candidate_attempt_count,
            decision="ACCEPTED",
            reason=accepted_reason,
            initial_probe=discovery_summary,
            previous_probe=previous_stable_probe,
            candidate_probe=probe_summary,
            restored_voltage_mv=int(stable_voltage_mv),
            restored_lock_clock_mhz=int(stable_lock_clock_mhz),
        )
        candidate_voltage_mv = _next_search_candidate_voltage_mv(
            plan=flattened_plan,
            start_voltage_mv=int(start_voltage_mv),
            stable_voltage_mv=int(stable_voltage_mv),
            reference_actual_voltage_mv=_latest_reference_voltage_mv(
                stable_history,
                reference_actual_voltage_mv,
            ),
            preserve_vanilla_below_mv=preserve_vanilla_below_mv,
            min_search_voltage_mv=min_search_voltage_mv,
            failed_floor_voltage_mv=failed_candidate_floor_mv,
        )
        continue

    return {
        "stable_plan": stable_plan,
        "stable_voltage_mv": stable_voltage_mv,
        "stable_lock_clock_mhz": stable_lock_clock_mhz,
        "stable_probe": stable_probe,
    }


def _run_final_verification_and_save(
    *,
    log,
    reader,
    stable_plan,
    stable_voltage_mv,
    stable_lock_clock_mhz,
    stable_probe,
    stable_history,
    probe_history,
    q2rtx_config,
    final_verification_duration_s,
    source_result,
    start_voltage_mv,
    measured_clock_mhz,
    nvml_session,
    clock_ceiling,
    discovery_summary,
    translated_gpu_policy,
    min_performance_core_clock_pct,
    runtime_default_plan,
    final_clock_drop_margin_pct,
):
    _log_phase(
        log,
        "final",
        f"last-stable={stable_voltage_mv}mV@{stable_lock_clock_mhz}MHz",
    )
    last_stable_path = _write_uv_result_snapshot(
        plan=stable_plan,
        lock_clock_mhz=int(stable_lock_clock_mhz),
        voltage_mv=int(stable_voltage_mv),
        probe=stable_probe,
        reason="last-stable",
    )
    _log_phase(log, "final", f"last-stable-saved={last_stable_path}")
    final_voltage_mv = stable_voltage_mv
    final_lock_clock_mhz = stable_lock_clock_mhz
    final_plan = stable_plan
    final_stable_path = _write_stable_uv_result(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=stable_probe,
        label="final",
    )
    _log_phase(log, "final", f"stable-config-saved={final_stable_path}")
    final_saved_path = _write_saved_uv_state(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=stable_probe,
        label="best-undervolt",
    )
    _log_phase(log, "final", f"saved={final_saved_path}")
    if final_plan is not None:
        apply_plan(reader, final_plan)
        reader.refresh_points()
    final_q2rtx_duration_s, final_cuda_duration_s = _budget_final_probe_durations(
        int(final_verification_duration_s)
    )
    final_verify_config = _normalize_probe_config(
        replace(
            q2rtx_config,
            timedemo_loops=None,
            duration_s=int(final_q2rtx_duration_s),
            companion_command=_cuda_bruteforce_companion_command(
                gpu_index=int(q2rtx_config.gpu_index),
                duration_s=int(final_cuda_duration_s),
            ),
            single_pass_timeout_s=max(
                float(q2rtx_config.single_pass_timeout_s),
                float(final_verification_duration_s)
                + AUTO_UV_PROBE_TUNING.long_timeout_buffer_s,
            ),
        )
    )
    final_probe = None
    while (
        final_plan is not None
        and final_voltage_mv is not None
        and final_lock_clock_mhz is not None
    ):
        _log_phase(
            log,
            "final-verify",
            f"starting total-duration={int(final_verification_duration_s)}s "
            f"q2rtx-duration={int(final_q2rtx_duration_s)}s "
            f"cuda-duration={int(final_cuda_duration_s)}s "
            f"target={final_lock_clock_mhz}MHz voltage={final_voltage_mv}mV",
        )
        _log_user_stage(
            log,
            "Final long verification",
            [
                f"Candidate chosen for final check: {int(final_lock_clock_mhz)}MHz at {int(final_voltage_mv)}mV.",
                f"Running about {int(final_q2rtx_duration_s)}s of Q2RTX plus {int(final_cuda_duration_s)}s of CUDA load.",
                "If this fails, PenguinBurner will try to recover to the nearest safer stable curve.",
            ],
        )
        if clock_ceiling is not None:
            clock_ceiling.retarget(
                lock_clock_mhz=int(final_lock_clock_mhz),
                lock_voltage_mv=int(final_voltage_mv),
            )
            _log_phase(log, "ceiling", clock_ceiling.describe())
        final_probe, final_result = _probe_voltage_candidate(
            reader=reader,
            candidate_plan=final_plan,
            candidate_voltage_mv=int(final_voltage_mv),
            lock_clock_mhz=int(final_lock_clock_mhz),
            q2rtx_config=final_verify_config,
            stable_history=stable_history,
            initial_probe_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label="final-verify",
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            reset_plan=runtime_default_plan,
        )
        probe_history.append(final_probe)
        _log_benchmark(
            log,
            phase="final-verify",
            probe=final_probe,
            reference_probe=discovery_summary,
            reference_label="initial",
        )
        if final_result.success:
            final_error = _evaluate_probe(
                final_probe,
                stable_history=stable_history,
                min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            )
            if not final_error:
                _write_latest_verified_uv_result(
                    plan=final_plan,
                    lock_clock_mhz=int(final_lock_clock_mhz),
                    voltage_mv=int(final_voltage_mv),
                    probe=final_probe,
                )
                break
            _log_phase(log, "final-verify", f"rejected {final_error}")
        recovery_candidate, recovery_summary, recovery_result = (
            _probe_stabilization_search(
                reader=reader,
                plan_source=source_result["plan"],
                failure_voltage_mv=int(final_voltage_mv),
                failure_live_voltage_mv=final_probe.live_voltage_after_mv,
                minimum_candidate_voltage_mv=_next_higher_voltage_bin(
                    source_result["plan"], int(final_voltage_mv)
                ),
                target_clock_mhz=int(final_lock_clock_mhz),
                q2rtx_config=q2rtx_config,
                stable_history=stable_history,
                nvml_session=nvml_session,
                clock_ceiling=clock_ceiling,
                log=log,
                probe_history=probe_history,
                baseline_probe=discovery_summary,
                initial_target_voltage_mv=int(start_voltage_mv),
                initial_probe_clock_mhz=measured_clock_mhz,
                power_limit_w=translated_gpu_policy.get("power_limit_w"),
                min_performance_core_clock_pct=float(min_performance_core_clock_pct),
                reset_plan=runtime_default_plan,
            )
        )
        if (
            recovery_candidate is None
            or recovery_summary is None
            or recovery_result is None
        ):
            raise AutoUvError(
                "final long verification failed and stabilization recovery could not find a stable point"
            )
        final_plan = recovery_candidate.plan
        final_voltage_mv = int(recovery_candidate.candidate_voltage_mv)
        final_lock_clock_mhz = int(recovery_candidate.target_clock_mhz)
        stable_plan = final_plan
        stable_voltage_mv = final_voltage_mv
        stable_lock_clock_mhz = final_lock_clock_mhz
        stable_probe = recovery_summary
        if not recovery_summary.used_companion_load:
            stable_history.append(recovery_summary)
            _write_latest_verified_uv_result(
                plan=stable_plan,
                lock_clock_mhz=int(stable_lock_clock_mhz),
                voltage_mv=int(stable_voltage_mv),
                probe=stable_probe,
            )
    if final_plan is None or final_voltage_mv is None or final_lock_clock_mhz is None:
        raise AutoUvError("final verification did not produce a final curve")
    snapshot_path = _write_final_curve_snapshot(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=final_probe,
    )
    fan_tuning_result = write_auto_uv_fan_payload(
        final_probe=final_probe,
        probes=probe_history,
    )
    if fan_tuning_result is not None:
        if fan_tuning_result.blocked:
            loaded_temp = fan_tuning_result.payload.get("loaded_temperature_c")
            limit_temp = fan_tuning_result.payload.get(
                "max_stock_curve_load_temperature_c"
            )
            loaded_text = _format_user_value(loaded_temp, "C")
            limit_text = _format_user_value(limit_temp, "C")
            reason = fan_tuning_result.block_reason or "unknown"
            _log_phase(
                log,
                "fan-tune",
                f"curve-blocked reason={reason} loaded-temp={loaded_text} "
                f"limit={limit_text} marker={fan_tuning_result.path}",
            )
            _log_user_stage(
                log,
                "Silent fan curve skipped",
                [
                    f"Final long-run load temperature was {loaded_text}.",
                    f"The safety limit for generating a quieter fan curve is {limit_text}.",
                    "PenguinBurner will not generate a silent fan curve because reducing fan speed here could overheat the GPU.",
                ],
            )
        else:
            _log_phase(
                log,
                "fan-tune",
                f"curve-saved={fan_tuning_result.path} "
                f"points={len(fan_tuning_result.curve)} "
                f"cooling-headroom={_format_user_value(fan_tuning_result.payload.get('cooling_headroom_c'), 'C')} "
                f"speed-reduction={_format_user_value(fan_tuning_result.payload.get('cooling_headroom_speed_reduction_pct'), '%')} "
                f"exponent={_format_user_value(fan_tuning_result.payload.get('effective_exponential_power'), '', precision=2)}",
            )
            _log_user_stage(
                log,
                "Silent fan curve",
                [
                    f"Saved suggested curve with {len(fan_tuning_result.curve)} points.",
                    (
                        "Cooling headroom to "
                        f"{_format_user_value(fan_tuning_result.payload.get('max_stock_curve_load_temperature_c'), 'C')} "
                        f"safety target: {_format_user_value(fan_tuning_result.payload.get('cooling_headroom_c'), 'C')}."
                    ),
                    "This curve is not applied by default; normal runtime uses it only with --silent-fan-curve.",
                ],
            )
            _log_fan_curve_ascii_chart(
                log,
                curve=fan_tuning_result.curve,
                loaded_temperature_c=fan_tuning_result.payload.get(
                    "load_anchor_temperature_c"
                ),
                load_anchor_fan_speed_pct=fan_tuning_result.payload.get(
                    "load_anchor_fan_speed_pct"
                ),
            )
    _log_user_stage(
        log,
        "Final voltage/frequency curve",
        [
            "The chart below shows the curve that will be saved.",
            "'#' is the target curve, '.' is the stock/base curve, and '@' marks the final lock point.",
        ],
    )
    _log_vf_ascii_chart(
        log,
        plan=final_plan,
        target_clock_mhz=int(final_lock_clock_mhz),
        candidate_voltage_mv=int(final_voltage_mv),
    )
    _log_vf_point_list(
        log,
        plan=final_plan,
        label=f"final target={int(final_lock_clock_mhz)}MHz voltage={int(final_voltage_mv)}mV",
    )
    _log_final_summary(
        log,
        baseline_probe=stable_history[0] if stable_history else None,
        final_probe=final_probe,
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
        clock_drop_margin_pct=float(final_clock_drop_margin_pct),
    )
    _log_user_readable_final_summary(
        log,
        baseline_probe=stable_history[0] if stable_history else None,
        final_probe=final_probe,
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
        clock_drop_margin_pct=float(final_clock_drop_margin_pct),
        curve_path=snapshot_path,
    )
    _log_phase(log, "final", f"curve-saved={snapshot_path}")
    return _build_voltage_scan_result(
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
        probe_history=probe_history,
        stable_history=stable_history,
        final_probe=final_probe,
    )


def _run_auto_uv_voltage_scan_impl(
    *,
    gpu_index,
    runtime_options,
    q2rtx_config,
    log=print,
):
    settings = _scan_settings(runtime_options, q2rtx_config)
    q2rtx_config = settings.q2rtx_config
    final_clock_drop_margin_pct = settings.final_clock_drop_margin_pct
    min_performance_core_clock_pct = settings.min_performance_core_clock_pct
    preserve_vanilla_below_mv = settings.preserve_vanilla_below_mv
    configured_max_drop_pct = settings.configured_max_drop_pct
    final_verification_duration_s = settings.final_verification_duration_s
    efficiency_stop_streak = settings.efficiency_stop_streak
    min_efficiency_stop_voltage_drop_pct = settings.min_efficiency_stop_voltage_drop_pct
    interrupted_probe = _consume_interrupted_uv_probe_marker()
    unsafe_voltage_entries = _load_uv_unsafe_voltage_entries()
    if interrupted_probe is not None:
        blacklist_path, unsafe_entry = interrupted_probe
        _log_phase(
            log,
            "crash-recovery",
            "previous auto-UV probe did not finish; "
            f"blacklisted={int(unsafe_entry['candidate_voltage_mv'])}mV "
            f"target={int(unsafe_entry['lock_clock_mhz'])}MHz "
            f"phase={unsafe_entry.get('phase') or 'unknown'} "
            f"blacklist={blacklist_path}",
        )
        _log_user_stage(
            log,
            "Previous Auto-UV run did not finish",
            [
                (
                    "PenguinBurner found an unfinished probe marker from the previous run. "
                    "That usually means the system rebooted, crashed, or lost power during a voltage test."
                ),
                (
                    f"Voltage {int(unsafe_entry['candidate_voltage_mv'])}mV is now marked unsafe "
                    "and this run will not test it again."
                ),
                "The next search will stop at the next higher voltage bin instead.",
            ],
        )
        unsafe_voltage_entries = _load_uv_unsafe_voltage_entries()

    reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    if reader is None:
        last_error = get_hidden_vf_curve_reader_last_error()
        detail = f": {last_error}" if last_error is not None else ""
        raise AutoUvError(
            "failed to create Linux NVAPI VF helper"
            f"{detail}. This driver/GPU combination may not expose editable voltage-based V/F points."
        )
    vf_summary = reader.summary()
    _log_phase(
        log,
        "source",
        "linux-nvapi-vf "
        f"active-points={vf_summary['active_points']} "
        f"editable-core-points={vf_summary['editable_core_points']}",
    )

    policy_controller = None
    nvml_session = None
    clock_ceiling = None
    stable_plan = None
    stable_voltage_mv = None
    stable_lock_clock_mhz = None
    stable_probe = None
    stable_history: list[AutoUvProbeSummary] = []
    probe_history: list[AutoUvProbeSummary] = []
    previous_sigterm_handler = None
    runtime_default_plan = None

    try:

        def _interrupt_scan(_signum, _frame):
            raise KeyboardInterrupt()

        previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _interrupt_scan)
        try:
            policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
        except Exception as exc:
            raise AutoUvError(
                "GPU policy helper unavailable; auto-UV voltage scan needs locked "
                f"graphics clocks and power policy control: {exc}"
            ) from exc

        nvml_session = _NvmlDeviceSession(gpu_index=gpu_index)
        if nvml_session.voltage_reader_available():
            _log_phase(log, "telemetry", "hidden-voltage-reader=enabled")
        else:
            _log_phase(
                log,
                "telemetry",
                "hidden-voltage-reader=unavailable; measured voltage will be n/a",
            )
        runtime_reset = reset_nvidia_runtime_defaults(
            gpu_index=gpu_index,
            power_limit_override_w=runtime_options.get("power_limit_override_w"),
            log=log,
        )
        reader.refresh_points()
        source_result = {
            "plan": build_runtime_default_plan(reader),
            "translation_mode": "runtime-defaults",
            "changed_points": [],
        }
        runtime_default_plan = source_result["plan"]
        _validate_auto_uv_source_plan(source_result["plan"])
        apply_plan(reader, source_result["plan"])
        reader.refresh_points()
        translated_gpu_policy = {
            "power_limit_w": runtime_reset.get("power_limit_w"),
        }
        _log_phase(log, "source", "baseline=runtime-defaults")
        _log_phase(
            log,
            "source",
            f"plan-mode={source_result['translation_mode']} "
            + (
                f"loops={int(q2rtx_config.timedemo_loops)} "
                if q2rtx_config.timedemo_loops is not None
                else f"duration={int(q2rtx_config.duration_s)}s "
            )
            + f"min-performance-core-clock="
            f"{float(min_performance_core_clock_pct):.1f}% "
            f"max-clock-drop={final_clock_drop_margin_pct:.1f}%",
        )
        _log_user_stage(
            log,
            "Starting voltage scan",
            [
                "Goal: find the lowest stable voltage that keeps performance inside the configured safety margin.",
                "Performance guardrail: keep core clock at or above "
                f"{float(min_performance_core_clock_pct):.1f}% "
                f"of baseline, allowing at most {float(final_clock_drop_margin_pct):.1f}% drop.",
                f"Maximum voltage search drop: {float(configured_max_drop_pct):.1f}% below the discovered starting voltage.",
                "Each step applies one candidate curve, runs Q2RTX/CUDA, then either accepts it or restores the previous stable curve.",
            ],
        )
        if runtime_reset.get("power_limit_w") is not None:
            _log_phase(
                log,
                "source",
                f"power-limit={int(runtime_reset['power_limit_w'])}W source=runtime-defaults",
            )
        if preserve_vanilla_below_mv is not None:
            _log_phase(
                log,
                "source",
                f"base curve stays at and below {preserve_vanilla_below_mv}mV",
            )

        initial_lock_clock_mhz = max(
            int(item["target_mhz"])
            for item in source_result["plan"]
            if not bool(item.get("preserve_vanilla"))
        )
        initial_lock_voltage_mv = _nearest_voltage_bin(
            source_result["plan"],
            _find_lock_voltage_for_clock(
                source_result["plan"],
                initial_lock_clock_mhz,
            ),
        )
        initial_flattened_plan = _build_descended_plan(
            source_result["plan"],
            lock_clock_mhz=initial_lock_clock_mhz,
            candidate_voltage_mv=initial_lock_voltage_mv,
        )
        apply_plan(reader, initial_flattened_plan)
        reader.refresh_points()

        discovery_probe_config = _short_probe_config(
            q2rtx_config,
            target_duration_s=AUTO_UV_DEFAULTS.probe_duration_s,
        )
        _log_user_stage(
            log,
            "Stage 1 - measuring the baseline",
            [
                "PenguinBurner is applying the default curve and running a short probe.",
                f"This measures the real sustained clock, voltage, power, temperature, and fan speed for about {AUTO_UV_DEFAULTS.probe_duration_s}s before undervolting.",
                "The first warm-up seconds are ignored for decision averages so Q2RTX ramp-up does not skew the baseline.",
            ],
        )
        discovery_summary, discovery_result = _probe_voltage_candidate(
            reader=reader,
            candidate_plan=initial_flattened_plan,
            candidate_voltage_mv=initial_lock_voltage_mv,
            lock_clock_mhz=initial_lock_clock_mhz,
            q2rtx_config=discovery_probe_config,
            stable_history=[],
            initial_probe_clock_mhz=None,
            nvml_session=nvml_session,
            log=log,
            phase_label="discover",
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            enforce_target_core_clock_floor=False,
            show_candidate_target=False,
            summarize_saturated_tail=True,
            use_power_limit_floor=True,
            reset_plan=runtime_default_plan,
        )
        probe_history.append(discovery_summary)
        _log_benchmark(log, phase="discover", probe=discovery_summary)
        if not discovery_result.success:
            raise AutoUvError(
                "stock Defaults baseline failed the Q2RTX probe: "
                f"{discovery_result.reason}"
            )

        discovery_tail_samples = _saturated_tail_samples(
            discovery_result.telemetry_samples
        )
        fallback_clock_mhz = discovery_summary.avg_core_clock_mhz
        saturated_clock_mhz, saturated_sample_count, saturation_floor_w = (
            _derive_power_saturated_clock_mhz(
                discovery_tail_samples,
                power_limit_w=translated_gpu_policy.get("power_limit_w"),
            )
        )
        (
            active_avg_clock_mhz,
            active_preferred_clock_mhz,
            active_sample_count,
            active_power_floor_w,
        ) = _derive_active_core_clock_mhz(
            discovery_tail_samples,
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            use_power_limit_floor=True,
        )
        (
            discovery_loaded_floor_mv,
            discovery_loaded_avg_mv,
            discovery_loaded_ceiling_mv,
            discovery_loaded_samples,
        ) = _derive_loaded_voltage_band_mv(
            discovery_tail_samples,
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            use_power_limit_floor=True,
        )
        measured_clock_mhz = (
            saturated_clock_mhz
            if saturated_clock_mhz is not None
            else (
                active_preferred_clock_mhz
                if active_preferred_clock_mhz is not None
                else fallback_clock_mhz
            )
        )
        if measured_clock_mhz is None:
            raise AutoUvError(
                "stock Defaults baseline did not report an average core clock"
            )
        preferred_lock_clock_mhz = _choose_sustained_clock_target(
            source_result["plan"],
            measured_clock_mhz,
        )
        lock_clock_mhz = int(preferred_lock_clock_mhz)
        stock_lock_voltage_mv = _nearest_voltage_bin(
            source_result["plan"],
            _find_lock_voltage_for_clock(source_result["plan"], lock_clock_mhz),
        )
        baseline_reference_voltage_mv = _nearest_voltage_bin(
            source_result["plan"],
            int(discovery_loaded_avg_mv)
            if discovery_loaded_avg_mv is not None
            else (
                int(round(discovery_summary.avg_voltage_mv))
                if discovery_summary.avg_voltage_mv is not None
                else int(stock_lock_voltage_mv)
            ),
        )
        start_voltage_mv = int(baseline_reference_voltage_mv)
        flatten_target = _build_flatten_target(
            source_result["plan"],
            lock_clock_mhz=lock_clock_mhz,
            lock_voltage_mv=start_voltage_mv,
        )
        flattened_plan = _build_descended_plan(
            source_result["plan"],
            lock_clock_mhz=lock_clock_mhz,
            candidate_voltage_mv=start_voltage_mv,
        )
        apply_plan(reader, flattened_plan)
        reader.refresh_points()

        clock_ceiling = _ProbeClockCeilingController(
            flatten_target=flatten_target,
            policy_controller=policy_controller,
        )
        clock_ceiling.apply()
        power_limit_w = translated_gpu_policy.get("power_limit_w")
        _log_phase(
            log,
            "discover",
            (
                f"measured-core_clock={measured_clock_mhz:.1f}MHz "
                if saturated_clock_mhz is None
                else f"saturated-core_clock={measured_clock_mhz:.1f}MHz "
            )
            + (
                f"active-core_clock={active_avg_clock_mhz:.1f}MHz "
                f"active-preferred={active_preferred_clock_mhz:.1f}MHz "
                f"active-floor={active_power_floor_w:.1f}W "
                f"active-samples={active_sample_count} "
                if active_avg_clock_mhz is not None
                and active_preferred_clock_mhz is not None
                and active_power_floor_w is not None
                else ""
            )
            + (
                f"power-limit={int(power_limit_w)}W "
                f"sat-floor={saturation_floor_w:.1f}W "
                f"sat-samples={saturated_sample_count} "
                if power_limit_w is not None and saturation_floor_w is not None
                else ""
            )
            + (
                f"loaded-band={discovery_loaded_floor_mv}-{discovery_loaded_ceiling_mv if discovery_loaded_ceiling_mv is not None else discovery_loaded_floor_mv}mV "
                f"loaded-avg={discovery_loaded_avg_mv if discovery_loaded_avg_mv is not None else discovery_loaded_floor_mv}mV "
                f"loaded-samples={discovery_loaded_samples} "
                if discovery_loaded_floor_mv is not None
                else ""
            )
            + f"selected-target={lock_clock_mhz}MHz "
            f"stock-anchor={stock_lock_voltage_mv}mV "
            f"baseline-voltage={baseline_reference_voltage_mv}mV "
            f"start-voltage={start_voltage_mv}mV "
            f"flatten-target={describe_afterburner_dynamic_lock(flatten_target)}",
        )
        min_search_voltage_mv = max(
            0,
            int(
                round(
                    float(start_voltage_mv)
                    * (1.0 - (float(configured_max_drop_pct) / 100.0))
                )
            ),
        )
        unsafe_floor_mv, unsafe_next_higher_mv = _unsafe_min_search_voltage_mv(
            plan=flattened_plan,
            start_voltage_mv=int(start_voltage_mv),
            unsafe_entries=unsafe_voltage_entries,
        )
        if unsafe_floor_mv is not None:
            if unsafe_next_higher_mv is None:
                min_search_voltage_mv = int(start_voltage_mv)
                next_text = "none"
            else:
                min_search_voltage_mv = max(
                    int(min_search_voltage_mv),
                    int(unsafe_next_higher_mv),
                )
                next_text = f"{int(unsafe_next_higher_mv)}mV"
            _log_phase(
                log,
                "blacklist",
                f"unsafe-floor={int(unsafe_floor_mv)}mV "
                f"next-higher-bin={next_text} "
                f"effective-min-search-voltage={int(min_search_voltage_mv)}mV",
            )
        _log_phase(
            log,
            "discover",
            f"max-drop={float(configured_max_drop_pct):.1f}% "
            f"min-search-voltage={int(min_search_voltage_mv)}mV "
            f"from-start-voltage={int(start_voltage_mv)}mV",
        )
        _log_phase(log, "baseline", clock_ceiling.describe())
        _log_vf_ascii_chart(
            log,
            plan=flattened_plan,
            target_clock_mhz=lock_clock_mhz,
            candidate_voltage_mv=start_voltage_mv,
        )
        _log_vf_point_list(
            log,
            plan=flattened_plan,
            label=f"baseline target={lock_clock_mhz}MHz voltage={start_voltage_mv}mV",
        )
        baseline_summary = discovery_summary
        baseline_result = discovery_result
        _log_phase(
            log,
            "baseline",
            "reused initial warmed-up discovery probe and started descent without a redundant baseline rerun",
        )
        if not baseline_result.success:
            (
                baseline_recovery_candidate,
                baseline_recovery_summary,
                baseline_recovery_result,
            ) = _probe_stabilization_search(
                reader=reader,
                plan_source=source_result["plan"],
                failure_voltage_mv=int(start_voltage_mv),
                failure_live_voltage_mv=baseline_summary.live_voltage_after_mv,
                minimum_candidate_voltage_mv=_next_higher_voltage_bin(
                    source_result["plan"], int(start_voltage_mv)
                ),
                target_clock_mhz=int(lock_clock_mhz),
                q2rtx_config=q2rtx_config,
                stable_history=[],
                initial_probe_clock_mhz=measured_clock_mhz,
                nvml_session=nvml_session,
                clock_ceiling=clock_ceiling,
                log=log,
                probe_history=probe_history,
                baseline_probe=discovery_summary,
                initial_target_voltage_mv=int(start_voltage_mv),
                power_limit_w=translated_gpu_policy.get("power_limit_w"),
                min_performance_core_clock_pct=float(min_performance_core_clock_pct),
                reset_plan=runtime_default_plan,
            )
            if (
                baseline_recovery_candidate is None
                or baseline_recovery_summary is None
                or baseline_recovery_result is None
            ):
                raise AutoUvError(
                    "baseline flattened curve failed the Q2RTX probe: "
                    f"{baseline_result.reason} at {start_voltage_mv}mV"
                )
            baseline_summary = baseline_recovery_summary
            baseline_result = baseline_recovery_result
            flattened_plan = baseline_recovery_candidate.plan
            start_voltage_mv = int(baseline_recovery_candidate.candidate_voltage_mv)
            flatten_target["lock_voltage_mv"] = int(start_voltage_mv)
        stable_plan = flattened_plan
        stable_voltage_mv = int(start_voltage_mv)
        stable_lock_clock_mhz = int(lock_clock_mhz)
        stable_probe = baseline_summary
        stable_history.append(baseline_summary)
        _log_phase(
            log,
            "baseline",
            "accepted "
            + _format_probe_summary(baseline_summary)
            + " "
            + _describe_guardrails(
                stable_history,
                min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            ),
        )
        _log_user_stage(
            log,
            "Baseline accepted",
            [
                f"Starting point: {int(stable_lock_clock_mhz)}MHz at {int(stable_voltage_mv)}mV.",
                f"Measured power: {_format_user_value(baseline_summary.avg_power_w, 'W')}.",
                f"Measured temperature: {_format_user_value(baseline_summary.avg_temperature_c, 'C')}.",
                f"Measured fan speed: {_format_user_value(baseline_summary.avg_fan_speed_pct, '%')}.",
                "Next, PenguinBurner will walk downward through real voltage bins.",
            ],
        )
        _write_latest_verified_uv_result(
            plan=stable_plan,
            lock_clock_mhz=int(stable_lock_clock_mhz),
            voltage_mv=int(stable_voltage_mv),
            probe=stable_probe,
        )

        first_candidate_voltage_mv = _next_search_candidate_voltage_mv(
            plan=flattened_plan,
            start_voltage_mv=int(start_voltage_mv),
            stable_voltage_mv=int(start_voltage_mv),
            reference_actual_voltage_mv=baseline_summary.avg_voltage_mv,
            preserve_vanilla_below_mv=preserve_vanilla_below_mv,
            min_search_voltage_mv=min_search_voltage_mv,
        )
        if first_candidate_voltage_mv is None:
            log(
                "Auto-UV scan: no lower voltage bins are available below the current lock point"
            )
            return AutoUvVoltageScanResult(
                success=True,
                final_voltage_mv=stable_voltage_mv,
                lock_clock_mhz=stable_lock_clock_mhz,
                stop_reason="no-lower-voltage-bins",
                failed_candidate_voltage_mv=None,
                probes=probe_history,
            )

        sweep_result = _run_candidate_sweep(
            log=log,
            reader=reader,
            flattened_plan=flattened_plan,
            start_voltage_mv=start_voltage_mv,
            stable_plan=stable_plan,
            stable_voltage_mv=stable_voltage_mv,
            stable_lock_clock_mhz=stable_lock_clock_mhz,
            stable_probe=stable_probe,
            stable_history=stable_history,
            probe_history=probe_history,
            first_candidate_voltage_mv=first_candidate_voltage_mv,
            discovery_summary=discovery_summary,
            lock_clock_mhz=lock_clock_mhz,
            q2rtx_config=q2rtx_config,
            measured_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            translated_gpu_policy=translated_gpu_policy,
            runtime_default_plan=runtime_default_plan,
            clock_ceiling=clock_ceiling,
            source_result=source_result,
            min_performance_core_clock_pct=min_performance_core_clock_pct,
            min_search_voltage_mv=min_search_voltage_mv,
            preserve_vanilla_below_mv=preserve_vanilla_below_mv,
            min_efficiency_stop_voltage_drop_pct=min_efficiency_stop_voltage_drop_pct,
            efficiency_stop_streak=efficiency_stop_streak,
        )
        stable_plan = sweep_result["stable_plan"]
        stable_voltage_mv = sweep_result["stable_voltage_mv"]
        stable_lock_clock_mhz = sweep_result["stable_lock_clock_mhz"]
        stable_probe = sweep_result["stable_probe"]

        return _run_final_verification_and_save(
            log=log,
            reader=reader,
            stable_plan=stable_plan,
            stable_voltage_mv=stable_voltage_mv,
            stable_lock_clock_mhz=stable_lock_clock_mhz,
            stable_probe=stable_probe,
            stable_history=stable_history,
            probe_history=probe_history,
            q2rtx_config=q2rtx_config,
            final_verification_duration_s=final_verification_duration_s,
            source_result=source_result,
            start_voltage_mv=start_voltage_mv,
            measured_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            clock_ceiling=clock_ceiling,
            discovery_summary=discovery_summary,
            translated_gpu_policy=translated_gpu_policy,
            min_performance_core_clock_pct=min_performance_core_clock_pct,
            runtime_default_plan=runtime_default_plan,
            final_clock_drop_margin_pct=final_clock_drop_margin_pct,
        )
    except StabilityTestError as exc:
        if stable_plan is not None:
            try:
                apply_plan(reader, stable_plan)
                reader.refresh_points()
            except Exception:
                pass
        raise AutoUvError(f"auto-UV stability probe failed: {exc}") from exc
    except KeyboardInterrupt:
        if (
            stable_plan is not None
            and stable_voltage_mv is not None
            and stable_lock_clock_mhz is not None
        ):
            try:
                apply_plan(reader, stable_plan)
                reader.refresh_points()
                interrupted_path = _write_uv_result_snapshot(
                    plan=stable_plan,
                    lock_clock_mhz=int(stable_lock_clock_mhz),
                    voltage_mv=int(stable_voltage_mv),
                    probe=stable_probe,
                    reason="interrupted",
                )
                _log_phase(
                    log,
                    "final",
                    f"interrupt-saved={interrupted_path} "
                    f"last-stable={stable_voltage_mv}mV@{stable_lock_clock_mhz}MHz",
                )
            except Exception as exc:
                log(f"Auto-UV interrupt save skipped: {exc}")
        raise
    except Exception:
        if stable_plan is not None:
            try:
                apply_plan(reader, stable_plan)
                reader.refresh_points()
            except Exception:
                pass
        raise
    finally:
        if clock_ceiling is not None:
            try:
                clock_ceiling.close()
            except Exception as exc:
                log(f"Auto-UV clock-ceiling reset skipped: {exc}")
        if runtime_default_plan is not None:
            try:
                apply_plan(reader, runtime_default_plan)
                reader.refresh_points()
                _log_phase(
                    log,
                    "cleanup",
                    "restored runtime-default V/F curve; saved Auto-UV curve will be applied by daemon/runtime",
                )
            except Exception as exc:
                log(f"Auto-UV default V/F restore skipped: {exc}")
        if nvml_session is not None:
            nvml_session.close()
        reader.close()
        if policy_controller is not None:
            policy_controller.close()
        if previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)


def run_auto_uv_voltage_scan(
    *,
    gpu_index: int,
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
    log: Callable[[str], None] = print,
) -> AutoUvVoltageScanResult:
    result = _run_auto_uv_voltage_scan_impl(
        gpu_index=gpu_index,
        runtime_options=runtime_options,
        q2rtx_config=q2rtx_config,
        log=log,
    )
    if not isinstance(result, AutoUvVoltageScanResult):
        raise AutoUvError("auto-UV scanner returned an unexpected result")
    return result
