from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


GPU_IDENTITY_KEY = "gpu_identity"
GPU_COMPATIBILITY_MATCH = "match"
GPU_COMPATIBILITY_LEGACY = "legacy"
GPU_COMPATIBILITY_MISMATCH = "mismatch"
GPU_COMPATIBILITY_TARGET_UNKNOWN = "target-unknown"


def normalized_gpu_identity(
    identity: object,
    *,
    index_at_verification: int | None = None,
) -> dict[str, str | int]:
    """Return the stable, JSON-safe identity stored with a verified profile."""

    raw = identity if isinstance(identity, Mapping) else None

    def value(key: str) -> object:
        if raw is not None:
            return raw.get(key)
        return getattr(identity, key, None)

    result: dict[str, str | int] = {}
    for key in ("name", "uuid", "pci_bus_id", "pci_device_id"):
        text = str(value(key) or "").strip()
        if text:
            result[key] = text

    raw_index = (
        index_at_verification
        if index_at_verification is not None
        else value("index_at_verification")
    )
    if raw_index is None:
        raw_index = value("index")
    try:
        if raw_index is not None:
            result["index_at_verification"] = max(0, int(str(raw_index)))
    except (TypeError, ValueError):
        pass
    return result


def profile_gpu_identity(profile: Mapping[str, Any] | object) -> dict[str, str | int]:
    if not isinstance(profile, Mapping):
        return {}
    identity = profile.get(GPU_IDENTITY_KEY)
    if not isinstance(identity, Mapping):
        return {}
    return normalized_gpu_identity(identity)


def profile_gpu_uuid(profile: Mapping[str, Any] | object) -> str:
    return str(profile_gpu_identity(profile).get("uuid") or "").strip()


def bound_profile_gpu_uuids(profiles: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    values: dict[str, str] = {}
    for profile in profiles:
        uuid = profile_gpu_uuid(profile)
        if uuid:
            values.setdefault(uuid.casefold(), uuid)
    return tuple(values[key] for key in sorted(values))


def profile_gpu_compatibility(
    profile: Mapping[str, Any] | object,
    target_uuid: object,
) -> str:
    profile_uuid = profile_gpu_uuid(profile)
    if not profile_uuid:
        return GPU_COMPATIBILITY_LEGACY
    selected_uuid = str(target_uuid or "").strip()
    if not selected_uuid:
        return GPU_COMPATIBILITY_TARGET_UNKNOWN
    if profile_uuid.casefold() == selected_uuid.casefold():
        return GPU_COMPATIBILITY_MATCH
    return GPU_COMPATIBILITY_MISMATCH


def gpu_index_for_uuid(identities: Iterable[object], target_uuid: object) -> int | None:
    selected_uuid = str(target_uuid or "").strip()
    if not selected_uuid:
        return None
    for identity in identities:
        raw = identity if isinstance(identity, Mapping) else None
        uuid = (
            str(raw.get("uuid") or "").strip()
            if raw is not None
            else str(getattr(identity, "uuid", "") or "").strip()
        )
        if uuid.casefold() != selected_uuid.casefold():
            continue
        raw_index = raw.get("index") if raw is not None else getattr(identity, "index", None)
        try:
            return max(0, int(str(raw_index)))
        except (TypeError, ValueError):
            return None
    return None


def gpu_identity_label(identity: Mapping[str, Any] | object) -> str:
    normalized = normalized_gpu_identity(identity)
    if not normalized:
        return "Unassigned (legacy)"
    name = str(normalized.get("name") or "").strip() or "NVIDIA GPU"
    bus = _short_bus_id(str(normalized.get("pci_bus_id") or ""))
    return f"{name} ({bus})" if bus else name


def profile_gpu_label(profile: Mapping[str, Any] | object) -> str:
    identity = profile_gpu_identity(profile)
    return gpu_identity_label(identity) if identity else "Unassigned (legacy)"


def _short_bus_id(bus_id: str) -> str:
    text = str(bus_id or "").strip()
    if not text:
        return ""
    parts = text.split(":")
    return ":".join(parts[-2:]) if len(parts) >= 2 else text
