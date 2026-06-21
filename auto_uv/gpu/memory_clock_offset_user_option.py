"""Clamp the optional memory-clock offset before applying it during Auto-UV.

Driver limits and Afterburner limits are both respected so the applied value is explainable.
"""

from __future__ import annotations

from drivers.nvidia.nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ


def auto_uv_memory_offset_mhz(
    runtime_options: dict,
    *,
    policy_controller=None,
) -> tuple[int | None, int]:
    raw_value = runtime_options.get(
        "auto_uv_memory_offset_mhz",
        runtime_options.get("memory_offset_mhz"),
    )
    if raw_value in (None, ""):
        return None, MAX_AFTERBURNER_MEM_OFFSET_MHZ

    requested = max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(raw_value)))
    effective_max = MAX_AFTERBURNER_MEM_OFFSET_MHZ
    if policy_controller is not None:
        try:
            driver_range = policy_controller.get_memory_clock_offset_range_mhz()
        except Exception:
            driver_range = None
        if driver_range:
            _driver_min, driver_max = driver_range
            effective_max = max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(driver_max)))
            requested = min(int(requested), int(effective_max))
    return int(requested), int(effective_max)
