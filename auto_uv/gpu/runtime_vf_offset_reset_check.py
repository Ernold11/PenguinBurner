"""Verify runtime V/F offsets were cleared before Auto-UV applies a curve.

Starting from non-zero offsets would make the measured curve impossible to explain or reproduce.
"""

from __future__ import annotations

from auto_uv.domain.types import AutoUvError


def assert_zero_runtime_vf_offsets(reader) -> None:
    reader.refresh_points()
    stale = []
    for point in reader.editable_core_points():
        current_offset_mhz = int(point["current_offset_khz"] // 1000)
        if current_offset_mhz != 0:
            stale.append(
                f"{int(point['voltage_uv'] // 1000)}mV={current_offset_mhz:+d}MHz"
            )
    if stale:
        sample = ", ".join(stale[:12])
        suffix = f", ... {len(stale) - 12} more" if len(stale) > 12 else ""
        raise AutoUvError(
            f"failed to clear per-point V/F offsets before Auto-UV: {sample}{suffix}"
        )
