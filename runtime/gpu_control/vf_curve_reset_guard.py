"""Detect when the driver reset applied V/F offsets.

The guard samples the largest expected offsets and reports the exact voltage bins that drifted.
"""

from __future__ import annotations


def select_expected_vf_samples(plan, *, max_samples=8):
    candidates = [item for item in plan if int(item["new_offset_mhz"]) != 0]
    candidates.sort(key=lambda item: abs(int(item["new_offset_mhz"])), reverse=True)
    return candidates[:max_samples]


def detect_vf_curve_reset(vf_curve_reader, expected_samples, *, tolerance_mhz=1):
    if vf_curve_reader is None or not expected_samples:
        return []

    current_points = {
        int(point["index"]): int(point["current_offset_khz"] // 1000)
        for point in vf_curve_reader.editable_core_points()
    }
    mismatches = []
    for sample in expected_samples:
        index = int(sample["index"])
        expected_offset_mhz = int(sample["new_offset_mhz"])
        current_offset_mhz = int(current_points.get(index, 0))
        if abs(current_offset_mhz - expected_offset_mhz) > int(tolerance_mhz):
            mismatches.append(
                {
                    "index": index,
                    "expected_offset_mhz": expected_offset_mhz,
                    "current_offset_mhz": current_offset_mhz,
                    "voltage_mv": int(sample["voltage_mv"]),
                }
            )
    return mismatches


def format_vf_curve_mismatch_preview(mismatches, *, max_items=4) -> str:
    preview = ", ".join(
        (
            f"{int(item['voltage_mv'])}mV:"
            f"{int(item['current_offset_mhz']):+d}->"
            f"{int(item['expected_offset_mhz']):+d}MHz"
        )
        for item in list(mismatches)[: int(max_items)]
    )
    if len(mismatches) > int(max_items):
        preview += ", ..."
    return preview
