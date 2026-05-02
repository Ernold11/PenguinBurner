#!/usr/bin/env python3

import atexit
import ctypes
import json
from pathlib import Path
import signal
import shutil
import sys
import tempfile
import time

from auto_uv3 import run_voltage_frequency_undervolt_main_loop
from auto_uv3.auto_uv_types import AutoUvError, AutoUvFinalChoiceDiscarded
from auto_uv3.auto_uv_user_options import (
    AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO,
    AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO,
)
from auto_uv3.scan_mode import normalize_auto_uv_mode
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
from nvml_gpu_policy import (
    MAX_AFTERBURNER_MEM_OFFSET_MHZ,
    NvmlGpuPolicyController,
    apply_translated_gpu_policy,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_saved_uv_dir,
    default_user_config_dir,
    resolve_afterburner_root,
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
from cli.penguin_burner_arguments import parse_penguin_burner_arguments
from cli.runtime_profile_argument import (
    runtime_profile_selector_allows_unverified_from_argv,
    runtime_profile_selector_from_argv,
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
from penguin_burner_errors import FanCurveBlockedError, NvmlError
from runtime_fan_control import (
    apply_hysteresis,
    build_effective_manual_curve,
    clamp,
    describe_fan_curve_state,
    format_curve_points,
    limit_speed_change,
    load_auto_uv_fan_curve,
    load_runtime_afterburner_fan_config,
    speed_for_temp,
    validate_curve,
)
from runtime_gpu_control import (
    NVML_SUCCESS,
    NVML_TEMPERATURE_GPU,
    FlattenedClockCeilingController,
    apply_gpu_base_policy as apply_gpu_base_policy_with_nvidia_smi,
    check_nvml_return_code as check,
    detect_vf_curve_reset,
    format_telemetry,
    format_vf_curve_mismatch_preview,
    get_power_draw_w,
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


def run_profile_verification(
    args,
    *,
    gpu_index,
    config_path,
    afterburner_runtime_options,
):
    selector = str(args.auto_uv_profile or "").strip()
    prefer_afterburner_curve = bool(args.prefer_afterburner_curve)
    if not selector and not prefer_afterburner_curve:
        run_stability_test(args, gpu_index=gpu_index, config_path=config_path)
        return

    stop_existing_penguin_burner_runtime(log=log)
    vf_curve_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    if vf_curve_reader is None:
        raise NvmlError("could not open the live Nvidia V/F curve reader")

    gpu_policy_controller = None
    clock_ceiling_controller = None
    backup_path = None
    try:
        try:
            gpu_policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
        except Exception as exc:
            log(f"Linux GPU policy helper unavailable: {exc}")
        backup_file = tempfile.NamedTemporaryFile(
            prefix="penguin-burner-verify-", suffix=".json", delete=False
        )
        backup_file.close()
        backup_path = Path(backup_file.name)
        backup_current_offsets(
            vf_curve_reader,
            backup_path,
            policy_controller=gpu_policy_controller,
        )

        if prefer_afterburner_curve:
            label, flatten_target, verify_plan = _apply_verify_afterburner_profile(
                vf_curve_reader,
                gpu_policy_controller,
                afterburner_runtime_options,
                gpu_index=gpu_index,
            )
        else:
            label, flatten_target, verify_plan = _apply_verify_auto_uv_profile(
                vf_curve_reader,
                selector,
                gpu_policy_controller,
            )

        if flatten_target is not None and gpu_policy_controller is not None:
            try:
                clock_ceiling_controller = FlattenedClockCeilingController(
                    flatten_target=flatten_target,
                    policy_controller=gpu_policy_controller,
                    exact_lock=True,
                )
                clock_ceiling_controller.apply()
            except Exception as exc:
                clock_ceiling_controller = None
                log(f"Skipping verification clock lock: {exc}")
            else:
                log(
                    "Configured verification clock lock: "
                    f"{clock_ceiling_controller.describe()}."
                )
                _apply_and_verify_profile_vf_plan(
                    vf_curve_reader,
                    verify_plan,
                    context="selected profile after clock lock",
                )
                log(
                    "Re-applied profile V/F curve after verification clock lock: "
                    f"points={len(verify_plan)}."
                )

        duration_s = int(args.stability_seconds)
        include_q2rtx, include_cuda = _stability_workload_selection(args)
        workload_label = _stability_workload_label(
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        split_label = _stability_workload_split_label(
            duration_s,
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        log(
            "Profile verification workload split: "
            f"{split_label}."
        )
        log(
            "Profile verification starting: "
            f"profile={label} duration={duration_s}s workload={workload_label}."
        )
        stability_config = (
            build_stability_config(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
                progress_context="Profile verification",
            )
            if include_q2rtx
            else build_cuda_stability_config(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
            )
        )
        stability_config = build_long_stability_test_config(
            stability_config,
            total_duration_s=duration_s,
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        if flatten_target is not None:
            stability_config.abort_callback = (
                _profile_verification_voltage_abort_callback(
                    flatten_target,
                    previous_callback=stability_config.abort_callback,
                )
            )
        stop_request_path = _stability_stop_request_path(args)
        if stop_request_path is not None:
            stability_config.abort_callback = _stability_stop_request_abort_callback(
                stop_request_path,
                previous_callback=stability_config.abort_callback,
            )
        attach_stdout_progress(stability_config)
        try:
            result = (
                run_q2rtx_stability_test(stability_config)
                if include_q2rtx
                else run_cuda_stability_test(stability_config)
            )
        except StabilityTestError as exc:
            raise NvmlError(f"stability test configuration error: {exc}") from exc
        print_q2rtx_stability_result(result)
        if not result.success:
            if (
                not prefer_afterburner_curve
                and _profile_verification_failure_blocks_apply(result.reason)
            ):
                try:
                    failed_path = mark_auto_uv_profile_verification_failed(
                        selector,
                        failure={
                            "reason": result.reason,
                            "log_path": str(result.log_path),
                            "workload": workload_label,
                            "fatal_output_matches": list(
                                getattr(result, "fatal_output_matches", []) or []
                            ),
                        },
                    )
                    if failed_path is not None:
                        log(
                            "Marked profile verification failed: "
                            f"path={failed_path} reason={result.reason}"
                        )
                except Exception as exc:
                    log(f"Warning: failed to mark profile verification failed: {exc}")
            raise NvmlError(
                f"profile verification failed: {result.reason}; log={result.log_path}"
            )
        base_metrics = None
        if not prefer_afterburner_curve and _profile_needs_verify_baseline(selector):
            if clock_ceiling_controller is not None:
                try:
                    clock_ceiling_controller.close()
                except Exception as exc:
                    log(
                        "Warning: failed to reset verification clock lock before "
                        f"baseline probe: {exc}"
                    )
                clock_ceiling_controller = None
            try:
                base_plan = _base_vf_plan_from_profile_plan(verify_plan)
            except Exception as exc:
                log(f"Profile verification baseline probe skipped: {exc}")
            else:
                base_metrics = _run_profile_verification_baseline_probe(
                    args,
                    gpu_index=gpu_index,
                    config_path=config_path,
                    base_plan=base_plan,
                    gpu_policy_controller=gpu_policy_controller,
                    duration_s=_profile_verification_baseline_duration_s(duration_s),
                    include_q2rtx=include_q2rtx,
                    include_cuda=include_cuda,
                )
        if not prefer_afterburner_curve:
            verified_path = mark_auto_uv_profile_verified(
                selector,
                verification={
                    "workload": workload_label,
                    "duration_s": duration_s,
                    "result_reason": result.reason,
                    "log_path": str(result.log_path),
                    "target_clock_mhz": (
                        flatten_target.get("lock_clock_mhz")
                        if isinstance(flatten_target, dict)
                        else None
                    ),
                    "target_voltage_mv": (
                        flatten_target.get("lock_voltage_mv")
                        if isinstance(flatten_target, dict)
                        else None
                    ),
                },
                metrics=_profile_verification_metrics_from_result(result),
                base_metrics=base_metrics,
            )
            log(f"Marked profile verified: path={verified_path}")
        log(f"Profile verification passed: profile={label}.")
    finally:
        if clock_ceiling_controller is not None:
            try:
                clock_ceiling_controller.close()
            except Exception as exc:
                log(f"Warning: failed to reset verification clock lock: {exc}")
        if backup_path is not None:
            try:
                restore_offsets(
                    vf_curve_reader,
                    backup_path,
                    policy_controller=gpu_policy_controller,
                )
                log("Restored V/F offsets after profile verification.")
            except Exception as exc:
                log(f"Warning: failed to restore V/F offsets after verification: {exc}")
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
        if gpu_policy_controller is not None:
            gpu_policy_controller.close()
        vf_curve_reader.close()


def _apply_verify_auto_uv_profile(vf_curve_reader, selector: str, gpu_policy_controller):
    auto_uv_final_curve = load_auto_uv_final_curve(selector, allow_unverified=True)
    if auto_uv_final_curve is None:
        raise NvmlError("Auto-UV profile not found")
    _apply_and_verify_profile_vf_plan(
        vf_curve_reader,
        auto_uv_final_curve["plan"],
        context="selected profile",
    )
    label = (
        f"auto-UV:{auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV"
    )
    log(
        "Applied profile for verification: "
        f"path={auto_uv_final_curve['path']} "
        f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV "
        f"points={len(auto_uv_final_curve['plan'])}."
    )
    memory_policy = _apply_auto_uv_profile_memory_offset(
        profile_label=label,
        memory_offset_mhz=auto_uv_final_curve.get("memory_offset_mhz"),
        gpu_policy_controller=gpu_policy_controller,
    )
    if memory_policy:
        log(
            "Applied profile memory offset for verification: "
            f"{int(memory_policy['mem_clk_vf_offset_mhz']):+d}MHz."
        )
    return label, auto_uv_final_curve["flatten_target"], auto_uv_final_curve["plan"]


def _apply_and_verify_profile_vf_plan(
    vf_curve_reader,
    plan: list[dict],
    *,
    context: str,
) -> None:
    apply_and_verify_profile_vf_plan(
        vf_curve_reader,
        plan,
        context=context,
        apply_plan_fn=apply_plan,
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
    try:
        log(
            "Profile verification baseline probe starting: "
            f"duration={int(duration_s)}s "
            f"{_stability_workload_split_label(duration_s, include_q2rtx=include_q2rtx, include_cuda=include_cuda)}."
        )
        baseline_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
        if baseline_reader is None:
            raise NvmlError("could not open the live Nvidia V/F curve reader")
        try:
            _apply_and_verify_profile_vf_plan(
                baseline_reader,
                base_plan,
                context="baseline profile",
            )
        finally:
            baseline_reader.close()
        if gpu_policy_controller is not None:
            try:
                gpu_policy_controller.apply_clock_offsets(mem_clk_vf_offset_mhz=0)
            except Exception as exc:
                log(f"Warning: failed to reset memory offset for baseline probe: {exc}")
        stability_config = (
            build_stability_config(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
                duration_override=int(duration_s),
                progress_context="Profile baseline",
            )
            if include_q2rtx
            else build_cuda_stability_config(
                args,
                gpu_index=gpu_index,
                config_path=config_path,
            )
        )
        stability_config = build_long_stability_test_config(
            stability_config,
            total_duration_s=int(duration_s),
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        stop_request_path = _stability_stop_request_path(args)
        if stop_request_path is not None:
            stability_config.abort_callback = _stability_stop_request_abort_callback(
                stop_request_path,
                previous_callback=stability_config.abort_callback,
            )
        attach_stdout_progress(stability_config)
        result = (
            run_q2rtx_stability_test(stability_config)
            if include_q2rtx
            else run_cuda_stability_test(stability_config)
        )
        print_q2rtx_stability_result(result)
        if not result.success:
            log(
                "Profile verification baseline probe skipped: "
                f"{result.reason}; log={result.log_path}"
            )
            return None
        metrics = _profile_verification_metrics_from_result(result)
        log("Profile verification baseline probe complete.")
        return metrics
    except Exception as exc:
        log(f"Profile verification baseline probe skipped: {exc}")
        return None


def _apply_verify_afterburner_profile(
    vf_curve_reader,
    gpu_policy_controller,
    afterburner_runtime_options,
    *,
    gpu_index,
):
    afterburner_root = str(
        afterburner_runtime_options.get("afterburner_root", "")
    ).strip()
    if not afterburner_root:
        raise NvmlError("Afterburner profile is not configured")
    section = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
    device_profile = str(
        afterburner_runtime_options.get("afterburner_device_profile", "")
    ).strip()
    source = resolve_afterburner_vf_source(
        afterburner_root=afterburner_root,
        section=section or None,
        device_profile_hint=device_profile or None,
        dangerously_skip_validation=bool(
            afterburner_runtime_options.get("dangerously_skip_validation")
        ),
    )
    translated_gpu_policy = None
    if gpu_policy_controller is not None:
        try:
            profile_settings = load_afterburner_profile_settings(
                profile_path=source["profile_path"],
                section=source["section"],
            )
            translated_gpu_policy = translate_afterburner_gpu_policy(
                profile_settings,
                power_limits=gpu_policy_controller.query_power_limits(),
                power_limit_cap_w=afterburner_runtime_options[
                    "power_limit_override_w"
                ],
            )
            apply_translated_gpu_policy(gpu_policy_controller, translated_gpu_policy)
        except Exception as exc:
            translated_gpu_policy = None
            log(f"Skipping Afterburner GPU policy during verification: {exc}")
    vf_apply_result = apply_afterburner_curve_to_reader(
        vf_curve_reader,
        profile_path=source["profile_path"],
        section=source["section"],
        gpu_policy=translated_gpu_policy,
        preserve_base_below_mv=afterburner_runtime_options["preserve_base_below_mv"],
    )
    vf_curve_reader.refresh_points()
    flatten_target = derive_afterburner_dynamic_lock(
        vf_apply_result["materialization"]["points"]
    )
    label = f"afterburner:{source['section']}"
    log(
        "Applied Afterburner profile for verification: "
        f"section={source['section']} matched={len(vf_apply_result['plan'])} "
        f"changed={len(vf_apply_result['changed_points'])} "
        f"gpu-index={int(gpu_index)}."
    )
    return label, flatten_target, vf_apply_result["plan"]


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
    if args.clear_auto_uv_state and args.fresh_auto_uv_scan:
        raise NvmlError(
            "choose only one of --clear-auto-uv-state or --fresh-auto-uv-scan"
        )
    if args.fresh_auto_uv_scan:
        clear_auto_uv_state(log=log)
        args.auto_uv_voltage_scan = True
    elif args.clear_auto_uv_state:
        clear_auto_uv_state(log=log)
        return
    if args.auto_uv3:
        args.auto_uv_voltage_scan = True
    if args.list_auto_uv_profiles:
        profiles = read_auto_uv_profile_summaries()
        if args.json_events:
            print(json.dumps({"profiles": profiles}, indent=2), flush=True)
        else:
            print(format_profile_table(profiles), flush=True)
        return
    if args.delete_auto_uv_profiles:
        deleted = delete_auto_uv_profiles(args.delete_auto_uv_profiles)
        payload = {
            "deleted": [str(path) for path in deleted],
            "deleted_count": len(deleted),
        }
        if args.json_events:
            print(json.dumps(payload, indent=2), flush=True)
        else:
            label = "profile" if len(deleted) == 1 else "profiles"
            print(f"Deleted {len(deleted)} Auto-UV {label}.", flush=True)
        return
    if args.install_q2rtx:
        run_q2rtx_install()
        return
    config, config_path = load_config(args.config)
    gpu_config = config["gpu"]
    fan_config = config["fan"]
    if args.gpu_index is not None:
        gpu_config["index"] = int(args.gpu_index)
    gpu_index = int(gpu_config["index"])
    if args.stability_test and not (
        str(args.auto_uv_profile or "").strip() or bool(args.prefer_afterburner_curve)
    ):
        run_stability_test(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
        )
        return
    stored_afterburner_runtime_options = load_afterburner_runtime_options(config_path)
    had_persisted_afterburner_root = bool(
        str(stored_afterburner_runtime_options.get("afterburner_root", "")).strip()
    )
    has_usable_persisted_afterburner_import = afterburner_root_has_imported_profiles(
        stored_afterburner_runtime_options.get("afterburner_root", "")
    )
    auto_uv_profile_selector = str(args.auto_uv_profile or "").strip()
    auto_uv_final_curve_available = False
    try:
        auto_uv_final_curve_available = (
            load_auto_uv_final_curve(auto_uv_profile_selector) is not None
        )
    except Exception:
        auto_uv_final_curve_available = False
    default_auto_uv_started = False
    if (
        not explicit_cli_args
        and not running_under_systemd_service()
        and not auto_uv_final_curve_available
        and not has_usable_persisted_afterburner_import
    ):
        args.auto_uv_voltage_scan = True
        default_auto_uv_started = True
    if args.auto_uv_require_final_choice and not args.json_events:
        raise NvmlError("--auto-uv-require-final-choice requires --json-events")
    if args.auto_uv_voltage_scan:
        capture_path = enable_stdio_capture(
            config_path,
            argv=argv or ["--auto-uv-voltage-scan"],
            label="auto-uv-stdout",
        )
        if capture_path is not None:
            log(f"Auto-UV stdout/stderr log: {capture_path}")
        if default_auto_uv_started:
            log(
                "No saved Auto-UV curve or usable Afterburner import found; "
                "starting the default foreground Auto-UV scan."
            )
    if args.auto_uv_voltage_scan and args.restore_defaults_from_config:
        raise NvmlError(
            "choose only one of --auto-uv-voltage-scan or --restore-defaults-from-config"
        )
    if args.auto_uv_voltage_scan and running_under_systemd_service():
        raise NvmlError(
            "Auto-UV scans are foreground-only; run the scan directly first, "
            "then daemonize normal runtime after the final curve is saved"
        )
    if args.auto_uv_voltage_scan and args.silent_fan_curve:
        log(
            "Auto-UV note: --silent-fan-curve is a normal runtime/daemon option. "
            "The scan will still save a suggested fan curve automatically when safe, "
            "but it will not take over fan control during the scan."
        )
    if args.auto_uv_voltage_scan:
        stop_existing_penguin_burner_runtime(log=log)
    afterburner_runtime_options = dict(stored_afterburner_runtime_options)
    if args.afterburner_dir.strip():
        afterburner_runtime_options["afterburner_root"] = str(
            resolve_afterburner_root(args.afterburner_dir)
        )
    if args.profile_section.strip():
        afterburner_runtime_options["afterburner_profile"] = str(
            args.profile_section
        ).strip()
    if args.afterburner_device_profile.strip():
        afterburner_runtime_options["afterburner_device_profile"] = str(
            args.afterburner_device_profile
        ).strip()
    if args.power_limit_override_w is not None:
        afterburner_runtime_options["power_limit_override_w"] = (
            int(args.power_limit_override_w)
            if int(args.power_limit_override_w) > 0
            else None
        )
    if args.preserve_base_below_mv is not None:
        afterburner_runtime_options["preserve_base_below_mv"] = (
            int(args.preserve_base_below_mv)
            if int(args.preserve_base_below_mv) > 0
            else None
        )
    if args.auto_uv_max_drop_pct is not None:
        afterburner_runtime_options["auto_uv_max_drop_pct"] = (
            float(args.auto_uv_max_drop_pct)
            if float(args.auto_uv_max_drop_pct) > 0.0
            else None
        )
    if args.auto_uv_final_seconds is not None:
        afterburner_runtime_options["auto_uv_final_seconds"] = (
            int(args.auto_uv_final_seconds)
            if int(args.auto_uv_final_seconds) > 0
            else None
        )
    if args.auto_uv_short_seconds is not None:
        afterburner_runtime_options["auto_uv_short_seconds"] = (
            max(10, min(60, int(args.auto_uv_short_seconds)))
            if int(args.auto_uv_short_seconds) > 0
            else None
        )
    if args.auto_uv_memory_offset_mhz is not None:
        afterburner_runtime_options["auto_uv_memory_offset_mhz"] = max(
            0,
            min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(args.auto_uv_memory_offset_mhz)),
        )
    if args.auto_uv_efficiency_stop_streak is not None:
        afterburner_runtime_options["auto_uv_efficiency_stop_streak"] = max(
            0,
            int(args.auto_uv_efficiency_stop_streak),
        )
    if args.auto_uv_min_efficiency_stop_drop_pct is not None:
        afterburner_runtime_options["auto_uv_min_efficiency_stop_drop_pct"] = max(
            0.0,
            float(args.auto_uv_min_efficiency_stop_drop_pct),
        )
    if args.auto_uv_max_clock_drop_pct is not None:
        afterburner_runtime_options["auto_uv_max_clock_drop_pct"] = max(
            0.0,
            float(args.auto_uv_max_clock_drop_pct),
        )
    if args.auto_uv_clock_bump_budget_ratio is not None:
        max_clock_bump_budget_ratio = (
            AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO
            if bool(args.yolo)
            else AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO
        )
        afterburner_runtime_options["auto_uv_clock_bump_budget_ratio"] = max(
            0.0,
            min(
                float(max_clock_bump_budget_ratio),
                float(args.auto_uv_clock_bump_budget_ratio),
            ),
        )
    if args.yolo:
        afterburner_runtime_options["auto_uv_yolo"] = True
    if args.auto_uv_mode is not None:
        afterburner_runtime_options["auto_uv_mode"] = normalize_auto_uv_mode(
            args.auto_uv_mode
        )
    if args.auto_uv_require_final_choice:
        afterburner_runtime_options["auto_uv_require_final_choice"] = True
    if args.dangerously_skip_validation:
        afterburner_runtime_options["dangerously_skip_validation"] = True
    prefer_afterburner_curve = bool(args.prefer_afterburner_curve)
    debug_effective_runtime_options(
        config_path=config_path,
        gpu_index=gpu_index,
        afterburner_runtime_options=afterburner_runtime_options,
    )
    if str(args.export_lact_config).strip():
        export_lact_config(
            args=args,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
            log=log,
        )
        return
    if args.stability_test:
        run_profile_verification(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return
    if args.restore_defaults_from_config or args.auto_uv_voltage_scan:
        if args.restore_defaults_from_config:
            afterburner_runtime_options = ensure_afterburner_root_configured(
                config_path,
                afterburner_runtime_options,
                gpu_index=gpu_index,
                interactive=sys.stdin.isatty(),
            )
        try:
            if args.restore_defaults_from_config:
                restore_afterburner_defaults_from_config(
                    gpu_index=gpu_index,
                    runtime_options=afterburner_runtime_options,
                    log=log,
                )
            elif args.auto_uv_voltage_scan:
                emit_json_event(
                    bool(args.json_events),
                    "auto_uv_start",
                    gpu_index=int(gpu_index),
                    algorithm="auto_uv3",
                )

                def _auto_uv_json_event(event, payload):
                    emit_json_event(bool(args.json_events), event, **dict(payload))

                def _dependency_json_event(payload):
                    emit_json_event(
                        bool(args.json_events),
                        "dependency_progress",
                        **dict(payload),
                    )

                try:
                    require_auto_uv_initial_check(gpu_index=gpu_index, log=log)
                except RuntimeError as exc:
                    raise NvmlError(str(exc)) from exc

                log("Auto-UV3: running the voltage-frequency undervolt main loop.")
                result = run_voltage_frequency_undervolt_main_loop(
                    gpu_index=gpu_index,
                    runtime_options=afterburner_runtime_options,
                    q2rtx_config=build_stability_config(
                        args,
                        gpu_index=gpu_index,
                        config_path=config_path,
                        auto_install_q2rtx=True,
                        progress_context="Auto-UV",
                        dependency_progress_callback=(
                            _dependency_json_event
                            if bool(args.json_events)
                            else None
                        ),
                        dependency_text_progress=not bool(args.json_events),
                    ),
                    log=log,
                    event_callback=_auto_uv_json_event
                    if bool(args.json_events)
                    else None,
                )
                emit_json_event(
                    bool(args.json_events),
                    "final_result",
                    voltage_mv=int(result.final_voltage_mv),
                    clock_mhz=int(result.lock_clock_mhz),
                    power_w=result.final_power_w,
                    temperature_c=result.final_temperature_c,
                    fan_pct=result.final_fan_speed_pct,
                    stop_reason=result.stop_reason,
                    failed_candidate_voltage_mv=result.failed_candidate_voltage_mv,
                )
                log(
                    "Auto-UV final state: "
                    f"{result.lock_clock_mhz}MHz@{result.final_voltage_mv}mV "
                    f"power={result.final_power_w if result.final_power_w is not None else 'n/a'}W "
                    f"temp={result.final_temperature_c if result.final_temperature_c is not None else 'n/a'}C "
                    f"fan={result.final_fan_speed_pct if result.final_fan_speed_pct is not None else 'n/a'}% "
                    f"stop_reason={result.stop_reason} "
                    f"failed_candidate={result.failed_candidate_voltage_mv if result.failed_candidate_voltage_mv is not None else 'none'}"
                )
        except AutoUvFinalChoiceDiscarded as exc:
            log(str(exc))
        except AutoUvError as exc:
            raise NvmlError(str(exc)) from exc
        return
    if args.dry_run:
        run_afterburner_dry_run(
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return

    fan_control_enabled = bool(args.silent_fan_curve)
    afterburner_root = str(
        afterburner_runtime_options.get("afterburner_root", "")
    ).strip()
    afterburner_profile = str(
        afterburner_runtime_options.get("afterburner_profile", "")
    ).strip()
    afterburner_device_profile = str(
        afterburner_runtime_options.get("afterburner_device_profile", "")
    ).strip()
    if not had_persisted_afterburner_root and not auto_uv_final_curve_available:
        afterburner_runtime_options = ensure_afterburner_root_configured(
            config_path,
            afterburner_runtime_options,
            gpu_index=gpu_index,
            interactive=sys.stdin.isatty(),
        )
        afterburner_root = str(
            afterburner_runtime_options.get("afterburner_root", "")
        ).strip()
        afterburner_profile = str(
            afterburner_runtime_options.get("afterburner_profile", "")
        ).strip()
        afterburner_device_profile = str(
            afterburner_runtime_options.get("afterburner_device_profile", "")
        ).strip()
        if afterburner_root and maybe_handle_first_time_afterburner_setup(
            argv=argv,
            journal_hours=journal_hours,
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
            program_file=__file__,
            prompt_yes_no=prompt_yes_no,
            log=log,
        ):
            return
    elif not afterburner_root and not auto_uv_final_curve_available:
        afterburner_runtime_options = ensure_afterburner_root_configured(
            config_path,
            afterburner_runtime_options,
            gpu_index=gpu_index,
            interactive=sys.stdin.isatty(),
        )
        afterburner_root = str(
            afterburner_runtime_options.get("afterburner_root", "")
        ).strip()
        afterburner_profile = str(
            afterburner_runtime_options.get("afterburner_profile", "")
        ).strip()
        afterburner_device_profile = str(
            afterburner_runtime_options.get("afterburner_device_profile", "")
        ).strip()
    if fan_control_enabled:
        auto_uv_fan_curve_path = default_user_config_dir() / "auto-uv-fan-curve.json"
        if auto_uv_fan_curve_path.is_file():
            try:
                auto_uv_fan_curve = load_auto_uv_fan_curve(fan_config)
            except FanCurveBlockedError as exc:
                auto_uv_fan_curve = None
                fan_control_enabled = False
                log(f"Manual fan control disabled by auto-UV safety guard: {exc}")
            except Exception as exc:
                auto_uv_fan_curve = None
                fan_control_enabled = False
                log(
                    "Manual fan control disabled because the auto-UV fan curve "
                    f"is present but invalid: path={auto_uv_fan_curve_path} error={exc}"
                )
            if fan_control_enabled and auto_uv_fan_curve is not None:
                fan_config = auto_uv_fan_curve["fan_config"]
            elif fan_control_enabled:
                fan_control_enabled = False
                log(
                    "Manual fan control disabled because the auto-UV fan curve "
                    f"file could not be loaded: path={auto_uv_fan_curve_path}"
                )
        elif afterburner_root:
            fan_config = load_runtime_afterburner_fan_config(
                fan_config,
                afterburner_root=afterburner_root,
                gpu_index=gpu_index,
            )

    nvml = ctypes.CDLL("libnvidia-ml.so.1")

    c_uint = ctypes.c_uint
    c_void_p = ctypes.c_void_p

    nvml.nvmlInit_v2.restype = ctypes.c_int
    nvml.nvmlShutdown.restype = ctypes.c_int
    nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [c_uint, ctypes.POINTER(c_void_p)]
    nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
    nvml.nvmlDeviceGetTemperature.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetTemperature.restype = ctypes.c_int
    nvml.nvmlDeviceGetNumFans.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetNumFans.restype = ctypes.c_int
    nvml.nvmlDeviceGetFanSpeed.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetFanSpeed.restype = ctypes.c_int
    nvml.nvmlDeviceGetFanSpeed_v2.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetFanSpeed_v2.restype = ctypes.c_int
    nvml.nvmlDeviceGetPowerUsage.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetPowerUsage.restype = ctypes.c_int
    nvml.nvmlDeviceGetClockInfo.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetClockInfo.restype = ctypes.c_int
    if hasattr(nvml, "nvmlDeviceGetMinMaxFanSpeed"):
        nvml.nvmlDeviceGetMinMaxFanSpeed.argtypes = [
            c_void_p,
            ctypes.POINTER(c_uint),
            ctypes.POINTER(c_uint),
        ]
        nvml.nvmlDeviceGetMinMaxFanSpeed.restype = ctypes.c_int

    gpu_index = gpu_config["index"]
    poll_interval_s = fan_config["poll_interval_s"]
    curve = []
    effective_manual_curve = []
    hysteresis_c = 0.0
    mode = "linear"
    min_fan_speed_pct = 0
    max_fan_speed_pct = 100
    effective_min_fan_speed_pct = 0
    effective_max_fan_speed_pct = 100
    max_step_up_pct_per_s = 0.0
    max_step_down_pct_per_s = 0.0
    manual_enable_temp_c = 0.0
    auto_restore_temp_c = 0.0
    emergency_auto_override_temp_c = 80.0
    emergency_auto_resume_temp_c = 75.0
    force_update_every_poll = False
    if fan_control_enabled:
        nvml.nvmlDeviceSetFanSpeed_v2.argtypes = [c_void_p, c_uint, c_uint]
        nvml.nvmlDeviceSetFanSpeed_v2.restype = ctypes.c_int
        nvml.nvmlDeviceSetDefaultFanSpeed_v2.argtypes = [c_void_p, c_uint]
        nvml.nvmlDeviceSetDefaultFanSpeed_v2.restype = ctypes.c_int
        curve = [tuple(point) for point in fan_config["curve"]]
        validate_curve(curve)
        hysteresis_c = float(fan_config["hysteresis_c"])
        mode = str(fan_config["mode"])
        min_fan_speed_pct = int(fan_config["min_fan_speed_pct"])
        max_fan_speed_pct = int(fan_config["max_fan_speed_pct"])
        max_step_up_pct_per_s = float(fan_config["max_step_up_pct_per_s"])
        max_step_down_pct_per_s = float(fan_config["max_step_down_pct_per_s"])
        manual_enable_temp_c = float(fan_config["manual_enable_temp_c"])
        auto_restore_temp_c = float(fan_config["auto_restore_temp_c"])
        emergency_auto_override_temp_c = float(
            fan_config.get("emergency_auto_override_temp_c", 80.0)
        )
        emergency_auto_resume_temp_c = float(
            fan_config.get("emergency_auto_resume_temp_c", 75.0)
        )
        force_update_every_poll = bool(fan_config["force_update_every_poll"])
    enable_persistence_mode = gpu_config["enable_persistence_mode"]
    translated_gpu_policy = None
    afterburner_source = None
    afterburner_profile_settings = None
    auto_uv_final_curve = None
    vf_apply_result = None
    active_vf_curve_source = None
    auto_uv_profile_gpu_policy = None
    clock_ceiling_controller = None
    vf_expected_samples = []
    last_vf_reapply_monotonic = 0.0
    vf_reapply_cooldown_s = max(float(poll_interval_s), 10.0)

    device = c_void_p()
    check(nvml.nvmlInit_v2(), "nvmlInit_v2")
    check(
        nvml.nvmlDeviceGetHandleByIndex_v2(c_uint(gpu_index), ctypes.byref(device)),
        "nvmlDeviceGetHandleByIndex_v2",
    )
    voltage_reader = create_hidden_voltage_reader(gpu_index=gpu_index)
    vf_curve_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    try:
        gpu_policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
    except Exception as exc:
        gpu_policy_controller = None
        log(f"Linux GPU policy helper unavailable: {exc}")

    try:
        auto_uv_final_curve = load_auto_uv_final_curve(auto_uv_profile_selector)
    except Exception as exc:
        auto_uv_final_curve = None
        log(f"Skipping auto-UV final curve: error={exc}")
    if afterburner_root:
        try:
            afterburner_source = resolve_afterburner_vf_source(
                afterburner_root=afterburner_root,
                section=afterburner_profile or None,
                device_profile_hint=afterburner_device_profile or None,
                dangerously_skip_validation=bool(
                    afterburner_runtime_options.get("dangerously_skip_validation")
                ),
            )
        except Exception as exc:
            log(f"Skipping Afterburner source resolve: error={exc}")
        else:
            if afterburner_source.get("dangerously_skip_validation"):
                log(
                    "Afterburner validation override enabled: skipping the default "
                    "flat-tail and undervolt checks for the saved profile."
                )
            if gpu_policy_controller is not None:
                try:
                    afterburner_profile_settings = load_afterburner_profile_settings(
                        profile_path=afterburner_source["profile_path"],
                        section=afterburner_source["section"],
                    )
                    translated_gpu_policy = translate_afterburner_gpu_policy(
                        afterburner_profile_settings,
                        power_limits=gpu_policy_controller.query_power_limits(),
                        power_limit_cap_w=afterburner_runtime_options[
                            "power_limit_override_w"
                        ],
                    )
                except Exception as exc:
                    translated_gpu_policy = None
                    log(
                        "Skipping Afterburner GPU policy translate: "
                        f"section={afterburner_source['section']} error={exc}"
                    )

    startup_power_limit_w = None
    if (
        translated_gpu_policy is not None
        and translated_gpu_policy.get("power_limit_w") is not None
    ):
        startup_power_limit_w = translated_gpu_policy["power_limit_w"]
    apply_gpu_base_policy(
        gpu_index=gpu_index,
        enable_persistence_mode=enable_persistence_mode,
        power_limit_w=startup_power_limit_w,
    )
    if (
        translated_gpu_policy is not None
        and gpu_policy_controller is not None
        and afterburner_source is not None
    ):
        try:
            apply_translated_gpu_policy(gpu_policy_controller, translated_gpu_policy)
        except Exception as exc:
            log(
                "Skipping Afterburner GPU policy apply: "
                f"section={afterburner_source['section']} error={exc}"
            )
        else:
            log(
                f"Applied Afterburner GPU policy: section={afterburner_source['section']} "
                f"{describe_translated_gpu_policy(translated_gpu_policy)}."
            )

    if vf_curve_reader is not None:
        afterburner_curve_applied = False
        auto_uv_curve_applied = False

        def _apply_auto_uv_final_curve() -> bool:
            nonlocal auto_uv_profile_gpu_policy
            nonlocal auto_uv_final_curve
            nonlocal vf_apply_result
            nonlocal vf_expected_samples
            nonlocal clock_ceiling_controller
            nonlocal active_vf_curve_source
            if auto_uv_final_curve is None:
                return False
            try:
                apply_plan(vf_curve_reader, auto_uv_final_curve["plan"])
                vf_curve_reader.refresh_points()
            except Exception as exc:
                log(
                    "Skipping auto-UV final curve apply: "
                    f"path={auto_uv_final_curve['path']} error={exc}"
                )
                auto_uv_final_curve = None
                return False
            else:
                vf_apply_result = {
                    "source": "auto-uv-final",
                    "plan": auto_uv_final_curve["plan"],
                    "path": auto_uv_final_curve["path"],
                }
                active_vf_curve_source = "auto-uv-final"
                vf_expected_samples = select_expected_vf_samples(
                    vf_apply_result["plan"]
                )
                log(
                    "Applied auto-UV final curve: "
                    f"path={auto_uv_final_curve['path']} "
                    f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
                    f"{auto_uv_final_curve['candidate_voltage_mv']}mV "
                    f"points={len(auto_uv_final_curve['plan'])}."
                )
                auto_uv_profile_gpu_policy = _apply_auto_uv_profile_memory_offset(
                    profile_label="auto-UV final curve",
                    memory_offset_mhz=auto_uv_final_curve.get("memory_offset_mhz"),
                    gpu_policy_controller=gpu_policy_controller,
                )
                if auto_uv_profile_gpu_policy:
                    log(
                        "Applied auto-UV profile memory offset: "
                        f"{int(auto_uv_profile_gpu_policy['mem_clk_vf_offset_mhz']):+d}MHz."
                    )
                try:
                    clock_ceiling_controller = FlattenedClockCeilingController(
                        flatten_target=auto_uv_final_curve["flatten_target"],
                        policy_controller=gpu_policy_controller,
                    )
                    clock_ceiling_controller.apply()
                except Exception as exc:
                    clock_ceiling_controller = None
                    log(
                        "Skipping auto-UV clock ceiling: "
                        f"path={auto_uv_final_curve['path']} error={exc}"
                    )
                else:
                    log(
                        "Configured auto-UV clock ceiling: "
                        f"{clock_ceiling_controller.describe()}."
                    )
                return True

        def _apply_afterburner_curve() -> bool:
            nonlocal vf_apply_result
            nonlocal vf_expected_samples
            nonlocal clock_ceiling_controller
            nonlocal active_vf_curve_source
            if afterburner_source is None:
                return False
            try:
                vf_apply_result = apply_afterburner_curve_to_reader(
                    vf_curve_reader,
                    profile_path=afterburner_source["profile_path"],
                    section=afterburner_source["section"],
                    gpu_policy=translated_gpu_policy,
                    preserve_base_below_mv=afterburner_runtime_options[
                        "preserve_base_below_mv"
                    ],
                )
            except Exception as exc:
                log(
                    "Skipping Afterburner VF curve apply: "
                    f"section={afterburner_source['section']} error={exc}"
                )
                return False
            else:
                log(
                    f"Applied Afterburner VF curve: section={afterburner_source['section']} "
                    f"matched={len(vf_apply_result['plan'])} "
                    f"changed={len(vf_apply_result['changed_points'])} "
                    f"mode={vf_apply_result['translation_mode']} "
                    f"origin={vf_apply_result['translation_origin']} "
                    f"linux_profile={vf_apply_result['translated_linux_profile_path']}."
                )
                active_vf_curve_source = "afterburner"
                vf_expected_samples = select_expected_vf_samples(
                    vf_apply_result["plan"]
                )
                flatten_target = derive_afterburner_dynamic_lock(
                    vf_apply_result["materialization"]["points"]
                )
                if flatten_target is None:
                    log(
                        f"Skipping Afterburner clock ceiling: section={afterburner_source['section']} "
                        "no flattened V/F target was detected."
                    )
                else:
                    try:
                        clock_ceiling_controller = FlattenedClockCeilingController(
                            flatten_target=flatten_target,
                            policy_controller=gpu_policy_controller,
                        )
                        clock_ceiling_controller.apply()
                    except Exception as exc:
                        clock_ceiling_controller = None
                        log(
                            "Skipping Afterburner clock ceiling: "
                            f"section={afterburner_source['section']} error={exc}"
                        )
                    else:
                        log(
                            f"Configured Afterburner clock ceiling: section={afterburner_source['section']} "
                            f"{clock_ceiling_controller.describe()}."
                        )
                return True

        if prefer_afterburner_curve:
            afterburner_curve_applied = _apply_afterburner_curve()
            if afterburner_curve_applied and auto_uv_final_curve is not None:
                log(
                    "Auto-UV final curve is present but skipped because "
                    "--prefer-afterburner-curve was requested."
                )
            if not afterburner_curve_applied:
                log(
                    "--prefer-afterburner-curve requested, but no usable Afterburner "
                    "V/F curve was applied; trying Auto-UV final curve fallback."
                )
                auto_uv_curve_applied = _apply_auto_uv_final_curve()
        else:
            auto_uv_curve_applied = _apply_auto_uv_final_curve()
            if not auto_uv_curve_applied:
                afterburner_curve_applied = _apply_afterburner_curve()

    fan_count = c_uint()
    check(
        nvml.nvmlDeviceGetNumFans(device, ctypes.byref(fan_count)),
        "nvmlDeviceGetNumFans",
    )

    if fan_control_enabled and fan_count.value == 0:
        raise NvmlError("GPU reports zero controllable fans")

    device_min_fan_speed_pct = None
    device_max_fan_speed_pct = None
    if fan_control_enabled and hasattr(nvml, "nvmlDeviceGetMinMaxFanSpeed"):
        fan_min = c_uint()
        fan_max = c_uint()
        rc = nvml.nvmlDeviceGetMinMaxFanSpeed(
            device,
            ctypes.byref(fan_min),
            ctypes.byref(fan_max),
        )
        if rc == NVML_SUCCESS and fan_max.value >= fan_min.value:
            device_min_fan_speed_pct = fan_min.value
            device_max_fan_speed_pct = fan_max.value

    if fan_control_enabled:
        effective_min_fan_speed_pct = min_fan_speed_pct
        effective_max_fan_speed_pct = max_fan_speed_pct
        if device_min_fan_speed_pct is not None:
            effective_min_fan_speed_pct = max(
                effective_min_fan_speed_pct, device_min_fan_speed_pct
            )
        if device_max_fan_speed_pct is not None:
            effective_max_fan_speed_pct = min(
                effective_max_fan_speed_pct, device_max_fan_speed_pct
            )
        if effective_max_fan_speed_pct < effective_min_fan_speed_pct:
            raise NvmlError("effective fan speed range is invalid")

    restored = False

    def restore_default():
        nonlocal restored
        if restored:
            return
        restored = True
        if fan_control_enabled:
            for fan_idx in range(fan_count.value):
                nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx))
        if clock_ceiling_controller is not None:
            clock_ceiling_controller.close()
        if vf_curve_reader is not None:
            vf_curve_reader.close()
        if gpu_policy_controller is not None:
            gpu_policy_controller.close()
        nvml.nvmlShutdown()

    def stop(_signum, _frame):
        restore_default()
        sys.exit(0)

    atexit.register(restore_default)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    last_speed = None
    last_set_temp_c = None
    last_update_time = time.monotonic()
    manual_mode_active = False
    hot_auto_mode_active = False
    if fan_control_enabled:
        print(
            f"Controlling GPU {gpu_index} with {fan_count.value} fan(s), "
            f"mode={mode}, hysteresis={hysteresis_c} C, "
            f"manual-limits={effective_min_fan_speed_pct}-{effective_max_fan_speed_pct}%, "
            f"manual-enable={manual_enable_temp_c} C, auto-restore={auto_restore_temp_c} C, "
            f"emergency-auto={emergency_auto_override_temp_c} C/{emergency_auto_resume_temp_c} C. "
            "Press Ctrl-C to restore auto mode.",
            flush=True,
        )
    else:
        print(
            f"Running GPU {gpu_index} telemetry and V/F policy loop with fan control disabled. "
            "Use --silent-fan-curve to let PenguinBurner control fans. Press Ctrl-C to exit.",
            flush=True,
        )
    startup_gpu_policy = translated_gpu_policy or auto_uv_profile_gpu_policy or {
        "power_limit_w": startup_power_limit_w
    }
    log(
        f"GPU policy: persistence={'on' if enable_persistence_mode else 'off'}, "
        f"{describe_translated_gpu_policy(startup_gpu_policy)}."
    )
    log(f"Config file: {config_path}")
    if active_vf_curve_source == "auto-uv-final" and auto_uv_final_curve is not None:
        log(
            "Active VF curve source: auto-UV final "
            f"path={auto_uv_final_curve['path']} "
            f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
            f"{auto_uv_final_curve['candidate_voltage_mv']}mV; "
            "Afterburner V/F import skipped."
        )
    elif active_vf_curve_source == "afterburner" and prefer_afterburner_curve:
        log(
            "Active VF curve source: Afterburner import requested by --prefer-afterburner-curve."
        )
    if afterburner_source is not None:
        flatten_target = afterburner_source["section_info"].get("flatten_target")
        flatten_text = (
            describe_afterburner_dynamic_lock(flatten_target)
            if flatten_target is not None
            else "none"
        )
        log(
            "Afterburner import: "
            f"root={afterburner_source['afterburner_root']} "
            f"device_profile={afterburner_source['profile_path'].name} "
            f"profile={afterburner_source['section']} "
            f"flatten-target={flatten_text}."
        )
        log(
            "Afterburner flatten validation: "
            f"{describe_afterburner_flatten_validation(afterburner_source['section_info'].get('flatten_validation'))}."
        )
        if afterburner_profile_settings is not None:
            log(
                "Afterburner parsed settings: "
                f"{describe_afterburner_profile_settings(afterburner_profile_settings)}."
            )
    if vf_curve_reader is not None:
        vf_summary = vf_curve_reader.summary()
        log(
            f"Linux NVAPI VF curve: "
            f"active-points={vf_summary['active_points']}, "
            f"editable-core-points={vf_summary['editable_core_points']}."
        )
    if device_min_fan_speed_pct is not None and device_max_fan_speed_pct is not None:
        log(
            f"Device fan limits reported by NVML: "
            f"{device_min_fan_speed_pct}-{device_max_fan_speed_pct}%."
        )
    if fan_control_enabled:
        effective_manual_curve = build_effective_manual_curve(
            curve=curve,
            manual_enable_temp_c=manual_enable_temp_c,
            effective_min_fan_speed_pct=effective_min_fan_speed_pct,
            effective_max_fan_speed_pct=effective_max_fan_speed_pct,
            mode=mode,
        )
        curve_source = fan_config.get("curve_source")
        if curve_source:
            if str(curve_source) == "auto-uv":
                log(
                    "Fan curve source: auto-UV "
                    f"path={fan_config.get('curve_source_path', 'n/a')} "
                    f"generated={fan_config.get('curve_source_generated_at', 'n/a')} "
                    f"target-temp={fan_config.get('curve_source_target_load_temp_c', 'n/a')}C "
                    f"observed-load={fan_config.get('curve_source_loaded_temperature_c', 'n/a')}C "
                    f"observed-fan={fan_config.get('curve_source_observed_fan_speed_pct', 'n/a')}%."
                )
            else:
                curve_flags_u32 = int(fan_config.get("curve_source_flags_u32", 0))
                curve_period_ms = int(
                    fan_config.get(
                        "curve_source_period_ms", int(round(poll_interval_s * 1000))
                    )
                )
                log(
                    f"Fan curve source: {curve_source} "
                    f"period={curve_period_ms}ms flags=0x{curve_flags_u32:08x}."
                )
        log(f"Fan curve points: {format_curve_points(curve)}")
        log(
            f"Effective manual fan curve: {format_curve_points(effective_manual_curve)}"
        )
        if fan_config.get("curve_override_zero_with_hardware_curve"):
            behavior_parts = ["zero-rpm zone uses hardware auto curve"]
            if fan_config.get("curve_hardware_auto_below_device_min"):
                behavior_parts.append(
                    "below device manual minimum uses hardware auto curve"
                )
            takeover_temp_c = fan_config.get("curve_manual_takeover_temp_c")
            if takeover_temp_c is not None:
                behavior_parts.append(
                    f"manual takeover near {float(takeover_temp_c):.2f}C"
                )
            log("Fan curve behavior: " + "; ".join(behavior_parts) + ".")
        log(
            "Silent fan curve guardrail: "
            f"hardware auto above {float(emergency_auto_override_temp_c):.0f}C, "
            f"resume manual below {float(emergency_auto_resume_temp_c):.0f}C."
        )
    else:
        log(
            "Fan control disabled: hardware/driver fan policy remains active; "
            "fan curve files are ignored unless --silent-fan-curve is used."
        )
    if clock_ceiling_controller is not None:
        log(f"Clock ceiling policy: {clock_ceiling_controller.describe()}.")

    while True:
        loop_started = time.monotonic()
        temp = c_uint()
        check(
            nvml.nvmlDeviceGetTemperature(
                device, c_uint(NVML_TEMPERATURE_GPU), ctypes.byref(temp)
            ),
            "nvmlDeviceGetTemperature",
        )

        current_temp_c = float(temp.value)
        power_draw_w = get_power_draw_w(nvml, device)

        telemetry_text = format_telemetry(
            nvml,
            device,
            fan_count.value,
            current_temp_c,
            voltage_reader=voltage_reader,
            vf_curve_reader=vf_curve_reader,
            gpu_policy_controller=gpu_policy_controller,
            power_draw_w=power_draw_w,
            clock_ceiling_controller=clock_ceiling_controller,
        )
        if (
            vf_curve_reader is not None
            and vf_expected_samples
            and vf_apply_result is not None
        ):
            vf_mismatches = detect_vf_curve_reset(vf_curve_reader, vf_expected_samples)
            if (
                vf_mismatches
                and (loop_started - last_vf_reapply_monotonic) >= vf_reapply_cooldown_s
            ):
                try:
                    apply_plan(vf_curve_reader, vf_apply_result["plan"])
                    vf_curve_reader.refresh_points()
                except Exception as exc:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log(f"{timestamp} event=vf-curve-reapply-error error={exc}")
                else:
                    last_vf_reapply_monotonic = loop_started
                    mismatch_preview = format_vf_curve_mismatch_preview(
                        vf_mismatches
                    )
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log(
                        f"{timestamp} {telemetry_text} "
                        f"event=vf-curve-reapplied mismatches={len(vf_mismatches)} "
                        f"samples={mismatch_preview}"
                    )
        if not fan_control_enabled:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"{timestamp} {telemetry_text} fan_control=disabled")
            time.sleep(poll_interval_s)
            continue
        fan_curve_state_text = describe_fan_curve_state(
            current_temp_c=current_temp_c,
            effective_curve=effective_manual_curve,
            manual_mode_active=manual_mode_active,
            emergency_auto_mode_active=hot_auto_mode_active,
            emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
        )
        if hot_auto_mode_active and current_temp_c > emergency_auto_resume_temp_c:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} fan_mode=auto reason=emergency-override"
            )
            time.sleep(poll_interval_s)
            continue

        if hot_auto_mode_active and current_temp_c <= emergency_auto_resume_temp_c:
            hot_auto_mode_active = False
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} event=emergency-override-cleared"
            )

        if current_temp_c > emergency_auto_override_temp_c:
            if manual_mode_active:
                for fan_idx in range(fan_count.value):
                    check(
                        nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx)),
                        f"nvmlDeviceSetDefaultFanSpeed_v2 fan {fan_idx}",
                    )
                manual_mode_active = False
                last_speed = None
                last_set_temp_c = None
            hot_auto_mode_active = True
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} "
                f"event=restoring-auto-mode reason=emergency-override"
            )
            time.sleep(poll_interval_s)
            continue

        if not manual_mode_active and current_temp_c < manual_enable_temp_c:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} fan_mode=auto")
            time.sleep(poll_interval_s)
            continue

        if not manual_mode_active:
            manual_mode_active = True
            last_speed = None
            last_set_temp_c = None
            last_update_time = loop_started
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} event=entering-manual-mode"
            )

        if current_temp_c <= auto_restore_temp_c:
            for fan_idx in range(fan_count.value):
                check(
                    nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx)),
                    f"nvmlDeviceSetDefaultFanSpeed_v2 fan {fan_idx}",
                )
            manual_mode_active = False
            last_speed = None
            last_set_temp_c = None
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} event=restoring-auto-mode"
            )
            time.sleep(poll_interval_s)
            continue

        raw_target_speed = speed_for_temp(current_temp_c, curve, mode=mode)
        raw_target_speed = clamp(
            raw_target_speed,
            effective_min_fan_speed_pct,
            effective_max_fan_speed_pct,
        )

        hysteresis_target_speed = apply_hysteresis(
            current_temp_c=current_temp_c,
            raw_target_speed=raw_target_speed,
            last_temp_c=last_set_temp_c,
            last_speed=last_speed,
            hysteresis_c=hysteresis_c,
        )

        limited_target_speed = limit_speed_change(
            target_speed=hysteresis_target_speed,
            last_speed=last_speed,
            elapsed_s=loop_started - last_update_time,
            max_step_up_pct_per_s=max_step_up_pct_per_s,
            max_step_down_pct_per_s=max_step_down_pct_per_s,
        )
        target_speed = round(
            clamp(
                limited_target_speed,
                effective_min_fan_speed_pct,
                effective_max_fan_speed_pct,
            )
        )

        if force_update_every_poll or target_speed != last_speed:
            for fan_idx in range(fan_count.value):
                check(
                    nvml.nvmlDeviceSetFanSpeed_v2(
                        device, c_uint(fan_idx), c_uint(target_speed)
                    ),
                    f"nvmlDeviceSetFanSpeed_v2 fan {fan_idx}",
                )
            last_set_temp_c = current_temp_c
            last_speed = target_speed
            last_update_time = loop_started

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log(
            f"{timestamp} {telemetry_text} {fan_curve_state_text} "
            f"target={target_speed}% curve={raw_target_speed:.1f}% "
            f"hyst={hysteresis_target_speed:.1f}% fan_mode=manual"
        )

        time.sleep(poll_interval_s)


def _runtime_profile_selector_from_argv(argv) -> str:
    return runtime_profile_selector_from_argv(argv)


def _runtime_profile_selector_allows_unverified_from_argv(argv) -> bool:
    return runtime_profile_selector_allows_unverified_from_argv(argv)


def cli_main() -> int:
    return run_penguin_burner_cli(program_file=__file__, main_callback=main)


if __name__ == "__main__":
    raise SystemExit(cli_main())
