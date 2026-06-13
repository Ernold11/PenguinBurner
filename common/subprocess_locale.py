from __future__ import annotations

import os


def stable_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update(extra)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env
