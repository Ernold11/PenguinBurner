"""Run the top-level voltage-frequency undervolt main loop.

This keeps phase order readable: setup, base-load probe, lower-voltage sweep,
user final choice, final verification, and cleanup.
"""

from __future__ import annotations

from typing import Callable

from stability.q2rtx import Q2RTXStabilityConfig, cleanup_managed_q2rtx_processes

from .auto_uv_types import (
    AutoUvError,
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
    VfCurveCandidate,
)
from .auto_uv_scan_settings import AutoUvScanSettings
from .auto_uv_console_log import log_benchmark, log_phase, log_user_stage
from .auto_uv_scan_result import build_voltage_scan_result
from .auto_uv_user_options import AUTO_UV_DEFAULTS, AUTO_UV_METRIC_TUNING
from .curve.base_load_flatten_target import (
    choose_base_load_flatten_target,
    selected_nvidia_light_load_diagnostic,
)
from .curve.base_load_voltage import derive_loaded_voltage_band
from .curve.base_vf_curve import editable_base_vf_points
from .curve.base_vf_curve_validation import validate_base_vf_curve
from .curve.base_vf_curve_voltage_bins import (
    lock_voltage_for_target_clock,
    nearest_editable_voltage_bin,
)
from .ui.candidate_choice import choose_final_verification_candidate
from .efficiency_tune import (
    min_search_voltage_mv,
    voltage_descent_tail_rise_bins,
)
from .gpu.gpu_vf_curve_applier import open_live_gpu_vf_curve_applier
from .persistence.interrupted_probe_crash_cache import consume_interrupted_probe_crash_marker
from .lower_voltage_sweep_loop import (
    LowerVoltageSweepHooks,
    run_lower_voltage_sweep_loop,
)
from .curve.measured_probe_lock_clock import lock_clock_from_probe_loaded_clock
from .ui.probe_summary_ui_payload import probe_summary_ui_payload
from .q2rtx.q2rtx_cuda_probe_runner import Q2RtxCudaProbeRunner
from .q2rtx.q2rtx_cuda_voltage_probe import probe_voltage_candidate
from .q2rtx.q2rtx_cuda_probe_config import reference_discovery_q2rtx_duration_s
from .scan_runtime_settings import read_scan_runtime_settings
from .scan_mode.efficiency_fps_per_w_policy import (
    derive_efficiency_stop_streak_from_fps_variance,
)
from .ui.ui_json_event_writer import AutoUvEventCallback, emit_ui_json_event
from .persistence.unsafe_voltage_blacklist_file import load_unsafe_voltage_blacklist
from .persistence.verified_candidate_result_file import write_latest_verified_candidate
from .persistence.auto_uv_persisted_json_files import clear_auto_uv_stop_request
from .curve.vf_curve_flattening import (
    build_flatten_target_for_plan,
    build_flattened_plan,
)
from .curve.performance_sweep_profile import (
    build_performance_sweep_profile_candidate,
)
from .ui.vf_curve_ui_points import vf_curve_ui_points
from .voltage_sweep_state import VoltageProbeOutcome
from .final_verification import run_final_verification_and_save
from .scan_mode import AUTO_UV_MODE_EFFICIENCY, AUTO_UV_MODE_PERFORMANCE
from saved_uv_profiles.profile_tiers import generated_profile_tier_from_runtime_options


