from __future__ import annotations

from overlay import launcher
from overlay.config import OVERLAY_CONFIG_ENV
from overlay.config import OverlayConfig
from overlay.config import save_overlay_config
from overlay.native_layer import NATIVE_LAYER_DIR_ENV
from overlay.native_layer import NATIVE_LAYER_LIBRARY
from overlay.native_layer import NATIVE_LAYER_MANIFEST
from overlay.state import (
    OVERLAY_ENABLE_ENV_ALIAS,
    OVERLAY_ENABLE_ENV,
    OVERLAY_STATE_ENV,
    OVERLAY_TEXT_ENV,
)


def test_pb_overlay_launcher_execs_with_layer_environment(monkeypatch, tmp_path) -> None:
    calls = []
    state_path = tmp_path / "overlay-state.txt"
    text_path = tmp_path / "overlay-text.txt"
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(state_path))
    monkeypatch.setenv(OVERLAY_TEXT_ENV, str(text_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    native_layer_dir = _fake_native_layer_dir(tmp_path)
    monkeypatch.setenv(NATIVE_LAYER_DIR_ENV, str(native_layer_dir))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("LD_PRELOAD", "steam-overlay.so")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/steam/runtime")
    monkeypatch.setenv("PRESSURE_VESSEL_RUNTIME", "steamrt")

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game", "--arg"])
    except RuntimeError:
        pass

    file, args, env = calls[0]
    assert file == "game"
    assert args == ["game", "--arg"]
    assert env["PENGUIN_BURNER"] == "1"
    assert env["PENGUIN_BURNER_LATENCY_LAYER"] == "1"
    assert env[OVERLAY_ENABLE_ENV] == "auto"
    assert env["PENGUIN_BURNER_LATENCY_SOCKET"].endswith(
        "/.cache/penguin-burner/latency.sock"
    )
    assert env["DXVK_NVAPI_VKREFLEX"] == "1"
    assert env["PROTON_ENABLE_NVAPI"] == "1"
    assert env["PROTON_HIDE_NVIDIA_GPU"] == "0"
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert "DXVK_NVAPI_LOG_LEVEL" not in env
    assert "PROTON_LOG" not in env
    assert "VK_LAYER_PENGUINBURNER_latency" in env["VK_LOADER_LAYERS_ENABLE"]
    assert "VK_LAYER_DXVK_NVAPI_reflex" in env["VK_LOADER_LAYERS_ENABLE"]
    assert str(native_layer_dir) in env["VK_ADD_IMPLICIT_LAYER_PATH"]
    assert "third_party/dxvk-nvapi/build.layer" in env["VK_ADD_IMPLICIT_LAYER_PATH"]
    assert env[OVERLAY_STATE_ENV] == str(state_path)
    assert env[OVERLAY_TEXT_ENV] == str(text_path)


def test_configure_environment_arms_live_overlay_config_by_default(tmp_path) -> None:
    env = {OVERLAY_CONFIG_ENV: str(tmp_path / "overlay.toml")}
    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "auto"


def test_configure_environment_uses_live_overlay_config_when_global_config_exists(
    tmp_path,
) -> None:
    path = tmp_path / "overlay.toml"
    save_overlay_config(OverlayConfig(enabled=True), path)
    env = {OVERLAY_CONFIG_ENV: str(path)}

    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "auto"
    assert env[OVERLAY_CONFIG_ENV] == str(path)


def test_configure_environment_accepts_short_overlay_alias() -> None:
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        OVERLAY_ENABLE_ENV_ALIAS: "1",
    }
    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "1"


def test_configure_environment_keeps_explicit_overlay_env_over_alias() -> None:
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        OVERLAY_ENABLE_ENV: "0",
        OVERLAY_ENABLE_ENV_ALIAS: "1",
    }
    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "0"


def test_configure_environment_prefers_marker_log_with_patched_prefix(tmp_path) -> None:
    off = {OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml"}
    launcher.configure_penguin_burner_environment(off)
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in off
    assert "DXVK_NVAPI_LOG_LEVEL" not in off

    _write_prefix_nvapi(tmp_path, supports_marker_log=True)
    on = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PENGUIN_BURNER_INGAME_LATENCY": "1",
        "STEAM_COMPAT_DATA_PATH": str(tmp_path),
        # Exercise the marker-log/trace fallback selector in isolation; the
        # preferred NVAPI shim path is covered by tests/test_shim_deploy.py.
        "PENGUIN_BURNER_NVAPI_SHIM_DISABLE": "1",
    }
    launcher.configure_penguin_burner_environment(on)
    assert on["DXVK_NVAPI_LATENCY_MARKER_LOG"] == "1"
    assert "DXVK_NVAPI_LOG_LEVEL" not in on
    assert "PROTON_LOG" not in on


def test_configure_environment_falls_back_to_trace_with_unpatched_prefix(tmp_path) -> None:
    _write_prefix_nvapi(tmp_path, supports_marker_log=False)
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_INGAME_LATENCY": "1",
        "STEAM_COMPAT_DATA_PATH": str(tmp_path),
        "PENGUIN_BURNER_NVAPI_SHIM_DISABLE": "1",
    }

    launcher.configure_penguin_burner_environment(env)

    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert env["DXVK_NVAPI_LOG_LEVEL"] == "trace"


