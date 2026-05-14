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

from ..auto_uv_console_log import log_phase
from ..persistence.auto_uv_persisted_json_files import auto_uv_stop_requested
from ..auto_uv_types import AutoUvProbeSummary
from ..auto_uv_user_options import AUTO_UV_METRIC_TUNING, AUTO_UV_STALL_TUNING
from ..persistence.probe_in_progress_marker_file import (
    clear_probe_in_progress_marker,
    write_probe_in_progress_marker,
)
from .probe_runtime_guardrails import (
    final_failure_can_accept_budget_curve,
    percent,
    probe_failure_should_mark_voltage_unsafe,
    target_core_clock_floor,
    telemetry_sample_is_busy,
)
from .q2rtx_live_abort_rules import (
    telemetry_live_abort_reason,
    timedemo_live_abort_reason,
    timedemo_stall_abort_reason,
)
from .q2rtx_cuda_probe_config import timedemo_seconds_hint
from .q2rtx_probe_summary import (
    decision_timedemo_runs,
    history_average,
    mean,
    saturated_probe_tail_samples,
    summarize_q2rtx_cuda_probe,
)
from ..ui.ui_json_event_writer import AutoUvEventCallback, emit_ui_json_event
from ..persistence.unsafe_voltage_blacklist_file import record_unsafe_voltage


UNSAFE_CLOCK_BINDING_LOWER_BINS = 2


