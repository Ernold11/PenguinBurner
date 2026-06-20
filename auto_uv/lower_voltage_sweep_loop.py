"""Sweep downward through lower voltage bins and keep the last stable curve.

GPU side effects stay behind hooks; this file shows only scan order and decision flow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from .auto_uv_types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from .auto_uv_scan_settings import AutoUvScanSettings
from .curve.base_vf_curve_validation import validate_base_vf_curve
from .scan_mode.efficiency_fps_per_w_policy import (
    compare_temperature_normalized_fps_per_w,
    decide_efficiency_stop,
    power_increased_while_efficiency_flat,
)
from .scan_mode import AUTO_UV_MODE_EFFICIENCY
from .curve.flattened_voltage_probe_curve import build_flattened_voltage_probe_curve
from .efficiency_tune import voltage_descent_candidate_policy
from .lower_voltage_probe_target import (
    base_curve_target_for_lower_voltage,
    lower_voltage_phase,
)
from .lower_voltage_search import select_next_lower_voltage
from .persistence.unsafe_voltage_cache import (
    unsafe_min_search_voltage,
    unsafe_voltage_block_reason,
)
from .voltage_sweep_state import (
    LowerVoltageSweepEvent,
    LowerVoltageSweepResult,
    VoltageProbeOutcome,
    VoltageSweepState,
)


@dataclass(frozen=True, slots=True)
class LowerVoltageSweepHooks:
    probe_candidate: Callable[[VfCurveCandidate], VoltageProbeOutcome]
    write_verified_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None]
    mark_unsafe_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None]
    record_passed_candidate: (
        Callable[[VfCurveCandidate, VoltageProbeOutcome], None] | None
    ) = None


@dataclass(frozen=True, slots=True)
class EfficiencySelection:
    selected_candidate: VfCurveCandidate
    selected_outcome: VoltageProbeOutcome | None
    no_gain_streak: int = 0
    pending_previous_curve: bool = False


@dataclass(frozen=True, slots=True)
class EfficiencyAcceptDecision:
    selected_candidate: VfCurveCandidate
    selected_outcome: VoltageProbeOutcome | None
    no_gain_streak: int
    pending_previous_curve: bool
    write_current: bool
    record_current: bool
    should_stop: bool
    stop_message: str | None = None


def run_lower_voltage_sweep_loop(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    hooks: LowerVoltageSweepHooks,
    unsafe_entries: list[dict] | None = None,
    initial_stable_outcome: VoltageProbeOutcome | None = None,
) -> LowerVoltageSweepResult:
    validate_base_vf_curve(base_curve)
    unsafe_floor_mv, unsafe_min_search_mv = unsafe_min_search_voltage(
        base_curve,
        start_voltage_mv=int(settings.start_voltage_mv),
        unsafe_entries=list(unsafe_entries or []),
    )
    min_search_voltage_mv = higher_min_search_voltage(
        configured_min_search_mv=settings.min_search_voltage_mv,
        unsafe_min_search_mv=unsafe_min_search_mv,
    )
    state = VoltageSweepState(
        stable_voltage_mv=int(initial_stable_candidate.voltage_mv),
        stable_target_mhz=int(initial_stable_candidate.target_mhz),
        next_voltage_mv=select_next_lower_voltage(
            base_curve,
            start_voltage_mv=int(settings.start_voltage_mv),
            stable_voltage_mv=int(initial_stable_candidate.voltage_mv),
            reference_actual_voltage_mv=settings.reference_actual_voltage_mv,
            min_search_voltage_mv=min_search_voltage_mv,
            failed_floor_voltage_mv=unsafe_floor_mv,
        ),
        stable_measured_target_mhz=measured_target_from_outcome(
            initial_stable_outcome
        ),
    )
    stable_candidate = initial_stable_candidate
    efficiency_selection = EfficiencySelection(
        selected_candidate=initial_stable_candidate,
        selected_outcome=initial_stable_outcome,
    )
    probe_history: list[VoltageProbeOutcome] = []
    events: list[LowerVoltageSweepEvent] = []

    while state.next_voltage_mv is not None:
        block_reason = unsafe_voltage_block_reason(
            list(unsafe_entries or []),
            candidate_voltage_mv=int(state.next_voltage_mv),
            lock_clock_mhz=int(state.stable_target_mhz),
        )
        if block_reason:
            events.append(LowerVoltageSweepEvent("stop", block_reason))
            break

        candidate, state = build_next_lower_voltage_candidate(
            base_curve,
            settings=settings,
            state=state,
            probe_history=probe_history,
        )
        outcome = hooks.probe_candidate(candidate)
        probe_history.append(outcome)
        if outcome.decision.passed:
            previous_selection = efficiency_selection
            stable_candidate, state = accept_voltage_probe(
                base_curve,
                settings=settings,
                state=state,
                candidate=candidate,
                outcome=outcome,
                min_search_voltage_mv=min_search_voltage_mv,
            )
            efficiency_acceptance = decide_efficiency_acceptance(
                settings=settings,
                state=state,
                selection=previous_selection,
                candidate=stable_candidate,
                outcome=outcome,
            )
            efficiency_selection = EfficiencySelection(
                selected_candidate=efficiency_acceptance.selected_candidate,
                selected_outcome=efficiency_acceptance.selected_outcome,
                no_gain_streak=int(efficiency_acceptance.no_gain_streak),
                pending_previous_curve=bool(
                    efficiency_acceptance.pending_previous_curve
                ),
            )
            if efficiency_acceptance.write_current:
                hooks.write_verified_candidate(stable_candidate, outcome)
            elif (
                efficiency_acceptance.record_current
                and hooks.record_passed_candidate is not None
            ):
                hooks.record_passed_candidate(stable_candidate, outcome)
            events.append(
                LowerVoltageSweepEvent(
                    "accept",
                    f"{stable_candidate.voltage_mv}mV@{stable_candidate.target_mhz}MHz",
                )
            )
            if efficiency_acceptance.should_stop:
                selected_candidate = efficiency_selection.selected_candidate
                selected_outcome = efficiency_selection.selected_outcome
                stable_candidate = selected_candidate
                state = state_for_selected_candidate(
                    state,
                    candidate=selected_candidate,
                    outcome=selected_outcome,
                )
                events.append(
                    LowerVoltageSweepEvent(
                        "stop",
                        efficiency_acceptance.stop_message
                        or "fps-per-watt wall reached",
                    )
                )
                break
            continue

        if is_recoverable_low_clock(outcome.decision):
            # The GPU stayed stable; only the core clock dipped below the floor.
            # This is not an unstable voltage, so it must NOT be cached unsafe --
            # doing so would also block the tail-tune pass from retrying it.
            if settings.descend_through_low_clock:
                events.append(
                    LowerVoltageSweepEvent(
                        "low-clock-skip",
                        f"{candidate.voltage_mv}mV below clock floor; descending further",
                    )
                )
                state = advance_through_low_clock(
                    base_curve,
                    settings=settings,
                    state=state,
                    candidate=candidate,
                    outcome=outcome,
                    min_search_voltage_mv=min_search_voltage_mv,
                )
                continue
            # First pass: the natural clock floor is reached. Stop here and let
            # the caller launch the raised-tail tail-tune pass to push lower.
            events.append(LowerVoltageSweepEvent("stop", outcome.decision.reason))
            break

        hooks.mark_unsafe_candidate(candidate, outcome)
        events.append(LowerVoltageSweepEvent("stop", outcome.decision.reason))
        break

    if not same_candidate_identity(
        efficiency_selection.selected_candidate,
        stable_candidate,
    ):
        state = state_for_selected_candidate(
            state,
            candidate=efficiency_selection.selected_candidate,
            outcome=efficiency_selection.selected_outcome,
        )
    return LowerVoltageSweepResult(
        stable_candidate=efficiency_selection.selected_candidate,
        state=state,
        probe_history=probe_history,
        events=events,
    )


def build_next_lower_voltage_candidate(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    probe_history: list[VoltageProbeOutcome],
) -> tuple[VfCurveCandidate, VoltageSweepState]:
    assert state.next_voltage_mv is not None
    policy = voltage_descent_candidate_policy(
        settings=settings,
        stable_target_mhz=int(state.stable_target_mhz),
    )
    _ = probe_history
    target_mhz = base_curve_target_for_lower_voltage(
        base_curve,
        candidate_voltage_mv=int(state.next_voltage_mv),
        stable_target_mhz=int(policy.target_mhz),
        stable_measured_target_mhz=state.stable_measured_target_mhz,
    )
    phase = lower_voltage_phase(
        start_voltage_mv=int(settings.start_voltage_mv),
        candidate_voltage_mv=int(state.next_voltage_mv),
    )
    candidate = build_flattened_voltage_probe_curve(
        base_curve,
        candidate_voltage_mv=int(state.next_voltage_mv),
        target_clock_mhz=int(target_mhz),
        label=(
            f"lower-voltage {int(state.next_voltage_mv)}mV "
            f"phase={phase}"
        ),
        tail_rise_bins=int(policy.tail_rise_bins),
        metadata={
            "tail_rise_bins": int(policy.tail_rise_bins),
            "target_policy": "hold-required-clock",
        },
    )
    return candidate, state


def measured_target_from_outcome(outcome: VoltageProbeOutcome | None) -> int | None:
    if outcome is None or outcome.measured_core_clock_mhz is None:
        return None
    return int(outcome.measured_core_clock_mhz)


def decide_efficiency_acceptance(
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    selection: EfficiencySelection,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
) -> EfficiencyAcceptDecision:
    if not efficiency_stop_enabled(settings):
        return select_current_efficiency_candidate(
            candidate,
            outcome,
            write_current=True,
        )

    previous_outcome = selection.selected_outcome
    previous_probe = previous_outcome.raw_probe if previous_outcome is not None else None
    candidate_probe = outcome.raw_probe
    if previous_probe is None or candidate_probe is None:
        return select_current_efficiency_candidate(
            candidate,
            outcome,
            write_current=True,
        )

    normalized = compare_temperature_normalized_fps_per_w(
        previous_probe,
        candidate_probe,
    )
    improved = normalized.get("improved")
    voltage_close = bool(normalized.get("measured_voltage_close_to_requested", True))
    if improved is not False or not voltage_close:
        return select_current_efficiency_candidate(
            candidate,
            outcome,
            write_current=True,
        )

    no_gain_streak = int(selection.no_gain_streak) + 1
    power_up_efficiency_down = power_increased_while_efficiency_flat(
        previous_power_w=float_or_none(normalized.get("previous_power_w")),
        candidate_power_w=float_or_none(normalized.get("candidate_power_w")),
        previous_fps_per_w=float_or_none(normalized.get("previous_fps_per_w")),
        candidate_fps_per_w=float_or_none(normalized.get("candidate_fps_per_w")),
    )
    stop_decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=voltage_drop_from_start_pct(
            start_voltage_mv=int(settings.start_voltage_mv),
            candidate_voltage_mv=int(candidate.voltage_mv),
        ),
        min_voltage_drop_pct=float(settings.min_efficiency_stop_voltage_drop_pct),
        no_gain_streak=int(no_gain_streak),
        required_extra_confirmations=max(0, int(settings.efficiency_stop_streak)),
        pending_previous_curve=bool(selection.pending_previous_curve),
        power_up_efficiency_down=bool(power_up_efficiency_down),
        efficiency_delta_pct=float_or_none(normalized.get("delta_pct")),
    )
    if bool(stop_decision.should_stop) and bool(stop_decision.use_current_curve):
        return EfficiencyAcceptDecision(
            selected_candidate=candidate,
            selected_outcome=outcome,
            no_gain_streak=int(no_gain_streak),
            pending_previous_curve=False,
            write_current=True,
            record_current=False,
            should_stop=True,
            stop_message=stop_decision.reason,
        )
    return EfficiencyAcceptDecision(
        selected_candidate=selection.selected_candidate,
        selected_outcome=selection.selected_outcome,
        no_gain_streak=int(no_gain_streak),
        pending_previous_curve=True,
        write_current=False,
        record_current=True,
        should_stop=bool(stop_decision.should_stop),
        stop_message=stop_decision.reason,
    )


def efficiency_stop_enabled(settings: AutoUvScanSettings) -> bool:
    return str(settings.auto_uv_mode) == AUTO_UV_MODE_EFFICIENCY


def select_current_efficiency_candidate(
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    *,
    write_current: bool,
) -> EfficiencyAcceptDecision:
    return EfficiencyAcceptDecision(
        selected_candidate=candidate,
        selected_outcome=outcome,
        no_gain_streak=0,
        pending_previous_curve=False,
        write_current=bool(write_current),
        record_current=False,
        should_stop=False,
    )


def state_for_selected_candidate(
    state: VoltageSweepState,
    *,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome | None,
) -> VoltageSweepState:
    measured_target_mhz = int(
        (
            outcome.measured_core_clock_mhz
            if outcome is not None and outcome.measured_core_clock_mhz is not None
            else candidate.target_mhz
        )
    )
    return replace(
        state,
        stable_voltage_mv=int(candidate.voltage_mv),
        stable_target_mhz=int(candidate.target_mhz),
        stable_measured_target_mhz=int(measured_target_mhz),
        next_voltage_mv=None,
    )


def same_candidate_identity(left: VfCurveCandidate, right: VfCurveCandidate) -> bool:
    return int(left.voltage_mv) == int(right.voltage_mv) and int(left.target_mhz) == int(
        right.target_mhz
    )


def voltage_drop_from_start_pct(*, start_voltage_mv: int, candidate_voltage_mv: int) -> float:
    if int(start_voltage_mv) <= 0:
        return 0.0
    return (
        max(0.0, float(start_voltage_mv) - float(candidate_voltage_mv))
        / float(start_voltage_mv)
        * 100.0
    )


def float_or_none(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def is_recoverable_low_clock(decision: StableRunDecision) -> bool:
    return (
        not decision.passed
        and decision.failure_kind is FailureKind.LOW_CLOCK
        and decision.severity is FailureSeverity.RECOVERABLE
    )


def advance_through_low_clock(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    min_search_voltage_mv: int | None,
) -> VoltageSweepState:
    """Skip a low-clock voltage and keep descending toward the minimum.

    The stable point is left untouched (its measured clock stays the held
    target), so only the next voltage to probe moves down one step.
    """

    reference_voltage_mv = (
        float(outcome.measured_voltage_mv)
        if outcome.measured_voltage_mv is not None
        else settings.reference_actual_voltage_mv
    )
    next_voltage_mv = select_next_lower_voltage(
        base_curve,
        start_voltage_mv=int(settings.start_voltage_mv),
        stable_voltage_mv=int(candidate.voltage_mv),
        reference_actual_voltage_mv=reference_voltage_mv,
        min_search_voltage_mv=min_search_voltage_mv,
        failed_floor_voltage_mv=state.failed_floor_voltage_mv,
    )
    return replace(state, next_voltage_mv=next_voltage_mv)


def accept_voltage_probe(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    min_search_voltage_mv: int | None,
) -> tuple[VfCurveCandidate, VoltageSweepState]:
    measured_target_mhz = int(outcome.measured_core_clock_mhz or candidate.target_mhz)
    reference_voltage_mv = (
        float(outcome.measured_voltage_mv)
        if outcome.measured_voltage_mv is not None
        else settings.reference_actual_voltage_mv
    )
    next_voltage_mv = select_next_lower_voltage(
        base_curve,
        start_voltage_mv=int(settings.start_voltage_mv),
        stable_voltage_mv=int(candidate.voltage_mv),
        reference_actual_voltage_mv=reference_voltage_mv,
        min_search_voltage_mv=min_search_voltage_mv,
        failed_floor_voltage_mv=state.failed_floor_voltage_mv,
    )
    next_state = replace(
        state,
        stable_voltage_mv=int(candidate.voltage_mv),
        stable_target_mhz=int(candidate.target_mhz),
        stable_measured_target_mhz=measured_target_mhz,
        next_voltage_mv=next_voltage_mv,
    )
    return candidate, next_state


def higher_min_search_voltage(
    *,
    configured_min_search_mv: int | None,
    unsafe_min_search_mv: int | None,
) -> int | None:
    values = [
        int(value)
        for value in (configured_min_search_mv, unsafe_min_search_mv)
        if value is not None
    ]
    return max(values) if values else None
