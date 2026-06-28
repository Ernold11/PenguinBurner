"""Command-line arguments for the PenguinBurner CLI.

This module only defines flags and defaults; command execution stays in the runtime entrypoint.
"""

from __future__ import annotations

import argparse

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODES
from drivers.nvidia.nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ
from common.penguin_burner_paths import default_runtime_config_path
from runtime.support.runtime_service import DEFAULT_JOURNAL_HOURS

DEFAULT_AUTO_UV_FINAL_DURATION_S = AUTO_UV_DEFAULTS.final_duration_s


def default_cli_config_path() -> str:
    return str(default_runtime_config_path())


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="penguin_burner.py",
        usage="penguin_burner.py [options]",
        description=(
            "PenguinBurner Auto-UV scan and runtime profile utility."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    auto_uv_group = parser.add_argument_group("Auto-UV")
    steam_group = parser.add_argument_group("Steam overlay setup")
    daemon_group = parser.add_argument_group("Runtime and daemon essentials")
    runtime_group = parser.add_argument_group("Runtime tuning")
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
        "--assign-auto-uv-tier",
        nargs=2,
        metavar=("PROFILE", "TIER"),
        help=(
            "Assign a saved verified Auto-UV profile to an adaptive tier and "
            "exit. PROFILE accepts the same selectors as --auto-uv-profile; "
            "TIER is efficiency, balanced, performance, or none."
        ),
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
            "default is preset-aware from the GPU table when detected, otherwise "
            "12.5. Example: 12 allows up to a 12%% clock drop."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-mode",
        choices=AUTO_UV_MODES,
        default=None,
        metavar="MODE",
        help=(
            "Auto-UV preset path. efficiency uses a flat base sweep plus a "
            "low-voltage tail-tune pass; balanced uses the 4-bin tail sweep; "
            "performance uses the 6-bin tail sweep plus Auto-OC."
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
    steam_group.add_argument(
        "--set-steam-overlay-launch",
        metavar="APPID",
        help=(
            "Write the PenguinBurner native-overlay launch option for a Steam "
            "app id and exit. The write is verified so running Steam rewrites "
            "are reported instead of treated as success."
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
        "--migrate-to-daemon-service",
        action="store_true",
        help=(
            "Install penguin-burnerd.service and migrate an existing "
            "PenguinBurner.service when possible."
        ),
    )
    daemon_group.add_argument(
        "--daemon-status",
        action="store_true",
        help="Print PenguinBurner root daemon status and exit.",
    )
    daemon_group.add_argument(
        "--daemon-api",
        metavar="SOCKET",
        help=argparse.SUPPRESS,
    )
    daemon_group.add_argument(
        "--auto-uv-profile",
        default="",
        help=(
            "Use an Auto-UV profile by profile id, candidate id, JSON path, "
            "'active', or 'latest' for daemon runtime, systemd service runtime, "
            "and internal final verification."
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
    advanced_group.add_argument(
        "--stability-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--stability-seconds",
        type=int,
        default=DEFAULT_AUTO_UV_FINAL_DURATION_S,
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--stability-stop-request-file",
        default="",
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--debug-log",
        action="store_true",
        help=(
            "Write a verbose diagnostic log next to the selected config file "
            "under debug-logs/; with the default config this is "
            "~/.config/PenguinBurner/debug-logs"
        ),
    )
    return parser.parse_args(argv)
