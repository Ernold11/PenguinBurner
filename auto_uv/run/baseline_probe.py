from __future__ import annotations

from typing import Callable

from auto_uv.stability.q2rtx import Q2RTXStabilityConfig

from auto_uv.domain.types import AutoUvError, AutoUvProbeSummary, VfCurveCandidate
from auto_uv.domain.console_log import log_benchmark, log_phase
from auto_uv.curve.base_load_flatten_target import (
    choose_base_load_flatten_target,
    selected_nvidia_light_load_diagnostic,
)
from auto_uv.curve.base_load_voltage import derive_loaded_voltage_band
from auto_uv.curve.base_vf_curve import editable_base_vf_points
from auto_uv.curve.base_vf_curve_voltage_bins import (
    lock_voltage_for_target_clock,
    nearest_editable_voltage_bin,
)
from auto_uv.curve.measured_probe_lock_clock import lock_clock_from_probe_loaded_clock
from auto_uv.curve.vf_curve_flattening import (
    build_flatten_target_for_plan,
    build_flattened_plan,
)
from auto_uv.persistence.verified_candidate_result_file import write_latest_verified_candidate
from auto_uv.q2rtx.q2rtx_cuda_probe_config import reference_discovery_q2rtx_duration_s
from auto_uv.q2rtx.q2rtx_cuda_probe_runner import Q2RtxCudaProbeRunner
from auto_uv.ui.probe_summary_ui_payload import probe_summary_ui_payload
from auto_uv.ui.ui_json_event_writer import AutoUvEventCallback, emit_ui_json_event
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome


def retarget_clock_ceiling_for_candidate(
    clock_ceiling,
    candidate: VfCurveCandidate,
) -> None:
    if clock_ceiling is None:
        return
    clock_ceiling.retarget(
        lock_clock_mhz=int(candidate.target_mhz),
        lock_voltage_mv=int(candidate.voltage_mv),
        ceiling_clock_mhz=tail_ceiling_for_plan(
            candidate.flattened_plan,
            lock_clock_mhz=int(candidate.target_mhz),
            lock_voltage_mv=int(candidate.voltage_mv),
        ),
    )


def lower_voltage_descent_enforces_clock_floor(*, tail_rise_bins: int) -> bool:
    return int(tail_rise_bins) > 0


def run_discovery_probe(
    base_curve: list[dict],
    *,
    gpu,
    q2rtx_config: Q2RTXStabilityConfig,
    short_probe_base_duration_s: int,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
    marker_details: dict | None = None,
) -> tuple[AutoUvProbeSummary, object]:
    point = max(editable_base_vf_points(base_curve), key=lambda item: item.target_mhz)
    runner = Q2RtxCudaProbeRunner(
        reader=gpu.reader,
        live_voltage_reader=gpu.live_voltage_reader,
        q2rtx_config=q2rtx_config,
        runtime_default_plan=gpu.runtime_default_plan,
        power_limit_w=gpu.power_limit_w,
        start_voltage_mv=int(point.voltage_mv),
        baseline_clock_mhz=None,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        log=log,
        marker_details=marker_details,
        event_callback=event_callback,
    )
    emit_ui_json_event(
        event_callback,
        "probe_start",
        stage="base-baseline",
        voltage_mv=int(point.voltage_mv),
        clock_mhz=int(point.target_mhz),
        label="base default curve",
        elapsed_s=0.0,
        target_duration_s=reference_discovery_q2rtx_duration_s(
            int(short_probe_base_duration_s)
        ),
    )
    summary, result = runner.probe_default_curve(
        base_curve=base_curve,
        label_voltage_mv=int(point.voltage_mv),
        label_clock_mhz=int(point.target_mhz),
    )
    emit_ui_json_event(
        event_callback,
        "probe_result",
        **probe_summary_ui_payload(
            summary,
            stage="base-baseline",
            decision="pass" if getattr(result, "success", False) else "fail",
            reason=str(getattr(result, "reason", "")),
        ),
    )
    log_benchmark(log, phase="discover", probe=summary)
    light_load_diagnostic = selected_nvidia_light_load_diagnostic(
        list(getattr(result, "telemetry_samples", []) or []),
        power_limit_w=gpu.power_limit_w,
    )
    if light_load_diagnostic is not None:
        log_phase(log, "discover", light_load_diagnostic)
    return summary, result


