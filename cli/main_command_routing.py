from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable

from afterburner.import_vf_curve import load_afterburner_runtime_options
from cli.effective_runtime_options import build_effective_afterburner_runtime_options
from dry_run_preview import run_afterburner_dry_run
from lact import export_lact_config
from penguin_burner_errors import NvmlError
from runtime_debug import (
    debug_effective_runtime_options,
    enable_stdio_capture,
    log as runtime_log,
)
from runtime_service import running_under_systemd_service, stop_existing_penguin_burner_runtime
from runtime_stability_test import run_stability_test
from saved_uv_profiles import (
    delete_auto_uv_profiles,
    format_profile_table,
    load_auto_uv_final_curve,
    read_auto_uv_profile_summaries,
)


@dataclass(slots=True)
class MainCommandRoutingDependencies:
    clear_auto_uv_state: Callable
    load_config: Callable
    afterburner_root_has_imported_profiles: Callable
    run_q2rtx_install: Callable
    run_stability_test: Callable = run_stability_test
    load_afterburner_runtime_options: Callable = load_afterburner_runtime_options
    load_auto_uv_final_curve: Callable = load_auto_uv_final_curve
    running_under_systemd_service: Callable = running_under_systemd_service
    enable_stdio_capture: Callable = enable_stdio_capture
    stop_existing_penguin_burner_runtime: Callable = stop_existing_penguin_burner_runtime
    build_effective_afterburner_runtime_options: Callable = (
        build_effective_afterburner_runtime_options
    )
    debug_effective_runtime_options: Callable = debug_effective_runtime_options
    export_lact_config: Callable = export_lact_config
    run_profile_verification: Callable | None = None
    run_auto_uv_foreground_command: Callable | None = None
    run_afterburner_dry_run: Callable = run_afterburner_dry_run
    read_auto_uv_profile_summaries: Callable = read_auto_uv_profile_summaries
    format_profile_table: Callable = format_profile_table
    delete_auto_uv_profiles: Callable = delete_auto_uv_profiles
    log: Callable[[str], None] = runtime_log
    print_fn: Callable = print


@dataclass(slots=True)
class MainCommandRoutingResult:
    handled: bool
    config_path: object | None = None
    gpu_config: dict | None = None
    fan_config: dict | None = None
    gpu_index: int | None = None
    afterburner_runtime_options: dict | None = None
    prefer_afterburner_curve: bool = False
    auto_uv_profile_selector: str = ""
    auto_uv_final_curve_available: bool = False
    had_persisted_afterburner_root: bool = False


