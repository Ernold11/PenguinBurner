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
