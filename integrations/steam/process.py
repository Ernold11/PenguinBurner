"""Steam client process control for the fallback write path and init flow.

Reaching the host from inside a Flatpak is the same problem for every
launcher, and is solved once in integrations/launchers/host_process.py.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from integrations.launchers.host_process import (
    HOST_PGREP,
    host_pgrep,
    run_on_host,
    start_on_host,
)

from .users import steam_installation

_STEAM_LAUNCH_APPID_RE = re.compile(r"SteamLaunch AppId=(\d+)")


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


# Read only launcher identity, never export the rest of a process environment.
_STEAM_INSTALLATIONS = """
import os
from pathlib import Path
kinds = set()
for proc in Path('/proc').iterdir():
    try:
        if not proc.name.isdigit() or proc.stat().st_uid != os.getuid():
            continue
        if (proc / 'comm').read_text().strip() != 'steam':
            continue
        env = (proc / 'environ').read_bytes().split(b'\\0')
        kinds.add('flatpak' if b'FLATPAK_ID=com.valvesoftware.Steam' in env else 'native')
    except FileNotFoundError:
        continue
    except (OSError, ValueError):
        kinds.add('unknown')
print(' '.join(sorted(kinds)))
"""


def steam_matches_installation(home: Path | None = None) -> bool:
    """Do not send global CDP/IPC commands to another Steam installation."""
    result = run_on_host(['/usr/bin/python3', '-c', _STEAM_INSTALLATIONS], capture=True)
    if result is None or result.returncode:
        return False
    expected = 'flatpak' if steam_installation(home).flatpak else 'native'
    return set((result.stdout or '').split()) <= {expected}


def steam_available(home: Path | None = None) -> bool:
    return steam_installation(home).command() is not None


def shutdown_steam(*, timeout_s: float = 30.0, home: Path | None = None) -> bool:
    """Ask Steam to exit cleanly and wait until the process is gone."""
    if not steam_running():
        return True
    if not steam_matches_installation(home):
        return False
    command = steam_installation(home).command("-shutdown")
    if command is None or run_on_host(command, timeout=10.0) is None:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not steam_running():
            return True
        time.sleep(0.5)
    return not steam_running()


def launch_steam(*, silent: bool = True, home: Path | None = None) -> bool:
    if not steam_matches_installation(home):
        return False
    command = steam_installation(home).command(*(["-silent"] if silent else []))
    return command is not None and start_on_host(command)


def restart_steam(*, shutdown_timeout_s: float = 30.0, silent: bool = True, home: Path | None = None) -> bool:
    if not shutdown_steam(timeout_s=shutdown_timeout_s, home=home):
        return False
    return launch_steam(silent=silent, home=home)


def launch_steam_game(app_id: str, *, home: Path | None = None) -> bool:
    """Ask the running Steam client to launch a game (detached)."""
    if not str(app_id).isdigit():
        return False
    if not steam_matches_installation(home):
        return False
    command = steam_installation(home).command("-applaunch", str(app_id))
    return command is not None and start_on_host(command)
