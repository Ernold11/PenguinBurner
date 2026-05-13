"""Normal runtime orchestration after top-level command routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cli.runtime_startup_preparation import prepare_runtime_startup
from hidden_nvapi_vf import create_hidden_vf_curve_reader
from hidden_nvapi_voltage import create_hidden_voltage_reader
from nvml_gpu_policy import NvmlGpuPolicyController
from runtime_debug import log as runtime_log
from runtime_fan_control import run_runtime_fan_control_loop
from runtime_gpu_control import NvmlRuntimeSession, configure_runtime_vf_curve_policy


@dataclass(slots=True)
class NormalRuntimeDependencies:
    prepare_runtime_startup: Callable = prepare_runtime_startup
    nvml_session_factory: Callable = NvmlRuntimeSession
    create_hidden_voltage_reader: Callable = create_hidden_voltage_reader
    create_hidden_vf_curve_reader: Callable = create_hidden_vf_curve_reader
    gpu_policy_controller_factory: Callable = NvmlGpuPolicyController
    configure_runtime_vf_curve_policy: Callable = configure_runtime_vf_curve_policy
    run_runtime_fan_control_loop: Callable = run_runtime_fan_control_loop
    log: Callable[[str], None] = runtime_log


def _dependencies(
    dependencies: NormalRuntimeDependencies | None,
) -> NormalRuntimeDependencies:
    return dependencies or NormalRuntimeDependencies()


def run_normal_runtime(
    *,
    args,
    argv,
    journal_hours,
    program_file,
    command_route,
    prompt_yes_no,
    interactive: bool,
    startup_dependencies=None,
    vf_curve_policy_dependencies=None,
    fan_loop_dependencies=None,
    dependencies: NormalRuntimeDependencies | None = None,
) -> None:
    deps = _dependencies(dependencies)
    config_path = command_route.config_path
    gpu_config = command_route.gpu_config
    fan_config = command_route.fan_config
    gpu_index = command_route.gpu_index
    afterburner_runtime_options = command_route.afterburner_runtime_options

    runtime_startup = deps.prepare_runtime_startup(
        config_path=config_path,
        fan_config=fan_config,
        gpu_index=gpu_index,
        afterburner_runtime_options=afterburner_runtime_options,
        fan_control_enabled=bool(args.silent_fan_curve),
        had_persisted_afterburner_root=command_route.had_persisted_afterburner_root,
        auto_uv_final_curve_available=command_route.auto_uv_final_curve_available,
        argv=argv,
        journal_hours=journal_hours,
        program_file=program_file,
        interactive=interactive,
        prompt_yes_no=prompt_yes_no,
        dependencies=startup_dependencies,
    )
    if runtime_startup.should_exit:
        return

    afterburner_runtime_options = runtime_startup.afterburner_runtime_options
    enable_persistence_mode = gpu_config["enable_persistence_mode"]

    nvml_session = deps.nvml_session_factory(gpu_index=gpu_index)
    voltage_reader = deps.create_hidden_voltage_reader(gpu_index=gpu_index)
    vf_curve_reader = deps.create_hidden_vf_curve_reader(gpu_index=gpu_index)
    try:
        gpu_policy_controller = deps.gpu_policy_controller_factory(
            gpu_index=gpu_index
        )
    except Exception as exc:
        gpu_policy_controller = None
        deps.log(f"Linux GPU policy helper unavailable: {exc}")

    vf_policy = deps.configure_runtime_vf_curve_policy(
        gpu_index=gpu_index,
        enable_persistence_mode=enable_persistence_mode,
        auto_uv_profile_selector=command_route.auto_uv_profile_selector,
        prefer_afterburner_curve=command_route.prefer_afterburner_curve,
        afterburner_root=runtime_startup.afterburner_root,
        afterburner_profile=runtime_startup.afterburner_profile,
        afterburner_device_profile=runtime_startup.afterburner_device_profile,
        afterburner_runtime_options=afterburner_runtime_options,
        vf_curve_reader=vf_curve_reader,
        gpu_policy_controller=gpu_policy_controller,
        dependencies=vf_curve_policy_dependencies,
    )
    deps.run_runtime_fan_control_loop(
        gpu_index=gpu_index,
        config_path=config_path,
        fan_config=runtime_startup.fan_config,
        fan_control_enabled=runtime_startup.fan_control_enabled,
        enable_persistence_mode=enable_persistence_mode,
        prefer_afterburner_curve=command_route.prefer_afterburner_curve,
        nvml_session=nvml_session,
        voltage_reader=voltage_reader,
        vf_curve_reader=vf_curve_reader,
        gpu_policy_controller=gpu_policy_controller,
        translated_gpu_policy=vf_policy.translated_gpu_policy,
        afterburner_source=vf_policy.afterburner_source,
        afterburner_profile_settings=vf_policy.afterburner_profile_settings,
        auto_uv_final_curve=vf_policy.auto_uv_final_curve,
        vf_apply_result=vf_policy.vf_apply_result,
        active_vf_curve_source=vf_policy.active_vf_curve_source,
        auto_uv_profile_gpu_policy=vf_policy.auto_uv_profile_gpu_policy,
        clock_ceiling_controller=vf_policy.clock_ceiling_controller,
        vf_expected_samples=vf_policy.vf_expected_samples,
        startup_power_limit_w=vf_policy.startup_power_limit_w,
        dependencies=fan_loop_dependencies,
    )
