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
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue


def _start_overlay_window(env: dict[str, str]) -> None:
    if str(env.get("PENGUIN_BURNER_OVERLAY_WINDOW") or "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
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
        log_file = None
        try:
            log_path = Path(str(env[OVERLAY_TEXT_ENV])).expanduser().with_name(
                "overlay-display.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("ab")
        except OSError:
            pass
        try:
            subprocess.Popen(
                command,
                env=_display_process_env(env),
                stdin=subprocess.DEVNULL,
                stdout=log_file if log_file is not None else subprocess.DEVNULL,
                stderr=log_file if log_file is not None else subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            if log_file is not None:
                log_file.close()
    except Exception:
        return


def _display_process_env(env: dict[str, str]) -> dict[str, str]:
    display_env = dict(env)
    for key in (
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "ORIG_LD_LIBRARY_PATH",
        "SYSTEM_LD_LIBRARY_PATH",
        "WINE_LD_PRELOAD",
        "STEAM_RUNTIME",
        "STEAM_RUNTIME_LIBRARY_PATH",
    ):
        display_env.pop(key, None)
    for key in tuple(display_env):
        if key.startswith("PRESSURE_VESSEL_"):
            display_env.pop(key, None)
    if not display_env.get("QT_QPA_PLATFORM"):
        if display_env.get("DISPLAY"):
            display_env["QT_QPA_PLATFORM"] = "xcb"
        elif display_env.get("WAYLAND_DISPLAY"):
            display_env["QT_QPA_PLATFORM"] = "wayland"
    return display_env
