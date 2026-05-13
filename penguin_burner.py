#!/usr/bin/env python3

import atexit
from pathlib import Path
import signal
import shutil
import sys
import time

from auto_uv3 import run_voltage_frequency_undervolt_main_loop
from auto_uv3.cli_runtime import (
    AutoUvForegroundDependencies,
    run_auto_uv_foreground_command as _runner_auto_uv_foreground_command,
)
from afterburner.default_profile_restore import restore_afterburner_defaults_from_config
from saved_uv_profiles import (
    apply_auto_uv_profile_memory_offset as _apply_auto_uv_profile_memory_offset,
    auto_uv_profiles_dir,
    delete_auto_uv_profiles,
    format_profile_table,
    load_auto_uv_final_curve,
    mark_auto_uv_profile_verification_failed,
    mark_auto_uv_profile_verified,
    read_auto_uv_profile_summaries,
)
from initial_check import require_auto_uv_initial_check
from stability.q2rtx import build_long_stability_test_config
from afterburner.first_time_import_prompt import (
    maybe_handle_first_time_afterburner_setup,
)
from afterburner.vfcurve import (
    derive_afterburner_dynamic_lock,
    describe_afterburner_dynamic_lock,
    describe_afterburner_flatten_validation,
    describe_afterburner_profile_settings,
    load_afterburner_profile_settings,
    resolve_afterburner_vf_source,
)
from dry_run_preview import run_afterburner_dry_run
from hidden_nvapi_vf import create_hidden_vf_curve_reader
from hidden_nvapi_voltage import create_hidden_voltage_reader
from lact import export_lact_config
from afterburner.import_vf_curve import (
    apply_plan,
    apply_afterburner_curve_to_reader,
    backup_current_offsets,
    ensure_afterburner_root_configured,
    load_afterburner_runtime_options,
    restore_offsets,
)
from cli.effective_runtime_options import build_effective_afterburner_runtime_options
from nvml_gpu_policy import (
    NvmlGpuPolicyController,
    apply_translated_gpu_policy,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_saved_uv_dir,
    default_user_config_dir,
)
from stability.q2rtx import (
    StabilityTestError,
    attach_stdout_progress,
    install_latest_q2rtx,
    print_q2rtx_stability_result,
    run_cuda_stability_test,
    run_q2rtx_stability_test,
)
from cli.penguin_burner_cli_entry import run_penguin_burner_cli
from cli.interactive_terminal_prompt import prompt_yes_no as cli_prompt_yes_no
from cli.json_event_output import emit_cli_json_event, json_without_none_values
from cli.main_command_routing import (
    MainCommandRoutingDependencies,
    run_main_command_routing,
)
from cli.normal_runtime import NormalRuntimeDependencies, run_normal_runtime
from cli.penguin_burner_arguments import parse_penguin_burner_arguments
from cli.runtime_profile_argument import (
    runtime_profile_selector_allows_unverified_from_argv,
    runtime_profile_selector_from_argv,
)
from cli.runtime_startup_preparation import (
    RuntimeStartupPreparationDependencies,
    prepare_runtime_startup,
)
from cli.runtime_config_file import (
    afterburner_root_has_imported_profiles as cli_afterburner_root_has_imported_profiles,
    default_config_path,
    default_runtime_config,
    load_raw_runtime_config,
    load_runtime_config,
    persist_q2rtx_source_to_runtime_config,
)
from runtime_debug import (
    close_debug_log,
    close_stdio_capture,
    debug_log,
    debug_effective_runtime_options,
    enable_debug_logging,
    enable_stdio_capture,
    log,
)

