from __future__ import annotations

from dataclasses import dataclass

from profiles.gpu_identity import (
    GPU_COMPATIBILITY_LEGACY,
    GPU_COMPATIBILITY_MATCH,
    GPU_COMPATIBILITY_MISMATCH,
    GPU_COMPATIBILITY_TARGET_UNKNOWN,
    bound_profile_gpu_uuids,
    gpu_identity_label,
    gpu_index_for_uuid,
    normalized_gpu_identity,
    profile_gpu_compatibility,
    profile_gpu_identity,
    profile_gpu_label,
)


@dataclass
class Identity:
    index: int
    name: str
    uuid: str
    pci_bus_id: str
    pci_device_id: str


def _identity(index: int, uuid: str, *, name: str = "NVIDIA RTX 5090") -> Identity:
    return Identity(
        index=index,
        name=name,
        uuid=uuid,
        pci_bus_id=f"00000000:0{index + 1}:00.0",
        pci_device_id="0x2B8510DE",
    )


def test_normalized_gpu_identity_is_json_safe_and_keeps_stable_fields() -> None:
    assert normalized_gpu_identity(_identity(2, "GPU-C")) == {
        "name": "NVIDIA RTX 5090",
        "uuid": "GPU-C",
        "pci_bus_id": "00000000:03:00.0",
        "pci_device_id": "0x2B8510DE",
        "index_at_verification": 2,
    }


def test_profile_gpu_identity_rejects_flat_or_invalid_legacy_payloads() -> None:
    assert profile_gpu_identity({"gpu_name": "old value", "gpu_index": 1}) == {}
    assert profile_gpu_identity({"gpu_identity": "GPU-A"}) == {}
    assert profile_gpu_label({}) == "Unassigned (legacy)"


def test_gpu_identity_label_keeps_name_and_short_pci_bus() -> None:
    assert (
        gpu_identity_label(normalized_gpu_identity(_identity(2, "GPU-C")))
        == "NVIDIA RTX 5090 (03:00.0)"
    )


def test_profile_compatibility_uses_uuid_not_name_or_device_id() -> None:
    profile = {"gpu_identity": normalized_gpu_identity(_identity(0, "GPU-A"))}
    assert profile_gpu_compatibility(profile, "gpu-a") == GPU_COMPATIBILITY_MATCH
    assert profile_gpu_compatibility(profile, "GPU-B") == GPU_COMPATIBILITY_MISMATCH
    assert profile_gpu_compatibility(profile, "") == GPU_COMPATIBILITY_TARGET_UNKNOWN
    assert profile_gpu_compatibility({}, "GPU-A") == GPU_COMPATIBILITY_LEGACY


def test_bound_profile_gpu_uuids_are_unique_for_identical_models() -> None:
    profiles = [
        {"gpu_identity": normalized_gpu_identity(_identity(0, "GPU-B"))},
        {"gpu_identity": normalized_gpu_identity(_identity(1, "GPU-A"))},
        {"gpu_identity": normalized_gpu_identity(_identity(2, "gpu-b"))},
        {},
    ]
    assert bound_profile_gpu_uuids(profiles) == ("GPU-A", "GPU-B")


def test_gpu_index_is_resolved_from_uuid_after_index_reordering() -> None:
    identities = [_identity(0, "GPU-C"), _identity(1, "GPU-A"), _identity(2, "GPU-B")]
    assert gpu_index_for_uuid(identities, "gpu-a") == 1
    assert gpu_index_for_uuid(identities, "GPU-missing") is None
