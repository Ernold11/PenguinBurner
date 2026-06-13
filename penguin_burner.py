#!/usr/bin/env python3

import atexit
import functools
from pathlib import Path
import shutil
import sys

from auto_uv.cli_runtime import (
    AutoUvForegroundDependencies,
    run_auto_uv_foreground_command,
)
from saved_uv_profiles import auto_uv_profiles_dir, load_auto_uv_final_curve
from cli.interactive_terminal_prompt import prompt_yes_no as cli_prompt_yes_no
from cli.json_event_output import emit_cli_json_event
from cli.main_command_routing import (
    MainCommandRoutingDependencies,
    route_main_command,
)
from cli.normal_runtime import run_normal_runtime
from cli.arguments import parse_arguments
from cli.entry import dispatch_cli
from cli.runtime_config_file import (
    afterburner_root_has_imported_profiles,
    load_runtime_config,
)
from common.penguin_burner_errors import NvmlError
from common.penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_saved_uv_dir,
    default_user_config_dir,
)
from runtime_debug import (
    close_debug_log,
    close_stdio_capture,
    debug_log,
    enable_debug_logging,
    log,
)
from runtime_gpu_control import FlattenedClockCeilingController, run_nvidia_smi_command
from runtime_service import DEFAULT_JOURNAL_HOURS
from saved_profile_verification.runner import run_profile_verification
from stability.q2rtx import StabilityTestError, install_latest_q2rtx

NVIDIA_SMI = shutil.which("nvidia-smi") or "nvidia-smi"


atexit.register(close_debug_log)
atexit.register(close_stdio_capture)


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


def run_q2rtx_install():
    try:
        result = install_latest_q2rtx()
    except StabilityTestError as exc:
        raise NvmlError(f"Q2RTX install failed: {exc}") from exc

    print(f"Installed Q2RTX {result.version} to {result.install_dir}", flush=True)
    print(f"Executable: {result.executable_path}", flush=True)
    print(f"Archive cache: {result.archive_path}", flush=True)
    print(f"Source: {result.asset_url}", flush=True)


def run_nvidia_smi(args):
    return run_nvidia_smi_command(args, executable=NVIDIA_SMI)


def main(argv=None, *, journal_hours=DEFAULT_JOURNAL_HOURS):
    if argv is None:
        argv = sys.argv[1:]
    explicit_cli_args = bool(argv)

    args = parse_arguments(argv)
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
    command_route = route_main_command(
        args=args,
        argv=argv,
        explicit_cli_args=explicit_cli_args,
        interactive=sys.stdin.isatty(),
        dependencies=MainCommandRoutingDependencies(
            clear_auto_uv_state=clear_auto_uv_state,
            load_config=load_runtime_config,
            afterburner_root_has_imported_profiles=afterburner_root_has_imported_profiles,
            run_q2rtx_install=run_q2rtx_install,
            run_profile_verification=run_profile_verification,
            run_auto_uv_foreground_command=functools.partial(
                run_auto_uv_foreground_command,
                dependencies=AutoUvForegroundDependencies(
                    emit_json_event=emit_cli_json_event,
                ),
            ),
        ),
    )
    if command_route.handled:
        return

    run_normal_runtime(
        args=args,
        argv=argv,
        journal_hours=journal_hours,
        program_file=__file__,
        command_route=command_route,
        prompt_yes_no=functools.partial(cli_prompt_yes_no, debug_log=debug_log),
        interactive=sys.stdin.isatty(),
    )


def cli_main() -> int:
    return dispatch_cli(program_file=__file__, main_callback=main)


if __name__ == "__main__":
    raise SystemExit(cli_main())