def route_main_command(
    *,
    args,
    argv,
    explicit_cli_args: bool,
    interactive: bool,
    dependencies: MainCommandRoutingDependencies,
) -> MainCommandRoutingResult:
    deps = dependencies
    if args.clear_auto_uv_state and args.fresh_auto_uv_scan:
        raise NvmlError(
            "choose only one of --clear-auto-uv-state or --fresh-auto-uv-scan"
        )
    if args.fresh_auto_uv_scan:
        deps.clear_auto_uv_state(log=deps.log)
        args.auto_uv_voltage_scan = True
    elif args.clear_auto_uv_state:
        deps.clear_auto_uv_state(log=deps.log)
        return MainCommandRoutingResult(handled=True)

    if getattr(args, "auto_uv", False) or getattr(args, "auto_uv3", False):
        args.auto_uv_voltage_scan = True

    if args.list_auto_uv_profiles:
        _print_profile_list(args, deps=deps)
        return MainCommandRoutingResult(handled=True)

    if args.delete_auto_uv_profiles:
        _delete_profiles(args, deps=deps)
        return MainCommandRoutingResult(handled=True)

    if args.install_q2rtx:
        deps.run_q2rtx_install()
        return MainCommandRoutingResult(handled=True)

    config, config_path = deps.load_config(args.config)
    gpu_config = config["gpu"]
    fan_config = config["fan"]
    if args.gpu_index is not None:
        gpu_config["index"] = int(args.gpu_index)
    gpu_index = int(gpu_config["index"])

    if args.stability_test and not (
        str(args.auto_uv_profile or "").strip() or bool(args.prefer_afterburner_curve)
    ):
        deps.run_stability_test(args, gpu_index=gpu_index, config_path=config_path)
        return MainCommandRoutingResult(handled=True)

    stored_options = deps.load_afterburner_runtime_options(config_path)
    had_persisted_afterburner_root = bool(
        str(stored_options.get("afterburner_root", "")).strip()
    )
    has_usable_persisted_afterburner_import = (
        deps.afterburner_root_has_imported_profiles(
            stored_options.get("afterburner_root", "")
        )
    )
    auto_uv_profile_selector = str(args.auto_uv_profile or "").strip()
    auto_uv_final_curve_available = _auto_uv_final_curve_available(
        auto_uv_profile_selector,
        deps=deps,
    )

    default_auto_uv_started = False
    if (
        not explicit_cli_args
        and not deps.running_under_systemd_service()
        and not auto_uv_final_curve_available
        and not has_usable_persisted_afterburner_import
    ):
        args.auto_uv_voltage_scan = True
        default_auto_uv_started = True

    _validate_auto_uv_foreground_args(args, deps=deps)
    if args.auto_uv_voltage_scan:
        _prepare_auto_uv_stdout_capture(
            config_path=config_path,
            argv=argv,
            default_auto_uv_started=default_auto_uv_started,
            deps=deps,
        )
        if args.silent_fan_curve:
            deps.log(
                "Auto-UV note: --silent-fan-curve is a normal runtime/daemon option. "
                "The scan will still save a suggested fan curve automatically when safe, "
                "but it will not take over fan control during the scan."
            )
        deps.stop_existing_penguin_burner_runtime(log=deps.log)

    afterburner_runtime_options = deps.build_effective_afterburner_runtime_options(
        args,
        stored_options,
    )
    prefer_afterburner_curve = bool(args.prefer_afterburner_curve)
    deps.debug_effective_runtime_options(
        config_path=config_path,
        gpu_index=gpu_index,
        afterburner_runtime_options=afterburner_runtime_options,
    )

    if str(args.export_lact_config).strip():
        deps.export_lact_config(
            args=args,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
            log=deps.log,
        )
        return MainCommandRoutingResult(handled=True)

    if args.stability_test:
        if deps.run_profile_verification is None:
            raise RuntimeError("run_profile_verification dependency is required")
        deps.run_profile_verification(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return MainCommandRoutingResult(handled=True)

    if args.restore_defaults_from_config or args.auto_uv_voltage_scan:
        if deps.run_auto_uv_foreground_command is None:
            raise RuntimeError("run_auto_uv_foreground_command dependency is required")
        deps.run_auto_uv_foreground_command(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            afterburner_runtime_options=afterburner_runtime_options,
            interactive=interactive,
        )
        return MainCommandRoutingResult(handled=True)

    if args.dry_run:
        deps.run_afterburner_dry_run(
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return MainCommandRoutingResult(handled=True)

    return MainCommandRoutingResult(
        handled=False,
        config_path=config_path,
        gpu_config=gpu_config,
        fan_config=fan_config,
        gpu_index=gpu_index,
        afterburner_runtime_options=afterburner_runtime_options,
        prefer_afterburner_curve=prefer_afterburner_curve,
        auto_uv_profile_selector=auto_uv_profile_selector,
        auto_uv_final_curve_available=auto_uv_final_curve_available,
        had_persisted_afterburner_root=had_persisted_afterburner_root,
    )


def _print_profile_list(args, *, deps: MainCommandRoutingDependencies) -> None:
    profiles = deps.read_auto_uv_profile_summaries()
    if args.json_events:
        deps.print_fn(json.dumps({"profiles": profiles}, indent=2), flush=True)
    else:
        deps.print_fn(deps.format_profile_table(profiles), flush=True)


def _delete_profiles(args, *, deps: MainCommandRoutingDependencies) -> None:
    deleted = deps.delete_auto_uv_profiles(args.delete_auto_uv_profiles)
    payload = {
        "deleted": [str(path) for path in deleted],
        "deleted_count": len(deleted),
    }
    if args.json_events:
        deps.print_fn(json.dumps(payload, indent=2), flush=True)
    else:
        label = "profile" if len(deleted) == 1 else "profiles"
        deps.print_fn(f"Deleted {len(deleted)} Auto-UV {label}.", flush=True)


def _auto_uv_final_curve_available(
    auto_uv_profile_selector: str,
    *,
    deps: MainCommandRoutingDependencies,
) -> bool:
    try:
        return deps.load_auto_uv_final_curve(auto_uv_profile_selector) is not None
    except Exception:
        return False


def _validate_auto_uv_foreground_args(args, *, deps: MainCommandRoutingDependencies):
    if args.auto_uv_require_final_choice and not args.json_events:
        raise NvmlError("--auto-uv-require-final-choice requires --json-events")
    if args.auto_uv_voltage_scan and args.restore_defaults_from_config:
        raise NvmlError(
            "choose only one of --auto-uv-voltage-scan or --restore-defaults-from-config"
        )
    if args.auto_uv_voltage_scan and deps.running_under_systemd_service():
        raise NvmlError(
            "Auto-UV scans are foreground-only; run the scan directly first, "
            "then daemonize normal runtime after the final curve is saved"
        )


def _prepare_auto_uv_stdout_capture(
    *,
    config_path,
    argv,
    default_auto_uv_started: bool,
    deps: MainCommandRoutingDependencies,
):
    capture_path = deps.enable_stdio_capture(
        config_path,
        argv=argv or ["--auto-uv-voltage-scan"],
        label="auto-uv-stdout",
    )
    if capture_path is not None:
        deps.log(f"Auto-UV stdout/stderr log: {capture_path}")
    if default_auto_uv_started:
        deps.log(
            "No saved Auto-UV curve or usable Afterburner import found; "
            "starting the default foreground Auto-UV scan."
        )
