#!/usr/bin/env python3

import argparse
import atexit
import ctypes
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import tomllib

from afterburner_fan_curve import (
    load_afterburner_fan_settings,
    resolve_afterburner_fan_profile,
)
from afterburner_vfcurve import (
    derive_afterburner_dynamic_lock,
    describe_afterburner_dynamic_lock,
    describe_afterburner_flatten_validation,
    describe_afterburner_profile_settings,
    load_afterburner_profile_settings,
    resolve_afterburner_vf_source,
)
from dry_run_preview import run_afterburner_dry_run
from hidden_nvapi_vf import create_hidden_vf_curve_reader
from hidden_nvml_voltage import create_hidden_voltage_reader
from import_afterburner_fan_curve import build_imported_fan_section
from import_afterburner_vf_curve import (
    apply_plan,
    apply_afterburner_curve_to_reader,
    build_plan,
    ensure_afterburner_root_configured,
    load_afterburner_runtime_options,
    persist_afterburner_import,
)
from nvml_gpu_policy import (
    NvmlGpuPolicyController,
    apply_translated_gpu_policy,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from penguin_burner_paths import default_runtime_config_path, resolve_afterburner_root
from runtime_debug import (
    close_debug_log,
    debug_log,
    debug_effective_runtime_options,
    debug_exception,
    enable_debug_logging,
    log,
)
from runtime_service import (
    DEFAULT_JOURNAL_HOURS,
    daemonize_with_systemd,
    install_systemd_service,
    launcher_script_path,
    parse_runtime_flags,
    running_under_systemd_service,
    systemd_is_available,
    uninstall_systemd_service,
)


NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2
NVIDIA_SMI = shutil.which("nvidia-smi") or "nvidia-smi"


class NvmlError(RuntimeError):
    pass


atexit.register(close_debug_log)


def prompt_yes_no(prompt, *, default):
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        entered = input(f"{prompt} {suffix}: ").strip().lower()
        debug_log(f"prompt={prompt} answer={entered or '<enter>'}")
        if not entered:
            return bool(default)
        if entered in ("y", "yes"):
            return True
        if entered in ("n", "no"):
            return False
        print("Please answer y or n.", flush=True)


def get_config_path():
    return default_runtime_config_path()


def default_config():
    return {
        "gpu": {
            "index": 0,
            "enable_persistence_mode": True,
        },
        "fan": {
            "poll_interval_s": 2,
            "hysteresis_c": 2.0,
            "mode": "linear",
            "min_fan_speed_pct": 20,
            "max_fan_speed_pct": 100,
            "max_step_up_pct_per_s": 25.0,
            "max_step_down_pct_per_s": 15.0,
            "manual_enable_temp_c": 55.0,
            "auto_restore_temp_c": 50.0,
            "emergency_auto_override_temp_c": 80.0,
            "emergency_auto_resume_temp_c": 75.0,
            "force_update_every_poll": False,
            "curve": [
                [55, 30],
                [65, 35],
                [70, 40],
                [80, 45],
            ],
        },
    }


def load_config(config_path=None):
    if config_path is None:
        config_path = get_config_path()
    else:
        config_path = Path(config_path).expanduser()
    config = default_config()

    if not config_path.exists():
        return config, config_path

    with config_path.open("rb") as config_file:
        loaded = tomllib.load(config_file)

    for section in ("gpu", "fan"):
        values = loaded.get(section)
        if isinstance(values, dict):
            config[section].update(values)

    return config, config_path


def parse_main_args(argv):
    parser = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description="PenguinBurner runtime and Afterburner inspection utility.",
        epilog=(
            "System-level flags handled before runtime parsing:\n"
            "  --foreground\n"
            "  --daemonize\n"
            "  --install-systemd-service\n"
            "  --uninstall-systemd-service\n"
            f"  --journal-hours N (default {DEFAULT_JOURNAL_HOURS})"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=str(get_config_path()),
        help="Runtime config path to read defaults from",
    )
    parser.add_argument(
        "--gpu-index",
        type=int,
        default=None,
        help="Override the configured GPU index",
    )
    parser.add_argument(
        "--afterburner-dir",
        default="",
        help="Path to the MSI Afterburner root directory",
    )
    parser.add_argument(
        "--profile-section",
        "--section",
        dest="profile_section",
        default="",
        help="Optional saved Afterburner profile section such as profile2",
    )
    parser.add_argument(
        "--afterburner-device-profile",
        default="",
        help="Optional device profile file under Profiles/ to inspect or use",
    )
    parser.add_argument(
        "--power-limit-override-w",
        type=int,
        default=None,
        help="Optional manual power-limit cap in watts for translation preview",
    )
    parser.add_argument(
        "--preserve-vf-below-mv",
        "--preserve-vanilla-vf-below-mv",
        dest="preserve_vanilla_below_mv",
        type=int,
        default=None,
        help=(
            "Keep the stock/base Linux VF curve at and below this inclusive "
            "voltage; useful if repeated Afterburner curve edits disturbed "
            "idle or low-voltage scaling"
        ),
    )
    parser.add_argument(
        "--dangerously-skip-validation",
        action="store_true",
        help=(
            "Bypass the default flat-tail and undervolt checks when selecting "
            "the saved Afterburner profile; advanced and not recommended"
        ),
    )
    parser.add_argument(
        "--debug-log",
        action="store_true",
        help=(
            "Write a verbose dry-run and first-import diagnostic log next to "
            "the selected config file under debug-logs/; with the default "
            "config this is ~/.config/PenguinBurner/debug-logs"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Inspect Afterburner fan/VF data and draw dry-run previews without "
            "touching GPU state; recommended first step and does not require sudo"
        ),
    )
    return parser.parse_args(argv)


def check(rc, name):
    if rc != NVML_SUCCESS:
        raise NvmlError(f"{name} failed with NVML error {rc}")


def clamp(value, lower, upper):
    return max(lower, min(value, upper))


def validate_curve(curve):
    if len(curve) < 2:
        raise NvmlError("curve must contain at least two points")

    last_temp = None
    last_speed = None
    for temp_c, speed_pct in curve:
        if last_temp is not None and temp_c <= last_temp:
            raise NvmlError("curve temperatures must be strictly increasing")
        if not 0 <= speed_pct <= 100:
            raise NvmlError("curve fan speeds must be in the range 0..100")
        if last_speed is not None and speed_pct < last_speed:
            raise NvmlError("curve fan speeds must not decrease as temperature rises")
        last_temp = temp_c
        last_speed = speed_pct


def format_curve_points(curve):
    return ", ".join(f"{temp_c:.0f}C->{speed_pct:.0f}%" for temp_c, speed_pct in curve)


def format_curve_temp(temp_c):
    if abs(temp_c - round(temp_c)) < 0.01:
        return f"{int(round(temp_c))}C"
    return f"{temp_c:.2f}C"


def select_expected_vf_samples(plan, *, max_samples=8):
    candidates = [
        item
        for item in plan
        if int(item["new_offset_mhz"]) != 0
    ]
    candidates.sort(key=lambda item: abs(int(item["new_offset_mhz"])), reverse=True)
    return candidates[:max_samples]


def detect_vf_curve_reset(vf_curve_reader, expected_samples, *, tolerance_mhz=1):
    if vf_curve_reader is None or not expected_samples:
        return []

    current_points = {
        int(point["index"]): int(point["current_offset_khz"] // 1000)
        for point in vf_curve_reader.editable_core_points()
    }
    mismatches = []
    for sample in expected_samples:
        index = int(sample["index"])
        expected_offset_mhz = int(sample["new_offset_mhz"])
        current_offset_mhz = int(current_points.get(index, 0))
        if abs(current_offset_mhz - expected_offset_mhz) > int(tolerance_mhz):
            mismatches.append(
                {
                    "index": index,
                    "expected_offset_mhz": expected_offset_mhz,
                    "current_offset_mhz": current_offset_mhz,
                    "voltage_mv": int(sample["voltage_mv"]),
                }
            )
    return mismatches


def build_effective_manual_curve(
    curve,
    manual_enable_temp_c,
    effective_min_fan_speed_pct,
    effective_max_fan_speed_pct,
    mode,
):
    start_speed_pct = clamp(
        speed_for_temp(manual_enable_temp_c, curve, mode=mode),
        effective_min_fan_speed_pct,
        effective_max_fan_speed_pct,
    )
    effective_curve = [(float(manual_enable_temp_c), float(start_speed_pct))]

    for temp_c, speed_pct in curve:
        if temp_c <= manual_enable_temp_c:
            continue
        clamped_speed_pct = clamp(
            float(speed_pct),
            effective_min_fan_speed_pct,
            effective_max_fan_speed_pct,
        )
        last_temp_c, last_speed_pct = effective_curve[-1]
        if abs(clamped_speed_pct - last_speed_pct) < 0.001:
            continue
        effective_curve.append((float(temp_c), float(clamped_speed_pct)))

    return effective_curve


def describe_fan_curve_state(
    current_temp_c,
    effective_curve,
    manual_mode_active,
    emergency_auto_mode_active,
    emergency_auto_resume_temp_c,
):
    if emergency_auto_mode_active:
        return (
            "fan_curve_state=emergency-auto "
            f"next_fan_step={format_curve_temp(emergency_auto_resume_temp_c)}->resume-custom"
        )

    if not manual_mode_active:
        takeover_temp_c, takeover_speed_pct = effective_curve[0]
        return (
            "fan_curve_state=hardware-auto "
            f"next_fan_step={format_curve_temp(takeover_temp_c)}->{takeover_speed_pct:.0f}%"
        )

    if len(effective_curve) == 1:
        temp_c, speed_pct = effective_curve[0]
        return (
            f"fan_curve_state={format_curve_temp(temp_c)}+:{speed_pct:.0f}% "
            "next_fan_step=none"
        )

    for index in range(len(effective_curve) - 1):
        left_temp_c, left_speed_pct = effective_curve[index]
        right_temp_c, right_speed_pct = effective_curve[index + 1]
        if current_temp_c < right_temp_c:
            return (
                f"fan_curve_state={format_curve_temp(left_temp_c)}-{format_curve_temp(right_temp_c)}:"
                f"{left_speed_pct:.0f}-{right_speed_pct:.0f}% "
                f"next_fan_step={format_curve_temp(right_temp_c)}->{right_speed_pct:.0f}%"
            )

    last_temp_c, last_speed_pct = effective_curve[-1]
    return (
        f"fan_curve_state={format_curve_temp(last_temp_c)}+:{last_speed_pct:.0f}% "
        "next_fan_step=none"
    )


def speed_for_temp(temp_c, curve, mode):
    if temp_c <= curve[0][0]:
        return curve[0][1]

    for index in range(1, len(curve)):
        left_temp, left_speed = curve[index - 1]
        right_temp, right_speed = curve[index]

        if temp_c <= right_temp:
            if mode == "step":
                return left_speed

            span = right_temp - left_temp
            t = (temp_c - left_temp) / span
            return left_speed + (right_speed - left_speed) * t

    return curve[-1][1]


def apply_hysteresis(current_temp_c, raw_target_speed, last_temp_c, last_speed, hysteresis_c):
    if last_temp_c is None or last_speed is None or hysteresis_c <= 0.0:
        return raw_target_speed

    if raw_target_speed >= last_speed:
        return raw_target_speed

    if current_temp_c > last_temp_c:
        return raw_target_speed

    if (last_temp_c - current_temp_c) < hysteresis_c:
        return float(last_speed)

    return raw_target_speed


def limit_speed_change(target_speed, last_speed, elapsed_s, max_step_up_pct_per_s, max_step_down_pct_per_s):
    if last_speed is None or elapsed_s <= 0.0:
        return target_speed

    limited_speed = float(target_speed)

    if limited_speed > last_speed and max_step_up_pct_per_s > 0.0:
        max_up = max_step_up_pct_per_s * elapsed_s
        limited_speed = min(limited_speed, last_speed + max_up)

    if limited_speed < last_speed and max_step_down_pct_per_s > 0.0:
        max_down = max_step_down_pct_per_s * elapsed_s
        limited_speed = max(limited_speed, last_speed - max_down)

    return limited_speed


def get_reported_fan_speeds(nvml, device, fan_count):
    fan_speeds = []

    for fan_idx in range(fan_count):
        speed = ctypes.c_uint()
        rc = nvml.nvmlDeviceGetFanSpeed_v2(device, ctypes.c_uint(fan_idx), ctypes.byref(speed))
        if rc != NVML_SUCCESS:
            fan_speeds = []
            break
        fan_speeds.append(int(speed.value))

    if fan_speeds:
        return fan_speeds

    if fan_count == 1:
        speed = ctypes.c_uint()
        rc = nvml.nvmlDeviceGetFanSpeed(device, ctypes.byref(speed))
        if rc == NVML_SUCCESS:
            return [int(speed.value)]

    return None


def get_power_draw_w(nvml, device):
    power_mw = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetPowerUsage(device, ctypes.byref(power_mw))
    if rc != NVML_SUCCESS:
        return None
    return power_mw.value / 1000.0


def get_graphics_clock_mhz(nvml, device):
    clock_mhz = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetClockInfo(device, ctypes.c_uint(NVML_CLOCK_GRAPHICS), ctypes.byref(clock_mhz))
    if rc != NVML_SUCCESS:
        return None
    return int(clock_mhz.value)


def get_memory_clock_mhz(nvml, device):
    clock_mhz = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetClockInfo(device, ctypes.c_uint(NVML_CLOCK_MEM), ctypes.byref(clock_mhz))
    if rc != NVML_SUCCESS:
        return None
    return int(clock_mhz.value)


def format_vf_curve_comparison(vf_curve_reader, graphics_clock_mhz, voltage_uv):
    point = vf_curve_reader.find_nearest_point(graphics_clock_mhz, voltage_uv)
    if point is None:
        return ""

    point_freq_mhz = int(point["freq_khz"] // 1000)
    point_voltage_mv = int(point["voltage_uv"] // 1000)
    point_base_freq_mhz = int(point["base_freq_khz"] // 1000)
    point_offset_mhz = int(point["current_offset_khz"] // 1000)

    vanilla_point = min(
        vf_curve_reader.editable_core_points(),
        key=lambda candidate: abs(int(candidate["base_freq_khz"]) - int(graphics_clock_mhz) * 1000),
    )
    vanilla_clock_mhz = int(vanilla_point["base_freq_khz"] // 1000)
    vanilla_voltage_mv = int(vanilla_point["voltage_uv"] // 1000)
    uv_delta_mv = int(point_voltage_mv - vanilla_voltage_mv)

    return (
        f"vf_point={point_freq_mhz}MHz@{point_voltage_mv}mV "
        f"vf_offset={point_offset_mhz:+d}MHz "
        f"vf_vanilla={vanilla_clock_mhz}MHz@{vanilla_voltage_mv}mV "
        f"uv={uv_delta_mv:+d}mV "
    )


def format_clock_offsets(gpu_policy_controller):
    if gpu_policy_controller is None:
        return ""

    try:
        offsets = gpu_policy_controller.get_clock_offsets()
    except Exception:
        return ""

    mem_clk_vf_offset_mhz = offsets.get("mem_clk_vf_offset_mhz")
    if mem_clk_vf_offset_mhz is None:
        return ""
    return f"mem_vf_offset={int(mem_clk_vf_offset_mhz):+d}MHz "


def format_clock_ceiling_state(clock_ceiling_controller):
    if clock_ceiling_controller is None:
        return ""
    return clock_ceiling_controller.telemetry_text()


def format_telemetry(
    nvml,
    device,
    fan_count,
    current_temp_c,
    voltage_reader=None,
    vf_curve_reader=None,
    gpu_policy_controller=None,
    power_draw_w=None,
    clock_ceiling_controller=None,
):
    reported_fan_speeds = get_reported_fan_speeds(nvml, device, fan_count)
    if reported_fan_speeds is None:
        fan_text = "n/a"
    else:
        fan_text = "/".join(f"{speed}%" for speed in reported_fan_speeds)

    if power_draw_w is None:
        power_draw_w = get_power_draw_w(nvml, device)
    power_text = "n/a" if power_draw_w is None else f"{power_draw_w:.2f}W"

    graphics_clock_mhz = get_graphics_clock_mhz(nvml, device)
    if graphics_clock_mhz is None:
        clock_text = "n/a"
    else:
        clock_text = f"{graphics_clock_mhz}MHz"

    memory_clock_mhz = get_memory_clock_mhz(nvml, device)
    if memory_clock_mhz is None:
        mem_clock_text = "n/a"
    else:
        mem_clock_text = f"{memory_clock_mhz}MHz"

    voltage_uv = None
    if voltage_reader is not None:
        voltage_uv = voltage_reader.read_microvolts(device)
    if voltage_uv is None:
        voltage_text = "n/a"
    else:
        voltage_text = f"{voltage_uv / 1000.0:.0f}mV"

    clock_offset_text = format_clock_offsets(gpu_policy_controller)
    clock_ceiling_text = format_clock_ceiling_state(clock_ceiling_controller)
    vf_point_text = ""
    if vf_curve_reader is not None and graphics_clock_mhz is not None and voltage_uv is not None:
        try:
            vf_curve_reader.refresh_points()
        except Exception:
            pass
        vf_point_text = format_vf_curve_comparison(
            vf_curve_reader,
            graphics_clock_mhz,
            voltage_uv,
        )

    return (
        f"temp={current_temp_c:.1f}C "
        f"fan={fan_text} "
        f"power={power_text} "
        f"gpu_clock={clock_text} "
        f"mem_clock={mem_clock_text} "
        f"voltage={voltage_text} "
        f"{clock_ceiling_text}"
        f"{clock_offset_text}"
        f"{vf_point_text}"
    ).rstrip()


def run_nvidia_smi(args):
    try:
        result = subprocess.run(
            [NVIDIA_SMI, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NvmlError(f"{NVIDIA_SMI} not found") from exc

    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0:
        raise NvmlError(f"nvidia-smi {' '.join(args)} failed: {output or result.returncode}")

    return output


class FlattenedClockCeilingController:
    def __init__(self, flatten_target, policy_controller):
        if not flatten_target:
            raise ValueError("flatten_target is required")
        if policy_controller is None:
            raise ValueError("policy_controller is required")

        self._flatten_target = dict(flatten_target)
        self._policy_controller = policy_controller
        self._active = False
        self._range_lock = None

    @property
    def target_clock_mhz(self):
        return int(self._flatten_target["lock_clock_mhz"])

    @property
    def target_voltage_mv(self):
        voltage_mv = self._flatten_target.get("lock_voltage_mv")
        return int(voltage_mv) if voltage_mv is not None else None

    @property
    def requested_max_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["requested_max_clock_mhz"])

    @property
    def applied_max_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["applied_max_clock_mhz"])

    @property
    def applied_min_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["applied_min_clock_mhz"])

    def telemetry_text(self):
        if not self._active:
            return ""

        ceiling_text = (
            f"{self.requested_max_clock_mhz}MHz"
            if self.requested_max_clock_mhz == self.applied_max_clock_mhz
            else f"{self.requested_max_clock_mhz}->{self.applied_max_clock_mhz}MHz"
        )
        voltage_mv = self.target_voltage_mv
        if voltage_mv is not None:
            ceiling_text += f"@{voltage_mv}mV"
        return f"clk_ceiling={ceiling_text} "

    def describe(self):
        snap_text = ""
        if self._range_lock is not None and self.requested_max_clock_mhz != self.applied_max_clock_mhz:
            snap_text = (
                f", supported-max={self.applied_max_clock_mhz}MHz"
                f" ({self._range_lock['max_mode']})"
            )
        min_text = (
            f", min-step={self.applied_min_clock_mhz}MHz"
            if self._range_lock is not None
            else ""
        )
        return (
            f"{describe_afterburner_dynamic_lock(self._flatten_target)}, "
            f"ceiling={self.requested_max_clock_mhz}MHz"
            f"{snap_text}{min_text}"
        )

    def apply(self):
        supported_steps = self._policy_controller.get_supported_graphics_clock_steps_mhz()
        requested_min_clock_mhz = supported_steps[0] if supported_steps else self.target_clock_mhz
        self._range_lock = self._policy_controller.apply_locked_graphics_clock_range_mhz(
            requested_min_clock_mhz,
            self.target_clock_mhz,
            prefer_max_not_above=True,
            snap_to_supported=True,
        )
        self._active = True
        return dict(self._range_lock)

    def close(self):
        if self._active:
            self._policy_controller.reset_locked_graphics_clocks()
            self._active = False


def apply_gpu_base_policy(gpu_index, enable_persistence_mode, power_limit_w):
    if enable_persistence_mode:
        output = run_nvidia_smi(["-pm", "1"])
        if output:
            log(output)

    if power_limit_w is not None:
        output = run_nvidia_smi(["-i", str(gpu_index), "-pl", str(power_limit_w)])
        if output:
            log(output)


def load_runtime_afterburner_fan_config(current_fan_config, *, afterburner_root, gpu_index):
    try:
        settings = load_afterburner_fan_settings(
            resolve_afterburner_fan_profile(afterburner_root=afterburner_root)
        )
    except Exception as exc:
        raise NvmlError(
            f"failed to read the imported Afterburner fan profile under {afterburner_root}: {exc}"
        ) from exc

    settings["afterburner_root"] = Path(afterburner_root).expanduser()
    if not settings["sw_auto_enabled"]:
        raise NvmlError("Afterburner software auto fan control is disabled in the imported profile")

    try:
        return build_imported_fan_section(
            current_fan_config,
            settings,
            gpu_index=gpu_index,
        )
    except SystemExit as exc:
        raise NvmlError(str(exc)) from None


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


def maybe_handle_first_time_afterburner_setup(
    *,
    argv,
    journal_hours,
    config_path,
    fan_config,
    gpu_index,
    afterburner_runtime_options,
):
    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or running_under_systemd_service()
    ):
        return False

    print(flush=True)
    log("First-time Afterburner import detected. Running a dry run before touching GPU state.")
    print(flush=True)
    script_path = launcher_script_path(__file__)
    try:
        run_afterburner_dry_run(
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
    except Exception as exc:
        log(f"Dry run failed: {exc}")
        log("No GPU changes were applied.")
        log(
            "If the wrong saved Afterburner preset was auto-selected, re-run the dry run "
            "with an explicit section, for example:"
        )
        log(f"`{script_path} --dry-run --section Profile3`")
        return True
    try:
        source = resolve_afterburner_vf_source(
            afterburner_root=afterburner_runtime_options.get("afterburner_root") or None,
            section=afterburner_runtime_options.get("afterburner_profile") or None,
            device_profile_hint=afterburner_runtime_options.get("afterburner_device_profile") or None,
            dangerously_skip_validation=bool(
                afterburner_runtime_options.get("dangerously_skip_validation")
            ),
        )
    except Exception as exc:
        log(f"Warning: dry run succeeded but failed to persist the selected source: {exc}")
    else:
        afterburner_runtime_options["afterburner_root"] = str(source["afterburner_root"])
        afterburner_runtime_options["afterburner_profile"] = str(source["section"])
        afterburner_runtime_options["afterburner_device_profile"] = str(
            source["device_profile_relative_path"]
        )
        persist_afterburner_import(
            config_path,
            gpu_index,
            source["afterburner_root"],
            source["device_profile_relative_path"],
            source["section"],
            runtime_options=afterburner_runtime_options,
        )
    print(flush=True)
    log("Dry run complete.")
    log(
        "Recommended next step: run PenguinBurner in foreground first so you can "
        "watch stdout logs and stop it with Ctrl-C."
    )
    if prompt_yes_no("Start PenguinBurner in foreground now for testing?", default=True):
        return False

    if systemd_is_available():
        if prompt_yes_no("Daemonize PenguinBurner under systemd now instead?", default=False):
            if os.geteuid() != 0:
                log(
                    "Daemon mode needs sudo. Re-run with "
                    f"`sudo {script_path} --daemonize` after you are happy with the dry run."
                )
                return True
            daemonize_with_systemd(__file__, argv, journal_hours=journal_hours, log=log)
            return True
    else:
        log("systemd background mode is unavailable on this system.")

    log("No GPU changes were applied.")
    log(f"When you are ready, run `{script_path}` for a foreground test.")
    if systemd_is_available():
        log(f"After that, you can daemonize it with `sudo {script_path} --daemonize`.")
    return True


def main(argv=None, *, journal_hours=DEFAULT_JOURNAL_HOURS):
    if argv is None:
        argv = sys.argv[1:]

    args = parse_main_args(argv)
    if args.debug_log:
        enable_debug_logging(args.config, argv=argv)
    config, config_path = load_config(args.config)
    gpu_config = config["gpu"]
    fan_config = config["fan"]
    if args.gpu_index is not None:
        gpu_config["index"] = int(args.gpu_index)
    gpu_index = int(gpu_config["index"])
    stored_afterburner_runtime_options = load_afterburner_runtime_options(config_path)
    had_persisted_afterburner_root = bool(
        str(stored_afterburner_runtime_options.get("afterburner_root", "")).strip()
    )
    afterburner_runtime_options = dict(stored_afterburner_runtime_options)
    if args.afterburner_dir.strip():
        afterburner_runtime_options["afterburner_root"] = str(
            resolve_afterburner_root(args.afterburner_dir)
        )
    if args.profile_section.strip():
        afterburner_runtime_options["afterburner_profile"] = str(args.profile_section).strip()
    if args.afterburner_device_profile.strip():
        afterburner_runtime_options["afterburner_device_profile"] = str(
            args.afterburner_device_profile
        ).strip()
    if args.power_limit_override_w is not None:
        afterburner_runtime_options["power_limit_override_w"] = (
            int(args.power_limit_override_w) if int(args.power_limit_override_w) > 0 else None
        )
    if args.preserve_vanilla_below_mv is not None:
        afterburner_runtime_options["preserve_vanilla_below_mv"] = (
            int(args.preserve_vanilla_below_mv)
            if int(args.preserve_vanilla_below_mv) > 0
            else None
        )
    if args.dangerously_skip_validation:
        afterburner_runtime_options["dangerously_skip_validation"] = True
    debug_effective_runtime_options(
        config_path=config_path,
        gpu_index=gpu_index,
        afterburner_runtime_options=afterburner_runtime_options,
    )

    if args.dry_run:
        run_afterburner_dry_run(
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return

    afterburner_root = str(afterburner_runtime_options.get("afterburner_root", "")).strip()
    afterburner_profile = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
    afterburner_device_profile = str(
        afterburner_runtime_options.get("afterburner_device_profile", "")
    ).strip()
    if not had_persisted_afterburner_root:
        afterburner_runtime_options = ensure_afterburner_root_configured(
            config_path,
            afterburner_runtime_options,
            gpu_index=gpu_index,
            interactive=sys.stdin.isatty(),
        )
        afterburner_root = str(afterburner_runtime_options.get("afterburner_root", "")).strip()
        afterburner_profile = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
        afterburner_device_profile = str(
            afterburner_runtime_options.get("afterburner_device_profile", "")
        ).strip()
        if afterburner_root and maybe_handle_first_time_afterburner_setup(
            argv=argv,
            journal_hours=journal_hours,
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        ):
            return
    elif not afterburner_root:
        afterburner_runtime_options = ensure_afterburner_root_configured(
            config_path,
            afterburner_runtime_options,
            gpu_index=gpu_index,
            interactive=sys.stdin.isatty(),
        )
        afterburner_root = str(afterburner_runtime_options.get("afterburner_root", "")).strip()
        afterburner_profile = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
        afterburner_device_profile = str(
            afterburner_runtime_options.get("afterburner_device_profile", "")
        ).strip()
    if afterburner_root:
        fan_config = load_runtime_afterburner_fan_config(
            fan_config,
            afterburner_root=afterburner_root,
            gpu_index=gpu_index,
        )

    nvml = ctypes.CDLL("libnvidia-ml.so.1")

    c_uint = ctypes.c_uint
    c_void_p = ctypes.c_void_p

    nvml.nvmlInit_v2.restype = ctypes.c_int
    nvml.nvmlShutdown.restype = ctypes.c_int
    nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [c_uint, ctypes.POINTER(c_void_p)]
    nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
    nvml.nvmlDeviceGetTemperature.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetTemperature.restype = ctypes.c_int
    nvml.nvmlDeviceGetNumFans.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetNumFans.restype = ctypes.c_int
    nvml.nvmlDeviceGetFanSpeed.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetFanSpeed.restype = ctypes.c_int
    nvml.nvmlDeviceGetFanSpeed_v2.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetFanSpeed_v2.restype = ctypes.c_int
    nvml.nvmlDeviceGetPowerUsage.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetPowerUsage.restype = ctypes.c_int
    nvml.nvmlDeviceGetClockInfo.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetClockInfo.restype = ctypes.c_int
    if hasattr(nvml, "nvmlDeviceGetMinMaxFanSpeed"):
        nvml.nvmlDeviceGetMinMaxFanSpeed.argtypes = [
            c_void_p,
            ctypes.POINTER(c_uint),
            ctypes.POINTER(c_uint),
        ]
        nvml.nvmlDeviceGetMinMaxFanSpeed.restype = ctypes.c_int
    nvml.nvmlDeviceSetFanSpeed_v2.argtypes = [c_void_p, c_uint, c_uint]
    nvml.nvmlDeviceSetFanSpeed_v2.restype = ctypes.c_int
    nvml.nvmlDeviceSetDefaultFanSpeed_v2.argtypes = [c_void_p, c_uint]
    nvml.nvmlDeviceSetDefaultFanSpeed_v2.restype = ctypes.c_int

    curve = [tuple(point) for point in fan_config["curve"]]

    validate_curve(curve)

    gpu_index = gpu_config["index"]
    poll_interval_s = fan_config["poll_interval_s"]
    hysteresis_c = fan_config["hysteresis_c"]
    mode = fan_config["mode"]
    min_fan_speed_pct = fan_config["min_fan_speed_pct"]
    max_fan_speed_pct = fan_config["max_fan_speed_pct"]
    max_step_up_pct_per_s = fan_config["max_step_up_pct_per_s"]
    max_step_down_pct_per_s = fan_config["max_step_down_pct_per_s"]
    manual_enable_temp_c = fan_config["manual_enable_temp_c"]
    auto_restore_temp_c = fan_config["auto_restore_temp_c"]
    emergency_auto_override_temp_c = fan_config.get("emergency_auto_override_temp_c", 80.0)
    emergency_auto_resume_temp_c = fan_config.get("emergency_auto_resume_temp_c", 75.0)
    enable_persistence_mode = gpu_config["enable_persistence_mode"]
    force_update_every_poll = fan_config["force_update_every_poll"]
    translated_gpu_policy = None
    afterburner_source = None
    afterburner_profile_settings = None
    vf_apply_result = None
    clock_ceiling_controller = None
    vf_expected_samples = []
    last_vf_reapply_monotonic = 0.0
    vf_reapply_cooldown_s = max(float(poll_interval_s), 10.0)

    device = c_void_p()
    check(nvml.nvmlInit_v2(), "nvmlInit_v2")
    check(
        nvml.nvmlDeviceGetHandleByIndex_v2(c_uint(gpu_index), ctypes.byref(device)),
        "nvmlDeviceGetHandleByIndex_v2",
    )
    voltage_reader = create_hidden_voltage_reader(nvml)
    vf_curve_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    try:
        gpu_policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
    except Exception as exc:
        gpu_policy_controller = None
        log(f"Linux GPU policy helper unavailable: {exc}")

    if afterburner_root:
        try:
            afterburner_source = resolve_afterburner_vf_source(
                afterburner_root=afterburner_root,
                section=afterburner_profile or None,
                device_profile_hint=afterburner_device_profile or None,
                dangerously_skip_validation=bool(
                    afterburner_runtime_options.get("dangerously_skip_validation")
                ),
            )
        except Exception as exc:
            log(f"Skipping Afterburner source resolve: error={exc}")
        else:
            if afterburner_source.get("dangerously_skip_validation"):
                log(
                    "Afterburner validation override enabled: skipping the default "
                    "flat-tail and undervolt checks for the saved profile."
                )
            if gpu_policy_controller is not None:
                try:
                    afterburner_profile_settings = load_afterburner_profile_settings(
                        profile_path=afterburner_source["profile_path"],
                        section=afterburner_source["section"],
                    )
                    translated_gpu_policy = translate_afterburner_gpu_policy(
                        afterburner_profile_settings,
                        power_limits=gpu_policy_controller.query_power_limits(),
                        power_limit_cap_w=afterburner_runtime_options["power_limit_override_w"],
                    )
                except Exception as exc:
                    translated_gpu_policy = None
                    log(
                        "Skipping Afterburner GPU policy translate: "
                        f"section={afterburner_source['section']} error={exc}"
                    )

    startup_power_limit_w = None
    if translated_gpu_policy is not None and translated_gpu_policy.get("power_limit_w") is not None:
        startup_power_limit_w = translated_gpu_policy["power_limit_w"]
    apply_gpu_base_policy(
        gpu_index=gpu_index,
        enable_persistence_mode=enable_persistence_mode,
        power_limit_w=startup_power_limit_w,
    )
    if vf_curve_reader is not None and afterburner_source is not None:
            if translated_gpu_policy is not None and gpu_policy_controller is not None:
                try:
                    apply_translated_gpu_policy(gpu_policy_controller, translated_gpu_policy)
                except Exception as exc:
                    log(
                        "Skipping Afterburner GPU policy apply: "
                        f"section={afterburner_source['section']} error={exc}"
                    )
                else:
                    log(
                        f"Applied Afterburner GPU policy: section={afterburner_source['section']} "
                        f"{describe_translated_gpu_policy(translated_gpu_policy)}."
                    )
            try:
                vf_apply_result = apply_afterburner_curve_to_reader(
                    vf_curve_reader,
                    profile_path=afterburner_source["profile_path"],
                    section=afterburner_source["section"],
                    gpu_policy=translated_gpu_policy,
                    preserve_vanilla_below_mv=afterburner_runtime_options["preserve_vanilla_below_mv"],
                )
            except Exception as exc:
                log(
                    "Skipping Afterburner VF curve apply: "
                    f"section={afterburner_source['section']} error={exc}"
                )
            else:
                log(
                    f"Applied Afterburner VF curve: section={afterburner_source['section']} "
                    f"matched={len(vf_apply_result['plan'])} "
                    f"changed={len(vf_apply_result['changed_points'])} "
                    f"mode={vf_apply_result['translation_mode']} "
                    f"origin={vf_apply_result['translation_origin']} "
                    f"linux_profile={vf_apply_result['translated_linux_profile_path']}."
                )
                vf_expected_samples = select_expected_vf_samples(vf_apply_result["plan"])
                flatten_target = derive_afterburner_dynamic_lock(vf_apply_result["materialization"]["points"])
                if flatten_target is None:
                    log(
                        f"Skipping Afterburner clock ceiling: section={afterburner_source['section']} "
                        "no flattened V/F target was detected."
                    )
                else:
                    try:
                        clock_ceiling_controller = FlattenedClockCeilingController(
                            flatten_target=flatten_target,
                            policy_controller=gpu_policy_controller,
                        )
                        clock_ceiling_controller.apply()
                    except Exception as exc:
                        clock_ceiling_controller = None
                        log(
                            "Skipping Afterburner clock ceiling: "
                            f"section={afterburner_source['section']} error={exc}"
                        )
                    else:
                        log(
                            f"Configured Afterburner clock ceiling: section={afterburner_source['section']} "
                            f"{clock_ceiling_controller.describe()}."
                        )

    fan_count = c_uint()
    check(nvml.nvmlDeviceGetNumFans(device, ctypes.byref(fan_count)), "nvmlDeviceGetNumFans")

    if fan_count.value == 0:
        raise NvmlError("GPU reports zero controllable fans")

    device_min_fan_speed_pct = None
    device_max_fan_speed_pct = None
    if hasattr(nvml, "nvmlDeviceGetMinMaxFanSpeed"):
        fan_min = c_uint()
        fan_max = c_uint()
        rc = nvml.nvmlDeviceGetMinMaxFanSpeed(
            device,
            ctypes.byref(fan_min),
            ctypes.byref(fan_max),
        )
        if rc == NVML_SUCCESS and fan_max.value >= fan_min.value:
            device_min_fan_speed_pct = fan_min.value
            device_max_fan_speed_pct = fan_max.value

    effective_min_fan_speed_pct = min_fan_speed_pct
    effective_max_fan_speed_pct = max_fan_speed_pct
    if device_min_fan_speed_pct is not None:
        effective_min_fan_speed_pct = max(effective_min_fan_speed_pct, device_min_fan_speed_pct)
    if device_max_fan_speed_pct is not None:
        effective_max_fan_speed_pct = min(effective_max_fan_speed_pct, device_max_fan_speed_pct)
    if effective_max_fan_speed_pct < effective_min_fan_speed_pct:
        raise NvmlError("effective fan speed range is invalid")

    restored = False

    def restore_default():
        nonlocal restored
        if restored:
            return
        restored = True
        for fan_idx in range(fan_count.value):
            nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx))
        if clock_ceiling_controller is not None:
            clock_ceiling_controller.close()
        if vf_curve_reader is not None:
            vf_curve_reader.close()
        if gpu_policy_controller is not None:
            gpu_policy_controller.close()
        nvml.nvmlShutdown()

    def stop(_signum, _frame):
        restore_default()
        sys.exit(0)

    atexit.register(restore_default)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    last_speed = None
    last_set_temp_c = None
    last_update_time = time.monotonic()
    manual_mode_active = False
    hot_auto_mode_active = False
    print(
        f"Controlling GPU {gpu_index} with {fan_count.value} fan(s), "
        f"mode={mode}, hysteresis={hysteresis_c} C, "
        f"manual-limits={effective_min_fan_speed_pct}-{effective_max_fan_speed_pct}%, "
        f"manual-enable={manual_enable_temp_c} C, auto-restore={auto_restore_temp_c} C, "
        f"emergency-auto={emergency_auto_override_temp_c} C/{emergency_auto_resume_temp_c} C. "
        "Press Ctrl-C to restore auto mode."
    , flush=True)
    startup_gpu_policy = translated_gpu_policy or {"power_limit_w": startup_power_limit_w}
    log(
        f"GPU policy: persistence={'on' if enable_persistence_mode else 'off'}, "
        f"{describe_translated_gpu_policy(startup_gpu_policy)}."
    )
    log(f"Config file: {config_path}")
    if afterburner_source is not None:
        flatten_target = afterburner_source["section_info"].get("flatten_target")
        flatten_text = (
            describe_afterburner_dynamic_lock(flatten_target)
            if flatten_target is not None
            else "none"
        )
        log(
            "Afterburner import: "
            f"root={afterburner_source['afterburner_root']} "
            f"device_profile={afterburner_source['profile_path'].name} "
            f"profile={afterburner_source['section']} "
            f"flatten-target={flatten_text}."
        )
        log(
            "Afterburner flatten validation: "
            f"{describe_afterburner_flatten_validation(afterburner_source['section_info'].get('flatten_validation'))}."
        )
        if afterburner_profile_settings is not None:
            log(
                "Afterburner parsed settings: "
                f"{describe_afterburner_profile_settings(afterburner_profile_settings)}."
            )
    if vf_curve_reader is not None:
        vf_summary = vf_curve_reader.summary()
        log(
            f"Linux NVAPI VF curve: "
            f"active-points={vf_summary['active_points']}, "
            f"editable-core-points={vf_summary['editable_core_points']}."
        )
    if device_min_fan_speed_pct is not None and device_max_fan_speed_pct is not None:
        log(
            f"Device fan limits reported by NVML: "
            f"{device_min_fan_speed_pct}-{device_max_fan_speed_pct}%."
        )
    effective_manual_curve = build_effective_manual_curve(
        curve=curve,
        manual_enable_temp_c=manual_enable_temp_c,
        effective_min_fan_speed_pct=effective_min_fan_speed_pct,
        effective_max_fan_speed_pct=effective_max_fan_speed_pct,
        mode=mode,
    )
    curve_source = fan_config.get("curve_source")
    if curve_source:
        curve_flags_u32 = int(fan_config.get("curve_source_flags_u32", 0))
        curve_period_ms = int(fan_config.get("curve_source_period_ms", int(round(poll_interval_s * 1000))))
        log(
            f"Fan curve source: {curve_source} "
            f"period={curve_period_ms}ms flags=0x{curve_flags_u32:08x}."
        )
    log(f"Fan curve points: {format_curve_points(curve)}")
    log(f"Effective manual fan curve: {format_curve_points(effective_manual_curve)}")
    if fan_config.get("curve_override_zero_with_hardware_curve"):
        behavior_parts = ["zero-rpm zone uses hardware auto curve"]
        if fan_config.get("curve_hardware_auto_below_device_min"):
            behavior_parts.append(
                "below device manual minimum uses hardware auto curve"
            )
        takeover_temp_c = fan_config.get("curve_manual_takeover_temp_c")
        if takeover_temp_c is not None:
            behavior_parts.append(f"manual takeover near {float(takeover_temp_c):.2f}C")
        log("Fan curve behavior: " + "; ".join(behavior_parts) + ".")
    log(
        "Silent fan curve guardrail: "
        f"hardware auto above {float(emergency_auto_override_temp_c):.0f}C, "
        f"resume manual below {float(emergency_auto_resume_temp_c):.0f}C."
    )
    if clock_ceiling_controller is not None:
        log(f"Clock ceiling policy: {clock_ceiling_controller.describe()}.")

    while True:
        loop_started = time.monotonic()
        temp = c_uint()
        check(
            nvml.nvmlDeviceGetTemperature(device, c_uint(NVML_TEMPERATURE_GPU), ctypes.byref(temp)),
            "nvmlDeviceGetTemperature",
        )

        current_temp_c = float(temp.value)
        power_draw_w = get_power_draw_w(nvml, device)

        telemetry_text = format_telemetry(
            nvml,
            device,
            fan_count.value,
            current_temp_c,
            voltage_reader=voltage_reader,
            vf_curve_reader=vf_curve_reader,
            gpu_policy_controller=gpu_policy_controller,
            power_draw_w=power_draw_w,
            clock_ceiling_controller=clock_ceiling_controller,
        )
        if vf_curve_reader is not None and vf_expected_samples:
            vf_mismatches = detect_vf_curve_reset(vf_curve_reader, vf_expected_samples)
            if vf_mismatches and (loop_started - last_vf_reapply_monotonic) >= vf_reapply_cooldown_s:
                try:
                    apply_plan(vf_curve_reader, vf_apply_result["plan"])
                    vf_curve_reader.refresh_points()
                except Exception as exc:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log(f"{timestamp} event=vf-curve-reapply-error error={exc}")
                else:
                    last_vf_reapply_monotonic = loop_started
                    mismatch_preview = ", ".join(
                        (
                            f"{int(item['voltage_mv'])}mV:"
                            f"{int(item['current_offset_mhz']):+d}->"
                            f"{int(item['expected_offset_mhz']):+d}MHz"
                        )
                        for item in vf_mismatches[:4]
                    )
                    if len(vf_mismatches) > 4:
                        mismatch_preview += ", ..."
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log(
                        f"{timestamp} {telemetry_text} "
                        f"event=vf-curve-reapplied mismatches={len(vf_mismatches)} "
                        f"samples={mismatch_preview}"
                    )
        fan_curve_state_text = describe_fan_curve_state(
            current_temp_c=current_temp_c,
            effective_curve=effective_manual_curve,
            manual_mode_active=manual_mode_active,
            emergency_auto_mode_active=hot_auto_mode_active,
            emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
        )
        if hot_auto_mode_active and current_temp_c > emergency_auto_resume_temp_c:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} fan_mode=auto reason=emergency-override")
            time.sleep(poll_interval_s)
            continue

        if hot_auto_mode_active and current_temp_c <= emergency_auto_resume_temp_c:
            hot_auto_mode_active = False
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} event=emergency-override-cleared")

        if current_temp_c > emergency_auto_override_temp_c:
            if manual_mode_active:
                for fan_idx in range(fan_count.value):
                    check(
                        nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx)),
                        f"nvmlDeviceSetDefaultFanSpeed_v2 fan {fan_idx}",
                    )
                manual_mode_active = False
                last_speed = None
                last_set_temp_c = None
            hot_auto_mode_active = True
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} "
                f"event=restoring-auto-mode reason=emergency-override"
            )
            time.sleep(poll_interval_s)
            continue

        if not manual_mode_active and current_temp_c < manual_enable_temp_c:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} fan_mode=auto")
            time.sleep(poll_interval_s)
            continue

        if not manual_mode_active:
            manual_mode_active = True
            last_speed = None
            last_set_temp_c = None
            last_update_time = loop_started
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} event=entering-manual-mode")

        if current_temp_c <= auto_restore_temp_c:
            for fan_idx in range(fan_count.value):
                check(
                    nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx)),
                    f"nvmlDeviceSetDefaultFanSpeed_v2 fan {fan_idx}",
                )
            manual_mode_active = False
            last_speed = None
            last_set_temp_c = None
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} event=restoring-auto-mode")
            time.sleep(poll_interval_s)
            continue

        raw_target_speed = speed_for_temp(current_temp_c, curve, mode=mode)
        raw_target_speed = clamp(
            raw_target_speed,
            effective_min_fan_speed_pct,
            effective_max_fan_speed_pct,
        )

        hysteresis_target_speed = apply_hysteresis(
            current_temp_c=current_temp_c,
            raw_target_speed=raw_target_speed,
            last_temp_c=last_set_temp_c,
            last_speed=last_speed,
            hysteresis_c=hysteresis_c,
        )

        limited_target_speed = limit_speed_change(
            target_speed=hysteresis_target_speed,
            last_speed=last_speed,
            elapsed_s=loop_started - last_update_time,
            max_step_up_pct_per_s=max_step_up_pct_per_s,
            max_step_down_pct_per_s=max_step_down_pct_per_s,
        )
        target_speed = round(
            clamp(
                limited_target_speed,
                effective_min_fan_speed_pct,
                effective_max_fan_speed_pct,
            )
        )

        if force_update_every_poll or target_speed != last_speed:
            for fan_idx in range(fan_count.value):
                check(
                    nvml.nvmlDeviceSetFanSpeed_v2(device, c_uint(fan_idx), c_uint(target_speed)),
                    f"nvmlDeviceSetFanSpeed_v2 fan {fan_idx}",
                )
            last_set_temp_c = current_temp_c
            last_speed = target_speed
            last_update_time = loop_started

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log(
            f"{timestamp} {telemetry_text} {fan_curve_state_text} "
            f"target={target_speed}% curve={raw_target_speed:.1f}% "
            f"hyst={hysteresis_target_speed:.1f}% fan_mode=manual"
        )

        time.sleep(poll_interval_s)


if __name__ == "__main__":
    try:
        runtime_flags = parse_runtime_flags(sys.argv[1:])
        runtime_argv = runtime_flags["passthrough"]
        if runtime_flags["install_systemd_service"]:
            install_systemd_service(
                __file__,
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
                log=log,
            )
        elif runtime_flags["uninstall_systemd_service"]:
            uninstall_systemd_service(log=log)
        elif runtime_flags["daemonize"] and not runtime_flags["foreground"] and not running_under_systemd_service():
            daemonize_with_systemd(
                __file__,
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
                log=log,
            )
        else:
            main(
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
            )
    except Exception as exc:
        debug_exception("fatal error", exc)
        print(f"error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
