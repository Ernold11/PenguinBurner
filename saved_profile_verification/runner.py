from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import tempfile

from afterburner.import_vf_curve import (
    apply_afterburner_curve_to_reader,
    apply_plan,
    backup_current_offsets,
    restore_offsets,
)
from afterburner.vfcurve import (
    derive_afterburner_dynamic_lock,
    load_afterburner_profile_settings,
    resolve_afterburner_vf_source,
)
from hidden_nvapi_vf import create_hidden_vf_curve_reader
from nvml_gpu_policy import (
    NvmlGpuPolicyController,
    apply_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from penguin_burner_errors import NvmlError
from runtime_debug import log as runtime_log
from runtime_gpu_control import FlattenedClockCeilingController
from runtime_service import stop_existing_penguin_burner_runtime
from runtime_stability_test import (
    build_cuda_stability_config,
    build_stability_config,
    run_stability_test,
    stability_workload_label,
    stability_workload_selection,
    stability_workload_split_label,
)
from saved_uv_profiles import (
    apply_auto_uv_profile_memory_offset,
    load_auto_uv_final_curve,
    mark_auto_uv_profile_verification_failed,
    mark_auto_uv_profile_verified,
)
from stability.q2rtx import (
    StabilityTestError,
    attach_stdout_progress,
    build_long_stability_test_config,
    print_q2rtx_stability_result,
    run_cuda_stability_test,
    run_q2rtx_stability_test,
)

from .metrics import profile_verification_metrics_from_result
from .rules import (
    apply_and_verify_profile_vf_plan as verify_profile_vf_plan,
    base_vf_plan_from_profile_plan,
    profile_needs_verify_baseline,
    profile_verification_baseline_duration_s,
    profile_verification_failure_blocks_apply,
    profile_verification_voltage_abort_callback,
    stability_stop_request_abort_callback,
    stability_stop_request_path,
)


@dataclass(slots=True)
class ProfileVerificationDependencies:
    stop_existing_penguin_burner_runtime: Callable = (
        stop_existing_penguin_burner_runtime
    )
    create_hidden_vf_curve_reader: Callable = create_hidden_vf_curve_reader
    gpu_policy_controller_factory: Callable = NvmlGpuPolicyController
    backup_current_offsets: Callable = backup_current_offsets
    restore_offsets: Callable = restore_offsets
    apply_plan: Callable = apply_plan
    load_auto_uv_final_curve: Callable = load_auto_uv_final_curve
    build_stability_config: Callable = build_stability_config
    build_long_stability_test_config: Callable = build_long_stability_test_config
    run_q2rtx_stability_test: Callable = run_q2rtx_stability_test
    attach_stdout_progress: Callable = attach_stdout_progress
    print_q2rtx_stability_result: Callable = print_q2rtx_stability_result
    log: Callable[[str], None] = runtime_log


def run_profile_verification(
    args,
    *,
    gpu_index,
    config_path,
    afterburner_runtime_options,
    dependencies: ProfileVerificationDependencies | None = None,
):
    deps = dependencies or ProfileVerificationDependencies()
    selector = str(args.auto_uv_profile or "").strip()
    prefer_afterburner_curve = bool(args.prefer_afterburner_curve)
    if not selector and not prefer_afterburner_curve:
        run_stability_test(args, gpu_index=gpu_index, config_path=config_path)
        return

    deps.stop_existing_penguin_burner_runtime(log=deps.log)
    vf_curve_reader = deps.create_hidden_vf_curve_reader(gpu_index=gpu_index)
    if vf_curve_reader is None:
        raise NvmlError("could not open the live Nvidia V/F curve reader")

    clock_ceiling_controller: object | None = None

    def close_clock_ceiling() -> None:
        nonlocal clock_ceiling_controller
        if clock_ceiling_controller is None:
            return
        try:
            clock_ceiling_controller.close()
        except Exception as exc:
            deps.log(f"Warning: failed to reset verification clock lock: {exc}")
        clock_ceiling_controller = None

    with contextlib.ExitStack() as stack:
        stack.callback(vf_curve_reader.close)

        try:
            gpu_policy_controller = deps.gpu_policy_controller_factory(
                gpu_index=gpu_index
            )
        except Exception as exc:
            gpu_policy_controller = None
            deps.log(f"Linux GPU policy helper unavailable: {exc}")
        if gpu_policy_controller is not None:
            stack.callback(gpu_policy_controller.close)

        backup_file = tempfile.NamedTemporaryFile(
            prefix="penguin-burner-verify-", suffix=".json", delete=False
        )
        backup_file.close()
        backup_path = Path(backup_file.name)
        deps.backup_current_offsets(
            vf_curve_reader,
            backup_path,
            policy_controller=gpu_policy_controller,
        )
        stack.callback(_restore_and_unlink_backup, deps, vf_curve_reader, backup_path, gpu_policy_controller)
        stack.callback(close_clock_ceiling)

        if prefer_afterburner_curve:
            label, flatten_target, verify_plan = apply_verify_afterburner_profile(
                vf_curve_reader,
                gpu_policy_controller,
                afterburner_runtime_options,
                gpu_index=gpu_index,
                dependencies=deps,
            )
        else:
            label, flatten_target, verify_plan = apply_verify_auto_uv_profile(
                vf_curve_reader,
                selector,
                gpu_policy_controller,
                dependencies=deps,
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
                deps.log(f"Skipping verification clock lock: {exc}")
            else:
                deps.log(
                    "Configured verification clock lock: "
                    f"{clock_ceiling_controller.describe()}."
                )
                apply_and_verify_profile_vf_plan(
                    vf_curve_reader,
                    verify_plan,
                    context="selected profile after clock lock",
                    dependencies=deps,
                )
                deps.log(
                    "Re-applied profile V/F curve after verification clock lock: "
                    f"points={len(verify_plan)}."
                )

        duration_s = int(args.stability_seconds)
        include_q2rtx, include_cuda = stability_workload_selection(args)
        workload_label = stability_workload_label(
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        split_label = stability_workload_split_label(
            duration_s,
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        deps.log(f"Profile verification workload split: {split_label}.")
        deps.log(
            "Profile verification starting: "
            f"profile={label} duration={duration_s}s workload={workload_label}."
        )
        stability_config = (
            deps.build_stability_config(
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
        stability_config = deps.build_long_stability_test_config(
            stability_config,
            total_duration_s=duration_s,
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        if flatten_target is not None:
            stability_config.abort_callback = profile_verification_voltage_abort_callback(
                flatten_target,
                previous_callback=stability_config.abort_callback,
            )
        stop_request_path = stability_stop_request_path(args)
        if stop_request_path is not None:
            stability_config.abort_callback = stability_stop_request_abort_callback(
                stop_request_path,
                previous_callback=stability_config.abort_callback,
            )
        deps.attach_stdout_progress(stability_config)
        try:
            result = (
                deps.run_q2rtx_stability_test(stability_config)
                if include_q2rtx
                else run_cuda_stability_test(stability_config)
            )
        except StabilityTestError as exc:
            raise NvmlError(f"stability test configuration error: {exc}") from exc
        deps.print_q2rtx_stability_result(result)
        if not result.success:
            if (
                not prefer_afterburner_curve
                and profile_verification_failure_blocks_apply(result.reason)
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
                        deps.log(
                            "Marked profile verification failed: "
                            f"path={failed_path} reason={result.reason}"
                        )
                except Exception as exc:
                    deps.log(f"Warning: failed to mark profile verification failed: {exc}")
            raise NvmlError(
                f"profile verification failed: {result.reason}; log={result.log_path}"
            )
        base_metrics = None
        if not prefer_afterburner_curve and profile_needs_verify_baseline(selector):
            close_clock_ceiling()
            try:
                base_plan = base_vf_plan_from_profile_plan(verify_plan)
            except Exception as exc:
                deps.log(f"Profile verification baseline probe skipped: {exc}")
            else:
                base_metrics = run_profile_verification_baseline_probe(
                    args,
                    gpu_index=gpu_index,
                    config_path=config_path,
                    base_plan=base_plan,
                    gpu_policy_controller=gpu_policy_controller,
                    duration_s=profile_verification_baseline_duration_s(duration_s),
                    include_q2rtx=include_q2rtx,
                    include_cuda=include_cuda,
                    dependencies=deps,
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
                metrics=profile_verification_metrics_from_result(result),
                base_metrics=base_metrics,
            )
            deps.log(f"Marked profile verified: path={verified_path}")
        deps.log(f"Profile verification passed: profile={label}.")


def _restore_and_unlink_backup(
    deps: ProfileVerificationDependencies,
    vf_curve_reader,
    backup_path: Path,
    gpu_policy_controller,
) -> None:
    try:
        deps.restore_offsets(
            vf_curve_reader,
            backup_path,
            policy_controller=gpu_policy_controller,
        )
        deps.log("Restored V/F offsets after profile verification.")
    except Exception as exc:
        deps.log(f"Warning: failed to restore V/F offsets after verification: {exc}")
    try:
        backup_path.unlink(missing_ok=True)
    except OSError:
        pass


def apply_verify_auto_uv_profile(
    vf_curve_reader,
    selector: str,
    gpu_policy_controller,
    *,
    dependencies: ProfileVerificationDependencies | None = None,
):
    deps = dependencies or ProfileVerificationDependencies()
    auto_uv_final_curve = deps.load_auto_uv_final_curve(
        selector,
        allow_unverified=True,
    )
    if auto_uv_final_curve is None:
        raise NvmlError("Auto-UV profile not found")
    apply_and_verify_profile_vf_plan(
        vf_curve_reader,
        auto_uv_final_curve["plan"],
        context="selected profile",
        dependencies=deps,
    )
    label = (
        f"auto-UV:{auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV"
    )
    deps.log(
        "Applied profile for verification: "
        f"path={auto_uv_final_curve['path']} "
        f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV "
        f"points={len(auto_uv_final_curve['plan'])}."
    )
    memory_policy = apply_auto_uv_profile_memory_offset(
        profile_label=label,
        memory_offset_mhz=auto_uv_final_curve.get("memory_offset_mhz"),
        gpu_policy_controller=gpu_policy_controller,
    )
    if memory_policy:
        deps.log(
            "Applied profile memory offset for verification: "
            f"{int(memory_policy['mem_clk_vf_offset_mhz']):+d}MHz."
        )
    return label, auto_uv_final_curve["flatten_target"], auto_uv_final_curve["plan"]


def apply_and_verify_profile_vf_plan(
    vf_curve_reader,
    plan: list[dict],
    *,
    context: str,
    dependencies: ProfileVerificationDependencies | None = None,
) -> None:
    deps = dependencies or ProfileVerificationDependencies()
    verify_profile_vf_plan(
        vf_curve_reader,
        plan,
        context=context,
        apply_plan_fn=deps.apply_plan,
    )


def run_profile_verification_baseline_probe(
    args,
    *,
    gpu_index,
    config_path,
    base_plan: list[dict],
    gpu_policy_controller,
    duration_s: int,
    include_q2rtx: bool,
    include_cuda: bool,
    dependencies: ProfileVerificationDependencies | None = None,
) -> dict | None:
    deps = dependencies or ProfileVerificationDependencies()
    try:
        deps.log(
            "Profile verification baseline probe starting: "
            f"duration={int(duration_s)}s "
            f"{stability_workload_split_label(duration_s, include_q2rtx=include_q2rtx, include_cuda=include_cuda)}."
        )
        baseline_reader = deps.create_hidden_vf_curve_reader(gpu_index=gpu_index)
        if baseline_reader is None:
            raise NvmlError("could not open the live Nvidia V/F curve reader")
        try:
            apply_and_verify_profile_vf_plan(
                baseline_reader,
                base_plan,
                context="baseline profile",
                dependencies=deps,
            )
        finally:
            baseline_reader.close()
        if gpu_policy_controller is not None:
            try:
                gpu_policy_controller.apply_clock_offsets(mem_clk_vf_offset_mhz=0)
            except Exception as exc:
                deps.log(f"Warning: failed to reset memory offset for baseline probe: {exc}")
        stability_config = (
            deps.build_stability_config(
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
        stability_config = deps.build_long_stability_test_config(
            stability_config,
            total_duration_s=int(duration_s),
            include_q2rtx=include_q2rtx,
            include_cuda=include_cuda,
        )
        stop_request_path = stability_stop_request_path(args)
        if stop_request_path is not None:
            stability_config.abort_callback = stability_stop_request_abort_callback(
                stop_request_path,
                previous_callback=stability_config.abort_callback,
            )
        deps.attach_stdout_progress(stability_config)
        result = (
            deps.run_q2rtx_stability_test(stability_config)
            if include_q2rtx
            else run_cuda_stability_test(stability_config)
        )
        deps.print_q2rtx_stability_result(result)
        if not result.success:
            deps.log(
                "Profile verification baseline probe skipped: "
                f"{result.reason}; log={result.log_path}"
            )
            return None
        metrics = profile_verification_metrics_from_result(result)
        deps.log("Profile verification baseline probe complete.")
        return metrics
    except Exception as exc:
        deps.log(f"Profile verification baseline probe skipped: {exc}")
        return None


def apply_verify_afterburner_profile(
    vf_curve_reader,
    gpu_policy_controller,
    afterburner_runtime_options,
    *,
    gpu_index,
    dependencies: ProfileVerificationDependencies | None = None,
):
    deps = dependencies or ProfileVerificationDependencies()
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
            deps.log(f"Skipping Afterburner GPU policy during verification: {exc}")
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
    deps.log(
        "Applied Afterburner profile for verification: "
        f"section={source['section']} matched={len(vf_apply_result['plan'])} "
        f"changed={len(vf_apply_result['changed_points'])} "
        f"gpu-index={int(gpu_index)}."
    )
    return label, flatten_target, vf_apply_result["plan"]
