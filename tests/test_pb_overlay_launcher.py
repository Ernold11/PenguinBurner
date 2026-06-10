from __future__ import annotations

from penguin_burner_overlay import launcher
from penguin_burner_overlay.state import OVERLAY_ENABLE_ENV, OVERLAY_STATE_ENV


def test_pb_overlay_launcher_execs_with_layer_environment(monkeypatch, tmp_path) -> None:
    calls = []
    popen_calls = []
    state_path = tmp_path / "overlay-state.txt"
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(state_path))

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
    assert popen_calls
