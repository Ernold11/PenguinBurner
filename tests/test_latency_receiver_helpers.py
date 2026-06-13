"""Coverage for the pure helpers in latency_telemetry/receiver.py.

Socket-path resolution is env-driven and timing-sample normalization is pure,
so both are tested without sockets or hardware.
"""

from __future__ import annotations

import os
from pathlib import Path

from latency_telemetry.receiver import (
    latency_socket_path,
    latency_socket_paths,
    normalize_timing_sample,
)


# --- socket path resolution ---------------------------------------------------


def test_socket_path_prefers_explicit_env() -> None:
    env = {"PENGUIN_BURNER_LATENCY_SOCKET": "~/custom/latency.sock"}
    assert latency_socket_path(env) == Path("~/custom/latency.sock").expanduser()


def test_socket_path_uses_xdg_runtime_dir() -> None:
    env = {"XDG_RUNTIME_DIR": "/run/user/1000"}
    assert latency_socket_path(env) == Path(
        "/run/user/1000/penguin-burner/latency.sock"
    )


def test_socket_path_falls_back_to_tmp() -> None:
    # No explicit socket and no XDG dir -> per-uid /tmp path (non-root run).
    assert latency_socket_path({}) == Path(
        f"/tmp/penguin-burner-latency-{os.getuid()}.sock"
    )


def test_socket_paths_explicit_is_single() -> None:
    env = {"PENGUIN_BURNER_LATENCY_SOCKET": "/x/y.sock"}
    assert latency_socket_paths(env) == [Path("/x/y.sock")]


def test_socket_paths_adds_home_cache_and_dedupes() -> None:
    env = {"XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/penguin"}
    paths = latency_socket_paths(env)
    assert Path("/run/user/1000/penguin-burner/latency.sock") in paths
    assert Path("/home/penguin/.cache/penguin-burner/latency.sock") in paths
    assert len(paths) == len(set(str(p) for p in paths))  # no duplicates


# --- timing-sample normalization ----------------------------------------------


def test_normalize_passes_through_non_timing() -> None:
    sample = {"type": "frame", "value": 1}
    assert normalize_timing_sample(sample) is sample


def test_normalize_derives_gpu_render_span() -> None:
    out = normalize_timing_sample(
        {"type": "timing", "gpu_render_start_us": 100, "gpu_render_end_us": 250}
    )
    assert out["gpu_render_us"] == 150


def test_normalize_derives_render_present_span() -> None:
    out = normalize_timing_sample(
        {"type": "timing", "present_start_us": 50, "gpu_render_end_us": 200}
    )
    assert out.get("render_present_us") and out["render_present_us"] > 0


def test_normalize_keeps_existing_positive_values() -> None:
    out = normalize_timing_sample(
        {
            "type": "timing",
            "gpu_render_us": 99,
            "gpu_render_start_us": 100,
            "gpu_render_end_us": 250,
        }
    )
    # An already-present positive span is not overwritten.
    assert out["gpu_render_us"] == 99
