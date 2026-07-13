from __future__ import annotations

import json
import time
from pathlib import Path

from common.penguin_burner_paths import claim_desktop_user_ownership
from drivers.nvidia.nvml_gpu_policy import fixed_power_limit_excluded_by_identity
from integrations.afterburner.policy import apply_translated_gpu_policy


def _fixed_power_limit_excluded(capabilities) -> bool:
    """Whether this GPU's board power limit is fixed (mobile / low-TGP).

    A fixed power limit cannot be set, so it must never be backed up or
    restored — doing so would fail on the apply. Identity-based, matching the
    0.6.6 mobile support."""
    identity = getattr(capabilities, "identity", None)
    return fixed_power_limit_excluded_by_identity(
        gpu_name=getattr(identity, "name", None),
        pci_device_id=getattr(identity, "pci_device_id", None),
    )


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
        # Mobile GPUs with a fixed board power limit cannot have it set, so
        # never store one — restore would then try to apply it and fail
        # (0.6.6 mobile support). The V/F offset restore is unaffected.
        if not _fixed_power_limit_excluded(capabilities):
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
