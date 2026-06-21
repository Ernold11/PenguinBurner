from __future__ import annotations

from dataclasses import dataclass

from auto_uv.domain.scan_settings import AutoUvScanSettings


@dataclass(frozen=True, slots=True)
class VoltageDescentCandidatePolicy:
    target_mhz: int
    tail_rise_bins: int


def voltage_descent_candidate_policy(
    *,
    settings: AutoUvScanSettings,
    stable_target_mhz: int,
) -> VoltageDescentCandidatePolicy:
    """Keep the requested clock fixed while the sweep lowers voltage."""

    tail_rise_bins = voltage_descent_tail_rise_bins(settings)
    return VoltageDescentCandidatePolicy(
        target_mhz=int(stable_target_mhz),
        tail_rise_bins=int(tail_rise_bins),
    )


def voltage_descent_tail_rise_bins(settings: AutoUvScanSettings) -> int:
    return max(0, int(settings.tail_rise_bins))
