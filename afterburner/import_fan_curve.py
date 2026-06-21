#!/usr/bin/env python3

from __future__ import annotations

import ctypes
import math
from pathlib import Path
import tomllib

from common.penguin_burner_paths import (
    claim_desktop_user_ownership,
)
from .fan_curve import (
    highest_point_temperature_at_or_below_speed,
    highest_zero_speed_temperature,
    temperature_for_speed,
    validate_afterburner_fan_settings,
)


DEFAULT_EMERGENCY_AUTO_OVERRIDE_TEMP_C = 80.0
DEFAULT_EMERGENCY_AUTO_RESUME_TEMP_C = 75.0
SECTION_ORDERS = {
    "gpu": [
        "index",
        "enable_persistence_mode",
        "afterburner_root",
        "afterburner_device_profile",
        "afterburner_profile",
        "afterburner_power_limit_override_w",
        "afterburner_preserve_base_below_mv",
        "afterburner_dangerously_skip_validation",
        "afterburner_auto_uv_max_drop_pct",
        "auto_uv_final_seconds",
        "auto_uv_efficiency_stop_streak",
    ],
    "fan": [
        "poll_interval_s",
        "hysteresis_c",
        "mode",
        "min_fan_speed_pct",
        "max_fan_speed_pct",
        "max_step_up_pct_per_s",
        "max_step_down_pct_per_s",
        "manual_enable_temp_c",
        "auto_restore_temp_c",
        "emergency_auto_override_temp_c",
        "emergency_auto_resume_temp_c",
        "force_update_every_poll",
        "curve_source",
        "curve_source_root",
        "curve_source_flags_u32",
        "curve_source_period_ms",
        "curve_override_zero_with_hardware_curve",
        "curve_hardware_auto_below_device_min",
        "curve_device_min_fan_speed_pct",
        "curve_manual_takeover_temp_c",
        "curve_auto_restore_temp_c",
        "curve2_points",
        "curve",
    ],
    # PenguinBurner always uses its own managed, headless Q2RTX fork; there is
    # deliberately no configurable Q2RTX directory/binary override here.
    "stability": [],
}


def load_config(config_path: Path):
    if not config_path.exists():
        return {}

    with config_path.open("rb") as config_file:
        return tomllib.load(config_file)


def query_device_fan_limits(gpu_index: int):
    nvml = ctypes.CDLL("libnvidia-ml.so.1")
    c_uint = ctypes.c_uint
    c_void_p = ctypes.c_void_p

    nvml.nvmlInit_v2.restype = ctypes.c_int
    nvml.nvmlShutdown.restype = ctypes.c_int
    nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [c_uint, ctypes.POINTER(c_void_p)]
    nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
    nvml.nvmlDeviceGetMinMaxFanSpeed.argtypes = [
        c_void_p,
        ctypes.POINTER(c_uint),
        ctypes.POINTER(c_uint),
    ]
    nvml.nvmlDeviceGetMinMaxFanSpeed.restype = ctypes.c_int

    device = c_void_p()
    fan_min = c_uint()
    fan_max = c_uint()

    rc = nvml.nvmlInit_v2()
    if rc != 0:
        return None, None

    try:
        rc = nvml.nvmlDeviceGetHandleByIndex_v2(c_uint(gpu_index), ctypes.byref(device))
        if rc != 0:
            return None, None

        rc = nvml.nvmlDeviceGetMinMaxFanSpeed(
            device,
            ctypes.byref(fan_min),
            ctypes.byref(fan_max),
        )
        if rc != 0 or fan_max.value < fan_min.value:
            return None, None

        return float(fan_min.value), float(fan_max.value)
    finally:
        nvml.nvmlShutdown()


def _toml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:.6f}".rstrip("0").rstrip(".")
        raise ValueError("non-finite float in config")
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"unsupported TOML scalar type: {type(value)!r}")


def _toml_value(value, indent=""):
    if isinstance(value, list):
        if value and all(isinstance(item, list) for item in value):
            lines = ["["]
            for item in value:
                row = ", ".join(_toml_scalar(inner) for inner in item)
                lines.append(f"{indent}  [{row}],")
            lines.append(f"{indent}]")
            return "\n".join(lines)
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    return _toml_scalar(value)


