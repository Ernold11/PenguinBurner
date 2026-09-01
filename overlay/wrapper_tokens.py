"""The tokens that put the PenguinBurner wrapper in front of a game.

Both launchers splice the same argv fragment — the wrapper name plus its
``--pb-*`` flags — into a string the launcher later runs. Only the surrounding
field differs: Steam replaces ``%command%`` inside its launch options, Lutris
prepends to ``prefix_command``. The vocabulary itself, and stripping it back
out, is the wrapper's own business, so it lives beside the wrapper rather than
inside either integration.
"""

from __future__ import annotations

import re

from overlay.telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER

# The overlay switch rides as a wrapper FLAG, not an env-assignment token:
# gamescope (and anything else that execs its child directly, without a shell)
# cannot start "PB_OVERLAY=1" as a program, so env tokens after "gamescope --"
# brick the launch. A flag is plain argv everywhere. Explicit =0 (not merely
# absent) makes the per-game toggle deterministic -- it also decides the
# wrapper's MangoHud strip.
OVERLAY_FLAG = "--pb-overlay=1"
OVERLAY_OFF_FLAG = "--pb-overlay=0"

# Lutris has no stable per-launch app id of its own (LUTRIS_GAME_UUID is
# regenerated every run), so the game identity is injected by us and read back
# off argv. Steam does not need this: it publishes SteamAppId in the
# environment.
# Latency markers ride an env assignment rather than a --pb-* flag, because the
# launcher reads them before it parses anything: the opt-in has to be in the
# environment the wrapper starts with. It is introduced by `env` so the pair
# survives an argv exec -- Lutris spawns prefix_command as a command list with
# no shell, where a bare `VAR=1` first token would be taken as the program name.
INGAME_LATENCY_ASSIGNMENT = "PB_INGAME_LATENCY=1"
INGAME_LATENCY_TOKENS = f"env {INGAME_LATENCY_ASSIGNMENT}"
# Steam takes the same opt-in as a wrapper FLAG, for the reason the overlay
# switch is one: Steam's tokens land where %command% was, and an assignment
# there is a program name to anything that execs its child directly -- which
# is exactly what `gamescope -- %command%` does. Lutris cannot use a flag for
# it (the wrapper is not running yet when prefix_command's env is built), so
# the two launchers write the same meaning in the two shapes each can run.
INGAME_LATENCY_FLAG = "--pb-ingame-latency=1"

LUTRIS_ID_FLAG_PREFIX = "--pb-lutris-id="
# Where the wrapper parks the id it read off that flag, so the Lutris runtime
# hook can find it the same way the Steam one finds SteamAppId.
LUTRIS_GAME_ID_ENV = "PENGUIN_BURNER_LUTRIS_GAME_ID"

# Our tokens, standing alone between whitespace: the bare wrapper name, its
# --pb-* flags, and any legacy PB_*/PENGUIN_BURNER_* env assignment (still
# stripped so hand-added setups normalize). Consuming trailing whitespace keeps
# removal from leaving double spaces behind.
_PB_TOKEN_RE = re.compile(
    r"(?:(?<=\s)|^)"
    # The `env` that introduces our assignment goes with it; a bare `env` the
    # user put there for their own reasons is left alone.
    r"(?:env(?=\s+PB_INGAME_LATENCY=)"
    r"|--pb-[a-z0-9-]+=\S*"
    r"|PB_[A-Za-z0-9_]+=\S*"
    rf"|{PENGUIN_BURNER_WRAPPER}(?:_[A-Za-z0-9_]+)?=\S*"
    rf"|{PENGUIN_BURNER_WRAPPER})"
    r"(?:\s+|$)"
)
_WRAPPER_PRESENT_RE = re.compile(rf"(?:^|\s){PENGUIN_BURNER_WRAPPER}(?:\s|$)")
_OVERLAY_PRESENT_RE = re.compile(
    rf"(?:^|\s)(?:{re.escape(OVERLAY_FLAG)}|PB_OVERLAY=1)(?:\s|$)"
)


def strip_penguin_burner_tokens(value: str) -> str:
    return _PB_TOKEN_RE.sub("", value or "").strip()


def wrapper_present(value: str | None) -> bool:
    return bool(_WRAPPER_PRESENT_RE.search(value or ""))


def overlay_present(value: str | None) -> bool:
    return bool(_OVERLAY_PRESENT_RE.search(value or ""))


_INGAME_LATENCY_PRESENT_RE = re.compile(
    rf"(?:^|\s)(?:{re.escape(INGAME_LATENCY_ASSIGNMENT)}"
    rf"|{re.escape(INGAME_LATENCY_FLAG)})(?:\s|$)"
)


def ingame_latency_present(value: str | None) -> bool:
    """Either shape of the opt-in, so state reads back off any launch string."""
    return bool(_INGAME_LATENCY_PRESENT_RE.search(value or ""))


def overlay_flag(overlay: bool) -> str:
    return OVERLAY_FLAG if overlay else OVERLAY_OFF_FLAG


def lutris_id_flag(game_id: str) -> str:
    return f"{LUTRIS_ID_FLAG_PREFIX}{str(game_id).strip()}"


def wrapper_tokens(
    *,
    overlay: bool,
    lutris_game_id: str = "",
    ingame_latency: bool = False,
    latency_as_flag: bool = False,
) -> str:
    """The wrapper plus its flags, in the order the launcher will run them.

    As an assignment the latency opt-in comes first -- it is environment for
    the wrapper, so it has to be set before the wrapper is the thing running.
    As a flag it comes after, because then it is an argument to the wrapper.
    """
    parts = [] if (latency_as_flag or not ingame_latency) else [INGAME_LATENCY_TOKENS]
    parts += [PENGUIN_BURNER_WRAPPER, overlay_flag(overlay)]
    if ingame_latency and latency_as_flag:
        parts.append(INGAME_LATENCY_FLAG)
    game_id = str(lutris_game_id or "").strip()
    if game_id:
        parts.append(lutris_id_flag(game_id))
    return " ".join(parts)
