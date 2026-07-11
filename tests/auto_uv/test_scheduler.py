"""Tests for the sweep scheduler: ladder parity with the old walk, guard
placement rules, target ratcheting, bounded validation scans, and the
overnight resume journal."""

from __future__ import annotations

import pytest

from auto_uv.envelope import ProbeOutcome
from auto_uv.run.lower_voltage_search import select_next_lower_voltage
from auto_uv.scheduler import (
    PriorSource,
    ProbeRequest,
    SweepPrior,
    SweepScheduler,
    build_voltage_ladder,
)
from tests.auto_uv.auto_uv_test_data import base_curve, wide_base_curve


START_MV = 1240


def old_walk(curve: list[dict], *, start_voltage_mv: int, min_search_voltage_mv: int | None) -> list[int]:
    """Reproduce the old loop's descent: one selection per stable probe."""
    sequence: list[int] = []
    stable_mv = int(start_voltage_mv)
    while True:
        next_mv = select_next_lower_voltage(
            curve,
            start_voltage_mv=int(start_voltage_mv),
            stable_voltage_mv=stable_mv,
            reference_actual_voltage_mv=None,
            min_search_voltage_mv=min_search_voltage_mv,
        )
        if next_mv is None:
            return sequence
        sequence.append(int(next_mv))
        stable_mv = int(next_mv)


@pytest.mark.parametrize("min_search_mv", [None, 850, 990])
def test_ladder_matches_the_old_iterative_walk(min_search_mv: int | None) -> None:
    curve = wide_base_curve()
    ladder = build_voltage_ladder(
        curve,
        start_voltage_mv=START_MV,
        min_search_voltage_mv=min_search_mv,
    )
    assert ladder == old_walk(
        curve, start_voltage_mv=START_MV, min_search_voltage_mv=min_search_mv
    )
    assert ladder, "wide curve must produce a non-empty ladder"


def test_ladder_is_empty_below_the_search_floor() -> None:
    curve = base_curve()
    assert build_voltage_ladder(curve, start_voltage_mv=790, min_search_voltage_mv=None) == []
    scheduler = SweepScheduler(curve, start_voltage_mv=790, stable_target_mhz=2000)
    assert scheduler.next_request() is None
    assert scheduler.is_complete()
    assert scheduler.edge_candidate() is None


def make_scheduler(**overrides) -> SweepScheduler:
    defaults = dict(
        start_voltage_mv=START_MV,
        stable_target_mhz=2900,
        min_search_voltage_mv=840,
    )
    defaults.update(overrides)
    return SweepScheduler(wide_base_curve(), **defaults)


def run_to_completion(
    scheduler: SweepScheduler,
    outcome_for_request,
    *,
    max_steps: int = 500,
) -> list[ProbeRequest]:
    requests: list[ProbeRequest] = []
    for _ in range(max_steps):
        request = scheduler.next_request()
        if request is None:
            return requests
        requests.append(request)
        outcome, measured = outcome_for_request(request)
        scheduler.apply_result(request, outcome, measured_clock_mhz=measured)
    raise AssertionError("scheduler did not complete")


def stable_above(edge_voltage_mv: int):
    def outcome(request: ProbeRequest):
        if request.voltage_mv >= edge_voltage_mv:
            return ProbeOutcome.PASS, request.target_clock_mhz
        return ProbeOutcome.HARD_FAIL, None

    return outcome


def test_walk_without_prior_finds_the_edge_and_confirms_it() -> None:
    scheduler = make_scheduler()
    requests = run_to_completion(scheduler, stable_above(900))
    edge = scheduler.edge_candidate()
    assert edge is not None
    assert edge.voltage_mv >= 900
    # The last applied request must be the confirm soak on the edge rung.
    assert requests[-1].confirm and requests[-1].rung == edge.rung
    assert sum(1 for r in requests if r.confirm) == 1


def test_own_scan_prior_places_guard_and_skips_the_boring_top() -> None:
    prior = SweepPrior(source=PriorSource.OWN_SCAN, edge_voltage_mv=900)
    with_prior = make_scheduler(prior=prior)
    first = with_prior.next_request()
    assert first is not None
    # Guard sits two rungs above the remembered edge, well below the start.
    assert 900 < first.voltage_mv < START_MV - 50

    baseline = run_to_completion(make_scheduler(), stable_above(900))
    guarded = run_to_completion(make_scheduler(prior=prior), stable_above(900))
    assert len(guarded) < len(baseline)
    assert make_scheduler(prior=prior).next_request() == first


