from __future__ import annotations

from dataclasses import replace
from typing import Callable

from afterburner.import_vf_curve import apply_plan
from stability.q2rtx import (
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    StabilityTestError,
    print_q2rtx_stability_result,
    query_gpu_metrics,
    run_q2rtx_stability_test,
)

from .artifacts import (
    _clear_uv_probe_in_progress,
    _record_unsafe_uv_voltage,
    _write_uv_probe_in_progress,
)
from .models import AutoUvProbeSummary
from .probe_metrics import (
    _history_average,
    _mean,
    _saturated_tail_samples,
    _summarize_probe,
)
from .scan_rules import (
    _core_clock_below_floor,
    _final_failure_can_accept_budget_curve,
    _percent,
    _probe_failure_should_mark_voltage_unsafe,
    _target_core_clock_floor,
    _telemetry_sample_is_busy,
)
from .tuning import (
    AUTO_UV_CURVE_TUNING,
    AUTO_UV_METRIC_TUNING,
    AUTO_UV_STALL_TUNING,
)
from .user_output import log_phase as _log_phase


def _probe_voltage_candidate(
    *,
    reader,
    candidate_plan: list[dict],
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    q2rtx_config: Q2RTXStabilityConfig,
    stable_history: list[AutoUvProbeSummary],
    initial_probe_clock_mhz: float | None,
    nvml_session,
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
    marker_details: dict | None = None,
    suppress_unsafe_for_controlled_clock_abort: bool = False,
    suppress_unsafe_recording: bool = False,
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
    target_core_clock_floor_mhz, target_core_clock_floor_base_mhz = (
        _target_core_clock_floor(
            lock_clock_mhz=int(lock_clock_mhz),
            initial_probe_clock_mhz=initial_probe_clock_mhz,
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            enforce_target_core_clock_floor=bool(enforce_target_core_clock_floor),
        )
    )

    latest_stable_probe = stable_history[-1] if stable_history else None
    progress_state = {
        "last_completed_runs": 0,
        "last_progress_elapsed_s": 0.0,
        "expected_loop_s": (
            latest_stable_probe.avg_seconds_per_run
            if latest_stable_probe is not None
            else _history_average(stable_history, "avg_seconds_per_run")
        ),
        "low_fps_streak": 0,
        "low_core_clock_streak": 0,
        "low_power_streak": 0,
    }
    busy_power_floor_w = None
    reference_probe = latest_stable_probe
    proper_run_fps_floor = None
    proper_run_power_floor_w = None
    if reference_probe is not None and reference_probe.avg_fps is not None:
        proper_run_fps_floor = float(reference_probe.avg_fps) * _percent(
            AUTO_UV_METRIC_TUNING.min_proper_run_fps_pct
        )
    if reference_probe is not None and reference_probe.avg_power_w is not None:
        proper_run_power_floor_w = float(reference_probe.avg_power_w) * _percent(
            AUTO_UV_METRIC_TUNING.min_proper_run_power_pct
        )
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
            if busy_power_floor_w is not None:
                parts.append(
                    "load=busy"
                    if _telemetry_sample_is_busy(latest_sample, busy_power_floor_w)
                    else "load=idle"
                )
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
            assert target_core_clock_floor_base_mhz is not None
            parts.append(
                f"target-floor={target_core_clock_floor_mhz:.1f}MHz"
                f"({float(min_performance_core_clock_pct):.1f}%"
                f" of {target_core_clock_floor_base_mhz:.1f}MHz baseline)"
            )
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
            if proper_run_fps_floor is not None:
                if int(run.run_index) <= 1:
                    progress_state["low_fps_streak"] = 0
                    continue
                if fps < float(proper_run_fps_floor):
                    progress_state["low_fps_streak"] = (
                        int(progress_state.get("low_fps_streak", 0)) + 1
                    )
                else:
                    progress_state["low_fps_streak"] = 0
                if (
                    int(progress_state["low_fps_streak"])
                    >= AUTO_UV_METRIC_TUNING.min_proper_run_fps_regression_streak
                ):
                    return (
                        f"timedemo-live-fps-regression current={fps:.1f} "
                        f"floor={float(proper_run_fps_floor):.1f} "
                        f"margin={AUTO_UV_METRIC_TUNING.min_proper_run_fps_pct:.1f}% "
                        f"streak={int(progress_state['low_fps_streak'])} "
                        f"run={int(run.run_index)}"
                    )

        telemetry_samples = list(state.get("telemetry_samples") or [])
        busy_telemetry_samples = [
            sample
            for sample in telemetry_samples
            if _telemetry_sample_is_busy(sample, busy_power_floor_w)
        ]
        core_clock_samples = [
            float(sample.core_clock_mhz)
            for sample in busy_telemetry_samples
            if sample is not None and sample.core_clock_mhz is not None
        ]
        latest_sample = state.get("latest_sample")
        live_core_clock_mhz = (
            float(latest_sample.core_clock_mhz)
            if latest_sample is not None and latest_sample.core_clock_mhz is not None
            else None
        )
        live_sample_is_busy = _telemetry_sample_is_busy(
            latest_sample,
            busy_power_floor_w,
        )
        running_avg_core_clock = _mean(core_clock_samples)
        if (
            proper_run_power_floor_w is not None
            and latest_sample is not None
            and latest_sample.power_w is not None
            and len(telemetry_samples)
            >= AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
            and float(state.get("elapsed_s", 0.0))
            >= float(AUTO_UV_METRIC_TUNING.loaded_sample_warmup_s)
        ):
            if not live_sample_is_busy and float(latest_sample.power_w) < float(
                proper_run_power_floor_w
            ):
                progress_state["low_power_streak"] = (
                    int(progress_state.get("low_power_streak", 0)) + 1
                )
            else:
                progress_state["low_power_streak"] = 0
            if (
                int(progress_state["low_power_streak"])
                >= AUTO_UV_METRIC_TUNING.target_core_clock_low_streak_samples
            ):
                busy_floor_text = (
                    f"{float(busy_power_floor_w):.1f}W"
                    if busy_power_floor_w is not None
                    else "n/a"
                )
                return (
                    f"telemetry-live-load-lost current={float(latest_sample.power_w):.1f}W "
                    f"floor={float(proper_run_power_floor_w):.1f}W "
                    f"busy-floor={busy_floor_text} "
                    f"load-floor={AUTO_UV_METRIC_TUNING.min_proper_run_power_pct:.1f}%"
                )
        if (
            target_core_clock_floor_mhz is not None
            and live_core_clock_mhz is not None
            and len(core_clock_samples)
            >= AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
        ):
            if live_sample_is_busy and _core_clock_below_floor(
                live_core_clock_mhz,
                target_core_clock_floor_mhz,
            ):
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
                    f"floor={target_core_clock_floor_mhz:.1f}MHz "
                    f"tolerance={AUTO_UV_CURVE_TUNING.clock_select_tolerance_mhz:.1f}MHz"
                )
        if (
            target_core_clock_floor_mhz is not None
            and running_avg_core_clock is not None
            and len(core_clock_samples)
            >= AUTO_UV_STALL_TUNING.avg_core_clock_abort_min_samples
            and _core_clock_below_floor(
                running_avg_core_clock,
                target_core_clock_floor_mhz,
            )
        ):
            return (
                f"telemetry-live-core_clock-avg current={running_avg_core_clock:.1f}MHz "
                f"floor={target_core_clock_floor_mhz:.1f}MHz "
                f"tolerance={AUTO_UV_CURVE_TUNING.clock_select_tolerance_mhz:.1f}MHz"
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

    mark_in_progress = str(phase_label) in {
        "candidate",
        "candidate-recovery",
        "final-verify",
        "stabilize",
    }

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
            details=marker_details,
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
            failure_details = {
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
            }
            should_record_unsafe = _probe_failure_should_mark_voltage_unsafe(
                str(result.reason)
            )
            if bool(suppress_unsafe_recording):
                should_record_unsafe = False
            if bool(
                suppress_unsafe_for_controlled_clock_abort
            ) and _final_failure_can_accept_budget_curve(str(result.reason)):
                should_record_unsafe = False
            if should_record_unsafe:
                _record_probe_unsafe(
                    "stability-probe-failed",
                    details=failure_details,
                )
            else:
                _log_phase(
                    log,
                    "blacklist",
                    f"unsafe-voltage-skipped voltage={int(candidate_voltage_mv)}mV "
                    f"target={int(lock_clock_mhz)}MHz result={result.reason}",
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
