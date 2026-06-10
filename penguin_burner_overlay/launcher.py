from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from .state import (
    OVERLAY_ENABLE_ENV,
    OVERLAY_STATE_ENV,
    OVERLAY_TEXT_ENV,
    overlay_state_path,
    overlay_text_path,
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "Usage: PB_OVERLAY %command%\n"
            "Steam launch option example: PB_OVERLAY %command%",
            file=sys.stderr,
        )
        return 2

    env = dict(os.environ)
    env.setdefault("PENGUIN_BURNER_LATENCY_LAYER", "1")
    env.setdefault(OVERLAY_ENABLE_ENV, "1")
    env.setdefault("DXVK_NVAPI_VKREFLEX", "1")
    env.setdefault("PROTON_ENABLE_NVAPI", "1")
    env.setdefault(OVERLAY_STATE_ENV, str(overlay_state_path(env)))
    env.setdefault(OVERLAY_TEXT_ENV, str(overlay_text_path(env)))
    _prepare_overlay_paths(env)
    _start_overlay_window(env)
    os.execvpe(args[0], args, env)
    return 127


def _prepare_overlay_paths(env: dict[str, str]) -> None:
    for key in (OVERLAY_STATE_ENV, OVERLAY_TEXT_ENV):
        path = Path(str(env.get(key) or "")).expanduser()
        if not str(path):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)


def _start_overlay_window(env: dict[str, str]) -> None:
    if str(env.get("PENGUIN_BURNER_OVERLAY_WINDOW") or "").lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    command = [
        sys.executable,
        "-m",
        "penguin_burner_overlay.display",
        "--text-file",
        str(env[OVERLAY_TEXT_ENV]),
        "--parent-pid",
        str(os.getpid()),
    ]
    try:
        subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return
