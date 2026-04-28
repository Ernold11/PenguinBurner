from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary

from .curve_planning import _next_search_candidate_voltage_mv
from .overclock_recovery import make_overclock_attempt, step_back_overclock_target
from .candidate_decision import AutoUv2SweepState, choose_next_candidate
from .probe_decision import classify_probe_result
from .sweep_behavior import (
    AutoUv2SweepEvent,
    AutoUv2SweepHooks,
    AutoUvAcceptedCandidateContext,
    accepted_candidate_pair,
    probe_evaluation_error,
    probe_reason,
    probe_success,
    state_uses_overclock,
)
from .sweep_modes import AUTO_UV_MODE_EFFICIENCY, make_auto_uv_sweep_behavior
from .sweep_state import apply_probe_decision


@dataclass(frozen=True, slots=True)
class AutoUv2SweepResult:
    state: AutoUv2SweepState
    stable_candidate: AutoUvCurveCandidate
    stable_probe: AutoUvProbeSummary
    stable_history: list[AutoUvProbeSummary]
    probe_history: list[AutoUvProbeSummary]
    events: list[AutoUv2SweepEvent]
    stop_reason: str


def _roll_back_full_budget_target_after_hard_failure(
    source_plan: list[dict],
    *,
    state: AutoUv2SweepState,
    failed_target_mhz: int,
) -> tuple[AutoUv2SweepState, int | None]:
    if state.full_budget_target_mhz is None:
        return state, None
    if int(failed_target_mhz) != int(state.full_budget_target_mhz):
        return state, None
    backed_off = step_back_overclock_target(
        source_plan,
        current_target_mhz=int(failed_target_mhz),
        last_overclock_target_mhz=int(state.full_budget_target_mhz),
    )
    if backed_off is None:
        return state, None
    return (
        replace(
            state,
            full_budget_target_mhz=int(backed_off),
            last_overclock_target_mhz=int(backed_off),
        ),
        int(backed_off),
    )


def _recover_and_update(
    source_plan: list[dict],
    *,
    hooks: AutoUv2SweepHooks,
    state: AutoUv2SweepState,
    candidate: AutoUvCurveCandidate,
    probe: AutoUvProbeSummary,
    reason: str,
    start_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
    preserve_base_below_mv: int | None,
    min_search_voltage_mv: int,
) -> tuple[
    AutoUv2SweepState,
    AutoUvCurveCandidate | None,
    AutoUvProbeSummary | None,
    AutoUv2SweepEvent,
    bool,
]:
    # Upward recovery is for hard failures, not efficiency walls.
    recovery_candidate, recovery_probe, recovery_result = hooks.recover_upward(
        candidate,
        probe,
        reason,
    )
    recovered = (
        recovery_candidate is not None
        and recovery_probe is not None
        and recovery_result is not None
        and probe_success(recovery_result)
    )
    if not recovered:
        return (
            state,
            None,
            None,
            AutoUv2SweepEvent("stop", "recovery failed; keeping previous stable curve"),
            True,
        )

    active_recovery_candidate, measured_recovery_candidate = accepted_candidate_pair(
        hooks,
        probed_candidate=recovery_candidate,
        probe=recovery_probe,
        uses_overclock=float(state.persistent_overclock_pct) > 0.0,
    )

    update = apply_probe_decision(
        source_plan,
        state=state,
        decision=classify_probe_result(
            probe_success=True,
            probe_failure_reason=None,
            evaluation_error=None,
            budget=state.budget,
            candidate_used_overclock=state_uses_overclock(state),
        ),
        candidate=active_recovery_candidate,
        probe=recovery_probe,
        start_voltage_mv=int(start_voltage_mv),
        reference_actual_voltage_mv=reference_actual_voltage_mv,
        preserve_base_below_mv=preserve_base_below_mv,
        min_search_voltage_mv=int(min_search_voltage_mv),
        recovered_voltage_mv=int(active_recovery_candidate.candidate_voltage_mv),
        recovered_target_mhz=int(active_recovery_candidate.target_clock_mhz),
        measured_target_mhz=int(measured_recovery_candidate.target_clock_mhz),
    )
    if update.write_latest_verified:
        hooks.write_latest_verified(active_recovery_candidate, recovery_probe)
    return (
        update.state,
        active_recovery_candidate,
        recovery_probe,
        AutoUv2SweepEvent("recover", update.reason),
        bool(update.stop),
    )


