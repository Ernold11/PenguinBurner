"""Convert an Auto-UV3 V/F curve plan into UI plot points.

The UI expects sorted voltage points with target clock, base clock, and offset.
"""

from __future__ import annotations


def vf_curve_ui_points(plan: list[dict]) -> list[dict]:
    points = []
    for item in sorted(plan, key=lambda value: int(value["voltage_mv"])):
        target_mhz = int(item["target_mhz"])
        base_mhz = int(item["base_mhz"])
        points.append(
            {
                "voltage_mv": int(item["voltage_mv"]),
                "clock_mhz": target_mhz,
                "base_mhz": base_mhz,
                "offset_mhz": target_mhz - base_mhz,
            }
        )
    return points
