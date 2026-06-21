"""Top-level PenguinBurner CLI entry flow.

This module handles daemon flags and early profile validation before running the selected command.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Callable

from common.cli_output import enable_cli_output_wrapping
from runtime.support.runtime_debug import debug_exception, log
from runtime.support.runtime_service import (
    daemonize_with_systemd,
    install_systemd_service,
    parse_runtime_flags,
    running_under_systemd_service,
    uninstall_systemd_service,
)
from saved_uv_profiles import resolve_auto_uv_profile

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
        _require_selected_profile_exists(runtime_argv)
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
        elif (
            runtime_flags["daemonize"]
            and not runtime_flags["foreground"]
            and not running_under_systemd_service()
        ):
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
    selector = runtime_profile_selector_from_argv(runtime_argv)
    if not selector:
        return
    if resolve_auto_uv_profile(
        selector,
        allow_unverified=runtime_profile_selector_allows_unverified_from_argv(
            runtime_argv
        ),
    ) is None:
        raise RuntimeError(f"Auto-UV profile not found: {selector}")


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
