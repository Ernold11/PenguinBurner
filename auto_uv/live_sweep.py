from __future__ import annotations

import re

from auto_uv.artifacts import _write_latest_verified_uv_result
from auto_uv.models import AutoUvCurveCandidate, AutoUvProbeSummary
from auto_uv.probe_config import _stability_probe_config_for_voltage_band
from auto_uv.probe_metrics import (
    _evaluate_probe,
    _latest_non_companion_probe,
    _temperature_normalized_efficiency_delta,
)
from auto_uv.probe_runner import _probe_voltage_candidate
from auto_uv.scan import _probe_stabilization_search
from auto_uv.scan_rules import _is_power_up_efficiency_down_regression
from auto_uv.scan_rules import _real_clock_adjusted_stable_curve
from auto_uv.tuning import AUTO_UV_DEFAULTS
from auto_uv.events import (
    AutoUvEventCallback,
    emit_event,
    overclock_budget_payload_from_label,
    plan_event_points,
    probe_event_payload,
)
from auto_uv.user_output import (
    log_benchmark as _log_benchmark,
    log_phase as _log_phase,
    log_user_candidate_result as _log_user_candidate_result,
    log_vf_ascii_chart as _log_vf_ascii_chart,
    log_vf_point_list as _log_vf_point_list,
)

from .candidate_decision import AutoUv2OverclockBudget, AutoUv2SweepState
from .sweep import AutoUv2SweepHooks, run_sweep


_OVERCLOCK_BUDGET_RE = re.compile(r"\boverclocking-budget=[^\s]+")


def _candidate_log_context(candidate: AutoUvCurveCandidate) -> str:
    match = _OVERCLOCK_BUDGET_RE.search(str(candidate.label))
    if match is not None:
        return match.group(0)
    return str(candidate.label).strip()


def _latest_reference_voltage_mv(
    history: list[AutoUvProbeSummary],
    fallback_voltage_mv: float | None,
) -> float | None:
    probe = _latest_non_companion_probe(history)
    if probe is not None and probe.avg_voltage_mv is not None:
        return float(probe.avg_voltage_mv)
    return fallback_voltage_mv


