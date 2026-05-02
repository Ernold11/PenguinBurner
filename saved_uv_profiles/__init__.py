"""Saved undervolt profile storage and lookup.

This package is shared by the CLI, UI, LACT export, and Auto-UV final verification.
"""

from .profile_store import (
    archive_auto_uv_profile,
    auto_uv_profiles_dir,
    delete_auto_uv_profile_paths,
    delete_auto_uv_profiles,
    format_profile_table,
    mark_auto_uv_profile_verification_failed,
    mark_auto_uv_profile_verified,
    profile_display_name,
    profile_summary,
    read_auto_uv_profile_summaries,
    read_auto_uv_profiles,
    resolve_auto_uv_profile,
    wait_for_new_profile,
)
from .runtime_auto_uv_profile import (
    apply_auto_uv_profile_memory_offset,
    load_auto_uv_final_curve,
    profile_memory_offset_mhz,
)

__all__ = [
    "archive_auto_uv_profile",
    "auto_uv_profiles_dir",
    "delete_auto_uv_profile_paths",
    "delete_auto_uv_profiles",
    "format_profile_table",
    "mark_auto_uv_profile_verification_failed",
    "mark_auto_uv_profile_verified",
    "profile_display_name",
    "profile_summary",
    "read_auto_uv_profile_summaries",
    "read_auto_uv_profiles",
    "resolve_auto_uv_profile",
    "apply_auto_uv_profile_memory_offset",
    "load_auto_uv_final_curve",
    "profile_memory_offset_mhz",
    "wait_for_new_profile",
]
