"""Create one flattened V/F curve candidate for a voltage probe.

It names the probe and delegates the actual plateau/ramp math to vf_curve_flattening.
"""

from __future__ import annotations

from auto_uv.domain.types import VfCurveCandidate
from .vf_curve_flattening import FlatteningRules, build_flattened_plan


def build_flattened_voltage_probe_curve(
    base_curve: list[dict],
    *,
    candidate_voltage_mv: int,
    target_clock_mhz: int,
    label: str,
    below_lock_gap_mhz: int | None = None,
    tail_rise_bins: int = 0,
    metadata: dict[str, object] | None = None,
    rules: FlatteningRules = FlatteningRules(),
) -> VfCurveCandidate:
    plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(target_clock_mhz),
        candidate_voltage_mv=int(candidate_voltage_mv),
        below_lock_gap_mhz=below_lock_gap_mhz,
        tail_rise_bins=int(tail_rise_bins),
        rules=rules,
    )
    return VfCurveCandidate(
        label=str(label),
        voltage_mv=int(candidate_voltage_mv),
        target_mhz=int(target_clock_mhz),
        flattened_plan=plan,
        metadata=dict(metadata or {}),
    )
