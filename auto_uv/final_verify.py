from __future__ import annotations

from dataclasses import replace

from afterburner.import_vf_curve import apply_plan

from .artifacts import (
    _write_final_curve_snapshot,
    _write_latest_verified_uv_result,
    _write_saved_uv_state,
    _write_stable_uv_result,
    _write_uv_result_snapshot,
)
from .clock_bump import (
    _clock_bump_consumed_pct,
    _format_clock_bump_budget,
    _clock_bump_marker_details,
    _make_clock_bump_candidate,
    _next_clock_bump_target_mhz,
)
from .curve_planning import _next_higher_voltage_bin
from .fan_tuning import write_auto_uv_fan_payload
from .models import AutoUvError
from .probe_config import (
    _budget_final_probe_durations,
    _cuda_bruteforce_companion_command,
    _normalize_probe_config,
)
from .probe_metrics import _evaluate_probe
from .scan_rules import _final_failure_can_accept_budget_curve
from .tuning import AUTO_UV_PROBE_TUNING
from .user_output import (
    format_user_value as _format_user_value,
    log_benchmark as _log_benchmark,
    log_fan_curve_ascii_chart as _log_fan_curve_ascii_chart,
    log_final_summary as _log_final_summary,
    log_phase as _log_phase,
    log_user_readable_final_summary as _log_user_readable_final_summary,
    log_user_stage as _log_user_stage,
    log_vf_ascii_chart as _log_vf_ascii_chart,
    log_vf_point_list as _log_vf_point_list,
)


def _choose_final_comparison_probe(
    *,
    stable_probe,
    final_probe,
    final_voltage_mv: int,
    final_lock_clock_mhz: int,
):
    if (
        stable_probe is not None
        and int(stable_probe.candidate_voltage_mv) == int(final_voltage_mv)
        and int(stable_probe.lock_clock_mhz) == int(final_lock_clock_mhz)
    ):
        return stable_probe
    return final_probe or stable_probe


