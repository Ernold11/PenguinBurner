#!/usr/bin/env python3

import argparse
import atexit
import ctypes
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

from auto_uv import (
    DEFAULT_AUTO_UV_FINAL_DURATION_S,
    AutoUvError,
    AutoUvFinalChoiceDiscarded,
    build_long_stability_test_config,
    long_stability_workload_durations,
    restore_afterburner_defaults_from_config,
    run_auto_uv_voltage_scan,
)
from auto_uv.profiles import (
    auto_uv_profiles_dir,
    delete_auto_uv_profiles,
    format_profile_table,
    mark_auto_uv_profile_verification_failed,
    mark_auto_uv_profile_verified,
    read_auto_uv_profile_summaries,
    resolve_auto_uv_profile,
)
from auto_uv.preflight import require_auto_uv_preflight
from auto_uv.sweep_modes import AUTO_UV_MODES, normalize_auto_uv_mode
from auto_uv.tuning import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_FAN_TUNING,
    AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO,
    AUTO_UV_METRIC_TUNING,
    AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO,
)
from afterburner.fan_curve import (
    load_afterburner_fan_settings,
    resolve_afterburner_fan_profile,
)
from afterburner.vfcurve import (
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
from lact import (
    LactExportError,
    write_lact_nvidia_config,
    write_lact_nvidia_config_from_afterburner,
)
from afterburner.import_fan_curve import (
    build_imported_fan_section,
    write_config as write_runtime_config,
)
from afterburner.import_vf_curve import (
    apply_plan,
    apply_afterburner_curve_to_reader,
    backup_current_offsets,
    ensure_afterburner_root_configured,
    load_afterburner_runtime_options,
    persist_afterburner_import,
    restore_offsets,
)
from nvml_gpu_policy import (
    MAX_AFTERBURNER_MEM_OFFSET_MHZ,
    NvmlGpuPolicyController,
    apply_translated_gpu_policy,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_runtime_config_path,
    default_saved_uv_dir,
    default_user_config_dir,
    resolve_afterburner_root,
)
from stability.q2rtx import (
    DEFAULT_DEMO_NAME,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    Q2RTXStabilityConfig,
    StabilityTestError,
    attach_stdout_progress,
    default_q2rtx_install_data_dir,
    install_latest_q2rtx,
    print_q2rtx_stability_result,
    run_cuda_stability_test,
    run_q2rtx_stability_test,
    resolve_q2rtx_executable,
)
from runtime_debug import (
    close_debug_log,
    close_stdio_capture,
    debug_log,
    debug_effective_runtime_options,
    debug_exception,
    enable_cli_output_wrapping,
    enable_debug_logging,
    enable_stdio_capture,
    log,
)

from subprocess_locale import stable_subprocess_env
from runtime_service import (
    DEFAULT_JOURNAL_HOURS,
    daemonize_with_systemd,
    install_systemd_service,
    launcher_script_path,
    parse_runtime_flags,
    running_under_systemd_service,
    stop_existing_penguin_burner_runtime,
    systemd_is_available,
    uninstall_systemd_service,
)


PROFILE_VERIFY_VOLTAGE_TOLERANCE_MV = 50
PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK = 5
PROFILE_VERIFY_VOLTAGE_WARMUP_S = 8.0
PROFILE_VERIFY_BASELINE_DURATION_S = 60
PROFILE_VERIFY_BASELINE_MIN_DURATION_S = 10

NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2
NVIDIA_SMI = shutil.which("nvidia-smi") or "nvidia-smi"


class NvmlError(RuntimeError):
    pass


class FanCurveBlockedError(NvmlError):
    pass


atexit.register(close_debug_log)
atexit.register(close_stdio_capture)


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
            "afterburner_auto_uv_max_drop_pct": 16.0,
            "auto_uv_final_seconds": DEFAULT_AUTO_UV_FINAL_DURATION_S,
            "auto_uv_efficiency_stop_streak": AUTO_UV_DEFAULTS.efficiency_stop_streak,
            "auto_uv_min_efficiency_stop_drop_pct": (
                AUTO_UV_METRIC_TUNING.min_efficiency_stop_voltage_drop_pct
            ),
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
        "stability": {
            "q2rtx_dir": "",
            "q2rtx_binary": "",
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

    for section in ("gpu", "fan", "stability"):
        values = loaded.get(section)
        if isinstance(values, dict):
            config[section].update(values)

    return config, config_path


def _load_raw_runtime_config(config_path):
    path = Path(config_path).expanduser()
    if not path.exists():
        return {}
    with path.open("rb") as config_file:
        return tomllib.load(config_file)


def persist_stability_q2rtx_source(
    config_path,
    *,
    q2rtx_dir,
    q2rtx_binary,
    progress_context,
):
    path = Path(config_path).expanduser()
    config = _load_raw_runtime_config(path)
    gpu = dict(config.get("gpu", {}))
    fan = dict(config.get("fan", {}))
    stability = dict(config.get("stability", {}))
    stability["q2rtx_dir"] = str(q2rtx_dir) if q2rtx_dir else ""
    stability["q2rtx_binary"] = str(q2rtx_binary) if q2rtx_binary else ""
    write_runtime_config(
        path,
        {
            "gpu": gpu,
            "fan": fan,
            "stability": stability,
        },
    )
    print(f"{progress_context}: saved Q2RTX source to {path}", flush=True)


def afterburner_root_has_imported_profiles(afterburner_root) -> bool:
    root_text = str(afterburner_root or "").strip()
    if not root_text:
        return False
    root = Path(root_text).expanduser()
    return (root / "MSIAfterburner.cfg").is_file() and (root / "Profiles").is_dir()


def clear_auto_uv_state(*, log=print) -> None:
    config_dir = default_user_config_dir()
    paths = [
        config_dir / "uv-result",
        auto_uv_profiles_dir(),
        config_dir / "auto-uv-final-curve.json",
        config_dir / "auto-uv-fan-curve.json",
        config_dir / "debug-logs",
        config_dir / "stability-logs",
        default_saved_uv_dir(),
    ]
    for path in paths:
        path = Path(path)
        if not path.exists():
            log(f"Auto-UV clear: already absent {path}")
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except PermissionError as exc:
            raise NvmlError(
                f"failed to remove {path}: {exc}. Re-run with sudo."
            ) from exc
        log(f"Auto-UV clear: removed {path}")
    log(
        "Auto-UV clear: complete. Afterburner imports and Q2RTX downloads were left untouched."
    )


def emit_json_event(enabled: bool, event: str, **payload) -> None:
    if not enabled:
        return
    data = {"event": str(event)}
    data.update(_json_event_without_none_values(payload))
    print(json.dumps(data, sort_keys=True), flush=True)


def _json_event_without_none_values(value):
    if isinstance(value, dict):
        return {
            key: _json_event_without_none_values(inner)
            for key, inner in value.items()
            if inner is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            _json_event_without_none_values(inner)
            for inner in value
            if inner is not None
        ]
    return value


def parse_main_args(argv):
    parser = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "PenguinBurner Auto-UV runtime, stability, and optional "
            "Afterburner import utility."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    auto_uv_group = parser.add_argument_group("Auto-UV")
    daemon_group = parser.add_argument_group("Runtime and daemon essentials")
    runtime_group = parser.add_argument_group("Runtime tuning")
    lact_group = parser.add_argument_group("LACT export")
    stability_group = parser.add_argument_group("Stability workload")
    afterburner_group = parser.add_argument_group("Afterburner import")
    advanced_group = parser.add_argument_group("Advanced/debug")

    auto_uv_group.add_argument(
        "--auto-uv-voltage-scan",
        action="store_true",
        help=(
            "Discover a stable fixed-clock undervolt from the live/default "
            "NVIDIA V/F curve, step the lock voltage down through real editable "
            "VF bins, and verify candidates with Q2RTX plus CUDA load"
        ),
    )
    auto_uv_group.add_argument(
        "--json-events",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-require-final-choice",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--list-auto-uv-profiles",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--delete-auto-uv-profiles",
        nargs="+",
        default=[],
        metavar="PROFILE",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--fresh-auto-uv-scan",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--clear-auto-uv-state",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-max-clock-drop-pct",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Maximum loaded GPU core clock drop allowed during Auto-UV; "
            "default 10.0. Example: 12 allows up to a looser 12%% clock drop."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-overclock-budget-ratio",
        dest="auto_uv_clock_bump_budget_ratio",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Fraction of --auto-uv-max-clock-drop-pct available as total Auto-UV "
            "overclock budget; each recovery spends only the measured clock "
            "shortfall plus a small safety step. "
            f"default {AUTO_UV_DEFAULTS.clock_bump_budget_ratio:.2f} "
            "for efficiency, "
            f"{AUTO_UV_DEFAULTS.performance_clock_bump_budget_ratio:.2f} "
            "for performance. "
            f"Clamped to 0.0..{AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO:.2f}."
        ),
    )
    auto_uv_group.add_argument(
        "--yolo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-clock-bump-budget-ratio",
        dest="auto_uv_clock_bump_budget_ratio",
        type=float,
        metavar="N",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-mode",
        choices=AUTO_UV_MODES,
        default=None,
        metavar="MODE",
        help=(
            "Auto-UV tuning behavior. efficiency keeps the current FPS/W-first "
            "search behavior; performance disables efficiency-wall stopping so "
            "performance-specific behavior can evolve separately."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-max-drop-pct",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Maximum percentage drop below the first discovered auto-UV start "
            "voltage allowed during candidate search; default 16.0 for "
            "efficiency, 14.0 for performance."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-final-seconds",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Final Auto-UV verification duration in seconds after the best curve "
            "is selected; default 600. Candidate probes remain tiered short tests."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-short-seconds",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Base Auto-UV verification length in seconds; default "
            f"{AUTO_UV_DEFAULTS.probe_duration_s}. Allowed range 10..60. "
            "Deeper voltage tiers use 2x and 3x this value."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-memory-offset-mhz",
        type=int,
        default=None,
        metavar="MHz",
        help=(
            "Memory clock V/F offset in MHz to apply during Auto-UV and save "
            f"with the final profile; range 0..{MAX_AFTERBURNER_MEM_OFFSET_MHZ}."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-efficiency-stop-streak",
        type=int,
        default=None,
        metavar="N",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-min-efficiency-stop-drop-pct",
        type=float,
        default=None,
        metavar="N",
        help=argparse.SUPPRESS,
    )
    daemon_group.add_argument(
        "--silent-fan-curve",
        action="store_true",
        help=(
            "Runtime/daemon only: opt in to PenguinBurner manual fan-curve "
            "control; by default fan control is left to the GPU driver. "
            "Auto-UV scans write a suggested fan curve automatically when safe."
        ),
    )
    daemon_group.add_argument(
        "--daemonize",
        action="store_true",
        help=(
            "Launch normal runtime as a transient systemd service after an "
            "Auto-UV final curve exists. Auto-UV scans remain foreground-only."
        ),
    )
    daemon_group.add_argument(
        "--install-systemd-service",
        action="store_true",
        help=(
            "Install and start the persistent boot-time PenguinBurner systemd "
            "service for the current checkout."
        ),
    )
    daemon_group.add_argument(
        "--uninstall-systemd-service",
        "--deinstall-systemd-service",
        dest="uninstall_systemd_service",
        action="store_true",
        help="Stop and remove the persistent PenguinBurner systemd service.",
    )
    daemon_group.add_argument(
        "--foreground",
        action="store_true",
        help=(
            "Force normal runtime to stay in the current foreground process; "
            "this is the default when not daemonizing."
        ),
    )
    daemon_group.add_argument(
        "--auto-uv-profile",
        default="",
        help=(
            "Runtime/daemon only: use an Auto-UV profile by profile id, candidate "
            "id, JSON path, 'active', or 'latest'."
        ),
    )
    daemon_group.add_argument(
        "--journal-hours",
        type=float,
        default=DEFAULT_JOURNAL_HOURS,
        metavar="N",
        help=(
            "Hours of systemd journal history to suggest after daemonizing; "
            f"default {DEFAULT_JOURNAL_HOURS}."
        ),
    )
    runtime_group.add_argument(
        "--config",
        default=str(get_config_path()),
        help="Runtime config path to read defaults from",
    )
    runtime_group.add_argument(
        "--gpu-index",
        type=int,
        default=None,
        help="Override the configured GPU index",
    )
    runtime_group.add_argument(
        "--power-limit-override-w",
        type=int,
        default=None,
        help="Optional manual power-limit cap in watts for translation preview",
    )
    lact_group.add_argument(
        "--export-lact-config",
        default="",
        help=(
            "Write a complete Nvidia-only LACT config.yaml from the saved "
            "Auto-UV or Afterburner V/F curve. Add --silent-fan-curve to "
            "include fan settings. Use --lact-gpu-id with the id from `lact cli list-gpus`."
        ),
    )
    lact_group.add_argument(
        "--lact-source",
        choices=("auto-uv", "afterburner"),
        default="auto-uv",
        help="Source for --export-lact-config; default auto-uv.",
    )
    lact_group.add_argument(
        "--fan-curve-export",
        action="store_true",
        help=(
            "With --export-lact-config, export only the fan curve to LACT and "
            "omit gpu_vf_curve."
        ),
    )
    lact_group.add_argument(
        "--lact-gpu-id",
        default="",
        help="LACT GPU id to use in --export-lact-config output.",
    )
    stability_group.add_argument(
        "--stability-test",
        action="store_true",
        help=("Run a non-interactive Q2RTX timedemo stability workload and exit"),
    )
    stability_group.add_argument(
        "--install-q2rtx",
        action="store_true",
        help=(
            "Download the latest official Q2RTX Linux tar.gz to "
            "~/.cache/PenguinBurner/q2rtx and extract everything under "
            "~/.local/share/PenguinBurner/q2rtx"
        ),
    )
    stability_group.add_argument(
        "--stability-seconds",
        type=int,
        default=DEFAULT_AUTO_UV_FINAL_DURATION_S,
        help=(
            "Wall-clock duration budget for --stability-test; uses the same "
            "Q2RTX + CUDA companion load as auto-UV final verification; "
            "default 600"
        ),
    )
    stability_group.add_argument(
        "--stability-workload",
        choices=("q2rtx-cuda", "q2rtx", "cuda"),
        default="q2rtx-cuda",
        help=(
            "Workload selection for --stability-test; q2rtx-cuda keeps the "
            "standard Q2RTX timedemo plus CUDA compute split, q2rtx or cuda "
            "runs only that workload for the full duration."
        ),
    )
    stability_group.add_argument(
        "--stability-width",
        type=int,
        default=DEFAULT_WIDTH,
        help=f"Q2RTX render width used by --stability-test; default {DEFAULT_WIDTH}",
    )
    stability_group.add_argument(
        "--stability-height",
        type=int,
        default=DEFAULT_HEIGHT,
        help=f"Q2RTX render height used by --stability-test; default {DEFAULT_HEIGHT}",
    )
    stability_group.add_argument(
        "--show-q2rtx-window",
        action="store_true",
        help=(
            "Do not move the Q2RTX Vulkan window off-screen during stability "
            "tests and Auto-UV scans"
        ),
    )
    stability_group.add_argument(
        "--stability-log-dir",
        default="",
        help=(
            "Optional log directory for --stability-test; defaults to "
            "~/.config/PenguinBurner/stability-logs"
        ),
    )
    stability_group.add_argument(
        "--stability-stop-request-file",
        default="",
        help=argparse.SUPPRESS,
    )
    stability_group.add_argument(
        "--stability-q2rtx-dir",
        default="",
        help="Q2RTX install/source root containing q2rtx and baseq2/",
    )
    stability_group.add_argument(
        "--stability-q2rtx-binary",
        default="",
        help="Explicit q2rtx executable path",
    )
    afterburner_group.add_argument(
        "--afterburner-dir",
        default="",
        help="Path to the MSI Afterburner root directory",
    )
    afterburner_group.add_argument(
        "--profile-section",
        "--section",
        dest="profile_section",
        default="",
        help="Optional saved Afterburner profile section such as profile2",
    )
    afterburner_group.add_argument(
        "--afterburner-device-profile",
        default="",
        help="Optional device profile file under Profiles/ to inspect or use",
    )
    afterburner_group.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Inspect Afterburner fan/VF data and draw dry-run previews without "
            "touching GPU state; recommended first step and does not require sudo"
        ),
    )
    afterburner_group.add_argument(
        "--prefer-afterburner-curve",
        action="store_true",
        help=(
            "Runtime/daemon only: apply the imported Afterburner V/F curve before "
            "the saved Auto-UV final curve. Auto-UV remains the fallback if "
            "Afterburner is missing or cannot be applied."
        ),
    )
    afterburner_group.add_argument(
        "--restore-defaults-from-config",
        "--restore-afterburner-defaults",
        dest="restore_defaults_from_config",
        action="store_true",
        help=(
            "Apply the Defaults V/F curve and translated GPU policy from the "
            "Afterburner device profile saved in config, then exit"
        ),
    )
    advanced_group.add_argument(
        "--preserve-vf-below-mv",
        "--preserve-base-vf-below-mv",
        dest="preserve_base_below_mv",
        type=int,
        default=None,
        help=(
            "Keep the base Linux VF curve at and below this inclusive "
            "voltage; useful if repeated Afterburner curve edits disturbed "
            "idle or low-voltage scaling"
        ),
    )
    advanced_group.add_argument(
        "--preserve-vanilla-vf-below-mv",
        dest="preserve_base_below_mv",
        type=int,
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--dangerously-skip-validation",
        action="store_true",
        help=(
            "Bypass the default flat-tail and undervolt checks when selecting "
            "the saved Afterburner profile; advanced and not recommended"
        ),
    )
    advanced_group.add_argument(
        "--debug-log",
        action="store_true",
        help=(
            "Write a verbose dry-run and first-import diagnostic log next to "
            "the selected config file under debug-logs/; with the default "
            "config this is ~/.config/PenguinBurner/debug-logs"
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


def validate_auto_uv_fan_curve_safety(curve, path):
    max_points = int(AUTO_UV_FAN_TUNING.max_curve_points)
    if len(curve) > max_points:
        raise FanCurveBlockedError(
            f"auto-UV fan curve has too many points: {len(curve)} > {max_points}: {path}"
        )

    zero_temp_c = float(AUTO_UV_FAN_TUNING.zero_rpm_until_temp_c)
    active_temp_c = float(AUTO_UV_FAN_TUNING.minimum_active_temp_c)
    active_speed_pct = float(AUTO_UV_FAN_TUNING.minimum_active_speed_pct)
    safe_temp_c = float(AUTO_UV_FAN_TUNING.max_base_curve_load_temp_c)
    emergency_temp_c = float(AUTO_UV_FAN_TUNING.emergency_temp_c)
    emergency_speed_pct = float(AUTO_UV_FAN_TUNING.emergency_min_speed_pct)
    full_speed_temp_c = float(AUTO_UV_FAN_TUNING.full_speed_temp_c)
    full_speed_pct = float(AUTO_UV_FAN_TUNING.full_speed_pct)
    hardware_override_temp_c = float(AUTO_UV_FAN_TUNING.hardware_auto_override_temp_c)

    def require_speed(temp_c, minimum_speed_pct, label):
        speed_pct = float(speed_for_temp(temp_c, curve, mode="linear"))
        if speed_pct + 0.01 < float(minimum_speed_pct):
            raise FanCurveBlockedError(
                f"auto-UV fan curve unsafe at {label}: "
                f"{speed_pct:.1f}% < {float(minimum_speed_pct):.1f}%: {path}"
            )

    zero_speed_pct = float(speed_for_temp(zero_temp_c, curve, mode="linear"))
    if zero_speed_pct > 0.01:
        raise FanCurveBlockedError(
            f"auto-UV fan curve unsafe at zero-rpm point: "
            f"{zero_temp_c:.1f}C is {zero_speed_pct:.1f}% instead of 0%: {path}"
        )
    require_speed(active_temp_c, active_speed_pct, "active minimum")
    require_speed(safe_temp_c, active_speed_pct, "safe load target")
    require_speed(emergency_temp_c, emergency_speed_pct, "emergency")
    require_speed(full_speed_temp_c, full_speed_pct, "full speed")
    if hardware_override_temp_c <= full_speed_temp_c:
        raise FanCurveBlockedError(
            "auto-UV fan curve hardware-auto override must be above the "
            f"{full_speed_temp_c:.1f}C full-speed point"
        )


def select_expected_vf_samples(plan, *, max_samples=8):
    candidates = [item for item in plan if int(item["new_offset_mhz"]) != 0]
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


def format_vf_curve_mismatch_preview(mismatches, *, max_items=4) -> str:
    preview = ", ".join(
        (
            f"{int(item['voltage_mv'])}mV:"
            f"{int(item['current_offset_mhz']):+d}->"
            f"{int(item['expected_offset_mhz']):+d}MHz"
        )
        for item in list(mismatches)[: int(max_items)]
    )
    if len(mismatches) > int(max_items):
        preview += ", ..."
    return preview


def load_auto_uv_final_curve(profile_selector="", *, allow_unverified: bool = False):
    selector = str(profile_selector or "").strip()
    resolved_profile = resolve_auto_uv_profile(
        selector or "latest",
        allow_unverified=bool(allow_unverified),
    )
    if resolved_profile is None:
        if selector:
            raise NvmlError(f"Auto-UV profile not found: {selector}")
        return None
    path, _profile_payload = resolved_profile
    if not path.is_file():
        return None

    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    raw_points = payload.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raw_points = payload.get("plan")
    if not isinstance(raw_points, list) or not raw_points:
        raise NvmlError(f"auto-UV final curve has no V/F points: {path}")

    plan = []
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise NvmlError(f"auto-UV final curve contains a non-object point: {path}")
        try:
            item = {
                "index": int(raw["index"]),
                "voltage_mv": int(raw["voltage_mv"]),
                "base_mhz": int(raw["base_mhz"]),
                "target_mhz": int(raw["target_mhz"]),
                "new_offset_mhz": int(raw["new_offset_mhz"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise NvmlError(
                f"auto-UV final curve point is invalid: {path}: {raw}"
            ) from exc
        plan.append(item)

    lock_clock_mhz = int(payload.get("lock_clock_mhz", 0) or 0)
    candidate_voltage_mv = int(payload.get("candidate_voltage_mv", 0) or 0)
    if lock_clock_mhz <= 0 or candidate_voltage_mv <= 0:
        raise NvmlError(f"auto-UV final curve is missing lock clock or voltage: {path}")
    memory_offset_mhz = _profile_memory_offset_mhz(payload)

    voltage_bins = sorted({int(item["voltage_mv"]) for item in plan})
    tail_point_count = sum(
        1 for item in plan if int(item["voltage_mv"]) >= int(candidate_voltage_mv)
    )
    flatten_target = {
        "source": "auto-uv-final",
        "lock_clock_mhz": int(lock_clock_mhz),
        "lock_voltage_mv": int(candidate_voltage_mv),
        "end_voltage_mv": int(voltage_bins[-1]),
        "tail_point_count": int(tail_point_count),
    }
    return {
        "path": path,
        "plan": plan,
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(candidate_voltage_mv),
        "memory_offset_mhz": memory_offset_mhz,
        "flatten_target": flatten_target,
    }


def _profile_memory_offset_mhz(payload):
    for key in ("memory_offset_mhz", "mem_clk_vf_offset_mhz"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if value in (None, ""):
            continue
        try:
            return max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(round(float(value)))))
        except (TypeError, ValueError):
            return None
    return None


def _apply_auto_uv_profile_memory_offset(
    *,
    profile_label: str,
    memory_offset_mhz,
    gpu_policy_controller,
) -> dict:
    memory_offset = _profile_memory_offset_mhz(
        {"memory_offset_mhz": memory_offset_mhz}
    )
    if memory_offset is None:
        return {}
    if gpu_policy_controller is None:
        if int(memory_offset) == 0:
            return {"mem_clk_vf_offset_mhz": int(memory_offset)}
        raise NvmlError(
            "failed to apply saved profile memory offset "
            f"{int(memory_offset):+d} MHz for {profile_label}: "
            "Linux GPU policy helper is unavailable"
        )
    try:
        gpu_policy_controller.apply_clock_offsets(
            mem_clk_vf_offset_mhz=int(memory_offset)
        )
    except Exception as exc:
        raise NvmlError(
            "failed to apply saved profile memory offset "
            f"{int(memory_offset):+d} MHz for {profile_label}: "
            f"driver rejected nvmlDeviceSetMemClkVfOffset: {exc}"
        ) from exc
    return {"mem_clk_vf_offset_mhz": int(memory_offset)}


def load_auto_uv_fan_curve(current_fan_config):
    path = default_user_config_dir() / "auto-uv-fan-curve.json"
    if not path.is_file():
        return None

    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise NvmlError(f"auto-UV fan curve payload is invalid: {path}")
    max_base_load_temp_c = float(AUTO_UV_FAN_TUNING.max_base_curve_load_temp_c)
    loaded_temp_c = payload.get("loaded_temperature_c")
    if payload.get("fan_curve_blocked"):
        reason = str(payload.get("block_reason") or "unknown")
        try:
            temp_text = (
                "n/a" if loaded_temp_c is None else f"{float(loaded_temp_c):.1f}C"
            )
        except (TypeError, ValueError):
            temp_text = "invalid"
        raise FanCurveBlockedError(
            "auto-UV fan curve is blocked: "
            f"reason={reason} loaded-temp={temp_text} "
            f"limit={max_base_load_temp_c:.1f}C"
        )
    if loaded_temp_c is not None:
        try:
            loaded_temp = float(loaded_temp_c)
        except (TypeError, ValueError) as exc:
            raise NvmlError(
                f"auto-UV fan curve loaded temperature is invalid: {path}"
            ) from exc
        if loaded_temp > max_base_load_temp_c:
            raise FanCurveBlockedError(
                "auto-UV fan curve rejected: "
                f"saved final load temperature {loaded_temp:.1f}C is above "
                f"the {max_base_load_temp_c:.1f}C safety limit"
            )
    else:
        raise FanCurveBlockedError(
            "auto-UV fan curve rejected: missing saved final load temperature"
        )
    raw_fan = payload.get("fan")
    if not isinstance(raw_fan, dict):
        raise NvmlError(f"auto-UV fan curve has no fan section: {path}")
    raw_curve = raw_fan.get("curve")
    if not isinstance(raw_curve, list) or not raw_curve:
        raise NvmlError(f"auto-UV fan curve has no curve points: {path}")

    curve = []
    for raw_point in raw_curve:
        if not isinstance(raw_point, list) or len(raw_point) != 2:
            raise NvmlError(f"auto-UV fan curve point is invalid: {path}: {raw_point}")
        try:
            curve.append([float(raw_point[0]), float(raw_point[1])])
        except (TypeError, ValueError) as exc:
            raise NvmlError(
                f"auto-UV fan curve point is invalid: {path}: {raw_point}"
            ) from exc
    validate_curve(curve)
    validate_auto_uv_fan_curve_safety(curve, path)

    fan_config = dict(current_fan_config)
    fan_config.update(
        {
            "poll_interval_s": AUTO_UV_FAN_TUNING.poll_interval_s,
            "hysteresis_c": AUTO_UV_FAN_TUNING.hysteresis_c,
            "mode": "linear",
            "min_fan_speed_pct": AUTO_UV_FAN_TUNING.min_speed_pct,
            "max_fan_speed_pct": AUTO_UV_FAN_TUNING.max_speed_pct,
            "max_step_up_pct_per_s": AUTO_UV_FAN_TUNING.max_step_up_pct_per_s,
            "max_step_down_pct_per_s": AUTO_UV_FAN_TUNING.max_step_down_pct_per_s,
            "manual_enable_temp_c": AUTO_UV_FAN_TUNING.minimum_active_temp_c,
            "auto_restore_temp_c": AUTO_UV_FAN_TUNING.zero_rpm_until_temp_c,
            "emergency_auto_override_temp_c": AUTO_UV_FAN_TUNING.hardware_auto_override_temp_c,
            "emergency_auto_resume_temp_c": AUTO_UV_FAN_TUNING.emergency_resume_temp_c,
            "force_update_every_poll": False,
            "curve": curve,
            "curve_source": "auto-uv",
            "curve_source_path": str(path),
            "curve_source_loaded_temperature_c": payload.get("loaded_temperature_c"),
            "curve_source_observed_fan_speed_pct": payload.get(
                "observed_fan_speed_pct"
            ),
            "curve_source_generated_at": raw_fan.get("curve_source_generated_at"),
            "curve_source_target_load_temp_c": raw_fan.get(
                "curve_source_target_load_temp_c"
            ),
        }
    )
    return {
        "path": path,
        "payload": payload,
        "fan_config": fan_config,
    }


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


def build_stability_config(
    args,
    *,
    gpu_index,
    config_path,
    timedemo_loops_override=None,
    duration_override=None,
    auto_install_q2rtx=True,
    progress_context="Q2RTX stability",
    dependency_progress_callback=None,
    dependency_text_progress=True,
):
    def _emit_dependency_progress(percent, detail, **payload) -> None:
        if dependency_progress_callback is None:
            return
        data = {
            "label": "Downloading dependencies",
            "percent": round(max(0.0, min(100.0, float(percent))), 1),
            "detail": str(detail),
        }
        data.update(payload)
        dependency_progress_callback(data)

    config, _resolved_config_path = load_config(config_path)
    config_dir = Path(config_path).expanduser().parent
    default_log_dir = config_dir / "stability-logs"
    stability_config = dict(config.get("stability", {}))
    q2rtx_dir = str(stability_config.get("q2rtx_dir", "")).strip()
    q2rtx_binary = str(stability_config.get("q2rtx_binary", "")).strip()
    cli_q2rtx_dir = str(getattr(args, "stability_q2rtx_dir", "") or "").strip()
    cli_q2rtx_binary = str(getattr(args, "stability_q2rtx_binary", "") or "").strip()
    if cli_q2rtx_dir or cli_q2rtx_binary:
        q2rtx_dir = cli_q2rtx_dir
        q2rtx_binary = cli_q2rtx_binary
    q2rtx_source_from_config = bool(q2rtx_dir or q2rtx_binary)
    should_persist_q2rtx_source = bool(cli_q2rtx_dir or cli_q2rtx_binary)
    _emit_dependency_progress(0.0, "Checking Q2RTX dependency setup")

    if q2rtx_dir or q2rtx_binary:
        configured_dir = Path(q2rtx_dir).expanduser() if q2rtx_dir else None
        configured_binary = Path(q2rtx_binary).expanduser() if q2rtx_binary else None
        try:
            resolve_q2rtx_executable(
                q2rtx_dir=configured_dir,
                q2rtx_binary=configured_binary,
            )
        except StabilityTestError as exc:
            if not auto_install_q2rtx:
                print(
                    f"{progress_context}: configured Q2RTX source is not usable: {exc}",
                    flush=True,
                )
            else:
                configured_source = q2rtx_binary or q2rtx_dir
                print(
                    f"{progress_context}: configured Q2RTX source is not usable: "
                    f"{configured_source} ({exc})",
                    flush=True,
                )
                print(
                    f"{progress_context}: ignoring stale Q2RTX source and repairing automatically",
                    flush=True,
                )
                q2rtx_dir = ""
                q2rtx_binary = ""
                should_persist_q2rtx_source = True

    if not q2rtx_dir and not q2rtx_binary:
        managed_root = default_q2rtx_install_data_dir()
        print(
            f"{progress_context}: checking managed Q2RTX install under {managed_root}",
            flush=True,
        )
        _emit_dependency_progress(
            2.0,
            "Checking managed Q2RTX install",
            path=str(managed_root),
        )
        if managed_root.exists():
            version_dirs = sorted(
                (
                    path
                    for path in managed_root.iterdir()
                    if path.is_dir() and path.name != "compat"
                ),
                reverse=True,
            )
            for candidate in version_dirs:
                if (candidate / "q2rtx").is_file() and (candidate / "baseq2").exists():
                    q2rtx_dir = str(candidate)
                    print(
                        f"{progress_context}: found managed Q2RTX install {q2rtx_dir}",
                        flush=True,
                    )
                    if not q2rtx_source_from_config:
                        should_persist_q2rtx_source = True
                    break
            if not q2rtx_dir and (managed_root / "q2rtx").is_file():
                q2rtx_dir = str(managed_root)
                print(
                    f"{progress_context}: found managed Q2RTX install {q2rtx_dir}",
                    flush=True,
                )
                if not q2rtx_source_from_config:
                    should_persist_q2rtx_source = True
    if not q2rtx_dir and not q2rtx_binary:
        if not auto_install_q2rtx:
            print(
                f"{progress_context}: no managed Q2RTX install found and auto-install is disabled",
                flush=True,
            )
        else:
            print(
                f"{progress_context}: no managed Q2RTX install found; installing now",
                flush=True,
            )
            _emit_dependency_progress(
                4.0,
                "No managed Q2RTX install found; downloading dependencies",
            )
            install_result = install_latest_q2rtx(
                show_progress=bool(dependency_text_progress),
                progress_callback=dependency_progress_callback,
            )
            q2rtx_dir = str(install_result.install_dir)
            print(
                f"{progress_context}: using installed Q2RTX {install_result.version} at {q2rtx_dir}",
                flush=True,
            )
            should_persist_q2rtx_source = True
    if q2rtx_dir or q2rtx_binary:
        configured_source = q2rtx_binary or q2rtx_dir
        print(f"{progress_context}: Q2RTX source {configured_source}", flush=True)
        if should_persist_q2rtx_source:
            try:
                persist_stability_q2rtx_source(
                    config_path,
                    q2rtx_dir=q2rtx_dir,
                    q2rtx_binary=q2rtx_binary,
                    progress_context=progress_context,
                )
            except Exception as exc:
                print(
                    f"{progress_context}: warning: failed to save Q2RTX source to config: {exc}",
                    flush=True,
                )

    timedemo_loops = (
        int(timedemo_loops_override) if timedemo_loops_override is not None else None
    )
    if q2rtx_dir or q2rtx_binary:
        _emit_dependency_progress(
            100.0,
            "Dependencies are ready",
            source=str(q2rtx_binary or q2rtx_dir),
        )
    return Q2RTXStabilityConfig(
        duration_s=(
            int(duration_override)
            if duration_override is not None
            else int(args.stability_seconds)
        ),
        width=int(args.stability_width),
        height=int(args.stability_height),
        hide_window=not bool(args.show_q2rtx_window),
        demo_name=str(DEFAULT_DEMO_NAME).strip(),
        timedemo_loops=timedemo_loops,
        gpu_index=int(gpu_index),
        q2rtx_dir=Path(q2rtx_dir).expanduser() if q2rtx_dir else None,
        q2rtx_binary=Path(q2rtx_binary).expanduser() if q2rtx_binary else None,
        log_dir=Path(args.stability_log_dir).expanduser()
        if str(args.stability_log_dir).strip()
        else default_log_dir,
    )


def build_cuda_stability_config(
    args,
    *,
    gpu_index,
    config_path,
):
    config_dir = Path(config_path).expanduser().parent
    default_log_dir = config_dir / "stability-logs"
    return Q2RTXStabilityConfig(
        duration_s=int(args.stability_seconds),
        width=int(args.stability_width),
        height=int(args.stability_height),
        hide_window=not bool(args.show_q2rtx_window),
        demo_name=str(DEFAULT_DEMO_NAME).strip(),
        timedemo_loops=None,
        gpu_index=int(gpu_index),
        q2rtx_dir=None,
        q2rtx_binary=None,
        log_dir=Path(args.stability_log_dir).expanduser()
        if str(args.stability_log_dir).strip()
        else default_log_dir,
    )


def _stability_workload_selection(args) -> tuple[bool, bool]:
    workload = str(getattr(args, "stability_workload", "q2rtx-cuda") or "").strip()
    if workload == "q2rtx":
        return True, False
    if workload == "cuda":
        return False, True
    return True, True


def _stability_workload_label(*, include_q2rtx: bool, include_cuda: bool) -> str:
    if include_q2rtx and include_cuda:
        return "Q2RTX timedemo + CUDA compute"
    if include_q2rtx:
        return "Q2RTX timedemo"
    if include_cuda:
        return "CUDA compute"
    return "none"


def _stability_workload_split_label(
    total_duration_s: int,
    *,
    include_q2rtx: bool,
    include_cuda: bool,
) -> str:
    q2rtx_duration_s, cuda_duration_s = long_stability_workload_durations(
        int(total_duration_s),
        include_q2rtx=bool(include_q2rtx),
        include_cuda=bool(include_cuda),
    )
    parts = []
    if include_q2rtx:
        parts.append(f"q2rtx={int(q2rtx_duration_s)}s")
    if include_cuda:
        parts.append(f"cuda={int(cuda_duration_s)}s")
    return " ".join(parts)


def run_stability_test(args, *, gpu_index, config_path):
    include_q2rtx, include_cuda = _stability_workload_selection(args)
    total_duration_s = int(args.stability_seconds)
    split_label = _stability_workload_split_label(
        total_duration_s,
        include_q2rtx=include_q2rtx,
        include_cuda=include_cuda,
    )
    log(
        "Stability workload split: "
        f"{split_label}."
    )
    stability_config = (
        build_stability_config(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
        )
        if include_q2rtx
        else build_cuda_stability_config(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
        )
    )
    stability_config = build_long_stability_test_config(
        stability_config,
        total_duration_s=total_duration_s,
        include_q2rtx=include_q2rtx,
        include_cuda=include_cuda,
    )
    attach_stdout_progress(stability_config)
    try:
        result = (
            run_q2rtx_stability_test(stability_config)
            if include_q2rtx
            else run_cuda_stability_test(stability_config)
        )
    except StabilityTestError as exc:
        raise NvmlError(f"stability test configuration error: {exc}") from exc

    print_q2rtx_stability_result(result)
    if not result.success:
        raise NvmlError(
            f"stability test failed: {result.reason}; log={result.log_path}"
        )


def run_profile_reverification(
    args,
    *,
    gpu_index,
    config_path,
    afterburner_runtime_options,
):
    selector = str(args.auto_uv_profile or "").strip()
    prefer_afterburner_curve = bool(args.prefer_afterburner_curve)
    if not selector and not prefer_afterburner_curve:
        run_stability_test(args, gpu_index=gpu_index, config_path=config_path)
        return

    stop_existing_penguin_burner_runtime(log=log)
    vf_curve_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    if vf_curve_reader is None:
        raise NvmlError("could not open the live Nvidia V/F curve reader")

    gpu_policy_controller = None
    clock_ceiling_controller = None
    backup_path = None
    try:
        try:
            gpu_policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
        except Exception as exc:
            log(f"Linux GPU policy helper unavailable: {exc}")
        backup_file = tempfile.NamedTemporaryFile(
            prefix="penguin-burner-reverify-", suffix=".json", delete=False
        )
        backup_file.close()
        backup_path = Path(backup_file.name)
        backup_current_offsets(
            vf_curve_reader,
            backup_path,
            policy_controller=gpu_policy_controller,
        )

        if prefer_afterburner_curve:
            label, flatten_target, reverify_plan = _apply_reverify_afterburner_profile(
                vf_curve_reader,
                gpu_policy_controller,
                afterburner_runtime_options,
                gpu_index=gpu_index,
            )
        else:
            label, flatten_target, reverify_plan = _apply_reverify_auto_uv_profile(
                vf_curve_reader,
                selector,
                gpu_policy_controller,
            )

        if flatten_target is not None and gpu_policy_controller is not None:
            try:
                clock_ceiling_controller = FlattenedClockCeilingController(
                    flatten_target=flatten_target,
                    policy_controller=gpu_policy_controller,
                    exact_lock=True,
                )
                clock_ceiling_controller.apply()
            except Exception as exc:
                clock_ceiling_controller = None
                log(f"Skipping re-verification clock lock: {exc}")
            else:
                log(
                    "Configured re-verification clock lock: "
                    f"{clock_ceiling_controller.describe()}."
                )
                _apply_and_verify_reverify_vf_plan(
                    vf_curve_reader,
                    reverify_plan,
                    context="selected profile after clock lock",
                )
                log(
                    "Re-applied profile V/F curve after re-verification clock lock: "
                    f"points={len(reverify_plan)}."
                )

        duration_s = int(args.stability_seconds)
        include_q2rtx, include_cuda = _stability_workload_selection(args)
        workload_label = _stability_workload_label(
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        split_label = _stability_workload_split_label(
            duration_s,
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        log(
            "Profile re-verification workload split: "
            f"{split_label}."
        )
        log(
            "Profile re-verification starting: "
            f"profile={label} duration={duration_s}s workload={workload_label}."
        )
        stability_config = (
            build_stability_config(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
                progress_context="Profile re-verification",
            )
            if include_q2rtx
            else build_cuda_stability_config(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
            )
        )
        stability_config = build_long_stability_test_config(
            stability_config,
            total_duration_s=duration_s,
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        if flatten_target is not None:
            stability_config.abort_callback = (
                _profile_verification_voltage_abort_callback(
                    flatten_target,
                    previous_callback=stability_config.abort_callback,
                )
            )
        stop_request_path = _stability_stop_request_path(args)
        if stop_request_path is not None:
            stability_config.abort_callback = _stability_stop_request_abort_callback(
                stop_request_path,
                previous_callback=stability_config.abort_callback,
            )
        attach_stdout_progress(stability_config)
        try:
            result = (
                run_q2rtx_stability_test(stability_config)
                if include_q2rtx
                else run_cuda_stability_test(stability_config)
            )
        except StabilityTestError as exc:
            raise NvmlError(f"stability test configuration error: {exc}") from exc
        print_q2rtx_stability_result(result)
        if not result.success:
            if (
                not prefer_afterburner_curve
                and _profile_verification_failure_blocks_apply(result.reason)
            ):
                try:
                    failed_path = mark_auto_uv_profile_verification_failed(
                        selector,
                        failure={
                            "reason": result.reason,
                            "log_path": str(result.log_path),
                            "workload": workload_label,
                            "fatal_output_matches": list(
                                getattr(result, "fatal_output_matches", []) or []
                            ),
                        },
                    )
                    if failed_path is not None:
                        log(
                            "Marked profile verification failed: "
                            f"path={failed_path} reason={result.reason}"
                        )
                except Exception as exc:
                    log(f"Warning: failed to mark profile verification failed: {exc}")
            raise NvmlError(
                f"profile re-verification failed: {result.reason}; log={result.log_path}"
            )
        base_metrics = None
        if not prefer_afterburner_curve and _profile_needs_reverify_baseline(selector):
            if clock_ceiling_controller is not None:
                try:
                    clock_ceiling_controller.close()
                except Exception as exc:
                    log(
                        "Warning: failed to reset re-verification clock lock before "
                        f"baseline probe: {exc}"
                    )
                clock_ceiling_controller = None
            try:
                base_plan = _base_vf_plan_from_profile_plan(reverify_plan)
            except Exception as exc:
                log(f"Profile re-verification baseline probe skipped: {exc}")
            else:
                base_metrics = _run_profile_reverification_baseline_probe(
                    args,
                    gpu_index=gpu_index,
                    config_path=config_path,
                    base_plan=base_plan,
                    gpu_policy_controller=gpu_policy_controller,
                    duration_s=_profile_reverification_baseline_duration_s(duration_s),
                    include_q2rtx=include_q2rtx,
                    include_cuda=include_cuda,
                )
        if not prefer_afterburner_curve:
            verified_path = mark_auto_uv_profile_verified(
                selector,
                verification={
                    "workload": workload_label,
                    "duration_s": duration_s,
                    "result_reason": result.reason,
                    "log_path": str(result.log_path),
                    "target_clock_mhz": (
                        flatten_target.get("lock_clock_mhz")
                        if isinstance(flatten_target, dict)
                        else None
                    ),
                    "target_voltage_mv": (
                        flatten_target.get("lock_voltage_mv")
                        if isinstance(flatten_target, dict)
                        else None
                    ),
                },
                metrics=_profile_verification_metrics_from_result(result),
                base_metrics=base_metrics,
            )
            log(f"Marked profile verified: path={verified_path}")
        log(f"Profile re-verification passed: profile={label}.")
    finally:
        if clock_ceiling_controller is not None:
            try:
                clock_ceiling_controller.close()
            except Exception as exc:
                log(f"Warning: failed to reset re-verification clock lock: {exc}")
        if backup_path is not None:
            try:
                restore_offsets(
                    vf_curve_reader,
                    backup_path,
                    policy_controller=gpu_policy_controller,
                )
                log("Restored V/F offsets after profile re-verification.")
            except Exception as exc:
                log(f"Warning: failed to restore V/F offsets after re-verification: {exc}")
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
        if gpu_policy_controller is not None:
            gpu_policy_controller.close()
        vf_curve_reader.close()


def _apply_reverify_auto_uv_profile(vf_curve_reader, selector: str, gpu_policy_controller):
    auto_uv_final_curve = load_auto_uv_final_curve(selector, allow_unverified=True)
    if auto_uv_final_curve is None:
        raise NvmlError("Auto-UV profile not found")
    _apply_and_verify_reverify_vf_plan(
        vf_curve_reader,
        auto_uv_final_curve["plan"],
        context="selected profile",
    )
    label = (
        f"auto-UV:{auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV"
    )
    log(
        "Applied profile for re-verification: "
        f"path={auto_uv_final_curve['path']} "
        f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV "
        f"points={len(auto_uv_final_curve['plan'])}."
    )
    memory_policy = _apply_auto_uv_profile_memory_offset(
        profile_label=label,
        memory_offset_mhz=auto_uv_final_curve.get("memory_offset_mhz"),
        gpu_policy_controller=gpu_policy_controller,
    )
    if memory_policy:
        log(
            "Applied profile memory offset for re-verification: "
            f"{int(memory_policy['mem_clk_vf_offset_mhz']):+d}MHz."
        )
    return label, auto_uv_final_curve["flatten_target"], auto_uv_final_curve["plan"]


def _apply_and_verify_reverify_vf_plan(
    vf_curve_reader,
    plan: list[dict],
    *,
    context: str,
) -> None:
    apply_plan(vf_curve_reader, plan)
    vf_curve_reader.refresh_points()
    expected_samples = select_expected_vf_samples(plan)
    vf_mismatches = detect_vf_curve_reset(vf_curve_reader, expected_samples)
    if vf_mismatches:
        mismatch_preview = format_vf_curve_mismatch_preview(vf_mismatches)
        raise NvmlError(
            f"Profile verification V/F curve did not match the {context} "
            f"after apply: mismatches={len(vf_mismatches)} "
            f"samples={mismatch_preview}"
        )


def _profile_needs_reverify_baseline(selector: str) -> bool:
    try:
        resolved = resolve_auto_uv_profile(str(selector), allow_unverified=True)
    except Exception:
        return False
    if resolved is None:
        return False
    _path, profile = resolved
    if str(profile.get("profile_source", "")).strip() != "user-edited":
        return False
    return any(
        profile.get(key) in (None, "")
        for key in (
            "base_avg_core_clock_mhz",
            "base_avg_fps",
            "base_avg_power_w",
            "base_efficiency_fps_per_w",
        )
    )


def _profile_verification_failure_blocks_apply(reason: str) -> bool:
    text = str(reason or "").strip()
    if not text:
        return False
    if text.startswith("user-stop-requested"):
        return False
    return True


def _profile_reverification_baseline_duration_s(duration_s: int) -> int:
    return max(
        int(PROFILE_VERIFY_BASELINE_MIN_DURATION_S),
        min(
            int(PROFILE_VERIFY_BASELINE_DURATION_S),
            max(1, int(duration_s)) // 10,
        ),
    )


def _base_vf_plan_from_profile_plan(plan: list[dict]) -> list[dict]:
    base_plan = []
    for raw in list(plan or []):
        item = dict(raw)
        try:
            base_mhz = int(round(float(item["base_mhz"])))
            item["target_mhz"] = int(base_mhz)
            item["new_offset_mhz"] = 0
        except (KeyError, TypeError, ValueError) as exc:
            raise NvmlError(f"profile baseline V/F point is invalid: {raw}") from exc
        base_plan.append(item)
    if not base_plan:
        raise NvmlError("profile baseline V/F plan is empty")
    return base_plan


def _run_profile_reverification_baseline_probe(
    args,
    *,
    gpu_index,
    config_path,
    base_plan: list[dict],
    gpu_policy_controller,
    duration_s: int,
    include_q2rtx: bool,
    include_cuda: bool,
) -> dict | None:
    try:
        log(
            "Profile re-verification baseline probe starting: "
            f"duration={int(duration_s)}s "
            f"{_stability_workload_split_label(duration_s, include_q2rtx=include_q2rtx, include_cuda=include_cuda)}."
        )
        baseline_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
        if baseline_reader is None:
            raise NvmlError("could not open the live Nvidia V/F curve reader")
        try:
            _apply_and_verify_reverify_vf_plan(
                baseline_reader,
                base_plan,
                context="baseline profile",
            )
        finally:
            baseline_reader.close()
        if gpu_policy_controller is not None:
            try:
                gpu_policy_controller.apply_clock_offsets(mem_clk_vf_offset_mhz=0)
            except Exception as exc:
                log(f"Warning: failed to reset memory offset for baseline probe: {exc}")
        stability_config = (
            build_stability_config(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
                duration_override=int(duration_s),
                progress_context="Profile baseline",
            )
            if include_q2rtx
            else build_cuda_stability_config(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
            )
        )
        stability_config = build_long_stability_test_config(
            stability_config,
            total_duration_s=int(duration_s),
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        stop_request_path = _stability_stop_request_path(args)
        if stop_request_path is not None:
            stability_config.abort_callback = _stability_stop_request_abort_callback(
                stop_request_path,
                previous_callback=stability_config.abort_callback,
            )
        attach_stdout_progress(stability_config)
        result = (
            run_q2rtx_stability_test(stability_config)
            if include_q2rtx
            else run_cuda_stability_test(stability_config)
        )
        print_q2rtx_stability_result(result)
        if not result.success:
            log(
                "Profile re-verification baseline probe skipped: "
                f"{result.reason}; log={result.log_path}"
            )
            return None
        metrics = _profile_verification_metrics_from_result(result)
        log("Profile re-verification baseline probe complete.")
        return metrics
    except Exception as exc:
        log(f"Profile re-verification baseline probe skipped: {exc}")
        return None


def _profile_verification_metrics_from_result(result) -> dict:
    metrics = {}
    timedemo_runs = list(getattr(result, "timedemo_runs", []) or [])
    fps_values = [
        fps
        for fps in (_float_or_none(getattr(run, "fps", None)) for run in timedemo_runs)
        if fps is not None and fps > 0.0
    ]
    if fps_values:
        metrics["avg_fps"] = sum(fps_values) / len(fps_values)

    summary = {}
    telemetry_summary = getattr(result, "telemetry_summary", None)
    if callable(telemetry_summary):
        try:
            summary = telemetry_summary()
        except Exception:
            summary = {}
    if isinstance(summary, dict):
        field_map = {
            "core_clock_avg": "avg_core_clock_mhz",
            "power_avg": "avg_power_w",
            "power_max": "max_power_w",
            "voltage_avg": "avg_voltage_mv",
            "voltage_max": "max_voltage_mv",
            "temperature_avg": "avg_temperature_c",
            "temperature_max": "max_temperature_c",
            "fan_avg": "avg_fan_speed_pct",
            "fan_max": "max_fan_speed_pct",
        }
        for source_key, target_key in field_map.items():
            value = _float_or_none(summary.get(source_key))
            if value is not None:
                metrics[target_key] = value

    avg_fps = _float_or_none(metrics.get("avg_fps"))
    avg_power_w = _float_or_none(metrics.get("avg_power_w"))
    avg_core_clock_mhz = _float_or_none(metrics.get("avg_core_clock_mhz"))
    if avg_fps is not None and avg_power_w is not None and avg_power_w > 0.0:
        metrics["efficiency_fps_per_w"] = avg_fps / avg_power_w
    if (
        avg_core_clock_mhz is not None
        and avg_power_w is not None
        and avg_power_w > 0.0
    ):
        metrics["efficiency_mhz_per_w"] = avg_core_clock_mhz / avg_power_w
    if (
        avg_power_w is not None
        and avg_core_clock_mhz is not None
        and avg_core_clock_mhz > 0.0
    ):
        metrics["watts_per_mhz"] = avg_power_w / avg_core_clock_mhz
    return metrics


def _stability_stop_request_path(args) -> Path | None:
    text = str(getattr(args, "stability_stop_request_file", "") or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


def _stability_stop_request_abort_callback(
    stop_request_path: Path,
    *,
    previous_callback=None,
):
    def _abort_callback(state: dict) -> str | None:
        if previous_callback is not None:
            reason = previous_callback(state)
            if reason:
                return str(reason)
        try:
            if Path(stop_request_path).exists():
                return "user-stop-requested"
        except OSError:
            return None
        return None

    return _abort_callback


def _profile_verification_voltage_abort_callback(
    flatten_target: dict,
    *,
    previous_callback=None,
):
    target_voltage_mv = _coerce_positive_int(
        dict(flatten_target).get("lock_voltage_mv")
    )
    if target_voltage_mv is None:
        return previous_callback
    tolerance_mv = int(PROFILE_VERIFY_VOLTAGE_TOLERANCE_MV)
    state = {"high_voltage_streak": 0}

    def _abort_callback(progress_state: dict) -> str | None:
        if previous_callback is not None:
            reason = previous_callback(progress_state)
            if reason:
                return str(reason)
        try:
            elapsed_s = float(
                progress_state.get(
                    "progress_elapsed_s",
                    progress_state.get("elapsed_s", 0.0),
                )
                or 0.0
            )
        except (TypeError, ValueError):
            elapsed_s = 0.0
        if elapsed_s < float(PROFILE_VERIFY_VOLTAGE_WARMUP_S):
            state["high_voltage_streak"] = 0
            return None
        sample = progress_state.get("latest_sample")
        if sample is None or getattr(sample, "voltage_mv", None) is None:
            state["high_voltage_streak"] = 0
            return None
        try:
            voltage_mv = float(sample.voltage_mv)
        except (TypeError, ValueError):
            state["high_voltage_streak"] = 0
            return None
        gpu_util_pct = getattr(sample, "gpu_util_pct", None)
        if gpu_util_pct is not None:
            try:
                if float(gpu_util_pct) < 50.0:
                    state["high_voltage_streak"] = 0
                    return None
            except (TypeError, ValueError):
                pass
        if voltage_mv <= float(target_voltage_mv + tolerance_mv):
            state["high_voltage_streak"] = 0
            return None
        state["high_voltage_streak"] = int(state["high_voltage_streak"]) + 1
        if state["high_voltage_streak"] < int(
            PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK
        ):
            return None
        return (
            "profile-verification-voltage-mismatch "
            f"current={voltage_mv:.0f}mV "
            f"target={int(target_voltage_mv)}mV "
            f"tolerance={int(tolerance_mv)}mV "
            f"streak={int(state['high_voltage_streak'])}"
        )

    return _abort_callback


def _coerce_positive_int(value) -> int | None:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float_or_none(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _apply_reverify_afterburner_profile(
    vf_curve_reader,
    gpu_policy_controller,
    afterburner_runtime_options,
    *,
    gpu_index,
):
    afterburner_root = str(
        afterburner_runtime_options.get("afterburner_root", "")
    ).strip()
    if not afterburner_root:
        raise NvmlError("Afterburner profile is not configured")
    section = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
    device_profile = str(
        afterburner_runtime_options.get("afterburner_device_profile", "")
    ).strip()
    source = resolve_afterburner_vf_source(
        afterburner_root=afterburner_root,
        section=section or None,
        device_profile_hint=device_profile or None,
        dangerously_skip_validation=bool(
            afterburner_runtime_options.get("dangerously_skip_validation")
        ),
    )
    translated_gpu_policy = None
    if gpu_policy_controller is not None:
        try:
            profile_settings = load_afterburner_profile_settings(
                profile_path=source["profile_path"],
                section=source["section"],
            )
            translated_gpu_policy = translate_afterburner_gpu_policy(
                profile_settings,
                power_limits=gpu_policy_controller.query_power_limits(),
                power_limit_cap_w=afterburner_runtime_options[
                    "power_limit_override_w"
                ],
            )
            apply_translated_gpu_policy(gpu_policy_controller, translated_gpu_policy)
        except Exception as exc:
            translated_gpu_policy = None
            log(f"Skipping Afterburner GPU policy during re-verification: {exc}")
    vf_apply_result = apply_afterburner_curve_to_reader(
        vf_curve_reader,
        profile_path=source["profile_path"],
        section=source["section"],
        gpu_policy=translated_gpu_policy,
        preserve_base_below_mv=afterburner_runtime_options["preserve_base_below_mv"],
    )
    vf_curve_reader.refresh_points()
    flatten_target = derive_afterburner_dynamic_lock(
        vf_apply_result["materialization"]["points"]
    )
    label = f"afterburner:{source['section']}"
    log(
        "Applied Afterburner profile for re-verification: "
        f"section={source['section']} matched={len(vf_apply_result['plan'])} "
        f"changed={len(vf_apply_result['changed_points'])} "
        f"gpu-index={int(gpu_index)}."
    )
    return label, flatten_target, vf_apply_result["plan"]


def run_q2rtx_install():
    try:
        result = install_latest_q2rtx()
    except StabilityTestError as exc:
        raise NvmlError(f"Q2RTX install failed: {exc}") from exc

    print(f"Installed Q2RTX {result.version} to {result.install_dir}", flush=True)
    print(f"Executable: {result.executable_path}", flush=True)
    print(f"Archive cache: {result.archive_path}", flush=True)
    print(f"Source: {result.asset_url}", flush=True)


def apply_hysteresis(
    current_temp_c, raw_target_speed, last_temp_c, last_speed, hysteresis_c
):
    if last_temp_c is None or last_speed is None or hysteresis_c <= 0.0:
        return raw_target_speed

    if raw_target_speed >= last_speed:
        return raw_target_speed

    if current_temp_c > last_temp_c:
        return raw_target_speed

    if (last_temp_c - current_temp_c) < hysteresis_c:
        return float(last_speed)

    return raw_target_speed


def limit_speed_change(
    target_speed, last_speed, elapsed_s, max_step_up_pct_per_s, max_step_down_pct_per_s
):
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
        rc = nvml.nvmlDeviceGetFanSpeed_v2(
            device, ctypes.c_uint(fan_idx), ctypes.byref(speed)
        )
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


def get_core_clock_mhz(nvml, device):
    clock_mhz = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetClockInfo(
        device, ctypes.c_uint(NVML_CLOCK_GRAPHICS), ctypes.byref(clock_mhz)
    )
    if rc != NVML_SUCCESS:
        return None
    return int(clock_mhz.value)


def get_memory_clock_mhz(nvml, device):
    clock_mhz = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetClockInfo(
        device, ctypes.c_uint(NVML_CLOCK_MEM), ctypes.byref(clock_mhz)
    )
    if rc != NVML_SUCCESS:
        return None
    return int(clock_mhz.value)


def format_vf_curve_comparison(vf_curve_reader, core_clock_mhz, voltage_uv):
    point = vf_curve_reader.find_nearest_point(core_clock_mhz, voltage_uv)
    if point is None:
        return ""

    point_freq_mhz = int(point["freq_khz"] // 1000)
    point_voltage_mv = int(point["voltage_uv"] // 1000)
    point_offset_mhz = int(point["current_offset_khz"] // 1000)

    base_point = min(
        vf_curve_reader.editable_core_points(),
        key=lambda candidate: abs(
            int(candidate["base_freq_khz"]) - int(core_clock_mhz) * 1000
        ),
    )
    base_clock_mhz = int(base_point["base_freq_khz"] // 1000)
    base_voltage_mv = int(base_point["voltage_uv"] // 1000)
    uv_delta_mv = int(point_voltage_mv - base_voltage_mv)

    return (
        f"vf_point={point_freq_mhz}MHz@{point_voltage_mv}mV "
        f"vf_offset={point_offset_mhz:+d}MHz "
        f"vf_base={base_clock_mhz}MHz@{base_voltage_mv}mV "
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

    core_clock_mhz = get_core_clock_mhz(nvml, device)
    if core_clock_mhz is None:
        clock_text = "n/a"
    else:
        clock_text = f"{core_clock_mhz}MHz"

    memory_clock_mhz = get_memory_clock_mhz(nvml, device)
    if memory_clock_mhz is None:
        mem_clock_text = "n/a"
    else:
        mem_clock_text = f"{memory_clock_mhz}MHz"

    voltage_uv = None
    if voltage_reader is not None:
        try:
            voltage_uv = voltage_reader.read_microvolts(device)
        except Exception:
            voltage_uv = None
    if voltage_uv is None:
        voltage_text = "n/a"
    else:
        voltage_text = f"{voltage_uv / 1000.0:.0f}mV"

    clock_offset_text = format_clock_offsets(gpu_policy_controller)
    clock_ceiling_text = format_clock_ceiling_state(clock_ceiling_controller)
    vf_point_text = ""
    if (
        vf_curve_reader is not None
        and core_clock_mhz is not None
        and voltage_uv is not None
    ):
        try:
            vf_curve_reader.refresh_points()
        except Exception:
            pass
        vf_point_text = format_vf_curve_comparison(
            vf_curve_reader,
            core_clock_mhz,
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
            encoding="utf-8",
            errors="replace",
            check=False,
            env=stable_subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise NvmlError(f"{NVIDIA_SMI} not found") from exc

    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0:
        raise NvmlError(
            f"nvidia-smi {' '.join(args)} failed: {output or result.returncode}"
        )

    return output


class FlattenedClockCeilingController:
    def __init__(self, flatten_target, policy_controller, *, exact_lock=False):
        if not flatten_target:
            raise ValueError("flatten_target is required")
        if policy_controller is None:
            raise ValueError("policy_controller is required")

        self._flatten_target = dict(flatten_target)
        self._policy_controller = policy_controller
        self._exact_lock = bool(exact_lock)
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

        clock_text = (
            f"{self.requested_max_clock_mhz}MHz"
            if self.requested_max_clock_mhz == self.applied_max_clock_mhz
            else f"{self.requested_max_clock_mhz}->{self.applied_max_clock_mhz}MHz"
        )
        voltage_mv = self.target_voltage_mv
        if voltage_mv is not None:
            clock_text += f"@{voltage_mv}mV"
        field = "clk_lock" if self._exact_lock else "clk_ceiling"
        return f"{field}={clock_text} "

    def describe(self):
        snap_text = ""
        if (
            self._range_lock is not None
            and self.requested_max_clock_mhz != self.applied_max_clock_mhz
        ):
            snap_text = (
                f", supported-clock={self.applied_max_clock_mhz}MHz"
                f" ({self._range_lock['max_mode']})"
            )
        min_text = (
            f", min-step={self.applied_min_clock_mhz}MHz"
            if self._range_lock is not None and not self._exact_lock
            else ""
        )
        policy_text = "lock" if self._exact_lock else "ceiling"
        return (
            f"{describe_afterburner_dynamic_lock(self._flatten_target)}, "
            f"{policy_text}={self.requested_max_clock_mhz}MHz"
            f"{snap_text}{min_text}"
        )

    def apply(self):
        if self._exact_lock:
            snap = self._policy_controller.apply_locked_core_clock_mhz(
                self.target_clock_mhz,
                prefer_not_above=True,
                snap_to_supported=True,
            )
            applied_clock_mhz = int(snap["applied_clock_mhz"])
            self._range_lock = {
                "requested_min_clock_mhz": int(self.target_clock_mhz),
                "requested_max_clock_mhz": int(self.target_clock_mhz),
                "applied_min_clock_mhz": applied_clock_mhz,
                "applied_max_clock_mhz": applied_clock_mhz,
                "min_mode": str(snap["mode"]),
                "max_mode": str(snap["mode"]),
                "supported_steps_mhz": list(snap.get("supported_steps_mhz") or []),
            }
            self._active = True
            return dict(self._range_lock)

        supported_steps = self._policy_controller.get_supported_core_clock_steps_mhz()
        requested_min_clock_mhz = (
            supported_steps[0] if supported_steps else self.target_clock_mhz
        )
        self._range_lock = self._policy_controller.apply_locked_core_clock_range_mhz(
            requested_min_clock_mhz,
            self.target_clock_mhz,
            prefer_max_not_above=True,
            snap_to_supported=True,
        )
        self._active = True
        return dict(self._range_lock)

    def close(self):
        if self._active:
            self._policy_controller.reset_locked_core_clocks()
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


def load_runtime_afterburner_fan_config(
    current_fan_config, *, afterburner_root, gpu_index
):
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
        raise NvmlError(
            "Afterburner software auto fan control is disabled in the imported profile"
        )

    try:
        return build_imported_fan_section(
            current_fan_config,
            settings,
            gpu_index=gpu_index,
        )
    except SystemExit as exc:
        raise NvmlError(str(exc)) from None


def export_lact_config(
    *,
    args,
    fan_config,
    gpu_index,
    afterburner_runtime_options,
) -> None:
    output_path = Path(args.export_lact_config)
    include_vf_curve = not bool(args.fan_curve_export)
    include_fan_curve = bool(args.fan_curve_export or args.silent_fan_curve)
    try:
        if args.lact_source == "auto-uv":
            written_path, warnings = write_lact_nvidia_config(
                output_path=output_path,
                gpu_id=str(args.lact_gpu_id),
                include_vf_curve=include_vf_curve,
                include_fan_curve=include_fan_curve,
            )
        else:
            afterburner_root = str(
                afterburner_runtime_options.get("afterburner_root", "")
            ).strip()
            written_path, warnings = write_lact_nvidia_config_from_afterburner(
                output_path=output_path,
                gpu_id=str(args.lact_gpu_id),
                current_fan_config=fan_config,
                gpu_index=gpu_index,
                afterburner_root=afterburner_root,
                section=afterburner_runtime_options.get("afterburner_profile") or None,
                device_profile_hint=afterburner_runtime_options.get(
                    "afterburner_device_profile"
                )
                or None,
                dangerously_skip_validation=bool(
                    afterburner_runtime_options.get("dangerously_skip_validation")
                ),
                preserve_base_below_mv=afterburner_runtime_options.get(
                    "preserve_base_below_mv"
                ),
                include_vf_curve=include_vf_curve,
                include_fan_curve=include_fan_curve,
            )
    except LactExportError as exc:
        raise NvmlError(str(exc)) from exc

    log(f"LACT Nvidia config written: {written_path}")
    for warning in warnings:
        log(f"LACT export warning: {warning}")
    log(
        "Apply deliberately, for example: "
        f"sudo install -m 0644 {written_path} /etc/lact/config.yaml"
    )


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
    log(
        "First-time Afterburner import detected. Running a dry run before touching GPU state."
    )
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
        configured_section = str(
            afterburner_runtime_options.get("afterburner_profile", "")
        ).strip()
        section_example = configured_section or "<section>"
        log(f"`{script_path} --dry-run --section {section_example}`")
        return True
    try:
        source = resolve_afterburner_vf_source(
            afterburner_root=afterburner_runtime_options.get("afterburner_root")
            or None,
            section=afterburner_runtime_options.get("afterburner_profile") or None,
            device_profile_hint=afterburner_runtime_options.get(
                "afterburner_device_profile"
            )
            or None,
            dangerously_skip_validation=bool(
                afterburner_runtime_options.get("dangerously_skip_validation")
            ),
        )
    except Exception as exc:
        log(
            f"Warning: dry run succeeded but failed to persist the selected source: {exc}"
        )
    else:
        afterburner_runtime_options["afterburner_root"] = str(
            source["afterburner_root"]
        )
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
    if prompt_yes_no(
        "Start PenguinBurner in foreground now for testing?", default=True
    ):
        return False

    if systemd_is_available():
        if prompt_yes_no(
            "Daemonize PenguinBurner under systemd now instead?", default=False
        ):
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
    explicit_cli_args = bool(argv)

    args = parse_main_args(argv)
    if args.debug_log:
        enable_debug_logging(Path(args.config).expanduser(), argv=argv)
    claim_desktop_user_ownership(
        default_user_config_dir(),
        recursive=True,
        include_parents=True,
    )
    claim_desktop_user_ownership(
        default_saved_uv_dir(),
        recursive=True,
        include_parents=True,
    )
    if args.clear_auto_uv_state and args.fresh_auto_uv_scan:
        raise NvmlError(
            "choose only one of --clear-auto-uv-state or --fresh-auto-uv-scan"
        )
    if args.fresh_auto_uv_scan:
        clear_auto_uv_state(log=log)
        args.auto_uv_voltage_scan = True
    elif args.clear_auto_uv_state:
        clear_auto_uv_state(log=log)
        return
    if args.list_auto_uv_profiles:
        profiles = read_auto_uv_profile_summaries()
        if args.json_events:
            print(json.dumps({"profiles": profiles}, indent=2), flush=True)
        else:
            print(format_profile_table(profiles), flush=True)
        return
    if args.delete_auto_uv_profiles:
        deleted = delete_auto_uv_profiles(args.delete_auto_uv_profiles)
        payload = {
            "deleted": [str(path) for path in deleted],
            "deleted_count": len(deleted),
        }
        if args.json_events:
            print(json.dumps(payload, indent=2), flush=True)
        else:
            label = "profile" if len(deleted) == 1 else "profiles"
            print(f"Deleted {len(deleted)} Auto-UV {label}.", flush=True)
        return
    if args.install_q2rtx:
        run_q2rtx_install()
        return
    config, config_path = load_config(args.config)
    gpu_config = config["gpu"]
    fan_config = config["fan"]
    if args.gpu_index is not None:
        gpu_config["index"] = int(args.gpu_index)
    gpu_index = int(gpu_config["index"])
    if args.stability_test and not (
        str(args.auto_uv_profile or "").strip() or bool(args.prefer_afterburner_curve)
    ):
        run_stability_test(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
        )
        return
    stored_afterburner_runtime_options = load_afterburner_runtime_options(config_path)
    had_persisted_afterburner_root = bool(
        str(stored_afterburner_runtime_options.get("afterburner_root", "")).strip()
    )
    has_usable_persisted_afterburner_import = afterburner_root_has_imported_profiles(
        stored_afterburner_runtime_options.get("afterburner_root", "")
    )
    auto_uv_profile_selector = str(args.auto_uv_profile or "").strip()
    auto_uv_final_curve_available = False
    try:
        auto_uv_final_curve_available = (
            load_auto_uv_final_curve(auto_uv_profile_selector) is not None
        )
    except Exception:
        auto_uv_final_curve_available = False
    default_auto_uv_started = False
    if (
        not explicit_cli_args
        and not running_under_systemd_service()
        and not auto_uv_final_curve_available
        and not has_usable_persisted_afterburner_import
    ):
        args.auto_uv_voltage_scan = True
        default_auto_uv_started = True
    if args.auto_uv_require_final_choice and not args.json_events:
        raise NvmlError("--auto-uv-require-final-choice requires --json-events")
    if args.auto_uv_voltage_scan:
        capture_path = enable_stdio_capture(
            config_path,
            argv=argv or ["--auto-uv-voltage-scan"],
            label="auto-uv-stdout",
        )
        if capture_path is not None:
            log(f"Auto-UV stdout/stderr log: {capture_path}")
        if default_auto_uv_started:
            log(
                "No saved Auto-UV curve or usable Afterburner import found; "
                "starting the default foreground Auto-UV scan."
            )
    if args.auto_uv_voltage_scan and args.restore_defaults_from_config:
        raise NvmlError(
            "choose only one of --auto-uv-voltage-scan or --restore-defaults-from-config"
        )
    if args.auto_uv_voltage_scan and running_under_systemd_service():
        raise NvmlError(
            "Auto-UV scans are foreground-only; run the scan directly first, "
            "then daemonize normal runtime after the final curve is saved"
        )
    if args.auto_uv_voltage_scan and args.silent_fan_curve:
        log(
            "Auto-UV note: --silent-fan-curve is a normal runtime/daemon option. "
            "The scan will still save a suggested fan curve automatically when safe, "
            "but it will not take over fan control during the scan."
        )
    if args.auto_uv_voltage_scan:
        stop_existing_penguin_burner_runtime(log=log)
    afterburner_runtime_options = dict(stored_afterburner_runtime_options)
    if args.afterburner_dir.strip():
        afterburner_runtime_options["afterburner_root"] = str(
            resolve_afterburner_root(args.afterburner_dir)
        )
    if args.profile_section.strip():
        afterburner_runtime_options["afterburner_profile"] = str(
            args.profile_section
        ).strip()
    if args.afterburner_device_profile.strip():
        afterburner_runtime_options["afterburner_device_profile"] = str(
            args.afterburner_device_profile
        ).strip()
    if args.power_limit_override_w is not None:
        afterburner_runtime_options["power_limit_override_w"] = (
            int(args.power_limit_override_w)
            if int(args.power_limit_override_w) > 0
            else None
        )
    if args.preserve_base_below_mv is not None:
        afterburner_runtime_options["preserve_base_below_mv"] = (
            int(args.preserve_base_below_mv)
            if int(args.preserve_base_below_mv) > 0
            else None
        )
    if args.auto_uv_max_drop_pct is not None:
        afterburner_runtime_options["auto_uv_max_drop_pct"] = (
            float(args.auto_uv_max_drop_pct)
            if float(args.auto_uv_max_drop_pct) > 0.0
            else None
        )
    if args.auto_uv_final_seconds is not None:
        afterburner_runtime_options["auto_uv_final_seconds"] = (
            int(args.auto_uv_final_seconds)
            if int(args.auto_uv_final_seconds) > 0
            else None
        )
    if args.auto_uv_short_seconds is not None:
        afterburner_runtime_options["auto_uv_short_seconds"] = (
            max(10, min(60, int(args.auto_uv_short_seconds)))
            if int(args.auto_uv_short_seconds) > 0
            else None
        )
    if args.auto_uv_memory_offset_mhz is not None:
        afterburner_runtime_options["auto_uv_memory_offset_mhz"] = max(
            0,
            min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(args.auto_uv_memory_offset_mhz)),
        )
    if args.auto_uv_efficiency_stop_streak is not None:
        afterburner_runtime_options["auto_uv_efficiency_stop_streak"] = max(
            0,
            int(args.auto_uv_efficiency_stop_streak),
        )
    if args.auto_uv_min_efficiency_stop_drop_pct is not None:
        afterburner_runtime_options["auto_uv_min_efficiency_stop_drop_pct"] = max(
            0.0,
            float(args.auto_uv_min_efficiency_stop_drop_pct),
        )
    if args.auto_uv_max_clock_drop_pct is not None:
        afterburner_runtime_options["auto_uv_max_clock_drop_pct"] = max(
            0.0,
            float(args.auto_uv_max_clock_drop_pct),
        )
    if args.auto_uv_clock_bump_budget_ratio is not None:
        max_clock_bump_budget_ratio = (
            AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO
            if bool(args.yolo)
            else AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO
        )
        afterburner_runtime_options["auto_uv_clock_bump_budget_ratio"] = max(
            0.0,
            min(
                float(max_clock_bump_budget_ratio),
                float(args.auto_uv_clock_bump_budget_ratio),
            ),
        )
    if args.yolo:
        afterburner_runtime_options["auto_uv_yolo"] = True
    if args.auto_uv_mode is not None:
        afterburner_runtime_options["auto_uv_mode"] = normalize_auto_uv_mode(
            args.auto_uv_mode
        )
    if args.auto_uv_require_final_choice:
        afterburner_runtime_options["auto_uv_require_final_choice"] = True
    if args.dangerously_skip_validation:
        afterburner_runtime_options["dangerously_skip_validation"] = True
    prefer_afterburner_curve = bool(args.prefer_afterburner_curve)
    debug_effective_runtime_options(
        config_path=config_path,
        gpu_index=gpu_index,
        afterburner_runtime_options=afterburner_runtime_options,
    )
    if str(args.export_lact_config).strip():
        export_lact_config(
            args=args,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return
    if args.stability_test:
        run_profile_reverification(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return
    if args.restore_defaults_from_config or args.auto_uv_voltage_scan:
        if args.restore_defaults_from_config:
            afterburner_runtime_options = ensure_afterburner_root_configured(
                config_path,
                afterburner_runtime_options,
                gpu_index=gpu_index,
                interactive=sys.stdin.isatty(),
            )
        try:
            if args.restore_defaults_from_config:
                restore_afterburner_defaults_from_config(
                    gpu_index=gpu_index,
                    runtime_options=afterburner_runtime_options,
                    log=log,
                )
            elif args.auto_uv_voltage_scan:
                emit_json_event(
                    bool(args.json_events),
                    "auto_uv_start",
                    gpu_index=int(gpu_index),
                )

                def _auto_uv_json_event(event, payload):
                    emit_json_event(bool(args.json_events), event, **dict(payload))

                def _dependency_json_event(payload):
                    emit_json_event(
                        bool(args.json_events),
                        "dependency_progress",
                        **dict(payload),
                    )

                try:
                    require_auto_uv_preflight(gpu_index=gpu_index, log=log)
                except RuntimeError as exc:
                    raise NvmlError(str(exc)) from exc

                result = run_auto_uv_voltage_scan(
                    gpu_index=gpu_index,
                    runtime_options=afterburner_runtime_options,
                    q2rtx_config=build_stability_config(
                        args,
                        gpu_index=gpu_index,
                        config_path=config_path,
                        auto_install_q2rtx=True,
                        progress_context="Auto-UV",
                        dependency_progress_callback=(
                            _dependency_json_event
                            if bool(args.json_events)
                            else None
                        ),
                        dependency_text_progress=not bool(args.json_events),
                    ),
                    log=log,
                    event_callback=_auto_uv_json_event
                    if bool(args.json_events)
                    else None,
                )
                emit_json_event(
                    bool(args.json_events),
                    "final_result",
                    voltage_mv=int(result.final_voltage_mv),
                    clock_mhz=int(result.lock_clock_mhz),
                    power_w=result.final_power_w,
                    temperature_c=result.final_temperature_c,
                    fan_pct=result.final_fan_speed_pct,
                    stop_reason=result.stop_reason,
                    failed_candidate_voltage_mv=result.failed_candidate_voltage_mv,
                )
                log(
                    "Auto-UV final state: "
                    f"{result.lock_clock_mhz}MHz@{result.final_voltage_mv}mV "
                    f"power={result.final_power_w if result.final_power_w is not None else 'n/a'}W "
                    f"temp={result.final_temperature_c if result.final_temperature_c is not None else 'n/a'}C "
                    f"fan={result.final_fan_speed_pct if result.final_fan_speed_pct is not None else 'n/a'}% "
                    f"stop_reason={result.stop_reason} "
                    f"failed_candidate={result.failed_candidate_voltage_mv if result.failed_candidate_voltage_mv is not None else 'none'}"
                )
        except AutoUvFinalChoiceDiscarded as exc:
            log(str(exc))
        except AutoUvError as exc:
            raise NvmlError(str(exc)) from exc
        return
    if args.dry_run:
        run_afterburner_dry_run(
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return

    fan_control_enabled = bool(args.silent_fan_curve)
    afterburner_root = str(
        afterburner_runtime_options.get("afterburner_root", "")
    ).strip()
    afterburner_profile = str(
        afterburner_runtime_options.get("afterburner_profile", "")
    ).strip()
    afterburner_device_profile = str(
        afterburner_runtime_options.get("afterburner_device_profile", "")
    ).strip()
    if not had_persisted_afterburner_root and not auto_uv_final_curve_available:
        afterburner_runtime_options = ensure_afterburner_root_configured(
            config_path,
            afterburner_runtime_options,
            gpu_index=gpu_index,
            interactive=sys.stdin.isatty(),
        )
        afterburner_root = str(
            afterburner_runtime_options.get("afterburner_root", "")
        ).strip()
        afterburner_profile = str(
            afterburner_runtime_options.get("afterburner_profile", "")
        ).strip()
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
    elif not afterburner_root and not auto_uv_final_curve_available:
        afterburner_runtime_options = ensure_afterburner_root_configured(
            config_path,
            afterburner_runtime_options,
            gpu_index=gpu_index,
            interactive=sys.stdin.isatty(),
        )
        afterburner_root = str(
            afterburner_runtime_options.get("afterburner_root", "")
        ).strip()
        afterburner_profile = str(
            afterburner_runtime_options.get("afterburner_profile", "")
        ).strip()
        afterburner_device_profile = str(
            afterburner_runtime_options.get("afterburner_device_profile", "")
        ).strip()
    if fan_control_enabled:
        auto_uv_fan_curve_path = default_user_config_dir() / "auto-uv-fan-curve.json"
        if auto_uv_fan_curve_path.is_file():
            try:
                auto_uv_fan_curve = load_auto_uv_fan_curve(fan_config)
            except FanCurveBlockedError as exc:
                auto_uv_fan_curve = None
                fan_control_enabled = False
                log(f"Manual fan control disabled by auto-UV safety guard: {exc}")
            except Exception as exc:
                auto_uv_fan_curve = None
                fan_control_enabled = False
                log(
                    "Manual fan control disabled because the auto-UV fan curve "
                    f"is present but invalid: path={auto_uv_fan_curve_path} error={exc}"
                )
            if fan_control_enabled and auto_uv_fan_curve is not None:
                fan_config = auto_uv_fan_curve["fan_config"]
            elif fan_control_enabled:
                fan_control_enabled = False
                log(
                    "Manual fan control disabled because the auto-UV fan curve "
                    f"file could not be loaded: path={auto_uv_fan_curve_path}"
                )
        elif afterburner_root:
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

    gpu_index = gpu_config["index"]
    poll_interval_s = fan_config["poll_interval_s"]
    curve = []
    effective_manual_curve = []
    hysteresis_c = 0.0
    mode = "linear"
    min_fan_speed_pct = 0
    max_fan_speed_pct = 100
    effective_min_fan_speed_pct = 0
    effective_max_fan_speed_pct = 100
    max_step_up_pct_per_s = 0.0
    max_step_down_pct_per_s = 0.0
    manual_enable_temp_c = 0.0
    auto_restore_temp_c = 0.0
    emergency_auto_override_temp_c = 80.0
    emergency_auto_resume_temp_c = 75.0
    force_update_every_poll = False
    if fan_control_enabled:
        nvml.nvmlDeviceSetFanSpeed_v2.argtypes = [c_void_p, c_uint, c_uint]
        nvml.nvmlDeviceSetFanSpeed_v2.restype = ctypes.c_int
        nvml.nvmlDeviceSetDefaultFanSpeed_v2.argtypes = [c_void_p, c_uint]
        nvml.nvmlDeviceSetDefaultFanSpeed_v2.restype = ctypes.c_int
        curve = [tuple(point) for point in fan_config["curve"]]
        validate_curve(curve)
        hysteresis_c = float(fan_config["hysteresis_c"])
        mode = str(fan_config["mode"])
        min_fan_speed_pct = int(fan_config["min_fan_speed_pct"])
        max_fan_speed_pct = int(fan_config["max_fan_speed_pct"])
        max_step_up_pct_per_s = float(fan_config["max_step_up_pct_per_s"])
        max_step_down_pct_per_s = float(fan_config["max_step_down_pct_per_s"])
        manual_enable_temp_c = float(fan_config["manual_enable_temp_c"])
        auto_restore_temp_c = float(fan_config["auto_restore_temp_c"])
        emergency_auto_override_temp_c = float(
            fan_config.get("emergency_auto_override_temp_c", 80.0)
        )
        emergency_auto_resume_temp_c = float(
            fan_config.get("emergency_auto_resume_temp_c", 75.0)
        )
        force_update_every_poll = bool(fan_config["force_update_every_poll"])
    enable_persistence_mode = gpu_config["enable_persistence_mode"]
    translated_gpu_policy = None
    afterburner_source = None
    afterburner_profile_settings = None
    auto_uv_final_curve = None
    vf_apply_result = None
    active_vf_curve_source = None
    auto_uv_profile_gpu_policy = None
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

    try:
        auto_uv_final_curve = load_auto_uv_final_curve(auto_uv_profile_selector)
    except Exception as exc:
        auto_uv_final_curve = None
        log(f"Skipping auto-UV final curve: error={exc}")
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
                        power_limit_cap_w=afterburner_runtime_options[
                            "power_limit_override_w"
                        ],
                    )
                except Exception as exc:
                    translated_gpu_policy = None
                    log(
                        "Skipping Afterburner GPU policy translate: "
                        f"section={afterburner_source['section']} error={exc}"
                    )

    startup_power_limit_w = None
    if (
        translated_gpu_policy is not None
        and translated_gpu_policy.get("power_limit_w") is not None
    ):
        startup_power_limit_w = translated_gpu_policy["power_limit_w"]
    apply_gpu_base_policy(
        gpu_index=gpu_index,
        enable_persistence_mode=enable_persistence_mode,
        power_limit_w=startup_power_limit_w,
    )
    if (
        translated_gpu_policy is not None
        and gpu_policy_controller is not None
        and afterburner_source is not None
    ):
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

    if vf_curve_reader is not None:
        afterburner_curve_applied = False
        auto_uv_curve_applied = False

        def _apply_auto_uv_final_curve() -> bool:
            nonlocal auto_uv_profile_gpu_policy
            nonlocal auto_uv_final_curve
            nonlocal vf_apply_result
            nonlocal vf_expected_samples
            nonlocal clock_ceiling_controller
            nonlocal active_vf_curve_source
            if auto_uv_final_curve is None:
                return False
            try:
                apply_plan(vf_curve_reader, auto_uv_final_curve["plan"])
                vf_curve_reader.refresh_points()
            except Exception as exc:
                log(
                    "Skipping auto-UV final curve apply: "
                    f"path={auto_uv_final_curve['path']} error={exc}"
                )
                auto_uv_final_curve = None
                return False
            else:
                vf_apply_result = {
                    "source": "auto-uv-final",
                    "plan": auto_uv_final_curve["plan"],
                    "path": auto_uv_final_curve["path"],
                }
                active_vf_curve_source = "auto-uv-final"
                vf_expected_samples = select_expected_vf_samples(
                    vf_apply_result["plan"]
                )
                log(
                    "Applied auto-UV final curve: "
                    f"path={auto_uv_final_curve['path']} "
                    f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
                    f"{auto_uv_final_curve['candidate_voltage_mv']}mV "
                    f"points={len(auto_uv_final_curve['plan'])}."
                )
                auto_uv_profile_gpu_policy = _apply_auto_uv_profile_memory_offset(
                    profile_label="auto-UV final curve",
                    memory_offset_mhz=auto_uv_final_curve.get("memory_offset_mhz"),
                    gpu_policy_controller=gpu_policy_controller,
                )
                if auto_uv_profile_gpu_policy:
                    log(
                        "Applied auto-UV profile memory offset: "
                        f"{int(auto_uv_profile_gpu_policy['mem_clk_vf_offset_mhz']):+d}MHz."
                    )
                try:
                    clock_ceiling_controller = FlattenedClockCeilingController(
                        flatten_target=auto_uv_final_curve["flatten_target"],
                        policy_controller=gpu_policy_controller,
                    )
                    clock_ceiling_controller.apply()
                except Exception as exc:
                    clock_ceiling_controller = None
                    log(
                        "Skipping auto-UV clock ceiling: "
                        f"path={auto_uv_final_curve['path']} error={exc}"
                    )
                else:
                    log(
                        "Configured auto-UV clock ceiling: "
                        f"{clock_ceiling_controller.describe()}."
                    )
                return True

        def _apply_afterburner_curve() -> bool:
            nonlocal vf_apply_result
            nonlocal vf_expected_samples
            nonlocal clock_ceiling_controller
            nonlocal active_vf_curve_source
            if afterburner_source is None:
                return False
            try:
                vf_apply_result = apply_afterburner_curve_to_reader(
                    vf_curve_reader,
                    profile_path=afterburner_source["profile_path"],
                    section=afterburner_source["section"],
                    gpu_policy=translated_gpu_policy,
                    preserve_base_below_mv=afterburner_runtime_options[
                        "preserve_base_below_mv"
                    ],
                )
            except Exception as exc:
                log(
                    "Skipping Afterburner VF curve apply: "
                    f"section={afterburner_source['section']} error={exc}"
                )
                return False
            else:
                log(
                    f"Applied Afterburner VF curve: section={afterburner_source['section']} "
                    f"matched={len(vf_apply_result['plan'])} "
                    f"changed={len(vf_apply_result['changed_points'])} "
                    f"mode={vf_apply_result['translation_mode']} "
                    f"origin={vf_apply_result['translation_origin']} "
                    f"linux_profile={vf_apply_result['translated_linux_profile_path']}."
                )
                active_vf_curve_source = "afterburner"
                vf_expected_samples = select_expected_vf_samples(
                    vf_apply_result["plan"]
                )
                flatten_target = derive_afterburner_dynamic_lock(
                    vf_apply_result["materialization"]["points"]
                )
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
                return True

        if prefer_afterburner_curve:
            afterburner_curve_applied = _apply_afterburner_curve()
            if afterburner_curve_applied and auto_uv_final_curve is not None:
                log(
                    "Auto-UV final curve is present but skipped because "
                    "--prefer-afterburner-curve was requested."
                )
            if not afterburner_curve_applied:
                log(
                    "--prefer-afterburner-curve requested, but no usable Afterburner "
                    "V/F curve was applied; trying Auto-UV final curve fallback."
                )
                auto_uv_curve_applied = _apply_auto_uv_final_curve()
        else:
            auto_uv_curve_applied = _apply_auto_uv_final_curve()
            if not auto_uv_curve_applied:
                afterburner_curve_applied = _apply_afterburner_curve()

    fan_count = c_uint()
    check(
        nvml.nvmlDeviceGetNumFans(device, ctypes.byref(fan_count)),
        "nvmlDeviceGetNumFans",
    )

    if fan_control_enabled and fan_count.value == 0:
        raise NvmlError("GPU reports zero controllable fans")

    device_min_fan_speed_pct = None
    device_max_fan_speed_pct = None
    if fan_control_enabled and hasattr(nvml, "nvmlDeviceGetMinMaxFanSpeed"):
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

    if fan_control_enabled:
        effective_min_fan_speed_pct = min_fan_speed_pct
        effective_max_fan_speed_pct = max_fan_speed_pct
        if device_min_fan_speed_pct is not None:
            effective_min_fan_speed_pct = max(
                effective_min_fan_speed_pct, device_min_fan_speed_pct
            )
        if device_max_fan_speed_pct is not None:
            effective_max_fan_speed_pct = min(
                effective_max_fan_speed_pct, device_max_fan_speed_pct
            )
        if effective_max_fan_speed_pct < effective_min_fan_speed_pct:
            raise NvmlError("effective fan speed range is invalid")

    restored = False

    def restore_default():
        nonlocal restored
        if restored:
            return
        restored = True
        if fan_control_enabled:
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
    if fan_control_enabled:
        print(
            f"Controlling GPU {gpu_index} with {fan_count.value} fan(s), "
            f"mode={mode}, hysteresis={hysteresis_c} C, "
            f"manual-limits={effective_min_fan_speed_pct}-{effective_max_fan_speed_pct}%, "
            f"manual-enable={manual_enable_temp_c} C, auto-restore={auto_restore_temp_c} C, "
            f"emergency-auto={emergency_auto_override_temp_c} C/{emergency_auto_resume_temp_c} C. "
            "Press Ctrl-C to restore auto mode.",
            flush=True,
        )
    else:
        print(
            f"Running GPU {gpu_index} telemetry and V/F policy loop with fan control disabled. "
            "Use --silent-fan-curve to let PenguinBurner control fans. Press Ctrl-C to exit.",
            flush=True,
        )
    startup_gpu_policy = translated_gpu_policy or auto_uv_profile_gpu_policy or {
        "power_limit_w": startup_power_limit_w
    }
    log(
        f"GPU policy: persistence={'on' if enable_persistence_mode else 'off'}, "
        f"{describe_translated_gpu_policy(startup_gpu_policy)}."
    )
    log(f"Config file: {config_path}")
    if active_vf_curve_source == "auto-uv-final" and auto_uv_final_curve is not None:
        log(
            "Active VF curve source: auto-UV final "
            f"path={auto_uv_final_curve['path']} "
            f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
            f"{auto_uv_final_curve['candidate_voltage_mv']}mV; "
            "Afterburner V/F import skipped."
        )
    elif active_vf_curve_source == "afterburner" and prefer_afterburner_curve:
        log(
            "Active VF curve source: Afterburner import requested by --prefer-afterburner-curve."
        )
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
    if fan_control_enabled:
        effective_manual_curve = build_effective_manual_curve(
            curve=curve,
            manual_enable_temp_c=manual_enable_temp_c,
            effective_min_fan_speed_pct=effective_min_fan_speed_pct,
            effective_max_fan_speed_pct=effective_max_fan_speed_pct,
            mode=mode,
        )
        curve_source = fan_config.get("curve_source")
        if curve_source:
            if str(curve_source) == "auto-uv":
                log(
                    "Fan curve source: auto-UV "
                    f"path={fan_config.get('curve_source_path', 'n/a')} "
                    f"generated={fan_config.get('curve_source_generated_at', 'n/a')} "
                    f"target-temp={fan_config.get('curve_source_target_load_temp_c', 'n/a')}C "
                    f"observed-load={fan_config.get('curve_source_loaded_temperature_c', 'n/a')}C "
                    f"observed-fan={fan_config.get('curve_source_observed_fan_speed_pct', 'n/a')}%."
                )
            else:
                curve_flags_u32 = int(fan_config.get("curve_source_flags_u32", 0))
                curve_period_ms = int(
                    fan_config.get(
                        "curve_source_period_ms", int(round(poll_interval_s * 1000))
                    )
                )
                log(
                    f"Fan curve source: {curve_source} "
                    f"period={curve_period_ms}ms flags=0x{curve_flags_u32:08x}."
                )
        log(f"Fan curve points: {format_curve_points(curve)}")
        log(
            f"Effective manual fan curve: {format_curve_points(effective_manual_curve)}"
        )
        if fan_config.get("curve_override_zero_with_hardware_curve"):
            behavior_parts = ["zero-rpm zone uses hardware auto curve"]
            if fan_config.get("curve_hardware_auto_below_device_min"):
                behavior_parts.append(
                    "below device manual minimum uses hardware auto curve"
                )
            takeover_temp_c = fan_config.get("curve_manual_takeover_temp_c")
            if takeover_temp_c is not None:
                behavior_parts.append(
                    f"manual takeover near {float(takeover_temp_c):.2f}C"
                )
            log("Fan curve behavior: " + "; ".join(behavior_parts) + ".")
        log(
            "Silent fan curve guardrail: "
            f"hardware auto above {float(emergency_auto_override_temp_c):.0f}C, "
            f"resume manual below {float(emergency_auto_resume_temp_c):.0f}C."
        )
    else:
        log(
            "Fan control disabled: hardware/driver fan policy remains active; "
            "fan curve files are ignored unless --silent-fan-curve is used."
        )
    if clock_ceiling_controller is not None:
        log(f"Clock ceiling policy: {clock_ceiling_controller.describe()}.")

    while True:
        loop_started = time.monotonic()
        temp = c_uint()
        check(
            nvml.nvmlDeviceGetTemperature(
                device, c_uint(NVML_TEMPERATURE_GPU), ctypes.byref(temp)
            ),
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
        if (
            vf_curve_reader is not None
            and vf_expected_samples
            and vf_apply_result is not None
        ):
            vf_mismatches = detect_vf_curve_reset(vf_curve_reader, vf_expected_samples)
            if (
                vf_mismatches
                and (loop_started - last_vf_reapply_monotonic) >= vf_reapply_cooldown_s
            ):
                try:
                    apply_plan(vf_curve_reader, vf_apply_result["plan"])
                    vf_curve_reader.refresh_points()
                except Exception as exc:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log(f"{timestamp} event=vf-curve-reapply-error error={exc}")
                else:
                    last_vf_reapply_monotonic = loop_started
                    mismatch_preview = format_vf_curve_mismatch_preview(
                        vf_mismatches
                    )
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log(
                        f"{timestamp} {telemetry_text} "
                        f"event=vf-curve-reapplied mismatches={len(vf_mismatches)} "
                        f"samples={mismatch_preview}"
                    )
        if not fan_control_enabled:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"{timestamp} {telemetry_text} fan_control=disabled")
            time.sleep(poll_interval_s)
            continue
        fan_curve_state_text = describe_fan_curve_state(
            current_temp_c=current_temp_c,
            effective_curve=effective_manual_curve,
            manual_mode_active=manual_mode_active,
            emergency_auto_mode_active=hot_auto_mode_active,
            emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
        )
        if hot_auto_mode_active and current_temp_c > emergency_auto_resume_temp_c:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} fan_mode=auto reason=emergency-override"
            )
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
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} event=emergency-override-cleared"
            )

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
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} event=entering-manual-mode"
            )

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
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} event=restoring-auto-mode"
            )
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
                    nvml.nvmlDeviceSetFanSpeed_v2(
                        device, c_uint(fan_idx), c_uint(target_speed)
                    ),
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


def _runtime_profile_selector_from_argv(argv) -> str:
    index = 0
    while index < len(argv):
        arg = str(argv[index])
        if arg == "--auto-uv-profile" and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if arg.startswith("--auto-uv-profile="):
            return arg.split("=", 1)[1].strip()
        index += 1
    return ""


def _runtime_profile_selector_allows_unverified_from_argv(argv) -> bool:
    return any(str(arg) == "--stability-test" for arg in argv)


def cli_main() -> int:
    enable_cli_output_wrapping()
    try:
        runtime_flags = parse_runtime_flags(sys.argv[1:])
        runtime_argv = runtime_flags["passthrough"]
        runtime_profile_selector = _runtime_profile_selector_from_argv(runtime_argv)
        if runtime_profile_selector and resolve_auto_uv_profile(
            runtime_profile_selector,
            allow_unverified=_runtime_profile_selector_allows_unverified_from_argv(
                runtime_argv
            ),
        ) is None:
            raise NvmlError(f"Auto-UV profile not found: {runtime_profile_selector}")
        auto_uv_requested = "--auto-uv-voltage-scan" in runtime_argv
        if auto_uv_requested and (
            runtime_flags["daemonize"]
            or runtime_flags["install_systemd_service"]
            or running_under_systemd_service()
        ):
            raise NvmlError(
                "Auto-UV scans are foreground-only; run the scan directly first, "
                "then daemonize normal runtime after the final curve is saved"
            )
        if runtime_flags["install_systemd_service"]:
            install_systemd_service(
                __file__,
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
                log=log,
            )
        elif runtime_flags["uninstall_systemd_service"]:
            uninstall_systemd_service(log=log)
        elif (
            runtime_flags["daemonize"]
            and not runtime_flags["foreground"]
            and not running_under_systemd_service()
        ):
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
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        debug_exception("fatal error", exc)
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
