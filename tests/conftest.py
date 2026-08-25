from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def write_desktop_rtd3_tree() -> Callable[[Path], tuple[Path, Path]]:
    """Build a fake NVIDIA PCI tree that explicitly selects Desktop mode."""

    def _write(base: Path) -> tuple[Path, Path]:
        address = "0000:01:00.0"
        sys_root = base / "rtd3-sys"
        proc_root = base / "rtd3-proc"
        device = sys_root / address
        (device / "power").mkdir(parents=True)
        (device / "vendor").write_text("0x10de\n", encoding="utf-8")
        (device / "class").write_text("0x030000\n", encoding="utf-8")
        (device / "power" / "control").write_text("auto\n", encoding="utf-8")
        (device / "power" / "runtime_status").write_text(
            "active\n", encoding="utf-8"
        )
        proc_gpu = proc_root / address
        proc_gpu.mkdir(parents=True)
        (proc_gpu / "power").write_text(
            "Runtime D3 status:          Disabled by default\n",
            encoding="utf-8",
        )
        return sys_root, proc_root

    return _write


@pytest.fixture(autouse=True)
def _isolate_user_home(monkeypatch, tmp_path_factory):
    """Keep the suite out of the developer's own PenguinBurner state.

    Every user-visible path (runtime config, saved profiles, caches) hangs off
    _effective_home(), and PENGUIN_BURNER_HOME overrides it. Without this,
    anything that reaches a real persistence helper writes the machine it runs
    on: selecting a GPU in a widget, for instance, calls through to
    persist_runtime_gpu_index and rewrites [gpu] index in the developer's
    ~/.config/PenguinBurner/penguin_burner.toml, silently repointing the tool
    at a different card.

    A test that needs to control the home itself sets the variable after this
    fixture has run, which wins.

    The directory comes from the factory rather than the test's own tmp_path:
    several tests assert on the exact contents of tmp_path, and a home planted
    inside it would show up as a stray entry.
    """
    home = tmp_path_factory.mktemp("penguin-burner-home")
    monkeypatch.setenv("PENGUIN_BURNER_HOME", str(home))


@pytest.fixture(autouse=True)
def _no_detached_launch_processes(monkeypatch):
    """Launcher main()-flow tests must never spawn real detached processes
    (re-front watcher, per-game FIFO drainer): those outlive the test run and
    litter ~/.cache with FIFOs. Tests that assert on the spawns monkeypatch
    these attributes themselves, which overrides this default stub."""
    from overlay import launcher

    monkeypatch.setattr(launcher, "spawn_refront_watcher", lambda env: None)
    monkeypatch.setattr(
        launcher, "spawn_detached_drainer", lambda env, path, session_pid=None: None
    )
