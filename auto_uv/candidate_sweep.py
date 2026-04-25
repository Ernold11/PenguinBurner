from __future__ import annotations

from dataclasses import dataclass

from afterburner.import_vf_curve import apply_plan

from .artifacts import _write_latest_verified_uv_result
from .clock_bump import (
    _clock_bump_marker_details,
    _make_clock_bump_candidate,
    _next_clock_bump_target_mhz,
)
from .curve_planning import (
    _make_curve_candidate,
    _next_higher_voltage_bin,
    _next_search_candidate_voltage_mv,
)
from .models import AutoUvCurveCandidate, AutoUvProbeSummary
from .probe_config import _stability_probe_config_for_voltage_band
from .probe_metrics import _evaluate_probe, _temperature_normalized_efficiency_delta
from .scan_rules import (
    _final_failure_can_accept_budget_curve,
    _is_power_up_efficiency_down_regression,
    _percent,
    _real_clock_adjusted_stable_curve,
)
from .tuning import AUTO_UV_VOLTAGE_PHASE_TUNING
from .user_output import (
    format_probe_summary as _format_probe_summary,
    log_benchmark as _log_benchmark,
    log_phase as _log_phase,
    log_user_candidate_intro as _log_user_candidate_intro,
    log_user_candidate_result as _log_user_candidate_result,
    log_vf_ascii_chart as _log_vf_ascii_chart,
    log_vf_point_list as _log_vf_point_list,
)


@dataclass(slots=True)
class _CandidateSweepState:
    stable_plan: list[dict]
    stable_voltage_mv: int
    stable_lock_clock_mhz: int
    stable_probe: AutoUvProbeSummary
    candidate_voltage_mv: int | None
    clock_bump_recovery_count: int = 0
    clock_bump_last_target_mhz: int | None = None


def _candidate_sweep_result_from_state(state: _CandidateSweepState) -> dict:
    return {
        "stable_plan": state.stable_plan,
        "stable_voltage_mv": int(state.stable_voltage_mv),
        "stable_lock_clock_mhz": int(state.stable_lock_clock_mhz),
        "stable_probe": state.stable_probe,
        "ended_by_clock_bump_limit": False,
        "clock_bump_recovery_count": int(state.clock_bump_recovery_count),
    }


def _current_candidate_target_mhz(
    stable_lock_clock_mhz: int,
    clock_bump_last_target_mhz: int | None,
) -> int:
    if clock_bump_last_target_mhz is None:
        return int(stable_lock_clock_mhz)
    return max(int(stable_lock_clock_mhz), int(clock_bump_last_target_mhz))


def _keep_bumped_target_floor(
    *,
    log,
    source_plan: list[dict],
    candidate_voltage_mv: int,
    adjusted_plan: list[dict],
    adjusted_lock_clock_mhz: int,
    clock_bump_last_target_mhz: int | None,
    phase: str,
    context: str,
) -> tuple[list[dict], int]:
    if clock_bump_last_target_mhz is None:
        return adjusted_plan, int(adjusted_lock_clock_mhz)
    bumped_floor_mhz = int(clock_bump_last_target_mhz)
    if int(adjusted_lock_clock_mhz) >= bumped_floor_mhz:
        return adjusted_plan, int(adjusted_lock_clock_mhz)
    promoted = _make_curve_candidate(
        source_plan,
        candidate_voltage_mv=int(candidate_voltage_mv),
        target_clock_mhz=bumped_floor_mhz,
        label=f"{context} keep-accepted-clock-bump",
    )
    _log_phase(
        log,
        phase,
        f"keeping accepted bumped target={bumped_floor_mhz}MHz "
        f"instead of lowering to real-clock adjusted {int(adjusted_lock_clock_mhz)}MHz "
        f"voltage={int(candidate_voltage_mv)}mV",
    )
    return promoted.plan, bumped_floor_mhz