from runtime_service import (
    DEFAULT_JOURNAL_HOURS,
    running_under_systemd_service,
    stop_existing_penguin_burner_runtime,
)
from penguin_burner_errors import NvmlError
from runtime_fan_control import (
    RuntimeFanLoopDependencies,
    apply_hysteresis,
    build_effective_manual_curve,
    clamp,
    describe_fan_curve_state,
    format_curve_points,
    limit_speed_change,
    load_auto_uv_fan_curve,
    load_runtime_afterburner_fan_config,
    speed_for_temp,
    run_runtime_fan_control_loop,
    validate_curve,
)
from runtime_gpu_control import (
    FlattenedClockCeilingController,
    NvmlRuntimeSession,
    RuntimeVfCurvePolicyDependencies,
    apply_gpu_base_policy as apply_gpu_base_policy_with_nvidia_smi,
    configure_runtime_vf_curve_policy,
    detect_vf_curve_reset,
    format_vf_curve_mismatch_preview,
    run_nvidia_smi_command,
    select_expected_vf_samples,
)
from runtime_stability_test import (
    build_cuda_stability_config,
    build_stability_config,
    run_stability_test,
    stability_workload_label as _stability_workload_label,
    stability_workload_selection as _stability_workload_selection,
    stability_workload_split_label as _stability_workload_split_label,
)
import saved_profile_verification as saved_profile_verification_rules
from saved_profile_verification import (
    apply_and_verify_profile_vf_plan,
    base_vf_plan_from_profile_plan as _base_vf_plan_from_profile_plan,
    profile_needs_verify_baseline as _profile_needs_verify_baseline,
    profile_verification_baseline_duration_s
    as _profile_verification_baseline_duration_s,
    profile_verification_failure_blocks_apply
    as _profile_verification_failure_blocks_apply,
    profile_verification_metrics_from_result
    as _profile_verification_metrics_from_result,
    profile_verification_voltage_abort_callback
    as _profile_verification_voltage_abort_callback,
    stability_stop_request_abort_callback as _stability_stop_request_abort_callback,
    stability_stop_request_path as _stability_stop_request_path,
)
from saved_profile_verification.runner import (
    ProfileVerificationDependencies,
    apply_and_verify_profile_vf_plan as _runner_apply_and_verify_profile_vf_plan,
    apply_verify_afterburner_profile as _runner_apply_verify_afterburner_profile,
    apply_verify_auto_uv_profile as _runner_apply_verify_auto_uv_profile,
    run_profile_verification as _runner_run_profile_verification,
    run_profile_verification_baseline_probe as _runner_profile_baseline_probe,
)


PROFILE_VERIFY_VOLTAGE_TOLERANCE_MV = (
    saved_profile_verification_rules.PROFILE_VERIFY_VOLTAGE_TOLERANCE_MV
)
PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK = (
    saved_profile_verification_rules.PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK
)
PROFILE_VERIFY_VOLTAGE_WARMUP_S = (
    saved_profile_verification_rules.PROFILE_VERIFY_VOLTAGE_WARMUP_S
)
PROFILE_VERIFY_BASELINE_DURATION_S = (
    saved_profile_verification_rules.PROFILE_VERIFY_BASELINE_DURATION_S
)
PROFILE_VERIFY_BASELINE_MIN_DURATION_S = (
    saved_profile_verification_rules.PROFILE_VERIFY_BASELINE_MIN_DURATION_S
)
NVIDIA_SMI = shutil.which("nvidia-smi") or "nvidia-smi"


atexit.register(close_debug_log)
atexit.register(close_stdio_capture)


def prompt_yes_no(prompt, *, default):
    return cli_prompt_yes_no(prompt, default=default, debug_log=debug_log)


def get_config_path():
    return default_config_path()


def default_config():
    return default_runtime_config()


def load_config(config_path=None):
    return load_runtime_config(config_path)


def _load_raw_runtime_config(config_path):
    return load_raw_runtime_config(config_path)


def persist_stability_q2rtx_source(
    config_path,
    *,
    q2rtx_dir,
    q2rtx_binary,
    progress_context,
):
    persist_q2rtx_source_to_runtime_config(
        config_path,
        q2rtx_dir=q2rtx_dir,
        q2rtx_binary=q2rtx_binary,
        progress_context=progress_context,
    )


def afterburner_root_has_imported_profiles(afterburner_root) -> bool:
    return cli_afterburner_root_has_imported_profiles(afterburner_root)


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


