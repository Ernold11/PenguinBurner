from __future__ import annotations

from runtime.support.adaptive_target_fps import (
    ADAPTIVE_TARGET_FPS_ENV,
    DEFAULT_ADAPTIVE_TARGET_FPS,
    adaptive_target_fps_from_config,
    adaptive_target_fps_from_env,
    adaptive_target_ms_from_fps,
    parse_adaptive_target_fps,
)
from cli.runtime_config_file import persist_adaptive_target_fps_to_runtime_config
from cli.runtime_config_file import persist_on_startup_from_runtime_config
from cli.runtime_config_file import persist_on_startup_to_runtime_config
from cli.runtime_config_file import silent_fan_curve_from_runtime_config
from cli.runtime_config_file import silent_fan_curve_to_runtime_config


def test_adaptive_target_fps_defaults_to_60() -> None:
    assert adaptive_target_fps_from_env({}) == DEFAULT_ADAPTIVE_TARGET_FPS
    assert adaptive_target_ms_from_fps(DEFAULT_ADAPTIVE_TARGET_FPS) == 1000.0 / 60.0


def test_adaptive_target_fps_reads_service_env() -> None:
    assert adaptive_target_fps_from_env({ADAPTIVE_TARGET_FPS_ENV: "50"}) == 50.0
    assert adaptive_target_ms_from_fps(50) == 20.0


def test_adaptive_target_fps_reads_runtime_config(tmp_path) -> None:
    path = tmp_path / "penguin_burner.toml"
    path.write_text("[adaptive]\ntarget_fps = 72\n", encoding="utf-8")

    assert adaptive_target_fps_from_config(path) == 72.0
    assert adaptive_target_fps_from_env({}, config_path=path) == 72.0
    assert (
        adaptive_target_fps_from_env(
            {ADAPTIVE_TARGET_FPS_ENV: "50"},
            config_path=path,
        )
        == 50.0
    )


def test_persist_on_startup_preference_round_trips_runtime_config(tmp_path) -> None:
    path = tmp_path / "penguin_burner.toml"
    path.write_text("[adaptive]\ntarget_fps = 72\n", encoding="utf-8")

    # The default only applies while the key has never been written.
    assert persist_on_startup_from_runtime_config(path, default=True) is True
    assert persist_on_startup_from_runtime_config(path, default=False) is False
    # Once written, the stored value is authoritative over any default.
    assert persist_on_startup_to_runtime_config(False, path) is False
    assert persist_on_startup_from_runtime_config(path, default=True) is False
    assert persist_on_startup_to_runtime_config(True, path) is True
    assert persist_on_startup_from_runtime_config(path, default=False) is True
    text = path.read_text(encoding="utf-8")
    assert "[adaptive]" in text
    assert "target_fps = 72" in text
    assert "[ui]" in text
    assert "persist_on_startup = true" in text


def test_adaptive_target_fps_persists_to_runtime_config(tmp_path) -> None:
    path = tmp_path / "penguin_burner.toml"
    path.write_text("[fan]\npoll_interval_s = 3\n", encoding="utf-8")

    persisted = persist_adaptive_target_fps_to_runtime_config(90, path)

    assert persisted == 90.0
    assert adaptive_target_fps_from_config(path) == 90.0
    text = path.read_text(encoding="utf-8")
    assert "[fan]" in text
    assert "poll_interval_s = 3" in text
    assert "[adaptive]" in text
    assert "target_fps = 90" in text


def test_silent_fan_curve_preference_round_trips_runtime_config(tmp_path) -> None:
    # The silent-fan choice must survive durably so the latest profile setup
    # (e.g. after an aborted Auto-UV run) restores it.
    path = tmp_path / "penguin_burner.toml"
    path.write_text("[adaptive]\ntarget_fps = 72\n", encoding="utf-8")

    assert silent_fan_curve_from_runtime_config(path, default=False) is False
    assert silent_fan_curve_to_runtime_config(True, path) is True
    assert silent_fan_curve_from_runtime_config(path, default=False) is True
    assert silent_fan_curve_to_runtime_config(False, path) is False
    assert silent_fan_curve_from_runtime_config(path, default=True) is False
    text = path.read_text(encoding="utf-8")
    assert "target_fps = 72" in text
    assert "[ui]" in text
    assert "silent_fan_curve = false" in text


def test_adaptive_target_fps_rejects_invalid_values() -> None:
    assert parse_adaptive_target_fps("0") == DEFAULT_ADAPTIVE_TARGET_FPS
    assert parse_adaptive_target_fps("nan") == DEFAULT_ADAPTIVE_TARGET_FPS
    assert parse_adaptive_target_fps("fast") == DEFAULT_ADAPTIVE_TARGET_FPS
    # Below 15 FPS pacing targets make no sense; fall back to the default.
    assert parse_adaptive_target_fps("14") == DEFAULT_ADAPTIVE_TARGET_FPS
    assert parse_adaptive_target_fps("1001") == DEFAULT_ADAPTIVE_TARGET_FPS
    assert parse_adaptive_target_fps("15") == 15.0
    assert parse_adaptive_target_fps("1000") == 1000.0
