from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable

from cli.effective_runtime_options import build_effective_auto_uv_runtime_options
from common.penguin_burner_errors import NvmlError
from runtime.support.runtime_debug import (
    debug_effective_runtime_options,
    enable_stdio_capture,
    log as runtime_log,
)
from runtime.support.runtime_service import running_under_systemd_service, stop_existing_penguin_burner_runtime
from runtime.gpu_control.fan_release import release_fans_to_hardware_auto
from saved_uv_profiles import (
    delete_auto_uv_profiles,
    format_profile_table,
    load_auto_uv_final_curve,
    normalize_profile_tier,
    profile_tier_is_none,
    profile_tier_label,
    read_auto_uv_profile_summaries,
    resolve_auto_uv_profile,
    save_profile_tier_assignment,
)


@dataclass(slots=True)
class MainCommandRoutingDependencies:
    clear_auto_uv_state: Callable
    load_config: Callable
    load_auto_uv_final_curve: Callable = load_auto_uv_final_curve
    running_under_systemd_service: Callable = running_under_systemd_service
    enable_stdio_capture: Callable = enable_stdio_capture
    stop_existing_penguin_burner_runtime: Callable = stop_existing_penguin_burner_runtime
    release_fans_to_hardware_auto: Callable = release_fans_to_hardware_auto
    build_effective_auto_uv_runtime_options: Callable = (
        build_effective_auto_uv_runtime_options
    )
    debug_effective_runtime_options: Callable = debug_effective_runtime_options
    run_profile_verification: Callable | None = None
    run_auto_uv_foreground_command: Callable | None = None
    read_auto_uv_profile_summaries: Callable = read_auto_uv_profile_summaries
    format_profile_table: Callable = format_profile_table
    delete_auto_uv_profiles: Callable = delete_auto_uv_profiles
    resolve_auto_uv_profile: Callable = resolve_auto_uv_profile
    save_profile_tier_assignment: Callable = save_profile_tier_assignment
    normalize_profile_tier: Callable = normalize_profile_tier
    profile_tier_is_none: Callable = profile_tier_is_none
    profile_tier_label: Callable = profile_tier_label
    log: Callable[[str], None] = runtime_log
    print_fn: Callable = print