def probe_voltage_candidate(
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
    initial_target_voltage_mv: int | None = None,
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
    expected_total_duration_s: int | None = None,
    timedemo_warmup_runs: int = 0,
    event_callback: AutoUvEventCallback | None = None,
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
    target_floor_mhz, floor_base_mhz = target_core_clock_floor(
        lock_clock_mhz=int(lock_clock_mhz),
        initial_probe_clock_mhz=initial_probe_clock_mhz,
        min_performance_core_clock_pct=float(min_performance_core_clock_pct),
        enforce_target_core_clock_floor=bool(enforce_target_core_clock_floor),
    )

    latest_stable_probe = stable_history[-1] if stable_history else None
    progress_state = {
        "last_completed_runs": 0,
        "last_progress_elapsed_s": 0.0,
        "expected_loop_s": (
            latest_stable_probe.avg_seconds_per_run
            if latest_stable_probe is not None
            else history_average(stable_history, "avg_seconds_per_run")
        )
        or timedemo_seconds_hint(q2rtx_config),
        "low_fps_streak": 0,
        "low_core_clock_streak": 0,
        "low_power_streak": 0,
    }
    busy_power_floor_w, proper_run_fps_floor, proper_run_power_floor_w = (
        live_probe_reference_floors(
            latest_stable_probe,
            power_limit_w=power_limit_w,
        )
    )
    companion_duration_s = companion_duration_s_from_command(
        q2rtx_config.companion_command
    )

    def target_duration_s(state: dict) -> float | None:
        return progress_target_duration_s(
            q2rtx_config=q2rtx_config,
            companion_duration_s=float(companion_duration_s),
            expected_loop_s=progress_state.get("expected_loop_s"),
            state=state,
            expected_total_duration_s=expected_total_duration_s,
        )

    def format_live_status(state: dict) -> str:
        elapsed_s = progress_elapsed_s_for_ui(state)
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
            parts.append(
                "load=busy"
                if telemetry_sample_is_busy(latest_sample, busy_power_floor_w)
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
        if target_floor_mhz is not None:
            assert floor_base_mhz is not None
            parts.append(
                f"target-floor={target_floor_mhz:.1f}MHz"
                f"({float(min_performance_core_clock_pct):.1f}%"
                f" of {floor_base_mhz:.1f}MHz baseline)"
            )
        return " ".join(parts)

    def progress_callback(state: dict) -> None:
        completed_runs = int(state.get("completed_runs", 0))
        wall_elapsed_s = float(state.get("elapsed_s", 0.0))
        progress_elapsed_s = progress_elapsed_s_for_ui(state)
        timedemo_runs = list(state.get("timedemo_runs") or [])
        if completed_runs > int(progress_state["last_completed_runs"]):
            progress_state["last_completed_runs"] = completed_runs
            progress_state["last_progress_elapsed_s"] = wall_elapsed_s
            current_loop_s = mean([float(run.seconds) for run in timedemo_runs])
            if current_loop_s is not None:
                progress_state["expected_loop_s"] = current_loop_s
            log_completed_timedemo_run(
                log,
                phase_label=phase_label,
                log_context=log_context,
                state=state,
            )
        live_status = format_live_status(state)
        if log_context is not None and str(log_context).strip():
            live_status = f"{str(log_context).strip()} {live_status}"
        log_phase(log, f"{phase_label}-live", live_status)
        emit_live_telemetry_event(
            event_callback,
            state=state,
            stage=str(phase_label),
            voltage_mv=int(candidate_voltage_mv),
            clock_mhz=int(lock_clock_mhz),
            elapsed_s=progress_elapsed_s,
            target_duration_s=target_duration_s(state),
        )

    def abort_callback(state: dict) -> str | None:
        if auto_uv_stop_requested():
            return "user-stop-requested"
        fatal_output_matches = list(state.get("fatal_output_matches") or [])
        if fatal_output_matches:
            return "fatal-q2rtx-output"
        timedemo_abort = timedemo_live_abort_reason(
            state,
            frame_reference=frame_reference,
            proper_run_fps_floor=proper_run_fps_floor,
            progress_state=progress_state,
        )
        if timedemo_abort is not None:
            return timedemo_abort
        telemetry_abort = telemetry_live_abort_reason(
            state,
            busy_power_floor_w=busy_power_floor_w,
            proper_run_power_floor_w=proper_run_power_floor_w,
            target_core_clock_floor_mhz=target_floor_mhz,
            progress_state=progress_state,
        )
        if telemetry_abort is not None:
            return telemetry_abort
        return timedemo_stall_abort_reason(
            state,
            busy_power_floor_w=busy_power_floor_w,
            expected_loop_s=progress_state.get("expected_loop_s"),
            last_progress_elapsed_s=float(progress_state["last_progress_elapsed_s"]),
        )

    mark_in_progress = probe_phase_writes_crash_marker(str(phase_label))
    unsafe_clock_bindings = unsafe_clock_bindings_from_plan(
        candidate_plan,
        lock_clock_mhz=int(lock_clock_mhz),
    )
    crash_marker_details = probe_crash_marker_details(
        phase_label=str(phase_label),
        candidate_voltage_mv=int(candidate_voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        stable_history=stable_history,
        initial_target_voltage_mv=initial_target_voltage_mv,
        initial_probe_clock_mhz=initial_probe_clock_mhz,
        min_performance_core_clock_pct=min_performance_core_clock_pct,
        used_companion_load=bool(q2rtx_config.companion_command),
        expected_total_duration_s=expected_total_duration_s,
        marker_details=marker_details,
    )

    def record_probe_unsafe(reason: str, details: dict | None = None) -> None:
        if not mark_in_progress:
            return
        try:
            blacklist_path, _unsafe_entry = record_unsafe_voltage(
                candidate_voltage_mv=int(candidate_voltage_mv),
                lock_clock_mhz=int(lock_clock_mhz),
                reason=str(reason),
                phase=str(phase_label),
                details=details,
                blocked_lock_clock_mhz=unsafe_clock_bindings,
            )
        except Exception as exc:
            log_phase(
                log,
                "blacklist",
                f"failed-to-record-unsafe-voltage voltage={int(candidate_voltage_mv)}mV "
                f"target={int(lock_clock_mhz)}MHz reason={reason} error={exc}",
            )
            return
        detail_text = unsafe_detail_text(details)
        log_phase(
            log,
            "blacklist",
            f"unsafe-voltage-recorded voltage={int(candidate_voltage_mv)}mV "
            f"target={int(lock_clock_mhz)}MHz reason={reason}{detail_text} "
            f"path={blacklist_path}",
        )

    write_probe_marker_if_needed(
        mark_in_progress=mark_in_progress,
        phase_label=str(phase_label),
        candidate_voltage_mv=int(candidate_voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        log_context=log_context,
        marker_details=crash_marker_details,
        unsafe_clock_bindings=unsafe_clock_bindings,
    )
    try:
        live_voltage_before_mv = nvml_session.read_live_voltage_mv()
        log_probe_start(
            log,
            q2rtx_config=q2rtx_config,
            phase_label=str(phase_label),
            log_context=log_context,
            candidate_voltage_mv=int(candidate_voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
            live_voltage_before_mv=live_voltage_before_mv,
            show_candidate_target=bool(show_candidate_target),
        )
        if reset_plan is not None:
            apply_plan(reader, reset_plan)
            reader.refresh_points()
            log_phase(
                log,
                phase_label,
                "reset runtime-default V/F curve before applying probe curve",
            )
        apply_plan(reader, candidate_plan)
        reader.refresh_points()
        probe_config = replace(
            q2rtx_config,
            progress_callback=progress_callback,
            abort_callback=abort_callback,
        )
        try:
            result = run_q2rtx_stability_test(probe_config)
        except StabilityTestError:
            raise
        except Exception as exc:
            record_probe_unsafe(
                "stability-probe-exception",
                details={
                    "exception": f"{exc.__class__.__name__}: {exc}",
                    "used_companion_load": bool(q2rtx_config.companion_command),
                },
            )
            raise
        if str(result.reason) == "user-stop-requested":
            raise KeyboardInterrupt()
        handle_probe_result_logging_and_blacklist(
            result,
            log=log,
            record_probe_unsafe=record_probe_unsafe,
            candidate_voltage_mv=int(candidate_voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
            suppress_unsafe_recording=bool(suppress_unsafe_recording),
            suppress_unsafe_for_controlled_clock_abort=bool(
                suppress_unsafe_for_controlled_clock_abort
            ),
            used_companion_load=bool(q2rtx_config.companion_command),
        )
        live_voltage_after_mv = nvml_session.read_live_voltage_mv()
        summary_samples = (
            saturated_probe_tail_samples(result.telemetry_samples)
            if summarize_saturated_tail
            else None
        )
        if summarize_saturated_tail and summary_samples is not None:
            log_phase(
                log,
                phase_label,
                f"using saturated telemetry tail samples={len(summary_samples)}/"
                f"{len(result.telemetry_samples)} for base-load measurement",
            )
        decision_runs = decision_timedemo_runs(
            result.timedemo_runs,
            timedemo_warmup_runs=int(timedemo_warmup_runs),
        )
        if len(decision_runs) < len(result.timedemo_runs):
            log_phase(
                log,
                phase_label,
                f"timedemo-warmup-ignored="
                f"{len(result.timedemo_runs) - len(decision_runs)}/"
                f"{len(result.timedemo_runs)} runs for decision FPS",
            )
        return (
            summarize_q2rtx_cuda_probe(
                candidate_voltage_mv=candidate_voltage_mv,
                lock_clock_mhz=lock_clock_mhz,
                live_voltage_before_mv=live_voltage_before_mv,
                live_voltage_after_mv=live_voltage_after_mv,
                used_companion_load=bool(q2rtx_config.companion_command),
                power_limit_w=power_limit_w,
                result=result,
                telemetry_samples=summary_samples,
                use_power_limit_floor=use_power_limit_floor,
                timedemo_warmup_runs=int(timedemo_warmup_runs),
            ),
            result,
        )
    finally:
        if mark_in_progress:
            clear_probe_in_progress_marker()


def live_probe_reference_floors(
    reference_probe: AutoUvProbeSummary | None,
    *,
    power_limit_w: int | None,
) -> tuple[float | None, float | None, float | None]:
    proper_run_fps_floor = None
    proper_run_power_floor_w = None
    busy_power_floor_w = None
    if reference_probe is not None and reference_probe.avg_fps is not None:
        proper_run_fps_floor = float(reference_probe.avg_fps) * percent(
            AUTO_UV_METRIC_TUNING.min_proper_run_fps_pct
        )
    if reference_probe is not None and reference_probe.avg_power_w is not None:
        proper_run_power_floor_w = float(reference_probe.avg_power_w) * percent(
            AUTO_UV_METRIC_TUNING.min_proper_run_power_pct
        )
        busy_power_floor_w = float(reference_probe.avg_power_w) * percent(
            AUTO_UV_STALL_TUNING.busy_reference_power_pct
        )
    elif power_limit_w is not None and int(power_limit_w) > 0:
        busy_power_floor_w = float(power_limit_w) * percent(
            AUTO_UV_STALL_TUNING.busy_power_limit_pct
        )
    return busy_power_floor_w, proper_run_fps_floor, proper_run_power_floor_w


def handle_probe_result_logging_and_blacklist(
    result,
    *,
    log: Callable[[str], None],
    record_probe_unsafe: Callable[[str, dict | None], None],
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    suppress_unsafe_recording: bool,
    suppress_unsafe_for_controlled_clock_abort: bool,
    used_companion_load: bool,
) -> None:
    if result.success:
        print_q2rtx_stability_result(result)
        return
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
        "used_companion_load": bool(used_companion_load),
    }
    should_record_unsafe = probe_failure_should_mark_voltage_unsafe(str(result.reason))
    if suppress_unsafe_recording:
        should_record_unsafe = False
    if suppress_unsafe_for_controlled_clock_abort and final_failure_can_accept_budget_curve(
        str(result.reason)
    ):
        should_record_unsafe = False
    if should_record_unsafe:
        record_probe_unsafe("stability-probe-failed", failure_details)
    else:
        log_phase(
            log,
            "blacklist",
            f"unsafe-voltage-skipped voltage={int(candidate_voltage_mv)}mV "
            f"target={int(lock_clock_mhz)}MHz result={result.reason}",
        )
    print_q2rtx_stability_result(result)


def log_completed_timedemo_run(
    log: Callable[[str], None],
    *,
    phase_label: str,
    log_context: str | None,
    state: dict,
) -> None:
    last_run = state.get("last_run")
    if last_run is None:
        return
    latest_sample = state.get("latest_sample")
    sample_parts = []
    if latest_sample is not None and latest_sample.power_w is not None:
        sample_parts.append(f"power={float(latest_sample.power_w):.1f}W")
    if latest_sample is not None and latest_sample.temperature_c is not None:
        sample_parts.append(f"temp={float(latest_sample.temperature_c):.0f}C")
    else:
        sample_parts.append("temp=n/a")
    if latest_sample is not None and latest_sample.fan_speed_pct is not None:
        sample_parts.append(f"fan={float(latest_sample.fan_speed_pct):.0f}%")
    else:
        sample_parts.append("fan=n/a")
    prefix = (
        f"{str(log_context).strip()} "
        if log_context is not None and str(log_context).strip()
        else ""
    )
    log_phase(
        log,
        f"{phase_label}-pass",
        prefix
        + f"run={int(last_run.run_index)} "
        f"frames={int(last_run.frames)} "
        f"fps={float(last_run.fps):.1f} "
        f"seconds={float(last_run.seconds):.2f} "
        + " ".join(sample_parts),
    )


def emit_live_telemetry_event(
    event_callback: AutoUvEventCallback | None,
    *,
    state: dict,
    stage: str,
    voltage_mv: int,
    clock_mhz: int,
    elapsed_s: float,
    target_duration_s: float | None,
) -> None:
    latest_sample = state.get("latest_sample")
    if latest_sample is None:
        return
    emit_ui_json_event(
        event_callback,
        "load_telemetry",
        stage=str(stage),
        workload=str(state.get("running", "") or "q2rtx"),
        voltage_mv=int(voltage_mv),
        clock_mhz=int(clock_mhz),
        elapsed_s=round(float(elapsed_s), 2),
        target_duration_s=round_or_none(target_duration_s),
        measured_clock_mhz=round_or_none(getattr(latest_sample, "core_clock_mhz", None)),
        measured_voltage_mv=round_or_none(getattr(latest_sample, "voltage_mv", None)),
        power_w=round_or_none(getattr(latest_sample, "power_w", None)),
        temp_c=round_or_none(getattr(latest_sample, "temperature_c", None)),
        fan_pct=round_or_none(getattr(latest_sample, "fan_speed_pct", None)),
    )


def log_probe_start(
    log: Callable[[str], None],
    *,
    q2rtx_config: Q2RTXStabilityConfig,
    phase_label: str,
    log_context: str | None,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    live_voltage_before_mv: int | None,
    show_candidate_target: bool,
) -> None:
    current_sample = query_gpu_metrics(int(q2rtx_config.gpu_index))
    start_message = (
        f"live-voltage-before={live_voltage_before_mv if live_voltage_before_mv is not None else 'n/a'}"
    )
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
            f"target={lock_clock_mhz}MHz "
            + start_message
        )
    if log_context is not None and str(log_context).strip():
        start_message = f"{str(log_context).strip()} {start_message}"
    log_phase(log, phase_label, start_message)


def progress_target_duration_s(
    *,
    q2rtx_config: Q2RTXStabilityConfig,
    companion_duration_s: float,
    expected_loop_s: float | None,
    state: dict,
    expected_total_duration_s: int | float | None = None,
) -> float | None:
    if expected_total_duration_s not in (None, ""):
        try:
            return max(1.0, float(expected_total_duration_s))
        except (TypeError, ValueError):
            pass
    expected_runs = state.get("expected_runs")
    if expected_runs is None and q2rtx_config.timedemo_loops is not None:
        expected_runs = int(q2rtx_config.timedemo_loops)
    q2rtx_duration_s = (
        float(q2rtx_config.duration_s) if int(q2rtx_config.duration_s) > 0 else None
    )
    if expected_runs is not None and expected_loop_s is not None:
        q2rtx_duration_s = float(expected_runs) * float(expected_loop_s)
    if q2rtx_duration_s is None:
        return None
    return max(1.0, float(q2rtx_duration_s) + float(companion_duration_s))


def progress_elapsed_s_for_ui(state: dict) -> float:
    for key in ("progress_elapsed_s", "elapsed_s", "net_elapsed_s"):
        value = state.get(key)
        if value in (None, ""):
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return 0.0


def companion_duration_s_from_command(command: tuple[str, ...] | None) -> float:
    if not command:
        return 0.0
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == "--duration-seconds":
            try:
                return max(0.0, float(parts[index + 1]))
            except ValueError:
                return 0.0
    return 0.0


def probe_phase_writes_crash_marker(phase_label: str) -> bool:
    return str(phase_label) in {
        "candidate",
        "candidate-recovery",
        "final-recovery",
        "final-verify",
        "stabilize",
    }


def unsafe_clock_bindings_from_plan(
    plan: list[dict],
    *,
    lock_clock_mhz: int,
    lower_bins: int = UNSAFE_CLOCK_BINDING_LOWER_BINS,
) -> list[int]:
    failed_clock_mhz = int(lock_clock_mhz)
    if failed_clock_mhz <= 0:
        return []
    lower_clocks: set[int] = set()
    for point in plan:
        if not isinstance(point, dict):
            continue
        for key in ("base_mhz", "target_mhz"):
            try:
                clock_mhz = int(point[key])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < int(clock_mhz) < failed_clock_mhz:
                lower_clocks.add(int(clock_mhz))
            if key == "base_mhz":
                break
    selected_lower = sorted(lower_clocks, reverse=True)[: max(0, int(lower_bins))]
    return [failed_clock_mhz, *selected_lower]


def probe_crash_marker_details(
    *,
    phase_label: str,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    stable_history: list[AutoUvProbeSummary],
    initial_target_voltage_mv: int | None,
    initial_probe_clock_mhz: float | None,
    min_performance_core_clock_pct: float | None,
    used_companion_load: bool,
    expected_total_duration_s: int | None,
    marker_details: dict | None,
) -> dict:
    start_voltage_mv = (
        int(initial_target_voltage_mv) if initial_target_voltage_mv else None
    )
    if start_voltage_mv is None and stable_history:
        start_voltage_mv = int(stable_history[0].candidate_voltage_mv)

    details = {
        "phase_label": str(phase_label),
        "candidate_voltage_mv": int(candidate_voltage_mv),
        "lock_clock_mhz": int(lock_clock_mhz),
        "used_companion_load": bool(used_companion_load),
    }
    if start_voltage_mv is not None and int(start_voltage_mv) > 0:
        details["start_voltage_mv"] = int(start_voltage_mv)
        details["initial_target_voltage_mv"] = int(start_voltage_mv)
        details["voltage_drop_from_start_pct"] = round(
            (
                (float(start_voltage_mv) - float(candidate_voltage_mv))
                / float(start_voltage_mv)
            )
            * 100.0,
            4,
        )
    if initial_probe_clock_mhz is not None and float(initial_probe_clock_mhz) > 0.0:
        baseline_clock_mhz = float(initial_probe_clock_mhz)
        details["initial_probe_clock_mhz"] = round(baseline_clock_mhz, 4)
        details["baseline_clock_mhz"] = round(baseline_clock_mhz, 4)
        details["target_clock_pct_of_baseline"] = round(
            float(lock_clock_mhz) / baseline_clock_mhz * 100.0,
            4,
        )
    if min_performance_core_clock_pct is not None:
        details["min_performance_core_clock_pct"] = float(
            min_performance_core_clock_pct
        )
    if expected_total_duration_s is not None:
        details["expected_total_duration_s"] = int(expected_total_duration_s)
    if stable_history:
        latest_stable = stable_history[-1]
        details["previous_stable_voltage_mv"] = int(
            latest_stable.candidate_voltage_mv
        )
        details["previous_stable_lock_clock_mhz"] = int(latest_stable.lock_clock_mhz)
        if latest_stable.avg_core_clock_mhz is not None:
            details["previous_stable_avg_core_clock_mhz"] = round(
                float(latest_stable.avg_core_clock_mhz),
                4,
            )
    details.update(dict(marker_details or {}))
    return details


def write_probe_marker_if_needed(
    *,
    mark_in_progress: bool,
    phase_label: str,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    log_context: str | None,
    marker_details: dict | None,
    unsafe_clock_bindings: list[int],
) -> None:
    if not mark_in_progress:
        return
    payload_details = dict(marker_details or {})
    if unsafe_clock_bindings:
        payload_details["blocked_lock_clock_mhz"] = list(unsafe_clock_bindings)
    write_probe_in_progress_marker(
        phase=phase_label,
        candidate_voltage_mv=int(candidate_voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        log_context=log_context,
        details=payload_details,
    )


def unsafe_detail_text(details: dict | None) -> str:
    if details and details.get("result_reason"):
        return f" result={details['result_reason']}"
    if details and details.get("exception"):
        return f" exception={details['exception']}"
    return ""


def round_or_none(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)
