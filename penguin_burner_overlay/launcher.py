from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

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
    env.setdefault(OVERLAY_TEXT_ENV, str(_writable_overlay_text_path(env)))
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
        log_path = _overlay_display_log_path(env)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_file:
            subprocess.Popen(
                command,
                env=_display_process_env(env),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
    except Exception:
        return


def _writable_overlay_text_path(env: dict[str, str]) -> Path:
    path = overlay_text_path(env)
    if _path_parent_writable(path):
        return path
    return Path(tempfile.gettempdir()) / f"penguin-burner-overlay-text-{os.getuid()}.txt"


def _path_parent_writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path.parent, os.W_OK)


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


def _overlay_display_log_path(env: dict[str, str]) -> Path:
    text_path = Path(str(env.get(OVERLAY_TEXT_ENV) or "")).expanduser()
    if text_path and _path_parent_writable(text_path):
        return text_path.with_name("overlay-display.log")
    return Path(tempfile.gettempdir()) / f"penguin-burner-overlay-display-{os.getuid()}.log"
