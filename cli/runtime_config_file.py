"""Read and write the PenguinBurner runtime TOML config used by CLI commands.

The module owns config defaults and small persistence updates; runtime behavior stays elsewhere.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

from afterburner.import_fan_curve import write_config as write_runtime_config
from runtime_support.adaptive_target_fps import (
    ADAPTIVE_TARGET_FPS_CONFIG_KEY,
    ADAPTIVE_TARGET_FPS_CONFIG_SECTION,
    DEFAULT_ADAPTIVE_TARGET_FPS,
    parse_adaptive_target_fps,
)
from auto_uv.auto_uv_user_options import AUTO_UV_DEFAULTS, AUTO_UV_METRIC_TUNING
from common.penguin_burner_paths import default_runtime_config_path

UI_CONFIG_SECTION = "ui"
PERSIST_ON_STARTUP_CONFIG_KEY = "persist_on_startup"


def default_config_path():
    return default_runtime_config_path()


def default_runtime_config() -> dict:
    return {
        "gpu": {
            "index": 0,
            "enable_persistence_mode": True,
            "afterburner_auto_uv_max_drop_pct": 16.0,
            "auto_uv_final_seconds": AUTO_UV_DEFAULTS.final_duration_s,
            "auto_uv_efficiency_stop_streak": AUTO_UV_DEFAULTS.efficiency_stop_streak,
            "auto_uv_min_efficiency_stop_drop_pct": (
                AUTO_UV_METRIC_TUNING.min_efficiency_stop_voltage_drop_pct
            ),
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
        "stability": {
            "q2rtx_dir": "",
            "q2rtx_binary": "",
        },
        "adaptive": {
            "target_fps": DEFAULT_ADAPTIVE_TARGET_FPS,
        },
        UI_CONFIG_SECTION: {
            PERSIST_ON_STARTUP_CONFIG_KEY: False,
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


def persist_q2rtx_source_to_runtime_config(
    config_path,
    *,
    q2rtx_dir,
    q2rtx_binary,
    progress_context,
) -> None:
    path = Path(config_path).expanduser()
    config = load_raw_runtime_config(path)
    gpu = dict(config.get("gpu", {}))
    fan = dict(config.get("fan", {}))
    stability = dict(config.get("stability", {}))
    stability["q2rtx_dir"] = str(q2rtx_dir) if q2rtx_dir else ""
    stability["q2rtx_binary"] = str(q2rtx_binary) if q2rtx_binary else ""
    config["gpu"] = gpu
    config["fan"] = fan
    config["stability"] = stability
    write_runtime_config(path, config)
    print(f"{progress_context}: saved Q2RTX source to {path}", flush=True)


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


def persist_on_startup_from_runtime_config(
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
    value = raw_ui.get(PERSIST_ON_STARTUP_CONFIG_KEY)
    if value is None:
        return bool(default)
    return _config_bool(value, default=default)


def persist_on_startup_to_runtime_config(
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
    ui[PERSIST_ON_STARTUP_CONFIG_KEY] = normalized
    config[UI_CONFIG_SECTION] = ui
    write_runtime_config(path, config)
    return normalized


def afterburner_root_has_imported_profiles(afterburner_root) -> bool:
    root_text = str(afterburner_root or "").strip()
    if not root_text:
        return False
    root = Path(root_text).expanduser()
    return (root / "MSIAfterburner.cfg").is_file() and (root / "Profiles").is_dir()


def _config_bool(value, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)
