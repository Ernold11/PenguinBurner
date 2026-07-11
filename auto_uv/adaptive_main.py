"""Assemble the adaptive 3-in-1 engine inside the classic scan environment.

The classic main loop owns GPU access, the baseline probe, persistence and
events. This module borrows those in-scope pieces (as plain callables) and
runs the adaptive engine with them, so ``--auto-uv-mode adaptive`` needs no
second scan environment. The caller then runs final verification per tier
result, fragile-first.
"""

from __future__ import annotations

from typing import Callable

from auto_uv.adaptive_scan import (
    AdaptiveScanIO,
    AdaptiveScanResult,
    AdaptiveScanSettings,
    run_adaptive_scan,
)
from auto_uv.domain.events import AutoUvEventCallback, emit_auto_uv_event
from auto_uv.domain.types import AutoUvError, VfCurveCandidate
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from auto_uv.scan_mode.uv_limits import uv_limit_profile_target_for_gpu
from auto_uv.scheduler import SweepPrior


ADAPTIVE_TIERS = ("efficiency", "balanced", "performance")
# The first (deepest, most fragile) profile soaks the full final duration;
# shallower profiles sit on already-soaked edges and get a shorter confirm.
SECONDARY_FINAL_VERIFICATION_S = 120


def run_adaptive_uv_scan(
    base_curve: list[dict],
    *,
    gpu_name: object | None,
    probe_candidate: Callable[[VfCurveCandidate], VoltageProbeOutcome],
    mark_unsafe_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None],
    write_verified_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None],
    initial_stable_candidate: VfCurveCandidate,
    initial_stable_outcome: VoltageProbeOutcome | None,
    start_voltage_mv: int,
    min_search_voltage_mv: int,
    baseline_core_clock_mhz: float | None,
    min_core_clock_pct: float | None,
    efficiency_stop_streak: int,
    min_efficiency_stop_voltage_drop_pct: float,
    unsafe_voltages_mv: tuple[int, ...] = (),
    prior: SweepPrior = SweepPrior(),
    event_callback: AutoUvEventCallback | None = None,
    log: Callable[[str], None] = lambda _message: None,
) -> AdaptiveScanResult:
    """Run the one-sweep-three-profiles engine and return its tier results.

    Raises AutoUvError when the engine aborted (the failsafe already reported
    why); the caller's cleanup restores the GPU exactly as for any scan error.
    """
    settings = AdaptiveScanSettings(
        gpu_name=str(gpu_name or ""),
        start_voltage_mv=int(start_voltage_mv),
        min_search_voltage_mv=int(min_search_voltage_mv),
        tiers=ADAPTIVE_TIERS,
        baseline_core_clock_mhz=baseline_core_clock_mhz,
        min_core_clock_pct=min_core_clock_pct,
        efficiency_stop_streak=int(efficiency_stop_streak),
        min_efficiency_stop_voltage_drop_pct=float(min_efficiency_stop_voltage_drop_pct),
        target_clock_ceiling_mhz=adaptive_clock_ceiling_mhz(gpu_name),
        prior=prior,
        unsafe_voltages_mv=tuple(unsafe_voltages_mv),
    )
    io = AdaptiveScanIO(
        probe_candidate=probe_candidate,
        # v1 reuses the sweep probe for tier confirms: probes already run
        # 2-2.5x longer in the deep bands and the per-tier finals below are
        # the long soak. A dedicated confirm duration is a later refinement.
        confirm_candidate=probe_candidate,
        mark_unsafe_candidate=mark_unsafe_candidate,
        write_verified_candidate=write_verified_candidate,
        emit_event=lambda name, payload: emit_auto_uv_event(
            event_callback, name, **payload
        ),
    )
    result = run_adaptive_scan(
        base_curve,
        settings=settings,
        initial_stable_candidate=initial_stable_candidate,
        initial_stable_outcome=initial_stable_outcome,
        io=io,
    )
    if result.aborted:
        raise AutoUvError(result.abort_reason or "adaptive scan aborted")
    if not result.tier_results:
        raise AutoUvError(
            "adaptive scan found no stable tier candidate "
            f"(skipped: {', '.join(result.skipped_tiers) or 'all'})"
        )
    log(
        "Auto-UV adaptive sweep complete: "
        + ", ".join(
            f"{tier.spec.tier} {int(tier.candidate.voltage_mv)}mV"
            f"@{int(tier.candidate.target_mhz)}MHz"
            for tier in result.tier_results
        )
    )
    return result


def adaptive_clock_ceiling_mhz(gpu_name: object | None) -> int | None:
    """Bound every adaptive probe at the family's performance-tier clock.

    For known GPUs this is the do-not-exceed line (5080: 2980MHz@925mV rule);
    sweep targets descend from the baseline anyway, so the bound only ever
    trims runaway targets, never normal descent. Unknown GPUs get no bound —
    the base curve and drop ceiling still apply.
    """
    target = uv_limit_profile_target_for_gpu(gpu_name, "performance")
    return None if target is None else int(target.clock_mhz)


def final_verification_duration_for_tier(
    tier_index: int,
    *,
    configured_final_duration_s: int,
) -> int:
    if int(tier_index) == 0:
        return int(configured_final_duration_s)
    return min(int(configured_final_duration_s), SECONDARY_FINAL_VERIFICATION_S)
