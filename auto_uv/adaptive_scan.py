"""One sweep, three profiles: the adaptive Auto-UV orchestrator.

Flow:
1. Trunk sweep. The scheduler walks one tail-less ladder from the scan start
   down to the deepest tier floor, recording every probe. Guard placement,
   step caps, and the confirm soak on the edge all live in the scheduler.
2. Tier selection. Each requested tier picks its lock record offline from
   the recorded telemetry (tier_policies) — no extra descent per tier.
3. Tail confirm. Each tier's candidate is rebuilt with its rising tail and
   must survive one long soak. A failed confirm prunes that depth and the
   tier re-selects shallower.

GPU side effects stay behind the IO callbacks; this file shows scan order
and decision flow, like the old base loop did — for all tiers at once.

Failsafe (unattended scans): too many consecutive hard failures abort the
scan and restore stock instead of grinding through crash recovery all night.
An aborted result still carries everything recorded so far.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Callable

from auto_uv.base_uv_loop import is_recoverable_low_clock
from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.domain.types import VfCurveCandidate
from auto_uv.envelope import ProbeOutcome
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from auto_uv.scheduler import ProbeRequest, SweepPrior, SweepScheduler
from auto_uv.tier_policies import (
    SweepProbeRecord,
    TierSpec,
    decorated_tier_candidate,
    select_tier_record,
    tier_specs_for_gpu,
)


@dataclass(frozen=True, slots=True)
class AdaptiveScanIO:
    """GPU and persistence side effects, injected."""

    probe_candidate: Callable[[VfCurveCandidate], VoltageProbeOutcome]
    confirm_candidate: Callable[[VfCurveCandidate], VoltageProbeOutcome]
    mark_unsafe_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None]
    write_verified_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None]
    restore_stock: Callable[[], None] | None = None
    save_journal: Callable[[dict], None] | None = None
    emit_event: Callable[[str, dict], None] | None = None
    run_performance_auto_oc: (
        Callable[[VfCurveCandidate, VoltageProbeOutcome], VfCurveCandidate | None] | None
    ) = None


@dataclass(frozen=True, slots=True)
class AdaptiveScanSettings:
    gpu_name: str
    start_voltage_mv: int
    min_search_voltage_mv: int
    tiers: tuple[str, ...]
    baseline_core_clock_mhz: float | None = None
    min_core_clock_pct: float | None = None
    efficiency_stop_streak: int = 2
    min_efficiency_stop_voltage_drop_pct: float = 10.0
    target_clock_ceiling_mhz: int | None = None
    max_consecutive_hard_fails: int = 3
    prior: SweepPrior = SweepPrior()
    # Voltages the crash cache already condemned on this card. They seed the
    # envelope as hard fails so the sweep never re-probes them (or anything
    # below them) after a reboot — the classic loop's unsafe-floor behavior.
    unsafe_voltages_mv: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TierScanResult:
    spec: TierSpec
    candidate: VfCurveCandidate
    confirm_outcome: VoltageProbeOutcome
    source_record: SweepProbeRecord


@dataclass(frozen=True, slots=True)
class AdaptiveScanResult:
    """Tier results ordered most-fragile-first (deepest voltage first), so
    graduated final verification soaks the riskiest profile longest."""

    tier_results: list[TierScanResult]
    records: list[SweepProbeRecord]
    skipped_tiers: list[str] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str | None = None


def run_adaptive_scan(
    base_curve: list[dict],
    *,
    settings: AdaptiveScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    io: AdaptiveScanIO,
    initial_stable_outcome: VoltageProbeOutcome | None = None,
    resume_journal: dict | None = None,
) -> AdaptiveScanResult:
    specs = requested_tier_specs(settings)
    records = seed_records(initial_stable_candidate, initial_stable_outcome)
    scheduler = SweepScheduler(
        base_curve,
        start_voltage_mv=int(settings.start_voltage_mv),
        stable_target_mhz=int(initial_stable_candidate.target_mhz),
        min_search_voltage_mv=trunk_floor_mv(settings, specs),
        baseline_core_clock_mhz=settings.baseline_core_clock_mhz,
        min_core_clock_pct=settings.min_core_clock_pct,
        target_clock_ceiling_mhz=settings.target_clock_ceiling_mhz,
        prior=settings.prior,
        descend_through_low_clock=any(not spec.uses_fps_per_w_wall for spec in specs),
    )
    if scheduler.envelope is not None and settings.unsafe_voltages_mv:
        unsafe = {int(voltage) for voltage in settings.unsafe_voltages_mv}
        for rung, voltage_mv in enumerate(scheduler.ladder):
            if int(voltage_mv) in unsafe:
                scheduler.envelope.record(rung, ProbeOutcome.HARD_FAIL)
    if resume_journal is not None:
        scheduler.restore_payload(resume_journal["scheduler"])
        records = [record_from_payload(entry) for entry in resume_journal["records"]]

    abort_reason = run_trunk_sweep(
        base_curve,
        scheduler=scheduler,
        records=records,
        settings=settings,
        io=io,
    )
    if abort_reason is not None:
        if io.restore_stock is not None:
            io.restore_stock()
        emit(io, "adaptive_abort", {"reason": abort_reason})
        return AdaptiveScanResult(
            tier_results=[],
            records=records,
            skipped_tiers=[spec.tier for spec in specs],
            aborted=True,
            abort_reason=abort_reason,
        )

    tier_results: list[TierScanResult] = []
    skipped: list[str] = []
    for spec in specs:
        result = confirm_tier(base_curve, spec, records, settings, io)
        if result is None:
            skipped.append(spec.tier)
            emit(io, "tier_skipped", {"tier": spec.tier})
            continue
        tier_results.append(result)

    tier_results.sort(key=lambda result: int(result.candidate.voltage_mv))
    emit(
        io,
        "adaptive_result",
        {
            "tiers": [
                {
                    "tier": result.spec.tier,
                    "voltage_mv": int(result.candidate.voltage_mv),
                    "target_mhz": int(result.candidate.target_mhz),
                    "tail_rise_bins": int(result.spec.tail_rise_bins),
                }
                for result in tier_results
            ],
            "skipped": list(skipped),
        },
    )
    return AdaptiveScanResult(tier_results=tier_results, records=records, skipped_tiers=skipped)


def run_trunk_sweep(
    base_curve: list[dict],
    *,
    scheduler: SweepScheduler,
    records: list[SweepProbeRecord],
    settings: AdaptiveScanSettings,
    io: AdaptiveScanIO,
) -> str | None:
    """Drive the scheduler until settled. Returns an abort reason, or None."""
    consecutive_hard_fails = 0
    while True:
        request = scheduler.next_request()
        if request is None:
            return None
        candidate = trunk_candidate(base_curve, request)
        outcome = (
            io.confirm_candidate(candidate)
            if request.confirm
            else io.probe_candidate(candidate)
        )
        classified = classify_probe_outcome(outcome)
        if classified is ProbeOutcome.HARD_FAIL:
            io.mark_unsafe_candidate(candidate, outcome)
            consecutive_hard_fails += 1
        else:
            consecutive_hard_fails = 0
        if classified is ProbeOutcome.PASS:
            # Persist every passing rung like the classic loop did: the
            # verified-candidate store backs crash recovery and the user-stop
            # final-choice flow, which must see trunk progress, not just the
            # eventual tier confirms.
            io.write_verified_candidate(candidate, outcome)
        measured = outcome.measured_core_clock_mhz
        scheduler.apply_result(
            request,
            classified,
            measured_clock_mhz=None if measured is None else int(measured),
        )
        records.append(
            SweepProbeRecord(
                voltage_mv=int(request.voltage_mv),
                target_clock_mhz=int(request.target_clock_mhz),
                outcome=classified,
                probe_payload=probe_payload(outcome),
            )
        )
        save_journal(io, scheduler, records)
        emit(
            io,
            "envelope_progress",
            {
                "voltage_mv": int(request.voltage_mv),
                "target_mhz": int(request.target_clock_mhz),
                "outcome": classified.value,
                "confirm": bool(request.confirm),
            },
        )
        if consecutive_hard_fails >= max(1, int(settings.max_consecutive_hard_fails)):
            return (
                f"{consecutive_hard_fails} consecutive hard failures — "
                "aborting and restoring stock"
            )


def confirm_tier(
    base_curve: list[dict],
    spec: TierSpec,
    records: list[SweepProbeRecord],
    settings: AdaptiveScanSettings,
    io: AdaptiveScanIO,
) -> TierScanResult | None:
    """Select the tier's record and prove its tail with one long soak.

    A failed tail confirm prunes that depth: everything at or below the
    failed voltage drops out and the tier re-selects shallower. The trunk
    swept tail-less, so this soak is the first time the tail shape runs.
    """
    candidates = list(records)
    while True:
        record = select_tier_record(
            candidates,
            spec,
            start_voltage_mv=int(settings.start_voltage_mv),
            efficiency_stop_streak=int(settings.efficiency_stop_streak),
            min_efficiency_stop_voltage_drop_pct=float(
                settings.min_efficiency_stop_voltage_drop_pct
            ),
        )
        if record is None:
            return None
        candidate = decorated_tier_candidate(base_curve, record, spec)
        outcome = io.confirm_candidate(candidate)
        if classify_probe_outcome(outcome) is ProbeOutcome.PASS:
            io.write_verified_candidate(candidate, outcome)
            emit(
                io,
                "tier_confirmed",
                {
                    "tier": spec.tier,
                    "voltage_mv": int(candidate.voltage_mv),
                    "target_mhz": int(candidate.target_mhz),
                },
            )
            if spec.tier == "performance" and io.run_performance_auto_oc is not None:
                raised = io.run_performance_auto_oc(candidate, outcome)
                if raised is not None:
                    candidate = raised
            return TierScanResult(
                spec=spec,
                candidate=candidate,
                confirm_outcome=outcome,
                source_record=record,
            )
        io.mark_unsafe_candidate(candidate, outcome)
        # Poison this voltage in the SHARED records too: a failed long soak
        # outranks the short pass that preceded it, for every later tier's
        # selection, not just this tier's retry.
        records.append(
            SweepProbeRecord(
                voltage_mv=int(record.voltage_mv),
                target_clock_mhz=int(record.target_clock_mhz),
                outcome=ProbeOutcome.HARD_FAIL,
                probe_payload=probe_payload(outcome),
            )
        )
        candidates = [
            entry
            for entry in candidates
            if int(entry.voltage_mv) > int(record.voltage_mv)
        ]


# ── Helpers ───────────────────────────────────────────────────────────────


def requested_tier_specs(settings: AdaptiveScanSettings) -> list[TierSpec]:
    requested = {str(tier) for tier in settings.tiers}
    return [
        spec
        for spec in tier_specs_for_gpu(settings.gpu_name)
        if spec.tier in requested
    ]


def trunk_floor_mv(settings: AdaptiveScanSettings, specs: list[TierSpec]) -> int:
    """The sweep descends to the deepest floor any requested tier needs; a
    tier without its own floor uses the configured card/user minimum."""
    floors = [
        int(spec.floor_voltage_mv)
        if spec.floor_voltage_mv is not None
        else int(settings.min_search_voltage_mv)
        for spec in specs
    ]
    deepest = min(floors) if floors else int(settings.min_search_voltage_mv)
    return max(0, deepest)


def trunk_candidate(base_curve: list[dict], request: ProbeRequest) -> VfCurveCandidate:
    label = f"adaptive {int(request.voltage_mv)}mV"
    if request.confirm:
        label += " edge-confirm"
    return build_flattened_voltage_probe_curve(
        base_curve,
        candidate_voltage_mv=int(request.voltage_mv),
        target_clock_mhz=int(request.target_clock_mhz),
        label=label,
        tail_rise_bins=0,
        metadata={"tail_rise_bins": 0, "adaptive_trunk": True},
    )


def classify_probe_outcome(outcome: VoltageProbeOutcome) -> ProbeOutcome:
    if outcome.decision.passed:
        return ProbeOutcome.PASS
    if is_recoverable_low_clock(outcome.decision):
        return ProbeOutcome.LOW_CLOCK
    return ProbeOutcome.HARD_FAIL


def seed_records(
    initial_stable_candidate: VfCurveCandidate,
    initial_stable_outcome: VoltageProbeOutcome | None,
) -> list[SweepProbeRecord]:
    """The baseline is the first stable record, exactly like the old loops
    started their selection from the initial stable candidate."""
    return [
        SweepProbeRecord(
            voltage_mv=int(initial_stable_candidate.voltage_mv),
            target_clock_mhz=int(initial_stable_candidate.target_mhz),
            outcome=ProbeOutcome.PASS,
            probe_payload=probe_payload(initial_stable_outcome),
        )
    ]


def probe_payload(outcome: VoltageProbeOutcome | None):
    """The raw probe as-is: policies read it via read_field, which handles
    both probe-summary objects and plain dicts (journal round trips)."""
    if outcome is None:
        return None
    return outcome.raw_probe


def save_journal(io: AdaptiveScanIO, scheduler: SweepScheduler, records: list[SweepProbeRecord]) -> None:
    if io.save_journal is None:
        return
    io.save_journal(
        {
            "scheduler": scheduler.to_payload(),
            "records": [record_to_payload(record) for record in records],
        }
    )


def record_to_payload(record: SweepProbeRecord) -> dict:
    payload = record.probe_payload
    if payload is not None and not isinstance(payload, dict):
        if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
            payload = dataclasses.asdict(payload)
        else:
            payload = None
    return {
        "voltage_mv": int(record.voltage_mv),
        "target_clock_mhz": int(record.target_clock_mhz),
        "outcome": record.outcome.value,
        "probe_payload": payload,
    }


def record_from_payload(payload: dict) -> SweepProbeRecord:
    return SweepProbeRecord(
        voltage_mv=int(payload["voltage_mv"]),
        target_clock_mhz=int(payload["target_clock_mhz"]),
        outcome=ProbeOutcome(payload["outcome"]),
        probe_payload=payload.get("probe_payload"),
    )


def emit(io: AdaptiveScanIO, name: str, payload: dict) -> None:
    if io.emit_event is not None:
        io.emit_event(name, payload)
