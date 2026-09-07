"""Putting the PenguinBurner wrapper into a launch command, and taking it out.

Lutris prepends to ``prefix_command`` and Heroic adds a wrapper entry, but both
end up with the same shape: our tokens run first and whatever the user already
had there stays between us and the game. Only Steam differs, because it splices
into a ``%command%`` placeholder instead of prepending, and keeps its own
module for it.
"""

from __future__ import annotations

import re

from overlay.wrapper_tokens import (
    game_key_from_flag,
    strip_penguin_burner_tokens,
    wrapper_present,
    wrapper_tokens,
)

from .host_process import host_pgrep


def game_key(launcher_id: str, game_id: str) -> str:
    """The one identity a game carries from the tab to the wrapper to the daemon.

    Namespaced by launcher because ids collide across them: a Lutris game 27
    and a Steam app 27 are not the same game, and the daemon keys the running
    -game registry by one opaque string.

    Both halves are required. A key missing one of them -- ``:27``, ``lutris:``
    -- namespaces nothing while still looking like an identity, so it would be
    written into a launch command and read back as a game that cannot be
    resolved. Empty says plainly that this game has no key, which every caller
    already handles by leaving the flag out.
    """
    launcher = str(launcher_id or "").strip()
    value = str(game_id or "").strip()
    return f"{launcher}:{value}" if launcher and value else ""


def inject_wrapper(
    command: str | None,
    *,
    overlay: bool,
    launcher_id: str,
    game_id: str,
    ingame_latency: bool = False,
) -> str:
    """Put our tokens in front of whatever the user already had there."""
    base = strip_penguin_burner_tokens(command or "")
    tokens = wrapper_tokens(
        overlay=overlay,
        game_key=game_key(launcher_id, game_id),
        # With the overlay on the launcher turns the markers on by itself, so
        # writing the opt-in as well would only be noise in the command.
        ingame_latency=ingame_latency and not overlay,
    )
    return f"{tokens} {base}".strip() if base else tokens


def remove_wrapper(
    command: str | None,
    *,
    stored_original: str | None = None,
    stored_injected: str | None = None,
) -> str:
    """Undo an injection, restoring the stored original when it still matches."""
    value = command or ""
    if stored_injected is not None and value == stored_injected:
        return stored_original or ""
    return strip_penguin_burner_tokens(value)


def command_wrapped(command: str | None) -> bool:
    return wrapper_present(command)


def running_wrapped_games(launcher_id: str) -> dict[str, tuple[int, ...]] | None:
    """This launcher's running wrapped games, mapped to their wrapper pids.

    The identity flag we wrote is on the command line of the process we
    started, so one ``pgrep`` names the sessions exactly -- no title matching,
    no guessing which of two games with one name is running.

    It sees only games PenguinBurner wraps, which is the set the tab can act
    on: a game launched untouched has nothing of ours in its command line.

    ``None`` means the check itself failed -- not that nothing is running -- so
    a caller can hold what it knows instead of reading a stalled probe as every
    game having exited.
    """
    prefix = f"{str(launcher_id).strip()}:"
    # The [-] class keeps the query's own command line from matching itself.
    matches = host_pgrep(f"[-]-pb-game-id={re.escape(prefix)}")
    if matches is None:
        return None
    running: dict[str, tuple[int, ...]] = {}
    for pid, line in matches:
        for word in line.split():
            key = game_key_from_flag(word)
            if key.startswith(prefix):
                game_id = key[len(prefix) :]
                running[game_id] = (*running.get(game_id, ()), pid)
    return running
