#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from nvidia_driver.hidden_nvapi_vf import create_hidden_vf_curve_reader
from nvidia_driver.nvml_gpu_policy import (
    NvmlGpuPolicyController,
    apply_translated_gpu_policy,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from common.penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_linux_vf_profiles_dir,
    managed_afterburner_root,
    resolve_afterburner_root,
    sync_afterburner_export_tree,
)

from .import_fan_curve import DEFAULT_CONFIG_PATH, load_config, write_config
from .vfcurve import (
    AfterburnerProfileSelectionError,
    AfterburnerVfCurveSafetyError,
    derive_afterburner_dynamic_lock,
    ensure_safe_afterburner_vfcurve,
    hash_afterburner_vfcurve_hex,
    load_afterburner_profile_settings,
    load_afterburner_vfcurve,
    load_afterburner_vfcurve_hex,
    materialize_afterburner_vfcurve,
    point_map_by_voltage,
    resolve_afterburner_vf_source,
)
from .vfcurve_describe import (
    describe_afterburner_dynamic_lock,
    describe_afterburner_flatten_validation,
    describe_afterburner_profile_settings,
    describe_afterburner_vfcurve_analysis,
)

TRANSLATED_LINUX_PROFILE_DIR = default_linux_vf_profiles_dir()
TRANSLATED_LINUX_PROFILE_SCHEMA_VERSION = 5


