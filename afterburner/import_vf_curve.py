#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path

from .import_fan_curve import load_config, write_config
from .vfcurve import (
    point_map_by_voltage,
)


def _coerce_optional_int(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(round(float(text)))


def _coerce_optional_positive_int(value):
    coerced = _coerce_optional_int(value)
    if coerced is None or int(coerced) <= 0:
        return None
    return int(coerced)


def _coerce_optional_nonnegative_int(value):
    coerced = _coerce_optional_int(value)
    if coerced is None or int(coerced) < 0:
        return None
    return int(coerced)


def _coerce_optional_positive_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    coerced = float(text)
    if float(coerced) <= 0.0:
        return None
    return float(coerced)


def load_afterburner_runtime_options(config_path):
    current = load_config(Path(config_path))
    gpu = dict(current.get("gpu", {}))
    return {
        "afterburner_root": str(gpu.get("afterburner_root", "")).strip(),
        "afterburner_profile": str(gpu.get("afterburner_profile", "")).strip(),
        "afterburner_device_profile": str(
            gpu.get("afterburner_device_profile", "")
        ).strip(),
        "auto_uv_max_drop_pct": _coerce_optional_positive_float(
            gpu.get("afterburner_auto_uv_max_drop_pct")
        ),
        "auto_uv_final_seconds": _coerce_optional_positive_int(
            gpu.get("auto_uv_final_seconds")
        ),
        "auto_uv_efficiency_stop_streak": _coerce_optional_nonnegative_int(
            gpu.get("auto_uv_efficiency_stop_streak")
        ),
    }


def build_plan(reader, afterburner_points):
    ab_by_voltage = point_map_by_voltage(afterburner_points)
    plan = []
    missing_in_afterburner = []
    for point in reader.editable_core_points():
        voltage_mv = point["voltage_uv"] // 1000
        base_mhz = point["base_freq_khz"] // 1000
        current_offset_mhz = point["current_offset_khz"] // 1000
        ab_point = ab_by_voltage.get(voltage_mv)
        if ab_point is None:
            missing_in_afterburner.append(voltage_mv)
            continue

        target_mhz = int(round(ab_point["frequency_mhz"]))
        new_offset_mhz = target_mhz - base_mhz
        plan.append(
            {
                "index": point["index"],
                "voltage_mv": int(voltage_mv),
                "base_mhz": int(base_mhz),
                "target_mhz": int(target_mhz),
                "current_offset_mhz": int(current_offset_mhz),
                "new_offset_mhz": int(new_offset_mhz),
                "preserve_base": False,
            }
        )

    return plan, sorted(set(missing_in_afterburner))


LEGACY_GPU_CONFIG_KEYS = (
    "power_limit_w",
    "gpc_clk_vf_offset",
    "mem_clk_vf_offset",
    "vf_curve_source",
    "vf_curve_source_path",
    "vf_curve_source_section",
    "afterburner_power_limit_cap_w",
    "afterburner_power_limit_pct",
    "afterburner_core_clk_boost_khz",
    "afterburner_mem_clk_boost_khz",
    "afterburner_thermal_limit",
    "afterburner_dynamic_lock_enabled",
    "afterburner_dynamic_lock_enter_power_w",
    "afterburner_dynamic_lock_exit_power_w",
    "afterburner_flatten_at_voltage_mv",
    "afterburner_flatten_to_mhz",
    "vf_curve_curve_sha256",
    "vf_curve_linux_profile_path",
    "vf_curve_translation_mode",
)


def _persist_afterburner_runtime_state(
    config_path,
    gpu_index,
    *,
    afterburner_root=None,
    device_profile_relative_path=None,
    section=None,
    runtime_options=None,
):
    config = load_config(Path(config_path))
    gpu = dict(config.get("gpu", {}))
    fan = dict(config.get("fan", {}))
    stability = dict(config.get("stability", {}))
    # PenguinBurner always uses its own managed, headless Q2RTX fork. A custom
    # Q2RTX directory/binary is never honoured, so drop any stale override keys
    # rather than carrying them forward into the generated config.
    stability.pop("q2rtx_dir", None)
    stability.pop("q2rtx_binary", None)
    gpu["index"] = int(gpu.get("index", gpu_index))
    if afterburner_root is not None:
        gpu["afterburner_root"] = str(Path(afterburner_root))
    gpu.pop("afterburner_profile", None)
    gpu.pop("afterburner_device_profile", None)

    for key in LEGACY_GPU_CONFIG_KEYS:
        gpu.pop(key, None)
    gpu.pop("afterburner_power_limit_override_w", None)
    gpu.pop("afterburner_preserve_base_below_mv", None)
    gpu.pop("afterburner_preserve_vanilla_below_mv", None)
    gpu.pop("afterburner_dangerously_skip_validation", None)

    if runtime_options is not None:
        auto_uv_max_drop_pct = _coerce_optional_positive_float(
            runtime_options.get("auto_uv_max_drop_pct")
        )
        if auto_uv_max_drop_pct is not None:
            gpu["afterburner_auto_uv_max_drop_pct"] = float(auto_uv_max_drop_pct)
        else:
            gpu.pop("afterburner_auto_uv_max_drop_pct", None)
        auto_uv_final_seconds = _coerce_optional_positive_int(
            runtime_options.get("auto_uv_final_seconds")
        )
        if auto_uv_final_seconds is not None:
            gpu["auto_uv_final_seconds"] = int(auto_uv_final_seconds)
        else:
            gpu.pop("auto_uv_final_seconds", None)
        auto_uv_efficiency_stop_streak = _coerce_optional_nonnegative_int(
            runtime_options.get("auto_uv_efficiency_stop_streak")
        )
        if auto_uv_efficiency_stop_streak is not None:
            gpu["auto_uv_efficiency_stop_streak"] = int(auto_uv_efficiency_stop_streak)
        else:
            gpu.pop("auto_uv_efficiency_stop_streak", None)
        gpu.pop("auto_uv_max_oc_budget_recoveries", None)
        gpu.pop("auto_uv_oc_budget_ratio", None)
        gpu.pop("auto_uv_overclock_budget_ratio", None)
        gpu.pop("auto_uv_clock_bump_budget_ratio", None)

    write_config(
        Path(config_path),
        {
            "gpu": gpu,
            "fan": fan,
            "stability": stability,
        },
    )


def persist_afterburner_import(
    config_path,
    gpu_index,
    afterburner_root,
    device_profile_relative_path,
    section,
    *,
    runtime_options=None,
):
    _persist_afterburner_runtime_state(
        config_path,
        gpu_index,
        afterburner_root=afterburner_root,
        device_profile_relative_path=device_profile_relative_path,
        section=section,
        runtime_options=runtime_options,
    )