def _run_final_verification_and_save(
    *,
    probe_voltage_candidate,
    probe_stabilization_search,
    build_voltage_scan_result,
    curve_overclock_summary,
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
    clock_bump_budget_limit_pct,
    clock_bump_recovery_count=0,
    clock_bump_budget_used_pct=0.0,
    max_bump_recovery_was_used=False,
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
    final_verification_status = "not-run"
    final_clock_bump_recovery_count = max(0, int(clock_bump_recovery_count))
    final_clock_bump_budget_used_pct = max(0.0, float(clock_bump_budget_used_pct))
    final_recovery_marker_details = None
    while (
        final_plan is not None
        and final_voltage_mv is not None
        and final_lock_clock_mhz is not None
    ):
        final_error = ""
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
        final_probe, final_result = probe_voltage_candidate(
            reader=reader,
            candidate_plan=final_plan,
            candidate_voltage_mv=int(final_voltage_mv),
            lock_clock_mhz=int(final_lock_clock_mhz),
            q2rtx_config=final_verify_config,
            stable_history=stable_history,
            initial_probe_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label=(
                "final-recovery"
                if final_recovery_marker_details is not None
                else "final-verify"
            ),
            log_context=_format_clock_bump_budget(
                used_pct=float(final_clock_bump_budget_used_pct),
                limit_pct=float(clock_bump_budget_limit_pct),
            ),
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            enforce_target_core_clock_floor=False,
            reset_plan=runtime_default_plan,
            suppress_unsafe_for_controlled_clock_abort=(
                bool(max_bump_recovery_was_used)
                or float(final_clock_bump_budget_used_pct)
                < float(clock_bump_budget_limit_pct)
            ),
            marker_details=final_recovery_marker_details,
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
                final_verification_status = (
                    f"completed {int(final_verification_duration_s)}s long check"
                )
                break
            _log_phase(log, "final-verify", f"rejected {final_error}")
        reason = (
            final_error
            if final_result.success
            else str(getattr(final_result, "reason", "unknown"))
        )
        if _final_failure_can_accept_budget_curve(str(reason)) and float(
            final_clock_bump_budget_used_pct
        ) < float(clock_bump_budget_limit_pct):
            bump_limit_mhz = (
                float(measured_clock_mhz)
                if measured_clock_mhz is not None
                else float(final_lock_clock_mhz)
            )
            bumped_clock_mhz = _next_clock_bump_target_mhz(
                source_result["plan"],
                current_clock_mhz=int(final_lock_clock_mhz),
                cap_clock_mhz=float(bump_limit_mhz),
                remaining_budget_pct=max(
                    0.0,
                    float(clock_bump_budget_limit_pct)
                    - float(final_clock_bump_budget_used_pct),
                ),
                reason=str(reason),
            )
            if bumped_clock_mhz is not None:
                budget_used_before_pct = float(final_clock_bump_budget_used_pct)
                final_clock_bump_recovery_count += 1
                final_clock_bump_budget_used_pct += _clock_bump_consumed_pct(
                    previous_target_clock_mhz=int(final_lock_clock_mhz),
                    bumped_target_clock_mhz=int(bumped_clock_mhz),
                )
                bumped_candidate = _make_clock_bump_candidate(
                    source_result["plan"],
                    candidate_voltage_mv=int(final_voltage_mv),
                    target_clock_mhz=int(bumped_clock_mhz),
                    reason_label="final-low-clock-recovery",
                    budget_used_pct=float(final_clock_bump_budget_used_pct),
                    budget_limit_pct=float(clock_bump_budget_limit_pct),
                )
                _log_phase(
                    log,
                    "final-verify",
                    f"applying budgeted curve overclock "
                    f"attempt={int(final_clock_bump_recovery_count)} "
                    f"{_format_clock_bump_budget(used_pct=final_clock_bump_budget_used_pct, limit_pct=clock_bump_budget_limit_pct)} "
                    f"voltage={int(final_voltage_mv)}mV "
                    f"target={int(final_lock_clock_mhz)}->{int(bumped_candidate.target_clock_mhz)}MHz "
                    f"cap={int(round(float(bump_limit_mhz)))}MHz "
                    f"reason={reason}",
                )
                _log_user_stage(
                    log,
                    "Final long verification",
                    [
                        "The long check missed the loaded-clock floor.",
                        f"Retrying the final verification with a budgeted overclock ({_format_clock_bump_budget(used_pct=final_clock_bump_budget_used_pct, limit_pct=clock_bump_budget_limit_pct)}).",
                        f"New final-check target: {int(bumped_candidate.target_clock_mhz)}MHz at {int(final_voltage_mv)}mV.",
                    ],
                )
                _log_vf_ascii_chart(
                    log,
                    plan=bumped_candidate.plan,
                    target_clock_mhz=bumped_candidate.target_clock_mhz,
                    candidate_voltage_mv=bumped_candidate.candidate_voltage_mv,
                )
                _log_vf_point_list(
                    log,
                    plan=bumped_candidate.plan,
                    label=(
                        f"final recovery target={int(bumped_candidate.target_clock_mhz)}MHz "
                        f"voltage={int(bumped_candidate.candidate_voltage_mv)}mV"
                    ),
                )
                previous_final_lock_clock_mhz = int(final_lock_clock_mhz)
                final_plan = bumped_candidate.plan
                final_lock_clock_mhz = int(bumped_candidate.target_clock_mhz)
                final_recovery_marker_details = _clock_bump_marker_details(
                    previous_target_clock_mhz=int(previous_final_lock_clock_mhz),
                    bumped_target_clock_mhz=int(bumped_candidate.target_clock_mhz),
                    budget_used_before_pct=float(budget_used_before_pct),
                    budget_used_after_pct=float(final_clock_bump_budget_used_pct),
                    budget_limit_pct=float(clock_bump_budget_limit_pct),
                    reason="final-low-clock-recovery",
                )
                _log_phase(
                    log,
                    "final-verify",
                    f"overclock retry armed "
                    f"attempt={int(final_clock_bump_recovery_count)} "
                    f"{_format_clock_bump_budget(used_pct=final_clock_bump_budget_used_pct, limit_pct=clock_bump_budget_limit_pct)} "
                    f"target={int(previous_final_lock_clock_mhz)}->{int(final_lock_clock_mhz)}MHz "
                    f"voltage={int(final_voltage_mv)}mV",
                )
                max_bump_recovery_was_used = float(
                    final_clock_bump_budget_used_pct
                ) >= float(clock_bump_budget_limit_pct)
                continue
            _log_phase(
                log,
                "final-verify",
                f"cannot apply budgeted curve overclock "
                f"{_format_clock_bump_budget(used_pct=final_clock_bump_budget_used_pct, limit_pct=clock_bump_budget_limit_pct)} "
                f"target={int(final_lock_clock_mhz)}MHz "
                f"cap={int(round(float(bump_limit_mhz)))}MHz "
                f"reason={reason}",
            )
        if max_bump_recovery_was_used:
            _log_phase(
                log,
                "final-verify",
                "overclock budget was already used, but final long "
                "verification hit only the clock-floor guardrail; accepting "
                "the lowest curve instead of walking voltage upward "
                f"reason={reason}",
            )
            _log_user_stage(
                log,
                "Final long verification",
                [
                    "The lowest accepted curve only missed the loaded-clock floor.",
                    "PenguinBurner is keeping the lowest-voltage curve instead of raising voltage to chase the floor exactly.",
                ],
            )
            final_verification_status = (
                "accepted lowest curve after clock-floor guardrail miss"
            )
            break
        if _final_failure_can_accept_budget_curve(str(reason)):
            _log_phase(
                log,
                "final-verify",
                "accepting lowest curve after clock-floor guardrail miss; "
                f"not walking voltage upward reason={reason}",
            )
            _log_user_stage(
                log,
                "Final long verification",
                [
                    "The lowest accepted curve only missed the loaded-clock floor.",
                    "PenguinBurner is keeping the lowest-voltage curve instead of raising voltage to chase the floor exactly.",
                ],
            )
            final_verification_status = (
                "accepted lowest curve after clock-floor guardrail miss"
            )
            break
        recovery_candidate, recovery_summary, recovery_result = (
            probe_stabilization_search(
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
        stable_history.append(recovery_summary)
        if not recovery_summary.used_companion_load:
            _write_latest_verified_uv_result(
                plan=stable_plan,
                lock_clock_mhz=int(stable_lock_clock_mhz),
                voltage_mv=int(stable_voltage_mv),
                probe=stable_probe,
            )
    if final_plan is None or final_voltage_mv is None or final_lock_clock_mhz is None:
        raise AutoUvError("final verification did not produce a final curve")
    final_stable_path = _write_stable_uv_result(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=final_probe or stable_probe,
        label="final",
    )
    _log_phase(log, "final", f"stable-config-saved={final_stable_path}")
    final_saved_path = _write_saved_uv_state(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=final_probe or stable_probe,
        label="best-undervolt",
    )
    _log_phase(log, "final", f"saved={final_saved_path}")
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
    final_curve_overclock = curve_overclock_summary(
        final_plan=final_plan,
        vanilla_plan=runtime_default_plan,
        final_voltage_mv=int(final_voltage_mv),
    )
    final_comparison_probe = _choose_final_comparison_probe(
        stable_probe=stable_probe,
        final_probe=final_probe,
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
    )
    _log_final_summary(
        log,
        baseline_probe=discovery_summary,
        final_probe=final_comparison_probe,
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
        clock_drop_margin_pct=float(final_clock_drop_margin_pct),
        final_verification_status=final_verification_status,
        final_curve_overclock=final_curve_overclock,
    )
    _log_user_readable_final_summary(
        log,
        baseline_probe=discovery_summary,
        final_probe=final_comparison_probe,
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
        clock_drop_margin_pct=float(final_clock_drop_margin_pct),
        curve_path=snapshot_path,
        final_verification_status=final_verification_status,
        final_curve_overclock=final_curve_overclock,
    )
    _log_phase(log, "final", f"curve-saved={snapshot_path}")
    return build_voltage_scan_result(
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
        initial_probe=discovery_summary,
        probe_history=probe_history,
        final_probe=final_comparison_probe,
    )
