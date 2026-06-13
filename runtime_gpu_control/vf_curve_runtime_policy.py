from __future__ import annotations

from dataclasses import dataclass, field
import shutil
from typing import Callable

from afterburner.import_vf_curve import (
    apply_afterburner_curve_to_reader,
    apply_plan,
)
from afterburner.vfcurve import (
    derive_afterburner_dynamic_lock,
    load_afterburner_profile_settings,
    resolve_afterburner_vf_source,
)
from nvidia_driver.nvml_gpu_policy import (
    apply_translated_gpu_policy,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from runtime_support.runtime_debug import log as runtime_log
from saved_uv_profiles import (
    apply_auto_uv_profile_memory_offset,
    load_auto_uv_final_curve,
)

from .flattened_clock_ceiling import FlattenedClockCeilingController
from .nvidia_smi_command import (
    apply_gpu_base_policy as _apply_gpu_base_policy_with_nvidia_smi,
    run_nvidia_smi_command,
)
from .vf_curve_reset_guard import select_expected_vf_samples

_NVIDIA_SMI = shutil.which("nvidia-smi") or "nvidia-smi"


def _run_nvidia_smi(args):
    return run_nvidia_smi_command(args, executable=_NVIDIA_SMI)


def _default_apply_gpu_base_policy(
    *,
    gpu_index,
    enable_persistence_mode,
    power_limit_w,
):
    return _apply_gpu_base_policy_with_nvidia_smi(
        gpu_index,
        enable_persistence_mode,
        power_limit_w,
        run_nvidia_smi_fn=_run_nvidia_smi,
        log=runtime_log,
    )


@dataclass(slots=True)
class RuntimeVfCurvePolicyDependencies:
    load_auto_uv_final_curve: Callable = load_auto_uv_final_curve
    resolve_afterburner_vf_source: Callable = resolve_afterburner_vf_source
    load_afterburner_profile_settings: Callable = load_afterburner_profile_settings
    translate_afterburner_gpu_policy: Callable = translate_afterburner_gpu_policy
    apply_translated_gpu_policy: Callable = apply_translated_gpu_policy
    describe_translated_gpu_policy: Callable = describe_translated_gpu_policy
    apply_gpu_base_policy: Callable = _default_apply_gpu_base_policy
    apply_plan: Callable = apply_plan
    apply_afterburner_curve_to_reader: Callable = apply_afterburner_curve_to_reader
    apply_auto_uv_profile_memory_offset: Callable = apply_auto_uv_profile_memory_offset
    flattened_clock_ceiling_controller_factory: Callable = FlattenedClockCeilingController
    select_expected_vf_samples: Callable = select_expected_vf_samples
    derive_afterburner_dynamic_lock: Callable = derive_afterburner_dynamic_lock
    log: Callable[[str], None] = runtime_log


@dataclass(slots=True)
class RuntimeVfCurvePolicyResult:
    translated_gpu_policy: dict | None = None
    afterburner_source: dict | None = None
    afterburner_profile_settings: dict | None = None
    auto_uv_final_curve: dict | None = None
    vf_apply_result: dict | None = None
    active_vf_curve_source: str | None = None
    auto_uv_profile_gpu_policy: dict | None = None
    active_profile_id: str = ""
    active_profile_tier: str = ""
    active_profile_tier_key: str = ""
    clock_ceiling_controller: object | None = None
    vf_expected_samples: list = field(default_factory=list)
    startup_power_limit_w: int | None = None


def configure_runtime_vf_curve_policy(
    *,
    gpu_index,
    enable_persistence_mode,
    auto_uv_profile_selector,
    prefer_afterburner_curve,
    afterburner_root,
    afterburner_profile,
    afterburner_device_profile,
    afterburner_runtime_options: dict,
    vf_curve_reader,
    gpu_policy_controller,
    dependencies: RuntimeVfCurvePolicyDependencies | None = None,
) -> RuntimeVfCurvePolicyResult:
    deps = dependencies or RuntimeVfCurvePolicyDependencies()
    result = RuntimeVfCurvePolicyResult()

    try:
        result.auto_uv_final_curve = deps.load_auto_uv_final_curve(
            auto_uv_profile_selector
        )
    except Exception as exc:
        result.auto_uv_final_curve = None
        deps.log(f"Skipping auto-UV final curve: error={exc}")

    if afterburner_root:
        _resolve_afterburner_source(
            result,
            afterburner_root=afterburner_root,
            afterburner_profile=afterburner_profile,
            afterburner_device_profile=afterburner_device_profile,
            afterburner_runtime_options=afterburner_runtime_options,
            gpu_policy_controller=gpu_policy_controller,
            deps=deps,
        )

    if (
        result.translated_gpu_policy is not None
        and result.translated_gpu_policy.get("power_limit_w") is not None
    ):
        result.startup_power_limit_w = result.translated_gpu_policy["power_limit_w"]
    deps.apply_gpu_base_policy(
        gpu_index=gpu_index,
        enable_persistence_mode=enable_persistence_mode,
        power_limit_w=result.startup_power_limit_w,
    )
    _apply_translated_afterburner_policy(
        result,
        gpu_policy_controller=gpu_policy_controller,
        deps=deps,
    )

    if vf_curve_reader is None:
        return result

    if prefer_afterburner_curve:
        afterburner_curve_applied = _apply_afterburner_curve(
            result,
            vf_curve_reader=vf_curve_reader,
            afterburner_runtime_options=afterburner_runtime_options,
            gpu_policy_controller=gpu_policy_controller,
            deps=deps,
        )
        if afterburner_curve_applied and result.auto_uv_final_curve is not None:
            deps.log(
                "Auto-UV final curve is present but skipped because "
                "--prefer-afterburner-curve was requested."
            )
        if not afterburner_curve_applied:
            deps.log(
                "--prefer-afterburner-curve requested, but no usable Afterburner "
                "V/F curve was applied; trying Auto-UV final curve fallback."
            )
            _apply_auto_uv_final_curve(
                result,
                vf_curve_reader=vf_curve_reader,
                gpu_policy_controller=gpu_policy_controller,
                deps=deps,
            )
    else:
        auto_uv_curve_applied = _apply_auto_uv_final_curve(
            result,
            vf_curve_reader=vf_curve_reader,
            gpu_policy_controller=gpu_policy_controller,
            deps=deps,
        )
        if not auto_uv_curve_applied:
            _apply_afterburner_curve(
                result,
                vf_curve_reader=vf_curve_reader,
                afterburner_runtime_options=afterburner_runtime_options,
                gpu_policy_controller=gpu_policy_controller,
                deps=deps,
            )

    return result


def _resolve_afterburner_source(
    result: RuntimeVfCurvePolicyResult,
    *,
    afterburner_root,
    afterburner_profile,
    afterburner_device_profile,
    afterburner_runtime_options: dict,
    gpu_policy_controller,
    deps: RuntimeVfCurvePolicyDependencies,
) -> None:
    try:
        result.afterburner_source = deps.resolve_afterburner_vf_source(
            afterburner_root=afterburner_root,
            section=afterburner_profile or None,
            device_profile_hint=afterburner_device_profile or None,
            dangerously_skip_validation=bool(
                afterburner_runtime_options.get("dangerously_skip_validation")
            ),
        )
    except Exception as exc:
        deps.log(f"Skipping Afterburner source resolve: error={exc}")
        return

    if result.afterburner_source.get("dangerously_skip_validation"):
        deps.log(
            "Afterburner validation override enabled: skipping the default "
            "flat-tail and undervolt checks for the saved profile."
        )

    if gpu_policy_controller is None:
        return

    try:
        result.afterburner_profile_settings = deps.load_afterburner_profile_settings(
            profile_path=result.afterburner_source["profile_path"],
            section=result.afterburner_source["section"],
        )
        result.translated_gpu_policy = deps.translate_afterburner_gpu_policy(
            result.afterburner_profile_settings,
            power_limits=gpu_policy_controller.query_power_limits(),
            power_limit_cap_w=afterburner_runtime_options["power_limit_override_w"],
        )
    except Exception as exc:
        result.translated_gpu_policy = None
        deps.log(
            "Skipping Afterburner GPU policy translate: "
            f"section={result.afterburner_source['section']} error={exc}"
        )


def _apply_translated_afterburner_policy(
    result: RuntimeVfCurvePolicyResult,
    *,
    gpu_policy_controller,
    deps: RuntimeVfCurvePolicyDependencies,
) -> None:
    if (
        result.translated_gpu_policy is None
        or gpu_policy_controller is None
        or result.afterburner_source is None
    ):
        return

    try:
        deps.apply_translated_gpu_policy(
            gpu_policy_controller,
            result.translated_gpu_policy,
        )
    except Exception as exc:
        deps.log(
            "Skipping Afterburner GPU policy apply: "
            f"section={result.afterburner_source['section']} error={exc}"
        )
    else:
        deps.log(
            f"Applied Afterburner GPU policy: section={result.afterburner_source['section']} "
            f"{deps.describe_translated_gpu_policy(result.translated_gpu_policy)}."
        )


def _apply_auto_uv_final_curve(
    result: RuntimeVfCurvePolicyResult,
    *,
    vf_curve_reader,
    gpu_policy_controller,
    deps: RuntimeVfCurvePolicyDependencies,
) -> bool:
    if result.auto_uv_final_curve is None:
        return False

    auto_uv_final_curve = result.auto_uv_final_curve
    try:
        deps.apply_plan(vf_curve_reader, auto_uv_final_curve["plan"])
        vf_curve_reader.refresh_points()
    except Exception as exc:
        deps.log(
            "Skipping auto-UV final curve apply: "
            f"path={auto_uv_final_curve['path']} error={exc}"
        )
        result.auto_uv_final_curve = None
        return False

    result.vf_apply_result = {
        "source": "auto-uv-final",
        "plan": auto_uv_final_curve["plan"],
        "path": auto_uv_final_curve["path"],
    }
    result.active_vf_curve_source = "auto-uv-final"
    result.active_profile_id = str(auto_uv_final_curve.get("profile_id") or "")
    result.active_profile_tier = str(auto_uv_final_curve.get("profile_tier") or "")
    result.active_profile_tier_key = str(
        auto_uv_final_curve.get("profile_tier_key") or ""
    )
    result.vf_expected_samples = deps.select_expected_vf_samples(
        result.vf_apply_result["plan"]
    )
    deps.log(
        "Applied auto-UV final curve: "
        f"path={auto_uv_final_curve['path']} "
        f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV "
        f"points={len(auto_uv_final_curve['plan'])}."
    )
    result.auto_uv_profile_gpu_policy = deps.apply_auto_uv_profile_memory_offset(
        profile_label="auto-UV final curve",
        memory_offset_mhz=auto_uv_final_curve.get("memory_offset_mhz"),
        gpu_policy_controller=gpu_policy_controller,
    )
    if result.auto_uv_profile_gpu_policy:
        deps.log(
            "Applied auto-UV profile memory offset: "
            f"{int(result.auto_uv_profile_gpu_policy['mem_clk_vf_offset_mhz']):+d}MHz."
        )

    _apply_clock_ceiling(
        result,
        flatten_target=auto_uv_final_curve["flatten_target"],
        gpu_policy_controller=gpu_policy_controller,
        label_prefix="auto-UV",
        context=f"path={auto_uv_final_curve['path']}",
        deps=deps,
    )
    return True


def _apply_afterburner_curve(
    result: RuntimeVfCurvePolicyResult,
    *,
    vf_curve_reader,
    afterburner_runtime_options: dict,
    gpu_policy_controller,
    deps: RuntimeVfCurvePolicyDependencies,
) -> bool:
    if result.afterburner_source is None:
        return False

    try:
        result.vf_apply_result = deps.apply_afterburner_curve_to_reader(
            vf_curve_reader,
            profile_path=result.afterburner_source["profile_path"],
            section=result.afterburner_source["section"],
            gpu_policy=result.translated_gpu_policy,
            preserve_base_below_mv=afterburner_runtime_options[
                "preserve_base_below_mv"
            ],
        )
    except Exception as exc:
        deps.log(
            "Skipping Afterburner VF curve apply: "
            f"section={result.afterburner_source['section']} error={exc}"
        )
        return False

    deps.log(
        f"Applied Afterburner VF curve: section={result.afterburner_source['section']} "
        f"matched={len(result.vf_apply_result['plan'])} "
        f"changed={len(result.vf_apply_result['changed_points'])} "
        f"mode={result.vf_apply_result['translation_mode']} "
        f"origin={result.vf_apply_result['translation_origin']} "
        f"linux_profile={result.vf_apply_result['translated_linux_profile_path']}."
    )
    result.active_vf_curve_source = "afterburner"
    result.vf_expected_samples = deps.select_expected_vf_samples(
        result.vf_apply_result["plan"]
    )
    flatten_target = deps.derive_afterburner_dynamic_lock(
        result.vf_apply_result["materialization"]["points"]
    )
    section_context = f"section={result.afterburner_source['section']}"
    if flatten_target is None:
        deps.log(
            f"Skipping Afterburner clock ceiling: {section_context} "
            "no flattened V/F target was detected."
        )
        return True

    _apply_clock_ceiling(
        result,
        flatten_target=flatten_target,
        gpu_policy_controller=gpu_policy_controller,
        label_prefix="Afterburner",
        context=section_context,
        deps=deps,
    )
    return True


def _apply_clock_ceiling(
    result: RuntimeVfCurvePolicyResult,
    *,
    flatten_target,
    gpu_policy_controller,
    label_prefix: str,
    context: str,
    deps: RuntimeVfCurvePolicyDependencies,
) -> None:
    try:
        result.clock_ceiling_controller = (
            deps.flattened_clock_ceiling_controller_factory(
                flatten_target=flatten_target,
                policy_controller=gpu_policy_controller,
            )
        )
        result.clock_ceiling_controller.apply()
    except Exception as exc:
        result.clock_ceiling_controller = None
        deps.log(f"Skipping {label_prefix} clock ceiling: {context} error={exc}")
    else:
        suffix = f" {context}" if label_prefix == "Afterburner" else ""
        deps.log(
            f"Configured {label_prefix} clock ceiling:{suffix} "
            f"{result.clock_ceiling_controller.describe()}."
        )
