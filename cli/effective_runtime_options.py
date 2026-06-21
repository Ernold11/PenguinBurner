from __future__ import annotations

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode import AUTO_UV_MODE_BALANCED, normalize_auto_uv_mode
from drivers.nvidia.nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ


def _positive_or_none(coerce):
    def fn(value):
        v = coerce(value)
        return v if v > coerce(0) else None
    return fn


_INT_POS = _positive_or_none(int)


def _memory_offset(value):
    return max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(value)))


def _float_nonnegative(value):
    return max(0.0, float(value))


# (arg_name, runtime_key, transform). transform is called only when args.<arg_name> is not None.
_AUTO_UV_NUMERIC_OPTIONS = [
    ("auto_uv_min_voltage_mv", "auto_uv_min_voltage_mv", _INT_POS),
    ("auto_uv_memory_offset_mhz", "auto_uv_memory_offset_mhz", _memory_offset),
    ("auto_uv_power_limit_w", "auto_uv_power_limit_w", _INT_POS),
    (
        "auto_uv_tail_rise_bins",
        "auto_uv_tail_rise_bins",
        lambda v: max(0, min(int(AUTO_UV_DEFAULTS.max_tail_rise_bins), int(v))),
    ),
    ("auto_oc_target_voltage_mv", "auto_oc_target_voltage_mv", _INT_POS),
    ("auto_oc_target_clock_mhz", "auto_oc_target_clock_mhz", _INT_POS),
    ("auto_uv_max_clock_drop_pct", "auto_uv_max_clock_drop_pct", _float_nonnegative),
]


def build_effective_auto_uv_runtime_options(args) -> dict:
    runtime_options: dict = {}
    for arg_name, key, transform in _AUTO_UV_NUMERIC_OPTIONS:
        value = getattr(args, arg_name, None)
        if value is not None:
            runtime_options[key] = transform(value)

    if getattr(args, "auto_uv_mode", None) is not None:
        requested_auto_uv_mode = str(args.auto_uv_mode).strip().lower()
        runtime_options["auto_uv_requested_mode"] = requested_auto_uv_mode
        runtime_options["auto_uv_mode"] = normalize_auto_uv_mode(args.auto_uv_mode)
        if (
            requested_auto_uv_mode == AUTO_UV_MODE_BALANCED
            and getattr(args, "auto_uv_tail_rise_bins") is None
        ):
            runtime_options["auto_uv_tail_rise_bins"] = (
                AUTO_UV_DEFAULTS.balanced_tail_rise_bins
            )
    if getattr(args, "auto_uv_require_final_choice", False):
        runtime_options["auto_uv_require_final_choice"] = True
    return runtime_options
