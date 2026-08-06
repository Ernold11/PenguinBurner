from __future__ import annotations

USER_EDITED_MEMORY_OFFSET_SOURCE = "user-edited"

_EXCLUDED_PARENT_KEYS = {
    "profile_id",
    "profile_created_at",
    "path",
    "verified_at",
    "avg_core_clock_mhz",
    "avg_fps",
    "avg_power_w",
    "efficiency_fps_per_w",
    "efficiency_mhz_per_w",
    "watts_per_mhz",
    "final_validation",
    "validation",
    "verification",
}


def editable_memory_offset_from_profile(profile: dict) -> int | None:
    raw_offset = profile.get("memory_offset_mhz")
    if raw_offset is None:
        return None
    try:
        return round(float(raw_offset))
    except (TypeError, ValueError):
        return None


def user_edited_memory_offset_profile_payload(
    parent_profile: dict,
    new_memory_offset_mhz: int,
    *,
    original_memory_offset_mhz: int | None = None,
) -> dict:
    payload = {
        key: value
        for key, value in dict(parent_profile).items()
        if key not in _EXCLUDED_PARENT_KEYS
    }
    parent_profile_id = str(parent_profile.get("profile_id", "")).strip()
    parent_path = str(parent_profile.get("path", "")).strip()
    payload.update(
        {
            "profile_source": USER_EDITED_MEMORY_OFFSET_SOURCE,
            "display_name": (
                f"User edited memory offset {int(new_memory_offset_mhz):+d} MT/s"
            ),
            "memory_offset_mhz": int(new_memory_offset_mhz),
            "final_verified": False,
            "verification_status": "unverified",
            "requires_verification": True,
            "manual_edit": {
                "edit_kind": "memory-offset",
                "parent_profile_id": parent_profile_id,
                "parent_path": parent_path,
                "original_memory_offset_mhz": (
                    None
                    if original_memory_offset_mhz is None
                    else int(original_memory_offset_mhz)
                ),
                "new_memory_offset_mhz": int(new_memory_offset_mhz),
            },
        }
    )
    return payload
