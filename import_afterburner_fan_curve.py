#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ctypes
import math
from pathlib import Path
import sys
import tomllib

from penguin_burner_paths import (
    default_runtime_config_path,
    managed_afterburner_root,
    resolve_afterburner_root,
    sync_afterburner_export_tree,
)
from afterburner_fan_curve import (
    format_curve_points,
    highest_point_temperature_at_or_below_speed,
    highest_zero_speed_temperature,
    load_afterburner_fan_settings,
    resolve_afterburner_fan_profile,
    temperature_for_speed,
    validate_afterburner_fan_settings,
)


DEFAULT_CONFIG_PATH = default_runtime_config_path()
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
        "afterburner_preserve_vanilla_below_mv",
        "afterburner_dangerously_skip_validation",
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

    for section_name in ("gpu", "fan"):
        section = config.get(section_name, {})
        if not section:
            continue
        lines.append(f"[{section_name}]")
        for key in SECTION_ORDERS[section_name]:
            if key not in section:
                continue
            value = section[key]
            rendered = _toml_value(value)
            lines.append(f"{key} = {rendered}")
        lines.append("")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines).rstrip() + "\n")


def build_imported_fan_section(current_fan: dict, settings: dict, gpu_index: int):
    points = [
        [float(point["temperature_c"]), float(point["speed_pct"])]
        for point in settings["curve"]["points"]
    ]
    curve2_points = [
        [float(point["temperature_c"]), float(point["speed_pct"])]
        for point in settings["curve2"]["points"]
    ]

    device_min_fan_speed_pct, device_max_fan_speed_pct = query_device_fan_limits(gpu_index)
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
    preserve_below_device_min = any(speed_pct < device_min_fan_speed_pct for _, speed_pct in points)

    if preserve_below_device_min:
        positive_points = [
            point
            for point in settings["curve"]["points"]
            if float(point["speed_pct"]) > 0.0
        ]
        manual_takeover_temp_c = (
            float(positive_points[0]["temperature_c"])
            if positive_points
            else temperature_for_speed(settings["curve"]["points"], device_min_fan_speed_pct)
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
        auto_restore_temp_c = highest_zero_speed_temperature(settings["curve"]["points"])
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--afterburner-dir",
        type=Path,
        default=None,
        help="Path to the exported MSI Afterburner directory",
    )
    args = parser.parse_args()

    current = load_config(args.config)
    gpu = dict(current.get("gpu", {}))
    for legacy_key in (
        "enable_dynamic_performance_mode",
        "performance_mode_enter_power_w",
        "performance_mode_exit_power_w",
        "force_locked_graphics_clock",
        "locked_graphics_clock_mhz",
        "locked_graphics_clock_mode",
        "reset_locked_graphics_clock_on_exit",
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
    ):
        gpu.pop(legacy_key, None)
    fan = dict(current.get("fan", {}))
    gpu_index = int(gpu.get("index", 0))

    configured_afterburner_root = str(gpu.get("afterburner_root", "")).strip()
    source_root = None
    if args.afterburner_dir is not None:
        source_root = resolve_afterburner_root(args.afterburner_dir)
    elif configured_afterburner_root:
        source_root = resolve_afterburner_root(configured_afterburner_root)
    elif sys.stdin.isatty():
        while True:
            entered = input("Paste the exported MSI Afterburner directory path: ").strip()
            if not entered:
                print("Afterburner directory is required.")
                continue
            source_root = resolve_afterburner_root(entered)
            try:
                afterburner_root = sync_afterburner_export_tree(
                    source_root,
                    managed_afterburner_root(),
                )
            except FileNotFoundError as exc:
                print(str(exc))
                continue
            break
    else:
        raise SystemExit(
            "No Afterburner directory is configured. Re-run interactively and paste the "
            "exported MSI Afterburner directory path."
        )

    if source_root is not None:
        afterburner_root = sync_afterburner_export_tree(
            source_root,
            managed_afterburner_root(),
        )

    gpu["afterburner_root"] = str(afterburner_root)
    settings = load_afterburner_fan_settings(resolve_afterburner_fan_profile(afterburner_root=afterburner_root))
    settings["afterburner_root"] = afterburner_root
    if not settings["sw_auto_enabled"]:
        raise SystemExit("Afterburner software auto fan control is disabled in the source profile")

    fan = build_imported_fan_section(fan, settings, gpu_index=gpu_index)
    config = {"gpu": gpu, "fan": {}}
    write_config(args.config, config)

    print(f"Imported Afterburner export into {afterburner_root}")
    print(f"Validated Afterburner fan curve and updated {args.config}")
    print(
        "Primary curve: "
        + format_curve_points(settings["curve"]["points"])
    )
    print(
        "Secondary curve: "
        + format_curve_points(settings["curve2"]["points"])
    )
    print(
        f"Flags=0x{settings['flags_u32']:08x} "
        f"force-update={'on' if settings['flags']['force_update_each_period'] else 'off'} "
        f"override-zero={'on' if settings['flags']['override_zero_with_hardware_curve'] else 'off'}"
    )
    print(
        f"Update period={settings['period_ms']}ms "
        f"manual-takeover={fan['curve_manual_takeover_temp_c']:.2f}C "
        f"auto-restore={fan['curve_auto_restore_temp_c']:.2f}C "
        f"device-min={fan['curve_device_min_fan_speed_pct']:.0f}%"
    )
    print(
        f"Silent fan curve guardrail: hardware auto above "
        f"{fan['emergency_auto_override_temp_c']:.0f}C, "
        f"resume manual below {fan['emergency_auto_resume_temp_c']:.0f}C."
    )


if __name__ == "__main__":
    main()