def _coerce_optional_int(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(round(float(text)))


def _coerce_optional_positive_int(value):
    coerced = _coerce_optional_int(value)
    if coerced is None or int(coerced) <= 0:
        return None
    return int(coerced)


def _coerce_optional_nonnegative_int(value):
    coerced = _coerce_optional_int(value)
    if coerced is None or int(coerced) < 0:
        return None
    return int(coerced)


def _coerce_optional_positive_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    coerced = float(text)
    if float(coerced) <= 0.0:
        return None
    return float(coerced)


def _coerce_optional_nonnegative_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    coerced = float(text)
    if float(coerced) < 0.0:
        return None
    return float(coerced)


def _coerce_optional_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return bool(value)


def load_afterburner_runtime_options(config_path):
    current = load_config(Path(config_path))
    gpu = dict(current.get("gpu", {}))
    return {
        "afterburner_root": str(gpu.get("afterburner_root", "")).strip(),
        "afterburner_profile": str(gpu.get("afterburner_profile", "")).strip(),
        "afterburner_device_profile": str(
            gpu.get("afterburner_device_profile", "")
        ).strip(),
        "auto_uv_max_drop_pct": _coerce_optional_positive_float(
            gpu.get("afterburner_auto_uv_max_drop_pct")
        ),
        "auto_uv_final_seconds": _coerce_optional_positive_int(
            gpu.get("auto_uv_final_seconds")
        ),
        "auto_uv_efficiency_stop_streak": _coerce_optional_nonnegative_int(
            gpu.get("auto_uv_efficiency_stop_streak")
        ),
    }


def summarize_plan(plan):
    if not plan:
        print("No editable VF points matched the selected Afterburner curve.")
        return

    print(f"Planned point updates: {len(plan)}")
    print("idx  voltage  base_mhz  target_mhz  current_offset  new_offset")
    for item in plan[:24]:
        print(
            f"{item['index']:3d}  "
            f"{item['voltage_mv']:7d}mV  "
            f"{item['base_mhz']:8d}  "
            f"{item['target_mhz']:10d}  "
            f"{item['current_offset_mhz']:14d}  "
            f"{item['new_offset_mhz']:10d}"
        )
    if len(plan) > 24:
        print(f"... {len(plan) - 24} more")


def translated_linux_profile_path(section, curve_sha256):
    return TRANSLATED_LINUX_PROFILE_DIR / f"{section}-{curve_sha256[:16]}.json"


def load_offsets_payload(payload_path):
    return json.loads(Path(payload_path).read_text(encoding="utf-8", errors="replace"))


def translated_linux_profile_is_current(payload, *, curve_sha256):
    if int(payload.get("schema_version", 0)) != TRANSLATED_LINUX_PROFILE_SCHEMA_VERSION:
        return False
    if (
        str(payload.get("curve_sha256", "")).strip().lower()
        != str(curve_sha256).lower()
    ):
        return False
    return isinstance(payload.get("points"), list) and bool(payload["points"])


def offsets_by_index(payload):
    return {int(item["index"]): item for item in payload["points"]}


def build_plan_from_offsets_payload(reader, payload):
    payload_points = offsets_by_index(payload)
    plan = []
    missing_indices = []
    voltage_mismatches = []

    for point in reader.editable_core_points():
        payload_point = payload_points.get(int(point["index"]))
        if payload_point is None:
            missing_indices.append(int(point["index"]))
            continue

        voltage_mv = point["voltage_uv"] // 1000
        payload_voltage_mv = int(payload_point.get("voltage_mv", voltage_mv))
        if payload_voltage_mv != int(voltage_mv):
            voltage_mismatches.append(
                {
                    "index": int(point["index"]),
                    "payload_voltage_mv": payload_voltage_mv,
                    "current_voltage_mv": int(voltage_mv),
                }
            )

        base_mhz = point["base_freq_khz"] // 1000
        current_offset_mhz = point["current_offset_khz"] // 1000
        new_offset_mhz = int(payload_point["current_offset_mhz"])
        plan.append(
            {
                "index": int(point["index"]),
                "voltage_mv": int(voltage_mv),
                "base_mhz": int(base_mhz),
                "target_mhz": int(base_mhz + new_offset_mhz),
                "current_offset_mhz": int(current_offset_mhz),
                "new_offset_mhz": int(new_offset_mhz),
            }
        )

    return plan, missing_indices, voltage_mismatches


def apply_offsets_payload(reader, payload):
    offsets = {
        int(item["index"]): int(item["current_offset_mhz"])
        for item in payload["points"]
    }
    control = reader.get_control_struct()
    for point in reader.editable_core_points():
        control.vf_points[point["index"]].prog.freq_offset_khz = (
            offsets.get(point["index"], 0) * 1000
        )
    reader.set_control_struct(control)
    return len(offsets)


def write_translated_linux_profile(
    output_path,
    *,
    profile_path,
    section,
    curve_sha256,
    translation_mode,
    plan,
    materialization=None,
    gpu_policy=None,
    source_profile_path=None,
    source_payload_path=None,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(output_path.parent, include_parents=True)

    payload = {
        "schema_version": TRANSLATED_LINUX_PROFILE_SCHEMA_VERSION,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "afterburner",
        "source_profile_path": str(Path(profile_path)),
        "source_section": str(section),
        "curve_sha256": str(curve_sha256),
        "translation_mode": str(translation_mode),
        "source_linux_profile_path": (
            str(Path(source_profile_path)) if source_profile_path is not None else ""
        ),
        "source_payload_path": str(Path(source_payload_path))
        if source_payload_path
        else "",
        "points": [
            {
                "index": int(item["index"]),
                "voltage_mv": int(item["voltage_mv"]),
                "base_mhz": int(item["base_mhz"]),
                "target_mhz": int(item["target_mhz"]),
                "current_offset_mhz": int(item["new_offset_mhz"]),
            }
            for item in plan
        ],
    }
    if materialization is not None:
        payload["afterburner_materialization"] = {
            "mode": str(materialization.get("mode", "")),
            "adjusted_anchor": materialization.get("adjusted_anchor"),
        }
        if materialization.get("flatten_override") is not None:
            payload["afterburner_materialization"]["flatten_override"] = (
                materialization.get("flatten_override")
            )
    if gpu_policy is not None:
        payload["gpu_policy"] = dict(gpu_policy)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    claim_desktop_user_ownership(output_path)
    return output_path


def build_plan(reader, afterburner_points):
    ab_by_voltage = point_map_by_voltage(afterburner_points)
    plan = []
    missing_in_afterburner = []
    for point in reader.editable_core_points():
        voltage_mv = point["voltage_uv"] // 1000
        base_mhz = point["base_freq_khz"] // 1000
        current_offset_mhz = point["current_offset_khz"] // 1000
        ab_point = ab_by_voltage.get(voltage_mv)
        if ab_point is None:
            missing_in_afterburner.append(voltage_mv)
            continue

        target_mhz = int(round(ab_point["frequency_mhz"]))
        new_offset_mhz = target_mhz - base_mhz
        plan.append(
            {
                "index": point["index"],
                "voltage_mv": int(voltage_mv),
                "base_mhz": int(base_mhz),
                "target_mhz": int(target_mhz),
                "current_offset_mhz": int(current_offset_mhz),
                "new_offset_mhz": int(new_offset_mhz),
                "preserve_base": False,
            }
        )

    return plan, sorted(set(missing_in_afterburner))


def apply_plan(reader, plan):
    control = reader.get_control_struct()
    for item in plan:
        control.vf_points[item["index"]].prog.freq_offset_khz = (
            item["new_offset_mhz"] * 1000
        )
    reader.set_control_struct(control)


def resolve_afterburner_curve_translation(
    reader,
    profile_path,
    section,
    allow_unsafe_curve=False,
    gpu_policy=None,
):
    profile_path = Path(profile_path)
    curve_hex = load_afterburner_vfcurve_hex(profile_path=profile_path, section=section)
    curve_sha256 = hash_afterburner_vfcurve_hex(curve_hex)
    translated_profile = translated_linux_profile_path(
        section=section,
        curve_sha256=curve_sha256,
    )

    header, afterburner_points, tail = load_afterburner_vfcurve(
        profile_path=profile_path,
        section=section,
    )
    analysis = ensure_safe_afterburner_vfcurve(
        afterburner_points,
        section=section,
        allow_unsafe=allow_unsafe_curve,
    )
    materialization = materialize_afterburner_vfcurve(afterburner_points)
    translation_mode = ""
    translation_origin = ""
    missing_in_afterburner = []
    missing_indices = []
    plan = []
    voltage_mismatches = []
    source_payload_path = None

    if translated_profile.exists():
        payload = load_offsets_payload(translated_profile)
        if translated_linux_profile_is_current(
            payload,
            curve_sha256=curve_sha256,
        ):
            plan, missing_indices, voltage_mismatches = build_plan_from_offsets_payload(
                reader, payload
            )
            translation_mode = str(
                payload.get("translation_mode", "translated-linux-profile")
            )
            translation_origin = "cached-linux-profile"
            source_payload_path = translated_profile
            cached_materialization = payload.get("afterburner_materialization")
            if isinstance(cached_materialization, dict):
                materialization = {
                    "mode": str(
                        cached_materialization.get(
                            "mode", materialization.get("mode", "")
                        )
                    ),
                    "points": list(materialization.get("points", [])),
                    "adjusted_anchor": cached_materialization.get(
                        "adjusted_anchor",
                        materialization.get("adjusted_anchor"),
                    ),
                    "flatten_override": cached_materialization.get(
                        "flatten_override",
                        materialization.get("flatten_override"),
                    ),
                }
            cached_gpu_policy = payload.get("gpu_policy")
            if gpu_policy is None and isinstance(cached_gpu_policy, dict):
                gpu_policy = cached_gpu_policy

    if not translation_origin:
        plan, missing_in_afterburner = build_plan(
            reader,
            materialization["points"],
        )
        if materialization["mode"] == "adjusted-anchor-cap":
            translation_mode = "adjusted-anchor-cap"
            translation_origin = "afterburner-adjusted-anchor"
        else:
            translation_mode = (
                "unsafe-voltage-bin-map"
                if analysis["nonzero_third_value_count"]
                else "voltage-bin-map"
            )
            translation_origin = "afterburner-voltage-bin-map"

        translated_profile = write_translated_linux_profile(
            translated_profile,
            profile_path=profile_path,
            section=section,
            curve_sha256=curve_sha256,
            translation_mode=translation_mode,
            plan=plan,
            materialization=materialization,
            gpu_policy=gpu_policy,
            source_profile_path=source_payload_path,
            source_payload_path=source_payload_path,
        )

    if missing_indices:
        raise RuntimeError(
            "Translated Linux VF profile does not cover all editable points: "
            + ", ".join(str(index) for index in missing_indices[:16])
            + (" ..." if len(missing_indices) > 16 else "")
        )

    changed_points = [
        item
        for item in plan
        if int(item["current_offset_mhz"]) != int(item["new_offset_mhz"])
    ]

    return {
        "analysis": analysis,
        "curve_sha256": curve_sha256,
        "header": header,
        "tail": tail,
        "plan": plan,
        "missing_in_afterburner": missing_in_afterburner,
        "changed_points": changed_points,
        "translated_linux_profile_path": str(translated_profile),
        "translation_mode": translation_mode,
        "translation_origin": translation_origin,
        "voltage_mismatches": voltage_mismatches,
        "materialization": materialization,
        "gpu_policy": gpu_policy,
    }


def apply_afterburner_curve_to_reader(
    reader,
    profile_path,
    section,
    allow_unsafe_curve=False,
    gpu_policy=None,
):
    result = resolve_afterburner_curve_translation(
        reader,
        profile_path=profile_path,
        section=section,
        allow_unsafe_curve=allow_unsafe_curve,
        gpu_policy=gpu_policy,
    )

    if result["changed_points"]:
        apply_plan(reader, result["plan"])
        reader.refresh_points()

    return result


def backup_current_offsets(reader, backup_path, policy_controller=None):
    backup_path = Path(backup_path)
    payload = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "points": [
            {
                "index": point["index"],
                "voltage_mv": point["voltage_uv"] // 1000,
                "base_mhz": point["base_freq_khz"] // 1000,
                "current_offset_mhz": point["current_offset_khz"] // 1000,
            }
            for point in reader.editable_core_points()
        ],
    }
    if policy_controller is not None:
        power_limits = policy_controller.query_power_limits()
        clock_offsets = policy_controller.get_clock_offsets()
        payload["gpu_policy"] = {
            "power_limit_w": power_limits.get("power_limit_w"),
            "power_limit_default_w": power_limits.get("power_limit_default_w"),
            "power_limit_min_w": power_limits.get("power_limit_min_w"),
            "power_limit_max_w": power_limits.get("power_limit_max_w"),
            "mem_clk_vf_offset_mhz": clock_offsets.get("mem_clk_vf_offset_mhz"),
        }
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(backup_path.parent, include_parents=True)
    backup_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    claim_desktop_user_ownership(backup_path)
    return backup_path


def restore_offsets(reader, backup_path, policy_controller=None):
    payload = load_offsets_payload(backup_path)
    gpu_policy = payload.get("gpu_policy")
    if policy_controller is not None and isinstance(gpu_policy, dict):
        apply_translated_gpu_policy(policy_controller, gpu_policy)
    return apply_offsets_payload(reader, payload)


LEGACY_GPU_CONFIG_KEYS = (
    "power_limit_w",
    "gpc_clk_vf_offset",
    "mem_clk_vf_offset",
    "vf_curve_source",
    "vf_curve_source_path",
    "vf_curve_source_section",
    "afterburner_power_limit_cap_w",
    "afterburner_power_limit_pct",
    "afterburner_core_clk_boost_khz",
    "afterburner_mem_clk_boost_khz",
    "afterburner_thermal_limit",
    "afterburner_dynamic_lock_enabled",
    "afterburner_dynamic_lock_enter_power_w",
    "afterburner_dynamic_lock_exit_power_w",
    "afterburner_flatten_at_voltage_mv",
    "afterburner_flatten_to_mhz",
    "vf_curve_curve_sha256",
    "vf_curve_linux_profile_path",
    "vf_curve_translation_mode",
)


def import_afterburner_export_into_managed_root(
    config_path, runtime_options, *, gpu_index=0
):
    configured_root = str(runtime_options.get("afterburner_root", "")).strip()
    if not configured_root:
        return runtime_options

    source_root = resolve_afterburner_root(configured_root)
    managed_root = managed_afterburner_root()
    imported_root = sync_afterburner_export_tree(source_root, managed_root)
    runtime_options["afterburner_root"] = str(imported_root)
    _persist_afterburner_runtime_state(
        config_path,
        gpu_index,
        afterburner_root=imported_root,
        runtime_options=runtime_options,
    )
    return runtime_options


def _persist_afterburner_runtime_state(
    config_path,
    gpu_index,
    *,
    afterburner_root=None,
    device_profile_relative_path=None,
    section=None,
    runtime_options=None,
):
    config = load_config(Path(config_path))
    gpu = dict(config.get("gpu", {}))
    fan = dict(config.get("fan", {}))
    stability = dict(config.get("stability", {}))
    gpu["index"] = int(gpu.get("index", gpu_index))
    if afterburner_root is not None:
        gpu["afterburner_root"] = str(Path(afterburner_root))
    if section is not None:
        gpu["afterburner_profile"] = str(section)
    if device_profile_relative_path is not None:
        gpu["afterburner_device_profile"] = str(device_profile_relative_path)

    for key in LEGACY_GPU_CONFIG_KEYS:
        gpu.pop(key, None)
    gpu.pop("afterburner_power_limit_override_w", None)
    gpu.pop("afterburner_preserve_base_below_mv", None)
    gpu.pop("afterburner_preserve_vanilla_below_mv", None)
    gpu.pop("afterburner_dangerously_skip_validation", None)

    if runtime_options is not None:
        auto_uv_max_drop_pct = _coerce_optional_positive_float(
            runtime_options.get("auto_uv_max_drop_pct")
        )
        if auto_uv_max_drop_pct is not None:
            gpu["afterburner_auto_uv_max_drop_pct"] = float(auto_uv_max_drop_pct)
        else:
            gpu.pop("afterburner_auto_uv_max_drop_pct", None)
        auto_uv_final_seconds = _coerce_optional_positive_int(
            runtime_options.get("auto_uv_final_seconds")
        )
        if auto_uv_final_seconds is not None:
            gpu["auto_uv_final_seconds"] = int(auto_uv_final_seconds)
        else:
            gpu.pop("auto_uv_final_seconds", None)
        auto_uv_efficiency_stop_streak = _coerce_optional_nonnegative_int(
            runtime_options.get("auto_uv_efficiency_stop_streak")
        )
        if auto_uv_efficiency_stop_streak is not None:
            gpu["auto_uv_efficiency_stop_streak"] = int(auto_uv_efficiency_stop_streak)
        else:
            gpu.pop("auto_uv_efficiency_stop_streak", None)
        gpu.pop("auto_uv_max_oc_budget_recoveries", None)
        gpu.pop("auto_uv_oc_budget_ratio", None)
        gpu.pop("auto_uv_overclock_budget_ratio", None)
        gpu.pop("auto_uv_clock_bump_budget_ratio", None)

    write_config(
        Path(config_path),
        {
            "gpu": gpu,
            "fan": fan,
            "stability": stability,
        },
    )


def persist_afterburner_import(
    config_path,
    gpu_index,
    afterburner_root,
    device_profile_relative_path,
    section,
    *,
    runtime_options=None,
):
    _persist_afterburner_runtime_state(
        config_path,
        gpu_index,
        afterburner_root=afterburner_root,
        device_profile_relative_path=device_profile_relative_path,
        section=section,
        runtime_options=runtime_options,
    )


def ensure_afterburner_root_configured(
    config_path, runtime_options, *, gpu_index=0, interactive=True
):
    configured_root = str(runtime_options.get("afterburner_root", "")).strip()
    if configured_root:
        return import_afterburner_export_into_managed_root(
            config_path,
            runtime_options,
            gpu_index=gpu_index,
        )

    if not interactive or not sys.stdin.isatty():
        raise AfterburnerProfileSelectionError(
            "No Afterburner directory is configured. Re-run interactively and paste the MSI "
            "Afterburner root directory path."
        )

    while True:
        entered = input("Paste the MSI Afterburner root directory path: ").strip()
        if not entered:
            print("Afterburner directory is required.")
            continue

        afterburner_root = resolve_afterburner_root(entered)
        try:
            managed_root = sync_afterburner_export_tree(
                afterburner_root, managed_afterburner_root()
            )
        except FileNotFoundError as exc:
            print(str(exc))
            continue

        runtime_options["afterburner_root"] = str(managed_root)
        _persist_afterburner_runtime_state(
            config_path,
            gpu_index,
            afterburner_root=managed_root,
            runtime_options=runtime_options,
        )
        print(f"Imported Afterburner export into {managed_root}")
        print(f"Saved PenguinBurner config to {config_path}")
        return runtime_options


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument(
        "--afterburner-dir",
        default="",
        help="Path to the MSI Afterburner root directory",
    )
    parser.add_argument(
        "--profile-section",
        "--section",
        dest="profile_section",
        default="",
        help="Optional saved Afterburner profile section such as profile2",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the mapped per-point offsets through SetControl",
    )
    parser.add_argument(
        "--backup-file",
        default="",
        help="Optional JSON file path to save current Linux VF offsets before applying",
    )
    parser.add_argument(
        "--restore-file",
        default="",
        help="Restore Linux VF offsets from a previously saved backup JSON and exit",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Print the full update plan instead of the first 24 points",
    )
    parser.add_argument(
        "--allow-unsafe-curve",
        action="store_true",
        help="Allow importing tuned blobs with nonzero third-field metadata",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Runtime config path to persist the active VF curve source",
    )
    parser.add_argument(
        "--no-write-config",
        action="store_true",
        help="Do not persist the Afterburner VF source into the runtime config",
    )
    args = parser.parse_args()

    reader = create_hidden_vf_curve_reader(gpu_index=args.gpu_index)
    if reader is None:
        raise RuntimeError("Failed to create Linux NVAPI VF helper")
    policy_controller = NvmlGpuPolicyController(gpu_index=args.gpu_index)
    runtime_options = load_afterburner_runtime_options(args.config)
    if args.afterburner_dir.strip():
        runtime_options["afterburner_root"] = str(
            resolve_afterburner_root(args.afterburner_dir)
        )
    if args.profile_section.strip():
        runtime_options["afterburner_profile"] = str(args.profile_section).strip()

    try:
        if args.restore_file:
            restored = restore_offsets(
                reader,
                args.restore_file,
                policy_controller=policy_controller,
            )
            print(
                f"Restored offsets for {restored} editable VF points from {args.restore_file}"
            )
            reader.refresh_points()
            return 0

        runtime_options = ensure_afterburner_root_configured(
            args.config,
            runtime_options,
            gpu_index=args.gpu_index,
            interactive=True,
        )
        source = resolve_afterburner_vf_source(
            afterburner_root=runtime_options.get("afterburner_root") or None,
            section=runtime_options.get("afterburner_profile") or None,
            device_profile_hint=runtime_options.get("afterburner_device_profile")
            or None,
        )
        profile_settings = load_afterburner_profile_settings(
            profile_path=source["profile_path"],
            section=source["section"],
        )
        translated_gpu_policy = translate_afterburner_gpu_policy(
            profile_settings,
            power_limits=policy_controller.query_power_limits(),
        )

        result = resolve_afterburner_curve_translation(
            reader,
            profile_path=source["profile_path"],
            section=source["section"],
            allow_unsafe_curve=args.allow_unsafe_curve,
            gpu_policy=translated_gpu_policy,
        )
        flatten_target = derive_afterburner_dynamic_lock(
            result["materialization"]["points"]
        )
        print(
            "Warning: use at your own risk. Importing vendor-undocumented Afterburner "
            "voltage/frequency data can hang the GPU or crash the driver."
        )
        print(
            f"Afterburner root={source['afterburner_root']} "
            f"device_profile={source['profile_path'].name} section={source['section']} "
            f"points={len(result['plan'])} hint={result['header']['point_count_hint']} "
            f"tail_nonzero={len(result['tail'])}"
        )
        print(
            "Parsed Afterburner settings: "
            f"{describe_afterburner_profile_settings(profile_settings)}"
        )
        print(
            f"Translated GPU policy: {describe_translated_gpu_policy(result['gpu_policy'])}"
        )
        print(f"Flatten target: {describe_afterburner_dynamic_lock(flatten_target)}")
        print(
            "Flatten validation: "
            f"{describe_afterburner_flatten_validation(source['section_info'].get('flatten_validation'))}"
        )
        print(
            f"Curve analysis: {describe_afterburner_vfcurve_analysis(result['analysis'])}"
        )
        if args.allow_unsafe_curve and result["analysis"]["nonzero_third_value_count"]:
            print(
                "Unsafe curve override enabled; proceeding despite nonzero third-field metadata."
            )
        print(
            f"Translation: mode={result['translation_mode']} "
            f"origin={result['translation_origin']} "
            f"curve_sha256={result['curve_sha256'][:16]}..."
        )
        print(f"Translated Linux VF profile: {result['translated_linux_profile_path']}")

        print(
            f"Linux editable core points={reader.summary()['editable_core_points']} "
            f"matched={len(result['plan'])}"
        )
        if result["missing_in_afterburner"]:
            print(
                "Missing voltage bins in Afterburner curve: "
                + ", ".join(f"{mv}mV" for mv in result["missing_in_afterburner"][:16])
                + (" ..." if len(result["missing_in_afterburner"]) > 16 else "")
            )
        if result["voltage_mismatches"]:
            print(
                f"Voltage-bin mismatches against cached Linux profile: "
                f"{len(result['voltage_mismatches'])}"
            )

        if args.show_all:
            for item in result["plan"]:
                print(item)
        else:
            summarize_plan(result["plan"])

        if args.backup_file:
            backup_path = backup_current_offsets(
                reader,
                args.backup_file,
                policy_controller=policy_controller,
            )
            print(f"Backed up current Linux VF offsets to {backup_path}")

        if not args.apply:
            print("Dry run only. Re-run with --apply to write the mapped offsets.")
            return 0

        apply_translated_gpu_policy(policy_controller, result["gpu_policy"])
        result = apply_afterburner_curve_to_reader(
            reader,
            profile_path=source["profile_path"],
            section=source["section"],
            allow_unsafe_curve=args.allow_unsafe_curve,
            gpu_policy=result["gpu_policy"],
        )
        print(
            f"Applied GPU policy: {describe_translated_gpu_policy(result['gpu_policy'])}"
        )
        print(
            f"SetControl(apply-plan): OK "
            f"matched={len(result['plan'])} changed={len(result['changed_points'])}"
        )

        if not args.no_write_config:
            persist_afterburner_import(
                config_path=args.config,
                gpu_index=args.gpu_index,
                afterburner_root=source["afterburner_root"],
                device_profile_relative_path=source["device_profile_relative_path"],
                section=source["section"],
                runtime_options=runtime_options,
            )
            print(f"Persisted active Afterburner import to {args.config}")

        reader.refresh_points()
        print("Readback after apply:")
        summarize_plan(
            build_plan_from_offsets_payload(
                reader,
                load_offsets_payload(result["translated_linux_profile_path"]),
            )[0]
        )
        return 0
    finally:
        policy_controller.close()
        reader.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except AfterburnerVfCurveSafetyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except AfterburnerProfileSelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
