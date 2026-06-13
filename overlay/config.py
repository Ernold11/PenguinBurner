from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

from penguin_burner_paths import claim_desktop_user_ownership
from penguin_burner_paths import default_user_config_dir


OVERLAY_CONFIG_ENV = "PENGUIN_BURNER_OVERLAY_CONFIG"
STEAM_LAUNCH_OPTION = "PENGUIN_BURNER %command%"


def steam_launch_option(*, latency_enabled: bool = False) -> str:
    """Steam launch string for the overlay.

    ``PB_OVERLAY=1`` turns the overlay on. ``PB_INGAME_LATENCY=1`` is the single
    opt-in that enables the whole latency stack -- render (Reflex) AND the
    present->scanout display tail -- via overlay.launcher; it is omitted by
    default so a plain overlay launch measures no latency.
    """
    tokens = ["PB_OVERLAY=1"]
    if latency_enabled:
        tokens.append("PB_INGAME_LATENCY=1")
    tokens.append(STEAM_LAUNCH_OPTION)
    return " ".join(tokens)


# Overlay on, no latency -- the default copy string.
STEAM_LAUNCH_OPTION_OVERLAY = steam_launch_option()
# Overlay on with the single latency opt-in (render + display).
STEAM_LAUNCH_OPTION_WITH_LATENCY = steam_launch_option(latency_enabled=True)

BASIC_OVERLAY_ITEM_IDS = (
    "base_fps",
    "fg_fps",
    "clock_mhz",
    "voltage_mv",
    "power_w",
    "profile",
)
ADVANCED_OVERLAY_ITEM_IDS = (
    "gpu_util_pct",
    "cpu_util_pct",
    "cpu_peak_thread_pct",
    "fan_pct",
    "temperature_c",
    "latency_ms",
    "uv_offset_mv",
)
OVERLAY_ITEM_IDS = (
    "base_fps",
    "fg_fps",
    "latency_ms",
    "clock_mhz",
    "voltage_mv",
    "power_w",
    "profile",
    "gpu_util_pct",
    "cpu_util_pct",
    "cpu_peak_thread_pct",
    "fan_pct",
    "temperature_c",
    "uv_offset_mv",
)
DEFAULT_ENABLED_OVERLAY_ITEM_IDS = BASIC_OVERLAY_ITEM_IDS
DEFAULT_OVERLAY_UPDATE_INTERVAL_S = 2
MIN_OVERLAY_UPDATE_INTERVAL_S = 1
MAX_OVERLAY_UPDATE_INTERVAL_S = 10
# Manual overlay scale multiplier applied on top of the native overlay's
# resolution-adaptive (1080p/1440p/4K) sizing. 1.0 keeps the adaptive default;
# 0.5/2.0 shrink/grow it. Values are snapped to the nearest offered option.
OVERLAY_SCALE_OPTIONS = (0.5, 1.0, 2.0)
DEFAULT_OVERLAY_SCALE = 1.0


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    enabled: bool = False
    enabled_item_ids: tuple[str, ...] = DEFAULT_ENABLED_OVERLAY_ITEM_IDS
    update_interval_s: int = DEFAULT_OVERLAY_UPDATE_INTERVAL_S
    scale: float = DEFAULT_OVERLAY_SCALE

    @property
    def latency_enabled(self) -> bool:
        return bool(self.enabled) and "latency_ms" in self.enabled_item_ids


