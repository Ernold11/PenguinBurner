"""Decide whether a user-edited profile needs a baseline comparison run.

Only user-edited drafts missing base metrics require this extra probe.
"""

from __future__ import annotations

from saved_uv_profiles import resolve_auto_uv_profile


def profile_needs_verify_baseline(selector: str) -> bool:
    try:
        resolved = resolve_auto_uv_profile(str(selector), allow_unverified=True)
    except Exception:
        return False
    if resolved is None:
        return False
    _path, profile = resolved
    if str(profile.get("profile_source", "")).strip() != "user-edited":
        return False
    return any(
        profile.get(key) in (None, "")
        for key in (
            "base_avg_core_clock_mhz",
            "base_avg_fps",
            "base_avg_power_w",
            "base_efficiency_fps_per_w",
        )
    )
