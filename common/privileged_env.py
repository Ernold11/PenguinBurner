from __future__ import annotations

from pathlib import Path

_MULTICALL_ENV_DISPATCHERS = frozenset(
    {"busybox", "coreutils", "toybox", "uu-coreutils"}
)


def env_command_prefix(env_path: str | Path = "/usr/bin/env") -> list[str]:
    """Return an ``env`` invocation safe to execute through ``pkexec``.

    pkexec resolves symlinks before executing its target. On systems where
    ``env`` is a symlink to a multicall binary, that loses the ``env`` applet
    name; explicitly dispatch it instead.
    """
    path = Path(env_path)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return [str(path)]
    if resolved.name in _MULTICALL_ENV_DISPATCHERS:
        return [str(resolved), "env"]
    return [str(path)]