def _accept_lowest_clock_floor_miss(
    *,
    log,
    reader,
    state: _CandidateSweepState,
    source_plan: list[dict],
    candidate: AutoUvCurveCandidate,
    probe: AutoUvProbeSummary,
    target_clock_mhz: int,
    reason: str,
    stable_history: list[AutoUvProbeSummary],
    discovery_summary: AutoUvProbeSummary,
    previous_stable_probe_for_iteration: AutoUvProbeSummary,
    candidate_attempt_count: int,
    max_clock_bump_recoveries: int,
) -> dict:
    state.stable_voltage_mv = int(candidate.candidate_voltage_mv)
    state.stable_probe = probe
    state.stable_lock_clock_mhz = int(target_clock_mhz)
    state.stable_plan = _make_curve_candidate(
        source_plan,
        candidate_voltage_mv=int(state.stable_voltage_mv),
        target_clock_mhz=int(state.stable_lock_clock_mhz),
        label="accepted-lowest-clock-floor-miss",
    ).plan
    apply_plan(reader, state.stable_plan)
    reader.refresh_points()
    stable_history.append(probe)
    if not probe.used_companion_load:
        _write_latest_verified_uv_result(
            plan=state.stable_plan,
            lock_clock_mhz=int(state.stable_lock_clock_mhz),
            voltage_mv=int(state.stable_voltage_mv),
            probe=state.stable_probe,
        )
    _log_phase(
        log,
        "final",
        f"clock-floor miss accepted as lowest voltage; "
        f"finishing with {int(state.stable_voltage_mv)}mV@{int(state.stable_lock_clock_mhz)}MHz "
        f"reason={reason}",
    )
    _log_user_candidate_result(
        log,
        attempt=candidate_attempt_count,
        decision="ACCEPTED AS LOWEST VOLTAGE",
        reason=(
            "This curve was slightly below the loaded-clock floor. "
            "PenguinBurner is keeping it as the final lowest-voltage "
            "curve instead of walking voltage upward to chase the "
            "floor exactly."
        ),
        initial_probe=discovery_summary,
        previous_probe=previous_stable_probe_for_iteration,
        candidate_probe=state.stable_probe,
        restored_voltage_mv=int(state.stable_voltage_mv),
        restored_lock_clock_mhz=int(state.stable_lock_clock_mhz),
    )
    result = _candidate_sweep_result_from_state(state)
    result["ended_by_clock_bump_limit"] = int(state.clock_bump_recovery_count) >= int(
        max_clock_bump_recoveries
    )
    return result


