"""Tests for offline tier selection over one recorded sweep."""

from __future__ import annotations

import pytest

from auto_uv.envelope import ProbeOutcome
from auto_uv.tier_policies import (
    SweepProbeRecord,
    TierSpec,
    decorated_tier_candidate,
    replay_fps_per_w_wall,
    select_tier_record,
    stable_records_above_floor,
    tier_specs_for_gpu,
)
from tests.auto_uv.auto_uv_test_data import wide_base_curve


def payload(voltage_mv: int, fps: float, power_w: float, temp_c: float = 64.0) -> dict:
    return {
        "candidate_voltage_mv": voltage_mv,
        "avg_voltage_mv": float(voltage_mv),
        "avg_fps": float(fps),
        "avg_power_w": float(power_w),
        "avg_temperature_c": float(temp_c),
    }


def record(
    voltage_mv: int,
    *,
    clock: int = 2600,
    outcome: ProbeOutcome = ProbeOutcome.PASS,
    fps: float = 60.0,
    power_w: float = 300.0,
) -> SweepProbeRecord:
    return SweepProbeRecord(
        voltage_mv=voltage_mv,
        target_clock_mhz=clock,
        outcome=outcome,
        probe_payload=payload(voltage_mv, fps, power_w),
    )


def spec(
    tier: str = "balanced",
    *,
    uses_wall: bool = True,
    floor: int | None = None,
    tail: int = 4,
) -> TierSpec:
    return TierSpec(
        tier=tier,
        tail_rise_bins=tail,
        uses_fps_per_w_wall=uses_wall,
        floor_voltage_mv=floor,
        target_clock_mhz=None,
        power_limit_pct=None,
    )


WALL_KWARGS = dict(
    start_voltage_mv=1000,
    required_extra_confirmations=2,
    min_efficiency_stop_voltage_drop_pct=10.0,
)


def run_wall(stable: list[SweepProbeRecord]) -> SweepProbeRecord:
    return replay_fps_per_w_wall(
        stable,
        start_voltage_mv=WALL_KWARGS["start_voltage_mv"],
        required_extra_confirmations=WALL_KWARGS["required_extra_confirmations"],
        min_voltage_drop_pct=WALL_KWARGS["min_efficiency_stop_voltage_drop_pct"],
    )


def test_monotone_improvement_selects_the_deepest_probe() -> None:
    # The 5080 reference-scan shape: FPS/W improves all the way to the floor.
    stable = [record(v, power_w=300.0 - (1000 - v) / 5.0) for v in range(1000, 840, -20)]
    assert run_wall(stable).voltage_mv == 860


def test_wall_keeps_the_earlier_best_after_confirmed_no_gain() -> None:
    stable = [
        record(940, power_w=300.0),
        record(920, power_w=290.0),  # clear gain: becomes the selection
        record(900, power_w=289.6),  # no-gain 1: arms the stop
        record(880, power_w=289.5),  # no-gain 2: confirming
        record(860, power_w=292.0),  # no-gain 3, worse: stop, keep 920
    ]
    assert run_wall(stable).voltage_mv == 920


def test_wall_uses_the_last_probe_when_it_still_breaks_even() -> None:
    stable = [
        record(940, power_w=300.0),
        record(920, power_w=290.0),
        record(900, power_w=289.9),
        record(880, power_w=289.8),
        record(860, power_w=289.7),  # flat but not worse and power went down
    ]
    assert run_wall(stable).voltage_mv == 860


def test_wall_never_stops_before_the_minimum_voltage_drop() -> None:
    # All probes sit above 900mV (10% below the 1000mV start), so however
    # flat the FPS/W gets the wall may not stop; the last selection wins.
    stable = [record(v, power_w=290.0) for v in (980, 970, 960, 950, 940, 930, 920, 910)]
    result = run_wall(stable)
    assert result.voltage_mv == 980  # nothing ever improved, nothing stopped


def test_vdroop_dead_zone_does_not_count_as_no_gain() -> None:
    # Measured voltage barely moved: the probe is inside the droop dead zone
    # and must not arm the wall even though FPS/W was flat.
    dead_zone = SweepProbeRecord(
        voltage_mv=900,
        target_clock_mhz=2600,
        outcome=ProbeOutcome.PASS,
        probe_payload={**payload(900, 60.0, 290.0), "avg_voltage_mv": 919.0},
    )
    stable = [record(940, power_w=300.0), record(920, power_w=290.0), dead_zone]
    assert run_wall(stable).voltage_mv == 900


