"""Property tests for the monotone ladder envelope.

The simulated GPUs here are deliberately hostile: silicon that fails the very
first probe, floor-limited cards that never fail (the 5080 reference-scan
shape), low-clock bands, and cards whose short-probe passes do not survive a
confirm soak. The envelope must converge on all of them without ever
suggesting a probe deeper than the jump cap allows.
"""

from __future__ import annotations

import random

import pytest

from auto_uv.envelope import LadderEnvelope, ProbeOutcome, RungRecord


def untested_gap(envelope: LadderEnvelope, rung: int) -> int:
    """Count untested rungs between the deepest pass and the suggestion."""
    deepest = envelope.deepest_pass()
    start = 0 if deepest is None else deepest + 1
    return sum(1 for r in range(start, rung + 1) if r not in envelope.records)


def drive_search(
    envelope: LadderEnvelope,
    outcome_for_rung,
    *,
    confirm_passes=lambda _rung: True,
) -> list[int]:
    """Run the search loop the scheduler would run and return probed rungs."""
    probes: list[int] = []
    for _ in range(10 * envelope.rung_count + 10):
        rung = envelope.next_probe_rung()
        if rung is not None:
            assert untested_gap(envelope, rung) <= envelope.max_jump_rungs, (
                f"suggestion {rung} jumps past the safety cap"
            )
            probes.append(rung)
            envelope.record(rung, outcome_for_rung(rung))
            continue
        if envelope.edge_needs_confirmation():
            edge = envelope.edge_rung()
            assert edge is not None
            if confirm_passes(edge):
                envelope.record(edge, ProbeOutcome.PASS, confirmed=True)
            else:
                envelope.demote_edge()
            continue
        return probes
    raise AssertionError("search did not terminate")


def simple_gpu(true_edge: int):
    """Monotone silicon: passes down to true_edge, hard-fails below it."""

    def outcome(rung: int) -> ProbeOutcome:
        return ProbeOutcome.PASS if rung <= true_edge else ProbeOutcome.HARD_FAIL

    return outcome


def test_converges_on_random_monotone_gpus() -> None:
    rng = random.Random(0xA07)
    for _ in range(300):
        rung_count = rng.randint(1, 40)
        true_edge = rng.randint(-1, rung_count - 1)  # -1: fails everywhere
        envelope = LadderEnvelope(rung_count=rung_count)
        drive_search(envelope, simple_gpu(true_edge))
        expected = None if true_edge < 0 else true_edge
        assert envelope.edge_rung() == expected
        if expected is not None:
            assert not envelope.edge_needs_confirmation()


def test_floor_limited_gpu_reaches_ladder_end_without_fails() -> None:
    # The 5080 reference-scan shape: every candidate passes to the floor.
    envelope = LadderEnvelope(rung_count=17)
    probes = drive_search(envelope, simple_gpu(true_edge=16))
    assert envelope.edge_rung() == 16
    assert envelope.shallowest_hard_fail() is None
    # Jumping by 2 must beat the old one-rung walk.
    assert len(probes) <= 9


def test_weak_silicon_failing_the_first_rung() -> None:
    envelope = LadderEnvelope(rung_count=12)
    drive_search(envelope, simple_gpu(true_edge=-1))
    assert envelope.edge_rung() is None
    assert envelope.shallowest_hard_fail() == 0


def test_guard_fail_recovery_converges_from_scheduler_jump() -> None:
    # A prior-authorized guard probe lands deep, fails, and the walk from the
    # top must still find the true edge above it.
    envelope = LadderEnvelope(rung_count=20)
    envelope.record(15, ProbeOutcome.HARD_FAIL)
    drive_search(envelope, simple_gpu(true_edge=9))
    assert envelope.edge_rung() == 9


def test_guard_pass_skips_the_boring_top() -> None:
    # A prior-authorized guard pass near the edge collapses the search: only
    # the rungs below the guard still need probing.
    envelope = LadderEnvelope(rung_count=20)
    envelope.record(14, ProbeOutcome.PASS)
    probes = drive_search(envelope, simple_gpu(true_edge=16))
    assert envelope.edge_rung() == 16
    assert all(rung > 14 for rung in probes)
    assert len(probes) <= 4


