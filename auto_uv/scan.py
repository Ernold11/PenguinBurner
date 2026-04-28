#!/usr/bin/env python3

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
import signal
import time
from typing import Callable

from afterburner.vfcurve import describe_afterburner_dynamic_lock
from hidden_nvapi_vf import (
    create_hidden_vf_curve_reader,
    get_hidden_vf_curve_reader_last_error,
)
from hidden_nvml_voltage import create_hidden_voltage_reader
from afterburner.import_vf_curve import apply_plan
from nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ, NvmlGpuPolicyController
from nvidia_runtime_defaults import (
    reset_nvidia_runtime_defaults,
)
from stability.q2rtx import (
    Q2RTXStabilityConfig,
    StabilityTestError,
    cleanup_managed_q2rtx_processes,
)

from .constants import NVML_SUCCESS
from .models import (
    AutoUvCurveCandidate,
    AutoUvError,
    AutoUvFinalChoiceDiscarded,
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
)
from .artifacts import (
    _consume_interrupted_uv_probe_marker,
    _final_choice_request_path,
    _final_choice_response_path,
    _load_uv_unsafe_voltage_entries,
    _safe_json_write,
    _write_latest_verified_uv_result,
    _write_uv_result_snapshot,
)
from .artifact_paths import auto_uv_stop_requested
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
    _validate_auto_uv_source_plan,
)
from .final_verify import _run_final_verification_and_save
from .events import (
    AutoUvEventCallback,
    emit_event,
    overclock_budget_event_payload,
    plan_event_points,
    probe_event_payload,
)
from .probe_metrics import (
    _baseline_value,
    _derive_active_core_clock_mhz,
    _derive_loaded_voltage_band_mv,
    _derive_power_saturated_clock_mhz,
    _evaluate_probe,
    _latest_non_companion_probe,
    _saturated_tail_samples,
)
from .probe_runner import _probe_voltage_candidate
from .probe_config import (
    _normalize_probe_config,
    _short_probe_config,
    _stability_probe_config_for_voltage_band,
    _tiered_probe_duration_s,
)
from .performance import (
    annotate_performance_candidate_scores,
    performance_candidate_sort_key,
)
from .scan_rules import (
    _core_clock_below_floor,
    _final_failure_can_accept_budget_curve,
    _is_power_up_efficiency_down_regression,
    _percent,
    _probe_failure_should_mark_voltage_unsafe,
    _real_clock_adjusted_stable_curve,
    _real_probe_lock_clock_mhz,
    _target_core_clock_floor,
    _telemetry_sample_is_busy,
)
from .sweep_modes import AUTO_UV_MODE_PERFORMANCE, normalize_auto_uv_mode
from .tuning import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO,
    AUTO_UV_METRIC_TUNING,
    AUTO_UV_STALL_TUNING,
    AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO,
)
from .clock_bump import _clock_bump_budget_pct
from .user_output import (
    format_probe_summary as _format_probe_summary,
    format_user_duration as _format_user_duration,
    format_user_value as _format_user_value,
    log_benchmark as _log_benchmark,
    log_phase as _log_phase,
    log_user_stage as _log_user_stage,
    log_vf_ascii_chart as _log_vf_ascii_chart,
    log_vf_point_list as _log_vf_point_list,
)

__all__ = [
    "_build_voltage_scan_result",
    "_clock_bump_recovery_limit_from_unsafe_entries",
    "_core_clock_below_floor",
    "_final_failure_can_accept_budget_curve",
    "_is_power_up_efficiency_down_regression",
    "_probe_failure_should_mark_voltage_unsafe",
    "_real_clock_adjusted_stable_curve",
    "_target_core_clock_floor",
    "_telemetry_sample_is_busy",
    "run_auto_uv_voltage_scan",
]


@dataclass(frozen=True, slots=True)
class _AutoUvScanSettings:
    q2rtx_config: Q2RTXStabilityConfig
    auto_uv_mode: str
    final_clock_drop_margin_pct: float
    min_performance_core_clock_pct: float
    preserve_base_below_mv: int | None
    configured_max_drop_pct: float
    final_verification_duration_s: int
    short_probe_base_duration_s: int
    efficiency_stop_streak: int
    min_efficiency_stop_voltage_drop_pct: float
    clock_bump_budget_ratio: float
    clock_bump_budget_limit_pct: float


