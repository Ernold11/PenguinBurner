from __future__ import annotations


DXVK_NVAPI_VKREFLEX_SOURCE = "dxvk-nvapi-vkreflex"


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _positive_us(sample: dict, key: str) -> bool:
    return _int_value(sample.get(key)) > 0


def _elapsed_us_from_sample(sample: dict, start_key: str, end_key: str) -> int:
    start_us = _int_value(sample.get(start_key))
    end_us = _int_value(sample.get(end_key))
    if not start_us or not end_us or end_us <= start_us:
        return 0
    return end_us - start_us


def _looks_like_driver_timing_report(sample: dict) -> bool:
    if _positive_us(sample, "timing_count"):
        return True
    return any(
        _positive_us(sample, key)
        for key in (
            "driver_start_us",
            "driver_end_us",
            "gpu_render_start_us",
            "gpu_render_end_us",
        )
    )


def normalize_timing_sample(sample: dict) -> dict:
    if sample.get("type") != "timing":
        return sample

    normalized = dict(sample)
    if (
        not str(normalized.get("measurement") or "")
        and normalized.get("source") == DXVK_NVAPI_VKREFLEX_SOURCE
        and _looks_like_driver_timing_report(normalized)
    ):
        normalized["measurement"] = "driver-report"

    if not _positive_us(normalized, "gpu_render_us"):
        gpu_render_us = _elapsed_us_from_sample(
            normalized, "gpu_render_start_us", "gpu_render_end_us"
        )
        if gpu_render_us:
            normalized["gpu_render_us"] = gpu_render_us

    if not _positive_us(normalized, "render_present_us"):
        render_present_us = _elapsed_us_from_sample(
            normalized, "present_start_us", "gpu_render_end_us"
        )
        if render_present_us:
            normalized["render_present_us"] = render_present_us

    return normalized
