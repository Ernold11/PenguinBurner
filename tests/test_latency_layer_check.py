from __future__ import annotations

import subprocess

from latency_telemetry import layer_check
from overlay.native_layer import NATIVE_LAYER_LIBRARY
from overlay.native_layer import NATIVE_LAYER_MANIFEST


def test_latency_layer_check_uses_build_tree_layer_path(monkeypatch, tmp_path) -> None:
    build_dir = tmp_path / "native/latency_layer/build"
    build_dir.mkdir(parents=True)
    (build_dir / NATIVE_LAYER_MANIFEST).write_text("{}", encoding="utf-8")
    (build_dir / NATIVE_LAYER_LIBRARY).write_bytes(b"")
    seen = {}

    def fake_run(*args, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="VK_LAYER_PENGUINBURNER_latency\n",
            stderr="",
        )

    monkeypatch.setattr(layer_check, "_BUILD_LAYER_DIR", build_dir)

    result = layer_check.check_latency_layer(
        env={},
        run=fake_run,
        which=lambda name: f"/usr/bin/{name}",
    )

    assert result["ok"] is True
    assert result["launch_options"] == "PENGUIN_BURNER %command%"
    assert seen["env"]["PENGUIN_BURNER_LATENCY_LAYER"] == "1"
    assert seen["env"]["VK_ADD_IMPLICIT_LAYER_PATH"] == str(build_dir)
    assert (
        seen["env"]["VK_LOADER_LAYERS_ENABLE"]
        == "VK_LAYER_PENGUINBURNER_latency"
    )


def test_latency_layer_check_reports_wrapper_launch_options_without_vulkaninfo() -> None:
    result = layer_check.check_latency_layer(
        env={},
        which=lambda _name: None,
    )

    assert result["ok"] is False
    assert result["launch_options"] == "PENGUIN_BURNER %command%"
