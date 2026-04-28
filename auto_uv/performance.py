from __future__ import annotations

from dataclasses import dataclass, replace

from auto_uv.clock_bump import (
    _make_clock_bump_candidate as _make_overclock_candidate,
)
from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary

from .candidate_decision import (
    AutoUv2SweepState,
    _fix_full_budget_target,
    charged_overclock_budget_pct,
    next_overclock_budget_used_pct,
    overclock_budget_pct_for_target,
    overclock_budget_snap_tolerance_pct,
    overclock_recovery_target_mhz,
)
from .curve_planning import _make_curve_candidate
from .overclock_recovery import AutoUv2OverclockAttempt
from .probe_decision import classify_probe_result
from .sweep_behavior import (
    AutoUv2SweepEvent,
    AutoUvAcceptedCandidateContext,
    AutoUvBehaviorResult,
    accepted_candidate_pair,
    probe_evaluation_error,
    probe_reason,
    probe_success,
)
from .sweep_state import apply_probe_decision
from .tuning import AUTO_UV_CURVE_TUNING


AUTO_UV_MODE_PERFORMANCE = "performance"


def performance_score_from_values(
    *,
    fps: float | None,
    base_fps: float | None,
    fps_per_w: float | None,
    base_fps_per_w: float | None,
    core_clock_mhz: float | None,
    base_core_clock_mhz: float | None,
) -> float | None:
    _ = core_clock_mhz, base_core_clock_mhz
    if fps is None or base_fps is None or float(fps) <= 0.0 or float(base_fps) <= 0.0:
        return None
    fps_ratio = float(fps) / float(base_fps)
    fps_per_w_ratio = _ratio_or_one(fps_per_w, base_fps_per_w)
    fps_weight = 8.0 if fps_ratio < 1.0 else 3.0
    return (
        100.0
        * min(max(fps_ratio, 0.0), 1.10) ** fps_weight
        * min(max(fps_per_w_ratio, 0.0), 1.45) ** 0.35
    )


def performance_score(
    probe: AutoUvProbeSummary,
    *,
    base_probe: AutoUvProbeSummary | None,
) -> float | None:
    if base_probe is None:
        return None
    return performance_score_from_values(
        fps=probe.avg_fps,
        base_fps=base_probe.avg_fps,
        fps_per_w=probe.efficiency_fps_per_w,
        base_fps_per_w=base_probe.efficiency_fps_per_w,
        core_clock_mhz=probe.avg_core_clock_mhz,
        base_core_clock_mhz=base_probe.avg_core_clock_mhz,
    )


def candidate_performance_score(
    candidate: dict,
    *,
    base_probe: AutoUvProbeSummary | None,
) -> float | None:
    if base_probe is None:
        return None
    return performance_score_from_values(
        fps=_float_or_none(candidate.get("avg_fps")),
        base_fps=base_probe.avg_fps,
        fps_per_w=_float_or_none(candidate.get("efficiency_fps_per_w")),
        base_fps_per_w=base_probe.efficiency_fps_per_w,
        core_clock_mhz=None,
        base_core_clock_mhz=None,
    )


def annotate_performance_candidate_scores(
    candidates: list[dict],
    *,
    base_probe: AutoUvProbeSummary | None,
) -> None:
    for candidate in candidates:
        candidate["performance_score"] = candidate_performance_score(
            candidate,
            base_probe=base_probe,
        )


def performance_candidate_sort_key(
    candidate: dict,
    *,
    base_probe: AutoUvProbeSummary | None,
) -> tuple[bool, float, float, int, int]:
    score = candidate_performance_score(candidate, base_probe=base_probe)
    fps = _float_or_none(candidate.get("avg_fps"))
    return (
        score is None,
        -float(score or 0.0),
        -float(fps or 0.0),
        int(candidate.get("candidate_voltage_mv") or 99999),
        -int(candidate.get("lock_clock_mhz") or 0),
    )


