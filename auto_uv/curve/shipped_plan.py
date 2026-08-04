"""Sanitize a final plan so only scan-validated curve geometry ships.

The scan's probe curves shape bins below the candidate for load saturation
(the baseline flatten floor and soft decay). Those shapes are scan machinery,
not validated operating points — a real game dips through them on every load
transition. Below the lowest voltage the current scan actually probed, the
shipped profile must remain at or below the stock curve (2026-07-13: shipping
raised probe debris and archive-envelope points there crashed fullscreen Q2RTX
repeatedly while every synthetic soak passed, because the soak pins at the
lock voltage and never operates the mid-ramp).

Raw stock is not always safe curve *geometry*, though. On steep Blackwell
curves stock immediately below the validated floor can exceed the selected
lock clock, creating a downward V/F edge at the lock. Clamp those restored
points to the same one-clock-step-below-lock ceiling used by probe curves.
"""

from __future__ import annotations

from auto_uv.domain.types import AutoUvError


def restore_stock_below_validated_floor(
    plan: list[dict],
    *,
    floor_voltage_mv: int,
    lock_clock_mhz: int,
    below_lock_gap_mhz: int = 15,
) -> list[dict]:
    """Restore safe stock geometry below the floor without a lock-edge cliff.

    Every rewritten target is at most its stock clock and at least one clock
    step below the selected lock. The result is rejected if the complete
    editable curve is still non-monotonic; silently raising a point would ship
    unverified frequency, while silently lowering a validated point would no
    longer be the plan that passed its probe.
    """
    floor = int(floor_voltage_mv)
    below_lock_cap_mhz = max(
        0,
        int(lock_clock_mhz) - max(1, int(below_lock_gap_mhz)),
    )
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
            target_mhz = min(int(base_mhz), int(below_lock_cap_mhz))
            new_point["target_mhz"] = int(target_mhz)
            new_point["new_offset_mhz"] = int(target_mhz) - int(base_mhz)
        shipped.append(new_point)
    assert_monotonic_editable_targets(shipped)
    return shipped


def assert_monotonic_editable_targets(plan: list[dict]) -> None:
    """Reject a shipped editable V/F curve that falls as voltage rises."""
    points: list[tuple[int, int]] = []
    for point in plan:
        if point.get("preserve_base"):
            continue
        try:
            points.append((int(point["voltage_mv"]), int(point["target_mhz"])))
        except (KeyError, TypeError, ValueError):
            continue
    points.sort(key=lambda item: item[0])
    for previous, current in zip(points, points[1:]):
        if int(current[1]) < int(previous[1]):
            raise AutoUvError(
                "refusing to ship non-monotonic V/F curve: "
                f"{int(previous[0])}mV@{int(previous[1])}MHz -> "
                f"{int(current[0])}mV@{int(current[1])}MHz"
            )


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