def emit_json_event(enabled: bool, event: str, **payload) -> None:
    emit_cli_json_event(enabled, event, **payload)


def _json_event_without_none_values(value):
    return json_without_none_values(value)


def parse_main_args(argv):
    return parse_penguin_burner_arguments(argv)


def _profile_verification_dependencies():
    return ProfileVerificationDependencies(
        stop_existing_penguin_burner_runtime=stop_existing_penguin_burner_runtime,
        create_hidden_vf_curve_reader=create_hidden_vf_curve_reader,
        gpu_policy_controller_factory=NvmlGpuPolicyController,
        flattened_clock_ceiling_controller_factory=FlattenedClockCeilingController,
        backup_current_offsets=backup_current_offsets,
        restore_offsets=restore_offsets,
        apply_plan=apply_plan,
        verify_profile_vf_plan=apply_and_verify_profile_vf_plan,
        apply_afterburner_curve_to_reader=apply_afterburner_curve_to_reader,
        derive_afterburner_dynamic_lock=derive_afterburner_dynamic_lock,
        load_afterburner_profile_settings=load_afterburner_profile_settings,
        resolve_afterburner_vf_source=resolve_afterburner_vf_source,
        translate_afterburner_gpu_policy=translate_afterburner_gpu_policy,
        apply_translated_gpu_policy=apply_translated_gpu_policy,
        load_auto_uv_final_curve=load_auto_uv_final_curve,
        apply_auto_uv_profile_memory_offset=_apply_auto_uv_profile_memory_offset,
        mark_auto_uv_profile_verification_failed=mark_auto_uv_profile_verification_failed,
        mark_auto_uv_profile_verified=mark_auto_uv_profile_verified,
        run_stability_test=run_stability_test,
        build_stability_config=build_stability_config,
        build_cuda_stability_config=build_cuda_stability_config,
        build_long_stability_test_config=build_long_stability_test_config,
        run_q2rtx_stability_test=run_q2rtx_stability_test,
        run_cuda_stability_test=run_cuda_stability_test,
        attach_stdout_progress=attach_stdout_progress,
        print_q2rtx_stability_result=print_q2rtx_stability_result,
        stability_workload_selection=_stability_workload_selection,
        stability_workload_label=_stability_workload_label,
        stability_workload_split_label=_stability_workload_split_label,
        apply_verify_auto_uv_profile=_apply_verify_auto_uv_profile,
        apply_verify_afterburner_profile=_apply_verify_afterburner_profile,
        run_profile_verification_baseline_probe=_run_profile_verification_baseline_probe,
        log=log,
    )


def _runtime_vf_curve_policy_dependencies():
    return RuntimeVfCurvePolicyDependencies(
        load_auto_uv_final_curve=load_auto_uv_final_curve,
        resolve_afterburner_vf_source=resolve_afterburner_vf_source,
        load_afterburner_profile_settings=load_afterburner_profile_settings,
        translate_afterburner_gpu_policy=translate_afterburner_gpu_policy,
        apply_translated_gpu_policy=apply_translated_gpu_policy,
        describe_translated_gpu_policy=describe_translated_gpu_policy,
        apply_gpu_base_policy=apply_gpu_base_policy,
        apply_plan=apply_plan,
        apply_afterburner_curve_to_reader=apply_afterburner_curve_to_reader,
        apply_auto_uv_profile_memory_offset=_apply_auto_uv_profile_memory_offset,
        flattened_clock_ceiling_controller_factory=FlattenedClockCeilingController,
        select_expected_vf_samples=select_expected_vf_samples,
        derive_afterburner_dynamic_lock=derive_afterburner_dynamic_lock,
        log=log,
    )