@dataclass(slots=True)
class MainCommandRoutingResult:
    handled: bool
    config_path: object | None = None
    gpu_config: dict | None = None
    fan_config: dict | None = None
    gpu_index: int | None = None
    auto_uv_runtime_options: dict | None = None
    auto_uv_profile_selector: str = ""
    auto_uv_final_curve_available: bool = False


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

    if getattr(args, "auto_uv", False):
        args.auto_uv_voltage_scan = True

    if args.list_auto_uv_profiles:
        _print_profile_list(args, deps=deps)
        return MainCommandRoutingResult(handled=True)

    if args.delete_auto_uv_profiles:
        _delete_profiles(args, deps=deps)
        return MainCommandRoutingResult(handled=True)

    if args.assign_auto_uv_tier:
        _assign_profile_tier(args, deps=deps)
        return MainCommandRoutingResult(handled=True)

    if not explicit_cli_args and not deps.running_under_systemd_service():
        raise NvmlError(
            "no CLI action selected; use --auto-uv-voltage-scan to scan, "
            "--daemonize --auto-uv-profile latest to apply a saved profile, "
            "or --help to list commands"
        )

    config, config_path = deps.load_config(args.config)
    gpu_config = config["gpu"]
    fan_config = config["fan"]
    if args.gpu_index is not None:
        gpu_config["index"] = int(args.gpu_index)
    gpu_index = int(gpu_config["index"])

    if args.stability_test and not str(args.auto_uv_profile or "").strip():
        raise NvmlError("profile verification requires --auto-uv-profile")

    auto_uv_profile_selector = str(args.auto_uv_profile or "").strip()
    auto_uv_final_curve_available = _auto_uv_final_curve_available(
        auto_uv_profile_selector,
        deps=deps,
    )

    _validate_auto_uv_foreground_args(args, deps=deps)
    if args.auto_uv_voltage_scan:
        _prepare_auto_uv_stdout_capture(
            config_path=config_path,
            argv=argv,
            deps=deps,
        )
        if args.silent_fan_curve:
            deps.log(
                "Auto-UV note: fan control is released to the GPU hardware-auto curve "
                "during the scan; a suggested silent fan curve is still saved "
                "automatically when safe."
            )
        deps.stop_existing_penguin_burner_runtime(log=deps.log)
        # PenguinBurner does not run its own fan control during a scan: the probe
        # drives clocks/voltage erratically, so curve-based fan control would be
        # meaningless and could fight the probe. Release fans to the GPU's
        # hardware-auto controller, clearing any leftover manual fan state from a
        # previously applied profile.
        deps.release_fans_to_hardware_auto(gpu_index, log=deps.log)

    auto_uv_runtime_options = deps.build_effective_auto_uv_runtime_options(args)
    deps.debug_effective_runtime_options(
        config_path=config_path,
        gpu_index=gpu_index,
        auto_uv_runtime_options=auto_uv_runtime_options,
    )

    if args.stability_test:
        if deps.run_profile_verification is None:
            raise RuntimeError("run_profile_verification dependency is required")
        deps.run_profile_verification(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            auto_uv_runtime_options=auto_uv_runtime_options,
        )
        return MainCommandRoutingResult(handled=True)

    if args.auto_uv_voltage_scan:
        if deps.run_auto_uv_foreground_command is None:
            raise RuntimeError("run_auto_uv_foreground_command dependency is required")
        deps.run_auto_uv_foreground_command(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            auto_uv_runtime_options=auto_uv_runtime_options,
            interactive=interactive,
        )
        return MainCommandRoutingResult(handled=True)

    return MainCommandRoutingResult(
        handled=False,
        config_path=config_path,
        gpu_config=gpu_config,
        fan_config=fan_config,
        gpu_index=gpu_index,
        auto_uv_runtime_options=auto_uv_runtime_options,
        auto_uv_profile_selector=auto_uv_profile_selector,
        auto_uv_final_curve_available=auto_uv_final_curve_available,
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


def _assign_profile_tier(args, *, deps: MainCommandRoutingDependencies) -> None:
    selector, raw_tier = args.assign_auto_uv_tier
    if not deps.profile_tier_is_none(raw_tier) and not deps.normalize_profile_tier(raw_tier):
        raise NvmlError(
            "profile tier must be efficiency, balanced, performance, or none"
        )
    resolved = deps.resolve_auto_uv_profile(str(selector), allow_unverified=False)
    if resolved is None:
        raise NvmlError(
            f"Auto-UV profile not found or not final-verified: {selector}"
        )
    _path, profile = resolved
    profile_id = str(profile.get("profile_id") or "").strip()
    if not profile_id:
        raise NvmlError(f"Auto-UV profile has no profile_id: {selector}")

    assignments = deps.save_profile_tier_assignment(profile_id, raw_tier)
    tier = deps.normalize_profile_tier(raw_tier)
    tier_label = deps.profile_tier_label(tier)
    disabled = bool(deps.profile_tier_is_none(raw_tier))
    payload = {
        "profile_id": profile_id,
        "selector": str(selector),
        "tier": "" if disabled else tier,
        "tier_label": "" if disabled else tier_label,
        "disabled": disabled,
        "assignments": assignments,
    }
    if args.json_events:
        deps.print_fn(json.dumps({"profile_tier_assignment": payload}, indent=2), flush=True)
        return
    if disabled:
        deps.print_fn(
            f"Removed adaptive tier assignment for Auto-UV profile {profile_id}.",
            flush=True,
        )
        return
    deps.print_fn(
        f"Assigned Auto-UV profile {profile_id} to {tier_label} tier.",
        flush=True,
    )


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
    if args.auto_uv_voltage_scan and deps.running_under_systemd_service():
        raise NvmlError(
            "Auto-UV scans are foreground-only; run the scan directly first, "
            "then daemonize normal runtime after the final curve is saved"
        )


def _prepare_auto_uv_stdout_capture(
    *,
    config_path,
    argv,
    deps: MainCommandRoutingDependencies,
):
    capture_path = deps.enable_stdio_capture(
        config_path,
        argv=argv or ["--auto-uv-voltage-scan"],
        label="auto-uv-stdout",
    )
    if capture_path is not None:
        deps.log(f"Auto-UV stdout/stderr log: {capture_path}")
