from __future__ import annotations

from pathlib import Path
from typing import Callable

from afterburner.import_vf_curve import (
    apply_plan,
    resolve_afterburner_curve_translation,
)
from afterburner.vfcurve import (
    load_afterburner_profile_settings,
    resolve_afterburner_device_profile,
)
from hidden_nvapi_vf import create_hidden_vf_curve_reader
from nvml_gpu_policy import (
    NvmlGpuPolicyController,
    apply_translated_gpu_policy,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from nvidia_runtime_defaults import reset_nvidia_runtime_defaults
from penguin_burner_paths import resolve_afterburner_root

from auto_uv.auto_uv_types import AutoUvError


AFTERBURNER_DEFAULTS_SECTION = "Defaults"


def _resolve_configured_device_profile(afterburner_root, runtime_options) -> Path:
    root = resolve_afterburner_root(afterburner_root).resolve()
    device_profile_hint = str(
        runtime_options.get("afterburner_device_profile", "")
    ).strip()
    if device_profile_hint:
        candidate = Path(device_profile_hint).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.exists():
            return candidate.resolve()
        raise AutoUvError(
            f"configured Afterburner device profile was not found: {candidate}"
        )
    return resolve_afterburner_device_profile(
        root,
        device_profile_hint=None,
        dangerously_skip_validation=True,
    )


def restore_afterburner_defaults_from_config(
    *,
    gpu_index: int,
    runtime_options: dict,
    log: Callable[[str], None] = print,
) -> dict:
    reset_result = reset_nvidia_runtime_defaults(
        gpu_index=gpu_index,
        power_limit_override_w=runtime_options.get("power_limit_override_w"),
        log=log,
    )
    afterburner_root = str(runtime_options.get("afterburner_root", "")).strip()
    if not afterburner_root:
        raise AutoUvError(
            "no Afterburner root is configured; configure one first before restoring defaults"
        )

    device_profile = _resolve_configured_device_profile(
        afterburner_root, runtime_options
    )
    reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    if reader is None:
        raise AutoUvError("failed to create Linux NVAPI VF helper")

    policy_controller = None
    try:
        try:
            policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
        except Exception as exc:
            log(
                f"Restore defaults: GPU policy helper unavailable, continuing without it: {exc}"
            )

        if policy_controller is not None:
            try:
                policy_controller.reset_locked_core_clocks()
            except Exception as exc:
                log(f"Restore defaults: locked core clock reset skipped: {exc}")

        profile_settings = load_afterburner_profile_settings(
            profile_path=device_profile,
            section=AFTERBURNER_DEFAULTS_SECTION,
        )
        translated_gpu_policy = None
        if policy_controller is not None:
            translated_gpu_policy = translate_afterburner_gpu_policy(
                profile_settings,
                power_limits=policy_controller.query_power_limits(),
                power_limit_cap_w=None,
            )
            applied_policy = apply_translated_gpu_policy(
                policy_controller,
                translated_gpu_policy,
            )
            if applied_policy:
                log(
                    "Restore defaults: applied GPU policy "
                    f"{describe_translated_gpu_policy(translated_gpu_policy)}"
                )

        result = resolve_afterburner_curve_translation(
            reader,
            profile_path=device_profile,
            section=AFTERBURNER_DEFAULTS_SECTION,
            gpu_policy=translated_gpu_policy,
            preserve_base_below_mv=None,
        )
        apply_plan(reader, result["plan"])
        reader.refresh_points()
        log(
            "Restore defaults: applied "
            f"{AFTERBURNER_DEFAULTS_SECTION} from {device_profile.name} "
            f"matched={len(result['plan'])} "
            f"changed={len(result['changed_points'])} "
            f"mode={result['translation_mode']}"
        )
        return {
            "profile_path": device_profile,
            "section": AFTERBURNER_DEFAULTS_SECTION,
            "plan": result["plan"],
            "gpu_policy": translated_gpu_policy,
            "runtime_reset": reset_result,
        }
    finally:
        reader.close()
        if policy_controller is not None:
            policy_controller.close()
