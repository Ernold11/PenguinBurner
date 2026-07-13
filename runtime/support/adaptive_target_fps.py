from __future__ import annotations

from collections.abc import Mapping
import math
import os
from pathlib import Path
import tomllib

from common.penguin_burner_paths import default_runtime_config_path


ADAPTIVE_TARGET_FPS_ENV = "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS"
ADAPTIVE_TARGET_FPS_ENV_ALIAS = "PB_ADAPTIVE_TARGET_FPS"
ADAPTIVE_TARGET_FPS_ENV_NAMES = (
    ADAPTIVE_TARGET_FPS_ENV,
    ADAPTIVE_TARGET_FPS_ENV_ALIAS,
)
DEFAULT_ADAPTIVE_TARGET_FPS = 60.0
# Below ~15 FPS present-frame pacing stops being meaningful for tier
# decisions; out-of-range values fall back to the 60 FPS default.
MIN_ADAPTIVE_TARGET_FPS = 15.0
MAX_ADAPTIVE_TARGET_FPS = 1000.0
ADAPTIVE_TARGET_FPS_CONFIG_SECTION = "adaptive"
ADAPTIVE_TARGET_FPS_CONFIG_KEY = "target_fps"


def parse_adaptive_target_fps(
    value: object,
    *,
    default: float = DEFAULT_ADAPTIVE_TARGET_FPS,
) -> float:
    try:
        fps = float(str(value or "").strip())
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(fps):
        return float(default)
    if fps < MIN_ADAPTIVE_TARGET_FPS or fps > MAX_ADAPTIVE_TARGET_FPS:
        return float(default)
    return fps


def adaptive_target_fps_from_env(
    env: Mapping[str, str] | None = None,
    *,
    default: float = DEFAULT_ADAPTIVE_TARGET_FPS,
    config_path: str | Path | None = None,
) -> float:
    explicit_env = env is not None
    resolved_env = os.environ if env is None else env
    for name in ADAPTIVE_TARGET_FPS_ENV_NAMES:
        raw = str(resolved_env.get(name) or "").strip()
        if raw:
            return parse_adaptive_target_fps(raw, default=default)
    if not explicit_env or config_path is not None:
        return adaptive_target_fps_from_config(config_path, default=default)
    return float(default)


def adaptive_target_fps_from_config(
    config_path: str | Path | None = None,
    *,
    default: float = DEFAULT_ADAPTIVE_TARGET_FPS,
) -> float:
    path = (
        default_runtime_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return float(default)
    try:
        value = config.get(ADAPTIVE_TARGET_FPS_CONFIG_SECTION, {}).get(
            ADAPTIVE_TARGET_FPS_CONFIG_KEY,
            "",
        )
    except AttributeError:
        return float(default)
    if value in (None, ""):
        return float(default)
    return parse_adaptive_target_fps(value, default=default)


def adaptive_target_ms_from_fps(fps: object) -> float:
    target_fps = parse_adaptive_target_fps(fps)
    return 1000.0 / target_fps