def default_overlay_config_path(
    env: dict[str, str] | None = None,
) -> Path:
    env = os.environ if env is None else env
    explicit = str(env.get(OVERLAY_CONFIG_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return default_user_config_dir() / "overlay.toml"


def default_overlay_config(*, enabled: bool = False) -> OverlayConfig:
    return OverlayConfig(
        enabled=bool(enabled),
        enabled_item_ids=DEFAULT_ENABLED_OVERLAY_ITEM_IDS,
        update_interval_s=DEFAULT_OVERLAY_UPDATE_INTERVAL_S,
        scale=DEFAULT_OVERLAY_SCALE,
    )


def load_overlay_config(path: str | Path | None = None) -> OverlayConfig:
    config_path = (
        default_overlay_config_path()
        if path is None
        else Path(path).expanduser()
    )
    try:
        with config_path.open("rb") as config_file:
            payload = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return default_overlay_config()
    if not isinstance(payload, dict):
        return default_overlay_config()
    return normalize_overlay_config(
        OverlayConfig(
            enabled=_bool_value(payload.get("enabled"), default=False),
            enabled_item_ids=normalize_overlay_item_ids(
                payload.get("items", DEFAULT_ENABLED_OVERLAY_ITEM_IDS)
            ),
            update_interval_s=clamp_overlay_update_interval_s(
                payload.get("update_interval_s", DEFAULT_OVERLAY_UPDATE_INTERVAL_S)
            ),
            scale=snap_overlay_scale(payload.get("scale", DEFAULT_OVERLAY_SCALE)),
        )
    )


def save_overlay_config(
    config: OverlayConfig,
    path: str | Path | None = None,
) -> Path:
    config = normalize_overlay_config(config)
    config_path = (
        default_overlay_config_path()
        if path is None
        else Path(path).expanduser()
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(config_path.parent, include_parents=True)
    text = (
        "version = 1\n"
        f"enabled = {_toml_bool(config.enabled)}\n"
        f"update_interval_s = {int(config.update_interval_s)}\n"
        f"scale = {float(config.scale)}\n"
        "items = ["
        + ", ".join(f'"{item_id}"' for item_id in config.enabled_item_ids)
        + "]\n"
    )
    temp_path = config_path.with_name(config_path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(config_path)
    claim_desktop_user_ownership(config_path)
    return config_path


def normalize_overlay_config(config: OverlayConfig) -> OverlayConfig:
    return OverlayConfig(
        enabled=bool(config.enabled),
        enabled_item_ids=normalize_overlay_item_ids(config.enabled_item_ids),
        update_interval_s=clamp_overlay_update_interval_s(config.update_interval_s),
        scale=snap_overlay_scale(config.scale),
    )


def normalize_overlay_item_ids(value: object) -> tuple[str, ...]:
    raw_ids: list[str] = []
    if isinstance(value, (list, tuple, set)):
        raw_ids = [str(item).strip() for item in value]
    elif isinstance(value, str):
        raw_ids = [item.strip() for item in value.split(",")]
    wanted = {item for item in raw_ids if item in OVERLAY_ITEM_IDS}
    ordered = tuple(item for item in OVERLAY_ITEM_IDS if item in wanted)
    return ordered or ("base_fps",)


def set_overlay_item_enabled(
    config: OverlayConfig,
    item_id: str,
    enabled: bool,
) -> OverlayConfig:
    item_id = str(item_id).strip()
    if item_id not in OVERLAY_ITEM_IDS:
        return normalize_overlay_config(config)
    current = set(normalize_overlay_item_ids(config.enabled_item_ids))
    if enabled:
        current.add(item_id)
    elif len(current) > 1:
        current.discard(item_id)
    return OverlayConfig(
        enabled=bool(config.enabled),
        enabled_item_ids=normalize_overlay_item_ids(tuple(current)),
        update_interval_s=clamp_overlay_update_interval_s(config.update_interval_s),
        scale=snap_overlay_scale(config.scale),
    )


def set_overlay_enabled(config: OverlayConfig, enabled: bool) -> OverlayConfig:
    return OverlayConfig(
        enabled=bool(enabled),
        enabled_item_ids=normalize_overlay_item_ids(config.enabled_item_ids),
        update_interval_s=clamp_overlay_update_interval_s(config.update_interval_s),
        scale=snap_overlay_scale(config.scale),
    )


def set_overlay_update_interval_s(
    config: OverlayConfig,
    value: object,
) -> OverlayConfig:
    return OverlayConfig(
        enabled=bool(config.enabled),
        enabled_item_ids=normalize_overlay_item_ids(config.enabled_item_ids),
        update_interval_s=clamp_overlay_update_interval_s(value),
        scale=snap_overlay_scale(config.scale),
    )


def set_overlay_scale(config: OverlayConfig, value: object) -> OverlayConfig:
    return OverlayConfig(
        enabled=bool(config.enabled),
        enabled_item_ids=normalize_overlay_item_ids(config.enabled_item_ids),
        update_interval_s=clamp_overlay_update_interval_s(config.update_interval_s),
        scale=snap_overlay_scale(value),
    )


def snap_overlay_scale(value: object) -> float:
    """Snap an arbitrary scale to the nearest offered option (0.5/1.0/2.0)."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_OVERLAY_SCALE
    if parsed <= 0:
        return DEFAULT_OVERLAY_SCALE
    return min(OVERLAY_SCALE_OPTIONS, key=lambda option: abs(option - parsed))


def clamp_overlay_update_interval_s(value: object) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = DEFAULT_OVERLAY_UPDATE_INTERVAL_S
    return max(
        MIN_OVERLAY_UPDATE_INTERVAL_S,
        min(MAX_OVERLAY_UPDATE_INTERVAL_S, parsed),
    )


def _bool_value(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _toml_bool(value: object) -> str:
    return "true" if bool(value) else "false"
