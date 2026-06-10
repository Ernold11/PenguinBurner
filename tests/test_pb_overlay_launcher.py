from __future__ import annotations

from penguin_burner_overlay import launcher
from penguin_burner_overlay.state import (
    OVERLAY_ENABLE_ENV,
    OVERLAY_STATE_ENV,
    OVERLAY_TEXT_ENV,
)


def test_pb_overlay_launcher_execs_with_layer_environment(monkeypatch, tmp_path) -> None:
    calls = []
    popen_calls = []
    state_path = tmp_path / "overlay-state.txt"
    text_path = tmp_path / "overlay-text.txt"
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(state_path))
    monkeypatch.setenv(OVERLAY_TEXT_ENV, str(text_path))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("LD_PRELOAD", "steam-overlay.so")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/steam/runtime")
    monkeypatch.setenv("PRESSURE_VESSEL_RUNTIME", "steamrt")

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )

    try:
        launcher.main(["game", "--arg"])
    except RuntimeError:
        pass

    file, args, env = calls[0]
    assert file == "game"
    assert args == ["game", "--arg"]
    assert env["PENGUIN_BURNER_LATENCY_LAYER"] == "1"
    assert env[OVERLAY_ENABLE_ENV] == "1"
    assert env["DXVK_NVAPI_VKREFLEX"] == "1"
    assert env["PROTON_ENABLE_NVAPI"] == "1"
    assert env[OVERLAY_STATE_ENV] == str(state_path)
    assert env[OVERLAY_TEXT_ENV] == str(text_path)
    assert popen_calls
    display_env = popen_calls[0][1]["env"]
    assert display_env["QT_QPA_PLATFORM"] == "xcb"
    assert "LD_PRELOAD" not in display_env
    assert "LD_LIBRARY_PATH" not in display_env
    assert "PRESSURE_VESSEL_RUNTIME" not in display_env


def test_pb_overlay_display_env_uses_wayland_without_x11() -> None:
    display_env = launcher._display_process_env(
        {
            "WAYLAND_DISPLAY": "wayland-0",
            "LD_PRELOAD": "steam-overlay.so",
        }
    )

    assert display_env["QT_QPA_PLATFORM"] == "wayland"
    assert "LD_PRELOAD" not in display_env