def write_config(config_path: Path, config: dict):
    lines = []
    ordered_sections = [name for name in ("gpu", "fan", "stability") if name in config]
    ordered_sections.extend(
        sorted(
            name
            for name in config
            if name not in SECTION_ORDERS and name not in ordered_sections
        )
    )

    for section_name in ordered_sections:
        section = config.get(section_name, {})
        if not section:
            continue
        lines.append(f"[{section_name}]")
        emitted_keys: set[str] = set()
        for key in SECTION_ORDERS.get(section_name, []):
            if key not in section:
                continue
            value = section[key]
            rendered = _toml_value(value)
            lines.append(f"{key} = {rendered}")
            emitted_keys.add(key)
        for key in sorted(key for key in section if key not in emitted_keys):
            value = section[key]
            rendered = _toml_value(value)
            lines.append(f"{key} = {rendered}")
        lines.append("")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(config_path.parent, include_parents=True)
    config_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    claim_desktop_user_ownership(config_path)


def build_imported_fan_section(current_fan: dict, settings: dict, gpu_index: int):
    points = [
        [float(point["temperature_c"]), float(point["speed_pct"])]
        for point in settings["curve"]["points"]
    ]
    curve2_points = [
        [float(point["temperature_c"]), float(point["speed_pct"])]
        for point in settings["curve2"]["points"]
    ]

    device_min_fan_speed_pct, device_max_fan_speed_pct = query_device_fan_limits(
        gpu_index
    )
    if device_min_fan_speed_pct is None:
        device_min_fan_speed_pct = 0.0
    if device_max_fan_speed_pct is None:
        device_max_fan_speed_pct = 100.0

    validation = validate_afterburner_fan_settings(
        settings,
        device_min_fan_speed_pct=device_min_fan_speed_pct,
        device_max_fan_speed_pct=device_max_fan_speed_pct,
    )
    if not validation["valid"]:
        raise SystemExit(
            "Invalid Afterburner fan profile: " + "; ".join(validation["problems"])
        )

    flags = settings["flags"]
    preserve_zero_with_hardware = bool(flags["override_zero_with_hardware_curve"])
    preserve_below_device_min = any(
        speed_pct < device_min_fan_speed_pct for _, speed_pct in points
    )

    if preserve_below_device_min:
        positive_points = [
            point
            for point in settings["curve"]["points"]
            if float(point["speed_pct"]) > 0.0
        ]
        manual_takeover_temp_c = (
            float(positive_points[0]["temperature_c"])
            if positive_points
            else temperature_for_speed(
                settings["curve"]["points"], device_min_fan_speed_pct
            )
        )
        auto_restore_temp_c = (
            highest_zero_speed_temperature(settings["curve"]["points"])
            if preserve_zero_with_hardware
            else highest_point_temperature_at_or_below_speed(
                settings["curve"]["points"],
                0.0,
            )
        )
    elif preserve_zero_with_hardware:
        manual_takeover_temp_c = temperature_for_speed(settings["curve"]["points"], 0.0)
        auto_restore_temp_c = highest_zero_speed_temperature(
            settings["curve"]["points"]
        )
    else:
        manual_takeover_temp_c = float(points[0][0])
        auto_restore_temp_c = float(points[0][0])

    if manual_takeover_temp_c is None:
        manual_takeover_temp_c = float(points[0][0])
    if auto_restore_temp_c is None:
        auto_restore_temp_c = float(points[0][0])

    fan = dict(current_fan)
    fan.pop("hot_auto_handoff_temp_c", None)
    fan.pop("hot_auto_reenable_temp_c", None)
    fan.update(
        {
            "poll_interval_s": settings["period_ms"] / 1000.0,
            "hysteresis_c": 0.0,
            "mode": "linear",
            "min_fan_speed_pct": 0,
            "max_fan_speed_pct": int(device_max_fan_speed_pct),
            "max_step_up_pct_per_s": 0.0,
            "max_step_down_pct_per_s": 0.0,
            "manual_enable_temp_c": float(manual_takeover_temp_c),
            "auto_restore_temp_c": float(auto_restore_temp_c),
            "emergency_auto_override_temp_c": DEFAULT_EMERGENCY_AUTO_OVERRIDE_TEMP_C,
            "emergency_auto_resume_temp_c": DEFAULT_EMERGENCY_AUTO_RESUME_TEMP_C,
            "force_update_every_poll": bool(flags["force_update_each_period"]),
            "curve_source": "afterburner",
            "curve_source_root": str(settings["afterburner_root"]),
            "curve_source_flags_u32": int(settings["flags_u32"]),
            "curve_source_period_ms": int(settings["period_ms"]),
            "curve_override_zero_with_hardware_curve": preserve_zero_with_hardware,
            "curve_hardware_auto_below_device_min": preserve_below_device_min,
            "curve_device_min_fan_speed_pct": float(device_min_fan_speed_pct),
            "curve_manual_takeover_temp_c": float(manual_takeover_temp_c),
            "curve_auto_restore_temp_c": float(auto_restore_temp_c),
            "curve2_points": curve2_points,
            "curve": points,
        }
    )
    return fan
