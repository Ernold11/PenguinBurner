#!/usr/bin/env python3

from __future__ import annotations

import ctypes
from dataclasses import dataclass
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
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
)
from .artifacts import (
    _consume_interrupted_uv_probe_marker,
    _load_uv_unsafe_voltage_entries,
    _write_latest_verified_uv_result,
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
from .candidate_sweep import _run_candidate_sweep
from .final_verify import _run_final_verification_and_save
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
from .tuning import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_METRIC_TUNING,
    AUTO_UV_STALL_TUNING,
)
from .clock_bump import _clock_bump_budget_pct
from .user_output import (
    format_probe_summary as _format_probe_summary,
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
    final_clock_drop_margin_pct: float
    min_performance_core_clock_pct: float
    preserve_vanilla_below_mv: int | None
    configured_max_drop_pct: float
    final_verification_duration_s: int
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
    clock_bump_budget_ratio = runtime_options.get("auto_uv_clock_bump_budget_ratio")
    if clock_bump_budget_ratio is None:
        clock_bump_budget_ratio = AUTO_UV_DEFAULTS.clock_bump_budget_ratio
    clock_bump_budget_ratio = max(0.0, min(1.0, float(clock_bump_budget_ratio)))
    clock_bump_budget_limit_pct = _clock_bump_budget_pct(
        max_clock_drop_pct=float(final_clock_drop_margin_pct),
        bump_budget_ratio=float(clock_bump_budget_ratio),
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
        clock_bump_budget_ratio=float(clock_bump_budget_ratio),
        clock_bump_budget_limit_pct=float(clock_bump_budget_limit_pct),
    )


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


def _curve_overclock_summary(
    *,
    final_plan: list[dict],
    vanilla_plan: list[dict] | None,
    final_voltage_mv: int,
) -> dict | None:
    if not vanilla_plan:
        return None
    final_by_voltage = {int(item["voltage_mv"]): item for item in final_plan}
    vanilla_by_voltage = {int(item["voltage_mv"]): item for item in vanilla_plan}
    common_voltages = sorted(set(final_by_voltage) & set(vanilla_by_voltage))
    offsets = []
    for voltage_mv in common_voltages:
        final_item = final_by_voltage[voltage_mv]
        vanilla_item = vanilla_by_voltage[voltage_mv]
        if bool(final_item.get("preserve_vanilla")):
            continue
        offsets.append(int(final_item["target_mhz"]) - int(vanilla_item["target_mhz"]))
    if not offsets:
        return None
    lock_voltage_mv = _nearest_voltage_bin(final_plan, int(final_voltage_mv))
    lock_final = final_by_voltage.get(int(lock_voltage_mv))
    lock_vanilla = vanilla_by_voltage.get(int(lock_voltage_mv))
    lock_offset_mhz = None
    lock_vanilla_mhz = None
    lock_final_mhz = None
    if lock_final is not None and lock_vanilla is not None:
        lock_final_mhz = int(lock_final["target_mhz"])
        lock_vanilla_mhz = int(lock_vanilla["target_mhz"])
        lock_offset_mhz = int(lock_final_mhz) - int(lock_vanilla_mhz)
    return {
        "lock_voltage_mv": int(lock_voltage_mv),
        "lock_final_mhz": lock_final_mhz,
        "lock_vanilla_mhz": lock_vanilla_mhz,
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
                    f"Voltage {int(unsafe_entry['candidate_voltage_mv'])}mV is now marked unsafe "
                    "and this run will not test it again."
                ),
                "The next search will stop at the next higher voltage bin instead.",
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
        runtime_default_plan = list(runtime_reset["plan"])
        apply_plan(reader, runtime_default_plan)
        _assert_zero_runtime_vf_offsets(reader)
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

        discovery_label_clock_mhz = max(
            int(item["target_mhz"])
            for item in source_result["plan"]
            if not bool(item.get("preserve_vanilla"))
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
            target_duration_s=AUTO_UV_DEFAULTS.probe_duration_s,
        )
        _log_user_stage(
            log,
            "Stage 1 - measuring the baseline",
            [
                "PenguinBurner is applying the untouched default curve and running a short probe.",
                f"This measures the real stock sustained clock, voltage, power, temperature, and fan speed for about {AUTO_UV_DEFAULTS.probe_duration_s}s before undervolting.",
                "The first warm-up seconds are ignored for decision averages so Q2RTX ramp-up does not skew the baseline.",
            ],
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
        baseline_summary, baseline_result = _probe_voltage_candidate(
            reader=reader,
            candidate_plan=flattened_plan,
            candidate_voltage_mv=int(start_voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
            q2rtx_config=_short_probe_config(
                q2rtx_config,
                target_duration_s=AUTO_UV_DEFAULTS.probe_duration_s,
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
        )
        probe_history.append(baseline_summary)
        _log_benchmark(
            log,
            phase="baseline",
            probe=baseline_summary,
            reference_probe=discovery_summary,
            reference_label="stock",
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

        if bool(runtime_options.get("experimental_auto_uv2")):
            from auto_uv2.live_sweep import run_auto_uv2_candidate_sweep

            _log_phase(log, "auto-uv2", "experimental candidate sweep enabled")
            sweep_result = run_auto_uv2_candidate_sweep(
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
                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
                start_voltage_mv=start_voltage_mv,
                clock_bump_budget_limit_pct=float(effective_clock_bump_budget_limit_pct),
                efficiency_stop_streak=efficiency_stop_streak,
                min_efficiency_stop_voltage_drop_pct=(
                    min_efficiency_stop_voltage_drop_pct
                ),
            )
        else:
            sweep_result = _run_candidate_sweep(
                probe_voltage_candidate=_probe_voltage_candidate,
                probe_stabilization_search=_probe_stabilization_search,
                describe_guardrails=_describe_guardrails,
                latest_reference_voltage_mv=_latest_reference_voltage_mv,
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
                clock_bump_budget_limit_pct=float(
                    effective_clock_bump_budget_limit_pct
                ),
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