def _overclock_and_update(
    source_plan: list[dict],
    *,
    hooks: AutoUv2SweepHooks,
    state: AutoUv2SweepState,
    candidate: AutoUvCurveCandidate,
    reason: str,
    start_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
    preserve_base_below_mv: int | None,
    min_search_voltage_mv: int,
    measured_clock_cap_mhz: float | None,
    initial_core_clock_mhz: float | None,
    min_core_clock_pct: float,
    stable_history: list[AutoUvProbeSummary],
    probe_history: list[AutoUvProbeSummary],
    attempt_index: int,
) -> tuple[
    AutoUv2SweepState,
    AutoUvCurveCandidate | None,
    AutoUvProbeSummary | None,
    list[AutoUv2SweepEvent],
    bool,
]:
    events: list[AutoUv2SweepEvent] = []
    current_state = state
    current_candidate = candidate

    # Low-clock recovery may need several overclock steps within budget.
    while True:
        attempt = make_overclock_attempt(
            source_plan,
            state=current_state,
            failed_candidate=current_candidate,
            reason=reason,
            cap_clock_mhz=measured_clock_cap_mhz or current_state.stable_target_mhz,
            baseline_clock_mhz=initial_core_clock_mhz,
            max_clock_drop_pct=max(0.0, 100.0 - float(min_core_clock_pct)),
        )
        if attempt is None:
            events.append(AutoUv2SweepEvent("stop", "overclock budget exhausted"))
            return current_state, None, None, events, True

        current_state = attempt.state
        current_candidate = attempt.candidate
        events.append(
            AutoUv2SweepEvent(
                "overclock",
                f"{attempt.old_target_mhz}->{attempt.candidate.target_clock_mhz}MHz",
            )
        )

        previous_probe = stable_history[-1] if stable_history else None
        probe, probe_result = hooks.probe_candidate(attempt.candidate)
        probe_history.append(probe)
        evaluation_error = probe_evaluation_error(
            hooks,
            probe=probe,
            probe_result=probe_result,
            stable_history=stable_history,
        )
        decision = classify_probe_result(
            probe_success=probe_success(probe_result),
            probe_failure_reason=(
                None if probe_success(probe_result) else probe_reason(probe_result)
            ),
            evaluation_error=evaluation_error,
            budget=current_state.budget,
            candidate_used_overclock=True,
        )
        events.append(AutoUv2SweepEvent("decision", decision.action))
        if hooks.log_probe_result is not None:
            hooks.log_probe_result(
                int(attempt_index),
                decision.action,
                decision.reason,
                probe,
                previous_probe,
            )

        if decision.action == "try-overclock":
            reason = evaluation_error or probe_reason(probe_result) or reason
            continue
        if decision.action == "accept":
            accepted_candidate, measured_candidate = accepted_candidate_pair(
                hooks,
                probed_candidate=attempt.candidate,
                probe=probe,
                uses_overclock=True,
            )
            update = apply_probe_decision(
                source_plan,
                state=current_state,
                decision=decision,
                candidate=accepted_candidate,
                probe=probe,
                start_voltage_mv=int(start_voltage_mv),
                reference_actual_voltage_mv=reference_actual_voltage_mv,
                preserve_base_below_mv=preserve_base_below_mv,
                min_search_voltage_mv=int(min_search_voltage_mv),
                probed_candidate=attempt.candidate,
                candidate_used_new_overclock=True,
                measured_target_mhz=int(measured_candidate.target_clock_mhz),
            )
            if update.write_latest_verified:
                hooks.write_latest_verified(accepted_candidate, probe)
            events.append(AutoUv2SweepEvent("state", update.reason))
            return update.state, accepted_candidate, probe, events, bool(update.stop)

        if decision.should_back_off_overclock:
            backed_off = step_back_overclock_target(
                source_plan,
                current_target_mhz=int(current_candidate.target_clock_mhz),
                last_overclock_target_mhz=current_state.last_overclock_target_mhz,
            )
            if backed_off is not None:
                next_full_budget_target_mhz = current_state.full_budget_target_mhz
                if (
                    decision.action == "recover-upward"
                    and next_full_budget_target_mhz is not None
                    and int(current_candidate.target_clock_mhz)
                    == int(next_full_budget_target_mhz)
                ):
                    next_full_budget_target_mhz = int(backed_off)
                current_state = replace(
                    current_state,
                    last_overclock_target_mhz=int(backed_off),
                    full_budget_target_mhz=next_full_budget_target_mhz,
                )
                events.append(
                    AutoUv2SweepEvent(
                        "overclock-backoff",
                        f"{current_candidate.target_clock_mhz}->{int(backed_off)}MHz",
                    )
                )
        events.append(AutoUv2SweepEvent("stop", decision.reason))
        return current_state, None, None, events, True


