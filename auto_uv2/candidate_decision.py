from __future__ import annotations

from dataclasses import dataclass, replace

from auto_uv.clock_bump import (
    _clock_bump_consumed_pct as _overclock_consumed_pct,
    _format_clock_bump_budget as _format_overclock_budget,
    _next_clock_bump_target_mhz as _next_overclock_target_mhz,
)
from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary
from auto_uv.scan_rules import _percent
from auto_uv.curve_planning import _make_curve_candidate


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

    def spend(self, *, old_target_mhz: int, new_target_mhz: int) -> "AutoUv2OverclockBudget":
        return replace(
            self,
            used_pct=float(self.used_pct)
            + _overclock_consumed_pct(
                previous_target_clock_mhz=int(old_target_mhz),
                bumped_target_clock_mhz=int(new_target_mhz),
            ),
        )


@dataclass(frozen=True, slots=True)
class AutoUv2SweepState:
    """The small mutable-looking state needed to pick the next probe."""

    stable_voltage_mv: int
    stable_target_mhz: int
    candidate_voltage_mv: int | None
    budget: AutoUv2OverclockBudget
    last_overclock_target_mhz: int | None = None
    overclock_count: int = 0


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
    source_target_mhz = _source_clock_at_voltage(
        source_plan,
        voltage_mv=int(state.candidate_voltage_mv),
        fallback_mhz=int(state.stable_target_mhz),
    )
    # Lower voltage should follow the stock V/F curve downward by default.
    descended_target_mhz = min(int(state.stable_target_mhz), int(source_target_mhz))
    if state.last_overclock_target_mhz is None:
        return int(descended_target_mhz)
    # Accepted overclocking becomes the new minimum target for later probes.
    return max(int(descended_target_mhz), int(state.last_overclock_target_mhz))


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
    cap_clock_mhz: float,
    reason: str | None,
) -> tuple[int, AutoUv2SweepState]:
    if reason is None or state.budget.spent_or_disabled:
        return int(target_mhz), state
    overclock_target_mhz = _next_overclock_target_mhz(
        source_plan,
        current_clock_mhz=int(target_mhz),
        cap_clock_mhz=float(cap_clock_mhz),
        remaining_budget_pct=state.budget.remaining_pct,
        reason=reason,
    )
    if overclock_target_mhz is None:
        return int(target_mhz), state
    # Charge exactly what the snapped V/F grid target consumed.
    next_budget = state.budget.spend(
        old_target_mhz=int(target_mhz),
        new_target_mhz=int(overclock_target_mhz),
    )
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
    target_mhz = _base_target_for_voltage(source_plan, state=state)
    target_mhz, next_state = _apply_preemptive_overclock(
        source_plan,
        state=state,
        target_mhz=int(target_mhz),
        cap_clock_mhz=(
            float(measured_clock_cap_mhz)
            if measured_clock_cap_mhz is not None
            else float(state.stable_target_mhz)
        ),
        reason=predicted_floor_miss,
    )
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
