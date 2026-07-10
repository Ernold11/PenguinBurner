"""Clamp the optional memory-clock offset before applying it during Auto-UV.

The driver-reported NVML offset range is the authority; the static
Afterburner-style cap only applies when the driver does not expose a range.
"""

from __future__ import annotations

from integrations.afterburner.policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ


def driver_memory_offset_limit_mhz(policy_controller=None) -> int:
    """Use the driver range when exposed, otherwise the proven fallback cap."""
    if policy_controller is not None:
        try:
            driver_range = policy_controller.get_memory_clock_offset_range_mhz()
        except Exception:
            driver_range = None
        if driver_range:
            try:
                return max(0, int(driver_range[1]))
            except (TypeError, ValueError):
                pass
    return MAX_AFTERBURNER_MEM_OFFSET_MHZ


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
