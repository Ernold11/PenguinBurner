"""Run the top-level voltage-frequency undervolt main loop.

This keeps phase order readable: setup, base-load probe, lower-voltage sweep,
user final choice, final verification, and cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

from auto_uv.stability.q2rtx import Q2RTXStabilityConfig, cleanup_managed_q2rtx_processes

from auto_uv.domain.types import (
    AutoUvError,
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
    VfCurveCandidate,
)
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.console_log import log_phase, log_user_stage
from auto_uv.domain.scan_result import build_voltage_scan_result
from auto_uv.domain.user_options import AUTO_UV_DEFAULTS, AUTO_UV_METRIC_TUNING
from auto_uv.shared.positive_int import positive_int
from auto_uv.run.baseline_probe import (
    adjust_baseline_to_measured_clock,
    build_loaded_baseline_candidate,
    lower_voltage_descent_enforces_clock_floor,
    require_probe_summary,
    retarget_clock_ceiling_for_candidate,
    run_discovery_probe,
    tail_ceiling_for_plan,
    write_verified_candidate,
)
from auto_uv.run.crash_recovery import (
    _float_or_none,
    append_unique_probe_summary,
    auto_uv_run_marker_details,
    auto_uv_run_profile_tier,
    base_probe_summary_from_candidate_record,
    consume_crash_cache,
    crash_recovery_decision,
    crash_recovery_entry_from_cache,
    crash_recovery_entry_profile_tier,
    next_safer_recovery_candidate_id,
    probe_summary_from_candidate_record,
    recovery_candidate_records_for_failed_run,
    recovery_initial_target_voltage_mv,
    replay_recovered_resume_probe_rows,
)
from auto_uv.curve.base_vf_curve_validation import validate_base_vf_curve
from auto_uv.ui.candidate_choice import (
    choose_final_verification_candidate,
    choose_recovery_final_verification_candidate,
)
from auto_uv.efficiency_tune import (
    min_search_voltage_mv,
    voltage_descent_tail_rise_bins,
)
from auto_uv.gpu.gpu_vf_curve_applier import open_live_gpu_vf_curve_applier
from auto_uv.persistence.interrupted_probe_crash_cache import consume_interrupted_probe_crash_marker
from auto_uv.run.lower_voltage_sweep_loop import (
    LowerVoltageSweepHooks,
    run_lower_voltage_sweep_loop,
)
from auto_uv.q2rtx.q2rtx_cuda_probe_runner import Q2RtxCudaProbeRunner
from auto_uv.q2rtx.q2rtx_cuda_voltage_probe import probe_voltage_candidate
from auto_uv.run.scan_runtime_settings import read_scan_runtime_settings
from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    derive_efficiency_stop_streak_from_fps_variance,
)
from auto_uv.ui.ui_json_event_writer import AutoUvEventCallback, emit_ui_json_event
from auto_uv.persistence.verified_candidate_result_file import (
    read_verified_candidates,
)
from auto_uv.persistence.auto_uv_persisted_json_files import clear_auto_uv_stop_request
from auto_uv.curve.vf_curve_flattening import build_flatten_target_for_plan
from auto_uv.run.performance_auto_oc_selection import (
    performance_auto_oc_progress_metadata,
    run_auto_oc_candidate_search,
    select_performance_auto_oc_candidate,
)
from auto_uv.ui.vf_curve_ui_points import vf_curve_ui_points
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from auto_uv.final_verification import run_final_verification_and_save
from auto_uv.scan_mode import AUTO_UV_MODE_EFFICIENCY


@dataclass(frozen=True, slots=True)
class FinalScanCandidate:
    plan: list[dict]
    voltage_mv: int
    lock_clock_mhz: int
    probe: AutoUvProbeSummary | None
    verification_duration_s: int
    auto_oc_metadata: dict


def run_voltage_frequency_undervolt_main_loop(
    *,
    gpu_index: int,
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
    log: Callable[[str], None] = print,
    event_callback: AutoUvEventCallback | None = None,
) -> AutoUvVoltageScanResult:
    unsafe_entries = consume_crash_cache(log=log)
    crash_recovery_entry = crash_recovery_entry_from_cache(unsafe_entries)
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
        final_verification_duration_s = int(settings.final_verification_duration_s)
        tail_rise_bins = int(getattr(settings, "tail_rise_bins", 0))
        run_profile_tier = auto_uv_run_profile_tier(
            runtime_options,
            settings,
            tail_rise_bins=int(tail_rise_bins),
        )
        descent_tail_rise_bins = int(voltage_descent_tail_rise_bins(settings))
        # After the first descent stops at the natural clock floor, the tail-tune
        # pass raises the tail by two more bins. The extra tail gives the GPU the
        # vdroop headroom to hold the floor clock at lower voltage, letting the
        # sweep push down toward the card minimum instead of stopping at the floor.
        efficiency_tail_tune_rise_bins = descent_tail_rise_bins + 2
        enforce_descent_clock_floor = lower_voltage_descent_enforces_clock_floor(
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
        pending_recovery_selection = None
        if bool(runtime_options.get("auto_uv_require_final_choice")) and isinstance(
            crash_recovery_entry,
            dict,
        ):
            recovery_candidates = recovery_candidate_records_for_failed_run(
                read_verified_candidates(),
                crash_recovery_entry=crash_recovery_entry,
                target_profile_tier=run_profile_tier,
            )
            recovery_default_id = next_safer_recovery_candidate_id(
                recovery_candidates,
                failed_voltage_mv=positive_int(
                    crash_recovery_entry.get("candidate_voltage_mv")
                ),
                auto_uv_mode=settings.auto_uv_mode,
            )
            if recovery_default_id:
                recovery_decision = crash_recovery_decision(crash_recovery_entry)
                log_phase(
                    log,
                    "crash-recovery",
                    "offering saved candidates before discovery "
                    f"failed={recovery_decision.get('candidate_voltage_mv')}mV@"
                    f"{recovery_decision.get('lock_clock_mhz')}MHz "
                    f"tier={run_profile_tier or 'unknown'} "
                    f"default={recovery_default_id} "
                    f"decision={recovery_decision.get('decision')}",
                )
                pending_recovery_selection = (
                    choose_recovery_final_verification_candidate(
                        log=log,
                        event_callback=event_callback,
                        auto_uv_mode=settings.auto_uv_mode,
                        base_probe=None,
                        candidate_records=recovery_candidates,
                        default_candidate_id=recovery_default_id,
                        final_verification_duration_s=int(
                            final_verification_duration_s
                        ),
                        initial_target_voltage_mv=recovery_initial_target_voltage_mv(
                            recovery_candidates,
                            fallback_voltage_mv=positive_int(
                                crash_recovery_entry.get("candidate_voltage_mv")
                            ),
                        ),
                        short_probe_base_duration_s=int(
                            settings.short_probe_base_duration_s
                        ),
                        recovery_decision=recovery_decision,
                    )
                )
                if pending_recovery_selection is None:
                    log_phase(
                        log,
                        "crash-recovery",
                        "user chose to start a new scan from scratch",
                    )
        log_phase(
            log,
            "auto-uv",
            "Auto-UV voltage-frequency main loop enabled "
            f"tail-rise-bins={int(tail_rise_bins)}",
        )
        if pending_recovery_selection is not None:
            return run_recovered_previous_crash_selection(
                pending_recovery_selection=pending_recovery_selection,
                base_curve=base_curve,
                gpu=gpu,
                settings=settings,
                q2rtx_config=q2rtx_config,
                runtime_options=runtime_options,
                final_verification_duration_s=int(final_verification_duration_s),
                tail_rise_bins=int(tail_rise_bins),
                log=log,
                event_callback=event_callback,
            )
        discovery_summary, discovery_result = run_discovery_probe(
            base_curve,
            gpu=gpu,
            q2rtx_config=q2rtx_config,
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            log=log,
            event_callback=event_callback,
            marker_details=auto_uv_run_marker_details(
                runtime_options,
                settings,
                tail_rise_bins=int(tail_rise_bins),
                profile_tier=run_profile_tier,
            ),
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
            log=log,
            event_callback=event_callback,
            marker_details=auto_uv_run_marker_details(
                runtime_options,
                settings,
                tail_rise_bins=int(tail_rise_bins),
                profile_tier=run_profile_tier,
            ),
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
        resumed_previous_crash = False

        def finish_with_final_verification(
            *,
            final_stable_plan: list[dict],
            final_stable_voltage_mv: int,
            final_stable_lock_clock_mhz: int,
            final_stable_probe: AutoUvProbeSummary | None,
            selected_final_verification_duration_s: int,
            final_tail_rise_bins: int,
            final_auto_oc_metadata: dict | None = None,
        ):
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
                final_verification_duration_s=int(
                    selected_final_verification_duration_s
                ),
                start_voltage_mv=int(baseline_candidate.voltage_mv),
                measured_clock_mhz=float(baseline_target.measured_clock_mhz),
                nvml_session=gpu.live_voltage_reader,
                clock_ceiling=gpu.clock_ceiling,
                discovery_summary=discovery_summary,
                translated_gpu_policy=gpu.translated_gpu_policy,
                min_performance_core_clock_pct=float(
                    settings.min_performance_core_clock_pct
                ),
                runtime_default_plan=gpu.runtime_default_plan,
                final_clock_drop_margin_pct=float(settings.final_clock_drop_margin_pct),
                tail_rise_bins=int(final_tail_rise_bins),
                auto_uv_mode=str(settings.auto_uv_mode),
                generated_profile_tier=run_profile_tier,
                auto_oc_metadata=dict(final_auto_oc_metadata or {}),
                event_callback=event_callback,
            )

        user_stop_final_choice = False
        try:
            if not bool(resumed_previous_crash):
                loop_result = run_lower_voltage_sweep_loop(
                    base_curve,
                    settings=AutoUvScanSettings(
                        start_voltage_mv=int(baseline_candidate.voltage_mv),
                        min_search_voltage_mv=int(effective_min_search_voltage_mv),
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
                            descend_through_low_clock=True,
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
        final_tail_rise_bins = int(
            stable_candidate.metadata.get("tail_rise_bins", descent_tail_rise_bins)
        )
        final_selection = select_final_scan_candidate(
            base_curve=base_curve,
            settings=settings,
            runtime_options=runtime_options,
            stable_plan=stable_candidate.flattened_plan,
            stable_voltage_mv=int(stable_candidate.voltage_mv),
            stable_lock_clock_mhz=int(stable_candidate.target_mhz),
            stable_probe=stable_probe,
            stable_history=stable_history,
            runner=runner,
            gpu=gpu,
            probe_history=probe_history,
            log=log,
            tail_rise_bins=int(final_tail_rise_bins),
            measured_baseline_clock_mhz=float(baseline_target.measured_clock_mhz),
            discovery_summary=discovery_summary,
            baseline_candidate=baseline_candidate,
            final_verification_duration_s=int(final_verification_duration_s),
            event_callback=event_callback,
            run_performance_auto_oc=not bool(user_stop_final_choice),
            request_reason=(
                "user-stop" if bool(user_stop_final_choice) else "sweep-complete"
            ),
        )

        return finish_with_final_verification(
            final_stable_plan=final_selection.plan,
            final_stable_voltage_mv=int(final_selection.voltage_mv),
            final_stable_lock_clock_mhz=int(final_selection.lock_clock_mhz),
            final_stable_probe=final_selection.probe,
            selected_final_verification_duration_s=int(
                final_selection.verification_duration_s
            ),
            final_tail_rise_bins=int(final_tail_rise_bins),
            final_auto_oc_metadata=final_selection.auto_oc_metadata,
        )
    finally:
        cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
        gpu.close()


def select_final_scan_candidate(
    *,
    base_curve: list[dict],
    settings,
    runtime_options: dict,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    stable_history: list[AutoUvProbeSummary],
    runner,
    gpu,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tail_rise_bins: int,
    measured_baseline_clock_mhz: float,
    discovery_summary: AutoUvProbeSummary,
    baseline_candidate: VfCurveCandidate,
    final_verification_duration_s: int,
    event_callback: AutoUvEventCallback | None,
    run_performance_auto_oc: bool,
    request_reason: str,
) -> FinalScanCandidate:
    final_plan = stable_plan
    final_voltage_mv = int(stable_voltage_mv)
    final_lock_clock_mhz = int(stable_lock_clock_mhz)
    final_probe = stable_probe
    final_auto_oc_metadata: dict = {}

    if bool(run_performance_auto_oc):
        (
            final_plan,
            final_voltage_mv,
            final_lock_clock_mhz,
            final_probe,
            final_auto_oc_metadata,
        ) = select_performance_auto_oc_candidate(
            base_curve,
            auto_uv_mode=settings.auto_uv_mode,
            stable_plan=final_plan,
            stable_voltage_mv=int(final_voltage_mv),
            stable_lock_clock_mhz=int(final_lock_clock_mhz),
            stable_probe=final_probe,
            stable_history=stable_history,
            runner=runner,
            gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
            clock_ceiling=gpu.clock_ceiling,
            probe_history=probe_history,
            log=log,
            tail_rise_bins=int(tail_rise_bins),
            target_voltage_mv=positive_int(
                runtime_options.get("auto_oc_target_voltage_mv")
            ),
            target_clock_mhz=positive_int(
                runtime_options.get("auto_oc_target_clock_mhz")
            ),
            measured_baseline_clock_mhz=float(measured_baseline_clock_mhz),
        )

    if bool(runtime_options.get("auto_uv_require_final_choice")):
        (
            final_plan,
            final_voltage_mv,
            final_lock_clock_mhz,
            selected_stable_probe,
            selected_final_verification_duration_s,
        ) = choose_final_verification_candidate(
            log=log,
            event_callback=event_callback,
            auto_uv_mode=settings.auto_uv_mode,
            base_probe=discovery_summary,
            stable_plan=final_plan,
            stable_voltage_mv=int(final_voltage_mv),
            stable_lock_clock_mhz=int(final_lock_clock_mhz),
            stable_probe=final_probe,
            stable_history=stable_history,
            base_curve=base_curve,
            final_verification_duration_s=int(final_verification_duration_s),
            initial_target_voltage_mv=int(baseline_candidate.voltage_mv),
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            tail_rise_bins=int(tail_rise_bins),
            request_reason=str(request_reason or "sweep-complete"),
        )
        final_verification_duration_s = int(selected_final_verification_duration_s)
        if selected_stable_probe is not None:
            final_probe = selected_stable_probe

    return FinalScanCandidate(
        plan=final_plan,
        voltage_mv=int(final_voltage_mv),
        lock_clock_mhz=int(final_lock_clock_mhz),
        probe=final_probe,
        verification_duration_s=int(final_verification_duration_s),
        auto_oc_metadata=dict(final_auto_oc_metadata or {}),
    )


def run_recovered_previous_crash_selection(
    *,
    pending_recovery_selection,
    base_curve: list[dict],
    gpu,
    settings,
    q2rtx_config: Q2RTXStabilityConfig,
    runtime_options: dict,
    final_verification_duration_s: int,
    tail_rise_bins: int,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
) -> AutoUvVoltageScanResult:
    (
        recovery_plan,
        recovery_voltage_mv,
        recovery_lock_clock_mhz,
        recovery_probe,
        selected_final_duration_s,
        recovery_tail_rise_bins,
        recovery_record,
    ) = pending_recovery_selection
    final_verification_duration_s = int(selected_final_duration_s)
    stable_candidate = VfCurveCandidate(
        label="previous-crash-resume",
        voltage_mv=int(recovery_voltage_mv),
        target_mhz=int(recovery_lock_clock_mhz),
        flattened_plan=recovery_plan,
        metadata={"tail_rise_bins": int(recovery_tail_rise_bins)},
    )
    stable_probe = recovery_probe or probe_summary_from_candidate_record(
        recovery_record
    )
    if stable_probe is None:
        raise AutoUvError("Recovered Auto-UV candidate did not include probe metrics")
    recovered_base_probe = base_probe_summary_from_candidate_record(recovery_record)
    discovery_summary = recovered_base_probe or stable_probe
    baseline_voltage_mv = positive_int(
        recovery_record.get("base_candidate_voltage_mv")
    ) or recovery_initial_target_voltage_mv(
        [recovery_record],
        fallback_voltage_mv=int(recovery_voltage_mv),
    )
    baseline_clock_mhz = _float_or_none(
        recovery_record.get("base_avg_core_clock_mhz"),
        getattr(discovery_summary, "avg_core_clock_mhz", None),
        recovery_lock_clock_mhz,
    )
    baseline_lock_clock_mhz = positive_int(
        recovery_record.get("base_lock_clock_mhz")
    ) or int(round(float(baseline_clock_mhz or recovery_lock_clock_mhz)))
    baseline_candidate = VfCurveCandidate(
        label="previous-crash-resume-baseline",
        voltage_mv=int(baseline_voltage_mv),
        target_mhz=int(baseline_lock_clock_mhz),
        flattened_plan=base_curve,
        metadata={"tail_rise_bins": int(tail_rise_bins)},
    )
    baseline_target = SimpleNamespace(
        measured_clock_mhz=float(baseline_clock_mhz or baseline_lock_clock_mhz)
    )
    recovered_profile_tier = auto_uv_run_profile_tier(
        runtime_options,
        settings,
        tail_rise_bins=int(recovery_tail_rise_bins),
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
        log=log,
        marker_details=auto_uv_run_marker_details(
            runtime_options,
            settings,
            tail_rise_bins=int(recovery_tail_rise_bins),
            profile_tier=recovered_profile_tier,
        ),
        event_callback=event_callback,
    )
    probe_history: list[AutoUvProbeSummary] = []
    append_unique_probe_summary(probe_history, discovery_summary)
    append_unique_probe_summary(probe_history, stable_probe)
    stable_history: list[AutoUvProbeSummary] = []
    append_unique_probe_summary(stable_history, stable_probe)
    final_tail_rise_bins = int(recovery_tail_rise_bins)
    log_phase(
        log,
        "crash-recovery",
        "resuming performance Auto-UV from saved candidate without baseline probe "
        f"candidate={int(recovery_voltage_mv)}mV@"
        f"{int(recovery_lock_clock_mhz)}MHz "
        f"base={int(baseline_voltage_mv)}mV@"
        f"{float(baseline_target.measured_clock_mhz):.0f}MHz",
    )
    replay_recovered_resume_probe_rows(
        event_callback=event_callback,
        base_probe=recovered_base_probe,
        stable_probe=stable_probe,
    )

    final_selection = select_final_scan_candidate(
        base_curve=base_curve,
        settings=settings,
        runtime_options=runtime_options,
        stable_plan=stable_candidate.flattened_plan,
        stable_voltage_mv=int(stable_candidate.voltage_mv),
        stable_lock_clock_mhz=int(stable_candidate.target_mhz),
        stable_probe=stable_probe,
        stable_history=stable_history,
        runner=runner,
        gpu=gpu,
        probe_history=probe_history,
        log=log,
        tail_rise_bins=int(final_tail_rise_bins),
        measured_baseline_clock_mhz=float(baseline_target.measured_clock_mhz),
        discovery_summary=discovery_summary,
        baseline_candidate=baseline_candidate,
        final_verification_duration_s=int(final_verification_duration_s),
        event_callback=event_callback,
        run_performance_auto_oc=True,
        request_reason="sweep-complete",
    )

    return run_final_verification_and_save(
        probe_voltage_candidate=probe_voltage_candidate,
        build_voltage_scan_result=build_voltage_scan_result,
        log=log,
        reader=gpu.reader,
        stable_plan=final_selection.plan,
        stable_voltage_mv=int(final_selection.voltage_mv),
        stable_lock_clock_mhz=int(final_selection.lock_clock_mhz),
        stable_probe=final_selection.probe,
        stable_history=stable_history,
        probe_history=probe_history,
        q2rtx_config=q2rtx_config,
        final_verification_duration_s=int(final_selection.verification_duration_s),
        start_voltage_mv=int(baseline_candidate.voltage_mv),
        measured_clock_mhz=float(baseline_target.measured_clock_mhz),
        nvml_session=gpu.live_voltage_reader,
        clock_ceiling=gpu.clock_ceiling,
        discovery_summary=discovery_summary,
        translated_gpu_policy=gpu.translated_gpu_policy,
        min_performance_core_clock_pct=float(settings.min_performance_core_clock_pct),
        runtime_default_plan=gpu.runtime_default_plan,
        final_clock_drop_margin_pct=float(settings.final_clock_drop_margin_pct),
        tail_rise_bins=int(final_tail_rise_bins),
        auto_uv_mode=str(settings.auto_uv_mode),
        generated_profile_tier=auto_uv_run_profile_tier(
            runtime_options,
            settings,
            tail_rise_bins=int(final_tail_rise_bins),
        ),
        auto_oc_metadata=dict(final_selection.auto_oc_metadata or {}),
        event_callback=event_callback,
    )




def _format_optional_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f}%"
