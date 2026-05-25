"""Describe final-verification probes for the persistent crash cache.

The marker records voltage drop and memory offset so abrupt exits can be blacklisted safely.
"""

from __future__ import annotations


def memory_offset_from_gpu_policy(translated_gpu_policy: dict | None) -> int | None:
    if not isinstance(translated_gpu_policy, dict):
        return None
    value = translated_gpu_policy.get("mem_clk_vf_offset_mhz")
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def final_probe_crash_marker_details(
    *,
    start_voltage_mv: int,
    candidate_voltage_mv: int,
    translated_gpu_policy: dict | None,
) -> dict:
    start_voltage = max(1, int(start_voltage_mv))
    voltage_drop_pct = (
        (float(start_voltage) - float(candidate_voltage_mv)) / float(start_voltage)
    ) * 100.0
    details = {
        "start_voltage_mv": int(start_voltage_mv),
        "voltage_drop_from_start_pct": round(float(voltage_drop_pct), 4),
        "memory_offset_mhz": memory_offset_from_gpu_policy(translated_gpu_policy) or 0,
    }
    return details
