from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from common.penguin_burner_errors import FanCurveBlockedError
from common.penguin_burner_paths import default_user_config_dir
from runtime_support.runtime_debug import log as runtime_log
from runtime_fan_control import load_auto_uv_fan_curve


@dataclass(slots=True)
class RuntimeStartupPreparationDependencies:
    default_user_config_dir: Callable = default_user_config_dir
    load_auto_uv_fan_curve: Callable = load_auto_uv_fan_curve
    log: Callable[[str], None] = runtime_log


@dataclass(slots=True)
class RuntimeStartupPreparationResult:
    auto_uv_runtime_options: dict
    fan_config: dict
    fan_control_enabled: bool
    should_exit: bool = False


def prepare_runtime_startup(
    *,
    config_path,
    fan_config: dict,
    gpu_index,
    auto_uv_runtime_options: dict,
    fan_control_enabled: bool,
    auto_uv_final_curve_available: bool,
    argv,
    journal_hours,
    program_file,
    interactive: bool,
    prompt_yes_no,
    dependencies: RuntimeStartupPreparationDependencies | None = None,
) -> RuntimeStartupPreparationResult:
    _ = (
        config_path,
        gpu_index,
        auto_uv_final_curve_available,
        argv,
        journal_hours,
        program_file,
        interactive,
        prompt_yes_no,
    )
    deps = dependencies or RuntimeStartupPreparationDependencies()
    prepared_fan_config = fan_config
    prepared_fan_config, fan_control_enabled = _prepare_runtime_fan_source(
        fan_config=prepared_fan_config,
        fan_control_enabled=bool(fan_control_enabled),
        deps=deps,
    )
    return RuntimeStartupPreparationResult(
        auto_uv_runtime_options=auto_uv_runtime_options,
        fan_config=prepared_fan_config,
        fan_control_enabled=bool(fan_control_enabled),
    )


def _prepare_runtime_fan_source(
    *,
    fan_config: dict,
    fan_control_enabled: bool,
    deps: RuntimeStartupPreparationDependencies,
) -> tuple[dict, bool]:
    if not fan_control_enabled:
        return fan_config, False

    auto_uv_fan_curve_path = deps.default_user_config_dir() / "auto-uv-fan-curve.json"
    if auto_uv_fan_curve_path.is_file():
        try:
            auto_uv_fan_curve = deps.load_auto_uv_fan_curve(fan_config)
        except FanCurveBlockedError as exc:
            deps.log(f"Manual fan control disabled by auto-UV safety guard: {exc}")
            return fan_config, False
        except Exception as exc:
            deps.log(
                "Manual fan control disabled because the auto-UV fan curve "
                f"is present but invalid: path={auto_uv_fan_curve_path} error={exc}"
            )
            return fan_config, False

        if auto_uv_fan_curve is not None:
            return auto_uv_fan_curve["fan_config"], True

        deps.log(
            "Manual fan control disabled because the auto-UV fan curve "
            f"file could not be loaded: path={auto_uv_fan_curve_path}"
        )
        return fan_config, False

    return fan_config, True
