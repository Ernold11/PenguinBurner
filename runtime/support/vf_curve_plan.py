from __future__ import annotations

import json
import time
from pathlib import Path

from common.penguin_burner_paths import claim_desktop_user_ownership
from drivers.nvidia.nvml_gpu_policy import apply_translated_gpu_policy
from drivers.nvidia.nvml_gpu_policy import fixed_power_limit_excluded_by_identity


def apply_plan(reader, plan: list[dict]) -> None:
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
        power_limits = (
            {}
            if _fixed_power_limit_excluded(policy_controller)
            else policy_controller.query_power_limits()
        )
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
        if _fixed_power_limit_excluded(policy_controller):
            gpu_policy = dict(gpu_policy)
            gpu_policy.pop("power_limit_w", None)
            gpu_policy.pop("power_limit_default_w", None)
            gpu_policy.pop("power_limit_min_w", None)
            gpu_policy.pop("power_limit_max_w", None)
        apply_translated_gpu_policy(policy_controller, gpu_policy)
    return apply_offsets_payload(reader, payload)


def _fixed_power_limit_excluded(policy_controller) -> bool:
    query_gpu_name = getattr(policy_controller, "query_gpu_name", None)
    query_pci_device_id = getattr(policy_controller, "query_pci_device_id", None)
    try:
        gpu_name = query_gpu_name() if callable(query_gpu_name) else None
    except Exception:
        gpu_name = None
    try:
        pci_device_id = query_pci_device_id() if callable(query_pci_device_id) else None
    except Exception:
        pci_device_id = None
    return fixed_power_limit_excluded_by_identity(
        gpu_name=gpu_name,
        pci_device_id=pci_device_id,
    )