def _memory_offset_key(item: object) -> int:
    try:
        return int(getattr(item, "memory_offset_mhz", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _candidate_probe_point_key(candidate: AutoUvCurveCandidate) -> tuple[int, int, int]:
    return (
        int(candidate.candidate_voltage_mv),
        int(candidate.target_clock_mhz),
        _memory_offset_key(candidate),
    )


def _probe_point_key(probe: AutoUvProbeSummary) -> tuple[int, int, int]:
    return (
        int(probe.candidate_voltage_mv),
        int(probe.lock_clock_mhz),
        _memory_offset_key(probe),
    )


def _visited_probe_points(
    context: AutoUvAcceptedCandidateContext,
) -> set[tuple[int, int, int]]:
    return {
        _probe_point_key(probe)
        for probe in [*context.probe_history, *context.stable_history]
    }


@dataclass(slots=True)
class AutoUvPerformanceBehavior:
    name: str = AUTO_UV_MODE_PERFORMANCE
    min_exploration_steps: int = 4
    max_no_score_gain_steps: int = 3
    min_score_gain_ratio: float = 0.005
    min_overclock_score_gain_ratio: float = 0.002
    fps_recovery_clock_floor_ratio: float = 0.98
    max_overclock_step_pct: float = 4.5
    max_voltage_recovery_mv: int = 60
    max_voltage_recovery_probes: int = 4

    best_candidate: AutoUvCurveCandidate | None = None
    best_probe: AutoUvProbeSummary | None = None
    best_score: float | None = None
    no_score_gain_steps: int = 0

    def after_stable_acceptance(
        self,
        context: AutoUvAcceptedCandidateContext,
    ) -> AutoUvBehaviorResult:
        base_probe = context.hooks.base_probe
        if base_probe is None:
            return self._unchanged(context)

        self._seed_best_candidate(context, base_probe=base_probe)
        current_score = performance_score(context.stable_probe, base_probe=base_probe)
        events: list[AutoUv2SweepEvent] = []

        improved = self._record_if_best(
            context.stable_candidate,
            context.stable_probe,
            current_score,
        )
        if improved:
            self.no_score_gain_steps = 0
        else:
            self.no_score_gain_steps += 1

        if (
            not improved
            and current_score is not None
            and not context.state.budget.spent_or_disabled
        ):
            (
                overclock_result,
                overclock_score,
                overclock_events,
            ) = self._try_performance_overclock(
                context,
                current_score=float(current_score),
                base_probe=base_probe,
            )
            events.extend(overclock_events)
            if overclock_result is not None:
                if overclock_score is not None:
                    self.no_score_gain_steps = 0
                return AutoUvBehaviorResult(
                    state=overclock_result.state,
                    stable_candidate=overclock_result.stable_candidate,
                    stable_probe=overclock_result.stable_probe,
                    events=events,
                    should_continue=overclock_score is not None,
                    should_stop=overclock_score is None
                    and bool(overclock_result.should_stop),
                )

        if self._should_stop(context):
            recovery_result, recovery_events = self._sweep_higher_voltage_recovery(
                context,
                base_probe=base_probe,
            )
            events.extend(recovery_events)
            if recovery_result is not None:
                events.append(
                    AutoUv2SweepEvent("stop", "performance score wall reached")
                )
                return AutoUvBehaviorResult(
                    state=recovery_result.state,
                    stable_candidate=recovery_result.stable_candidate,
                    stable_probe=recovery_result.stable_probe,
                    events=events,
                    should_stop=True,
                )
            best_candidate = self.best_candidate or context.stable_candidate
            best_probe = self.best_probe or context.stable_probe
            state = replace(
                context.state,
                stable_voltage_mv=int(best_candidate.candidate_voltage_mv),
                stable_target_mhz=int(best_candidate.target_clock_mhz),
                candidate_voltage_mv=None,
            )
            events.append(
                AutoUv2SweepEvent(
                    "stop",
                    "performance score wall reached",
                )
            )
            return AutoUvBehaviorResult(
                state=state,
                stable_candidate=best_candidate,
                stable_probe=best_probe,
                events=events,
                should_stop=True,
            )

        return AutoUvBehaviorResult(
            state=context.state,
            stable_candidate=context.stable_candidate,
            stable_probe=context.stable_probe,
            events=events,
        )

    def _sweep_higher_voltage_recovery(
        self,
        context: AutoUvAcceptedCandidateContext,
        *,
        base_probe: AutoUvProbeSummary,
    ) -> tuple[AutoUvBehaviorResult | None, list[AutoUv2SweepEvent]]:
        events: list[AutoUv2SweepEvent] = []
        current_voltage_mv = int(context.stable_candidate.candidate_voltage_mv)
        recovery_cap_mv = int(self.max_voltage_recovery_mv)
        max_voltage_mv = int(current_voltage_mv) + max(0, int(recovery_cap_mv))
        recovery_voltages = self._higher_recovery_voltages(
            context,
            current_voltage_mv=int(current_voltage_mv),
            max_voltage_mv=int(max_voltage_mv),
        )
        if not recovery_voltages:
            return None, events

        state = context.state
        accepted_result: AutoUvBehaviorResult | None = None
        visited_points = _visited_probe_points(context)
        probes = 0
        for voltage_mv in recovery_voltages:
            if probes >= int(self.max_voltage_recovery_probes):
                break
            candidate = _make_curve_candidate(
                context.source_plan,
                candidate_voltage_mv=int(voltage_mv),
                target_clock_mhz=max(
                    int(context.stable_candidate.target_clock_mhz),
                    int(
                        (
                            self.best_candidate or context.stable_candidate
                        ).target_clock_mhz
                    ),
                ),
                label=f"performance-voltage-recovery voltage={int(voltage_mv)}mV",
            )
            candidate, state = self._maybe_raise_recovery_target(
                context,
                state=state,
                candidate=candidate,
                base_probe=base_probe,
            )
            point_key = _candidate_probe_point_key(candidate)
            if point_key in visited_points:
                events.append(
                    AutoUv2SweepEvent(
                        "performance-voltage-recovery",
                        "skip-visited "
                        f"{candidate.candidate_voltage_mv}mV@"
                        f"{candidate.target_clock_mhz}MHz",
                    )
                )
                continue
            visited_points.add(point_key)
            events.append(
                AutoUv2SweepEvent(
                    "performance-voltage-recovery",
                    f"{candidate.candidate_voltage_mv}mV@"
                    f"{candidate.target_clock_mhz}MHz",
                )
            )
            previous_probe = (
                context.stable_history[-1] if context.stable_history else None
            )
            probe, probe_result = context.hooks.probe_candidate(candidate)
            context.probe_history.append(probe)
            probes += 1
            evaluation_error = probe_evaluation_error(
                context.hooks,
                probe=probe,
                probe_result=probe_result,
                stable_history=context.stable_history,
            )
            decision = classify_probe_result(
                probe_success=probe_success(probe_result),
                probe_failure_reason=(
                    None if probe_success(probe_result) else probe_reason(probe_result)
                ),
                evaluation_error=evaluation_error,
                budget=state.budget,
                candidate_used_overclock=True,
            )
            if context.hooks.log_probe_result is not None:
                context.hooks.log_probe_result(
                    int(context.attempt_index),
                    decision.action,
                    decision.reason,
                    probe,
                    previous_probe,
                )
            if decision.action != "accept":
                events.append(
                    AutoUv2SweepEvent("performance-voltage-recovery", "rejected")
                )
                break
            score = performance_score(probe, base_probe=base_probe)
            if score is None or not self._record_if_best(candidate, probe, score):
                events.append(
                    AutoUv2SweepEvent("performance-voltage-recovery", "no-score-gain")
                )
                continue

            accepted_candidate, measured_candidate = accepted_candidate_pair(
                context.hooks,
                probed_candidate=candidate,
                probe=probe,
                uses_overclock=True,
            )
            update = apply_probe_decision(
                context.source_plan,
                state=state,
                decision=decision,
                candidate=accepted_candidate,
                probe=probe,
                start_voltage_mv=int(context.start_voltage_mv),
                reference_actual_voltage_mv=context.reference_actual_voltage_mv,
                preserve_base_below_mv=context.preserve_base_below_mv,
                min_search_voltage_mv=int(context.min_search_voltage_mv),
                probed_candidate=candidate,
                candidate_used_new_overclock=state.last_overclock_target_mhz is not None,
                measured_target_mhz=int(measured_candidate.target_clock_mhz),
            )
            if update.write_latest_verified:
                context.hooks.write_latest_verified(accepted_candidate, probe)
            context.stable_history.append(probe)
            state = update.state
            accepted_result = AutoUvBehaviorResult(
                state=replace(state, candidate_voltage_mv=None),
                stable_candidate=accepted_candidate,
                stable_probe=probe,
                events=[],
                should_stop=True,
            )
            events.append(
                AutoUv2SweepEvent("performance-voltage-recovery", "accepted")
            )
        return accepted_result, events

    def _try_performance_overclock(
        self,
        context: AutoUvAcceptedCandidateContext,
        *,
        current_score: float,
        base_probe: AutoUvProbeSummary,
    ) -> tuple[AutoUvBehaviorResult | None, float | None, list[AutoUv2SweepEvent]]:
        events: list[AutoUv2SweepEvent] = []
        current_clock_mhz = (
            float(context.stable_probe.avg_core_clock_mhz)
            if context.stable_probe.avg_core_clock_mhz is not None
            else float(context.stable_candidate.target_clock_mhz)
        )
        baseline_clock_mhz = float(
            context.initial_core_clock_mhz
            or base_probe.avg_core_clock_mhz
            or current_clock_mhz
        )
        desired_floor_mhz = max(
            current_clock_mhz,
            baseline_clock_mhz * float(self.fps_recovery_clock_floor_ratio),
        )
        attempt = self._make_performance_overclock_attempt(
            context.source_plan,
            state=context.state,
            failed_candidate=context.stable_candidate,
            reason=(
                f"performance-fps current={current_clock_mhz:.1f}MHz "
                f"floor={desired_floor_mhz:.1f}MHz"
            ),
            cap_clock_mhz=(
                context.measured_clock_cap_mhz or baseline_clock_mhz
            ),
            baseline_clock_mhz=context.initial_core_clock_mhz,
            max_clock_drop_pct=max(0.0, 100.0 - float(context.min_core_clock_pct)),
        )
        if attempt is None:
            events.append(AutoUv2SweepEvent("performance-overclock", "no budget"))
            return None, None, events

        events.append(
            AutoUv2SweepEvent(
                "performance-overclock",
                f"{attempt.old_target_mhz}->{attempt.candidate.target_clock_mhz}MHz",
            )
        )
        previous_probe = (
            context.stable_history[-1]
            if context.stable_history
            else context.stable_probe
        )
        probe, probe_result = context.hooks.probe_candidate(attempt.candidate)
        context.probe_history.append(probe)
        evaluation_error = probe_evaluation_error(
            context.hooks,
            probe=probe,
            probe_result=probe_result,
            stable_history=context.stable_history,
        )
        decision = classify_probe_result(
            probe_success=probe_success(probe_result),
            probe_failure_reason=(
                None if probe_success(probe_result) else probe_reason(probe_result)
            ),
            evaluation_error=evaluation_error,
            budget=attempt.state.budget,
            candidate_used_overclock=True,
        )
        if context.hooks.log_probe_result is not None:
            context.hooks.log_probe_result(
                int(context.attempt_index),
                decision.action,
                decision.reason,
                probe,
                previous_probe,
            )
        if decision.action != "accept":
            events.append(AutoUv2SweepEvent("performance-overclock", "rejected"))
            if decision.action in {"recover-upward", "stop-critical"} or bool(
                decision.should_back_off_overclock
            ):
                recovery_result, recovery_events = self._sweep_higher_voltage_recovery(
                    context,
                    base_probe=base_probe,
                )
                events.extend(recovery_events)
                if recovery_result is not None:
                    events.append(
                        AutoUv2SweepEvent(
                            "stop",
                            "performance overclock failed; recovered at higher voltage",
                        )
                    )
                    return recovery_result, None, events
                fallback_candidate, fallback_probe = self._higher_voltage_fallback(
                    context,
                    failed_voltage_mv=int(attempt.candidate.candidate_voltage_mv),
                )
                events.append(
                    AutoUv2SweepEvent(
                        "stop",
                        "performance overclock failed; refusing lower voltage",
                    )
                )
                return (
                    AutoUvBehaviorResult(
                        state=replace(
                            context.state,
                            stable_voltage_mv=int(
                                fallback_candidate.candidate_voltage_mv
                            ),
                            stable_target_mhz=int(
                                fallback_candidate.target_clock_mhz
                            ),
                            candidate_voltage_mv=None,
                            last_overclock_target_mhz=None,
                            pending_measured_target_mhz=None,
                        ),
                        stable_candidate=fallback_candidate,
                        stable_probe=fallback_probe,
                        events=[],
                        should_stop=True,
                    ),
                    None,
                    events,
                )
            return None, None, events

        overclock_score = performance_score(probe, base_probe=base_probe)
        if overclock_score is None or float(overclock_score) <= float(
            current_score
        ) * (1.0 + float(self.min_overclock_score_gain_ratio)):
            events.append(AutoUv2SweepEvent("performance-overclock", "no-score-gain"))
            return None, None, events

        accepted_candidate, measured_candidate = accepted_candidate_pair(
            context.hooks,
            probed_candidate=attempt.candidate,
            probe=probe,
            uses_overclock=True,
        )
        update = apply_probe_decision(
            context.source_plan,
            state=attempt.state,
            decision=decision,
            candidate=accepted_candidate,
            probe=probe,
            start_voltage_mv=int(context.start_voltage_mv),
            reference_actual_voltage_mv=context.reference_actual_voltage_mv,
            preserve_base_below_mv=context.preserve_base_below_mv,
            min_search_voltage_mv=int(context.min_search_voltage_mv),
            probed_candidate=attempt.candidate,
            candidate_used_new_overclock=True,
            measured_target_mhz=int(measured_candidate.target_clock_mhz),
        )
        if update.write_latest_verified:
            context.hooks.write_latest_verified(accepted_candidate, probe)
        context.stable_history.append(probe)
        self._record_if_best(accepted_candidate, probe, overclock_score)
        events.append(AutoUv2SweepEvent("performance-overclock", "accepted"))
        return (
            AutoUvBehaviorResult(
                state=update.state,
                stable_candidate=accepted_candidate,
                stable_probe=probe,
                events=[],
            ),
            float(overclock_score),
            events,
        )

    def _higher_voltage_fallback(
        self,
        context: AutoUvAcceptedCandidateContext,
        *,
        failed_voltage_mv: int,
    ) -> tuple[AutoUvCurveCandidate, AutoUvProbeSummary]:
        candidates = [
            (context.previous_stable_candidate, context.previous_stable_probe),
            (self.best_candidate, self.best_probe),
            (context.stable_candidate, context.stable_probe),
        ]
        for candidate, probe in candidates:
            if candidate is None or probe is None:
                continue
            if int(candidate.candidate_voltage_mv) > int(failed_voltage_mv):
                return candidate, probe
        return context.stable_candidate, context.stable_probe

    def _maybe_raise_recovery_target(
        self,
        context: AutoUvAcceptedCandidateContext,
        *,
        state: AutoUv2SweepState,
        candidate: AutoUvCurveCandidate,
        base_probe: AutoUvProbeSummary,
    ) -> tuple[AutoUvCurveCandidate, AutoUv2SweepState]:
        if state.budget.spent_or_disabled:
            return candidate, state
        current_clock_mhz = (
            float(context.stable_probe.avg_core_clock_mhz)
            if context.stable_probe.avg_core_clock_mhz is not None
            else float(candidate.target_clock_mhz)
        )
        baseline_clock_mhz = float(
            context.initial_core_clock_mhz
            or base_probe.avg_core_clock_mhz
            or current_clock_mhz
        )
        attempt = self._make_performance_overclock_attempt(
            context.source_plan,
            state=state,
            failed_candidate=candidate,
            reason=(
                f"performance-voltage current={current_clock_mhz:.1f}MHz "
                f"floor={baseline_clock_mhz * self.fps_recovery_clock_floor_ratio:.1f}MHz"
            ),
            cap_clock_mhz=context.measured_clock_cap_mhz or baseline_clock_mhz,
            baseline_clock_mhz=context.initial_core_clock_mhz,
            max_clock_drop_pct=max(0.0, 100.0 - float(context.min_core_clock_pct)),
        )
        if attempt is None:
            return candidate, state
        return attempt.candidate, attempt.state

    def _make_performance_overclock_attempt(
        self,
        source_plan: list[dict],
        *,
        state: AutoUv2SweepState,
        failed_candidate: AutoUvCurveCandidate,
        reason: str,
        cap_clock_mhz: float,
        baseline_clock_mhz: float | None,
        max_clock_drop_pct: float,
    ) -> AutoUv2OverclockAttempt | None:
        if state.budget.spent_or_disabled:
            return None
        old_target_mhz = max(
            int(failed_candidate.target_clock_mhz),
            int(state.last_overclock_target_mhz or 0),
        )
        measured_target_mhz = self._overclock_measured_target_mhz(
            state,
            failed_candidate=failed_candidate,
        )
        next_used_pct = next_overclock_budget_used_pct(
            current_used_pct=float(state.budget.used_pct),
            limit_pct=float(state.budget.limit_pct),
            reason=str(reason),
            measured_target_mhz=int(measured_target_mhz),
            baseline_clock_mhz=baseline_clock_mhz,
            max_clock_drop_pct=float(max_clock_drop_pct),
        )
        if next_used_pct is None:
            return None

        step_limit_pct = self._overclock_step_limit_pct(state)
        requested_used_pct = min(float(next_used_pct), float(step_limit_pct))
        new_target_mhz = overclock_recovery_target_mhz(
            source_plan,
            measured_target_mhz=int(measured_target_mhz),
            baseline_clock_mhz=baseline_clock_mhz,
            max_clock_drop_pct=float(max_clock_drop_pct),
            budget_used_pct=float(requested_used_pct),
            cap_clock_mhz=float(cap_clock_mhz),
            minimum_target_mhz=int(old_target_mhz)
            + max(1, int(AUTO_UV_CURVE_TUNING.clock_step_mhz)),
        )
        consumed_pct = overclock_budget_pct_for_target(
            measured_target_mhz=int(measured_target_mhz),
            overclock_target_mhz=int(new_target_mhz),
            baseline_clock_mhz=baseline_clock_mhz,
            max_clock_drop_pct=float(max_clock_drop_pct),
        )
        step_tolerance_pct = overclock_budget_snap_tolerance_pct(
            measured_target_mhz=int(measured_target_mhz),
            baseline_clock_mhz=baseline_clock_mhz,
            max_clock_drop_pct=float(max_clock_drop_pct),
        )
        if float(consumed_pct) > float(step_limit_pct) + float(step_tolerance_pct) + 1e-9:
            return None

        charged_pct = charged_overclock_budget_pct(
            consumed_pct=float(consumed_pct),
            requested_used_pct=float(requested_used_pct),
            limit_pct=float(state.budget.limit_pct),
            measured_target_mhz=int(measured_target_mhz),
            baseline_clock_mhz=baseline_clock_mhz,
            max_clock_drop_pct=float(max_clock_drop_pct),
        )
        if int(new_target_mhz) <= int(old_target_mhz) or charged_pct is None:
            return None

        next_budget = state.budget.with_measured_used_pct(float(charged_pct))
        next_state = replace(
            state,
            budget=next_budget,
            last_overclock_target_mhz=int(new_target_mhz),
            overclock_count=int(state.overclock_count) + 1,
        )
        new_target_mhz, next_state = _fix_full_budget_target(
            next_state,
            int(new_target_mhz),
        )
        next_state = replace(next_state, last_overclock_target_mhz=int(new_target_mhz))
        candidate = _make_overclock_candidate(
            source_plan,
            candidate_voltage_mv=int(failed_candidate.candidate_voltage_mv),
            target_clock_mhz=int(new_target_mhz),
            reason_label="low-clock-recovery",
            budget_used_pct=float(next_budget.used_pct),
            budget_limit_pct=float(next_budget.limit_pct),
        )
        return AutoUv2OverclockAttempt(
            candidate=candidate,
            state=next_state,
            old_target_mhz=int(old_target_mhz),
        )

    def _overclock_step_limit_pct(self, state: AutoUv2SweepState) -> float:
        limit_pct = max(0.0, float(state.budget.limit_pct))
        used_pct = max(0.0, float(state.budget.used_pct))
        step_pct = max(0.0, float(self.max_overclock_step_pct))
        if step_pct <= 0.0:
            return float(limit_pct)
        return min(float(limit_pct), float(used_pct) + float(step_pct))

    @staticmethod
    def _overclock_measured_target_mhz(
        state: AutoUv2SweepState,
        *,
        failed_candidate: AutoUvCurveCandidate,
    ) -> int:
        if state.pending_measured_target_mhz is not None:
            return int(state.pending_measured_target_mhz)
        if state.stable_measured_target_mhz is not None:
            return int(state.stable_measured_target_mhz)
        return int(failed_candidate.target_clock_mhz)

    def _seed_best_candidate(
        self,
        context: AutoUvAcceptedCandidateContext,
        *,
        base_probe: AutoUvProbeSummary,
    ) -> None:
        if self.best_candidate is not None and self.best_probe is not None:
            return
        self.best_candidate = context.previous_stable_candidate
        self.best_probe = context.previous_stable_probe
        self.best_score = performance_score(
            context.previous_stable_probe,
            base_probe=base_probe,
        )

    def _record_if_best(
        self,
        candidate: AutoUvCurveCandidate,
        probe: AutoUvProbeSummary,
        score: float | None,
    ) -> bool:
        if score is None:
            return False
        if self.best_score is None or float(score) > float(self.best_score) * (
            1.0 + float(self.min_score_gain_ratio)
        ):
            self.best_candidate = candidate
            self.best_probe = probe
            self.best_score = float(score)
            return True
        return False

    def _should_stop(self, context: AutoUvAcceptedCandidateContext) -> bool:
        if int(context.attempt_index) < int(self.min_exploration_steps):
            return False
        return int(self.no_score_gain_steps) >= int(self.max_no_score_gain_steps)

    def _higher_recovery_voltages(
        self,
        context: AutoUvAcceptedCandidateContext,
        *,
        current_voltage_mv: int,
        max_voltage_mv: int,
    ) -> list[int]:
        seen: set[int] = set()
        voltages: list[int] = []
        for probe in reversed(context.stable_history):
            voltage_mv = int(probe.candidate_voltage_mv)
            if voltage_mv in seen:
                continue
            seen.add(voltage_mv)
            if int(current_voltage_mv) < voltage_mv <= int(max_voltage_mv):
                voltages.append(voltage_mv)
        return sorted(voltages)

    @staticmethod
    def _unchanged(context: AutoUvAcceptedCandidateContext) -> AutoUvBehaviorResult:
        return AutoUvBehaviorResult(
            state=context.state,
            stable_candidate=context.stable_candidate,
            stable_probe=context.stable_probe,
            events=[],
        )


def _ratio_or_one(value: float | None, base_value: float | None) -> float:
    if (
        value is None
        or base_value is None
        or float(value) <= 0.0
        or float(base_value) <= 0.0
    ):
        return 1.0
    return float(value) / float(base_value)


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
