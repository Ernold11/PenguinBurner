from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary

from .candidate_decision import AutoUv2SweepState


@dataclass(frozen=True, slots=True)
class AutoUv2SweepEvent:
    name: str
    message: str


@dataclass(frozen=True, slots=True)
class AutoUv2SweepHooks:
    probe_candidate: Callable[[AutoUvCurveCandidate], tuple[AutoUvProbeSummary, object]]
    evaluate_probe: Callable[[AutoUvProbeSummary, list[AutoUvProbeSummary]], str]
    recover_upward: Callable[
        [AutoUvCurveCandidate, AutoUvProbeSummary, str],
        tuple[AutoUvCurveCandidate | None, AutoUvProbeSummary | None, object | None],
    ]
    write_latest_verified: Callable[[AutoUvCurveCandidate, AutoUvProbeSummary], None]
    candidate_block_reason: Callable[[AutoUvCurveCandidate], str] | None = None
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
    base_probe: AutoUvProbeSummary | None = None


@dataclass(frozen=True, slots=True)
class AutoUvAcceptedCandidateContext:
    source_plan: list[dict]
    hooks: AutoUv2SweepHooks
    state: AutoUv2SweepState
    previous_stable_candidate: AutoUvCurveCandidate
    previous_stable_probe: AutoUvProbeSummary
    stable_candidate: AutoUvCurveCandidate
    stable_probe: AutoUvProbeSummary
    probed_candidate: AutoUvCurveCandidate
    stable_history: list[AutoUvProbeSummary]
    probe_history: list[AutoUvProbeSummary]
    start_voltage_mv: int
    reference_actual_voltage_mv: float | None
    preserve_base_below_mv: int | None
    min_search_voltage_mv: int
    measured_clock_cap_mhz: float | None
    initial_core_clock_mhz: float | None
    min_core_clock_pct: float
    attempt_index: int


@dataclass(frozen=True, slots=True)
class AutoUvBehaviorResult:
    state: AutoUv2SweepState
    stable_candidate: AutoUvCurveCandidate
    stable_probe: AutoUvProbeSummary
    events: list[AutoUv2SweepEvent]
    should_continue: bool = False
    should_stop: bool = False


class AutoUvSweepBehavior(Protocol):
    name: str

    def after_stable_acceptance(
        self,
        context: AutoUvAcceptedCandidateContext,
    ) -> AutoUvBehaviorResult:
        ...


def probe_success(result: object) -> bool:
    return bool(getattr(result, "success", False))


def probe_reason(result: object) -> str | None:
    value = getattr(result, "reason", None)
    return str(value) if value is not None else None


def metric_regression_on_failed_probe(
    hooks: AutoUv2SweepHooks,
    probe: AutoUvProbeSummary,
    stable_history: list[AutoUvProbeSummary],
) -> str:
    try:
        evaluation_error = str(hooks.evaluate_probe(probe, stable_history) or "")
    except Exception:
        return ""
    if evaluation_error.startswith(("fps-regression", "frame-count-regression")):
        return evaluation_error
    return ""


def probe_evaluation_error(
    hooks: AutoUv2SweepHooks,
    *,
    probe: AutoUvProbeSummary,
    probe_result: object,
    stable_history: list[AutoUvProbeSummary],
) -> str:
    if probe_success(probe_result):
        return hooks.evaluate_probe(probe, stable_history)
    return metric_regression_on_failed_probe(hooks, probe, stable_history)


def state_uses_overclock(state: AutoUv2SweepState) -> bool:
    return (
        state.last_overclock_target_mhz is not None
        or float(state.persistent_overclock_pct) > 0.0
    )


def accepted_candidate_pair(
    hooks: AutoUv2SweepHooks,
    *,
    probed_candidate: AutoUvCurveCandidate,
    probe: AutoUvProbeSummary,
    uses_overclock: bool,
) -> tuple[AutoUvCurveCandidate, AutoUvCurveCandidate]:
    measured_candidate = probed_candidate
    if hooks.normalize_accepted_candidate is not None:
        measured_candidate = hooks.normalize_accepted_candidate(
            probed_candidate,
            probe,
        )
    active_candidate = probed_candidate if bool(uses_overclock) else measured_candidate
    return active_candidate, measured_candidate
