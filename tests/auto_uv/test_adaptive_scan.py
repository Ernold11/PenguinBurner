"""End-to-end tests for the adaptive 3-in-1 orchestrator against a fake GPU."""

from __future__ import annotations

from dataclasses import dataclass, field

from auto_uv.adaptive_scan import (
    AdaptiveScanIO,
    AdaptiveScanSettings,
    run_adaptive_scan,
)
from auto_uv.domain.types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from tests.auto_uv.auto_uv_test_data import wide_base_curve

GPU = "NVIDIA GeForce RTX 5080"
ALL_TIERS = ("efficiency", "balanced", "performance")


def probe_payload_for(voltage_mv: int) -> dict:
    return {
        "candidate_voltage_mv": int(voltage_mv),
        "avg_voltage_mv": float(voltage_mv),
        "avg_fps": 60.0,
        # Power falls steeply with voltage so FPS/W gains stay above the
        # wall's 1% threshold at every rung — the 5080 reference-scan shape
        # where the wall never fires and every tier reads the deep edge.
        "avg_power_w": 300.0 - (1020 - int(voltage_mv)) * 0.6,
        "avg_temperature_c": 64.0,
    }


def outcome_for(
    candidate: VfCurveCandidate,
    *,
    passed: bool,
    kind: FailureKind = FailureKind.NONE,
    severity: FailureSeverity = FailureSeverity.PASS,
) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=passed,
            failure_kind=kind,
            severity=severity,
            reason="ok" if passed else kind.value,
        ),
        measured_core_clock_mhz=float(candidate.target_mhz) if passed else None,
        measured_voltage_mv=float(candidate.voltage_mv),
        raw_probe=probe_payload_for(int(candidate.voltage_mv)) if passed else None,
    )


