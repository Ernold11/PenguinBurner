"""Foreground CLI orchestration for Auto-UV scans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from auto_uv.auto_uv_types import AutoUvError, AutoUvFinalChoiceDiscarded
from auto_uv.voltage_frequency_undervolt_main_loop import (
    run_voltage_frequency_undervolt_main_loop,
)
from auto_uv.initial_check import require_auto_uv_initial_check
from common.penguin_burner_errors import NvmlError
from runtime.support.runtime_debug import log as runtime_log
from runtime.stability_test import build_stability_config


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
    log: Callable[[str], None] = runtime_log


def run_auto_uv_foreground_command(
    args,
    *,
    gpu_index,
    config_path,
    auto_uv_runtime_options: dict,
    interactive: bool,
    dependencies: AutoUvForegroundDependencies | None = None,
) -> None:
    deps = dependencies or AutoUvForegroundDependencies()
    runtime_options = auto_uv_runtime_options
    try:
        if args.auto_uv_voltage_scan:
            run_auto_uv_voltage_scan(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
                auto_uv_runtime_options=runtime_options,
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
    dependencies: AutoUvForegroundDependencies | None = None,
) -> None:
    deps = dependencies or AutoUvForegroundDependencies()
    json_events = bool(args.json_events)
    deps.emit_json_event(
        json_events,
        "auto_uv_start",
        gpu_index=int(gpu_index),
        algorithm="auto_uv",
    )

    def _auto_uv_json_event(event, payload):
        deps.emit_json_event(json_events, event, **dict(payload))

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
        event_callback=_auto_uv_json_event if json_events else None,
    )
    emit_auto_uv_final_result(result, json_events=json_events, dependencies=deps)


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