def run_auto_uv_candidate_sweep(
    *,
    probe_stabilization_search=_probe_stabilization_search,
    log,
    reader,
    source_plan: list[dict],
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary,
    stable_history: list[AutoUvProbeSummary],
    probe_history: list[AutoUvProbeSummary],
    first_candidate_voltage_mv: int,
    discovery_summary: AutoUvProbeSummary,
    q2rtx_config,
    measured_clock_mhz: float | None,
    nvml_session,
    translated_gpu_policy: dict,
    runtime_default_plan: list[dict],
    clock_ceiling,
    min_performance_core_clock_pct: float,
    min_search_voltage_mv: int,
    preserve_base_below_mv: int | None,
    start_voltage_mv: int,
    clock_bump_budget_limit_pct: float,
    max_clock_drop_pct: float,
    short_probe_base_duration_s: int = AUTO_UV_DEFAULTS.probe_duration_s,
    efficiency_stop_streak: int = 0,
    min_efficiency_stop_voltage_drop_pct: float = 0.0,
    event_callback: AutoUvEventCallback | None = None,
) -> dict:
    # This adapter is the only sweep layer that talks to live probe helpers.
    stable_candidate = AutoUvCurveCandidate(
        label="stable-start",
        candidate_voltage_mv=int(stable_voltage_mv),
        target_clock_mhz=int(stable_lock_clock_mhz),
        plan=stable_plan,
    )
    latest_budget_payload: dict = {}

    def _probe_candidate(candidate: AutoUvCurveCandidate):
        nonlocal latest_budget_payload
        latest_budget_payload = overclock_budget_payload_from_label(
            str(candidate.label),
            max_clock_drop_pct=float(max_clock_drop_pct),
        )
        emit_event(
            event_callback,
            "candidate_curve",
            stage="candidate",
            voltage_mv=int(candidate.candidate_voltage_mv),
            clock_mhz=int(candidate.target_clock_mhz),
            label=str(candidate.label),
            points=plan_event_points(candidate.plan),
            **latest_budget_payload,
        )
        emit_event(
            event_callback,
            "probe_start",
            stage="candidate",
            voltage_mv=int(candidate.candidate_voltage_mv),
            clock_mhz=int(candidate.target_clock_mhz),
            label=str(candidate.label),
            **latest_budget_payload,
        )
        _log_phase(
            log,
            "candidate",
            f"try={candidate.candidate_voltage_mv}mV@{candidate.target_clock_mhz}MHz "
            f"shape={candidate.label}",
        )
        _log_vf_ascii_chart(
            log,
            plan=candidate.plan,
            target_clock_mhz=int(candidate.target_clock_mhz),
            candidate_voltage_mv=int(candidate.candidate_voltage_mv),
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
        summary, result = _probe_voltage_candidate(
            reader=reader,
            candidate_plan=candidate.plan,
            candidate_voltage_mv=int(candidate.candidate_voltage_mv),
            lock_clock_mhz=int(candidate.target_clock_mhz),
            q2rtx_config=_stability_probe_config_for_voltage_band(
                q2rtx_config,
                initial_target_voltage_mv=int(start_voltage_mv),
                candidate_voltage_mv=int(candidate.candidate_voltage_mv),
                base_duration_s=int(short_probe_base_duration_s),
            ),
            stable_history=stable_history,
            initial_probe_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label="candidate",
            log_context=_candidate_log_context(candidate),
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            reset_plan=runtime_default_plan,
            event_callback=event_callback,
        )
        _log_benchmark(
            log,
            phase="candidate",
            probe=summary,
            reference_probe=discovery_summary,
            reference_label="initial",
        )
        return summary, result

    def _evaluate(
        probe: AutoUvProbeSummary,
        history: list[AutoUvProbeSummary],
    ) -> str:
        return _evaluate_probe(
            probe,
            stable_history=history,
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
        )

    def _recover_upward(
        candidate: AutoUvCurveCandidate,
        probe: AutoUvProbeSummary,
        _reason: str,
    ):
        return probe_stabilization_search(
            reader=reader,
            plan_source=source_plan,
            failure_voltage_mv=int(candidate.candidate_voltage_mv),
            failure_live_voltage_mv=probe.live_voltage_after_mv,
            minimum_candidate_voltage_mv=None,
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
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            short_probe_base_duration_s=int(short_probe_base_duration_s),
            reset_plan=runtime_default_plan,
            event_callback=event_callback,
        )

    def _write_latest(
        candidate: AutoUvCurveCandidate,
        probe: AutoUvProbeSummary,
    ) -> None:
        _write_latest_verified_uv_result(
            plan=candidate.plan,
            lock_clock_mhz=int(candidate.target_clock_mhz),
            voltage_mv=int(candidate.candidate_voltage_mv),
            probe=probe,
            base_probe=discovery_summary,
        )

    def _normalize_accepted(
        candidate: AutoUvCurveCandidate,
        probe: AutoUvProbeSummary,
    ) -> AutoUvCurveCandidate:
        # Save and verify the measured loaded clock, not just the request.
        adjusted_plan, adjusted_target_mhz = _real_clock_adjusted_stable_curve(
            source_plan,
            candidate_voltage_mv=int(candidate.candidate_voltage_mv),
            previous_lock_clock_mhz=int(candidate.target_clock_mhz),
            probe=probe,
        )
        return AutoUvCurveCandidate(
            label=f"{candidate.label} accepted-real-clock",
            candidate_voltage_mv=int(candidate.candidate_voltage_mv),
            target_clock_mhz=int(adjusted_target_mhz),
            plan=adjusted_plan,
        )

    def _log_probe_result(
        attempt: int,
        decision: str,
        reason: str,
        probe: AutoUvProbeSummary,
        previous_probe: AutoUvProbeSummary | None,
    ) -> None:
        payload = probe_event_payload(
            probe,
            stage="candidate",
            decision=str(decision),
            reason=str(reason),
        )
        payload.update(latest_budget_payload)
        emit_event(
            event_callback,
            "probe_result",
            **payload,
        )
        _log_user_candidate_result(
            log,
            attempt=int(attempt),
            decision=str(decision),
            reason=str(reason),
            initial_probe=discovery_summary,
            previous_probe=previous_probe,
            candidate_probe=probe,
        )

    state = AutoUv2SweepState(
        stable_voltage_mv=int(stable_voltage_mv),
        stable_target_mhz=int(stable_lock_clock_mhz),
        candidate_voltage_mv=int(first_candidate_voltage_mv),
        budget=AutoUv2OverclockBudget(
            used_pct=0.0,
            limit_pct=float(clock_bump_budget_limit_pct),
        ),
    )
    result = run_sweep(
        source_plan,
        initial_state=state,
        stable_candidate=stable_candidate,
        stable_probe=stable_probe,
        stable_history=stable_history,
        probe_history=probe_history,
        start_voltage_mv=int(start_voltage_mv),
        initial_core_clock_mhz=measured_clock_mhz,
        min_core_clock_pct=float(min_performance_core_clock_pct),
        measured_clock_cap_mhz=measured_clock_mhz,
        reference_actual_voltage_mv=_latest_reference_voltage_mv(
            stable_history,
            discovery_summary.avg_voltage_mv,
        ),
        preserve_base_below_mv=preserve_base_below_mv,
        min_search_voltage_mv=int(min_search_voltage_mv),
        hooks=AutoUv2SweepHooks(
            probe_candidate=_probe_candidate,
            evaluate_probe=_evaluate,
            recover_upward=_recover_upward,
            write_latest_verified=_write_latest,
            normalize_accepted_candidate=_normalize_accepted,
            efficiency_delta=_temperature_normalized_efficiency_delta,
            power_up_efficiency_down=_is_power_up_efficiency_down_regression,
            log_probe_result=_log_probe_result,
        ),
        efficiency_stop_streak=int(efficiency_stop_streak),
        min_efficiency_stop_voltage_drop_pct=float(
            min_efficiency_stop_voltage_drop_pct
        ),
    )
    for event in result.events:
        _log_phase(log, "auto-uv", f"{event.name}: {event.message}")
    return {
        "stable_plan": result.stable_candidate.plan,
        "stable_voltage_mv": int(result.state.stable_voltage_mv),
        "stable_lock_clock_mhz": int(result.state.stable_target_mhz),
        "stable_probe": result.stable_probe,
        "ended_by_clock_bump_limit": result.state.budget.spent_or_disabled,
        "clock_bump_recovery_count": int(result.state.overclock_count),
        "clock_bump_budget_used_pct": float(result.state.budget.used_pct),
    }
