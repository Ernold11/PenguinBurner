"""Steam client process control for the fallback write path and init flow."""

from __future__ import annotations

import shutil
import subprocess
import time


def steam_running() -> bool:
    # ~/.steam/steam.pid goes stale after exit; a live process check is the
    # only reliable signal.
    return (
        subprocess.run(
            ["pgrep", "-x", "steam"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def steam_available() -> bool:
    return shutil.which("steam") is not None


def shutdown_steam(*, timeout_s: float = 30.0) -> bool:
    """Ask Steam to exit cleanly and wait until the process is gone."""
    if not steam_running():
        return True
    subprocess.run(
        ["steam", "-shutdown"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not steam_running():
            return True
        time.sleep(0.5)
    return not steam_running()


def launch_steam(*, silent: bool = True) -> bool:
    if not steam_available():
        return False
    command = ["steam", "-silent"] if silent else ["steam"]
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def restart_steam(*, shutdown_timeout_s: float = 30.0, silent: bool = True) -> bool:
    if not shutdown_steam(timeout_s=shutdown_timeout_s):
        return False
    return launch_steam(silent=silent)


def launch_steam_game(app_id: str) -> bool:
    """Ask the running Steam client to launch a game (detached)."""
    if not steam_available() or not str(app_id).isdigit():
        return False
    try:
        subprocess.Popen(
            ["steam", "-applaunch", str(app_id)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True