def _assert_zero_runtime_vf_offsets(reader) -> None:
    reader.refresh_points()
    stale = []
    for point in reader.editable_core_points():
        current_offset_mhz = int(point["current_offset_khz"] // 1000)
        if current_offset_mhz != 0:
            stale.append(
                f"{int(point['voltage_uv'] // 1000)}mV={current_offset_mhz:+d}MHz"
            )
    if stale:
        sample = ", ".join(stale[:12])
        suffix = f", ... {len(stale) - 12} more" if len(stale) > 12 else ""
        raise AutoUvError(
            f"failed to clear per-point V/F offsets before Auto-UV: {sample}{suffix}"
        )


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
    short_probe_base_duration_s: int | None = None,
    reset_plan: list[dict] | None = None,
    timedemo_warmup_runs: int = 0,
    event_callback: AutoUvEventCallback | None = None,
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
            base_duration_s=short_probe_base_duration_s,
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
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
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
    baseline_fps = _baseline_value(stable_history, "avg_fps")
    baseline_power_w = _baseline_value(stable_history, "avg_power_w")
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
    if baseline_fps is not None:
        parts.append(
            "fps-floor="
            f"{baseline_fps * _percent(AUTO_UV_METRIC_TUNING.min_proper_run_fps_pct):.1f}"
        )
    if baseline_power_w is not None:
        parts.append(
            "load-power-floor="
            f"{baseline_power_w * _percent(AUTO_UV_METRIC_TUNING.min_proper_run_power_pct):.1f}W"
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


def _scan_settings(
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
) -> _AutoUvScanSettings:
    if q2rtx_config.timedemo_loops is None and int(q2rtx_config.duration_s) <= 0:
        raise AutoUvError(
            "auto-UV voltage scan needs either a positive timedemo loop count or a positive duration"
        )

    normalized_q2rtx_config = _normalize_probe_config(q2rtx_config)
    auto_uv_mode = normalize_auto_uv_mode(runtime_options.get("auto_uv_mode"))
    final_clock_drop_margin_pct = runtime_options.get("auto_uv_max_clock_drop_pct")
    if final_clock_drop_margin_pct is None:
        final_clock_drop_margin_pct = AUTO_UV_METRIC_TUNING.max_core_clock_drop_pct
    final_clock_drop_margin_pct = max(
        0.0, min(100.0, float(final_clock_drop_margin_pct))
    )
    min_performance_core_clock_pct = max(
        0.0, 100.0 - float(final_clock_drop_margin_pct)
    )
    preserve_base_below_mv = runtime_options.get(
        "preserve_base_below_mv", runtime_options.get("preserve_vanilla_below_mv")
    )
    if preserve_base_below_mv is not None:
        preserve_base_below_mv = int(preserve_base_below_mv)
    configured_max_drop_pct = runtime_options.get("auto_uv_max_drop_pct")
    if configured_max_drop_pct is None:
        configured_max_drop_pct = (
            AUTO_UV_DEFAULTS.performance_max_drop_pct
            if auto_uv_mode == AUTO_UV_MODE_PERFORMANCE
            else AUTO_UV_DEFAULTS.max_drop_pct
        )
    configured_max_drop_pct = max(0.0, float(configured_max_drop_pct))
    final_verification_duration_s = int(
        runtime_options.get(
            "auto_uv_final_seconds",
            AUTO_UV_DEFAULTS.final_duration_s,
        )
        or AUTO_UV_DEFAULTS.final_duration_s
    )
    final_verification_duration_s = max(1, int(final_verification_duration_s))
    short_probe_base_duration_s = int(
        runtime_options.get(
            "auto_uv_short_seconds",
            AUTO_UV_DEFAULTS.probe_duration_s,
        )
        or AUTO_UV_DEFAULTS.probe_duration_s
    )
    short_probe_base_duration_s = max(10, min(60, int(short_probe_base_duration_s)))
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
    clock_bump_budget_ratio = runtime_options.get("auto_uv_clock_bump_budget_ratio")
    if clock_bump_budget_ratio is None:
        clock_bump_budget_ratio = (
            AUTO_UV_DEFAULTS.performance_clock_bump_budget_ratio
            if auto_uv_mode == AUTO_UV_MODE_PERFORMANCE
            else AUTO_UV_DEFAULTS.clock_bump_budget_ratio
        )
    max_clock_bump_budget_ratio = (
        AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO
        if bool(runtime_options.get("auto_uv_yolo"))
        else AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO
    )
    clock_bump_budget_ratio = max(
        0.0,
        min(float(max_clock_bump_budget_ratio), float(clock_bump_budget_ratio)),
    )
    clock_bump_budget_limit_pct = _clock_bump_budget_pct(
        max_clock_drop_pct=float(final_clock_drop_margin_pct),
        bump_budget_ratio=float(clock_bump_budget_ratio),
        max_budget_ratio=float(max_clock_bump_budget_ratio),
    )
    return _AutoUvScanSettings(
        q2rtx_config=normalized_q2rtx_config,
        auto_uv_mode=str(auto_uv_mode),
        final_clock_drop_margin_pct=float(final_clock_drop_margin_pct),
        min_performance_core_clock_pct=float(min_performance_core_clock_pct),
        preserve_base_below_mv=preserve_base_below_mv,
        configured_max_drop_pct=float(configured_max_drop_pct),
        final_verification_duration_s=int(final_verification_duration_s),
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        efficiency_stop_streak=int(efficiency_stop_streak),
        min_efficiency_stop_voltage_drop_pct=float(
            min_efficiency_stop_voltage_drop_pct
        ),
        clock_bump_budget_ratio=float(clock_bump_budget_ratio),
        clock_bump_budget_limit_pct=float(clock_bump_budget_limit_pct),
    )


def _auto_uv_memory_offset_mhz(
    runtime_options: dict,
    policy_controller=None,
) -> tuple[int | None, int]:
    raw_value = runtime_options.get(
        "auto_uv_memory_offset_mhz",
        runtime_options.get("memory_offset_mhz"),
    )
    if raw_value in (None, ""):
        return None, MAX_AFTERBURNER_MEM_OFFSET_MHZ
    requested = max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(raw_value)))
    effective_max = MAX_AFTERBURNER_MEM_OFFSET_MHZ
    if policy_controller is not None:
        try:
            driver_range = policy_controller.get_memory_clock_offset_range_mhz()
        except Exception:
            driver_range = None
        if driver_range:
            _driver_min, driver_max = driver_range
            effective_max = max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(driver_max)))
            requested = min(int(requested), int(effective_max))
    return int(requested), int(effective_max)