def _runtime_fan_loop_dependencies():
    return RuntimeFanLoopDependencies(
        validate_curve=validate_curve,
        build_effective_manual_curve=build_effective_manual_curve,
        clamp=clamp,
        describe_fan_curve_state=describe_fan_curve_state,
        format_curve_points=format_curve_points,
        speed_for_temp=speed_for_temp,
        apply_hysteresis=apply_hysteresis,
        limit_speed_change=limit_speed_change,
        detect_vf_curve_reset=detect_vf_curve_reset,
        format_vf_curve_mismatch_preview=format_vf_curve_mismatch_preview,
        apply_plan=apply_plan,
        describe_translated_gpu_policy=describe_translated_gpu_policy,
        describe_afterburner_dynamic_lock=describe_afterburner_dynamic_lock,
        describe_afterburner_flatten_validation=describe_afterburner_flatten_validation,
        describe_afterburner_profile_settings=describe_afterburner_profile_settings,
        log=log,
        print_fn=print,
        time_monotonic=time.monotonic,
        time_sleep=time.sleep,
        time_strftime=time.strftime,
        atexit_register=atexit.register,
        signal_signal=signal.signal,
        exit_process=sys.exit,
    )


def _runtime_startup_preparation_dependencies():
    return RuntimeStartupPreparationDependencies(
        ensure_afterburner_root_configured=ensure_afterburner_root_configured,
        maybe_handle_first_time_afterburner_setup=maybe_handle_first_time_afterburner_setup,
        default_user_config_dir=default_user_config_dir,
        load_auto_uv_fan_curve=load_auto_uv_fan_curve,
        load_runtime_afterburner_fan_config=load_runtime_afterburner_fan_config,
        log=log,
    )


def _main_command_routing_dependencies():
    return MainCommandRoutingDependencies(
        clear_auto_uv_state=clear_auto_uv_state,
        load_config=load_config,
        afterburner_root_has_imported_profiles=afterburner_root_has_imported_profiles,
        run_q2rtx_install=run_q2rtx_install,
        run_stability_test=run_stability_test,
        load_afterburner_runtime_options=load_afterburner_runtime_options,
        load_auto_uv_final_curve=load_auto_uv_final_curve,
        running_under_systemd_service=running_under_systemd_service,
        enable_stdio_capture=enable_stdio_capture,
        stop_existing_penguin_burner_runtime=stop_existing_penguin_burner_runtime,
        build_effective_afterburner_runtime_options=build_effective_afterburner_runtime_options,
        debug_effective_runtime_options=debug_effective_runtime_options,
        export_lact_config=export_lact_config,
        run_profile_verification=run_profile_verification,
        run_auto_uv_foreground_command=run_auto_uv_foreground_command,
        run_afterburner_dry_run=run_afterburner_dry_run,
        read_auto_uv_profile_summaries=read_auto_uv_profile_summaries,
        format_profile_table=format_profile_table,
        delete_auto_uv_profiles=delete_auto_uv_profiles,
        log=log,
        print_fn=print,
    )


def _normal_runtime_dependencies():
    return NormalRuntimeDependencies(
        prepare_runtime_startup=prepare_runtime_startup,
        nvml_session_factory=NvmlRuntimeSession,
        create_hidden_voltage_reader=create_hidden_voltage_reader,
        create_hidden_vf_curve_reader=create_hidden_vf_curve_reader,
        gpu_policy_controller_factory=NvmlGpuPolicyController,
        configure_runtime_vf_curve_policy=configure_runtime_vf_curve_policy,
        run_runtime_fan_control_loop=run_runtime_fan_control_loop,
        log=log,
    )


def run_profile_verification(
    args,
    *,
    gpu_index,
    config_path,
    afterburner_runtime_options,
):
    return _runner_run_profile_verification(
        args,
        gpu_index=gpu_index,
        config_path=config_path,
        afterburner_runtime_options=afterburner_runtime_options,
        dependencies=_profile_verification_dependencies(),
    )


def _apply_verify_auto_uv_profile(vf_curve_reader, selector: str, gpu_policy_controller):
    return _runner_apply_verify_auto_uv_profile(
        vf_curve_reader,
        selector,
        gpu_policy_controller,
        dependencies=_profile_verification_dependencies(),
    )


