"""Read and write the PenguinBurner runtime TOML config used by CLI commands.

The module owns config defaults and small persistence updates; runtime behavior stays elsewhere.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

from integrations.afterburner.import_fan_curve import write_config as write_runtime_config
from runtime.support.adaptive_target_fps import (
    ADAPTIVE_TARGET_FPS_CONFIG_KEY,
    ADAPTIVE_TARGET_FPS_CONFIG_SECTION,
    DEFAULT_ADAPTIVE_TARGET_FPS,
    parse_adaptive_target_fps,
)
from common.penguin_burner_paths import default_runtime_config_path

UI_CONFIG_SECTION = "ui"
# Retired 2026-07 (applying a profile now always persists it as the boot
# profile); a stale key in existing configs is simply ignored.
SILENT_FAN_CURVE_CONFIG_KEY = "silent_fan_curve"


def default_config_path():
    return default_runtime_config_path()


def default_runtime_config() -> dict:
    return {
        "gpu": {
            "index": 0,
            "enable_persistence_mode": True,
        },
        "fan": {
            "poll_interval_s": 2,
            "hysteresis_c": 2.0,
            "mode": "linear",
            "min_fan_speed_pct": 20,
            "max_fan_speed_pct": 100,
            "max_step_up_pct_per_s": 25.0,
            "max_step_down_pct_per_s": 15.0,
            "manual_enable_temp_c": 55.0,
            "auto_restore_temp_c": 50.0,
            "emergency_auto_override_temp_c": 80.0,
            "emergency_auto_resume_temp_c": 75.0,
            "force_update_every_poll": False,
            "curve": [
                [55, 30],
                [65, 35],
                [70, 40],
                [80, 45],
            ],
        },
        "stability": {},
        "adaptive": {
            "target_fps": DEFAULT_ADAPTIVE_TARGET_FPS,
        },
        UI_CONFIG_SECTION: {
            SILENT_FAN_CURVE_CONFIG_KEY: False,
        },
    }


def load_runtime_config(config_path=None):
    path = default_config_path() if config_path is None else Path(config_path).expanduser()
    config = default_runtime_config()
    if not path.exists():
        return config, path

    loaded = load_raw_runtime_config(path)
    for section in ("gpu", "fan", "stability", "adaptive", UI_CONFIG_SECTION):
        values = loaded.get(section)
        if isinstance(values, dict):
            config[section].update(values)
    return config, path


def load_raw_runtime_config(config_path):
    path = Path(config_path).expanduser()
    if not path.exists():
        return {}
    with path.open("rb") as config_file:
        return tomllib.load(config_file)


def persist_adaptive_target_fps_to_runtime_config(
    target_fps,
    config_path=None,
) -> float:
    path = (
        default_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    config = load_raw_runtime_config(path)
    raw_adaptive = config.get(ADAPTIVE_TARGET_FPS_CONFIG_SECTION, {})
    adaptive = dict(raw_adaptive) if isinstance(raw_adaptive, dict) else {}
    normalized = parse_adaptive_target_fps(target_fps)
    adaptive[ADAPTIVE_TARGET_FPS_CONFIG_KEY] = normalized
    config[ADAPTIVE_TARGET_FPS_CONFIG_SECTION] = adaptive
    write_runtime_config(path, config)
    return normalized


def silent_fan_curve_from_runtime_config(
    config_path=None,
    *,
    default: bool = False,
) -> bool:
    path = (
        default_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    config = load_raw_runtime_config(path)
    raw_ui = config.get(UI_CONFIG_SECTION, {})
    if not isinstance(raw_ui, dict):
        return bool(default)
    value = raw_ui.get(SILENT_FAN_CURVE_CONFIG_KEY)
    if value is None:
        return bool(default)
    return _config_bool(value, default=default)


def silent_fan_curve_to_runtime_config(
    enabled,
    config_path=None,
) -> bool:
    path = (
        default_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    config = load_raw_runtime_config(path)
    raw_ui = config.get(UI_CONFIG_SECTION, {})
    ui = dict(raw_ui) if isinstance(raw_ui, dict) else {}
    normalized = bool(enabled)
    ui[SILENT_FAN_CURVE_CONFIG_KEY] = normalized
    config[UI_CONFIG_SECTION] = ui
    write_runtime_config(path, config)
    return normalized


def _config_bool(value, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)
