"""Foreground CLI orchestration for Auto-UV scans."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from auto_uv.domain.types import AutoUvError, AutoUvFinalChoiceDiscarded
from auto_uv.main_loop import (
    run_voltage_frequency_undervolt_main_loop,
)
from auto_uv.persistence.auto_uv_persisted_json_files import clear_auto_uv_stop_request
from auto_uv.initial_check.auto_uv_hardware_initial_check import (
    require_auto_uv_initial_check,
)
from cli.final_choice_text import handle_cli_final_choice_request
from common.penguin_burner_errors import NvmlError
from overlay.config import STEAM_LAUNCH_OPTION
from profiles.uv.profile_store import read_auto_uv_profiles
from profiles.uv.profile_tiers import (
    available_adaptive_tiers,
    profile_tier_label,
    resolve_profile_tier_profiles,
)
from runtime.daemon_client import apply_runtime_intent
from runtime.runtime_spec import runtime_intent_from_argv
from runtime.support.runtime_debug import log as runtime_log
from runtime.support.runtime_service import DEFAULT_JOURNAL_HOURS
from stability.q2rtx.config import build_stability_config


def _noop_emit_json_event(_enabled: bool, _event: str, **_payload) -> None:
    return None


@dataclass(slots=True)
class AutoUvForegroundDependencies:
    require_auto_uv_initial_check: Callable = require_auto_uv_initial_check
    build_stability_config: Callable = build_stability_config
    run_voltage_frequency_undervolt_main_loop: Callable = (
        run_voltage_frequency_undervolt_main_loop
    )
    emit_json_event: Callable[..., None] = _noop_emit_json_event
    final_choice_input: Callable[[str], str] = input
    apply_runtime_intent: Callable = apply_runtime_intent
    runtime_intent_from_argv: Callable = runtime_intent_from_argv
    read_auto_uv_profiles: Callable = read_auto_uv_profiles
    resolve_profile_tier_profiles: Callable = resolve_profile_tier_profiles
    available_adaptive_tiers: Callable = available_adaptive_tiers
    profile_tier_label: Callable = profile_tier_label
    clear_auto_uv_stop_request: Callable = clear_auto_uv_stop_request
    log: Callable[[str], None] = runtime_log


def run_auto_uv_foreground_command(
    args,
    *,
    gpu_index,
    config_path,
    auto_uv_runtime_options: dict,
    interactive: bool,
    program_file: str | Path | None = None,
    journal_hours: int | float = DEFAULT_JOURNAL_HOURS,
    prompt_yes_no: Callable[..., bool] | None = None,
    dependencies: AutoUvForegroundDependencies | None = None,
) -> None:
    deps = dependencies or AutoUvForegroundDependencies()
    runtime_options = auto_uv_runtime_options
    try:
        if args.auto_uv_voltage_scan:
            result = run_auto_uv_voltage_scan(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
                auto_uv_runtime_options=runtime_options,
                interactive=interactive,
                dependencies=deps,
            )
            maybe_prompt_post_scan_runtime_actions(
                result,
                args=args,
                interactive=interactive,
                program_file=program_file,
                journal_hours=journal_hours,
                prompt_yes_no=prompt_yes_no,
                dependencies=deps,
            )
    except AutoUvFinalChoiceDiscarded as exc:
        deps.log(str(exc))
    except AutoUvError as exc:
        raise NvmlError(str(exc)) from exc


def run_auto_uv_voltage_scan(
    args,
    *,
    gpu_index,
    config_path,
    auto_uv_runtime_options: dict,
    interactive: bool = False,
    dependencies: AutoUvForegroundDependencies | None = None,
):
    deps = dependencies or AutoUvForegroundDependencies()
    json_events = bool(args.json_events)
    deps.clear_auto_uv_stop_request()
    deps.emit_json_event(
        json_events,
        "auto_uv_start",
        gpu_index=int(gpu_index),
        algorithm="auto_uv",
    )

    def _auto_uv_event(event, payload):
        payload = dict(payload)
        if json_events:
            deps.emit_json_event(True, event, **payload)
            return
        if interactive and event == "final_choice_request":
            handle_cli_final_choice_request(
                payload,
                input_fn=deps.final_choice_input,
                log=deps.log,
            )

    def _dependency_json_event(payload):
        deps.emit_json_event(
            json_events,
            "dependency_progress",
            **dict(payload),
        )

    try:
        deps.require_auto_uv_initial_check(gpu_index=gpu_index, log=deps.log)
    except RuntimeError as exc:
        raise NvmlError(str(exc)) from exc

    deps.log("Auto-UV: running the voltage-frequency undervolt main loop.")
    result = deps.run_voltage_frequency_undervolt_main_loop(
        gpu_index=gpu_index,
        runtime_options=auto_uv_runtime_options,
        q2rtx_config=deps.build_stability_config(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            auto_install_q2rtx=True,
            progress_context="Auto-UV",
            dependency_progress_callback=(
                _dependency_json_event if json_events else None
            ),
            dependency_text_progress=not json_events,
        ),
        log=deps.log,
        event_callback=(
            _auto_uv_event
            if json_events or interactive
            else None
        ),
    )
    emit_auto_uv_final_result(result, json_events=json_events, dependencies=deps)
    return result


def maybe_prompt_post_scan_runtime_actions(
    result,
    *,
    args,
    interactive: bool,
    program_file: str | Path | None,
    journal_hours: int | float,
    prompt_yes_no: Callable[..., bool] | None,
    dependencies: AutoUvForegroundDependencies | None = None,
) -> None:
    deps = dependencies or AutoUvForegroundDependencies()
    if (
        not interactive
        or bool(getattr(args, "json_events", False))
        or prompt_yes_no is None
        or program_file is None
        or not bool(getattr(result, "success", True))
    ):
        return

    selector = auto_uv_result_profile_selector(result)
    if not selector:
        return

    runtime_argv = ["--auto-uv-profile", selector]
    mode_label = f"single profile {selector}"

    adaptive_tiers = _available_adaptive_tier_labels(deps)
    if len(adaptive_tiers) >= 2:
        deps.log(
            "Adaptive Auto-UV is also available from saved verified tiers: "
            + ", ".join(adaptive_tiers)
            + "."
        )
        deps.log(
            "For Steam games, launch the game through PenguinBurner so adaptive "
            f"runtime can see frame pacing: {STEAM_LAUNCH_OPTION}"
        )
        if prompt_yes_no(
            "Use adaptive Auto-UV instead of the single discovered profile "
            "for runtime/autostart?",
            default=False,
        ):
            runtime_argv = ["--adaptive-auto-uv"]
            mode_label = "adaptive Auto-UV"

    if prompt_yes_no(
        f"Start PenguinBurner runtime now with {mode_label}?",
        default=True,
    ):
        _run_optional_runtime_action(
            persist_on_startup=False,
            runtime_argv=runtime_argv,
            deps=deps,
        )

    if prompt_yes_no(
        f"Persist {mode_label} for startup?",
        default=False,
    ):
        _run_optional_runtime_action(
            persist_on_startup=True,
            runtime_argv=runtime_argv,
            deps=deps,
        )


def auto_uv_result_profile_selector(result) -> str:
    try:
        voltage_mv = int(getattr(result, "final_voltage_mv"))
        clock_mhz = int(getattr(result, "lock_clock_mhz"))
    except (TypeError, ValueError):
        return ""
    if voltage_mv <= 0 or clock_mhz <= 0:
        return ""
    return f"{voltage_mv}mv-{clock_mhz}mhz"


def _available_adaptive_tier_labels(deps: AutoUvForegroundDependencies) -> list[str]:
    try:
        profiles = deps.read_auto_uv_profiles()
        resolved = deps.resolve_profile_tier_profiles(profiles)
        tiers = deps.available_adaptive_tiers(resolved)
    except Exception as exc:
        deps.log(f"Adaptive Auto-UV availability check failed: {exc}")
        return []
    labels = []
    for tier in tiers:
        label = str(deps.profile_tier_label(tier) or tier).strip()
        if label:
            labels.append(label)
    return labels


def _run_optional_runtime_action(
    *,
    persist_on_startup: bool,
    runtime_argv: list[str],
    deps: AutoUvForegroundDependencies,
) -> None:
    try:
        intent = deps.runtime_intent_from_argv(runtime_argv)
        deps.apply_runtime_intent(
            intent,
            persist_on_startup=bool(persist_on_startup),
        )
    except Exception as exc:
        deps.log(f"Could not apply optional runtime action through burnerd: {exc}")


def emit_auto_uv_final_result(
    result,
    *,
    json_events: bool,
    dependencies: AutoUvForegroundDependencies | None = None,
) -> None:
    deps = dependencies or AutoUvForegroundDependencies()
    deps.emit_json_event(
        bool(json_events),
        "final_result",
        voltage_mv=int(result.final_voltage_mv),
        clock_mhz=int(result.lock_clock_mhz),
        power_w=result.final_power_w,
        temperature_c=result.final_temperature_c,
        fan_pct=result.final_fan_speed_pct,
        stop_reason=result.stop_reason,
        failed_candidate_voltage_mv=result.failed_candidate_voltage_mv,
    )
    deps.log(format_auto_uv_final_state(result))


def format_auto_uv_final_state(result) -> str:
    return (
        "Auto-UV final state: "
        f"{result.lock_clock_mhz}MHz@{result.final_voltage_mv}mV "
        f"power={result.final_power_w if result.final_power_w is not None else 'n/a'}W "
        f"temp={result.final_temperature_c if result.final_temperature_c is not None else 'n/a'}C "
        f"fan={result.final_fan_speed_pct if result.final_fan_speed_pct is not None else 'n/a'}% "
        f"stop_reason={result.stop_reason} "
        f"failed_candidate={result.failed_candidate_voltage_mv if result.failed_candidate_voltage_mv is not None else 'none'}"
    )