def test_configure_environment_ignores_patched_nvofapi_without_nvapi(tmp_path) -> None:
    _write_prefix_nvapi(tmp_path, supports_marker_log=False)
    nvofapi = tmp_path / "pfx/drive_c/windows/system32/nvofapi64.dll"
    nvofapi.write_bytes(b"DXVK_NVAPI_LATENCY_MARKER_LOG")
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_INGAME_LATENCY": "1",
        "STEAM_COMPAT_DATA_PATH": str(tmp_path),
        "PENGUIN_BURNER_NVAPI_SHIM_DISABLE": "1",
    }

    launcher.configure_penguin_burner_environment(env)

    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert env["DXVK_NVAPI_LOG_LEVEL"] == "trace"


def test_configure_environment_detects_marker_log_in_proton_command_path(
    tmp_path,
) -> None:
    proton = tmp_path / "Proton-Test"
    marker_dll = (
        proton / "files/lib/wine/nvapi/x86_64-windows/nvapi64.dll"
    )
    marker_dll.parent.mkdir(parents=True)
    marker_dll.write_bytes(b"abc DXVK_NVAPI_LATENCY_MARKER_LOG xyz")
    proton.joinpath("proton").write_text("#!/bin/sh\n", encoding="utf-8")

    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_INGAME_LATENCY": "1",
    }

    launcher.configure_penguin_burner_environment(
        env,
        command_args=[str(proton / "proton"), "waitforexitandrun"],
    )

    assert env["DXVK_NVAPI_LATENCY_MARKER_LOG"] == "1"
    assert "DXVK_NVAPI_LOG_LEVEL" not in env


def test_configure_environment_keeps_explicit_trace_last_resort() -> None:
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_INGAME_LATENCY": "1",
        "DXVK_NVAPI_LOG_LEVEL": "trace",
    }

    launcher.configure_penguin_burner_environment(env)

    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert env["DXVK_NVAPI_LOG_LEVEL"] == "trace"


def test_explicit_ingame_latency_zero_overrides_latency_alias() -> None:
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PENGUIN_BURNER_INGAME_LATENCY": "0",
        "PB_INGAME_LATENCY": "1",
    }

    launcher.configure_penguin_burner_environment(env)

    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert "DXVK_NVAPI_LOG_LEVEL" not in env
    assert "PENGUIN_BURNER_LATENCY_DISPLAY" not in env
    assert "PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID" not in env


def test_ingame_latency_also_enables_display_tail() -> None:
    off = {OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml"}
    launcher.configure_penguin_burner_environment(off)
    assert "PENGUIN_BURNER_LATENCY_DISPLAY" not in off
    assert "PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID" not in off

    on = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_INGAME_LATENCY": "1",
    }
    launcher.configure_penguin_burner_environment(on)
    assert on["PENGUIN_BURNER_LATENCY_DISPLAY"] == "1"
    assert on["PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID"] == "1"
    assert "PENGUIN_BURNER_LATENCY_DEBUG_FLOW" not in on


def test_configure_environment_keeps_global_latency_config_full_path(tmp_path) -> None:
    path = tmp_path / "overlay.toml"
    save_overlay_config(
        OverlayConfig(enabled=True, enabled_item_ids=("base_fps", "latency_ms")),
        path,
    )
    env = {OVERLAY_CONFIG_ENV: str(path)}

    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "auto"
    assert "PENGUIN_BURNER_INGAME_LATENCY" not in env
    assert "PB_INGAME_LATENCY" not in env
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert "DXVK_NVAPI_LOG_LEVEL" not in env


def test_pb_overlay_launcher_strips_mangohud_preload(monkeypatch, tmp_path) -> None:
    calls = []
    state_path = tmp_path / "overlay-state.txt"
    text_path = tmp_path / "overlay-text.txt"
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(state_path))
    monkeypatch.setenv(OVERLAY_TEXT_ENV, str(text_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    monkeypatch.setenv(
        "LD_PRELOAD",
        "steam-overlay.so:/run/host/usr/lib64/mangohud/libMangoHud_shim.so",
    )
    monkeypatch.setenv("MANGOHUD", "1")
    monkeypatch.setenv("MANGOHUD_CONFIG", "fps")
    monkeypatch.setenv("MANGOAPP_CONFIG", "fps")

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game"])
    except RuntimeError:
        pass

    env = calls[0][2]
    assert env["LD_PRELOAD"] == "steam-overlay.so"
    assert "MANGOHUD" not in env
    assert "MANGOHUD_CONFIG" not in env
    assert "MANGOAPP_CONFIG" not in env


def test_trace_fifo_path_is_in_cache_dir() -> None:
    p = launcher.trace_fifo_path({"HOME": "/home/jp"})
    assert str(p) == "/home/jp/.cache/penguin-burner/nvapi-trace.fifo"


def _fake_native_layer_dir(tmp_path):
    path = tmp_path / "native-layer"
    path.mkdir()
    (path / NATIVE_LAYER_MANIFEST).write_text("{}", encoding="utf-8")
    (path / NATIVE_LAYER_LIBRARY).write_bytes(b"")
    return path


def _write_prefix_nvapi(tmp_path, *, supports_marker_log: bool) -> None:
    dll = tmp_path / "pfx/drive_c/windows/system32/nvapi64.dll"
    dll.parent.mkdir(parents=True)
    marker = b"DXVK_NVAPI_LATENCY_MARKER_LOG" if supports_marker_log else b""
    dll.write_bytes(b"prefix-nvapi " + marker)
