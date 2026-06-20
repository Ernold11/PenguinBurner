"""Command-line arguments for the PenguinBurner CLI.

This module only defines flags and defaults; command execution stays in the runtime entrypoint.
"""

from __future__ import annotations

import argparse

from auto_uv.auto_uv_user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode import AUTO_UV_MODES
from nvidia_driver.nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ
from common.penguin_burner_paths import default_runtime_config_path
from runtime_support.runtime_service import DEFAULT_JOURNAL_HOURS
from stability.q2rtx import DEFAULT_HEIGHT, DEFAULT_WIDTH

DEFAULT_AUTO_UV_FINAL_DURATION_S = AUTO_UV_DEFAULTS.final_duration_s
DEFAULT_LACT_NVIDIA_MAX_VF_OFFSET_MHZ = 1000


def default_cli_config_path() -> str:
    return str(default_runtime_config_path())


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="penguin_burner.py",
        usage="penguin_burner.py [options]",
        description=(
            "PenguinBurner Auto-UV runtime, stability, and optional "
            "Afterburner import utility."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    auto_uv_group = parser.add_argument_group("Auto-UV")
    daemon_group = parser.add_argument_group("Runtime and daemon essentials")
    runtime_group = parser.add_argument_group("Runtime tuning")
    overlay_group = parser.add_argument_group("Overlay")
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
        help=(
            "List saved Auto-UV profiles and exit; use a shown profile id or "
            "candidate id with --auto-uv-profile."
        ),
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
            "default uses the GPU table Eco-to-Max clock ratio when detected, "
            "otherwise 12.5. Example: 12 allows up to a 12%% clock drop."
        ),
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
        "--auto-uv-min-voltage-mv",
        type=int,
        default=None,
        metavar="mV",
        help=(
            "Lowest voltage bin Auto-UV may try. Overrides the detected GPU "
            "table floor and the percentage-drop fallback."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-max-drop-pct",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Maximum percentage drop below the first discovered auto-UV start "
            "voltage allowed during candidate search when no GPU table floor "
            "or explicit min voltage is available; default 10.0."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-final-seconds",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Final Auto-UV verification duration in seconds after the best curve "
            "is selected; default "
            f"{AUTO_UV_DEFAULTS.final_duration_s}. Candidate probes remain tiered short tests."
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
            "Medium and deep voltage tiers use 2x and 2.5x this value."
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
        "--auto-uv-power-limit-w",
        type=int,
        default=None,
        metavar="W",
        help=(
            "Power limit in watts to apply during Auto-UV and save with the "
            "final profile. The UI clamps this to the selected GPU's NVML "
            "minimum and maximum power-limit range."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-tail-rise-bins",
        type=int,
        default=None,
        metavar="N",
        help=(
            "How many voltage bins can the voltage curve rise above the locked "
            "undervolt point."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-oc-target-voltage-mv",
        type=int,
        default=None,
        metavar="mV",
        help=(
            "Performance-mode Auto-OC voltage cap in mV. Overrides the detected "
            "GPU table target."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-oc-target-clock-mhz",
        type=int,
        default=None,
        metavar="MHz",
        help=(
            "Performance-mode Auto-OC core clock cap in MHz. Overrides the detected "
            "GPU table target."
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
    advanced_group.add_argument(
        "--check-latency-layer",
        action="store_true",
        help=(
            "Check Vulkan loader discovery for PenguinBurner's opt-in latency "
            "telemetry layer and print Steam launch options."
        ),
    )
    advanced_group.add_argument(
        "--dump-latency-data",
        action="store_true",
        help=(
            "Runtime/daemon only: dump verbose latency internals to the daemon "
            "log -- swapchain present mode and queue depth, plus Reflex "
            "sleep-mode (boost / FPS-cap) and recovery transitions. For "
            "debugging display/VRR and frame-generation behaviour; off by "
            "default. Equivalent to PENGUIN_BURNER_DUMP_LATENCY_DATA=1."
        ),
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
        "--auto-uv-profile",
        default="",
        help=(
            "Use an Auto-UV profile by profile id, candidate id, JSON path, "
            "'active', or 'latest' for runtime, daemon, stability, and LACT export."
        ),
    )
    daemon_group.add_argument(
        "--adaptive-auto-uv",
        action="store_true",
        help=(
            "Runtime/daemon only: allow PenguinBurner to adapt between saved "
            "Auto-UV profile tiers from base present-frame p95 pacing. Requires "
            "at least two available profile tiers. Target defaults to 60 FPS; "
            "override the service env PENGUIN_BURNER_ADAPTIVE_TARGET_FPS for "
            "30, 50, 60, 120, etc."
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
        default=default_cli_config_path(),
        help="Runtime config path to read defaults from",
    )
    runtime_group.add_argument(
        "--gpu-index",
        type=int,
        default=None,
        help="Override the configured GPU index",
    )
    overlay_action_group = overlay_group.add_mutually_exclusive_group()
    overlay_action_group.add_argument(
        "--overlay-toggle",
        action="store_true",
        help=(
            "Toggle PenguinBurner's native in-game overlay and exit. The running "
            "overlay reloads this setting live, so this is suitable for a desktop "
            "global shortcut."
        ),
    )
    overlay_action_group.add_argument(
        "--overlay-enable",
        action="store_true",
        help=(
            "Enable PenguinBurner's native in-game overlay and exit. The running "
            "overlay reloads this setting live."
        ),
    )
    overlay_action_group.add_argument(
        "--overlay-disable",
        action="store_true",
        help=(
            "Disable PenguinBurner's native in-game overlay and exit. The running "
            "overlay reloads this setting live."
        ),
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
    lact_group.add_argument(
        "--lact-max-vf-offset-mhz",
        type=int,
        default=DEFAULT_LACT_NVIDIA_MAX_VF_OFFSET_MHZ,
        metavar="MHz",
        help=(
            "Maximum positive per-point Nvidia V/F offset to emit for LACT; "
            f"default {DEFAULT_LACT_NVIDIA_MAX_VF_OFFSET_MHZ}. Exported "
            "clocks are clamped to base_mhz plus this value."
        ),
    )
    stability_group.add_argument(
        "--stability-test",
        action="store_true",
        help=("Run a non-interactive Q2RTX benchmark stability workload and exit"),
    )
    stability_group.add_argument(
        "--install-q2rtx",
        action="store_true",
        help=(
            "Download PenguinBurner's latest headless Q2RTX benchmark release "
            "and install the required shareware data under "
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
            f"default {DEFAULT_AUTO_UV_FINAL_DURATION_S}"
        ),
    )
    stability_group.add_argument(
        "--stability-workload",
        choices=("q2rtx-cuda", "q2rtx", "cuda"),
        default="q2rtx-cuda",
        help=(
            "Workload selection for --stability-test; q2rtx-cuda keeps the "
            "standard Q2RTX benchmark plus CUDA compute split, q2rtx or cuda "
            "runs only that workload for the full duration."
        ),
    )
    stability_group.add_argument(
        "--stability-width",
        type=int,
        default=None,
        help=(
            "Q2RTX render width used by --stability-test; default auto "
            f"(<=8 GiB VRAM: 2560, >8 GiB/unknown: {DEFAULT_WIDTH})"
        ),
    )
    stability_group.add_argument(
        "--stability-height",
        type=int,
        default=None,
        help=(
            "Q2RTX render height used by --stability-test; default auto "
            f"(<=8 GiB VRAM: 1440, >8 GiB/unknown: {DEFAULT_HEIGHT})"
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