def run_voltage_frequency_undervolt_main_loop(
    *,
    gpu_index: int,
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
    log: Callable[[str], None] = print,
    event_callback: AutoUvEventCallback | None = None,
) -> AutoUvVoltageScanResult:
    unsafe_entries = consume_crash_cache(log=log)
    gpu = open_live_gpu_vf_curve_applier(
        gpu_index=int(gpu_index),
        runtime_options=runtime_options,
        log=log,
    )
    try:
        settings = read_scan_runtime_settings(
            runtime_options,
            q2rtx_config,
            gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
        )
        q2rtx_config = settings.q2rtx_config
        timedemo_warmup_runs = int(settings.timedemo_warmup_runs)
        tail_rise_bins = int(getattr(settings, "tail_rise_bins", 0))
        descent_tail_rise_bins = int(voltage_descent_tail_rise_bins(settings))
        efficiency_tail_tune_rise_bins = efficiency_tail_tune_tail_rise_bins(
            runtime_options,
            descent_tail_rise_bins=int(descent_tail_rise_bins),
        )
        enforce_descent_clock_floor = lower_voltage_descent_enforces_clock_floor(
            runtime_options,
            tail_rise_bins=int(descent_tail_rise_bins),
        )
        cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
        base_curve = list(gpu.runtime_default_plan)
        validate_base_vf_curve(base_curve)
        emit_ui_json_event(
            event_callback,
            "base_curve",
            points=vf_curve_ui_points(base_curve),
        )
        log_phase(
            log,
            "auto-uv",
            "Auto-UV voltage-frequency main loop enabled "
            f"tail-rise-bins={int(tail_rise_bins)}",
        )
        discovery_summary, discovery_result = run_discovery_probe(
            base_curve,
            gpu=gpu,
            q2rtx_config=q2rtx_config,
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            log=log,
            event_callback=event_callback,
        )
        if not bool(getattr(discovery_result, "success", False)):
            raise AutoUvError(
                "base Defaults baseline failed the Q2RTX probe: "
                f"{getattr(discovery_result, 'reason', 'unknown')}"
            )

        baseline_candidate, baseline_target = build_loaded_baseline_candidate(
            base_curve,
            discovery_summary=discovery_summary,
            discovery_result=discovery_result,
            power_limit_w=gpu.power_limit_w,
            tail_rise_bins=int(tail_rise_bins),
        )
        gpu.start_clock_ceiling(
            build_flatten_target_for_plan(
                base_curve,
                baseline_candidate.flattened_plan,
                lock_clock_mhz=int(baseline_candidate.target_mhz),
                lock_voltage_mv=int(baseline_candidate.voltage_mv),
                tail_rise_bins=int(tail_rise_bins),
            )
        )
        runner = Q2RtxCudaProbeRunner(
            reader=gpu.reader,
            live_voltage_reader=gpu.live_voltage_reader,
            q2rtx_config=q2rtx_config,
            runtime_default_plan=gpu.runtime_default_plan,
            power_limit_w=gpu.power_limit_w,
            start_voltage_mv=int(baseline_candidate.voltage_mv),
            baseline_clock_mhz=float(baseline_target.measured_clock_mhz),
            min_performance_core_clock_pct=float(settings.min_performance_core_clock_pct),
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            log=log,
            event_callback=event_callback,
        )
        probe_history: list[AutoUvProbeSummary] = [discovery_summary]
        stable_history: list[AutoUvProbeSummary] = []
        baseline_outcome = runner.probe_baseline_candidate(baseline_candidate)
        if baseline_outcome.raw_probe is not None:
            probe_history.append(baseline_outcome.raw_probe)
        if not baseline_outcome.decision.passed:
            raise AutoUvError(
                "baseline flattened curve failed the Q2RTX probe: "
                f"{baseline_outcome.decision.reason}"
            )
        stable_probe = require_probe_summary(baseline_outcome)
        baseline_candidate = adjust_baseline_to_measured_clock(
            base_curve,
            candidate=baseline_candidate,
            stable_probe=stable_probe,
            gpu=gpu,
            tail_rise_bins=int(tail_rise_bins),
        )
        stable_history.append(stable_probe)
        write_verified_candidate(
            baseline_candidate,
            stable_probe,
            discovery_summary=discovery_summary,
            tail_rise_bins=int(tail_rise_bins),
        )
        log_user_stage(
            log,
            "Auto-UV baseline accepted",
            [
                f"Starting point: {baseline_candidate.target_mhz}MHz at {baseline_candidate.voltage_mv}mV.",
                "Next, Auto-UV will walk downward through real editable voltage bins.",
            ],
        )
        efficiency_stop_streak_default = derive_efficiency_stop_streak_from_fps_variance(
            stable_probe,
            configured_streak=int(
                getattr(
                    settings,
                    "efficiency_stop_streak",
                    AUTO_UV_DEFAULTS.efficiency_stop_streak,
                )
            ),
            derive=bool(getattr(settings, "derive_efficiency_stop_streak", True)),
            high_variance_threshold_pct=float(
                AUTO_UV_METRIC_TUNING.efficiency_stop_high_fps_variance_pct
            ),
            low_variance_streak=int(
                AUTO_UV_METRIC_TUNING.efficiency_stop_low_variance_streak
            ),
            high_variance_streak=int(
                AUTO_UV_METRIC_TUNING.efficiency_stop_high_variance_streak
            ),
        )
        emit_ui_json_event(
            event_callback,
            "derived_defaults",
            efficiency_stop_streak=int(efficiency_stop_streak_default.value),
            efficiency_stop_streak_source=str(efficiency_stop_streak_default.source),
            fps_variance_pct=efficiency_stop_streak_default.fps_variance_pct,
            fps_variance_threshold_pct=float(
                efficiency_stop_streak_default.threshold_pct
            ),
        )
        log_phase(
            log,
            "efficiency",
            "stop-streak="
            f"{int(efficiency_stop_streak_default.value)} "
            f"source={efficiency_stop_streak_default.source} "
            "fps_variance_pct="
            f"{_format_optional_pct(efficiency_stop_streak_default.fps_variance_pct)} "
            f"threshold={float(efficiency_stop_streak_default.threshold_pct):.2f}%",
        )

        stable_candidate = baseline_candidate
        effective_min_search_voltage_mv = min_search_voltage_mv(
            start_voltage_mv=int(baseline_candidate.voltage_mv),
            configured_min_voltage_mv=settings.configured_min_voltage_mv,
            configured_max_drop_pct=float(settings.configured_max_drop_pct),
            gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
        )

        def probe_candidate(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
            retarget_clock_ceiling_for_candidate(gpu.clock_ceiling, candidate)
            probe_kwargs = {
                "stable_history": stable_history,
                "phase_label": "candidate",
            }
            if not bool(enforce_descent_clock_floor):
                probe_kwargs["enforce_target_core_clock_floor"] = False
            outcome = runner.probe_sweep_candidate(candidate, **probe_kwargs)
            if outcome.raw_probe is not None:
                probe_history.append(outcome.raw_probe)
            return outcome

        def accept_candidate(
            candidate: VfCurveCandidate,
            outcome: VoltageProbeOutcome,
        ) -> None:
            nonlocal stable_candidate, stable_probe
            summary = require_probe_summary(outcome)
            stable_candidate = candidate
            stable_probe = summary
            stable_history.append(summary)
            write_verified_candidate(
                candidate,
                summary,
                discovery_summary=discovery_summary,
                tail_rise_bins=int(descent_tail_rise_bins),
            )

        def record_passed_candidate(
            candidate: VfCurveCandidate,
            outcome: VoltageProbeOutcome,
        ) -> None:
            summary = require_probe_summary(outcome)
            stable_history.append(summary)

        hooks = LowerVoltageSweepHooks(
            probe_candidate=probe_candidate,
            write_verified_candidate=accept_candidate,
            mark_unsafe_candidate=lambda _candidate, _outcome: None,
            record_passed_candidate=record_passed_candidate,
        )
        user_stop_final_choice = False
        try:
            loop_result = run_lower_voltage_sweep_loop(
                base_curve,
                settings=AutoUvScanSettings(
                    start_voltage_mv=int(baseline_candidate.voltage_mv),
                    min_search_voltage_mv=int(effective_min_search_voltage_mv),
                    preserve_base_below_mv=settings.preserve_base_below_mv,
                    baseline_core_clock_mhz=float(baseline_target.measured_clock_mhz),
                    auto_uv_mode=settings.auto_uv_mode,
                    min_core_clock_pct=float(settings.min_performance_core_clock_pct),
                    reference_actual_voltage_mv=stable_probe.avg_voltage_mv,
                    efficiency_stop_streak=int(efficiency_stop_streak_default.value),
                    min_efficiency_stop_voltage_drop_pct=float(
                        getattr(
                            settings,
                            "min_efficiency_stop_voltage_drop_pct",
                            10.0,
                        )
                    ),
                    tail_rise_bins=int(descent_tail_rise_bins),
                ),
                initial_stable_candidate=stable_candidate,
                hooks=hooks,
                unsafe_entries=unsafe_entries,
                initial_stable_outcome=VoltageProbeOutcome(
                    decision=baseline_outcome.decision,
                    measured_core_clock_mhz=stable_probe.avg_core_clock_mhz,
                    measured_voltage_mv=stable_probe.avg_voltage_mv,
                    raw_probe=stable_probe,
                    raw_result=baseline_outcome.raw_result,
                ),
            )
            stable_candidate = loop_result.stable_candidate
            if (
                settings.auto_uv_mode == AUTO_UV_MODE_EFFICIENCY
                and int(stable_candidate.voltage_mv)
                > int(effective_min_search_voltage_mv)
            ):
                log_user_stage(
                    log,
                    "Auto-UV efficiency tail tune",
                    [
                        (
                            "Continuing toward the card minimum voltage with "
                            f"{int(efficiency_tail_tune_rise_bins)} tail-rise bins."
                        ),
                        f"Keeping target clock: {int(stable_candidate.target_mhz)}MHz.",
                    ],
                )
                post_loop_result = run_lower_voltage_sweep_loop(
                    base_curve,
                    settings=AutoUvScanSettings(
                        start_voltage_mv=int(baseline_candidate.voltage_mv),
                        min_search_voltage_mv=int(effective_min_search_voltage_mv),
                        preserve_base_below_mv=settings.preserve_base_below_mv,
                        baseline_core_clock_mhz=float(
                            baseline_target.measured_clock_mhz
                        ),
                        auto_uv_mode="efficiency-tail-tune",
                        min_core_clock_pct=float(
                            settings.min_performance_core_clock_pct
                        ),
                        reference_actual_voltage_mv=stable_probe.avg_voltage_mv,
                        efficiency_stop_streak=0,
                        min_efficiency_stop_voltage_drop_pct=0.0,
                        tail_rise_bins=int(efficiency_tail_tune_rise_bins),
                    ),
                    initial_stable_candidate=stable_candidate,
                    hooks=hooks,
                    unsafe_entries=unsafe_entries,
                    initial_stable_outcome=VoltageProbeOutcome(
                        decision=baseline_outcome.decision,
                        measured_core_clock_mhz=stable_probe.avg_core_clock_mhz,
                        measured_voltage_mv=stable_probe.avg_voltage_mv,
                        raw_probe=stable_probe,
                        raw_result=baseline_outcome.raw_result,
                    ),
                )
                stable_candidate = post_loop_result.stable_candidate
        except KeyboardInterrupt:
            if not bool(runtime_options.get("auto_uv_require_final_choice")):
                raise
            user_stop_final_choice = True
            clear_auto_uv_stop_request()
            log_phase(
                log,
                "auto-uv",
                "user stop requested; offering past stable candidates for final verification",
            )
        final_verification_duration_s = int(settings.final_verification_duration_s)
        final_stable_plan = stable_candidate.flattened_plan
        final_stable_voltage_mv = int(stable_candidate.voltage_mv)
        final_stable_lock_clock_mhz = int(stable_candidate.target_mhz)
        final_stable_probe = stable_probe
        final_tail_rise_bins = int(
            stable_candidate.metadata.get("tail_rise_bins", descent_tail_rise_bins)
        )
        if bool(runtime_options.get("auto_uv_require_final_choice")) and bool(
            user_stop_final_choice
        ):
            (
                final_stable_plan,
                final_stable_voltage_mv,
                final_stable_lock_clock_mhz,
                selected_stable_probe,
                selected_final_verification_duration_s,
            ) = choose_final_verification_candidate(
                log=log,
                event_callback=event_callback,
                auto_uv_mode=settings.auto_uv_mode,
                base_probe=discovery_summary,
                stable_plan=final_stable_plan,
                stable_voltage_mv=int(final_stable_voltage_mv),
                stable_lock_clock_mhz=int(final_stable_lock_clock_mhz),
                stable_probe=final_stable_probe,
                stable_history=stable_history,
                base_curve=base_curve,
                final_verification_duration_s=int(final_verification_duration_s),
                initial_target_voltage_mv=int(baseline_candidate.voltage_mv),
                short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
                tail_rise_bins=int(final_tail_rise_bins),
                request_reason=(
                    "user-stop" if bool(user_stop_final_choice) else "sweep-complete"
                ),
            )
            final_verification_duration_s = int(selected_final_verification_duration_s)
            if selected_stable_probe is not None:
                final_stable_probe = selected_stable_probe

        if not bool(user_stop_final_choice):
            (
                final_stable_plan,
                final_stable_voltage_mv,
                final_stable_lock_clock_mhz,
                final_stable_probe,
            ) = select_performance_auto_oc_candidate(
                base_curve,
                auto_uv_mode=settings.auto_uv_mode,
                stable_plan=final_stable_plan,
                stable_voltage_mv=int(final_stable_voltage_mv),
                stable_lock_clock_mhz=int(final_stable_lock_clock_mhz),
                stable_probe=final_stable_probe,
                stable_history=stable_history,
                runner=runner,
                gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
                clock_ceiling=gpu.clock_ceiling,
                probe_history=probe_history,
                log=log,
                tail_rise_bins=int(final_tail_rise_bins),
                target_voltage_mv=positive_int(
                    runtime_options.get("auto_oc_target_voltage_mv")
                ),
                target_clock_mhz=positive_int(
                    runtime_options.get("auto_oc_target_clock_mhz")
                ),
            )

        if bool(runtime_options.get("auto_uv_require_final_choice")) and not bool(
            user_stop_final_choice
        ):
            (
                final_stable_plan,
                final_stable_voltage_mv,
                final_stable_lock_clock_mhz,
                selected_stable_probe,
                selected_final_verification_duration_s,
            ) = choose_final_verification_candidate(
                log=log,
                event_callback=event_callback,
                auto_uv_mode=settings.auto_uv_mode,
                base_probe=discovery_summary,
                stable_plan=final_stable_plan,
                stable_voltage_mv=int(final_stable_voltage_mv),
                stable_lock_clock_mhz=int(final_stable_lock_clock_mhz),
                stable_probe=final_stable_probe,
                stable_history=stable_history,
                base_curve=base_curve,
                final_verification_duration_s=int(final_verification_duration_s),
                initial_target_voltage_mv=int(baseline_candidate.voltage_mv),
                short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
                tail_rise_bins=int(final_tail_rise_bins),
                request_reason="sweep-complete",
            )
            final_verification_duration_s = int(selected_final_verification_duration_s)
            if selected_stable_probe is not None:
                final_stable_probe = selected_stable_probe

        return run_final_verification_and_save(
            probe_voltage_candidate=probe_voltage_candidate,
            build_voltage_scan_result=build_voltage_scan_result,
            log=log,
            reader=gpu.reader,
            stable_plan=final_stable_plan,
            stable_voltage_mv=int(final_stable_voltage_mv),
            stable_lock_clock_mhz=int(final_stable_lock_clock_mhz),
            stable_probe=final_stable_probe,
            stable_history=stable_history,
            probe_history=probe_history,
            q2rtx_config=q2rtx_config,
            final_verification_duration_s=int(final_verification_duration_s),
            start_voltage_mv=int(baseline_candidate.voltage_mv),
            measured_clock_mhz=float(baseline_target.measured_clock_mhz),
            nvml_session=gpu.live_voltage_reader,
            clock_ceiling=gpu.clock_ceiling,
            discovery_summary=discovery_summary,
            translated_gpu_policy=gpu.translated_gpu_policy,
            min_performance_core_clock_pct=float(settings.min_performance_core_clock_pct),
            runtime_default_plan=gpu.runtime_default_plan,
            final_clock_drop_margin_pct=float(settings.final_clock_drop_margin_pct),
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            tail_rise_bins=int(final_tail_rise_bins),
            auto_uv_mode=str(settings.auto_uv_mode),
            generated_profile_tier=generated_profile_tier_from_runtime_options(
                runtime_options
            ),
            event_callback=event_callback,
        )
    finally:
        cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
        gpu.close()


def consume_crash_cache(*, log: Callable[[str], None]) -> list[dict]:
    interrupted = consume_interrupted_probe_crash_marker()
    if interrupted is not None:
        _path, unsafe_entry = interrupted
        log_phase(
            log,
            "crash-cache",
            "previous auto-UV probe ended abruptly; "
            f"blacklisted={int(unsafe_entry['candidate_voltage_mv'])}mV "
            f"target={int(unsafe_entry['lock_clock_mhz'])}MHz",
        )
    return load_unsafe_voltage_blacklist()


def retarget_clock_ceiling_for_candidate(
    clock_ceiling,
    candidate: VfCurveCandidate,
) -> None:
    if clock_ceiling is None:
        return
    clock_ceiling.retarget(
        lock_clock_mhz=int(candidate.target_mhz),
        lock_voltage_mv=int(candidate.voltage_mv),
        ceiling_clock_mhz=tail_ceiling_for_plan(
            candidate.flattened_plan,
            lock_clock_mhz=int(candidate.target_mhz),
            lock_voltage_mv=int(candidate.voltage_mv),
        ),
    )


def run_auto_oc_candidate_search(**kwargs):
    from auto_oc import run_auto_oc_candidate_search as run_search

    return run_search(**kwargs)


def select_performance_auto_oc_candidate(
    base_curve: list[dict],
    *,
    auto_uv_mode: str,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    stable_history: list[AutoUvProbeSummary] | None,
    runner: Q2RtxCudaProbeRunner,
    gpu_name: object | None,
    clock_ceiling,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tail_rise_bins: int = 0,
    target_voltage_mv: int | None = None,
    target_clock_mhz: int | None = None,
) -> tuple[list[dict], int, int, AutoUvProbeSummary | None]:
    if str(auto_uv_mode) != AUTO_UV_MODE_PERFORMANCE:
        return (
            stable_plan,
            int(stable_voltage_mv),
            int(stable_lock_clock_mhz),
            stable_probe,
        )
    start_candidate = VfCurveCandidate(
        label="performance-auto-oc-start",
        voltage_mv=int(stable_voltage_mv),
        target_mhz=int(stable_lock_clock_mhz),
        flattened_plan=stable_plan,
    )
    result = run_auto_oc_candidate_search(
        base_curve=base_curve,
        start_candidate=start_candidate,
        start_probe=stable_probe,
        runner=runner,
        gpu_name=gpu_name,
        clock_ceiling=clock_ceiling,
        probe_history=probe_history,
        log=log,
        tail_rise_bins=int(tail_rise_bins),
        target_voltage_mv=target_voltage_mv,
        target_clock_mhz=target_clock_mhz,
    )
    if stable_history is not None:
        for attempt in getattr(result, "attempts", ()) or ():
            if (
                attempt.outcome.decision.passed
                and attempt.outcome.raw_probe is not None
            ):
                stable_history.append(attempt.outcome.raw_probe)
    selected = build_performance_sweep_profile_candidate(
        base_curve,
        selected_candidate=result.selected_candidate,
        stable_history=stable_history,
        auto_oc_attempts=getattr(result, "attempts", ()) or (),
    )
    if selected.metadata.get("profile_curve") == "performance-sweep":
        log_phase(
            log,
            "auto-oc",
            "profile-curve=performance-sweep "
            f"start={int(selected.metadata['profile_curve_start_voltage_mv'])}mV "
            f"anchors={int(selected.metadata['profile_curve_anchor_count'])}",
        )
    selected_changed = (
        int(selected.voltage_mv) != int(stable_voltage_mv)
        or int(selected.target_mhz) != int(stable_lock_clock_mhz)
    )
    return (
        selected.flattened_plan,
        int(selected.voltage_mv),
        int(selected.target_mhz),
        None if selected_changed else stable_probe,
    )


def positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def efficiency_tail_tune_tail_rise_bins(
    runtime_options: dict,
    *,
    descent_tail_rise_bins: int,
) -> int:
    if bool(runtime_options.get("auto_uv_tail_rise_bins_explicit")):
        return max(0, int(descent_tail_rise_bins))
    return int(AUTO_UV_DEFAULTS.balanced_tail_rise_bins)


def lower_voltage_descent_enforces_clock_floor(
    runtime_options: dict,
    *,
    tail_rise_bins: int,
) -> bool:
    if (
        bool(runtime_options.get("auto_uv_tail_rise_bins_explicit"))
        and bool(runtime_options.get("auto_uv_min_voltage_mv_explicit"))
        and int(tail_rise_bins) == 0
    ):
        return False
    return True


def run_discovery_probe(
    base_curve: list[dict],
    *,
    gpu,
    q2rtx_config: Q2RTXStabilityConfig,
    short_probe_base_duration_s: int,
    timedemo_warmup_runs: int,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
) -> tuple[AutoUvProbeSummary, object]:
    point = max(editable_base_vf_points(base_curve), key=lambda item: item.target_mhz)
    runner = Q2RtxCudaProbeRunner(
        reader=gpu.reader,
        live_voltage_reader=gpu.live_voltage_reader,
        q2rtx_config=q2rtx_config,
        runtime_default_plan=gpu.runtime_default_plan,
        power_limit_w=gpu.power_limit_w,
        start_voltage_mv=int(point.voltage_mv),
        baseline_clock_mhz=None,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        timedemo_warmup_runs=int(timedemo_warmup_runs),
        log=log,
        event_callback=event_callback,
    )
    emit_ui_json_event(
        event_callback,
        "probe_start",
        stage="base-baseline",
        voltage_mv=int(point.voltage_mv),
        clock_mhz=int(point.target_mhz),
        label="base default curve",
        elapsed_s=0.0,
        target_duration_s=reference_discovery_q2rtx_duration_s(
            int(short_probe_base_duration_s)
        ),
    )
    summary, result = runner.probe_default_curve(
        base_curve=base_curve,
        label_voltage_mv=int(point.voltage_mv),
        label_clock_mhz=int(point.target_mhz),
    )
    emit_ui_json_event(
        event_callback,
        "probe_result",
        **probe_summary_ui_payload(
            summary,
            stage="base-baseline",
            decision="pass" if getattr(result, "success", False) else "fail",
            reason=str(getattr(result, "reason", "")),
        ),
    )
    log_benchmark(log, phase="discover", probe=summary)
    light_load_diagnostic = selected_nvidia_light_load_diagnostic(
        list(getattr(result, "telemetry_samples", []) or []),
        power_limit_w=gpu.power_limit_w,
    )
    if light_load_diagnostic is not None:
        log_phase(log, "discover", light_load_diagnostic)
    return summary, result


def build_loaded_baseline_candidate(
    base_curve: list[dict],
    *,
    discovery_summary: AutoUvProbeSummary,
    discovery_result: object,
    power_limit_w: int | None,
    tail_rise_bins: int = 0,
) -> tuple[VfCurveCandidate, object]:
    target = choose_base_load_flatten_target(
        base_curve,
        list(getattr(discovery_result, "telemetry_samples", []) or []),
        power_limit_w=power_limit_w,
        fallback_clock_mhz=discovery_summary.avg_core_clock_mhz,
    )
    voltage_band = derive_loaded_voltage_band(
        list(getattr(discovery_result, "telemetry_samples", []) or []),
        power_limit_w=power_limit_w,
        use_power_limit_floor=True,
    )
    fallback_voltage_mv = lock_voltage_for_target_clock(
        base_curve,
        int(target.target_clock_mhz),
    )
    start_voltage_mv = nearest_editable_voltage_bin(
        base_curve,
        int(voltage_band.average_mv or fallback_voltage_mv),
    )
    plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(target.target_clock_mhz),
        candidate_voltage_mv=int(start_voltage_mv),
        tail_rise_bins=int(tail_rise_bins),
    )
    return (
        VfCurveCandidate(
            label="baseline-loaded-flattened-curve",
            voltage_mv=int(start_voltage_mv),
            target_mhz=int(target.target_clock_mhz),
            flattened_plan=plan,
        ),
        target,
    )


