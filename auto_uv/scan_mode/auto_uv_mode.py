"""Normalize the user-visible Auto-UV mode name.

Mode names are kept small because sweep behavior should branch only on efficiency versus performance.
"""

from __future__ import annotations


AUTO_UV_MODE_EFFICIENCY = "efficiency"
AUTO_UV_MODE_PERFORMANCE = "performance"
AUTO_UV_MODES = (AUTO_UV_MODE_EFFICIENCY, AUTO_UV_MODE_PERFORMANCE)

_AUTO_UV_MODE_ALIASES = {
    "": AUTO_UV_MODE_EFFICIENCY,
    "balanced": AUTO_UV_MODE_EFFICIENCY,
    "efficiency": AUTO_UV_MODE_EFFICIENCY,
    "aggressive": AUTO_UV_MODE_PERFORMANCE,
    "performance": AUTO_UV_MODE_PERFORMANCE,
}


def normalize_auto_uv_mode(value: object | None) -> str:
    normalized = str(value or "").strip().lower()
    return _AUTO_UV_MODE_ALIASES.get(normalized, AUTO_UV_MODE_EFFICIENCY)
