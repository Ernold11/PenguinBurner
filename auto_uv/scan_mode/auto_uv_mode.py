"""Normalize the user-visible Auto-UV preset/mode name."""

from __future__ import annotations


AUTO_UV_MODE_EFFICIENCY = "efficiency"
AUTO_UV_MODE_BALANCED = "balanced"
AUTO_UV_MODE_PERFORMANCE = "performance"
# One scan discovering all three tier profiles at once.
AUTO_UV_MODE_ADAPTIVE = "adaptive"
AUTO_UV_MODES = (
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_PERFORMANCE,
    AUTO_UV_MODE_ADAPTIVE,
)

_AUTO_UV_MODE_ALIASES = {
    "": AUTO_UV_MODE_EFFICIENCY,
    "balanced": AUTO_UV_MODE_BALANCED,
    "efficiency": AUTO_UV_MODE_EFFICIENCY,
    "aggressive": AUTO_UV_MODE_PERFORMANCE,
    "performance": AUTO_UV_MODE_PERFORMANCE,
    "adaptive": AUTO_UV_MODE_ADAPTIVE,
    "all": AUTO_UV_MODE_ADAPTIVE,
}


def normalize_auto_uv_mode(value: object | None) -> str:
    normalized = str(value or "").strip().lower()
    return _AUTO_UV_MODE_ALIASES.get(normalized, AUTO_UV_MODE_EFFICIENCY)
