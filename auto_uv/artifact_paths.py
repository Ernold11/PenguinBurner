from __future__ import annotations

from pathlib import Path

from penguin_burner_paths import default_saved_uv_dir, default_user_config_dir


def auto_uv_user_config_dir() -> Path:
    return default_user_config_dir()


def auto_uv_saved_uv_dir() -> Path:
    return default_saved_uv_dir()


def auto_uv_stop_request_path() -> Path:
    return auto_uv_user_config_dir() / "auto-uv-stop-requested"


def auto_uv_stop_requested() -> bool:
    return auto_uv_stop_request_path().exists()


def clear_auto_uv_stop_request() -> None:
    try:
        auto_uv_stop_request_path().unlink()
    except FileNotFoundError:
        pass