def build_loaded_baseline_candidate(
    base_curve: list[dict],
    *,
    discovery_summary: AutoUvProbeSummary,
    discovery_result: object,
    power_limit_w: int | None,
    tail_rise_bins: int = 0,
) -> tuple[VfCurveCandidate, object]:
    target = choose_base_load_flatten_target(
        base_curve,
        list(getattr(discovery_result, "telemetry_samples", []) or []),
        power_limit_w=power_limit_w,
        fallback_clock_mhz=discovery_summary.avg_core_clock_mhz,
    )
    voltage_band = derive_loaded_voltage_band(
        list(getattr(discovery_result, "telemetry_samples", []) or []),
        power_limit_w=power_limit_w,
        use_power_limit_floor=True,
    )
    fallback_voltage_mv = lock_voltage_for_target_clock(
        base_curve,
        int(target.target_clock_mhz),
    )
    start_voltage_mv = nearest_editable_voltage_bin(
        base_curve,
        int(voltage_band.average_mv or fallback_voltage_mv),
    )
    plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(target.target_clock_mhz),
        candidate_voltage_mv=int(start_voltage_mv),
        tail_rise_bins=int(tail_rise_bins),
    )
    return (
        VfCurveCandidate(
            label="baseline-loaded-flattened-curve",
            voltage_mv=int(start_voltage_mv),
            target_mhz=int(target.target_clock_mhz),
            flattened_plan=plan,
        ),
        target,
    )


def adjust_baseline_to_measured_clock(
    base_curve: list[dict],
    *,
    candidate: VfCurveCandidate,
    stable_probe: AutoUvProbeSummary,
    gpu,
    tail_rise_bins: int = 0,
) -> VfCurveCandidate:
    measured_target_mhz = lock_clock_from_probe_loaded_clock(
        base_curve,
        probe=stable_probe,
        previous_lock_clock_mhz=int(candidate.target_mhz),
    )
    if int(measured_target_mhz) == int(candidate.target_mhz):
        return candidate
    plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(measured_target_mhz),
        candidate_voltage_mv=int(candidate.voltage_mv),
        tail_rise_bins=int(tail_rise_bins),
    )
    if gpu.clock_ceiling is not None:
        gpu.clock_ceiling.retarget(
            lock_clock_mhz=int(measured_target_mhz),
            lock_voltage_mv=int(candidate.voltage_mv),
            ceiling_clock_mhz=tail_ceiling_for_plan(
                plan,
                lock_clock_mhz=int(measured_target_mhz),
                lock_voltage_mv=int(candidate.voltage_mv),
            ),
        )
    return VfCurveCandidate(
        label="baseline-measured-clock-adjusted",
        voltage_mv=int(candidate.voltage_mv),
        target_mhz=int(measured_target_mhz),
        flattened_plan=plan,
    )


def write_verified_candidate(
    candidate: VfCurveCandidate,
    probe: AutoUvProbeSummary,
    *,
    discovery_summary: AutoUvProbeSummary,
    tail_rise_bins: int = 0,
) -> None:
    effective_tail_rise_bins = int(
        candidate.metadata.get("tail_rise_bins", tail_rise_bins)
    )
    write_latest_verified_candidate(
        plan=candidate.flattened_plan,
        lock_clock_mhz=int(candidate.target_mhz),
        voltage_mv=int(candidate.voltage_mv),
        probe=probe,
        base_probe=discovery_summary,
        tail_rise_bins=int(effective_tail_rise_bins),
    )


def require_probe_summary(outcome: VoltageProbeOutcome) -> AutoUvProbeSummary:
    if outcome.raw_probe is None:
        raise AutoUvError("Auto-UV probe outcome did not include a probe summary")
    return outcome.raw_probe


def tail_ceiling_for_plan(
    plan: list[dict],
    *,
    lock_clock_mhz: int,
    lock_voltage_mv: int,
) -> int:
    target = build_flatten_target_for_plan(
        plan,
        plan,
        lock_clock_mhz=int(lock_clock_mhz),
        lock_voltage_mv=int(lock_voltage_mv),
    )
    return int(target.get("ceiling_clock_mhz", target["lock_clock_mhz"]))
