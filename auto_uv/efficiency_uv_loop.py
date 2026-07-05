"""Efficiency Auto-UV preset entry point.

Efficiency runs the shared base undervolt sweep first. If that first pass stops
above the allowed minimum voltage, it runs one more low-voltage tail-tune pass:
raise the curve tail by two bins, disable the FPS/W early-stop wall, and allow
low-clock-only probes to be skipped while searching lower voltage bins.

Whichever pass produces the final candidate, the saved efficiency profile
always carries the raised tail: scan probes flatten the tail anyway, so a
pass-1 result rebuilt with the tail is validated exactly as much as a
tail-tune result, and final verification still tests the real shape.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from auto_uv.base_uv_loop import BaseUvLoopIO, run_base_uv_loop
from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.curve.rising_tail import normalize_tail_rise_bins
from auto_uv.domain.console_log import log_phase, log_user_stage
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import VfCurveCandidate
from auto_uv.run.voltage_sweep_state import LowerVoltageSweepResult, VoltageProbeOutcome
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_EFFICIENCY

_EFFICIENCY_EXTRA_TAIL_RISE_BINS = 2


def run_efficiency_uv_loop(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    io: BaseUvLoopIO,
    unsafe_entries: list[dict] | None = None,
    initial_stable_outcome: VoltageProbeOutcome | None = None,
    min_search_voltage_mv: int,
    initial_tail_rise_bins: int,
    log: Callable[[str], None],
) -> LowerVoltageSweepResult:
    first_pass = run_base_uv_loop(
        base_curve,
        settings=settings,
        initial_stable_candidate=initial_stable_candidate,
        io=io,
        unsafe_entries=unsafe_entries,
        initial_stable_outcome=initial_stable_outcome,
    )
    if settings.auto_uv_mode != AUTO_UV_MODE_EFFICIENCY:
        return first_pass

    tail_tune_bins = int(initial_tail_rise_bins) + _EFFICIENCY_EXTRA_TAIL_RISE_BINS
    if int(first_pass.stable_candidate.voltage_mv) <= int(min_search_voltage_mv):
        return result_with_efficiency_tail(
            first_pass,
            base_curve,
            tail_rise_bins=int(tail_tune_bins),
            io=io,
            log=log,
        )

    log_user_stage(
        log,
        "Auto-UV efficiency tail tune",
        [
            (
                "Continuing toward the card minimum voltage with "
                f"{int(tail_tune_bins)} tail-rise bins."
            ),
            f"Keeping target clock: {int(first_pass.stable_candidate.target_mhz)}MHz.",
        ],
    )
    second_pass = run_base_uv_loop(
        base_curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=int(settings.start_voltage_mv),
            min_search_voltage_mv=int(min_search_voltage_mv),
            baseline_core_clock_mhz=settings.baseline_core_clock_mhz,
            auto_uv_mode="efficiency-tail-tune",
            min_core_clock_pct=float(settings.min_core_clock_pct),
            reference_actual_voltage_mv=_reference_voltage_mv(first_pass),
            efficiency_stop_streak=0,
            min_efficiency_stop_voltage_drop_pct=0.0,
            tail_rise_bins=int(tail_tune_bins),
            descend_through_low_clock=True,
        ),
        initial_stable_candidate=first_pass.stable_candidate,
        io=io,
        unsafe_entries=unsafe_entries,
        initial_stable_outcome=first_pass.stable_outcome,
    )
    # The tail-tune pass can end without accepting a single probe, handing the
    # tail-0 first-pass candidate back; the rebuild below covers that case too.
    return result_with_efficiency_tail(
        second_pass,
        base_curve,
        tail_rise_bins=int(tail_tune_bins),
        io=io,
        log=log,
    )


def result_with_efficiency_tail(
    result: LowerVoltageSweepResult,
    base_curve: list[dict],
    *,
    tail_rise_bins: int,
    io: BaseUvLoopIO,
    log: Callable[[str], None],
) -> LowerVoltageSweepResult:
    """Ensure the final efficiency candidate carries the raised rising tail."""

    candidate = result.stable_candidate
    current_tail_rise_bins = candidate_tail_rise_bins(candidate)
    if current_tail_rise_bins >= int(tail_rise_bins):
        return result
    try:
        rebuilt = build_flattened_voltage_probe_curve(
            base_curve,
            candidate_voltage_mv=int(candidate.voltage_mv),
            target_clock_mhz=int(candidate.target_mhz),
            label=f"{candidate.label} efficiency-tail",
            tail_rise_bins=int(tail_rise_bins),
            metadata={
                **dict(candidate.metadata or {}),
                "tail_rise_bins": int(tail_rise_bins),
            },
        )
    except ValueError as error:
        log_phase(
            log,
            "efficiency",
            "raised-tail rebuild skipped "
            f"{int(candidate.voltage_mv)}mV@{int(candidate.target_mhz)}MHz: "
            f"{error}",
        )
        return result
    log_phase(
        log,
        "efficiency",
        "raised-tail rebuild "
        f"{int(candidate.voltage_mv)}mV@{int(candidate.target_mhz)}MHz "
        f"tail {int(current_tail_rise_bins)}->{int(tail_rise_bins)}",
    )
    # Re-persist through the sweep IO so on-disk verified-candidate records
    # match the returned plan; crash recovery and the final-choice UI restore
    # from those records and must see the raised tail, not the probed tail-0
    # plan.
    outcome = result.stable_outcome
    if outcome is not None and outcome.raw_probe is not None:
        io.write_verified_candidate(rebuilt, outcome)
    return replace(result, stable_candidate=rebuilt)


def candidate_tail_rise_bins(candidate: VfCurveCandidate) -> int:
    return normalize_tail_rise_bins(
        dict(candidate.metadata or {}).get("tail_rise_bins")
    )


def _reference_voltage_mv(result: LowerVoltageSweepResult) -> float | None:
    outcome = result.stable_outcome
    return None if outcome is None else outcome.measured_voltage_mv
