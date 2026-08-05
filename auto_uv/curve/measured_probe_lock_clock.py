from __future__ import annotations

from auto_uv.domain.types import AutoUvProbeSummary
from .base_load_flatten_target import choose_sustained_curve_clock


def lock_clock_from_probe_loaded_clock(
    base_curve: list[dict],
    *,
    probe: AutoUvProbeSummary,
    previous_lock_clock_mhz: int,
    power_limit_w: int | None = None,
    power_saturation_headroom_pct: float = 2.0,
) -> int:
    if probe.avg_core_clock_mhz is None:
        return int(previous_lock_clock_mhz)
    if probe_indicates_power_saturation(
        probe,
        power_limit_w=power_limit_w,
        power_saturation_headroom_pct=float(power_saturation_headroom_pct),
    ):
        # A power governor may hold the loaded clock below the curve target.
        # That proves an operated point under the cap; it is not evidence that
        # the requested V/F target itself needs to be ratcheted downward.
        return int(previous_lock_clock_mhz)
    measured_lock_clock_mhz = choose_sustained_curve_clock(
        base_curve,
        float(probe.avg_core_clock_mhz),
    )
    return min(int(previous_lock_clock_mhz), int(measured_lock_clock_mhz))


def probe_indicates_power_saturation(
    probe: AutoUvProbeSummary,
    *,
    power_limit_w: int | None,
    power_saturation_headroom_pct: float = 2.0,
    require_power_evidence: bool = False,
) -> bool:
    """Whether this probe ran against the board-power wall.

    The driver's perf-cap reason alone is a weak signal: Blackwell summarizes
    a power cap on probes drawing well under the configured limit (287W of a
    319W cap in the 2026-08-04 log). That is enough to stop the scan from
    ratcheting a target downward — a conservative, reversible decision — but
    not enough to conclude the board CANNOT go faster. Callers making that
    stronger capability claim pass ``require_power_evidence`` so only measured
    power at the limit counts.
    """
    perf_cap_reason = str(getattr(probe, "perf_cap_reason", "") or "").lower()
    if not require_power_evidence and any(
        "power" in token for token in perf_cap_reason.replace(",", "+").split("+")
    ):
        return True
    avg_power_w = getattr(probe, "avg_power_w", None)
    if avg_power_w is None or power_limit_w is None or int(power_limit_w) <= 0:
        return False
    saturation_floor_w = float(power_limit_w) * (
        1.0 - max(0.0, float(power_saturation_headroom_pct)) / 100.0
    )
    return float(avg_power_w) >= float(saturation_floor_w)