def test_low_clock_blocks_non_efficiency_search() -> None:
    def outcome(rung: int) -> ProbeOutcome:
        if rung >= 6:
            return ProbeOutcome.LOW_CLOCK
        return ProbeOutcome.PASS

    envelope = LadderEnvelope(rung_count=15, descend_through_low_clock=False)
    drive_search(envelope, outcome)
    assert envelope.edge_rung() == 5
    assert envelope.shallowest_hard_fail() is None


def test_low_clock_band_is_traversed_in_efficiency_mode() -> None:
    band = {6, 7, 8}

    def outcome(rung: int) -> ProbeOutcome:
        if rung in band:
            return ProbeOutcome.LOW_CLOCK
        return ProbeOutcome.PASS if rung <= 11 else ProbeOutcome.HARD_FAIL

    envelope = LadderEnvelope(rung_count=15, descend_through_low_clock=True)
    drive_search(envelope, outcome)
    # The band is unusable but not condemning: the edge lies below it.
    assert envelope.edge_rung() == 11
    assert envelope.shallowest_hard_fail() == 12


def test_low_clock_never_moves_the_fail_boundary() -> None:
    envelope = LadderEnvelope(rung_count=10, descend_through_low_clock=True)
    envelope.record(4, ProbeOutcome.LOW_CLOCK)
    assert envelope.shallowest_hard_fail() is None
    assert envelope.search_floor() == 10


def test_confirm_failure_demotes_edge_and_reopens_skipped_rung() -> None:
    # Short probes pass down to rung 9 but rung 9 cannot survive a confirm
    # soak. The demotion must reopen any rung skipped by the 2-rung jump.
    flaky_edge = 9

    def confirm(rung: int) -> bool:
        return rung < flaky_edge

    envelope = LadderEnvelope(rung_count=14)
    drive_search(envelope, simple_gpu(true_edge=9), confirm_passes=confirm)
    edge = envelope.edge_rung()
    assert edge is not None
    assert edge < flaky_edge
    assert envelope.records[edge].confirmed
    assert envelope.records[flaky_edge].outcome is ProbeOutcome.HARD_FAIL


def test_confirm_failure_on_every_rung_ends_with_no_edge() -> None:
    # Nothing survives confirmation: demotion walks the edge all the way up
    # and the search must end cleanly with no edge instead of looping.
    envelope = LadderEnvelope(rung_count=4)
    drive_search(envelope, simple_gpu(true_edge=3), confirm_passes=lambda _rung: False)
    assert envelope.edge_rung() is None
    assert all(
        record.outcome is ProbeOutcome.HARD_FAIL for record in envelope.records.values()
    )


def test_hard_fail_is_never_exonerated_by_a_later_pass() -> None:
    envelope = LadderEnvelope(rung_count=8)
    envelope.record(3, ProbeOutcome.HARD_FAIL)
    envelope.record(3, ProbeOutcome.PASS, confirmed=True)
    assert envelope.records[3].outcome is ProbeOutcome.HARD_FAIL


def test_confirmed_pass_is_not_downgraded_by_provisional() -> None:
    envelope = LadderEnvelope(rung_count=8)
    envelope.record(2, ProbeOutcome.PASS, confirmed=True)
    envelope.record(2, ProbeOutcome.PASS)
    assert envelope.records[2].confirmed


def test_payload_round_trip_preserves_search_state() -> None:
    envelope = LadderEnvelope(rung_count=12, descend_through_low_clock=True)
    envelope.record(2, ProbeOutcome.PASS, confirmed=True)
    envelope.record(5, ProbeOutcome.LOW_CLOCK)
    envelope.record(7, ProbeOutcome.HARD_FAIL)
    restored = LadderEnvelope.from_payload(envelope.to_payload())
    assert restored.records == envelope.records
    assert restored.next_probe_rung() == envelope.next_probe_rung()
    assert restored.rung_count == envelope.rung_count


def test_rejects_out_of_range_rungs_and_bad_ladders() -> None:
    envelope = LadderEnvelope(rung_count=3)
    with pytest.raises(ValueError):
        envelope.record(3, ProbeOutcome.PASS)
    with pytest.raises(ValueError):
        LadderEnvelope(rung_count=0)
    with pytest.raises(ValueError):
        LadderEnvelope(rung_count=5, max_jump_rungs=0)


def test_records_are_plain_comparable_values() -> None:
    assert RungRecord(ProbeOutcome.PASS) == RungRecord(ProbeOutcome.PASS)
    assert RungRecord(ProbeOutcome.PASS) != RungRecord(ProbeOutcome.PASS, confirmed=True)
