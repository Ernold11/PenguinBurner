"""Faugus process control: start a game, see what runs, stop it.

``faugus-launcher --game <gameid>`` starts a game from its own entry, so the
wrapper PenguinBurner wrote into that game's ``launch_arguments`` is already
in the command Faugus builds. Nothing here re-implements a launch.

Faugus keeps a running-games file of its own, but only its window writes it,
so a game we started would be missing from it. The environment is what holds
regardless of who did the launching: our wrapper carries its session identity,
and Faugus stamps every game it starts with FAUGUSID -- the same marker its
own "kill this game" walks /proc for.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.host_process import (
    host_terminate,
    start_on_host,
)
from integrations.launchers.wrapped_sessions import (
    LauncherSessions,
    running_wrapped_sessions,
)

from .paths import faugus_installation

LAUNCHER_ID = "faugus"
#: What Faugus puts in front of every game command, holding that game's id.
GAME_ID_ENV = "FAUGUSID"


def faugus_available(home: Path | None = None) -> bool:
    """Whether Faugus can be asked to start a game on this machine.

    Distinct from having a Faugus library: a machine can carry games.json for
    a Faugus that is no longer installed, and those games are still worth
    listing and configuring -- just not startable.
    """
    return faugus_installation(home).command() is not None


def launch_command(game_id: str, *, home: Path | None = None) -> list[str] | None:
    """The command that asks Faugus to start one game, or None if unusable."""
    game_id = str(game_id or "").strip()
    # The id is spliced into a command line and Faugus resolves it against its
    # own files; a separator in it is refused rather than passed on.
    if not game_id or "/" in game_id or game_id.startswith("-"):
        return None
    return faugus_installation(home).command("--game", game_id)


def launch_faugus_game(game_id: str, *, home: Path | None = None) -> bool:
    """Ask Faugus to start a game (detached)."""
    command = launch_command(game_id, home=home)
    return bool(command) and start_on_host(command)


def probe_faugus_sessions(
    *, known_pids: tuple[int, ...] = (),
) -> LauncherSessions | None:
    """Wrapped sessions and observed Faugus launches; None if unreadable."""
    return running_wrapped_sessions(
        LAUNCHER_ID, external_env=GAME_ID_ENV, known_pids=known_pids
    )


def stop_faugus_game(pid: int) -> bool:
    """One SIGTERM to the wrapper, which is the game session after its exec."""
    return host_terminate(pid)
