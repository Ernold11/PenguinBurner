from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from runtime.support.runtime_debug import log as runtime_log
from runtime.support.vf_curve_plan import apply_plan
from runtime.support.nvidia_runtime_defaults import build_runtime_default_plan
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR
from profiles.uv.runtime_auto_uv_profile import (
    apply_auto_uv_profile_memory_offset,
    apply_auto_uv_profile_power_limit,
    load_auto_uv_final_curve,
)

from .flattened_clock_ceiling import FlattenedClockCeilingController
from .vf_curve_reset_guard import select_expected_vf_samples


def _default_apply_gpu_base_policy(
    *,
    gpu_policy_controller,
    enable_persistence_mode,
    log,
):
    if gpu_policy_controller is None:
        return
    if enable_persistence_mode:
        try:
            gpu_policy_controller.enable_persistence_mode()
            log("Enabled GPU persistence mode")
        except Exception as exc:
            log(f"Skipping GPU persistence mode: error={exc}")


@dataclass(slots=True)
class RuntimeVfCurvePolicyDependencies:
    load_auto_uv_final_curve: Callable = load_auto_uv_final_curve
    apply_gpu_base_policy: Callable = _default_apply_gpu_base_policy
    apply_plan: Callable = apply_plan
    apply_auto_uv_profile_memory_offset: Callable = apply_auto_uv_profile_memory_offset
    apply_auto_uv_profile_power_limit: Callable = apply_auto_uv_profile_power_limit
    flattened_clock_ceiling_controller_factory: Callable = FlattenedClockCeilingController
    select_expected_vf_samples: Callable = select_expected_vf_samples
    log: Callable[[str], None] = runtime_log


@dataclass(slots=True)
class RuntimeVfCurvePolicyResult:
    auto_uv_final_curve: dict | None = None
    vf_apply_result: dict | None = None
    active_vf_curve_source: str | None = None
    auto_uv_profile_gpu_policy: dict | None = None
    active_profile_id: str = ""
    active_profile_tier: str = ""
    active_profile_tier_key: str = ""
    clock_ceiling_controller: object | None = None
    vf_expected_samples: list = field(default_factory=list)


def configure_runtime_vf_curve_policy(
    *,
    gpu_index,
    enable_persistence_mode,
    auto_uv_profile_selector,
    vf_curve_reader,
    gpu_policy_controller,
    dependencies: RuntimeVfCurvePolicyDependencies | None = None,
) -> RuntimeVfCurvePolicyResult:
    deps = dependencies or RuntimeVfCurvePolicyDependencies()
    result = RuntimeVfCurvePolicyResult()

    if str(auto_uv_profile_selector or "").strip() == STOCK_PROFILE_SELECTOR:
        # "Keep stock" runtime: apply NO undervolt and actively reset the GPU to
        # factory (release locked clocks, zero VF/mem offsets, restore default
        # power limit, zero the per-point V/F curve). Runs inside the root daemon
        # -- no pkexec. The daemon keeps running for fan control.
        deps.apply_gpu_base_policy(
            gpu_policy_controller=gpu_policy_controller,
            enable_persistence_mode=enable_persistence_mode,
            log=deps.log,
        )
        _reset_gpu_to_stock(
            vf_curve_reader=vf_curve_reader,
            gpu_policy_controller=gpu_policy_controller,
            deps=deps,
        )
        result.active_vf_curve_source = "stock"
        return result

    try:
        result.auto_uv_final_curve = deps.load_auto_uv_final_curve(
            auto_uv_profile_selector
        )
    except Exception as exc:
        result.auto_uv_final_curve = None
        deps.log(f"Skipping auto-UV final curve: error={exc}")

    deps.apply_gpu_base_policy(
        gpu_policy_controller=gpu_policy_controller,
        enable_persistence_mode=enable_persistence_mode,
        log=deps.log,
    )

    if vf_curve_reader is None:
        return result

    _apply_auto_uv_final_curve(
        result,
        vf_curve_reader=vf_curve_reader,
        gpu_policy_controller=gpu_policy_controller,
        deps=deps,
    )

    return result


def _reset_gpu_to_stock(*, vf_curve_reader, gpu_policy_controller, deps) -> None:
    """Undo any applied profile: locks, VF/mem offsets, power limit, V/F curve.

    Uses the runtime's already-open handles (no second NVML/VF session).
    """
    if gpu_policy_controller is not None:
        for op, label in (
            (gpu_policy_controller.reset_locked_core_clocks, "locked core clocks"),
            (gpu_policy_controller.reset_locked_memory_clocks, "locked memory clocks"),
        ):
            try:
                op()
            except Exception as exc:
                deps.log(f"keep-stock: {label} reset skipped: {exc}")
        try:
            gpu_policy_controller.apply_clock_offsets(
                gpc_clk_vf_offset_mhz=0, mem_clk_vf_offset_mhz=0
            )
        except Exception as exc:
            deps.log(f"keep-stock: VF offset reset skipped: {exc}")
        try:
            default_w = gpu_policy_controller.query_power_limits().get(
                "power_limit_default_w"
            )
            if default_w:
                gpu_policy_controller.apply_power_limit_w(int(default_w))
        except Exception as exc:
            deps.log(f"keep-stock: power-limit reset skipped: {exc}")
    if vf_curve_reader is not None:
        try:
            if hasattr(vf_curve_reader, "refresh_points"):
                vf_curve_reader.refresh_points()
            deps.apply_plan(vf_curve_reader, build_runtime_default_plan(vf_curve_reader))
        except Exception as exc:
            deps.log(f"keep-stock: V/F curve reset skipped: {exc}")
    deps.log("keep-stock: GPU reset to factory V/F; no undervolt applied")


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
    profile_gpu_policy = deps.apply_auto_uv_profile_power_limit(
        profile_label="auto-UV final curve",
        power_limit_w=auto_uv_final_curve.get("power_limit_w"),
        gpu_policy_controller=gpu_policy_controller,
    ) or {}
    if profile_gpu_policy:
        deps.log(
            "Applied auto-UV profile power limit: "
            f"{int(profile_gpu_policy['power_limit_w'])}W."
        )

    memory_policy = deps.apply_auto_uv_profile_memory_offset(
        profile_label="auto-UV final curve",
        memory_offset_mhz=auto_uv_final_curve.get("memory_offset_mhz"),
        gpu_policy_controller=gpu_policy_controller,
    ) or {}
    if memory_policy:
        deps.log(
            "Applied auto-UV profile memory offset: "
            f"{int(memory_policy['mem_clk_vf_offset_mhz']):+d}MHz."
        )
    result.auto_uv_profile_gpu_policy = {**profile_gpu_policy, **memory_policy}

    _apply_clock_ceiling(
        result,
        flatten_target=auto_uv_final_curve["flatten_target"],
        gpu_policy_controller=gpu_policy_controller,
        label_prefix="auto-UV",
        context=f"path={auto_uv_final_curve['path']}",
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
        deps.log(
            f"Configured {label_prefix} clock ceiling: "
            f"{result.clock_ceiling_controller.describe()}."
        )
