"""Back off a recovered clock target after a hard failure.

The backoff uses real base-curve target clocks so retry points stay on the driver grid.
"""

from __future__ import annotations


def step_back_clock_recovery_target(
    base_curve: list[dict],
    *,
    current_target_mhz: int,
    last_recovered_target_mhz: int | None,
) -> int | None:
    if last_recovered_target_mhz is None:
        return None
    lower_targets = sorted(
        {
            int(point["target_mhz"])
            for point in base_curve
            if int(point["target_mhz"]) < int(current_target_mhz)
        },
        reverse=True,
    )
    if lower_targets:
        return int(lower_targets[0])
    return None
