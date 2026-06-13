from __future__ import annotations

from pathlib import Path

from .assets import _effective_q2rtx_xdg_dir
from .constants import (
    DEFAULT_INSTALL_CACHE_DIR,
    DEFAULT_INSTALL_DATA_DIR,
    OPENSSL_111_VERSION,
)


def default_q2rtx_install_data_dir() -> Path:
    xdg_data_home = _effective_q2rtx_xdg_dir("data")
    if xdg_data_home is not None:
        return xdg_data_home / "PenguinBurner" / "q2rtx"
    return DEFAULT_INSTALL_DATA_DIR


def default_q2rtx_install_cache_dir() -> Path:
    xdg_cache_home = _effective_q2rtx_xdg_dir("cache")
    if xdg_cache_home is not None:
        return xdg_cache_home / "PenguinBurner" / "q2rtx"
    return DEFAULT_INSTALL_CACHE_DIR


def default_q2rtx_compat_dir() -> Path:
    return (
        default_q2rtx_install_data_dir() / "compat" / f"openssl-{OPENSSL_111_VERSION}"
    )
