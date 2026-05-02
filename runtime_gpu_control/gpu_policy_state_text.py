"""Format current GPU policy values for foreground logs.

These helpers have no NVML side effects; they only convert queried values to text.
"""

from __future__ import annotations


def describe_current_gpu_policy_state(power_limits, clock_offsets):
    parts = []

    current_limit_w = power_limits.get("power_limit_w")
    default_limit_w = power_limits.get("power_limit_default_w")
    min_limit_w = power_limits.get("power_limit_min_w")
    max_limit_w = power_limits.get("power_limit_max_w")
    if current_limit_w is not None:
        power_text = f"power-limit={int(current_limit_w)}W"
        if default_limit_w is not None:
            power_text += f" default={int(default_limit_w)}W"
        if min_limit_w is not None and max_limit_w is not None:
            power_text += f" range={int(min_limit_w)}-{int(max_limit_w)}W"
        parts.append(power_text)

    mem_offset_mhz = clock_offsets.get("mem_clk_vf_offset_mhz")
    if mem_offset_mhz is not None:
        parts.append(f"mem-vf-offset={int(mem_offset_mhz):+d}MHz")

    return ", ".join(parts) if parts else "none"


def khz_to_mhz(value):
    if value is None:
        return None
    return int(round(float(value) / 1000.0))
