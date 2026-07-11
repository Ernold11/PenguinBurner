"""Pick every tier's lock point offline from one recorded sweep.

The old engine ran three separate descents because each tier stopped by its
own rule. Those rules are pure functions of the probe telemetry, so a single
trunk sweep that records every probe lets each tier's selection replay after
the fact:

- efficiency: the deepest stable probe above the efficiency floor. The old
  tail-tune second pass descended past the FPS/W wall anyway (wall disabled,
  low-clock bins skipped), so its outcome was always the deepest stable point.
- balanced: replay of the FPS/W wall over the stable probes in descent
  order, using the same comparison and stop functions the old loop called.
- performance: the deepest stable probe above the performance floor; the
  auto-OC ladder then climbs from there (orchestrator's job).

Tier shapes (rising tails, power limits, floors) come from the per-GPU
uv_limits table as data — never as behavior. A GPU without a table entry gets
open-ended specs and the sweep's own bounds take over.
"""

from __future__ import annotations

from dataclasses import dataclass

from auto_uv.base_uv_loop import float_or_none, voltage_drop_from_start_pct
from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.domain.types import VfCurveCandidate
from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.envelope import ProbeOutcome
from auto_uv.scan_mode.auto_uv_mode import (
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_PERFORMANCE,
)
from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    compare_temperature_normalized_fps_per_w,
    decide_efficiency_stop,
    power_increased_while_efficiency_flat,
)
from auto_uv.scan_mode.uv_limits import (
    uv_limit_power_limit_pct_for_gpu,
    uv_limit_profile_target_for_gpu,
)

_EFFICIENCY_TAIL_RISE_BINS = 2


@dataclass(frozen=True, slots=True)
class TierSpec:
    """Everything one tier needs from the shared sweep, as plain data."""

    tier: str
    tail_rise_bins: int
    uses_fps_per_w_wall: bool
    floor_voltage_mv: int | None
    target_clock_mhz: int | None
    power_limit_pct: float | None


@dataclass(frozen=True, slots=True)
class SweepProbeRecord:
    """One probed rung: the request that ran and what the probe reported.

    ``probe_payload`` is the raw probe as the runner produced it — a probe
    summary object live, or a plain dict after a journal round trip. The
    policies read it through ``read_field``, which accepts both.
    """

    voltage_mv: int
    target_clock_mhz: int
    outcome: ProbeOutcome
    probe_payload: object | None = None


def tier_specs_for_gpu(gpu_name: object | None) -> list[TierSpec]:
    """The three tier shapes for this GPU, table-seeded but never table-bound."""
    specs = []
    for tier, tail_bins, uses_wall in (
        (AUTO_UV_MODE_EFFICIENCY, _EFFICIENCY_TAIL_RISE_BINS, False),
        (AUTO_UV_MODE_BALANCED, AUTO_UV_DEFAULTS.balanced_tail_rise_bins, True),
        (AUTO_UV_MODE_PERFORMANCE, AUTO_UV_DEFAULTS.performance_tail_rise_bins, False),
    ):
        target = uv_limit_profile_target_for_gpu(gpu_name, tier)
        specs.append(
            TierSpec(
                tier=tier,
                tail_rise_bins=int(tail_bins),
                uses_fps_per_w_wall=uses_wall,
                # The old engine ran every tier to the same deep floor; tiers
                # differ by policy, never by descent depth (the July 5080
                # balanced scan locked at 850mV, the efficiency-table point).
                # The table's tier voltage/clock is the auto-OC target and the
                # validation bound — NOT a selection floor.
                floor_voltage_mv=None,
                target_clock_mhz=None if target is None else int(target.clock_mhz),
                power_limit_pct=uv_limit_power_limit_pct_for_gpu(gpu_name, tier),
            )
        )
    return specs


def select_tier_record(
    records: list[SweepProbeRecord],
    spec: TierSpec,
    *,
    start_voltage_mv: int,
    efficiency_stop_streak: int,
    min_efficiency_stop_voltage_drop_pct: float,
) -> SweepProbeRecord | None:
    """Choose the tier's lock record from the recorded sweep, or None when
    no stable probe exists above the tier's floor."""
    stable = stable_records_above_floor(records, spec.floor_voltage_mv)
    if not stable:
        return None
    if not spec.uses_fps_per_w_wall:
        return stable[-1]
    return replay_fps_per_w_wall(
        stable,
        start_voltage_mv=int(start_voltage_mv),
        required_extra_confirmations=max(0, int(efficiency_stop_streak)),
        min_voltage_drop_pct=float(min_efficiency_stop_voltage_drop_pct),
    )


