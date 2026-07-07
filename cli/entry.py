"""Top-level PenguinBurner CLI entry flow.

This module handles daemon flags and early profile validation before running the selected command.
"""

from __future__ import annotations

from pathlib import Path
import json
import sys
from typing import Callable

from common.cli_output import enable_cli_output_wrapping
from runtime.support.runtime_debug import debug_exception, log
from runtime.support.runtime_service import (
    DEFAULT_DAEMON_SOCKET,
    daemonize_with_systemd,
    install_systemd_service,
    migrate_to_daemon_service,
    parse_runtime_flags,
    running_under_systemd_service,
    uninstall_systemd_service,
)
from runtime.daemon_client import daemon_status
from profiles.uv.profile_store import read_auto_uv_profiles, resolve_auto_uv_profile
from profiles.uv.profile_tiers import (
    available_adaptive_tiers,
    profile_tier_label,
    resolve_profile_tier_profiles,
)

from .runtime_profile_argument import (
    runtime_profile_selector_allows_unverified_from_argv,
    runtime_profile_selector_from_argv,
)


def dispatch_cli(
    *,
    program_file: str | Path,
    main_callback: Callable[..., object],
    argv: list[str] | None = None,
) -> int:
    enable_cli_output_wrapping()
    try:
        raw_argv = list(sys.argv[1:] if argv is None else argv)
        runtime_flags = parse_runtime_flags(raw_argv)
        runtime_argv = runtime_flags["passthrough"]
        if runtime_flags["daemon_status"]:
            status = daemon_status(socket_path=DEFAULT_DAEMON_SOCKET)
            print(json.dumps(status, indent=2), flush=True)
            return 0
        if runtime_flags["migrate_to_daemon"]:
            migrate_to_daemon_service(
                program_file,
                socket_path=DEFAULT_DAEMON_SOCKET,
                log=log,
            )
            return 0
        _require_selected_profile_exists(runtime_argv)
        _require_adaptive_profiles_available(runtime_flags, runtime_argv)
        _reject_auto_uv_scan_in_background(runtime_flags, runtime_argv)
        if runtime_flags["install_systemd_service"]:
            install_systemd_service(
                program_file,
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
                log=log,
            )
        elif runtime_flags["uninstall_systemd_service"]:
            uninstall_systemd_service(log=log)
        elif runtime_flags["daemonize"] and not running_under_systemd_service():
            daemonize_with_systemd(
                program_file,
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
                log=log,
            )
        else:
            main_callback(runtime_argv, journal_hours=runtime_flags["journal_hours"])
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        debug_exception("fatal error", exc)
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def _require_selected_profile_exists(runtime_argv: list[str]) -> None:
    from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR

    selector = runtime_profile_selector_from_argv(runtime_argv)
    if not selector or selector == STOCK_PROFILE_SELECTOR:
        # Empty = latest; the stock sentinel = run at stock. Neither names a
        # saved profile to validate.
        return
    if resolve_auto_uv_profile(
        selector,
        allow_unverified=runtime_profile_selector_allows_unverified_from_argv(
            runtime_argv
        ),
    ) is None:
        raise RuntimeError(f"Auto-UV profile not found: {selector}")


def _require_adaptive_profiles_available(
    runtime_flags: dict,
    runtime_argv: list[str],
) -> None:
    if "--adaptive-auto-uv" not in runtime_argv:
        return
    if not (
        runtime_flags["daemonize"] or runtime_flags["install_systemd_service"]
    ):
        return
    profiles = read_auto_uv_profiles()
    tiers = available_adaptive_tiers(resolve_profile_tier_profiles(profiles))
    if len(tiers) >= 2:
        return
    tier_text = (
        ", ".join(profile_tier_label(tier) for tier in tiers)
        if tiers
        else "none"
    )
    raise RuntimeError(
        "Adaptive Auto-UV systemd mode requires at least two saved verified "
        "Auto-UV profiles in available adaptive tiers; currently available: "
        f"{tier_text}. Run Auto-UV for another mode or assign profiles with "
        "--assign-auto-uv-tier PROFILE TIER."
    )


def _reject_auto_uv_scan_in_background(runtime_flags: dict, runtime_argv: list[str]) -> None:
    auto_uv_requested = "--auto-uv-voltage-scan" in runtime_argv
    if auto_uv_requested and (
        runtime_flags["daemonize"]
        or runtime_flags["install_systemd_service"]
        or running_under_systemd_service()
    ):
        raise RuntimeError(
            "Auto-UV scans are foreground-only; run the scan directly first, "
            "then daemonize normal runtime after the final curve is saved"
        )
