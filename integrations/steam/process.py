"""Steam client process control for the fallback write path and init flow.

Reaching the host from inside a Flatpak is the same problem for every
launcher, and is solved once in integrations/launchers/host_process.py.
"""

from __future__ import annotations

import json
import os
import re
import sys

from integrations.launchers.host_process import (
    HOST_PGREP,
    host_command_path,
    host_pgrep,
    run_on_host,
    running_in_flatpak,
    start_on_host,
)

_STEAM_LAUNCH_APPID_RE = re.compile(r"SteamLaunch AppId=(\d+)")


def _steam_command(*args: str) -> list[str] | None:
    """The Steam client invocation, or None when Steam is not installed."""
    executable = host_command_path("steam")
    return None if executable is None else [executable, *args]


def steam_running() -> bool:
    # ~/.steam/steam.pid goes stale after exit; a live process check is the
    # only reliable signal.
    result = run_on_host([HOST_PGREP, "-x", "steam"])
    return result is not None and result.returncode == 0


def running_steam_game_ids() -> frozenset[str] | None:
    """App ids of every Steam game session alive right now, in ONE subprocess.

    A single ``pgrep -af`` returns all reaper command lines; parsing them out
    means a poller checks once per tick regardless of how many games it is
    tracking, instead of one subprocess per game. The ``[S]`` class keeps the
    query's own command line (which contains the pattern) from matching.

    Returns a (possibly empty) set of app ids on success, or ``None`` when the
    check itself failed (timeout / no host bridge) so a caller can distinguish
    "no games running" from "couldn't tell" instead of misreading a stalled
    probe as every game having exited.
    """
    matches = host_pgrep(r"[S]teamLaunch AppId=")
    if matches is None:
        return None
    ids: set[str] = set()
    for _pid, line in matches:
        match = _STEAM_LAUNCH_APPID_RE.search(line)
        if match:
            ids.add(match.group(1))
    return frozenset(ids)


def running_steam_processes() -> dict[str, tuple[int, ...]] | None:
    """Recovery discovery only; kernel process events own the lifetime."""
    matches = host_pgrep(r"[S]teamLaunch AppId=")
    if matches is None:
        return None
    games: dict[str, tuple[int, ...]] = {}
    for pid, line in matches:
        match = _STEAM_LAUNCH_APPID_RE.search(line)
        if match:
            app_id = match.group(1)
            games[app_id] = (*games.get(app_id, ()), pid)
    return games


def steam_available() -> bool:
    return host_command_path("steam") is not None


# Open identity-bound handles BEFORE requesting shutdown. An operation timeout
# bounds the settings workflow; it is never evidence that a game failed/exited.
_STEAM_SHUTDOWN = r'''
import os, selectors, subprocess, sys, json, time
from pathlib import Path
handles = []
try:
    with selectors.DefaultSelector() as selector:
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fd = os.pidfd_open(int(entry.name))
            except ProcessLookupError:
                continue
            try:
                if entry.stat().st_uid == os.getuid() and (entry / 'comm').read_text().strip() == 'steam':
                    selector.register(fd, selectors.EVENT_READ)
                    handles.append(fd)
                else:
                    os.close(fd)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                os.close(fd)
        if selector.get_map():
            subprocess.run(json.loads(sys.argv[1]), timeout=10, check=True)
        deadline = time.monotonic() + float(sys.argv[2])
        while selector.get_map():
            events = selector.select(max(0, deadline - time.monotonic()))
            if not events:
                raise RuntimeError('Steam shutdown was not confirmed')
            for key, _ in events:
                selector.unregister(key.fd)
finally:
    for fd in handles:
        os.close(fd)
'''


def shutdown_steam(*, timeout_s: float = 30.0) -> bool:
    """Ask Steam to exit and wait for kernel process-exit notifications."""
    if not steam_running():
        return True
    command = _steam_command("-shutdown")
    if command is None:
        return False
    python = (os.environ.get("PENGUIN_BURNER_HOST_PYTHON") or "/usr/bin/python3"
              if running_in_flatpak() else sys.executable)
    result = run_on_host(
        [python, "-c", _STEAM_SHUTDOWN, json.dumps(command), str(timeout_s)],
        timeout=timeout_s + 13.0,
    )
    return result is not None and result.returncode == 0 and not steam_running()


def launch_steam(*, silent: bool = True) -> bool:
    command = _steam_command(*(["-silent"] if silent else []))
    return command is not None and start_on_host(command)


def restart_steam(*, shutdown_timeout_s: float = 30.0, silent: bool = True) -> bool:
    if not shutdown_steam(timeout_s=shutdown_timeout_s):
        return False
    return launch_steam(silent=silent)


def launch_steam_game(app_id: str) -> bool:
    """Ask the running Steam client to launch a game (detached)."""
    if not str(app_id).isdigit():
        return False
    command = _steam_command("-applaunch", str(app_id))
    return command is not None and start_on_host(command)