def passing(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
    return outcome_for(candidate, passed=True)


def hard_fail(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
    return outcome_for(
        candidate,
        passed=False,
        kind=FailureKind.Q2RTX_FAILED,
        severity=FailureSeverity.UNSAFE,
    )


def low_clock(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
    return outcome_for(
        candidate,
        passed=False,
        kind=FailureKind.LOW_CLOCK,
        severity=FailureSeverity.RECOVERABLE,
    )


@dataclass
class FakeGpu:
    """Silicon simulator: stable at/above edge_mv, hard fail below, with an
    optional low-clock band and per-confirm failures keyed by (tail, mV)."""

    edge_mv: int
    confirm_edge_mv: int | None = None
    low_clock_band: tuple[int, int] | None = None
    failing_confirms: set[tuple[int, int]] = field(default_factory=set)
    probes: list[VfCurveCandidate] = field(default_factory=list)
    confirms: list[VfCurveCandidate] = field(default_factory=list)
    unsafe: list[int] = field(default_factory=list)
    verified: list[VfCurveCandidate] = field(default_factory=list)
    journals: list[dict] = field(default_factory=list)
    events: list[tuple[str, dict]] = field(default_factory=list)
    stock_restores: int = 0

    def classify(self, candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        voltage = int(candidate.voltage_mv)
        if self.low_clock_band is not None:
            low, high = self.low_clock_band
            if low <= voltage <= high:
                return low_clock(candidate)
        if voltage >= self.edge_mv:
            return passing(candidate)
        return hard_fail(candidate)

    def probe(self, candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        self.probes.append(candidate)
        return self.classify(candidate)

    def confirm(self, candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        self.confirms.append(candidate)
        tail = int(dict(candidate.metadata or {}).get("tail_rise_bins", 0))
        if (tail, int(candidate.voltage_mv)) in self.failing_confirms:
            return hard_fail(candidate)
        if self.confirm_edge_mv is not None:
            if int(candidate.voltage_mv) >= self.confirm_edge_mv:
                return passing(candidate)
            return hard_fail(candidate)
        return self.classify(candidate)

    def io(self) -> AdaptiveScanIO:
        return AdaptiveScanIO(
            probe_candidate=self.probe,
            confirm_candidate=self.confirm,
            mark_unsafe_candidate=lambda c, o: self.unsafe.append(int(c.voltage_mv)),
            write_verified_candidate=lambda c, o: self.verified.append(c),
            restore_stock=self._restore_stock,
            save_journal=self.journals.append,
            emit_event=lambda name, payload: self.events.append((name, payload)),
        )

    def _restore_stock(self) -> None:
        self.stock_restores += 1


def baseline_candidate() -> VfCurveCandidate:
    return VfCurveCandidate(
        label="baseline",
        voltage_mv=1020,
        target_mhz=2686,
        flattened_plan=[],
        metadata={},
    )


def baseline_outcome() -> VoltageProbeOutcome:
    return passing(baseline_candidate())


def settings(**overrides) -> AdaptiveScanSettings:
    defaults = dict(
        gpu_name=GPU,
        start_voltage_mv=1240,
        min_search_voltage_mv=850,
        tiers=ALL_TIERS,
    )
    defaults.update(overrides)
    return AdaptiveScanSettings(**defaults)


def run(gpu: FakeGpu, **overrides):
    return run_adaptive_scan(
        wide_base_curve(),
        settings=settings(**overrides),
        initial_stable_candidate=baseline_candidate(),
        initial_stable_outcome=baseline_outcome(),
        io=gpu.io(),
    )


def test_one_sweep_produces_all_three_tiers() -> None:
    gpu = FakeGpu(edge_mv=900)
    result = run(gpu)
    assert not result.aborted
    assert result.skipped_tiers == []
    tiers = {r.spec.tier: r for r in result.tier_results}
    assert set(tiers) == set(ALL_TIERS)
    # Monotone FPS/W: every tier locks the deepest stable rung.
    for tier_result in result.tier_results:
        assert int(tier_result.candidate.voltage_mv) >= 900
    tails = {r.spec.tier: dict(r.candidate.metadata)["tail_rise_bins"] for r in result.tier_results}
    assert tails == {"efficiency": 2, "balanced": 4, "performance": 6}
    # Fragile-first ordering: results ascend in voltage.
    voltages = [int(r.candidate.voltage_mv) for r in result.tier_results]
    assert voltages == sorted(voltages)
    # Every passing trunk rung persists (classic-loop parity for crash
    # recovery and user-stop) — including the trunk edge confirm — plus one
    # verified write per confirmed tier.
    passing_probes = [c for c in gpu.probes if int(c.voltage_mv) >= 900]
    trunk_confirms = [
        c
        for c in gpu.confirms
        if dict(c.metadata or {}).get("adaptive_trunk") and int(c.voltage_mv) >= 900
    ]
    assert len(gpu.verified) == len(passing_probes) + len(trunk_confirms) + 3
    assert any(name == "adaptive_result" for name, _ in gpu.events)


def test_trunk_never_probes_outside_the_bounds() -> None:
    gpu = FakeGpu(edge_mv=900)
    run(gpu, target_clock_ceiling_mhz=2400)
    assert gpu.probes, "trunk must probe"
    for candidate in gpu.probes:
        assert int(candidate.voltage_mv) >= 850
        assert int(candidate.target_mhz) <= 2400


def test_failed_tail_confirm_reselects_shallower() -> None:
    gpu = FakeGpu(edge_mv=900)
    # The balanced tail (4 bins) cannot hold at the deepest stable rung.
    probe_result = run(gpu)
    deepest = min(int(c.voltage_mv) for c in gpu.probes if int(c.voltage_mv) >= 900)

    gpu = FakeGpu(edge_mv=900, failing_confirms={(4, deepest)})
    result = run(gpu)
    tiers = {r.spec.tier: r for r in result.tier_results}
    assert set(tiers) == set(ALL_TIERS)
    assert int(tiers["balanced"].candidate.voltage_mv) > deepest
    assert deepest in gpu.unsafe
    del probe_result


def test_consecutive_hard_fails_abort_and_restore_stock() -> None:
    gpu = FakeGpu(edge_mv=5000)  # nothing ever passes
    result = run(gpu, max_consecutive_hard_fails=1)
    assert result.aborted
    assert result.abort_reason is not None
    assert result.tier_results == []
    assert set(result.skipped_tiers) == set(ALL_TIERS)
    assert gpu.stock_restores == 1
    assert any(name == "adaptive_abort" for name, _ in gpu.events)


def test_all_fails_without_abort_falls_back_to_the_baseline() -> None:
    # Two hard fails settle the envelope before the abort threshold: every
    # tier falls back to the seeded baseline record, like the old loops
    # returning the initial stable candidate. The baseline itself still
    # holds (confirm edge below it), only the descent found nothing.
    gpu = FakeGpu(edge_mv=1236, confirm_edge_mv=1000)
    result = run(gpu)
    assert not result.aborted
    assert {r.spec.tier for r in result.tier_results} == set(ALL_TIERS)
    for tier_result in result.tier_results:
        assert int(tier_result.candidate.voltage_mv) == 1020


def test_low_clock_band_is_traversed_to_the_deep_edge() -> None:
    gpu = FakeGpu(edge_mv=855, low_clock_band=(870, 890))
    result = run(gpu)
    tiers = {r.spec.tier: r for r in result.tier_results}
    assert int(tiers["efficiency"].candidate.voltage_mv) <= 865
    band_probes = [c for c in gpu.probes if 870 <= int(c.voltage_mv) <= 890]
    assert band_probes, "the band must have been probed, not condemned"


def test_journal_resume_continues_instead_of_restarting() -> None:
    gpu = FakeGpu(edge_mv=900)
    run(gpu)
    assert len(gpu.journals) == len(gpu.probes) + len(
        [c for c in gpu.confirms if dict(c.metadata or {}).get("adaptive_trunk")]
    )
    midpoint = gpu.journals[2]

    resumed_gpu = FakeGpu(edge_mv=900)
    resumed = run_adaptive_scan(
        wide_base_curve(),
        settings=settings(),
        initial_stable_candidate=baseline_candidate(),
        initial_stable_outcome=baseline_outcome(),
        io=resumed_gpu.io(),
        resume_journal=midpoint,
    )
    assert not resumed.aborted
    assert {r.spec.tier for r in resumed.tier_results} == set(ALL_TIERS)
    assert len(resumed_gpu.probes) < len(gpu.probes)


def test_unsafe_voltages_seed_the_envelope_floor() -> None:
    # The crash cache condemned 880mV: the sweep must never probe it (or
    # anything below it) again after a reboot.
    gpu = FakeGpu(edge_mv=860)
    result = run(gpu, unsafe_voltages_mv=(880,))
    assert not result.aborted
    assert all(int(c.voltage_mv) > 880 for c in gpu.probes)
    for tier_result in result.tier_results:
        assert int(tier_result.candidate.voltage_mv) > 880


def test_confirm_hard_fail_outranks_earlier_short_pass() -> None:
    # A tier tail-confirm hard-fails at the deepest voltage: the stale
    # short-probe PASS at that voltage must not be selectable by later tiers.
    gpu = FakeGpu(edge_mv=900)
    run(gpu)
    deepest = min(int(c.voltage_mv) for c in gpu.probes if int(c.voltage_mv) >= 900)

    failing = FakeGpu(edge_mv=900, failing_confirms={(2, deepest)})
    result = run(failing)
    tiers = {r.spec.tier: r for r in result.tier_results}
    # Efficiency confirm failed at the deepest rung, so EVERY tier must sit
    # above it — the hard fail poisons that voltage for all selections.
    for tier_result in result.tier_results:
        assert int(tier_result.candidate.voltage_mv) > deepest
    assert set(tiers) == set(ALL_TIERS)


def test_performance_auto_oc_hook_replaces_the_candidate() -> None:
    gpu = FakeGpu(edge_mv=900)
    raised = VfCurveCandidate(
        label="auto-oc",
        voltage_mv=925,
        target_mhz=2980,
        flattened_plan=[],
        metadata={"tail_rise_bins": 6, "auto_uv_mode": "performance"},
    )
    io = AdaptiveScanIO(
        probe_candidate=gpu.probe,
        confirm_candidate=gpu.confirm,
        mark_unsafe_candidate=lambda c, o: None,
        write_verified_candidate=lambda c, o: None,
        run_performance_auto_oc=lambda candidate, outcome: raised,
    )
    result = run_adaptive_scan(
        wide_base_curve(),
        settings=settings(),
        initial_stable_candidate=baseline_candidate(),
        initial_stable_outcome=baseline_outcome(),
        io=io,
    )
    tiers = {r.spec.tier: r for r in result.tier_results}
    assert tiers["performance"].candidate is raised
