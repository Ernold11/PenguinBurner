from __future__ import annotations

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
from auto_uv.user_output import (
    log_benchmark as _log_benchmark,
    log_phase as _log_phase,
    log_vf_ascii_chart as _log_vf_ascii_chart,
    log_vf_point_list as _log_vf_point_list,
)

from .candidate_decision import AutoUv2OverclockBudget, AutoUv2SweepState
from .sweep import AutoUv2SweepHooks, run_sweep


def _latest_reference_voltage_mv(
    history: list[AutoUvProbeSummary],
    fallback_voltage_mv: float | None,
) -> float | None:
    probe = _latest_non_companion_probe(history)
    if probe is not None and probe.avg_voltage_mv is not None:
        return float(probe.avg_voltage_mv)
    return fallback_voltage_mv


def run_auto_uv2_candidate_sweep(
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
    preserve_vanilla_below_mv: int | None,
    start_voltage_mv: int,
    clock_bump_budget_limit_pct: float,
    efficiency_stop_streak: int = 0,
    min_efficiency_stop_voltage_drop_pct: float = 0.0,
) -> dict:
    # This adapter is the only v2 layer that talks to live probe helpers.
    stable_candidate = AutoUvCurveCandidate(
        label="stable-start",
        candidate_voltage_mv=int(stable_voltage_mv),
        target_clock_mhz=int(stable_lock_clock_mhz),
        plan=stable_plan,
    )

    def _probe_candidate(candidate: AutoUvCurveCandidate):
        _log_phase(
            log,
            "candidate-v2",
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
                f"candidate-v2 target={candidate.target_clock_mhz}MHz "
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
            ),
            stable_history=stable_history,
            initial_probe_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label="candidate-v2",
            log_context=state.budget.describe(),
            power_limit_w=translated_gpu_policy.get("power_limit_w"),
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            reset_plan=runtime_default_plan,
        )
        _log_benchmark(
            log,
            phase="candidate-v2",
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
            reset_plan=runtime_default_plan,
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
        preserve_vanilla_below_mv=preserve_vanilla_below_mv,
        min_search_voltage_mv=int(min_search_voltage_mv),
        hooks=AutoUv2SweepHooks(
            probe_candidate=_probe_candidate,
            evaluate_probe=_evaluate,
            recover_upward=_recover_upward,
            write_latest_verified=_write_latest,
            normalize_accepted_candidate=_normalize_accepted,
            efficiency_delta=_temperature_normalized_efficiency_delta,
            power_up_efficiency_down=_is_power_up_efficiency_down_regression,
        ),
        efficiency_stop_streak=int(efficiency_stop_streak),
        min_efficiency_stop_voltage_drop_pct=float(
            min_efficiency_stop_voltage_drop_pct
        ),
    )
    for event in result.events:
        _log_phase(log, "auto-uv2", f"{event.name}: {event.message}")
    return {
        "stable_plan": result.stable_candidate.plan,
        "stable_voltage_mv": int(result.state.stable_voltage_mv),
        "stable_lock_clock_mhz": int(result.state.stable_target_mhz),
        "stable_probe": result.stable_probe,
        "ended_by_clock_bump_limit": result.state.budget.spent_or_disabled,
        "clock_bump_recovery_count": int(result.state.overclock_count),
        "clock_bump_budget_used_pct": float(result.state.budget.used_pct),
    }
