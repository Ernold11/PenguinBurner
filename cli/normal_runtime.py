from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cli.runtime_startup_preparation import prepare_runtime_startup
from nvidia_driver.hidden_nvapi_vf import create_hidden_vf_curve_reader
from nvidia_driver.hidden_nvapi_voltage import create_hidden_voltage_reader
from latency_telemetry import start_latency_telemetry_logger
from nvidia_driver.nvml_gpu_policy import NvmlGpuPolicyController
from overlay.config import default_overlay_config_path, load_overlay_config
from runtime_debug import log as runtime_log
from runtime_fan_control import run_runtime_fan_control_loop
from runtime_gpu_control import (
    AdaptiveAutoUvRuntimeController,
    NvmlRuntimeSession,
    OverlayStatePublisher,
    ProcessCpuUsageSampler,
    configure_runtime_vf_curve_policy,
)


@dataclass(slots=True)
class NormalRuntimeDependencies:
    prepare_runtime_startup: Callable = prepare_runtime_startup
    nvml_session_factory: Callable = NvmlRuntimeSession
    create_hidden_voltage_reader: Callable = create_hidden_voltage_reader
    create_hidden_vf_curve_reader: Callable = create_hidden_vf_curve_reader
    gpu_policy_controller_factory: Callable = NvmlGpuPolicyController
    configure_runtime_vf_curve_policy: Callable = configure_runtime_vf_curve_policy
    overlay_state_publisher_factory: Callable = OverlayStatePublisher
    process_cpu_sampler_factory: Callable = ProcessCpuUsageSampler
    adaptive_auto_uv_runtime_factory: Callable = AdaptiveAutoUvRuntimeController
    run_runtime_fan_control_loop: Callable = run_runtime_fan_control_loop
    start_latency_telemetry_logger: Callable = start_latency_telemetry_logger
    log: Callable[[str], None] = runtime_log


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
    deps = dependencies or NormalRuntimeDependencies()
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
    overlay_config_path = default_overlay_config_path()
    overlay_config = load_overlay_config(overlay_config_path)
    overlay_state_publisher = deps.overlay_state_publisher_factory(
        gpu_index=gpu_index,
        nvml_session=nvml_session,
        voltage_reader=voltage_reader,
        vf_curve_reader=vf_curve_reader,
        enabled=overlay_config.enabled,
        update_interval_s=overlay_config.update_interval_s,
        profile_tier=str(getattr(vf_policy, "active_profile_tier", "") or ""),
        profile_tier_key=str(getattr(vf_policy, "active_profile_tier_key", "") or ""),
        profile_id=str(getattr(vf_policy, "active_profile_id", "") or ""),
        adaptive=bool(getattr(args, "adaptive_auto_uv", False)),
        process_cpu_sampler=deps.process_cpu_sampler_factory(),
        config_path=overlay_config_path,
    )
    latency_logger = deps.start_latency_telemetry_logger(log=deps.log)
    adaptive_auto_uv_controller = None
    if bool(getattr(args, "adaptive_auto_uv", False)):
        if vf_policy.active_vf_curve_source == "auto-uv-final":
            adaptive_auto_uv_controller = deps.adaptive_auto_uv_runtime_factory(
                current_tier=vf_policy.active_profile_tier_key,
                vf_curve_reader=vf_curve_reader,
                gpu_policy_controller=gpu_policy_controller,
                clock_ceiling_controller=vf_policy.clock_ceiling_controller,
                overlay_state_publisher=overlay_state_publisher,
                runtime_config_path=config_path,
            )
        else:
            deps.log(
                "Adaptive Auto-UV disabled: active runtime curve is not an Auto-UV profile."
            )
    try:
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
            vf_policy=vf_policy,
            overlay_state_publisher=overlay_state_publisher,
            latency_meter=(latency_logger.meter if latency_logger is not None else None),
            adaptive_auto_uv_controller=adaptive_auto_uv_controller,
            dependencies=fan_loop_dependencies,
        )
    finally:
        if latency_logger is not None:
            latency_logger.close()
