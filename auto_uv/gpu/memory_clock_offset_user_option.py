"""Clamp the optional memory-clock offset before applying it during Auto-UV.

The driver-reported NVML offset range is the authority; the static
Afterburner-style cap only applies when the driver does not expose a range.
"""

from __future__ import annotations

from drivers.nvidia.nvml_gpu_policy import driver_memory_offset_limit_mhz


def auto_uv_memory_offset_mhz(
    runtime_options: dict,
    *,
    policy_controller=None,
) -> tuple[int | None, int]:
    effective_max = driver_memory_offset_limit_mhz(policy_controller)
    raw_value = runtime_options.get(
        "auto_uv_memory_offset_mhz",
        runtime_options.get("memory_offset_mhz"),
    )
    if raw_value in (None, ""):
        return None, int(effective_max)

    requested = max(0, min(int(effective_max), int(raw_value)))
    return int(requested), int(effective_max)
