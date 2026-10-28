from __future__ import annotations

from pathlib import Path

from common.privileged_env import env_command_prefix


def test_env_command_prefix_keeps_a_regular_env_executable(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.touch()

    assert env_command_prefix(env) == [str(env)]


def test_env_command_prefix_dispatches_uutils_multicall_binary(tmp_path: Path) -> None:
    multicall = tmp_path / "uu-coreutils"
    multicall.touch()
    env = tmp_path / "env"
    env.symlink_to(multicall)

    assert env_command_prefix(env) == [str(multicall), "env"]