TRUNK = [
    record(1000, clock=2686),
    record(960, clock=2660),
    record(925, clock=2640, power_w=290.0),
    record(900, clock=2620, power_w=280.0),
    record(880, clock=2610, outcome=ProbeOutcome.LOW_CLOCK),
    record(860, clock=2600, power_w=270.0),
    record(850, clock=2600, power_w=268.0),
    record(840, clock=2590, outcome=ProbeOutcome.HARD_FAIL),
]


def select(tier_spec: TierSpec) -> SweepProbeRecord | None:
    return select_tier_record(
        TRUNK,
        tier_spec,
        start_voltage_mv=1000,
        efficiency_stop_streak=2,
        min_efficiency_stop_voltage_drop_pct=10.0,
    )


def test_efficiency_takes_the_deepest_stable_probe_through_low_clock() -> None:
    chosen = select(spec("efficiency", uses_wall=False, floor=850, tail=2))
    assert chosen is not None
    assert chosen.voltage_mv == 850


def test_performance_respects_its_floor() -> None:
    chosen = select(spec("performance", uses_wall=False, floor=925, tail=6))
    assert chosen is not None
    assert chosen.voltage_mv == 925


def test_no_stable_probe_above_the_floor_returns_none() -> None:
    assert select(spec("performance", uses_wall=False, floor=1100)) is None


def test_stable_records_drop_failures_and_sort_by_descent() -> None:
    stable = stable_records_above_floor(list(reversed(TRUNK)), 850)
    assert [r.voltage_mv for r in stable] == [1000, 960, 925, 900, 860, 850]


def test_tier_specs_for_the_5080_come_from_the_table() -> None:
    eff, bal, perf = tier_specs_for_gpu("NVIDIA GeForce RTX 5080")
    assert (eff.tier, bal.tier, perf.tier) == ("efficiency", "balanced", "performance")
    assert (eff.tail_rise_bins, bal.tail_rise_bins, perf.tail_rise_bins) == (2, 4, 6)
    # Selection floors are never tier-differentiated: every tier reads the
    # same deep trunk (the July 5080 balanced scan locked at 850mV).
    assert (eff.floor_voltage_mv, bal.floor_voltage_mv, perf.floor_voltage_mv) == (
        None,
        None,
        None,
    )
    assert perf.target_clock_mhz == 2980
    assert perf.power_limit_pct == 100.0
    assert eff.power_limit_pct is not None and eff.power_limit_pct < 100.0
    assert (eff.uses_fps_per_w_wall, bal.uses_fps_per_w_wall, perf.uses_fps_per_w_wall) == (
        False,
        True,
        False,
    )


def test_unknown_gpu_gets_open_ended_specs() -> None:
    specs = tier_specs_for_gpu("Totally Unknown GPU 9000")
    assert len(specs) == 3
    for tier_spec in specs:
        assert tier_spec.floor_voltage_mv is None
        assert tier_spec.target_clock_mhz is None
        assert tier_spec.power_limit_pct is None


def test_decorated_candidate_wears_the_tier_tail() -> None:
    curve = wide_base_curve()
    chosen = SweepProbeRecord(voltage_mv=900, target_clock_mhz=2400, outcome=ProbeOutcome.PASS)
    candidate = decorated_tier_candidate(curve, chosen, spec("balanced", floor=900, tail=4))
    assert int(candidate.voltage_mv) == 900
    assert int(candidate.target_mhz) == 2400
    metadata = dict(candidate.metadata or {})
    assert metadata["tail_rise_bins"] == 4
    assert metadata["auto_uv_mode"] == "balanced"
    tail_targets = [
        int(point["target_mhz"])
        for point in candidate.flattened_plan
        if int(point["voltage_mv"]) > 900
    ]
    assert tail_targets and max(tail_targets) == 2400 + 4 * 15
    assert all(target >= 2400 for target in tail_targets)


def test_zero_tail_stays_flat() -> None:
    curve = wide_base_curve()
    chosen = SweepProbeRecord(voltage_mv=900, target_clock_mhz=2400, outcome=ProbeOutcome.PASS)
    candidate = decorated_tier_candidate(curve, chosen, spec("efficiency", floor=850, tail=0))
    tail_targets = {
        int(point["target_mhz"])
        for point in candidate.flattened_plan
        if int(point["voltage_mv"]) > 900
    }
    assert tail_targets == {2400}


def test_wall_requires_at_least_one_stable_record() -> None:
    with pytest.raises(IndexError):
        run_wall([])
