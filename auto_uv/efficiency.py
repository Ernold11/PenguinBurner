from __future__ import annotations

from dataclasses import dataclass, replace

from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary

from .candidate_decision import AutoUv2SweepState
from .overclock_recovery import make_overclock_attempt
from .probe_decision import classify_probe_result
from .stop_decision import decide_efficiency_stop
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


AUTO_UV_MODE_EFFICIENCY = "efficiency"


@dataclass(slots=True)
class AutoUvEfficiencyBehavior:
    efficiency_stop_streak: int = 0
    min_efficiency_stop_voltage_drop_pct: float = 0.0
    name: str = AUTO_UV_MODE_EFFICIENCY

    no_gain_streak: int = 0
    pending_stop_candidate: AutoUvCurveCandidate | None = None
    pending_stop_probe: AutoUvProbeSummary | None = None

    def after_stable_acceptance(
        self,
        context: AutoUvAcceptedCandidateContext,
    ) -> AutoUvBehaviorResult:
        if context.hooks.efficiency_delta is None:
            return self._unchanged(context)
        if int(self.efficiency_stop_streak) <= 0:
            return self._unchanged(context)

        delta = context.hooks.efficiency_delta(
            context.previous_stable_probe,
            context.stable_probe,
        )
        improved = delta.get("improved")
        power_regression = (
            context.hooks.power_up_efficiency_down(
                context.previous_stable_probe,
                context.stable_probe,
                delta,
            )
            if context.hooks.power_up_efficiency_down is not None
            else False
        )
        measured_close = bool(delta.get("measured_voltage_close_to_requested"))
        voltage_drop_pct = self._voltage_drop_pct(context)
        stop_candidate = (
            (improved is False or bool(power_regression)) and measured_close
        )
        past_efficiency_stop_floor = float(voltage_drop_pct) >= float(
            self.min_efficiency_stop_voltage_drop_pct
        )

        if stop_candidate:
            self.no_gain_streak += 1
            if self.pending_stop_candidate is None:
                self.pending_stop_candidate = context.previous_stable_candidate
                self.pending_stop_probe = context.previous_stable_probe
        elif improved is True:
            self._reset_stop_tracking()

        if (
            stop_candidate
            and past_efficiency_stop_floor
            and not context.state.budget.spent_or_disabled
        ):
            (
                state,
                efficiency_candidate,
                efficiency_probe,
                efficiency_events,
            ) = self._try_efficiency_wall_overclock(context)
            if efficiency_candidate is not None and efficiency_probe is not None:
                context.stable_history.append(efficiency_probe)
                self._reset_stop_tracking()
                return AutoUvBehaviorResult(
                    state=state,
                    stable_candidate=efficiency_candidate,
                    stable_probe=efficiency_probe,
                    events=efficiency_events,
                    should_continue=True,
                )
            return AutoUvBehaviorResult(
                state=state,
                stable_candidate=context.stable_candidate,
                stable_probe=context.stable_probe,
                events=efficiency_events,
            )

        stop_decision = decide_efficiency_stop(
            efficiency_stop_candidate=bool(stop_candidate),
            voltage_drop_from_start_pct=float(voltage_drop_pct),
            min_voltage_drop_pct=float(self.min_efficiency_stop_voltage_drop_pct),
            no_gain_streak=int(self.no_gain_streak),
            required_extra_confirmations=int(self.efficiency_stop_streak),
            pending_previous_curve=self.pending_stop_candidate is not None,
            budget=context.state.budget,
            power_up_efficiency_down=bool(power_regression),
            efficiency_delta_pct=delta.get("delta_pct"),
        )
        if not stop_decision.should_stop:
            return self._unchanged(context)

        events = [AutoUv2SweepEvent("stop", stop_decision.reason)]
        stable_candidate = context.stable_candidate
        stable_probe = context.stable_probe
        state = context.state
        if (
            not stop_decision.use_current_curve
            and self.pending_stop_candidate is not None
            and self.pending_stop_probe is not None
        ):
            stable_candidate = self.pending_stop_candidate
            stable_probe = self.pending_stop_probe
            state = replace(
                state,
                stable_voltage_mv=int(stable_candidate.candidate_voltage_mv),
                stable_target_mhz=int(stable_candidate.target_clock_mhz),
                candidate_voltage_mv=None,
            )
        return AutoUvBehaviorResult(
            state=state,
            stable_candidate=stable_candidate,
            stable_probe=stable_probe,
            events=events,
            should_stop=True,
        )

    def _try_efficiency_wall_overclock(
        self,
        context: AutoUvAcceptedCandidateContext,
    ) -> tuple[
        AutoUv2SweepState,
        AutoUvCurveCandidate | None,
        AutoUvProbeSummary | None,
        list[AutoUv2SweepEvent],
    ]:
        events: list[AutoUv2SweepEvent] = []
        attempt = make_overclock_attempt(
            context.source_plan,
            state=context.state,
            failed_candidate=context.stable_candidate,
            reason="efficiency-wall",
            cap_clock_mhz=(
                context.measured_clock_cap_mhz or context.state.stable_target_mhz
            ),
            baseline_clock_mhz=context.initial_core_clock_mhz,
            max_clock_drop_pct=max(0.0, 100.0 - float(context.min_core_clock_pct)),
        )
        if attempt is None:
            events.append(AutoUv2SweepEvent("efficiency-overclock", "no budget"))
            return context.state, None, None, events

        events.append(
            AutoUv2SweepEvent(
                "efficiency-overclock",
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
        if decision.action != "accept" or context.hooks.efficiency_delta is None:
            events.append(AutoUv2SweepEvent("efficiency-overclock", "rejected"))
            return attempt.state, None, None, events

        efficiency_delta = context.hooks.efficiency_delta(context.stable_probe, probe)
        if efficiency_delta.get("improved") is not True:
            events.append(
                AutoUv2SweepEvent("efficiency-overclock", "no-efficiency-gain")
            )
            return attempt.state, None, None, events

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
        events.append(AutoUv2SweepEvent("efficiency-overclock", "accepted"))
        return update.state, accepted_candidate, probe, events

    def _unchanged(
        self,
        context: AutoUvAcceptedCandidateContext,
    ) -> AutoUvBehaviorResult:
        return AutoUvBehaviorResult(
            state=context.state,
            stable_candidate=context.stable_candidate,
            stable_probe=context.stable_probe,
            events=[],
        )

    def _reset_stop_tracking(self) -> None:
        self.no_gain_streak = 0
        self.pending_stop_candidate = None
        self.pending_stop_probe = None

    @staticmethod
    def _voltage_drop_pct(context: AutoUvAcceptedCandidateContext) -> float:
        if int(context.start_voltage_mv) <= 0:
            return 0.0
        return (
            (
                float(context.start_voltage_mv)
                - float(context.probed_candidate.candidate_voltage_mv)
            )
            / float(context.start_voltage_mv)
        ) * 100.0
