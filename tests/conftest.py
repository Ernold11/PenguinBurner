from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


import pytest


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
