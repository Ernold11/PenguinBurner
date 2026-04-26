from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Callable

from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary

from .overclock_recovery import make_overclock_attempt, step_back_overclock_target
from .candidate_decision import AutoUv2SweepState, choose_next_candidate
from .probe_decision import classify_probe_result
from .stop_decision import decide_efficiency_stop
from .sweep_state import apply_probe_decision


@dataclass(frozen=True, slots=True)
class AutoUv2SweepEvent:
    name: str
    message: str


@dataclass(frozen=True, slots=True)
class AutoUv2SweepResult:
    state: AutoUv2SweepState
    stable_candidate: AutoUvCurveCandidate
    stable_probe: AutoUvProbeSummary
    stable_history: list[AutoUvProbeSummary]
    probe_history: list[AutoUvProbeSummary]
    events: list[AutoUv2SweepEvent]
    stop_reason: str


@dataclass(frozen=True, slots=True)
class AutoUv2SweepHooks:
    probe_candidate: Callable[[AutoUvCurveCandidate], tuple[AutoUvProbeSummary, object]]
    evaluate_probe: Callable[[AutoUvProbeSummary, list[AutoUvProbeSummary]], str]
    recover_upward: Callable[
        [AutoUvCurveCandidate, AutoUvProbeSummary, str],
        tuple[AutoUvCurveCandidate | None, AutoUvProbeSummary | None, object | None],
    ]
    write_latest_verified: Callable[[AutoUvCurveCandidate, AutoUvProbeSummary], None]
    normalize_accepted_candidate: Callable[
        [AutoUvCurveCandidate, AutoUvProbeSummary], AutoUvCurveCandidate
    ] | None = None
    efficiency_delta: Callable[
        [AutoUvProbeSummary, AutoUvProbeSummary], dict
    ] | None = None
    power_up_efficiency_down: Callable[
        [AutoUvProbeSummary, AutoUvProbeSummary, dict], bool
    ] | None = None
    log_probe_result: Callable[
        [int, str, str, AutoUvProbeSummary, AutoUvProbeSummary | None], None
    ] | None = None


def _probe_success(result: object) -> bool:
    return bool(getattr(result, "success", False))


def _probe_reason(result: object) -> str | None:
    value = getattr(result, "reason", None)
    return str(value) if value is not None else None


