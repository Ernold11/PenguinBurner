from __future__ import annotations

import re
from dataclasses import dataclass, replace

from auto_uv.clock_bump import (
    _format_clock_bump_budget as _format_overclock_budget,
)
from auto_uv.tuning import (
    AUTO_UV_CURVE_TUNING,
)
from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary
from auto_uv.scan_rules import _percent
from auto_uv.curve_planning import _choose_sustained_clock_target, _make_curve_candidate


_CLOCK_GUARDRAIL_RE = re.compile(
    r"(?:current|predicted)=(?P<current>-?\d+(?:\.\d+)?)MHz.*?floor=(?P<floor>-?\d+(?:\.\d+)?)MHz"
)


@dataclass(frozen=True, slots=True)
class AutoUv2OverclockBudget:
    """Tracks how much curve-target overclocking the scan may still spend."""

    used_pct: float = 0.0
    limit_pct: float = 0.0

    @property
    def remaining_pct(self) -> float:
        return max(0.0, float(self.limit_pct) - float(self.used_pct))

    @property
    def spent_or_disabled(self) -> bool:
        return float(self.limit_pct) <= 0.0 or self.remaining_pct <= 0.0

    def describe(self) -> str:
        return _format_overclock_budget(
            used_pct=float(self.used_pct),
            limit_pct=float(self.limit_pct),
        )

    def with_measured_used_pct(self, used_pct: float) -> "AutoUv2OverclockBudget":
        return replace(self, used_pct=max(0.0, float(used_pct)))


@dataclass(frozen=True, slots=True)
class AutoUv2SweepState:
    """The small mutable-looking state needed to pick the next probe."""

    stable_voltage_mv: int
    stable_target_mhz: int
    candidate_voltage_mv: int | None
    budget: AutoUv2OverclockBudget
    last_overclock_target_mhz: int | None = None
    overclock_count: int = 0
    persistent_overclock_pct: float = 0.0
    stable_measured_target_mhz: int | None = None
    pending_measured_target_mhz: int | None = None
    full_budget_target_mhz: int | None = None


@dataclass(frozen=True, slots=True)
class AutoUv2CandidateChoice:
    """A ready-to-probe candidate plus the state consumed to create it."""

    candidate: AutoUvCurveCandidate
    state: AutoUv2SweepState
    phase: str
    predicted_floor_miss: str | None


def _source_clock_at_voltage(
    source_plan: list[dict],
    *,
    voltage_mv: int,
    fallback_mhz: int,
) -> int:
    for point in source_plan:
        if int(point["voltage_mv"]) == int(voltage_mv):
            return int(point["target_mhz"])
    return int(fallback_mhz)


def _voltage_phase(*, start_voltage_mv: int, candidate_voltage_mv: int) -> str:
    ratio = float(candidate_voltage_mv) / float(start_voltage_mv)
    if ratio > 0.94:
        return "coarse"
    if ratio > 0.88:
        return "medium"
    return "fine"


def _base_target_for_voltage(
    source_plan: list[dict],
    *,
    state: AutoUv2SweepState,
) -> int:
    assert state.candidate_voltage_mv is not None
    stable_measured_target_mhz = (
        int(state.stable_measured_target_mhz)
        if state.stable_measured_target_mhz is not None
        else int(state.stable_target_mhz)
    )
    if state.stable_measured_target_mhz is not None:
        return int(stable_measured_target_mhz)
    source_target_mhz = _source_clock_at_voltage(
        source_plan,
        voltage_mv=int(state.candidate_voltage_mv),
        fallback_mhz=int(stable_measured_target_mhz),
    )
    # Lower voltage should follow the base V/F curve downward by default.
    descended_target_mhz = min(int(stable_measured_target_mhz), int(source_target_mhz))
    return int(descended_target_mhz)


def _clock_step_mhz() -> int:
    return max(1, int(AUTO_UV_CURVE_TUNING.clock_step_mhz))


def max_clock_drop_pct_for_min_core_clock(min_core_clock_pct: float) -> float:
    return max(0.0, 100.0 - float(min_core_clock_pct))


def _recovery_fraction(
    *,
    budget_used_pct: float,
    max_clock_drop_pct: float,
) -> float:
    max_drop = max(0.0, float(max_clock_drop_pct))
    if max_drop <= 0.0:
        return 0.0
    return max(
        0.0,
        float(budget_used_pct) / max_drop,
    )