def adjust_baseline_to_measured_clock(
    base_curve: list[dict],
    *,
    candidate: VfCurveCandidate,
    stable_probe: AutoUvProbeSummary,
    gpu,
    tail_rise_bins: int = 0,
) -> VfCurveCandidate:
    measured_target_mhz = lock_clock_from_probe_loaded_clock(
        base_curve,
        probe=stable_probe,
        previous_lock_clock_mhz=int(candidate.target_mhz),
    )
    if int(measured_target_mhz) == int(candidate.target_mhz):
        return candidate
    plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(measured_target_mhz),
        candidate_voltage_mv=int(candidate.voltage_mv),
        tail_rise_bins=int(tail_rise_bins),
    )
    if gpu.clock_ceiling is not None:
        gpu.clock_ceiling.retarget(
            lock_clock_mhz=int(measured_target_mhz),
            lock_voltage_mv=int(candidate.voltage_mv),
            ceiling_clock_mhz=tail_ceiling_for_plan(
                plan,
                lock_clock_mhz=int(measured_target_mhz),
                lock_voltage_mv=int(candidate.voltage_mv),
            ),
        )
    return VfCurveCandidate(
        label="baseline-measured-clock-adjusted",
        voltage_mv=int(candidate.voltage_mv),
        target_mhz=int(measured_target_mhz),
        flattened_plan=plan,
    )