def _candidate_uses_overclock(
    state: AutoUv2SweepState,
    candidate: AutoUvCurveCandidate,
) -> bool:
    return (
        state.last_overclock_target_mhz is not None
        and int(candidate.target_clock_mhz) >= int(state.last_overclock_target_mhz)
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
    preserve_vanilla_below_mv: int | None,
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
        and _probe_success(recovery_result)
    )
    if not recovered:
        return (
            state,
            None,
            None,
            AutoUv2SweepEvent("stop", "recovery failed; keeping previous stable curve"),
            True,
        )

    if hooks.normalize_accepted_candidate is not None:
        recovery_candidate = hooks.normalize_accepted_candidate(
            recovery_candidate,
            recovery_probe,
        )

    update = apply_probe_decision(
        source_plan,
        state=state,
        decision=classify_probe_result(
            probe_success=True,
            probe_failure_reason=None,
            evaluation_error=None,
            budget=state.budget,
            candidate_used_overclock=state.last_overclock_target_mhz is not None,
        ),
        candidate=recovery_candidate,
        probe=recovery_probe,
        start_voltage_mv=int(start_voltage_mv),
        reference_actual_voltage_mv=reference_actual_voltage_mv,
        preserve_vanilla_below_mv=preserve_vanilla_below_mv,
        min_search_voltage_mv=int(min_search_voltage_mv),
        recovered_voltage_mv=int(recovery_candidate.candidate_voltage_mv),
        recovered_target_mhz=int(recovery_candidate.target_clock_mhz),
    )
    if update.write_latest_verified:
        hooks.write_latest_verified(recovery_candidate, recovery_probe)
    return (
        update.state,
        recovery_candidate,
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
    preserve_vanilla_below_mv: int | None,
    min_search_voltage_mv: int,
    measured_clock_cap_mhz: float | None,
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
        evaluation_error = (
            "" if not _probe_success(probe_result) else hooks.evaluate_probe(probe, stable_history)
        )
        decision = classify_probe_result(
            probe_success=_probe_success(probe_result),
            probe_failure_reason=(
                None if _probe_success(probe_result) else _probe_reason(probe_result)
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
            reason = evaluation_error or _probe_reason(probe_result) or reason
            continue
        if decision.action == "accept":
            accepted_candidate = attempt.candidate
            update = apply_probe_decision(
                source_plan,
                state=current_state,
                decision=decision,
                candidate=accepted_candidate,
                probe=probe,
                start_voltage_mv=int(start_voltage_mv),
                reference_actual_voltage_mv=reference_actual_voltage_mv,
                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
                min_search_voltage_mv=int(min_search_voltage_mv),
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
                current_state = replace(
                    current_state,
                    last_overclock_target_mhz=int(backed_off),
                )
                events.append(
                    AutoUv2SweepEvent(
                        "overclock-backoff",
                        f"{current_candidate.target_clock_mhz}->{int(backed_off)}MHz",
                    )
                )
        events.append(AutoUv2SweepEvent("stop", decision.reason))
        return current_state, None, None, events, True


def _efficiency_overclock(
    source_plan: list[dict],
    *,
    hooks: AutoUv2SweepHooks,
    state: AutoUv2SweepState,
    stable_candidate: AutoUvCurveCandidate,
    stable_probe: AutoUvProbeSummary,
    start_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
    preserve_vanilla_below_mv: int | None,
    min_search_voltage_mv: int,
    measured_clock_cap_mhz: float | None,
    stable_history: list[AutoUvProbeSummary],
    probe_history: list[AutoUvProbeSummary],
    attempt_index: int,
) -> tuple[AutoUv2SweepState, AutoUvCurveCandidate | None, AutoUvProbeSummary | None, list[AutoUv2SweepEvent]]:
    events: list[AutoUv2SweepEvent] = []
    # FPS/W walls get one overclock chance before stopping.
    attempt = make_overclock_attempt(
        source_plan,
        state=state,
        failed_candidate=stable_candidate,
        reason="efficiency-wall",
        cap_clock_mhz=measured_clock_cap_mhz or state.stable_target_mhz,
    )
    if attempt is None:
        events.append(AutoUv2SweepEvent("efficiency-overclock", "no budget"))
        return state, None, None, events

    events.append(
        AutoUv2SweepEvent(
            "efficiency-overclock",
            f"{attempt.old_target_mhz}->{attempt.candidate.target_clock_mhz}MHz",
        )
    )
    previous_probe = stable_history[-1] if stable_history else stable_probe
    probe, probe_result = hooks.probe_candidate(attempt.candidate)
    probe_history.append(probe)
    evaluation_error = (
        "" if not _probe_success(probe_result) else hooks.evaluate_probe(probe, stable_history)
    )
    decision = classify_probe_result(
        probe_success=_probe_success(probe_result),
        probe_failure_reason=(
            None if _probe_success(probe_result) else _probe_reason(probe_result)
        ),
        evaluation_error=evaluation_error,
        budget=attempt.state.budget,
        candidate_used_overclock=True,
    )
    if hooks.log_probe_result is not None:
        hooks.log_probe_result(
            int(attempt_index),
            decision.action,
            decision.reason,
            probe,
            previous_probe,
        )
    if decision.action != "accept" or hooks.efficiency_delta is None:
        events.append(AutoUv2SweepEvent("efficiency-overclock", "rejected"))
        return attempt.state, None, None, events

    efficiency_delta = hooks.efficiency_delta(stable_probe, probe)
    if efficiency_delta.get("improved") is not True:
        events.append(AutoUv2SweepEvent("efficiency-overclock", "no-efficiency-gain"))
        return attempt.state, None, None, events

    accepted_candidate = attempt.candidate
    update = apply_probe_decision(
        source_plan,
        state=attempt.state,
        decision=decision,
        candidate=accepted_candidate,
        probe=probe,
        start_voltage_mv=int(start_voltage_mv),
        reference_actual_voltage_mv=reference_actual_voltage_mv,
        preserve_vanilla_below_mv=preserve_vanilla_below_mv,
        min_search_voltage_mv=int(min_search_voltage_mv),
    )
    if update.write_latest_verified:
        hooks.write_latest_verified(accepted_candidate, probe)
    events.append(AutoUv2SweepEvent("efficiency-overclock", "accepted"))
    return update.state, accepted_candidate, probe, events


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
    preserve_vanilla_below_mv: int | None,
    min_search_voltage_mv: int,
    hooks: AutoUv2SweepHooks,
    efficiency_stop_streak: int = 0,
    min_efficiency_stop_voltage_drop_pct: float = 0.0,
    max_attempts: int = 128,
) -> AutoUv2SweepResult:
    state = initial_state
    events: list[AutoUv2SweepEvent] = []
    seen_voltages: set[int] = set()
    no_gain_streak = 0
    pending_stop_candidate: AutoUvCurveCandidate | None = None
    pending_stop_probe: AutoUvProbeSummary | None = None

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
        evaluation_error = (
            "" if not _probe_success(probe_result) else hooks.evaluate_probe(probe, stable_history)
        )
        decision = classify_probe_result(
            probe_success=_probe_success(probe_result),
            probe_failure_reason=(
                None if _probe_success(probe_result) else _probe_reason(probe_result)
            ),
            evaluation_error=evaluation_error,
            budget=state.budget,
            candidate_used_overclock=state.last_overclock_target_mhz is not None,
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
                reason=_probe_reason(probe_result) or decision.reason,
                start_voltage_mv=int(start_voltage_mv),
                reference_actual_voltage_mv=reference_actual_voltage_mv,
                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
                min_search_voltage_mv=int(min_search_voltage_mv),
                measured_clock_cap_mhz=measured_clock_cap_mhz,
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
                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
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
        if (
            hooks.normalize_accepted_candidate is not None
            and decision.action in {"accept", "accept-lowest-floor-miss"}
            and not _candidate_uses_overclock(state, choice.candidate)
        ):
            candidate_for_update = hooks.normalize_accepted_candidate(
                choice.candidate,
                probe,
            )
        update = apply_probe_decision(
            source_plan,
            state=state,
            decision=decision,
            candidate=candidate_for_update,
            probe=probe,
            start_voltage_mv=int(start_voltage_mv),
            reference_actual_voltage_mv=reference_actual_voltage_mv,
            preserve_vanilla_below_mv=preserve_vanilla_below_mv,
            min_search_voltage_mv=int(min_search_voltage_mv),
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
            if hooks.efficiency_delta is not None and int(efficiency_stop_streak) > 0:
                # Efficiency stop is evaluated only after a stable candidate.
                delta = hooks.efficiency_delta(previous_stable_probe, probe)
                improved = delta.get("improved")
                power_regression = (
                    hooks.power_up_efficiency_down(
                        previous_stable_probe,
                        probe,
                        delta,
                    )
                    if hooks.power_up_efficiency_down is not None
                    else False
                )
                measured_close = bool(delta.get("measured_voltage_close_to_requested"))
                voltage_drop_pct = (
                    (
                        (float(start_voltage_mv) - float(choice.candidate.candidate_voltage_mv))
                        / float(start_voltage_mv)
                    )
                    * 100.0
                    if int(start_voltage_mv) > 0
                    else 0.0
                )
                stop_candidate = (
                    (improved is False or bool(power_regression)) and measured_close
                )
                if stop_candidate:
                    no_gain_streak += 1
                    if pending_stop_candidate is None:
                        pending_stop_candidate = previous_stable_candidate
                        pending_stop_probe = previous_stable_probe
                elif improved is True:
                    no_gain_streak = 0
                    pending_stop_candidate = None
                    pending_stop_probe = None
                if stop_candidate and not state.budget.spent_or_disabled:
                    (
                        state,
                        efficiency_candidate,
                        efficiency_probe,
                        efficiency_events,
                    ) = _efficiency_overclock(
                        source_plan,
                        hooks=hooks,
                        state=state,
                        stable_candidate=stable_candidate,
                        stable_probe=stable_probe,
                        start_voltage_mv=int(start_voltage_mv),
                        reference_actual_voltage_mv=reference_actual_voltage_mv,
                        preserve_vanilla_below_mv=preserve_vanilla_below_mv,
                        min_search_voltage_mv=int(min_search_voltage_mv),
                        measured_clock_cap_mhz=measured_clock_cap_mhz,
                        stable_history=stable_history,
                        probe_history=probe_history,
                        attempt_index=int(attempt),
                    )
                    events.extend(efficiency_events)
                    if efficiency_candidate is not None and efficiency_probe is not None:
                        stable_candidate = efficiency_candidate
                        stable_probe = efficiency_probe
                        stable_history.append(efficiency_probe)
                        no_gain_streak = 0
                        pending_stop_candidate = None
                        pending_stop_probe = None
                        continue
                stop_decision = decide_efficiency_stop(
                    efficiency_stop_candidate=bool(stop_candidate),
                    voltage_drop_from_start_pct=float(voltage_drop_pct),
                    min_voltage_drop_pct=float(min_efficiency_stop_voltage_drop_pct),
                    no_gain_streak=int(no_gain_streak),
                    required_extra_confirmations=int(efficiency_stop_streak),
                    pending_previous_curve=pending_stop_candidate is not None,
                    budget=state.budget,
                    power_up_efficiency_down=bool(power_regression),
                    efficiency_delta_pct=delta.get("delta_pct"),
                )
                if stop_decision.should_stop:
                    events.append(
                        AutoUv2SweepEvent("stop", stop_decision.reason)
                    )
                    if (
                        not stop_decision.use_current_curve
                        and pending_stop_candidate is not None
                        and pending_stop_probe is not None
                    ):
                        stable_candidate = pending_stop_candidate
                        stable_probe = pending_stop_probe
                        state = replace(
                            state,
                            stable_voltage_mv=int(stable_candidate.candidate_voltage_mv),
                            stable_target_mhz=int(stable_candidate.target_clock_mhz),
                            candidate_voltage_mv=None,
                        )
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