def _build_voltage_scan_result(
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


def _selection_candidate_id(*, voltage_mv: int, lock_clock_mhz: int) -> str:
    return f"{int(voltage_mv)}mv-{int(lock_clock_mhz)}mhz"


def _candidate_plan_from_record(candidate: dict) -> list[dict] | None:
    plan = candidate.get("plan")
    if isinstance(plan, list) and plan:
        return [dict(item) for item in plan if isinstance(item, dict)]
    points = candidate.get("points")
    if not isinstance(points, list) or not points:
        return None
    converted = []
    for point in points:
        if not isinstance(point, dict):
            continue
        item = dict(point)
        if "target_mhz" in item and "base_mhz" in item:
            item["new_offset_mhz"] = int(item["target_mhz"]) - int(item["base_mhz"])
        converted.append(item)
    return converted or None


def _candidate_selection_summary(
    candidate: dict,
    *,
    short_verification_duration_s: int | None = None,
) -> dict:
    summary = {
        "candidate_id": str(candidate.get("candidate_id", "")),
        "label": str(candidate.get("label", candidate.get("reason", ""))),
        "reason": str(candidate.get("reason", "")),
        "final_verified": bool(candidate.get("final_verified", False)),
        "candidate_voltage_mv": candidate.get("candidate_voltage_mv"),
        "lock_clock_mhz": candidate.get("lock_clock_mhz"),
        "avg_core_clock_mhz": candidate.get("avg_core_clock_mhz"),
        "avg_fps": candidate.get("avg_fps"),
        "avg_power_w": candidate.get("avg_power_w"),
        "efficiency_fps_per_w": candidate.get("efficiency_fps_per_w"),
    }
    if "performance_score" in candidate:
        summary["performance_score"] = candidate.get("performance_score")
    if short_verification_duration_s is not None:
        summary["short_verification_duration_s"] = int(
            short_verification_duration_s
        )
    return summary


def _candidate_efficiency_fps_per_w(candidate: dict) -> float | None:
    value = candidate.get("efficiency_fps_per_w")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_fpsw_sort_key(candidate: dict) -> tuple[bool, float, int, int]:
    efficiency = _candidate_efficiency_fps_per_w(candidate)
    return (
        efficiency is None,
        -float(efficiency or 0.0),
        int(candidate.get("candidate_voltage_mv") or 99999),
        -int(candidate.get("lock_clock_mhz") or 0),
    )


def _short_verification_duration_s(
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
    base_duration_s: int | None = None,
) -> int:
    return _tiered_probe_duration_s(
        initial_target_voltage_mv=initial_target_voltage_mv,
        candidate_voltage_mv=candidate_voltage_mv,
        base_duration_s=base_duration_s,
    )


def _candidate_short_verification_duration_s(
    candidate: dict,
    *,
    initial_target_voltage_mv: int,
    base_duration_s: int | None = None,
    use_recorded_duration: bool = True,
) -> int:
    value = candidate.get("short_verification_duration_s")
    if use_recorded_duration and value not in (None, ""):
        try:
            return max(1, int(round(float(value))))
        except (TypeError, ValueError):
            pass
    return _short_verification_duration_s(
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        candidate_voltage_mv=candidate.get("candidate_voltage_mv") or 0,
        base_duration_s=base_duration_s,
    )


def _candidate_record_from_probe(
    probe: AutoUvProbeSummary,
    *,
    source_plan: list[dict],
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
) -> dict | None:
    voltage_mv = int(probe.candidate_voltage_mv)
    lock_clock_mhz = int(probe.lock_clock_mhz)
    if (
        voltage_mv == int(stable_voltage_mv)
        and lock_clock_mhz == int(stable_lock_clock_mhz)
    ):
        plan = list(stable_plan)
    else:
        try:
            plan = _build_descended_plan(
                source_plan,
                lock_clock_mhz=int(lock_clock_mhz),
                candidate_voltage_mv=int(voltage_mv),
            )
        except AutoUvError:
            return None
    return {
        "candidate_id": _selection_candidate_id(
            voltage_mv=int(voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
        ),
        "label": "passed-initial-stability",
        "reason": str(probe.result_reason or "passed-initial-stability"),
        "final_verified": False,
        "candidate_voltage_mv": int(voltage_mv),
        "lock_clock_mhz": int(lock_clock_mhz),
        "avg_core_clock_mhz": (
            float(probe.avg_core_clock_mhz)
            if probe.avg_core_clock_mhz is not None
            else None
        ),
        "avg_fps": float(probe.avg_fps) if probe.avg_fps is not None else None,
        "avg_power_w": (
            float(probe.avg_power_w) if probe.avg_power_w is not None else None
        ),
        "efficiency_fps_per_w": (
            float(probe.efficiency_fps_per_w)
            if probe.efficiency_fps_per_w is not None
            else None
        ),
        "plan": plan,
    }


def _matching_probe_for_candidate(
    history: list[AutoUvProbeSummary],
    *,
    voltage_mv: int,
    lock_clock_mhz: int,
) -> AutoUvProbeSummary | None:
    for probe in reversed(history):
        if int(probe.candidate_voltage_mv) == int(voltage_mv) and int(
            probe.lock_clock_mhz
        ) == int(lock_clock_mhz):
            return probe
    return history[-1] if history else None


def _choose_final_verification_candidate(
    *,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
    auto_uv_mode: str = "efficiency",
    base_probe: AutoUvProbeSummary | None = None,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    stable_history: list[AutoUvProbeSummary],
    source_plan: list[dict],
    final_verification_duration_s: int,
    initial_target_voltage_mv: int,
    short_probe_base_duration_s: int,
) -> tuple[list[dict], int, int, AutoUvProbeSummary | None, int]:
    candidates_by_id: dict[str, dict] = {}

    for probe in stable_history:
        candidate = _candidate_record_from_probe(
            probe,
            source_plan=source_plan,
            stable_plan=stable_plan,
            stable_voltage_mv=int(stable_voltage_mv),
            stable_lock_clock_mhz=int(stable_lock_clock_mhz),
        )
        if candidate is None:
            continue
        candidates_by_id[str(candidate.get("candidate_id", ""))] = candidate

    current_stable_id = _selection_candidate_id(
        voltage_mv=int(stable_voltage_mv),
        lock_clock_mhz=int(stable_lock_clock_mhz),
    )
    stable_record = {
        "candidate_id": current_stable_id,
        "label": "current-stable-candidate",
        "reason": "current-stable-candidate",
        "candidate_voltage_mv": int(stable_voltage_mv),
        "lock_clock_mhz": int(stable_lock_clock_mhz),
        "avg_core_clock_mhz": (
            float(stable_probe.avg_core_clock_mhz)
            if stable_probe is not None and stable_probe.avg_core_clock_mhz is not None
            else None
        ),
        "avg_fps": (
            float(stable_probe.avg_fps)
            if stable_probe is not None and stable_probe.avg_fps is not None
            else None
        ),
        "avg_power_w": (
            float(stable_probe.avg_power_w)
            if stable_probe is not None and stable_probe.avg_power_w is not None
            else None
        ),
        "efficiency_fps_per_w": (
            float(stable_probe.efficiency_fps_per_w)
            if stable_probe is not None
            and stable_probe.efficiency_fps_per_w is not None
            else None
        ),
        "plan": list(stable_plan),
    }
    stable_record.update(
        {
            key: value
            for key, value in candidates_by_id.get(current_stable_id, {}).items()
            if key not in {"plan"}
        }
    )
    candidates_by_id[current_stable_id] = stable_record

    if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE:
        annotate_performance_candidate_scores(
            list(candidates_by_id.values()),
            base_probe=base_probe,
        )
        candidates = sorted(
            candidates_by_id.values(),
            key=lambda candidate: performance_candidate_sort_key(
                candidate,
                base_probe=base_probe,
            ),
        )
        sort_label = "performance-score"
    else:
        candidates = sorted(candidates_by_id.values(), key=_candidate_fpsw_sort_key)
        sort_label = "fps-per-w"
    default_id = (
        str(candidates[0].get("candidate_id", "")) if candidates else current_stable_id
    )
    request_path = _final_choice_request_path()
    response_path = _final_choice_response_path()
    try:
        response_path.unlink()
    except FileNotFoundError:
        pass
    request_payload = {
        "format_version": 1,
        "auto_uv_mode": normalize_auto_uv_mode(auto_uv_mode),
        "default_sort_metric": sort_label,
        "default_candidate_id": default_id,
        "final_verification_duration_s": int(final_verification_duration_s),
        "final_verification_duration_label": _format_user_duration(
            final_verification_duration_s
        ),
        "max_final_verification_duration_s": 3600,
        "request_path": str(request_path),
        "response_path": str(response_path),
        "candidates": [
            _candidate_selection_summary(
                candidate,
                short_verification_duration_s=_candidate_short_verification_duration_s(
                    candidate,
                    initial_target_voltage_mv=int(initial_target_voltage_mv),
                    base_duration_s=int(short_probe_base_duration_s),
                    use_recorded_duration=False,
                ),
            )
            for candidate in candidates
        ],
    }
    _safe_json_write(request_path, request_payload)
    emit_event(event_callback, "final_choice_request", **request_payload)
    _log_phase(
        log,
        "final-choice",
        f"waiting-for-ui-selection default={default_id} sort={sort_label} "
        f"response={response_path}",
    )
    while not response_path.exists():
        if auto_uv_stop_requested():
            raise KeyboardInterrupt()
        time.sleep(0.25)
    try:
        response = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        response = {}
    response_action = str(response.get("action", "")).strip().lower()
    if response_action in {"discard", "cancel", "cancelled"} or bool(
        response.get("discarded")
    ):
        _log_phase(
            log,
            "final-choice",
            "discarded-by-user; final verification skipped",
        )
        emit_event(
            event_callback,
            "final_choice_discarded",
            reason="user-discarded",
        )
        raise AutoUvFinalChoiceDiscarded(
            "Final verification discarded by user; no profile was saved."
        )
    selected_id = str(response.get("candidate_id", default_id))
    selected = next(
        (
            candidate
            for candidate in candidates
            if str(candidate.get("candidate_id", "")) == selected_id
        ),
        None,
    )
    if selected is None:
        _log_phase(
            log,
            "final-choice",
            f"selection-missing id={selected_id}; using default={default_id}",
        )
        selected = next(
            candidate
            for candidate in candidates
            if str(candidate.get("candidate_id", "")) == default_id
        )
    selected_plan = _candidate_plan_from_record(selected) or stable_plan
    selected_voltage_mv = int(selected.get("candidate_voltage_mv", stable_voltage_mv))
    selected_lock_clock_mhz = int(
        selected.get("lock_clock_mhz", stable_lock_clock_mhz)
    )
    selected_short_duration_s = _candidate_short_verification_duration_s(
        selected,
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        base_duration_s=int(short_probe_base_duration_s),
        use_recorded_duration=False,
    )
    selected_final_duration_s = _coerce_final_choice_duration_s(
        response.get("final_verification_duration_s"),
        default_s=int(final_verification_duration_s),
        min_s=int(selected_short_duration_s),
        max_s=3600,
    )
    selected_probe = _matching_probe_for_candidate(
        stable_history,
        voltage_mv=int(selected_voltage_mv),
        lock_clock_mhz=int(selected_lock_clock_mhz),
    )
    _log_phase(
        log,
        "final-choice",
        f"selected={selected_id} {selected_voltage_mv}mV@{selected_lock_clock_mhz}MHz "
        f"duration={selected_final_duration_s}s "
        f"min-duration={selected_short_duration_s}s max-duration=3600s",
    )
    return (
        selected_plan,
        selected_voltage_mv,
        selected_lock_clock_mhz,
        selected_probe,
        selected_final_duration_s,
    )


def _coerce_final_choice_duration_s(
    value,
    *,
    default_s: int,
    min_s: int = 1,
    max_s: int = 3600,
) -> int:
    max_duration_s = max(1, int(max_s))
    min_duration_s = min(max(1, int(min_s)), max_duration_s)
    try:
        duration_s = int(round(float(value)))
    except (TypeError, ValueError):
        duration_s = int(default_s)
    return max(min_duration_s, min(max_duration_s, int(duration_s)))


def _curve_overclock_summary(
    *,
    final_plan: list[dict],
    base_plan: list[dict] | None,
    final_voltage_mv: int,
) -> dict | None:
    if not base_plan:
        return None
    final_by_voltage = {int(item["voltage_mv"]): item for item in final_plan}
    base_by_voltage = {int(item["voltage_mv"]): item for item in base_plan}
    common_voltages = sorted(set(final_by_voltage) & set(base_by_voltage))
    offsets = []
    for voltage_mv in common_voltages:
        final_item = final_by_voltage[voltage_mv]
        base_item = base_by_voltage[voltage_mv]
        if bool(final_item.get("preserve_base")):
            continue
        offsets.append(int(final_item["target_mhz"]) - int(base_item["target_mhz"]))
    if not offsets:
        return None
    lock_voltage_mv = _nearest_voltage_bin(final_plan, int(final_voltage_mv))
    lock_final = final_by_voltage.get(int(lock_voltage_mv))
    lock_base = base_by_voltage.get(int(lock_voltage_mv))
    lock_offset_mhz = None
    lock_base_mhz = None
    lock_final_mhz = None
    if lock_final is not None and lock_base is not None:
        lock_final_mhz = int(lock_final["target_mhz"])
        lock_base_mhz = int(lock_base["target_mhz"])
        lock_offset_mhz = int(lock_final_mhz) - int(lock_base_mhz)
    return {
        "lock_voltage_mv": int(lock_voltage_mv),
        "lock_final_mhz": lock_final_mhz,
        "lock_base_mhz": lock_base_mhz,
        "lock_offset_mhz": lock_offset_mhz,
        "min_offset_mhz": min(offsets),
        "max_offset_mhz": max(offsets),
        "avg_offset_mhz": sum(offsets) / float(len(offsets)),
        "positive_points": sum(1 for offset in offsets if int(offset) > 0),
        "total_points": len(offsets),
    }


def _clock_bump_recovery_limit_from_unsafe_entries(
    unsafe_entries: list[dict],
    configured_limit: int,
) -> int:
    effective_limit = max(0, int(configured_limit))
    for entry in unsafe_entries:
        if str(entry.get("reason", "")) != "previous-run-abruptly-ended":
            continue
        if str(entry.get("phase", "")) not in {
            "candidate-recovery",
            "final-recovery",
        }:
            continue
        details = entry.get("details")
        if not isinstance(details, dict):
            continue
        marker_details = details.get("marker_details")
        if not isinstance(marker_details, dict):
            continue
        try:
            crashed_attempt = int(marker_details["clock_bump_attempt"])
        except (KeyError, TypeError, ValueError):
            continue
        effective_limit = min(effective_limit, max(0, int(crashed_attempt) - 1))
    return int(effective_limit)


def _clock_bump_budget_limit_from_unsafe_entries(
    unsafe_entries: list[dict],
    configured_budget_pct: float,
) -> float:
    effective_budget_pct = max(0.0, float(configured_budget_pct))
    for entry in unsafe_entries:
        if str(entry.get("reason", "")) != "previous-run-abruptly-ended":
            continue
        if str(entry.get("phase", "")) not in {
            "candidate-recovery",
            "final-recovery",
        }:
            continue
        details = entry.get("details")
        if not isinstance(details, dict):
            continue
        marker_details = details.get("marker_details")
        if not isinstance(marker_details, dict):
            continue
        try:
            used_before_pct = float(
                marker_details["clock_bump_budget_used_before_pct"]
            )
        except (KeyError, TypeError, ValueError):
            try:
                crashed_attempt = int(marker_details["clock_bump_attempt"])
            except (KeyError, TypeError, ValueError):
                continue
            if crashed_attempt <= 1:
                used_before_pct = 0.0
            else:
                continue
        effective_budget_pct = min(
            effective_budget_pct,
            max(0.0, float(used_before_pct)),
        )
    return float(effective_budget_pct)


def _timedemo_warmup_runs_for_mode(auto_uv_mode: str) -> int:
    if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE:
        return max(0, int(AUTO_UV_METRIC_TUNING.performance_timedemo_warmup_runs))
    return 0


def _run_auto_uv_voltage_scan_impl(
    *,
    gpu_index,
    runtime_options,
    q2rtx_config,
    log=print,
    event_callback: AutoUvEventCallback | None = None,
):
    settings = _scan_settings(runtime_options, q2rtx_config)
    q2rtx_config = settings.q2rtx_config
    auto_uv_mode = settings.auto_uv_mode
    final_clock_drop_margin_pct = settings.final_clock_drop_margin_pct
    min_performance_core_clock_pct = settings.min_performance_core_clock_pct
    preserve_base_below_mv = settings.preserve_base_below_mv
    configured_max_drop_pct = settings.configured_max_drop_pct
    final_verification_duration_s = settings.final_verification_duration_s
    short_probe_base_duration_s = settings.short_probe_base_duration_s
    efficiency_stop_streak = settings.efficiency_stop_streak
    min_efficiency_stop_voltage_drop_pct = settings.min_efficiency_stop_voltage_drop_pct
    timedemo_warmup_runs = _timedemo_warmup_runs_for_mode(auto_uv_mode)
    interrupted_probe = _consume_interrupted_uv_probe_marker()
    unsafe_voltage_entries = _load_uv_unsafe_voltage_entries()
    if interrupted_probe is not None:
        blacklist_path, unsafe_entry = interrupted_probe
        _log_phase(
            log,
            "crash-recovery",
            "previous auto-UV probe ended abruptly; "
            f"blacklisted={int(unsafe_entry['candidate_voltage_mv'])}mV "
            f"target={int(unsafe_entry['lock_clock_mhz'])}MHz "
            f"phase={unsafe_entry.get('phase') or 'unknown'} "
            f"blacklist={blacklist_path}",
        )
        _log_user_stage(
            log,
            "Previous Auto-UV probe ended abruptly",
            [
                (
                    "PenguinBurner found a stale active-probe marker from the previous run. "
                    "Clean Ctrl-C/SIGTERM shutdown removes this marker, so this usually means "
                    "the system rebooted, crashed, lost power, or the process was forcibly killed "
                    "during a voltage test."
                ),
                (
                    f"Point {int(unsafe_entry['candidate_voltage_mv'])}mV @ "
                    f"{int(unsafe_entry['lock_clock_mhz'])}MHz is now marked unsafe "
                    "and this run will not test that point, or a more aggressive "
                    "lower-voltage/equal-or-higher-clock point, again."
                ),
                "Lower-clock efficiency points at that voltage can still be tested.",
            ],
        )
        unsafe_voltage_entries = _load_uv_unsafe_voltage_entries()
    effective_clock_bump_budget_limit_pct = (
        _clock_bump_budget_limit_from_unsafe_entries(
            unsafe_voltage_entries,
            float(settings.clock_bump_budget_limit_pct),
        )
    )
    if float(effective_clock_bump_budget_limit_pct) < float(
        settings.clock_bump_budget_limit_pct
    ):
        _log_phase(
            log,
            "crash-recovery",
            f"reduced overclock budget "
            f"configured={float(settings.clock_bump_budget_limit_pct):.2f}% "
            f"effective={float(effective_clock_bump_budget_limit_pct):.2f}% "
            "reason=previous recovery probe ended abruptly; retry budget is capped before the failed bump",
        )

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
        cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
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
        initial_budget_payload = overclock_budget_event_payload(
            used_pct=0.0,
            limit_pct=float(effective_clock_bump_budget_limit_pct),
            max_clock_drop_pct=float(final_clock_drop_margin_pct),
        )
        runtime_default_plan = list(runtime_reset["plan"])
        apply_plan(reader, runtime_default_plan)
        _assert_zero_runtime_vf_offsets(reader)
        emit_event(
            event_callback,
            "source_curve",
            points=plan_event_points(runtime_default_plan),
        )
        source_result = {
            "plan": runtime_default_plan,
            "translation_mode": "runtime-defaults",
            "changed_points": [],
        }
        _validate_auto_uv_source_plan(source_result["plan"])
        apply_plan(reader, source_result["plan"])
        _assert_zero_runtime_vf_offsets(reader)
        translated_gpu_policy = {
            "power_limit_w": runtime_reset.get("power_limit_w"),
        }
        memory_offset_mhz, memory_offset_limit_mhz = _auto_uv_memory_offset_mhz(
            runtime_options,
            policy_controller=policy_controller,
        )
        if memory_offset_mhz is not None:
            translated_gpu_policy["mem_clk_vf_offset_mhz"] = int(memory_offset_mhz)
            translated_gpu_policy["mem_clk_vf_offset_limit_mhz"] = int(
                memory_offset_limit_mhz
            )
            if int(memory_offset_mhz) != 0:
                try:
                    policy_controller.apply_clock_offsets(
                        mem_clk_vf_offset_mhz=int(memory_offset_mhz)
                    )
                except Exception as exc:
                    raise AutoUvError(
                        "failed to apply Auto-UV memory offset "
                        f"{int(memory_offset_mhz):+d} MHz; driver rejected "
                        f"nvmlDeviceSetMemClkVfOffset: {exc}"
                    ) from exc
                _log_phase(
                    log,
                    "source",
                    f"memory-offset={int(memory_offset_mhz):+d}MHz "
                    f"limit=0..{int(memory_offset_limit_mhz)}MHz",
                )
        _log_phase(log, "source", "baseline=runtime-defaults")
        _log_phase(
            log,
            "source",
            f"plan-mode={source_result['translation_mode']} "
            f"auto-uv-mode={auto_uv_mode} "
            + (
                f"loops={int(q2rtx_config.timedemo_loops)} "
                if q2rtx_config.timedemo_loops is not None
                else f"duration={int(q2rtx_config.duration_s)}s "
            )
            + f"min-performance-core-clock="
            f"{float(min_performance_core_clock_pct):.1f}% "
            f"max-clock-drop={final_clock_drop_margin_pct:.1f}%",
        )
        if timedemo_warmup_runs > 0:
            _log_phase(
                log,
                "source",
                f"timedemo-warmup-runs={int(timedemo_warmup_runs)} "
                "ignored-for-decision-fps",
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
                "Short verification tiers: "
                f"{_format_user_duration(short_probe_base_duration_s)}, "
                f"{_format_user_duration(short_probe_base_duration_s * 2)}, "
                f"{_format_user_duration(short_probe_base_duration_s * 3)}.",
                "Each step applies one candidate curve, runs Q2RTX/CUDA, then either accepts it or restores the previous stable curve.",
            ],
        )
        if runtime_reset.get("power_limit_w") is not None:
            _log_phase(
                log,
                "source",
                f"power-limit={int(runtime_reset['power_limit_w'])}W source=runtime-defaults",
            )
        if preserve_base_below_mv is not None:
            _log_phase(
                log,
                "source",
                f"base curve stays at and below {preserve_base_below_mv}mV",
            )

        discovery_label_clock_mhz = max(
            int(item["target_mhz"])
            for item in source_result["plan"]
            if not bool(item.get("preserve_base"))
        )
        discovery_label_voltage_mv = _nearest_voltage_bin(
            source_result["plan"],
            _find_lock_voltage_for_clock(
                source_result["plan"],
                discovery_label_clock_mhz,
            ),
        )
        apply_plan(reader, source_result["plan"])
        reader.refresh_points()

        discovery_probe_config = _short_probe_config(
            q2rtx_config,
            target_duration_s=int(short_probe_base_duration_s),
        )
        _log_user_stage(
            log,
            "Stage 1 - measuring the baseline",
            [
                "PenguinBurner is applying the untouched default curve and running a short probe.",
                "This measures the real base sustained clock, voltage, power, temperature, and fan speed for about "
                f"{_format_user_duration(short_probe_base_duration_s)} before undervolting.",
                "The first warm-up seconds are ignored for decision averages so Q2RTX ramp-up does not skew the baseline.",
            ],
        )
        emit_event(
            event_callback,
            "probe_start",
            stage="base-baseline",
            voltage_mv=int(discovery_label_voltage_mv),
            clock_mhz=int(discovery_label_clock_mhz),
            label="base default curve",
            **initial_budget_payload,
        )
        discovery_summary, discovery_result = _probe_voltage_candidate(
            reader=reader,
            candidate_plan=source_result["plan"],
            candidate_voltage_mv=discovery_label_voltage_mv,
            lock_clock_mhz=discovery_label_clock_mhz,
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
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
        )
        probe_history.append(discovery_summary)
        emit_event(
            event_callback,
            "probe_result",
            **probe_event_payload(
                discovery_summary,
                stage="base-baseline",
                decision="pass",
                reason="base baseline measured",
            ),
            **initial_budget_payload,
        )
        _log_benchmark(log, phase="discover", probe=discovery_summary)
        if not discovery_result.success:
            raise AutoUvError(
                "base Defaults baseline failed the Q2RTX probe: "
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
        measured_clock_candidates = [
            float(value)
            for value in (
                saturated_clock_mhz,
                active_avg_clock_mhz,
                active_preferred_clock_mhz,
                fallback_clock_mhz,
            )
            if value is not None
        ]
        measured_clock_mhz = (
            min(measured_clock_candidates) if measured_clock_candidates else None
        )
        if measured_clock_mhz is None:
            raise AutoUvError(
                "base Defaults baseline did not report an average core clock"
            )
        preferred_lock_clock_mhz = _choose_sustained_clock_target(
            source_result["plan"],
            measured_clock_mhz,
        )
        lock_clock_mhz = int(preferred_lock_clock_mhz)
        base_lock_voltage_mv = _nearest_voltage_bin(
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
                else int(base_lock_voltage_mv)
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
        emit_event(
            event_callback,
            "candidate_curve",
            voltage_mv=int(start_voltage_mv),
            clock_mhz=int(lock_clock_mhz),
            stage="baseline",
            points=plan_event_points(flattened_plan),
            **initial_budget_payload,
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
            f"base-anchor={base_lock_voltage_mv}mV "
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
        if unsafe_voltage_entries:
            _log_phase(
                log,
                "unsafe-cache",
                f"entries={len(unsafe_voltage_entries)} mode=clock-band "
                "rule=block failed voltage/lower voltage only inside the failed-clock band",
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
        emit_event(
            event_callback,
            "probe_start",
            stage="baseline",
            voltage_mv=int(start_voltage_mv),
            clock_mhz=int(lock_clock_mhz),
            label="first flattened curve",
            **initial_budget_payload,
        )
        baseline_summary, baseline_result = _probe_voltage_candidate(
            reader=reader,
            candidate_plan=flattened_plan,
            candidate_voltage_mv=int(start_voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
            q2rtx_config=_short_probe_config(
                q2rtx_config,
                target_duration_s=int(short_probe_base_duration_s),
            ),
            stable_history=[],
            initial_probe_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label="baseline",
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            enforce_target_core_clock_floor=False,
            summarize_saturated_tail=True,
            use_power_limit_floor=True,
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            reset_plan=runtime_default_plan,
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
        )
        probe_history.append(baseline_summary)
        emit_event(
            event_callback,
            "probe_result",
            **probe_event_payload(
                baseline_summary,
                stage="baseline",
                decision="pass" if baseline_result.success else "fail",
                reason=str(getattr(baseline_result, "reason", "")),
            ),
            **initial_budget_payload,
        )
        _log_benchmark(
            log,
            phase="baseline",
            probe=baseline_summary,
            reference_probe=discovery_summary,
            reference_label="base",
        )
        _log_phase(
            log,
            "baseline",
            "verified first flattened real-clock curve before starting descent",
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
                short_probe_base_duration_s=int(short_probe_base_duration_s),
                reset_plan=runtime_default_plan,
                event_callback=event_callback,
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
        stable_probe = baseline_summary
        stable_lock_clock_mhz = _real_probe_lock_clock_mhz(
            source_result["plan"],
            probe=stable_probe,
            previous_lock_clock_mhz=int(lock_clock_mhz),
        )
        if int(stable_lock_clock_mhz) != int(lock_clock_mhz):
            stable_plan = _build_descended_plan(
                flattened_plan,
                lock_clock_mhz=int(stable_lock_clock_mhz),
                candidate_voltage_mv=int(stable_voltage_mv),
            )
            flattened_plan = stable_plan
            lock_clock_mhz = int(stable_lock_clock_mhz)
            flatten_target["lock_clock_mhz"] = int(stable_lock_clock_mhz)
            if clock_ceiling is not None:
                clock_ceiling.retarget(
                    lock_clock_mhz=int(stable_lock_clock_mhz),
                    lock_voltage_mv=int(stable_voltage_mv),
                )
            _log_phase(
                log,
                "baseline",
                f"real-clock target adjusted to {int(stable_lock_clock_mhz)}MHz "
                f"from baseline avg_core_clock={stable_probe.avg_core_clock_mhz:.1f}MHz",
            )
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
            base_probe=discovery_summary,
        )

        first_candidate_voltage_mv = _next_search_candidate_voltage_mv(
            plan=flattened_plan,
            start_voltage_mv=int(start_voltage_mv),
            stable_voltage_mv=int(start_voltage_mv),
            reference_actual_voltage_mv=baseline_summary.avg_voltage_mv,
            preserve_base_below_mv=preserve_base_below_mv,
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

        from .live_sweep import run_auto_uv_candidate_sweep

        _log_phase(log, "auto-uv", "candidate sweep enabled")
        sweep_result = run_auto_uv_candidate_sweep(
            probe_stabilization_search=_probe_stabilization_search,
            log=log,
            reader=reader,
            source_plan=source_result["plan"],
            stable_plan=stable_plan,
            stable_voltage_mv=stable_voltage_mv,
            stable_lock_clock_mhz=stable_lock_clock_mhz,
            stable_probe=stable_probe,
            stable_history=stable_history,
            probe_history=probe_history,
            first_candidate_voltage_mv=first_candidate_voltage_mv,
            discovery_summary=discovery_summary,
            q2rtx_config=q2rtx_config,
            measured_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            translated_gpu_policy=translated_gpu_policy,
            runtime_default_plan=runtime_default_plan,
            clock_ceiling=clock_ceiling,
            min_performance_core_clock_pct=min_performance_core_clock_pct,
            min_search_voltage_mv=min_search_voltage_mv,
            preserve_base_below_mv=preserve_base_below_mv,
            start_voltage_mv=start_voltage_mv,
            clock_bump_budget_limit_pct=float(effective_clock_bump_budget_limit_pct),
            max_clock_drop_pct=float(final_clock_drop_margin_pct),
            short_probe_base_duration_s=int(short_probe_base_duration_s),
            auto_uv_mode=auto_uv_mode,
            efficiency_stop_streak=efficiency_stop_streak,
            min_efficiency_stop_voltage_drop_pct=min_efficiency_stop_voltage_drop_pct,
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
        )
        stable_plan = sweep_result["stable_plan"]
        stable_voltage_mv = sweep_result["stable_voltage_mv"]
        stable_lock_clock_mhz = sweep_result["stable_lock_clock_mhz"]
        stable_probe = sweep_result["stable_probe"]
        clock_bump_recovery_count = int(
            sweep_result.get("clock_bump_recovery_count", 0)
        )
        clock_bump_budget_used_pct = float(
            sweep_result.get("clock_bump_budget_used_pct", 0.0)
        )
        ended_by_clock_bump_limit = bool(
            sweep_result.get("ended_by_clock_bump_limit", False)
        )
        if bool(runtime_options.get("auto_uv_require_final_choice")):
            (
                stable_plan,
                stable_voltage_mv,
                stable_lock_clock_mhz,
                selected_stable_probe,
                selected_final_verification_duration_s,
            ) = _choose_final_verification_candidate(
                log=log,
                event_callback=event_callback,
                auto_uv_mode=auto_uv_mode,
                base_probe=discovery_summary,
                stable_plan=stable_plan,
                stable_voltage_mv=int(stable_voltage_mv),
                stable_lock_clock_mhz=int(stable_lock_clock_mhz),
                stable_probe=stable_probe,
                stable_history=stable_history,
                source_plan=source_result["plan"],
                final_verification_duration_s=int(final_verification_duration_s),
                initial_target_voltage_mv=int(start_voltage_mv),
                short_probe_base_duration_s=int(short_probe_base_duration_s),
            )
            final_verification_duration_s = int(selected_final_verification_duration_s)
            if selected_stable_probe is not None:
                stable_probe = selected_stable_probe

        return _run_final_verification_and_save(
            probe_voltage_candidate=_probe_voltage_candidate,
            probe_stabilization_search=_probe_stabilization_search,
            build_voltage_scan_result=_build_voltage_scan_result,
            curve_overclock_summary=_curve_overclock_summary,
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
            clock_bump_budget_limit_pct=float(effective_clock_bump_budget_limit_pct),
            clock_bump_recovery_count=clock_bump_recovery_count,
            clock_bump_budget_used_pct=float(clock_bump_budget_used_pct),
            max_bump_recovery_was_used=ended_by_clock_bump_limit,
            short_probe_base_duration_s=int(short_probe_base_duration_s),
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
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
        cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
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
    event_callback: AutoUvEventCallback | None = None,
) -> AutoUvVoltageScanResult:
    result = _run_auto_uv_voltage_scan_impl(
        gpu_index=gpu_index,
        runtime_options=runtime_options,
        q2rtx_config=q2rtx_config,
        log=log,
        event_callback=event_callback,
    )
    if not isinstance(result, AutoUvVoltageScanResult):
        raise AutoUvError("auto-UV scanner returned an unexpected result")
    return result