def write_verified_candidate(
    candidate: VfCurveCandidate,
    probe: AutoUvProbeSummary,
    *,
    discovery_summary: AutoUvProbeSummary,
    tail_rise_bins: int = 0,
) -> None:
    effective_tail_rise_bins = int(
        candidate.metadata.get("tail_rise_bins", tail_rise_bins)
    )
    write_latest_verified_candidate(
        plan=candidate.flattened_plan,
        lock_clock_mhz=int(candidate.target_mhz),
        voltage_mv=int(candidate.voltage_mv),
        probe=probe,
        base_probe=discovery_summary,
        tail_rise_bins=int(effective_tail_rise_bins),
    )


def require_probe_summary(outcome: VoltageProbeOutcome) -> AutoUvProbeSummary:
    if outcome.raw_probe is None:
        raise AutoUvError("Auto-UV probe outcome did not include a probe summary")
    return outcome.raw_probe


def _format_optional_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f}%"


def tail_ceiling_for_plan(
    plan: list[dict],
    *,
    lock_clock_mhz: int,
    lock_voltage_mv: int,
) -> int:
    target = build_flatten_target_for_plan(
        plan,
        plan,
        lock_clock_mhz=int(lock_clock_mhz),
        lock_voltage_mv=int(lock_voltage_mv),
    )
    return int(target.get("ceiling_clock_mhz", target["lock_clock_mhz"]))