def overclock_budget_pct_for_target(
    *,
    measured_target_mhz: int,
    overclock_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
) -> float:
    measured = float(measured_target_mhz)
    baseline = float(baseline_clock_mhz or measured)
    target = float(overclock_target_mhz)
    delta_to_base_mhz = max(0.0, baseline - measured)
    if delta_to_base_mhz <= 0.0 or target <= measured:
        return 0.0
    recovered_fraction = max(0.0, (target - measured) / delta_to_base_mhz)
    return recovered_fraction * max(0.0, float(max_clock_drop_pct))


def overclock_budget_snap_tolerance_pct(
    *,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
) -> float:
    delta_to_base_mhz = max(
        0.0,
        float(baseline_clock_mhz or measured_target_mhz) - float(measured_target_mhz),
    )
    if delta_to_base_mhz <= 0.0:
        return 0.0
    return (
        float(_clock_step_mhz())
        / float(delta_to_base_mhz)
        * max(0.0, float(max_clock_drop_pct))
    )


def charged_overclock_budget_pct(
    *,
    consumed_pct: float,
    requested_used_pct: float,
    limit_pct: float,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
) -> float | None:
    limit = max(0.0, float(limit_pct))
    tolerance = overclock_budget_snap_tolerance_pct(
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if float(consumed_pct) > float(limit) + float(tolerance) + 1e-9:
        return None
    return min(float(limit), max(float(requested_used_pct), float(consumed_pct)))


def _choose_clock_target_at_or_above(
    source_plan: list[dict],
    *,
    desired_clock_mhz: float,
    cap_clock_mhz: float,
) -> int:
    step_mhz = _clock_step_mhz()
    cap_target_mhz = int(_choose_sustained_clock_target(source_plan, cap_clock_mhz))
    target_mhz = int(_choose_sustained_clock_target(source_plan, desired_clock_mhz))
    if float(target_mhz) < float(desired_clock_mhz):
        target_mhz += int(step_mhz)
    while int(target_mhz) > int(cap_target_mhz) or float(target_mhz) > float(cap_clock_mhz):
        target_mhz -= int(step_mhz)
    return max(int(step_mhz), int(target_mhz))


def overclock_recovery_target_mhz(
    source_plan: list[dict],
    *,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
    budget_used_pct: float,
    cap_clock_mhz: float | None = None,
    minimum_target_mhz: int | None = None,
) -> int:
    measured_target = int(measured_target_mhz)
    baseline_clock = float(baseline_clock_mhz or measured_target)
    if baseline_clock <= float(measured_target):
        return int(measured_target)
    fraction = _recovery_fraction(
        budget_used_pct=float(budget_used_pct),
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if fraction <= 0.0:
        return int(measured_target)
    desired_clock_mhz = float(measured_target) + (
        (float(baseline_clock) - float(measured_target)) * float(fraction)
    )
    if float(fraction) <= 1.0:
        cap_clock = min(
            float(baseline_clock),
            (
                float(cap_clock_mhz)
                if cap_clock_mhz is not None
                else float(baseline_clock)
            ),
        )
    else:
        cap_clock = float(desired_clock_mhz)
        if cap_clock_mhz is not None and float(cap_clock_mhz) > float(baseline_clock):
            cap_clock = min(float(cap_clock), float(cap_clock_mhz))
    if cap_clock <= float(measured_target):
        return int(measured_target)
    target_mhz = _choose_clock_target_at_or_above(
        source_plan,
        desired_clock_mhz=min(float(desired_clock_mhz), float(cap_clock)),
        cap_clock_mhz=float(cap_clock),
    )
    if minimum_target_mhz is not None and int(minimum_target_mhz) > int(target_mhz):
        target_mhz = int(minimum_target_mhz)
        while float(target_mhz) > float(cap_clock):
            target_mhz -= _clock_step_mhz()
    return max(int(measured_target), int(target_mhz))


def _requested_recovery_mhz(
    *,
    reason: str | None,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
) -> float:
    delta_to_base_mhz = max(
        0.0,
        float(baseline_clock_mhz or measured_target_mhz) - float(measured_target_mhz),
    )
    fallback_mhz = max(float(_clock_step_mhz()), float(delta_to_base_mhz) * 0.10)
    match = _CLOCK_GUARDRAIL_RE.search(str(reason or ""))
    if match is None:
        return float(fallback_mhz)
    observed_clock_mhz = float(match.group("current"))
    floor_clock_mhz = float(match.group("floor"))
    shortfall_mhz = max(0.0, float(floor_clock_mhz) - float(observed_clock_mhz))
    return max(float(fallback_mhz), float(shortfall_mhz) + float(_clock_step_mhz()))


def next_overclock_budget_used_pct(
    *,
    current_used_pct: float,
    limit_pct: float,
    reason: str | None,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
) -> float | None:
    current_used = max(0.0, float(current_used_pct))
    limit = max(0.0, float(limit_pct))
    if current_used >= limit:
        return None
    measured_target = int(measured_target_mhz)
    baseline_clock = float(baseline_clock_mhz or measured_target)
    delta_to_base_mhz = max(0.0, float(baseline_clock) - float(measured_target))
    max_drop = max(0.0, float(max_clock_drop_pct))
    if delta_to_base_mhz <= 0.0 or max_drop <= 0.0:
        return None
    requested_mhz = _requested_recovery_mhz(
        reason=reason,
        measured_target_mhz=int(measured_target),
        baseline_clock_mhz=float(baseline_clock),
    )
    requested_pct = float(requested_mhz) / float(delta_to_base_mhz) * float(max_drop)
    # Advance by at least 10% of the original allowed drop, so recovery does not
    # stall in tiny one-bin percentage steps when the low-voltage clock sags.
    minimum_step_pct = float(max_drop) * 0.10
    next_used = float(current_used) + max(float(requested_pct), float(minimum_step_pct))
    return min(float(limit), float(next_used))


def _apply_persistent_overclock(
    source_plan: list[dict],
    *,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    cap_clock_mhz: float | None,
    max_clock_drop_pct: float,
    budget_used_pct: float,
    minimum_target_mhz: int | None = None,
) -> int:
    budget_used = max(0.0, float(budget_used_pct))
    if budget_used <= 0.0:
        return int(measured_target_mhz)
    overclock_target_mhz = overclock_recovery_target_mhz(
        source_plan,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
        budget_used_pct=float(budget_used),
        cap_clock_mhz=cap_clock_mhz,
        minimum_target_mhz=minimum_target_mhz,
    )
    return max(int(measured_target_mhz), int(overclock_target_mhz))


def _fix_full_budget_target(
    state: AutoUv2SweepState,
    target_mhz: int,
) -> tuple[int, AutoUv2SweepState]:
    if float(state.budget.limit_pct) <= 0.0 or not state.budget.spent_or_disabled:
        return int(target_mhz), state
    if state.full_budget_target_mhz is not None:
        return int(state.full_budget_target_mhz), state
    return int(target_mhz), replace(state, full_budget_target_mhz=int(target_mhz))


def predict_clock_at_voltage(
    stable_history: list[AutoUvProbeSummary],
    *,
    candidate_voltage_mv: int,
) -> float | None:
    points = [
        (float(probe.candidate_voltage_mv), float(probe.avg_core_clock_mhz))
        for probe in stable_history[-4:]
        if probe.avg_core_clock_mhz is not None
    ]
    if len(points) < 2:
        return None
    first_voltage_mv, first_clock_mhz = points[0]
    last_voltage_mv, last_clock_mhz = points[-1]
    voltage_span_mv = float(last_voltage_mv) - float(first_voltage_mv)
    if abs(voltage_span_mv) <= 0.0:
        return None
    # Use the recent accepted slope to avoid knowingly probing below the floor.
    clock_per_mv = (float(last_clock_mhz) - float(first_clock_mhz)) / voltage_span_mv
    return float(last_clock_mhz) + (
        float(candidate_voltage_mv) - float(last_voltage_mv)
    ) * clock_per_mv


def predict_clock_floor_miss(
    stable_history: list[AutoUvProbeSummary],
    *,
    candidate_voltage_mv: int,
    initial_core_clock_mhz: float | None,
    min_core_clock_pct: float,
) -> str | None:
    if initial_core_clock_mhz is None:
        return None
    predicted_mhz = predict_clock_at_voltage(
        stable_history,
        candidate_voltage_mv=int(candidate_voltage_mv),
    )
    if predicted_mhz is None:
        return None
    floor_mhz = float(initial_core_clock_mhz) * _percent(float(min_core_clock_pct))
    if float(predicted_mhz) >= float(floor_mhz):
        return None
    # The overclock helper parses this text to size the next bump.
    return (
        f"predicted={float(predicted_mhz):.1f}MHz "
        f"floor={float(floor_mhz):.1f}MHz"
    )


def _apply_preemptive_overclock(
    source_plan: list[dict],
    *,
    state: AutoUv2SweepState,
    target_mhz: int,
    measured_target_mhz: int,
    baseline_clock_mhz: float | None,
    cap_clock_mhz: float | None,
    max_clock_drop_pct: float,
    reason: str | None,
) -> tuple[int, AutoUv2SweepState]:
    if reason is None or state.budget.spent_or_disabled:
        return int(target_mhz), state
    next_used_pct = next_overclock_budget_used_pct(
        current_used_pct=float(state.budget.used_pct),
        limit_pct=float(state.budget.limit_pct),
        reason=reason,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if next_used_pct is None:
        return int(target_mhz), state
    overclock_target_mhz = overclock_recovery_target_mhz(
        source_plan,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
        budget_used_pct=float(next_used_pct),
        cap_clock_mhz=cap_clock_mhz,
        minimum_target_mhz=int(target_mhz) + _clock_step_mhz(),
    )
    consumed_pct = overclock_budget_pct_for_target(
        measured_target_mhz=int(measured_target_mhz),
        overclock_target_mhz=int(overclock_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    charged_pct = charged_overclock_budget_pct(
        consumed_pct=float(consumed_pct),
        requested_used_pct=float(next_used_pct),
        limit_pct=float(state.budget.limit_pct),
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if charged_pct is None:
        return int(target_mhz), state
    if int(overclock_target_mhz) <= int(target_mhz):
        return int(target_mhz), state
    next_budget = state.budget.with_measured_used_pct(float(charged_pct))
    return int(overclock_target_mhz), replace(
        state,
        budget=next_budget,
        last_overclock_target_mhz=int(overclock_target_mhz),
        overclock_count=int(state.overclock_count) + 1,
    )


def choose_next_candidate(
    source_plan: list[dict],
    *,
    state: AutoUv2SweepState,
    start_voltage_mv: int,
    stable_history: list[AutoUvProbeSummary],
    initial_core_clock_mhz: float | None,
    min_core_clock_pct: float,
    measured_clock_cap_mhz: float | None,
) -> AutoUv2CandidateChoice | None:
    # This stays pure: the sweep decides when to touch the GPU.
    if state.candidate_voltage_mv is None:
        return None
    phase = _voltage_phase(
        start_voltage_mv=int(start_voltage_mv),
        candidate_voltage_mv=int(state.candidate_voltage_mv),
    )
    predicted_floor_miss = predict_clock_floor_miss(
        stable_history,
        candidate_voltage_mv=int(state.candidate_voltage_mv),
        initial_core_clock_mhz=initial_core_clock_mhz,
        min_core_clock_pct=float(min_core_clock_pct),
    )
    measured_target_mhz = _base_target_for_voltage(source_plan, state=state)
    next_state = replace(state, pending_measured_target_mhz=int(measured_target_mhz))
    max_clock_drop_pct = max_clock_drop_pct_for_min_core_clock(
        float(min_core_clock_pct)
    )
    persistent_budget_used_pct = min(
        float(next_state.persistent_overclock_pct),
        float(next_state.budget.limit_pct),
    )
    minimum_persistent_target_mhz = None
    if persistent_budget_used_pct > 0.0:
        minimum_candidate_pct = overclock_budget_pct_for_target(
            measured_target_mhz=int(measured_target_mhz),
            overclock_target_mhz=int(next_state.stable_target_mhz),
            baseline_clock_mhz=initial_core_clock_mhz,
            max_clock_drop_pct=float(max_clock_drop_pct),
        )
        if float(minimum_candidate_pct) <= float(next_state.budget.limit_pct) + 1e-9:
            minimum_persistent_target_mhz = int(next_state.stable_target_mhz)
    target_mhz = _apply_persistent_overclock(
        source_plan,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=initial_core_clock_mhz,
        cap_clock_mhz=(
            float(measured_clock_cap_mhz)
            if measured_clock_cap_mhz is not None
            else initial_core_clock_mhz
        ),
        max_clock_drop_pct=float(max_clock_drop_pct),
        budget_used_pct=float(persistent_budget_used_pct),
        minimum_target_mhz=minimum_persistent_target_mhz,
    )
    target_mhz, next_state = _apply_preemptive_overclock(
        source_plan,
        state=next_state,
        target_mhz=int(target_mhz),
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=initial_core_clock_mhz,
        cap_clock_mhz=(
            float(measured_clock_cap_mhz)
            if measured_clock_cap_mhz is not None
            else initial_core_clock_mhz
        ),
        max_clock_drop_pct=float(max_clock_drop_pct),
        reason=predicted_floor_miss,
    )
    target_mhz, next_state = _fix_full_budget_target(next_state, int(target_mhz))
    label = (
        f"voltage={int(state.candidate_voltage_mv)}mV phase={phase} "
        f"{next_state.budget.describe()}"
    )
    candidate = _make_curve_candidate(
        source_plan,
        candidate_voltage_mv=int(state.candidate_voltage_mv),
        target_clock_mhz=int(target_mhz),
        label=label,
    )
    return AutoUv2CandidateChoice(
        candidate=candidate,
        state=next_state,
        phase=phase,
        predicted_floor_miss=predicted_floor_miss,
    )
