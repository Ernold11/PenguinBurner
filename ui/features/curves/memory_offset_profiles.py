from __future__ import annotations

from pathlib import Path

from profiles.uv.memory_offset_edit import user_edited_memory_offset_profile_payload
from profiles.uv.profile_store import archive_auto_uv_profile
from ui.features.curves.fan_profiles import profile_payload_from_path


def save_edited_memory_offset_profile(
    profile: dict,
    new_memory_offset_mhz: int,
    *,
    original_memory_offset_mhz: int | None = None,
) -> tuple[Path, dict]:
    parent_payload = profile_payload_from_path(profile) or dict(profile)
    payload = user_edited_memory_offset_profile_payload(
        parent_payload,
        int(new_memory_offset_mhz),
        original_memory_offset_mhz=original_memory_offset_mhz,
    )
    return archive_auto_uv_profile(payload), payload
