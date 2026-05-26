"""Search the performance Auto-OC ladder before the final verification pass."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from auto_uv.auto_uv_console_log import log_phase
from auto_uv.auto_uv_types import (
    AutoUvProbeSummary,
    FailureKind,
    VfCurveCandidate,
)
from auto_uv.curve.flattened_voltage_probe_curve import build_flattened_voltage_probe_curve
from auto_uv.curve.rising_tail import tail_ceiling_clock_mhz
from auto_uv.q2rtx.q2rtx_cuda_probe_runner import Q2RtxCudaProbeRunner
from auto_uv.scan_mode.uv_limits import UvTierTarget, uv_limit_profile_target_for_gpu
from auto_uv.voltage_sweep_state import VoltageProbeOutcome
from .ladder import AutoOcStep, build_auto_oc_ladder
from .scoring import auto_oc_probe_key, effective_q2rtx_clock_mhz
from .settings import (
    AUTO_OC_DEFAULT_MAX_INTERPOLATION_STEPS,
    AUTO_OC_TARGET_PROFILE_ID,
)


@dataclass(frozen=True, slots=True)
class AutoOcAttempt:
    step: AutoOcStep
    candidate: VfCurveCandidate
    outcome: VoltageProbeOutcome


@dataclass(frozen=True, slots=True)
class AutoOcSearchResult:
    selected_candidate: VfCurveCandidate
    selected_probe: AutoUvProbeSummary | None
    endpoint: UvTierTarget | None = None
    attempts: tuple[AutoOcAttempt, ...] = ()
    skipped_reason: str | None = None


def run_auto_oc_candidate_search(
    *,
    base_curve: list[dict],
    start_candidate: VfCurveCandidate,
    start_probe: AutoUvProbeSummary | None,
    runner: Q2RtxCudaProbeRunner,
    gpu_name: object | None,
    clock_ceiling,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tail_rise_bins: int = 0,
    max_interpolation_steps: int = AUTO_OC_DEFAULT_MAX_INTERPOLATION_STEPS,
    target_voltage_mv: int | None = None,
    target_clock_mhz: int | None = None,
) -> AutoOcSearchResult:
    endpoint = auto_oc_endpoint(
        gpu_name,
        target_voltage_mv=target_voltage_mv,
        target_clock_mhz=target_clock_mhz,
    )
    if endpoint is None:
        return _skip(start_candidate, start_probe, "no-auto-oc-target")

    ladder = build_auto_oc_ladder(
        base_curve,
        start_voltage_mv=int(start_candidate.voltage_mv),
        start_clock_mhz=int(start_candidate.target_mhz),
        endpoint_voltage_mv=int(endpoint.voltage_mv),
        endpoint_clock_mhz=int(endpoint.clock_mhz),
        max_steps=int(max_interpolation_steps),
    )
    if not ladder:
        log_phase(
            log,
            "auto-oc",
            "skip no legal step from "
            f"{int(start_candidate.voltage_mv)}mV@{int(start_candidate.target_mhz)}MHz "
            f"to {int(endpoint.voltage_mv)}mV@{int(endpoint.clock_mhz)}MHz",
        )
        return AutoOcSearchResult(
            selected_candidate=start_candidate,
            selected_probe=start_probe,
            endpoint=endpoint,
            skipped_reason="no-legal-step",
        )

    log_phase(
        log,
        "auto-oc",
        f"target={endpoint.gpu_family}/{endpoint.profile_id} "
        f"cap={int(endpoint.voltage_mv)}mV@{int(endpoint.clock_mhz)}MHz "
        f"steps={len(ladder)}",
    )
    selected_candidate = start_candidate
    selected_probe = start_probe
    selected_key = auto_oc_probe_key(
        start_probe,
        voltage_mv=int(start_candidate.voltage_mv),
        step_index=0,
    )
    attempts: list[AutoOcAttempt] = []
    for step in ladder:
        candidate = auto_oc_candidate(
            base_curve,
            step=step,
            total_steps=len(ladder),
            tail_rise_bins=int(tail_rise_bins),
            start_clock_mhz=int(start_candidate.target_mhz),
            endpoint_clock_mhz=int(endpoint.clock_mhz),
        )
        retarget_clock_ceiling(
            clock_ceiling,
            candidate=candidate,
            log=log,
        )
        log_phase(
            log,
            "auto-oc",
            f"try={step.index}/{len(ladder)} "
            f"{candidate.voltage_mv}mV@{candidate.target_mhz}MHz",
        )
        outcome = runner.probe_candidate(
            candidate,
            stable_history=[],
            phase_label="candidate",
            enforce_target_core_clock_floor=False,
            summarize_saturated_tail=False,
            use_power_limit_floor=False,
            use_companion_load=True,
        )
        if outcome.raw_probe is not None:
            probe_history.append(outcome.raw_probe)
        attempts.append(AutoOcAttempt(step=step, candidate=candidate, outcome=outcome))
        if outcome.decision.passed and outcome.raw_probe is not None:
            candidate_key = auto_oc_probe_key(
                outcome.raw_probe,
                voltage_mv=int(candidate.voltage_mv),
                step_index=int(step.index),
            )
            if candidate_key > selected_key:
                selected_candidate = candidate
                selected_probe = outcome.raw_probe
                selected_key = candidate_key
            log_phase(
                log,
                "auto-oc",
                "pass measured-clock="
                f"{_format_clock(effective_q2rtx_clock_mhz(outcome.raw_probe))}",
            )
        else:
            log_phase(log, "auto-oc", f"rejected {outcome.decision.reason}")
        if auto_oc_should_stop(outcome):
            log_phase(log, "auto-oc", f"stop critical={outcome.decision.reason}")
            break

    if selected_candidate is start_candidate:
        log_phase(log, "auto-oc", "no measured-clock improvement; keeping UV candidate")
    else:
        log_phase(
            log,
            "auto-oc",
            f"selected={selected_candidate.voltage_mv}mV@{selected_candidate.target_mhz}MHz "
            f"measured-clock={_format_clock(effective_q2rtx_clock_mhz(selected_probe))}",
        )
    return AutoOcSearchResult(
        selected_candidate=selected_candidate,
        selected_probe=selected_probe,
        endpoint=endpoint,
        attempts=tuple(attempts),
    )


def auto_oc_endpoint(
    gpu_name: object | None,
    *,
    target_voltage_mv: int | None = None,
    target_clock_mhz: int | None = None,
) -> UvTierTarget | None:
    table_target = uv_limit_profile_target_for_gpu(gpu_name, AUTO_OC_TARGET_PROFILE_ID)
    voltage_mv = positive_int(target_voltage_mv)
    clock_mhz = positive_int(target_clock_mhz)
    if table_target is None and (voltage_mv is None or clock_mhz is None):
        return None
    return UvTierTarget(
        gpu_family=(
            table_target.gpu_family if table_target is not None else "Custom GPU"
        ),
        profile_id=(
            table_target.profile_id if table_target is not None else "custom"
        ),
        voltage_mv=int(
            voltage_mv
            if voltage_mv is not None
            else table_target.voltage_mv
        ),
        clock_mhz=int(
            clock_mhz
            if clock_mhz is not None
            else table_target.clock_mhz
        ),
    )


def auto_oc_candidate(
    base_curve: list[dict],
    *,
    step: AutoOcStep,
    total_steps: int,
    tail_rise_bins: int,
    start_clock_mhz: int | None = None,
    endpoint_clock_mhz: int | None = None,
) -> VfCurveCandidate:
    metadata = {
        "auto_oc": True,
        "auto_oc_step": int(step.index),
        "auto_oc_steps": int(total_steps),
    }
    if start_clock_mhz is not None and endpoint_clock_mhz is not None:
        start_clock = int(start_clock_mhz)
        endpoint_clock = int(endpoint_clock_mhz)
        limit_mhz = max(0, endpoint_clock - start_clock)
        applied_mhz = max(0, min(limit_mhz, int(step.target_mhz) - start_clock))
        metadata.update(
            {
                "auto_oc_start_clock_mhz": start_clock,
                "auto_oc_target_clock_mhz": endpoint_clock,
                "auto_oc_applied_mhz": applied_mhz,
                "auto_oc_limit_mhz": limit_mhz,
            }
        )
    return build_flattened_voltage_probe_curve(
        base_curve,
        candidate_voltage_mv=int(step.voltage_mv),
        target_clock_mhz=int(step.target_mhz),
        label=f"performance-oc {int(step.index)}/{int(total_steps)}",
        tail_rise_bins=int(tail_rise_bins),
        metadata=metadata,
    )


def retarget_clock_ceiling(
    clock_ceiling,
    *,
    candidate: VfCurveCandidate,
    log: Callable[[str], None],
) -> None:
    if clock_ceiling is None:
        return
    clock_ceiling.retarget(
        lock_clock_mhz=int(candidate.target_mhz),
        lock_voltage_mv=int(candidate.voltage_mv),
        ceiling_clock_mhz=tail_ceiling_clock_mhz(
            candidate.flattened_plan,
            fallback_clock_mhz=int(candidate.target_mhz),
            lock_voltage_mv=int(candidate.voltage_mv),
        ),
    )
    log_phase(log, "ceiling", clock_ceiling.describe())


def auto_oc_should_stop(outcome: VoltageProbeOutcome) -> bool:
    return outcome.decision.failure_kind in {
        FailureKind.FATAL_OUTPUT,
        FailureKind.NVIDIA_XID,
        FailureKind.USER_STOP,
    }


def _skip(
    candidate: VfCurveCandidate,
    probe: AutoUvProbeSummary | None,
    reason: str,
) -> AutoOcSearchResult:
    return AutoOcSearchResult(
        selected_candidate=candidate,
        selected_probe=probe,
        skipped_reason=str(reason),
    )


def _format_clock(value: float | None) -> str:
    return "n/a" if value is None else f"{float(value):.0f}MHz"


def positive_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
