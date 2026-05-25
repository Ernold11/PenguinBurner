from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from afterburner.first_time_import_prompt import maybe_handle_first_time_afterburner_setup
from afterburner.import_vf_curve import ensure_afterburner_root_configured
from penguin_burner_errors import FanCurveBlockedError
from penguin_burner_paths import default_user_config_dir
from runtime_debug import log as runtime_log
from runtime_fan_control import (
    load_auto_uv_fan_curve,
    load_runtime_afterburner_fan_config,
)


@dataclass(slots=True)
class RuntimeStartupPreparationDependencies:
    ensure_afterburner_root_configured: Callable = ensure_afterburner_root_configured
    maybe_handle_first_time_afterburner_setup: Callable = (
        maybe_handle_first_time_afterburner_setup
    )
    default_user_config_dir: Callable = default_user_config_dir
    load_auto_uv_fan_curve: Callable = load_auto_uv_fan_curve
    load_runtime_afterburner_fan_config: Callable = load_runtime_afterburner_fan_config
    log: Callable[[str], None] = runtime_log


@dataclass(slots=True)
class RuntimeStartupPreparationResult:
    afterburner_runtime_options: dict
    fan_config: dict
    fan_control_enabled: bool
    afterburner_root: str
    afterburner_profile: str
    afterburner_device_profile: str
    should_exit: bool = False


def prepare_runtime_startup(
    *,
    config_path,
    fan_config: dict,
    gpu_index,
    afterburner_runtime_options: dict,
    fan_control_enabled: bool,
    had_persisted_afterburner_root: bool,
    auto_uv_final_curve_available: bool,
    argv,
    journal_hours,
    program_file,
    interactive: bool,
    prompt_yes_no,
    dependencies: RuntimeStartupPreparationDependencies | None = None,
) -> RuntimeStartupPreparationResult:
    deps = dependencies or RuntimeStartupPreparationDependencies()
    runtime_options = afterburner_runtime_options
    prepared_fan_config = fan_config
    afterburner_root, afterburner_profile, afterburner_device_profile = (
        _afterburner_option_strings(runtime_options)
    )

    if not had_persisted_afterburner_root and not auto_uv_final_curve_available:
        runtime_options = deps.ensure_afterburner_root_configured(
            config_path,
            runtime_options,
            gpu_index=gpu_index,
            interactive=interactive,
        )
        afterburner_root, afterburner_profile, afterburner_device_profile = (
            _afterburner_option_strings(runtime_options)
        )
        if afterburner_root and deps.maybe_handle_first_time_afterburner_setup(
            argv=argv,
            journal_hours=journal_hours,
            config_path=config_path,
            fan_config=prepared_fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=runtime_options,
            program_file=program_file,
            prompt_yes_no=prompt_yes_no,
            log=deps.log,
        ):
            return RuntimeStartupPreparationResult(
                afterburner_runtime_options=runtime_options,
                fan_config=prepared_fan_config,
                fan_control_enabled=bool(fan_control_enabled),
                afterburner_root=afterburner_root,
                afterburner_profile=afterburner_profile,
                afterburner_device_profile=afterburner_device_profile,
                should_exit=True,
            )
    elif not afterburner_root and not auto_uv_final_curve_available:
        runtime_options = deps.ensure_afterburner_root_configured(
            config_path,
            runtime_options,
            gpu_index=gpu_index,
            interactive=interactive,
        )
        afterburner_root, afterburner_profile, afterburner_device_profile = (
            _afterburner_option_strings(runtime_options)
        )

    prepared_fan_config, fan_control_enabled = _prepare_runtime_fan_source(
        fan_config=prepared_fan_config,
        fan_control_enabled=bool(fan_control_enabled),
        afterburner_root=afterburner_root,
        gpu_index=gpu_index,
        deps=deps,
    )
    return RuntimeStartupPreparationResult(
        afterburner_runtime_options=runtime_options,
        fan_config=prepared_fan_config,
        fan_control_enabled=bool(fan_control_enabled),
        afterburner_root=afterburner_root,
        afterburner_profile=afterburner_profile,
        afterburner_device_profile=afterburner_device_profile,
    )


def _afterburner_option_strings(runtime_options: dict) -> tuple[str, str, str]:
    return (
        str(runtime_options.get("afterburner_root", "")).strip(),
        str(runtime_options.get("afterburner_profile", "")).strip(),
        str(runtime_options.get("afterburner_device_profile", "")).strip(),
    )


def _prepare_runtime_fan_source(
    *,
    fan_config: dict,
    fan_control_enabled: bool,
    afterburner_root: str,
    gpu_index,
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

    if afterburner_root:
        return (
            deps.load_runtime_afterburner_fan_config(
                fan_config,
                afterburner_root=afterburner_root,
                gpu_index=gpu_index,
            ),
            True,
        )

    return fan_config, True
