"""Efficiency Auto-UV preset entry point.

Efficiency runs the shared base undervolt sweep first. If that first pass stops
above the allowed minimum voltage, it runs one more low-voltage tail-tune pass:
raise the curve tail by two bins, disable the FPS/W early-stop wall, and allow
low-clock-only probes to be skipped while searching lower voltage bins.
"""

from __future__ import annotations

from typing import Callable

from auto_uv.base_uv_loop import BaseUvLoopIO, run_base_uv_loop
from auto_uv.domain.console_log import log_user_stage
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import VfCurveCandidate
from auto_uv.run.voltage_sweep_state import LowerVoltageSweepResult, VoltageProbeOutcome
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_EFFICIENCY


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
    if (
        settings.auto_uv_mode != AUTO_UV_MODE_EFFICIENCY
        or int(first_pass.stable_candidate.voltage_mv) <= int(min_search_voltage_mv)
    ):
        return first_pass

    tail_tune_bins = int(initial_tail_rise_bins) + 2
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
    return run_base_uv_loop(
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


def _reference_voltage_mv(result: LowerVoltageSweepResult) -> float | None:
    outcome = result.stable_outcome
    return None if outcome is None else outcome.measured_voltage_mv
