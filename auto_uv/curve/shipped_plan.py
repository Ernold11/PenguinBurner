"""Sanitize a final plan so only scan-validated points ship.

The scan's probe curves shape bins below the candidate for load saturation
(the baseline flatten floor and soft decay). Those shapes are scan machinery,
not validated operating points — a real game dips through them on every load
transition. Below the lowest voltage the current scan actually probed, the
shipped profile must run the stock curve (2026-07-13: shipping the probe
debris and archive-envelope points there crashed fullscreen Q2RTX repeatedly
while every synthetic soak passed, because the soak pins at the lock voltage
and never operates the mid-ramp).
"""

from __future__ import annotations


def restore_stock_below_validated_floor(
    plan: list[dict],
    *,
    floor_voltage_mv: int,
) -> list[dict]:
    """Return the plan with every editable bin below the floor at stock."""
    floor = int(floor_voltage_mv)
    shipped: list[dict] = []
    for point in plan:
        new_point = dict(point)
        try:
            voltage_mv = int(point["voltage_mv"])
            base_mhz = int(point["base_mhz"])
        except (KeyError, TypeError, ValueError):
            shipped.append(new_point)
            continue
        if not point.get("preserve_base") and voltage_mv < floor:
            new_point["target_mhz"] = base_mhz
            new_point["new_offset_mhz"] = 0
        shipped.append(new_point)
    return shipped


def validated_floor_voltage_mv(
    stable_history,
    *,
    fallback_voltage_mv: int,
) -> int:
    """The lowest voltage the current scan proved with a passed probe."""
    voltages = [
        int(probe.candidate_voltage_mv)
        for probe in list(stable_history or [])
        if getattr(probe, "candidate_voltage_mv", None) is not None
    ]
    voltages.append(int(fallback_voltage_mv))
    return min(voltages)
