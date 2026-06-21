"""Read the base V/F curve plan dictionaries into typed points.

This module owns shape conversion only; search, flattening, and safety rules live elsewhere.
"""

from __future__ import annotations

from auto_uv.domain.types import BaseVfPoint


def read_base_vf_points(base_curve: list[dict]) -> list[BaseVfPoint]:
    points = []
    for item in base_curve:
        preserve_base = bool(item.get("preserve_base", item.get("preserve_vanilla")))
        points.append(
            BaseVfPoint(
                index=int(item["index"]),
                voltage_mv=int(item["voltage_mv"]),
                base_mhz=int(item["base_mhz"]),
                target_mhz=int(item["target_mhz"]),
                preserve_base=preserve_base,
                original=dict(item),
            )
        )
    return points


def editable_base_vf_points(base_curve: list[dict]) -> list[BaseVfPoint]:
    return [
        point
        for point in sorted(read_base_vf_points(base_curve), key=_point_voltage)
        if not bool(point.preserve_base)
    ]


def _point_voltage(point: BaseVfPoint) -> int:
    return int(point.voltage_mv)
