from __future__ import annotations

from auto_uv.domain.types import AutoUvProbeSummary
from .base_load_flatten_target import choose_sustained_curve_clock


def lock_clock_from_probe_loaded_clock(
    base_curve: list[dict],
    *,
    probe: AutoUvProbeSummary,
    previous_lock_clock_mhz: int,
) -> int:
    if probe.avg_core_clock_mhz is None:
        return int(previous_lock_clock_mhz)
    measured_lock_clock_mhz = choose_sustained_curve_clock(
        base_curve,
        float(probe.avg_core_clock_mhz),
    )
    return min(int(previous_lock_clock_mhz), int(measured_lock_clock_mhz))
