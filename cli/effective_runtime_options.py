from __future__ import annotations

from auto_uv.auto_uv_user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode import normalize_auto_uv_mode
from nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ
from penguin_burner_paths import resolve_afterburner_root


def _positive_or_none(coerce):
    def fn(value):
        v = coerce(value)
        return v if v > coerce(0) else None
    return fn


_INT_POS = _positive_or_none(int)
_FLOAT_POS = _positive_or_none(float)


def _short_seconds(value):
    n = int(value)
    return max(10, min(60, n)) if n > 0 else None


def _memory_offset(value):
    return max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(value)))


_INT_NONNEG = lambda v: max(0, int(v))
_FLOAT_NONNEG = lambda v: max(0.0, float(v))


# (arg_name, runtime_key, transform). transform is called only when args.<arg_name> is not None.
_NULLABLE_NUMERIC_OPTIONS = [
    ("power_limit_override_w", "power_limit_override_w", _INT_POS),
    ("preserve_base_below_mv", "preserve_base_below_mv", _INT_POS),
    ("auto_uv_max_drop_pct", "auto_uv_max_drop_pct", _FLOAT_POS),
    ("auto_uv_final_seconds", "auto_uv_final_seconds", _INT_POS),
    ("auto_uv_short_seconds", "auto_uv_short_seconds", _short_seconds),
    ("auto_uv_memory_offset_mhz", "auto_uv_memory_offset_mhz", _memory_offset),
    (
        "auto_uv_tail_rise_bins",
        "auto_uv_tail_rise_bins",
        lambda v: max(0, min(int(AUTO_UV_DEFAULTS.max_tail_rise_bins), int(v))),
    ),
    ("auto_oc_target_voltage_mv", "auto_oc_target_voltage_mv", _INT_POS),
    ("auto_oc_target_clock_mhz", "auto_oc_target_clock_mhz", _INT_POS),
    ("auto_uv_efficiency_stop_streak", "auto_uv_efficiency_stop_streak", _INT_NONNEG),
    (
        "auto_uv_min_efficiency_stop_drop_pct",
        "auto_uv_min_efficiency_stop_drop_pct",
        _FLOAT_NONNEG,
    ),
    ("auto_uv_max_clock_drop_pct", "auto_uv_max_clock_drop_pct", _FLOAT_NONNEG),
]

# (arg_name, runtime_key, transform) for non-empty trimmed strings.
_STRING_OPTIONS = [
    ("afterburner_dir", "afterburner_root", lambda v: str(resolve_afterburner_root(v))),
    ("profile_section", "afterburner_profile", lambda v: str(v).strip()),
    (
        "afterburner_device_profile",
        "afterburner_device_profile",
        lambda v: str(v).strip(),
    ),
]


def build_effective_afterburner_runtime_options(args, stored_options: dict) -> dict:
    runtime_options = dict(stored_options)

    for arg_name, key, transform in _STRING_OPTIONS:
        value = getattr(args, arg_name)
        if value.strip():
            runtime_options[key] = transform(value)

    for arg_name, key, transform in _NULLABLE_NUMERIC_OPTIONS:
        value = getattr(args, arg_name)
        if value is not None:
            runtime_options[key] = transform(value)

    if args.auto_uv_mode is not None:
        runtime_options["auto_uv_mode"] = normalize_auto_uv_mode(args.auto_uv_mode)
    if args.auto_uv_require_final_choice:
        runtime_options["auto_uv_require_final_choice"] = True
    if args.dangerously_skip_validation:
        runtime_options["dangerously_skip_validation"] = True

    return runtime_options
