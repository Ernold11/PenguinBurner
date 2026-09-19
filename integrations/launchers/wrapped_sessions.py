"""Which of a launcher's games are running through our wrapper.

A launcher that starts a game through a wrapper command cannot be asked what
it started: it hands the session over and forgets it. The session identifies
itself in its own environment instead, which survives the exec that replaces
the wrapper with the game -- unlike the wrapper's argv, which does not.

Only wrapper sessions are found here, because only they can be stopped. A
launcher that also wants to observe its own unwrapped launches knows how to
recognise those itself, and keeps that probe in its own package.
"""

from __future__ import annotations

import json
import os
import sys

from overlay.wrapper_tokens import GAME_KEY_ENV, split_game_key
from runtime.daemon_client import daemon_status

from .host_process import run_on_host, running_in_flatpak

# A stdlib-only host probe: the PenguinBurner package need not be installed
# outside our Flatpak. Only the session leader qualifies; helpers and game
# children inherit its identity but must never become targets for Stop.
_SESSION_PROBE = """
import json, os, sys
from pathlib import Path

sessions = []
unreadable = []
for proc in Path(sys.argv[1]).iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        fields = (proc / 'environ').read_bytes().split(b'\\0')
    except PermissionError:
        unreadable.append(int(proc.name))
        continue
    except (FileNotFoundError, ProcessLookupError):
        continue
    env = dict(field.split(b'=', 1) for field in fields if b'=' in field)
    if env.get(b'PENGUIN_BURNER_TELEMETRY_SESSION') == proc.name.encode():
        key = env.get(sys.argv[2].encode(), b'').decode('utf-8', errors='replace')
        if key:
            sessions.append([int(proc.name), key])
print(json.dumps({'sessions': sessions, 'unreadable': unreadable}))
"""


def host_python() -> str:
    """The interpreter that can read the host's /proc, sandbox or not."""
    return (
        os.environ.get("PENGUIN_BURNER_HOST_PYTHON") or "/usr/bin/python3"
        if running_in_flatpak() else sys.executable
    )


def running_wrapped_sessions(
    launcher_id: str,
    *,
    known_pids: tuple[int, ...] = (),
) -> dict[str, tuple[int, ...]] | None:
    """Game id -> session pids, or None when the answer is unknowable.

    None is not "nothing is running": a caller told None must hold what it
    already knows rather than read a failed probe as every game having exited.
    """
    result = run_on_host(
        [host_python(), "-c", _SESSION_PROBE, "/proc", GAME_KEY_ENV], capture=True
    )
    if result is None or result.returncode != 0:
        return None
    running: dict[str, tuple[int, ...]] = {}
    try:
        payload = json.loads(result.stdout)
        sessions = payload["sessions"]
        unreadable = set(payload["unreadable"])
        if unreadable:
            # Daemon watches keep their identity independently of /proc access.
            # Only use them for inaccessible PIDs: an exited watch may remain
            # in the daemon's restore grace period after its process is gone.
            try:
                status = daemon_status(timeout_s=1.0)
            except (RuntimeError, OSError, ValueError):
                status = {}
            for watch in (status.get("game_runtime") or {}).get("watched", []):
                pid = watch["pid"]
                if pid in unreadable:
                    sessions.append((pid, watch["app_id"]))
                    unreadable.remove(pid)
            if unreadable.intersection(known_pids):
                return None
        for pid, key in sessions:
            launcher, game_id = split_game_key(key)
            if launcher == launcher_id:
                running[game_id] = (*running.get(game_id, ()), int(pid))
    except (KeyError, ValueError, TypeError):
        return None
    return running
