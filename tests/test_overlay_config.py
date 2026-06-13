from __future__ import annotations

from penguin_burner_overlay.config import ADVANCED_OVERLAY_ITEM_IDS
from penguin_burner_overlay.config import BASIC_OVERLAY_ITEM_IDS
from penguin_burner_overlay.config import OVERLAY_ITEM_IDS
from penguin_burner_overlay.config import OverlayConfig
from penguin_burner_overlay.config import load_overlay_config
from penguin_burner_overlay.config import save_overlay_config
from penguin_burner_overlay.config import OVERLAY_SCALE_OPTIONS
from penguin_burner_overlay.config import set_overlay_item_enabled
from penguin_burner_overlay.config import set_overlay_scale
from penguin_burner_overlay.config import set_overlay_update_interval_s
from penguin_burner_overlay.config import snap_overlay_scale


def test_overlay_config_defaults_to_hidden_base_items() -> None:
    config = load_overlay_config("/tmp/does-not-exist-pb-overlay.toml")

    assert config.enabled is False
    assert config.enabled_item_ids == BASIC_OVERLAY_ITEM_IDS
    assert config.update_interval_s == 2
    assert config.scale == 1.0


def test_overlay_config_keeps_gpu_and_cpu_util_in_advanced_items() -> None:
    assert "gpu_util_pct" in ADVANCED_OVERLAY_ITEM_IDS
    assert "cpu_util_pct" in ADVANCED_OVERLAY_ITEM_IDS
    assert "cpu_peak_thread_pct" in ADVANCED_OVERLAY_ITEM_IDS
    assert "gpu_util_pct" in OVERLAY_ITEM_IDS
    assert "cpu_util_pct" in OVERLAY_ITEM_IDS
    assert "cpu_peak_thread_pct" in OVERLAY_ITEM_IDS


def test_overlay_config_round_trips_ordered_items_and_interval(tmp_path) -> None:
    path = tmp_path / "overlay.toml"

    save_overlay_config(
        OverlayConfig(
            enabled=True,
            enabled_item_ids=("latency_ms", "base_fps", "fan_pct"),
            update_interval_s=9,
        ),
        path,
    )

    config = load_overlay_config(path)
    assert config.enabled is True
    assert config.enabled_item_ids == ("base_fps", "latency_ms", "fan_pct")
    assert config.update_interval_s == 9
    assert config.latency_enabled is True


def test_overlay_config_keeps_at_least_one_item_enabled() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("base_fps",))

    updated = set_overlay_item_enabled(config, "base_fps", False)

    assert updated.enabled_item_ids == ("base_fps",)


def test_overlay_config_clamps_update_interval() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("base_fps",))

    assert set_overlay_update_interval_s(config, 0).update_interval_s == 1
    assert set_overlay_update_interval_s(config, 11).update_interval_s == 10


def test_overlay_scale_round_trips(tmp_path) -> None:
    path = tmp_path / "overlay.toml"

    save_overlay_config(
        OverlayConfig(enabled=True, enabled_item_ids=("base_fps",), scale=2.0),
        path,
    )

    assert load_overlay_config(path).scale == 2.0


def test_overlay_scale_offers_half_one_and_double() -> None:
    assert OVERLAY_SCALE_OPTIONS == (0.5, 1.0, 2.0)


def test_overlay_scale_snaps_to_nearest_option() -> None:
    assert snap_overlay_scale(0.4) == 0.5
    assert snap_overlay_scale(0.9) == 1.0
    assert snap_overlay_scale(1.7) == 2.0
    assert snap_overlay_scale(5.0) == 2.0
    # Invalid or non-positive values fall back to the adaptive default.
    assert snap_overlay_scale("bogus") == 1.0
    assert snap_overlay_scale(0) == 1.0


def test_set_overlay_scale_preserves_other_fields() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=("base_fps", "latency_ms"),
        update_interval_s=7,
        scale=1.0,
    )

    updated = set_overlay_scale(config, 0.5)

    assert updated.scale == 0.5
    assert updated.enabled is True
    assert updated.update_interval_s == 7
    assert updated.enabled_item_ids == ("base_fps", "latency_ms")
