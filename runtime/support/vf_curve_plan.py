from __future__ import annotations

import json
import time
from pathlib import Path

from common.penguin_burner_paths import claim_desktop_user_ownership
from integrations.afterburner.policy import apply_translated_gpu_policy


def _power_limit_set_supported(policy_controller) -> bool:
    probe = getattr(policy_controller, "power_limit_set_supported", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:
        return False


def apply_plan(reader, plan: list[dict]) -> None:
    offsets = [
        (int(item["index"]), int(item["new_offset_mhz"]) * 1000) for item in plan
    ]
    apply_offsets = getattr(reader, "apply_offsets_khz", None)
    if callable(apply_offsets):
        apply_offsets(offsets)
        return
    control = reader.get_control_struct()
    for item in plan:
        control.vf_points[item["index"]].prog.freq_offset_khz = (
            item["new_offset_mhz"] * 1000
        )
    reader.set_control_struct(control)


def load_offsets_payload(payload_path):
    return json.loads(Path(payload_path).read_text(encoding="utf-8", errors="replace"))


def apply_offsets_payload(reader, payload):
    offsets = {
        int(item["index"]): int(item["current_offset_mhz"])
        for item in payload["points"]
    }
    planned_offsets = [
        (int(point["index"]), offsets.get(int(point["index"]), 0) * 1000)
        for point in reader.editable_core_points()
    ]
    apply_offsets = getattr(reader, "apply_offsets_khz", None)
    if callable(apply_offsets):
        apply_offsets(planned_offsets)
        return len(offsets)

    control = reader.get_control_struct()
    for point in reader.editable_core_points():
        control.vf_points[point["index"]].prog.freq_offset_khz = (
            offsets.get(point["index"], 0) * 1000
        )
    reader.set_control_struct(control)
    return len(offsets)


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
        capabilities = policy_controller.capabilities()
        power = capabilities.power
        gpu_policy = {
            "mem_clk_vf_offset_mhz": capabilities.clock_offsets.memory_mhz,
        }
        # Only store power state after the daemon has proven that the setter
        # works by safely re-applying the current value. Readable limits alone
        # are not evidence of write support on mobile GPUs.
        if _power_limit_set_supported(policy_controller):
            gpu_policy.update(
                {
                    "power_limit_w": _rounded_watts(power.current_w),
                    "power_limit_default_w": _rounded_watts(power.default_w),
                    "power_limit_min_w": _rounded_watts(power.minimum_w),
                    "power_limit_max_w": _rounded_watts(power.maximum_w),
                }
            )
        payload["gpu_policy"] = gpu_policy
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(backup_path.parent, include_parents=True)
    backup_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    claim_desktop_user_ownership(backup_path)
    return backup_path


def _rounded_watts(value) -> int | None:
    return None if value is None else int(round(float(value)))


def restore_offsets(reader, backup_path, policy_controller=None):
    payload = load_offsets_payload(backup_path)
    gpu_policy = payload.get("gpu_policy")
    if policy_controller is not None and isinstance(gpu_policy, dict):
        apply_translated_gpu_policy(policy_controller, gpu_policy)
    return apply_offsets_payload(reader, payload)
