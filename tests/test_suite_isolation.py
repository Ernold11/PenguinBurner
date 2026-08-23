"""The suite must not read or write the developer's own PenguinBurner state.

Every user-visible path hangs off ``_effective_home()``. A test that reaches a
real persistence helper without noticing would otherwise rewrite the machine it
runs on, and the damage is silent: the run still passes.
"""

from __future__ import annotations

import os
from pathlib import Path

from common.penguin_burner_paths import (
    default_runtime_config_path,
    default_saved_uv_dir,
    default_user_config_dir,
)


def _real_home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def test_the_home_override_is_set_for_every_test() -> None:
    override = os.environ.get("PENGUIN_BURNER_HOME", "").strip()

    assert override, "the autouse isolation fixture in conftest.py is not running"
    assert Path(override).is_dir()


def test_user_paths_resolve_outside_the_developers_home() -> None:
    real = _real_home()

    for path in (
        default_user_config_dir(),
        default_runtime_config_path(),
        default_saved_uv_dir(),
    ):
        resolved = Path(path).expanduser().resolve()
        assert real not in resolved.parents, f"{resolved} is under the real home"


def test_persisting_a_gpu_index_cannot_reach_the_real_config() -> None:
    """The concrete leak this guard was added for.

    Selecting a GPU in the Profiles widget calls through to
    persist_runtime_gpu_index. Unmocked, that used to rewrite [gpu] index in
    ~/.config/PenguinBurner/penguin_burner.toml and silently repoint the tool
    at a different card.
    """
    from ui.features.tuning.gpu_selection import persist_runtime_gpu_index

    written = default_runtime_config_path().resolve()
    assert _real_home() not in written.parents

    persist_runtime_gpu_index(1)

    assert written.exists()
