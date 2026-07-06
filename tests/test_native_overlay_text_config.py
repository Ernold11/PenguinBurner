from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_native_overlay_env_true_overrides_disabled_config(tmp_path: Path) -> None:
    binary = _build_native_overlay_probe(tmp_path)
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        "\n".join(
            [
                "version = 1",
                "enabled = false",
                (
                    'items = ["base_fps", "latency_ms", "clock_mhz", '
                    '"voltage_mv", "power_w", "profile"]'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(binary)],
        check=True,
        text=True,
        capture_output=True,
        env=_probe_env(config_path, pb_overlay="1"),
    )

    assert result.stdout.strip() == "19 FPS LAT 73 ms 1777 MHz 885 mV 54 W PERF"


def test_native_overlay_env_false_still_disables_enabled_config(tmp_path: Path) -> None:
    binary = _build_native_overlay_probe(tmp_path)
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        "\n".join(
            [
                "version = 1",
                "enabled = true",
                'items = ["base_fps", "clock_mhz"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(binary)],
        check=True,
        text=True,
        capture_output=True,
        env=_probe_env(config_path, pb_overlay="0"),
    )

    assert result.stdout.strip() == ""


def _build_native_overlay_probe(tmp_path: Path) -> Path:
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    repo_root = Path(__file__).resolve().parents[1]
    source = tmp_path / "native_overlay_probe.cpp"
    source.write_text(
        r'''
#include <iostream>
#include "latency_layer_internal.h"

namespace pblayer {
std::string build_overlay_text(
    uint64_t fps, const OverlayGpuState& state, uint64_t now_us);
}

int main() {
    pblayer::OverlayGpuState state{};
    state.clock_mhz = "1777";
    state.voltage_mv = "885";
    state.power_w = "54";
    state.latency_ms = "73";
    state.profile_tier = "Performance";
    std::cout << pblayer::build_overlay_text(19, state, 1000000) << "\n";
    return 0;
}
''',
        encoding="utf-8",
    )
    output = tmp_path / "native_overlay_probe"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-I",
            str(repo_root / "overlay/native/latency_layer/src"),
            str(source),
            str(repo_root / "overlay/native/latency_layer/src/overlay_text.cpp"),
            str(repo_root / "overlay/native/latency_layer/src/latency_state.cpp"),
            "-o",
            str(output),
        ],
        check=True,
        cwd=repo_root,
    )
    return output


def _probe_env(config_path: Path, *, pb_overlay: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
        "PB_OVERLAY": pb_overlay,
    }