def _apply_and_verify_profile_vf_plan(
    vf_curve_reader,
    plan: list[dict],
    *,
    context: str,
) -> None:
    return _runner_apply_and_verify_profile_vf_plan(
        vf_curve_reader,
        plan,
        context=context,
        dependencies=_profile_verification_dependencies(),
    )


def _run_profile_verification_baseline_probe(
    args,
    *,
    gpu_index,
    config_path,
    base_plan: list[dict],
    gpu_policy_controller,
    duration_s: int,
    include_q2rtx: bool,
    include_cuda: bool,
) -> dict | None:
    return _runner_profile_baseline_probe(
        args,
        gpu_index=gpu_index,
        config_path=config_path,
        base_plan=base_plan,
        gpu_policy_controller=gpu_policy_controller,
        duration_s=duration_s,
        include_q2rtx=include_q2rtx,
        include_cuda=include_cuda,
        dependencies=_profile_verification_dependencies(),
    )


def _apply_verify_afterburner_profile(
    vf_curve_reader,
    gpu_policy_controller,
    afterburner_runtime_options,
    *,
    gpu_index,
):
    return _runner_apply_verify_afterburner_profile(
        vf_curve_reader,
        gpu_policy_controller,
        afterburner_runtime_options,
        gpu_index=gpu_index,
        dependencies=_profile_verification_dependencies(),
    )


def _auto_uv_foreground_dependencies():
    return AutoUvForegroundDependencies(
        ensure_afterburner_root_configured=ensure_afterburner_root_configured,
        restore_afterburner_defaults_from_config=restore_afterburner_defaults_from_config,
        require_auto_uv_initial_check=require_auto_uv_initial_check,
        build_stability_config=build_stability_config,
        run_voltage_frequency_undervolt_main_loop=run_voltage_frequency_undervolt_main_loop,
        emit_json_event=emit_json_event,
        log=log,
    )


def run_auto_uv_foreground_command(
    args,
    *,
    gpu_index,
    config_path,
    afterburner_runtime_options,
    interactive,
) -> None:
    return _runner_auto_uv_foreground_command(
        args,
        gpu_index=gpu_index,
        config_path=config_path,
        afterburner_runtime_options=afterburner_runtime_options,
        interactive=interactive,
        dependencies=_auto_uv_foreground_dependencies(),
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


def apply_gpu_base_policy(gpu_index, enable_persistence_mode, power_limit_w):
    return apply_gpu_base_policy_with_nvidia_smi(
        gpu_index,
        enable_persistence_mode,
        power_limit_w,
        run_nvidia_smi_fn=run_nvidia_smi,
        log=log,
    )


def main(argv=None, *, journal_hours=DEFAULT_JOURNAL_HOURS):
    if argv is None:
        argv = sys.argv[1:]
    explicit_cli_args = bool(argv)

    args = parse_main_args(argv)
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
    command_route = run_main_command_routing(
        args=args,
        argv=argv,
        explicit_cli_args=explicit_cli_args,
        interactive=sys.stdin.isatty(),
        dependencies=_main_command_routing_dependencies(),
    )
    if command_route.handled:
        return

    run_normal_runtime(
        args=args,
        argv=argv,
        journal_hours=journal_hours,
        program_file=__file__,
        command_route=command_route,
        prompt_yes_no=prompt_yes_no,
        interactive=sys.stdin.isatty(),
        startup_dependencies=_runtime_startup_preparation_dependencies(),
        vf_curve_policy_dependencies=_runtime_vf_curve_policy_dependencies(),
        fan_loop_dependencies=_runtime_fan_loop_dependencies(),
        dependencies=_normal_runtime_dependencies(),
    )


def _runtime_profile_selector_from_argv(argv) -> str:
    return runtime_profile_selector_from_argv(argv)


def _runtime_profile_selector_allows_unverified_from_argv(argv) -> bool:
    return runtime_profile_selector_allows_unverified_from_argv(argv)


def cli_main() -> int:
    return run_penguin_burner_cli(program_file=__file__, main_callback=main)


if __name__ == "__main__":
    raise SystemExit(cli_main())
