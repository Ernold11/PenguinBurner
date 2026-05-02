"""Scan mode modules define efficiency and performance scoring.

They are separated from the main loop so mode policy does not obscure voltage-search order.
"""

from .auto_uv_mode import (
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_PERFORMANCE,
    AUTO_UV_MODES,
    normalize_auto_uv_mode,
)

__all__ = [
    "AUTO_UV_MODE_EFFICIENCY",
    "AUTO_UV_MODE_PERFORMANCE",
    "AUTO_UV_MODES",
    "normalize_auto_uv_mode",
]