def _run_candidate_sweep(
    *,
    probe_voltage_candidate,
    probe_stabilization_search,
    describe_guardrails,
    latest_reference_voltage_mv,
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
    max_clock_bump_recoveries,
):
    candidate_attempt_count = 0
    failed_candidate_floor_mv = None
    candidate_voltage_mv = int(first_candidate_voltage_mv)
    non_improving_efficiency_streak = 0
    pending_efficiency_stop_curve = None
    clock_bump_recovery_count = 0
    clock_bump_last_target_mhz = None
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
        reference_actual_voltage_mv = latest_reference_voltage_mv(
            stable_history,
            discovery_summary.avg_voltage_mv,
        )
        candidate_target_mhz = _current_candidate_target_mhz(
            int(stable_lock_clock_mhz),
            clock_bump_last_target_mhz,
        )
        candidate = _make_curve_candidate(
            source_result["plan"],
            candidate_voltage_mv=int(candidate_voltage_mv),
            target_clock_mhz=int(candidate_target_mhz),
            label=(
                f"voltage={candidate_voltage_mv}mV phase={phase} "
                + (
                    "target=accepted-clock-bump"
                    if clock_bump_last_target_mhz is not None
                    else "target=last-probe-real-clock"
                )
            ),
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
            + f"{describe_guardrails(stable_history, min_performance_core_clock_pct=float(min_performance_core_clock_pct))}",
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
        probe_summary, probe_result = probe_voltage_candidate(
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
                bumped_failure_reason = None
                bumped_failure_probe_summary = None
                accepted_low_clock_recovery = False
                bump_limit_mhz = (
                    float(measured_clock_mhz)
                    if measured_clock_mhz is not None
                    else float(candidate.target_clock_mhz)
                )
                recovery_source_target_mhz = int(candidate.target_clock_mhz)
                recovery_reason = str(probe_result.reason)
                while int(clock_bump_recovery_count) < int(max_clock_bump_recoveries):
                    bump_source_clock_mhz = max(
                        int(recovery_source_target_mhz),
                        int(clock_bump_last_target_mhz or 0),
                    )
                    bumped_clock_mhz = _next_clock_bump_target_mhz(
                        source_result["plan"],
                        current_clock_mhz=int(bump_source_clock_mhz),
                        cap_clock_mhz=float(bump_limit_mhz),
                    )
                    if bumped_clock_mhz is None:
                        break
                    clock_bump_recovery_count += 1
                    clock_bump_last_target_mhz = int(bumped_clock_mhz)
                    bumped_candidate = _make_clock_bump_candidate(
                        source_result["plan"],
                        candidate_voltage_mv=int(candidate.candidate_voltage_mv),
                        target_clock_mhz=int(bumped_clock_mhz),
                        reason_label="low-clock-recovery",
                    )
                    _log_phase(
                        log,
                        "candidate",
                        f"low-clock-recovery applying +2.0% curve overclock "
                        f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                        f"voltage={candidate.candidate_voltage_mv}mV "
                        f"target={int(bump_source_clock_mhz)}->{bumped_candidate.target_clock_mhz}MHz "
                        f"cap={int(round(float(bump_limit_mhz)))}MHz "
                        f"reason={recovery_reason}",
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
                            f"candidate recovery target={bumped_candidate.target_clock_mhz}MHz "
                            f"voltage={bumped_candidate.candidate_voltage_mv}mV"
                        ),
                    )
                    if clock_ceiling is not None:
                        clock_ceiling.retarget(
                            lock_clock_mhz=int(bumped_candidate.target_clock_mhz),
                            lock_voltage_mv=int(bumped_candidate.candidate_voltage_mv),
                        )
                        _log_phase(log, "ceiling", clock_ceiling.describe())
                    bumped_summary, bumped_result = probe_voltage_candidate(
                        reader=reader,
                        candidate_plan=bumped_candidate.plan,
                        candidate_voltage_mv=bumped_candidate.candidate_voltage_mv,
                        lock_clock_mhz=bumped_candidate.target_clock_mhz,
                        q2rtx_config=_stability_probe_config_for_voltage_band(
                            q2rtx_config,
                            initial_target_voltage_mv=int(start_voltage_mv),
                            candidate_voltage_mv=int(
                                bumped_candidate.candidate_voltage_mv
                            ),
                        ),
                        stable_history=stable_history,
                        initial_probe_clock_mhz=measured_clock_mhz,
                        nvml_session=nvml_session,
                        log=log,
                        phase_label="candidate-recovery",
                        power_limit_w=translated_gpu_policy.get("power_limit_w"),
                        min_performance_core_clock_pct=float(
                            min_performance_core_clock_pct
                        ),
                        reset_plan=runtime_default_plan,
                        marker_details=_clock_bump_marker_details(
                            attempt=int(clock_bump_recovery_count),
                            limit=int(max_clock_bump_recoveries),
                            previous_target_clock_mhz=int(recovery_source_target_mhz),
                            bumped_target_clock_mhz=int(
                                bumped_candidate.target_clock_mhz
                            ),
                        ),
                    )
                    probe_history.append(bumped_summary)
                    _log_benchmark(
                        log,
                        phase="candidate-recovery",
                        probe=bumped_summary,
                        reference_probe=discovery_summary,
                        reference_label="initial",
                    )
                    if bumped_result.success:
                        bumped_error = _evaluate_probe(
                            bumped_summary,
                            stable_history=stable_history,
                            min_performance_core_clock_pct=float(
                                min_performance_core_clock_pct
                            ),
                        )
                        if not bumped_error:
                            stable_voltage_mv = int(
                                bumped_candidate.candidate_voltage_mv
                            )
                            stable_probe = bumped_summary
                            stable_plan = bumped_candidate.plan
                            stable_lock_clock_mhz = int(
                                bumped_candidate.target_clock_mhz
                            )
                            stable_history.append(bumped_summary)
                            if not bumped_summary.used_companion_load:
                                _write_latest_verified_uv_result(
                                    plan=stable_plan,
                                    lock_clock_mhz=int(stable_lock_clock_mhz),
                                    voltage_mv=int(stable_voltage_mv),
                                    probe=stable_probe,
                                )
                            _log_phase(
                                log,
                                "candidate",
                                f"accepted low-clock recovery keeping bumped target={int(stable_lock_clock_mhz)}MHz "
                                f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                                + _format_probe_summary(bumped_summary),
                            )
                            if int(clock_bump_recovery_count) >= int(
                                max_clock_bump_recoveries
                            ):
                                _log_phase(
                                    log,
                                    "candidate",
                                    f"low-clock-recovery bump limit reached "
                                    f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)}; "
                                    f"continuing voltage descent with accepted curve "
                                    f"{int(stable_voltage_mv)}mV@{int(stable_lock_clock_mhz)}MHz",
                                )
                            candidate_voltage_mv = _next_search_candidate_voltage_mv(
                                plan=source_result["plan"],
                                start_voltage_mv=int(start_voltage_mv),
                                stable_voltage_mv=int(stable_voltage_mv),
                                reference_actual_voltage_mv=latest_reference_voltage_mv(
                                    stable_history,
                                    reference_actual_voltage_mv,
                                ),
                                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
                                min_search_voltage_mv=min_search_voltage_mv,
                            )
                            accepted_low_clock_recovery = True
                            break
                        if _final_failure_can_accept_budget_curve(
                            str(bumped_error)
                        ) and int(clock_bump_recovery_count) < int(
                            max_clock_bump_recoveries
                        ):
                            recovery_source_target_mhz = int(
                                bumped_candidate.target_clock_mhz
                            )
                            recovery_reason = str(bumped_error)
                            _log_phase(
                                log,
                                "candidate",
                                f"low-clock-recovery still below floor; "
                                f"trying next +2.0% bump "
                                f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                                f"voltage={candidate.candidate_voltage_mv}mV "
                                f"target={int(recovery_source_target_mhz)}MHz "
                                f"reason={bumped_error}",
                            )
                            continue
                        _log_phase(
                            log,
                            "candidate",
                            f"low-clock-recovery rejected {bumped_error} "
                            f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                            f"probe={_format_probe_summary(bumped_summary)}",
                        )
                        bumped_failure_reason = str(bumped_error)
                        bumped_failure_probe_summary = bumped_summary
                    else:
                        bumped_failure_reason = str(bumped_result.reason)
                        bumped_failure_probe_summary = bumped_summary
                        _log_phase(
                            log,
                            "candidate",
                            f"low-clock-recovery +2.0% probe failed "
                            f"voltage={bumped_candidate.candidate_voltage_mv}mV "
                            f"target={bumped_candidate.target_clock_mhz}MHz "
                            f"reason={bumped_result.reason} "
                            f"probe={_format_probe_summary(bumped_summary)}",
                        )
                        if str(bumped_result.reason).startswith(
                            (
                                "telemetry-live-core_clock",
                                "telemetry-live-core_clock-avg",
                            )
                        ) and int(clock_bump_recovery_count) < int(
                            max_clock_bump_recoveries
                        ):
                            recovery_source_target_mhz = int(
                                bumped_candidate.target_clock_mhz
                            )
                            recovery_reason = str(bumped_result.reason)
                            _log_phase(
                                log,
                                "candidate",
                                f"low-clock-recovery still below floor; "
                                f"trying next +2.0% bump "
                                f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                                f"voltage={candidate.candidate_voltage_mv}mV "
                                f"target={int(recovery_source_target_mhz)}MHz "
                                f"reason={bumped_result.reason}",
                            )
                            continue
                    break
                if accepted_low_clock_recovery:
                    continue
                if (
                    int(clock_bump_recovery_count) >= int(max_clock_bump_recoveries)
                    and bumped_failure_reason is not None
                ):
                    _log_phase(
                        log,
                        "candidate",
                        f"low-clock-recovery bump limit reached after failed probe "
                        f"limit={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                        f"voltage={candidate.candidate_voltage_mv}mV "
                        f"target={int(recovery_source_target_mhz)}MHz "
                        f"reason={bumped_failure_reason}",
                    )
                elif bumped_failure_reason is None:
                    bump_source_clock_mhz = max(
                        int(recovery_source_target_mhz),
                        int(clock_bump_last_target_mhz or 0),
                    )
                    bumped_clock_mhz = _next_clock_bump_target_mhz(
                        source_result["plan"],
                        current_clock_mhz=int(bump_source_clock_mhz),
                        cap_clock_mhz=float(bump_limit_mhz),
                    )
                    if bumped_clock_mhz is not None:
                        _log_phase(
                            log,
                            "candidate",
                            f"low-clock-recovery skipped +2.0% curve overclock "
                            f"limit={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                            f"voltage={candidate.candidate_voltage_mv}mV "
                            f"target={int(bump_source_clock_mhz)}->{int(bumped_clock_mhz)}MHz "
                            f"reason={recovery_reason}",
                        )
                    else:
                        _log_phase(
                            log,
                            "candidate",
                            f"low-clock-recovery cannot bump target further "
                            f"voltage={candidate.candidate_voltage_mv}mV "
                            f"target={int(recovery_source_target_mhz)}MHz "
                            f"cap={int(round(float(bump_limit_mhz)))}MHz "
                            f"reason={recovery_reason}",
                        )

                accepted_floor_miss_probe = None
                accepted_floor_miss_target_mhz = None
                if (
                    bumped_failure_reason is not None
                    and _final_failure_can_accept_budget_curve(
                        str(bumped_failure_reason)
                    )
                ):
                    accepted_floor_miss_probe = bumped_failure_probe_summary
                    if accepted_floor_miss_probe is not None:
                        accepted_floor_miss_target_mhz = int(
                            accepted_floor_miss_probe.lock_clock_mhz
                        )
                elif _final_failure_can_accept_budget_curve(str(probe_result.reason)):
                    accepted_floor_miss_probe = probe_summary
                    accepted_floor_miss_target_mhz = int(candidate.target_clock_mhz)
                if (
                    accepted_floor_miss_probe is not None
                    and accepted_floor_miss_target_mhz is not None
                ):
                    return _accept_lowest_clock_floor_miss(
                        log=log,
                        reader=reader,
                        state=_CandidateSweepState(
                            stable_plan=stable_plan,
                            stable_voltage_mv=int(stable_voltage_mv),
                            stable_lock_clock_mhz=int(stable_lock_clock_mhz),
                            stable_probe=stable_probe,
                            candidate_voltage_mv=int(candidate.candidate_voltage_mv),
                            clock_bump_recovery_count=int(clock_bump_recovery_count),
                            clock_bump_last_target_mhz=clock_bump_last_target_mhz,
                        ),
                        source_plan=source_result["plan"],
                        candidate=candidate,
                        probe=accepted_floor_miss_probe,
                        target_clock_mhz=int(accepted_floor_miss_target_mhz),
                        reason=str(bumped_failure_reason or probe_result.reason),
                        stable_history=stable_history,
                        discovery_summary=discovery_summary,
                        previous_stable_probe_for_iteration=previous_stable_probe_for_iteration,
                        candidate_attempt_count=int(candidate_attempt_count),
                        max_clock_bump_recoveries=int(max_clock_bump_recoveries),
                    )

                apply_plan(reader, stable_plan)
                reader.refresh_points()
                restored_live_mv = nvml_session.read_live_voltage_mv()
                if bumped_failure_reason is not None:
                    final_reason = (
                        f"+2.0% recovery at {candidate.candidate_voltage_mv}mV "
                        f"failed: {bumped_failure_reason}"
                    )
                    user_reason = (
                        "The candidate first missed the loaded-clock floor, then the "
                        f"+2.0% recovery probe failed: {bumped_failure_reason}. "
                        "The previous stable curve was restored."
                    )
                    candidate_result_probe = (
                        bumped_failure_probe_summary or probe_summary
                    )
                else:
                    final_reason = (
                        f"low-frequency fail at {candidate.candidate_voltage_mv}mV"
                    )
                    user_reason = (
                        "This voltage could not hold the target core clock. "
                        "The previous stable curve was restored."
                    )
                    candidate_result_probe = probe_summary
                _log_phase(
                    log,
                    "final",
                    f"{final_reason}; "
                    f"finishing with previous stable {stable_voltage_mv}mV@{stable_lock_clock_mhz}MHz "
                    f"restored-live-voltage={restored_live_mv if restored_live_mv is not None else 'n/a'}",
                )
                _log_user_candidate_result(
                    log,
                    attempt=candidate_attempt_count,
                    decision="STOPPED",
                    reason=user_reason,
                    initial_probe=discovery_summary,
                    previous_probe=previous_stable_probe_for_iteration,
                    candidate_probe=candidate_result_probe,
                    restored_voltage_mv=int(stable_voltage_mv),
                    restored_lock_clock_mhz=int(stable_lock_clock_mhz),
                )
                break
            recovery_candidate, recovery_summary, recovery_result = (
                probe_stabilization_search(
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
                stable_voltage_mv = int(recovery_candidate.candidate_voltage_mv)
                stable_probe = recovery_summary
                stable_plan, stable_lock_clock_mhz = _real_clock_adjusted_stable_curve(
                    source_result["plan"],
                    candidate_voltage_mv=int(stable_voltage_mv),
                    previous_lock_clock_mhz=int(recovery_candidate.target_clock_mhz),
                    probe=stable_probe,
                )
                stable_plan, stable_lock_clock_mhz = _keep_bumped_target_floor(
                    log=log,
                    source_plan=source_result["plan"],
                    candidate_voltage_mv=int(stable_voltage_mv),
                    adjusted_plan=stable_plan,
                    adjusted_lock_clock_mhz=int(stable_lock_clock_mhz),
                    clock_bump_last_target_mhz=clock_bump_last_target_mhz,
                    phase="retest",
                    context="retest",
                )
                if int(stable_lock_clock_mhz) != int(
                    recovery_candidate.target_clock_mhz
                ):
                    _log_phase(
                        log,
                        "retest",
                        f"real-clock target adjusted to {int(stable_lock_clock_mhz)}MHz "
                        f"from accepted avg_core_clock={stable_probe.avg_core_clock_mhz:.1f}MHz "
                        f"previous-target={int(recovery_candidate.target_clock_mhz)}MHz",
                    )
                stable_history.append(recovery_summary)
                if not recovery_summary.used_companion_load:
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
                plan=source_result["plan"],
                start_voltage_mv=int(start_voltage_mv),
                stable_voltage_mv=int(stable_voltage_mv),
                reference_actual_voltage_mv=latest_reference_voltage_mv(
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
            if _final_failure_can_accept_budget_curve(str(evaluation_error)):
                stable_voltage_mv = int(candidate.candidate_voltage_mv)
                stable_probe = probe_summary
                stable_plan, stable_lock_clock_mhz = _real_clock_adjusted_stable_curve(
                    source_result["plan"],
                    candidate_voltage_mv=int(stable_voltage_mv),
                    previous_lock_clock_mhz=int(candidate.target_clock_mhz),
                    probe=stable_probe,
                )
                stable_plan, stable_lock_clock_mhz = _keep_bumped_target_floor(
                    log=log,
                    source_plan=source_result["plan"],
                    candidate_voltage_mv=int(stable_voltage_mv),
                    adjusted_plan=stable_plan,
                    adjusted_lock_clock_mhz=int(stable_lock_clock_mhz),
                    clock_bump_last_target_mhz=clock_bump_last_target_mhz,
                    phase="final",
                    context="accepted-lowest",
                )
                apply_plan(reader, stable_plan)
                reader.refresh_points()
                stable_history.append(probe_summary)
                if not probe_summary.used_companion_load:
                    _write_latest_verified_uv_result(
                        plan=stable_plan,
                        lock_clock_mhz=int(stable_lock_clock_mhz),
                        voltage_mv=int(stable_voltage_mv),
                        probe=stable_probe,
                    )
                _log_phase(
                    log,
                    "final",
                    f"clock-floor guardrail accepted as lowest voltage; "
                    f"finishing with {int(stable_voltage_mv)}mV@{int(stable_lock_clock_mhz)}MHz "
                    f"reason={evaluation_error}",
                )
                _log_user_candidate_result(
                    log,
                    attempt=candidate_attempt_count,
                    decision="ACCEPTED AS LOWEST VOLTAGE",
                    reason=(
                        "This curve completed the probe but landed slightly below "
                        "the loaded-clock floor. PenguinBurner is keeping it as "
                        "the final lowest-voltage curve instead of walking voltage upward."
                    ),
                    initial_probe=discovery_summary,
                    previous_probe=previous_stable_probe_for_iteration,
                    candidate_probe=probe_summary,
                    restored_voltage_mv=int(stable_voltage_mv),
                    restored_lock_clock_mhz=int(stable_lock_clock_mhz),
                )
                break
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
                plan=source_result["plan"],
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
        stable_voltage_mv = int(candidate.candidate_voltage_mv)
        stable_probe = probe_summary
        stable_plan, stable_lock_clock_mhz = _real_clock_adjusted_stable_curve(
            source_result["plan"],
            candidate_voltage_mv=int(stable_voltage_mv),
            previous_lock_clock_mhz=int(candidate.target_clock_mhz),
            probe=stable_probe,
        )
        stable_plan, stable_lock_clock_mhz = _keep_bumped_target_floor(
            log=log,
            source_plan=source_result["plan"],
            candidate_voltage_mv=int(stable_voltage_mv),
            adjusted_plan=stable_plan,
            adjusted_lock_clock_mhz=int(stable_lock_clock_mhz),
            clock_bump_last_target_mhz=clock_bump_last_target_mhz,
            phase="accept",
            context="accepted-candidate",
        )
        if int(stable_lock_clock_mhz) != int(candidate.target_clock_mhz):
            _log_phase(
                log,
                "accept",
                f"real-clock target adjusted to {int(stable_lock_clock_mhz)}MHz "
                f"from accepted avg_core_clock={stable_probe.avg_core_clock_mhz:.1f}MHz "
                f"previous-target={int(candidate.target_clock_mhz)}MHz",
            )
        stable_history.append(probe_summary)
        if not probe_summary.used_companion_load:
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
        if efficiency_stop_candidate and int(clock_bump_recovery_count) < int(
            max_clock_bump_recoveries
        ):
            bump_limit_mhz = (
                float(measured_clock_mhz)
                if measured_clock_mhz is not None
                else float(stable_lock_clock_mhz)
            )
            bump_source_clock_mhz = max(
                int(stable_lock_clock_mhz),
                int(candidate.target_clock_mhz),
                int(clock_bump_last_target_mhz or 0),
            )
            bumped_clock_mhz = _next_clock_bump_target_mhz(
                source_result["plan"],
                current_clock_mhz=int(bump_source_clock_mhz),
                cap_clock_mhz=float(bump_limit_mhz),
            )
            if bumped_clock_mhz is not None:
                clock_bump_recovery_count += 1
                clock_bump_last_target_mhz = int(bumped_clock_mhz)
                bumped_candidate = _make_clock_bump_candidate(
                    source_result["plan"],
                    candidate_voltage_mv=int(stable_voltage_mv),
                    target_clock_mhz=int(bumped_clock_mhz),
                    reason_label="efficiency-wall",
                )
                _log_phase(
                    log,
                    "candidate",
                    f"efficiency-wall applying +2.0% curve overclock "
                    f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                    f"voltage={int(stable_voltage_mv)}mV "
                    f"target={int(bump_source_clock_mhz)}->{int(bumped_candidate.target_clock_mhz)}MHz "
                    f"cap={int(round(float(bump_limit_mhz)))}MHz",
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
                        f"candidate efficiency-bump target={int(bumped_candidate.target_clock_mhz)}MHz "
                        f"voltage={int(bumped_candidate.candidate_voltage_mv)}mV"
                    ),
                )
                bumped_summary, bumped_result = probe_voltage_candidate(
                    reader=reader,
                    candidate_plan=bumped_candidate.plan,
                    candidate_voltage_mv=int(bumped_candidate.candidate_voltage_mv),
                    lock_clock_mhz=int(bumped_candidate.target_clock_mhz),
                    q2rtx_config=_stability_probe_config_for_voltage_band(
                        q2rtx_config,
                        initial_target_voltage_mv=int(start_voltage_mv),
                        candidate_voltage_mv=int(bumped_candidate.candidate_voltage_mv),
                    ),
                    stable_history=stable_history,
                    initial_probe_clock_mhz=measured_clock_mhz,
                    nvml_session=nvml_session,
                    log=log,
                    phase_label="candidate-efficiency-bump",
                    power_limit_w=translated_gpu_policy.get("power_limit_w"),
                    min_performance_core_clock_pct=float(
                        min_performance_core_clock_pct
                    ),
                    reset_plan=runtime_default_plan,
                    marker_details=_clock_bump_marker_details(
                        attempt=int(clock_bump_recovery_count),
                        limit=int(max_clock_bump_recoveries),
                        previous_target_clock_mhz=int(stable_lock_clock_mhz),
                        bumped_target_clock_mhz=int(bumped_candidate.target_clock_mhz),
                        reason="efficiency-wall",
                    ),
                    suppress_unsafe_recording=True,
                )
                probe_history.append(bumped_summary)
                _log_benchmark(
                    log,
                    phase="candidate-efficiency-bump",
                    probe=bumped_summary,
                    reference_probe=discovery_summary,
                    reference_label="initial",
                )
                bumped_error = (
                    ""
                    if bumped_result.success
                    else str(getattr(bumped_result, "reason", "unknown"))
                )
                if bumped_result.success:
                    bumped_error = _evaluate_probe(
                        bumped_summary,
                        stable_history=stable_history,
                        min_performance_core_clock_pct=float(
                            min_performance_core_clock_pct
                        ),
                    )
                bumped_efficiency_delta = _temperature_normalized_efficiency_delta(
                    stable_probe,
                    bumped_summary,
                )
                if not bumped_error and bumped_efficiency_delta.get("improved") is True:
                    stable_probe = bumped_summary
                    stable_plan = bumped_candidate.plan
                    stable_lock_clock_mhz = int(bumped_candidate.target_clock_mhz)
                    stable_history.append(bumped_summary)
                    if not bumped_summary.used_companion_load:
                        _write_latest_verified_uv_result(
                            plan=stable_plan,
                            lock_clock_mhz=int(stable_lock_clock_mhz),
                            voltage_mv=int(stable_voltage_mv),
                            probe=stable_probe,
                        )
                    probe_summary = bumped_summary
                    efficiency_delta = _temperature_normalized_efficiency_delta(
                        previous_stable_probe,
                        stable_probe,
                    )
                    power_up_efficiency_down = _is_power_up_efficiency_down_regression(
                        previous_stable_probe,
                        stable_probe,
                        efficiency_delta,
                    )
                    efficiency_improved = efficiency_delta.get("improved")
                    measured_voltage_close_to_requested = bool(
                        efficiency_delta.get("measured_voltage_close_to_requested")
                    )
                    efficiency_stop_candidate = (
                        efficiency_stop_streak > 0
                        and (efficiency_improved is False or power_up_efficiency_down)
                        and measured_voltage_close_to_requested
                    )
                    _log_phase(
                        log,
                        "candidate",
                        f"efficiency-wall accepted +2.0% bump keeping target={int(stable_lock_clock_mhz)}MHz "
                        f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                        + _format_probe_summary(bumped_summary),
                    )
                else:
                    _log_phase(
                        log,
                        "candidate",
                        f"efficiency-wall rejected +2.0% bump "
                        f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                        f"reason={bumped_error or 'no-efficiency-gain'} "
                        f"probe={_format_probe_summary(bumped_summary)}",
                    )
                    apply_plan(reader, stable_plan)
                    reader.refresh_points()
            else:
                _log_phase(
                    log,
                    "candidate",
                    f"efficiency-wall cannot apply +2.0% curve overclock "
                    f"bump={int(clock_bump_recovery_count)}/{int(max_clock_bump_recoveries)} "
                    f"target={int(stable_lock_clock_mhz)}MHz "
                    f"cap={int(round(float(bump_limit_mhz)))}MHz",
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
            + describe_guardrails(
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
            plan=source_result["plan"],
            start_voltage_mv=int(start_voltage_mv),
            stable_voltage_mv=int(stable_voltage_mv),
            reference_actual_voltage_mv=latest_reference_voltage_mv(
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
        "ended_by_clock_bump_limit": int(clock_bump_recovery_count)
        >= int(max_clock_bump_recoveries),
        "clock_bump_recovery_count": int(clock_bump_recovery_count),
    }