def test_table_prior_never_authorizes_a_jump() -> None:
    for source in (PriorSource.TABLE, PriorSource.NONE):
        scheduler = make_scheduler(prior=SweepPrior(source=source, edge_voltage_mv=900))
        no_prior = make_scheduler()
        assert scheduler.next_request() == no_prior.next_request()


def test_failed_guard_recovers_to_the_true_edge() -> None:
    # The remembered edge is stale: the card now fails there. The walk from
    # the top must still converge to the true (shallower) edge.
    prior = SweepPrior(source=PriorSource.OWN_SCAN, edge_voltage_mv=880)
    scheduler = make_scheduler(prior=prior)
    run_to_completion(scheduler, stable_above(1000))
    edge = scheduler.edge_candidate()
    assert edge is not None
    assert edge.voltage_mv >= 1000


def test_prior_below_ladder_floor_guards_near_the_end() -> None:
    prior = SweepPrior(source=PriorSource.OWN_SCAN, edge_voltage_mv=100)
    scheduler = make_scheduler(prior=prior)
    first = scheduler.next_request()
    assert first is not None
    assert first.voltage_mv <= scheduler.ladder[-1] + 3 * 15


def test_measured_clock_ratchets_the_next_target() -> None:
    scheduler = make_scheduler()
    first = scheduler.next_request()
    assert first is not None
    scheduler.apply_result(first, ProbeOutcome.PASS, measured_clock_mhz=2500)
    second = scheduler.next_request()
    assert second is not None
    assert second.target_clock_mhz == 2500


def test_target_ceiling_bounds_validation_scans() -> None:
    # Bounded validation: never request a clock above the known-good profile
    # (5080 perf rule: cap at the 925mV/2980MHz table point).
    scheduler = make_scheduler(target_clock_ceiling_mhz=2400)
    requests = run_to_completion(scheduler, stable_above(900))
    assert requests
    assert all(r.target_clock_mhz <= 2400 for r in requests)


def test_voltage_floor_bounds_validation_scans() -> None:
    scheduler = make_scheduler(min_search_voltage_mv=925)
    requests = run_to_completion(scheduler, stable_above(0))
    assert requests
    assert all(r.voltage_mv >= 925 for r in requests)


def test_journal_saves_after_every_result_and_resumes() -> None:
    journal: list[dict] = []
    scheduler = make_scheduler(save_journal=journal.append)
    applied = run_to_completion(scheduler, stable_above(900))
    assert len(journal) == len(applied)

    resumed = make_scheduler()
    resumed.restore_payload(journal[-1])
    assert resumed.is_complete()
    original_edge = scheduler.edge_candidate()
    resumed_edge = resumed.edge_candidate()
    assert original_edge is not None and resumed_edge is not None
    assert resumed_edge.voltage_mv == original_edge.voltage_mv

    # Resume mid-scan: replaying from journal entry N continues, not restarts.
    partial = make_scheduler()
    partial.restore_payload(journal[2])
    request = partial.next_request()
    assert request is not None
    assert request == applied[3]


def test_journal_from_a_different_ladder_is_rejected() -> None:
    journal: list[dict] = []
    scheduler = make_scheduler(save_journal=journal.append)
    first = scheduler.next_request()
    assert first is not None
    scheduler.apply_result(first, ProbeOutcome.PASS)

    other = make_scheduler(min_search_voltage_mv=1000)
    with pytest.raises(ValueError, match="ladder"):
        other.restore_payload(journal[-1])


def test_low_clock_rungs_end_non_efficiency_search_but_not_efficiency() -> None:
    def low_below_900(request: ProbeRequest):
        if request.voltage_mv >= 900:
            return ProbeOutcome.PASS, request.target_clock_mhz
        if request.voltage_mv >= 860:
            return ProbeOutcome.LOW_CLOCK, None
        return ProbeOutcome.PASS, request.target_clock_mhz

    strict = make_scheduler()
    run_to_completion(strict, low_below_900)
    strict_edge = strict.edge_candidate()
    assert strict_edge is not None
    assert strict_edge.voltage_mv >= 900

    efficiency = make_scheduler(descend_through_low_clock=True)
    run_to_completion(efficiency, low_below_900)
    eff_edge = efficiency.edge_candidate()
    assert eff_edge is not None
    assert eff_edge.voltage_mv < 860