def run_sweep(
    source_plan: list[dict],
    *,
    initial_state: AutoUv2SweepState,
    stable_candidate: AutoUvCurveCandidate,
    stable_probe: AutoUvProbeSummary,
    stable_history: list[AutoUvProbeSummary],
    probe_history: list[AutoUvProbeSummary],
    start_voltage_mv: int,
    initial_core_clock_mhz: float | None,
    min_core_clock_pct: float,
    measured_clock_cap_mhz: float | None,
    reference_actual_voltage_mv: float | None,
    preserve_base_below_mv: int | None,
    min_search_voltage_mv: int,
    hooks: AutoUv2SweepHooks,
    auto_uv_mode: str = AUTO_UV_MODE_EFFICIENCY,
    efficiency_stop_streak: int = 0,
    min_efficiency_stop_voltage_drop_pct: float = 0.0,
    max_attempts: int = 128,
) -> AutoUv2SweepResult:
    state = initial_state
    events: list[AutoUv2SweepEvent] = []
    seen_voltages: set[int] = set()
    behavior = make_auto_uv_sweep_behavior(
        auto_uv_mode,
        efficiency_stop_streak=int(efficiency_stop_streak),
        min_efficiency_stop_voltage_drop_pct=float(
            min_efficiency_stop_voltage_drop_pct
        ),
    )

    for attempt in range(1, int(max_attempts) + 1):
        # Loop guards are safety rails; voltage selection should end first.
        if state.candidate_voltage_mv is None:
            events.append(AutoUv2SweepEvent("stop", "no lower voltage bin"))
            break
        if int(state.candidate_voltage_mv) in seen_voltages:
            events.append(AutoUv2SweepEvent("stop", "candidate voltage repeated"))
            break
        seen_voltages.add(int(state.candidate_voltage_mv))

        choice = choose_next_candidate(
            source_plan,
            state=state,
            start_voltage_mv=int(start_voltage_mv),
            stable_history=stable_history,
            initial_core_clock_mhz=initial_core_clock_mhz,
            min_core_clock_pct=float(min_core_clock_pct),
            measured_clock_cap_mhz=measured_clock_cap_mhz,
        )
        if choice is None:
            events.append(AutoUv2SweepEvent("stop", "candidate selection ended"))
            break
        state = choice.state
        if hooks.candidate_block_reason is not None:
            block_reason = str(hooks.candidate_block_reason(choice.candidate) or "")
            if block_reason:
                events.append(
                    AutoUv2SweepEvent(
                        "skip",
                        f"{choice.candidate.candidate_voltage_mv}mV@"
                        f"{choice.candidate.target_clock_mhz}MHz blocked by "
                        f"{block_reason}",
                    )
                )
                next_voltage_mv = _next_search_candidate_voltage_mv(
                    plan=source_plan,
                    start_voltage_mv=int(start_voltage_mv),
                    stable_voltage_mv=int(choice.candidate.candidate_voltage_mv),
                    reference_actual_voltage_mv=reference_actual_voltage_mv,
                    preserve_base_below_mv=preserve_base_below_mv,
                    min_search_voltage_mv=int(min_search_voltage_mv),
                )
                state = replace(
                    state,
                    candidate_voltage_mv=next_voltage_mv,
                    last_overclock_target_mhz=None,
                    pending_measured_target_mhz=None,
                )
                if next_voltage_mv is None:
                    events.append(AutoUv2SweepEvent("stop", "no unblocked lower voltage bin"))
                    break
                continue
        events.append(
            AutoUv2SweepEvent(
                "probe",
                f"{attempt}: {choice.candidate.candidate_voltage_mv}mV@"
                f"{choice.candidate.target_clock_mhz}MHz",
            )
        )

        previous_probe_for_table = stable_history[-1] if stable_history else stable_probe
        probe, probe_result = hooks.probe_candidate(choice.candidate)
        probe_history.append(probe)
        evaluation_error = probe_evaluation_error(
            hooks,
            probe=probe,
            probe_result=probe_result,
            stable_history=stable_history,
        )
        decision = classify_probe_result(
            probe_success=probe_success(probe_result),
            probe_failure_reason=(
                None if probe_success(probe_result) else probe_reason(probe_result)
            ),
            evaluation_error=evaluation_error,
            budget=state.budget,
            candidate_used_overclock=state_uses_overclock(state),
        )
        events.append(AutoUv2SweepEvent("decision", decision.action))
        if hooks.log_probe_result is not None:
            hooks.log_probe_result(
                int(attempt),
                decision.action,
                decision.reason,
                probe,
                previous_probe_for_table,
            )

        if decision.action == "try-overclock":
            (
                state,
                overclock_candidate,
                overclock_probe,
                overclock_events,
                stop,
            ) = _overclock_and_update(
                source_plan,
                hooks=hooks,
                state=state,
                candidate=choice.candidate,
                reason=probe_reason(probe_result) or decision.reason,
                start_voltage_mv=int(start_voltage_mv),
                reference_actual_voltage_mv=reference_actual_voltage_mv,
                preserve_base_below_mv=preserve_base_below_mv,
                min_search_voltage_mv=int(min_search_voltage_mv),
                measured_clock_cap_mhz=measured_clock_cap_mhz,
                initial_core_clock_mhz=initial_core_clock_mhz,
                min_core_clock_pct=float(min_core_clock_pct),
                stable_history=stable_history,
                probe_history=probe_history,
                attempt_index=int(attempt),
            )
            events.extend(overclock_events)
            if overclock_candidate is not None and overclock_probe is not None:
                stable_probe = overclock_probe
                stable_candidate = overclock_candidate
                stable_history.append(overclock_probe)
            if not stop:
                continue
            break

        if decision.action == "recover-upward":
            if decision.should_back_off_overclock:
                state, backed_off = _roll_back_full_budget_target_after_hard_failure(
                    source_plan,
                    state=state,
                    failed_target_mhz=int(choice.candidate.target_clock_mhz),
                )
                if backed_off is not None:
                    events.append(
                        AutoUv2SweepEvent(
                            "overclock-backoff",
                            f"{choice.candidate.target_clock_mhz}->{int(backed_off)}MHz",
                        )
                    )
            (
                state,
                recovered_candidate,
                recovered_probe,
                event,
                stop,
            ) = _recover_and_update(
                source_plan,
                hooks=hooks,
                state=state,
                candidate=choice.candidate,
                probe=probe,
                reason=decision.reason,
                start_voltage_mv=int(start_voltage_mv),
                reference_actual_voltage_mv=reference_actual_voltage_mv,
                preserve_base_below_mv=preserve_base_below_mv,
                min_search_voltage_mv=int(min_search_voltage_mv),
            )
            events.append(event)
            if recovered_candidate is not None and recovered_probe is not None:
                stable_probe = recovered_probe
                stable_candidate = recovered_candidate
                stable_history.append(recovered_probe)
            if stop:
                break
            continue

        candidate_for_update = choice.candidate
        measured_candidate_for_update = choice.candidate
        choice_uses_overclock = state_uses_overclock(state)
        if decision.action in {"accept", "accept-lowest-floor-miss"}:
            candidate_for_update, measured_candidate_for_update = (
                accepted_candidate_pair(
                    hooks,
                    probed_candidate=choice.candidate,
                    probe=probe,
                    uses_overclock=choice_uses_overclock,
                )
            )
        update = apply_probe_decision(
            source_plan,
            state=state,
            decision=decision,
            candidate=candidate_for_update,
            probe=probe,
            start_voltage_mv=int(start_voltage_mv),
            reference_actual_voltage_mv=reference_actual_voltage_mv,
            preserve_base_below_mv=preserve_base_below_mv,
            min_search_voltage_mv=int(min_search_voltage_mv),
            probed_candidate=choice.candidate,
            candidate_used_new_overclock=state.last_overclock_target_mhz is not None,
            measured_target_mhz=int(measured_candidate_for_update.target_clock_mhz),
        )
        state = update.state
        events.append(AutoUv2SweepEvent("state", update.reason))
        if update.write_latest_verified:
            hooks.write_latest_verified(candidate_for_update, probe)
        if decision.action in {"accept", "accept-lowest-floor-miss"}:
            previous_stable_candidate = stable_candidate
            previous_stable_probe = stable_probe
            stable_probe = probe
            stable_candidate = candidate_for_update
            stable_history.append(probe)
            behavior_result = behavior.after_stable_acceptance(
                AutoUvAcceptedCandidateContext(
                    source_plan=source_plan,
                    hooks=hooks,
                    state=state,
                    previous_stable_candidate=previous_stable_candidate,
                    previous_stable_probe=previous_stable_probe,
                    stable_candidate=stable_candidate,
                    stable_probe=stable_probe,
                    probed_candidate=choice.candidate,
                    stable_history=stable_history,
                    probe_history=probe_history,
                    start_voltage_mv=int(start_voltage_mv),
                    reference_actual_voltage_mv=reference_actual_voltage_mv,
                    preserve_base_below_mv=preserve_base_below_mv,
                    min_search_voltage_mv=int(min_search_voltage_mv),
                    measured_clock_cap_mhz=measured_clock_cap_mhz,
                    initial_core_clock_mhz=initial_core_clock_mhz,
                    min_core_clock_pct=float(min_core_clock_pct),
                    attempt_index=int(attempt),
                )
            )
            state = behavior_result.state
            stable_candidate = behavior_result.stable_candidate
            stable_probe = behavior_result.stable_probe
            events.extend(behavior_result.events)
            if behavior_result.should_continue:
                continue
            if behavior_result.should_stop:
                break
        if update.stop:
            break
    else:
        events.append(AutoUv2SweepEvent("stop", "max attempts reached"))

    stop_reason = events[-1].message if events else "no events"
    return AutoUv2SweepResult(
        state=state,
        stable_candidate=stable_candidate,
        stable_probe=stable_probe,
        stable_history=stable_history,
        probe_history=probe_history,
        events=events,
        stop_reason=stop_reason,
    )


def static_probe_result(success: bool, reason: str = "passed") -> object:
    return SimpleNamespace(success=bool(success), reason=str(reason))
