"""Handle the first interactive run after importing an Afterburner profile.

The flow performs a dry run, persists the chosen profile, and asks how to continue.
"""

from __future__ import annotations

import os
import sys

from afterburner.dry_run_preview import run_afterburner_dry_run
from runtime_support.runtime_service import (
    daemonize_with_systemd,
    launcher_script_path,
    running_under_systemd_service,
    systemd_is_available,
)

from .import_vf_curve import persist_afterburner_import
from .vfcurve import resolve_afterburner_vf_source


def maybe_handle_first_time_afterburner_setup(
    *,
    argv,
    journal_hours,
    config_path,
    fan_config,
    gpu_index,
    afterburner_runtime_options,
    program_file,
    prompt_yes_no,
    log,
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
    script_path = launcher_script_path(program_file)
    try:
        run_afterburner_dry_run(
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
    except Exception as exc:
        _log_afterburner_dry_run_failure(
            exc,
            script_path=script_path,
            afterburner_runtime_options=afterburner_runtime_options,
            log=log,
        )
        return True

    _persist_afterburner_dry_run_source(
        config_path=config_path,
        gpu_index=gpu_index,
        afterburner_runtime_options=afterburner_runtime_options,
        log=log,
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
        if prompt_yes_no(
            "Daemonize PenguinBurner under systemd now instead?", default=False
        ):
            if os.geteuid() != 0:
                log(
                    "Daemon mode needs sudo. Re-run with "
                    f"`sudo {script_path} --daemonize` after you are happy with the dry run."
                )
                return True
            daemonize_with_systemd(program_file, argv, journal_hours=journal_hours, log=log)
            return True
    else:
        log("systemd background mode is unavailable on this system.")

    log("No GPU changes were applied.")
    log(f"When you are ready, run `{script_path}` for a foreground test.")
    if systemd_is_available():
        log(f"After that, you can daemonize it with `sudo {script_path} --daemonize`.")
    return True


def _log_afterburner_dry_run_failure(
    exc,
    *,
    script_path,
    afterburner_runtime_options,
    log,
) -> None:
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


def _persist_afterburner_dry_run_source(
    *,
    config_path,
    gpu_index,
    afterburner_runtime_options,
    log,
) -> None:
    try:
        source = resolve_afterburner_vf_source(
            afterburner_root=afterburner_runtime_options.get("afterburner_root")
            or None,
            section=afterburner_runtime_options.get("afterburner_profile") or None,
            device_profile_hint=afterburner_runtime_options.get(
                "afterburner_device_profile"
            )
            or None,
        )
    except Exception as exc:
        log(
            f"Warning: dry run succeeded but failed to persist the selected source: {exc}"
        )
        return

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
