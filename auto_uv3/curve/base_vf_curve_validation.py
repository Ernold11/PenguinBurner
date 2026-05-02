"""Validate that the base V/F curve is safe enough for Auto-UV3 to edit.

The scan should fail before touching the GPU if editable voltage bins are missing or malformed.
"""

from __future__ import annotations

from .base_vf_curve import editable_base_vf_points


def validate_base_vf_curve(base_curve: list[dict]) -> None:
    editable_points = editable_base_vf_points(base_curve)
    editable_voltages = [int(point.voltage_mv) for point in editable_points]
    if len(editable_points) < 3:
        raise ValueError(
            "auto-UV needs at least 3 editable voltage-based core V/F points; "
            f"driver exposed {len(editable_points)}"
        )
    if len(set(editable_voltages)) != len(editable_voltages):
        raise ValueError("base V/F curve contains duplicate editable voltage bins")
    bad_points = [
        point
        for point in editable_points
        if int(point.voltage_mv) <= 0
        or int(point.base_mhz) <= 0
        or int(point.target_mhz) <= 0
    ]
    if bad_points:
        point = bad_points[0]
        raise ValueError(
            "base V/F curve contains invalid editable point "
            f"index={int(point.index)} voltage={int(point.voltage_mv)}mV "
            f"base={int(point.base_mhz)}MHz target={int(point.target_mhz)}MHz"
        )