def stable_records_above_floor(
    records: list[SweepProbeRecord],
    floor_voltage_mv: int | None,
) -> list[SweepProbeRecord]:
    """Stable probes in descent order; low-clock and failed rungs drop out.

    A voltage with ANY hard-fail record is excluded even when an earlier
    short probe passed there: a failed long confirm at that voltage outranks
    the short pass that preceded it.
    """
    hard_failed_voltages = {
        int(record.voltage_mv)
        for record in records
        if record.outcome is ProbeOutcome.HARD_FAIL
    }
    stable = [
        record
        for record in records
        if record.outcome is ProbeOutcome.PASS
        and int(record.voltage_mv) not in hard_failed_voltages
        and (floor_voltage_mv is None or int(record.voltage_mv) >= int(floor_voltage_mv))
    ]
    return sorted(stable, key=lambda record: -int(record.voltage_mv))


def replay_fps_per_w_wall(
    stable_descending: list[SweepProbeRecord],
    *,
    start_voltage_mv: int,
    required_extra_confirmations: int,
    min_voltage_drop_pct: float,
) -> SweepProbeRecord:
    """Walk the stable probes exactly like the old loop's passed-probe
    decision: keep descending while FPS/W improves, arm the stop on the first
    no-gain probe, and stop after the configured confirmations — keeping the
    earlier better candidate unless the last probe still broke even."""
    selected = stable_descending[0]
    no_gain_streak = 0
    pending_previous_curve = False
    for candidate in stable_descending[1:]:
        normalized = compare_temperature_normalized_fps_per_w(
            selected.probe_payload,
            candidate.probe_payload,
        )
        improved = normalized.get("improved")
        voltage_close = bool(normalized.get("measured_voltage_close_to_requested", True))
        if improved is not False or not voltage_close:
            selected = candidate
            no_gain_streak = 0
            pending_previous_curve = False
            continue
        no_gain_streak += 1
        stop = decide_efficiency_stop(
            efficiency_stop_candidate=True,
            voltage_drop_from_start_pct=voltage_drop_from_start_pct(
                start_voltage_mv=int(start_voltage_mv),
                candidate_voltage_mv=int(candidate.voltage_mv),
            ),
            min_voltage_drop_pct=float(min_voltage_drop_pct),
            no_gain_streak=no_gain_streak,
            required_extra_confirmations=int(required_extra_confirmations),
            pending_previous_curve=pending_previous_curve,
            power_up_efficiency_down=power_increased_while_efficiency_flat(
                previous_power_w=float_or_none(normalized.get("previous_power_w")),
                candidate_power_w=float_or_none(normalized.get("candidate_power_w")),
                previous_fps_per_w=float_or_none(normalized.get("previous_fps_per_w")),
                candidate_fps_per_w=float_or_none(normalized.get("candidate_fps_per_w")),
            ),
            efficiency_delta_pct=float_or_none(normalized.get("delta_pct")),
        )
        if stop.should_stop:
            if stop.use_current_curve:
                selected = candidate
            return selected
        pending_previous_curve = True
    return selected


def decorated_tier_candidate(
    base_curve: list[dict],
    record: SweepProbeRecord,
    spec: TierSpec,
) -> VfCurveCandidate:
    """The tier's final curve: the shared lock point wearing this tier's tail.

    The trunk swept tail-less; the tail is decoration applied here and then
    proven by the tier's own long confirm soak before final verification.
    """
    return build_flattened_voltage_probe_curve(
        base_curve,
        candidate_voltage_mv=int(record.voltage_mv),
        target_clock_mhz=int(record.target_clock_mhz),
        label=f"auto-uv {spec.tier}",
        tail_rise_bins=int(spec.tail_rise_bins),
        metadata={
            "tail_rise_bins": int(spec.tail_rise_bins),
            "auto_uv_mode": str(spec.tier),
            "generated_profile_tier": str(spec.tier),
        },
    )
