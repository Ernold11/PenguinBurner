"""Normalize the base V/F curve into typed points and back into plan dictionaries.

This module owns shape conversion only; search, flattening, and safety rules live elsewhere.
"""

from __future__ import annotations

from ..auto_uv_types import BaseVfPoint


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


def write_base_vf_points(points: list[BaseVfPoint]) -> list[dict]:
    return [write_base_vf_point(point) for point in points]


def write_base_vf_point(point: BaseVfPoint) -> dict:
    item = dict(point.original)
    item.pop("preserve_vanilla", None)
    item["index"] = int(point.index)
    item["voltage_mv"] = int(point.voltage_mv)
    item["base_mhz"] = int(point.base_mhz)
    item["target_mhz"] = int(point.target_mhz)
    item["new_offset_mhz"] = int(point.new_offset_mhz)
    if bool(point.preserve_base) or "preserve_base" in item:
        item["preserve_base"] = bool(point.preserve_base)
    return item


def editable_base_vf_points(base_curve: list[dict]) -> list[BaseVfPoint]:
    return [
        point
        for point in sorted(read_base_vf_points(base_curve), key=_point_voltage)
        if not bool(point.preserve_base)
    ]


def _point_voltage(point: BaseVfPoint) -> int:
    return int(point.voltage_mv)
