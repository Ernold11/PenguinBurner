"""Scan mode modules define the small set of user-visible Auto-UV mode names."""

from .auto_uv_mode import (
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_PERFORMANCE,
    AUTO_UV_MODES,
    normalize_auto_uv_mode,
)

__all__ = [
    "AUTO_UV_MODE_EFFICIENCY",
    "AUTO_UV_MODE_BALANCED",
    "AUTO_UV_MODE_PERFORMANCE",
    "AUTO_UV_MODES",
    "normalize_auto_uv_mode",
]
