"""Coverage for environment-driven overlay latency socket paths."""

from __future__ import annotations

import os
from pathlib import Path

from overlay.telemetry.sockets import latency_socket_path
from overlay.telemetry.sockets import latency_socket_paths


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
