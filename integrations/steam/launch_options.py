"""Token-splice injection of the PenguinBurner wrapper into launch options.

Steam substitutes the literal ``%command%`` with the full launch chain and
runs the result through ``/bin/sh -c``. Replacing the token (never
prepending) keeps every user prefix working — env assignments, gamemoderun,
mangohud, and gamescope's ``--`` all stay outside us, so our layer env
reaches only the game.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex

from overlay.telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER


COMMAND_TOKEN = "%command%"
# The overlay switch rides as a wrapper FLAG, not an env-assignment token:
# gamescope (and anything else that execs its child directly, without a
# shell) cannot start "PB_OVERLAY=1" as a program, so env tokens after
# "gamescope --" brick the launch. A flag is plain argv everywhere. Explicit
# =0 (not merely absent) makes the per-game toggle deterministic — it also
# decides the wrapper's MangoHud strip.
OVERLAY_FLAG = "--pb-overlay=1"
OVERLAY_OFF_FLAG = "--pb-overlay=0"

# Our tokens, standing alone between whitespace: the bare wrapper name, its
# --pb-* flags, and any legacy PB_*/PENGUIN_BURNER_* env assignment (still
# stripped so hand-added setups normalize). Consuming trailing whitespace
# keeps removal from leaving double spaces behind.
_PB_TOKEN_RE = re.compile(
    r"(?:(?<=\s)|^)"
    r"(?:--pb-[a-z0-9-]+=\S*"
    r"|PB_[A-Za-z0-9_]+=\S*"
    rf"|{PENGUIN_BURNER_WRAPPER}(?:_[A-Za-z0-9_]+)?=\S*"
    rf"|{PENGUIN_BURNER_WRAPPER})"
    r"(?:\s+|$)"
)
_WRAPPER_PRESENT_RE = re.compile(rf"(?:^|\s){PENGUIN_BURNER_WRAPPER}(?:\s|$)")
_OVERLAY_PRESENT_RE = re.compile(
    rf"(?:^|\s)(?:{re.escape(OVERLAY_FLAG)}|PB_OVERLAY=1)(?:\s|$)"
)


@dataclass(frozen=True)
class InjectionState:
    wrapped: bool
    overlay: bool


def injection_state(launch_options: str | None) -> InjectionState:
    value = launch_options or ""
    return InjectionState(
        wrapped=bool(_WRAPPER_PRESENT_RE.search(value)),
        overlay=bool(_OVERLAY_PRESENT_RE.search(value)),
    )


def strip_penguin_burner_tokens(launch_options: str) -> str:
    return _PB_TOKEN_RE.sub("", launch_options).strip()


def inject_launch_options(launch_options: str | None, *, overlay: bool = False) -> str:
    """Splice the wrapper innermost; idempotent and normalizes legacy placement.

    Existing PB tokens are stripped first, then the first ``%command%`` is
    replaced with ``<tokens> %command%``. A token-less string was game
    arguments, so it is preserved verbatim after the token.
    """
    base = strip_penguin_burner_tokens(launch_options or "")
    overlay_flag = OVERLAY_FLAG if overlay else OVERLAY_OFF_FLAG
    prefix = f"{PENGUIN_BURNER_WRAPPER} {overlay_flag}"
    if COMMAND_TOKEN in base:
        return base.replace(COMMAND_TOKEN, f"{prefix} {COMMAND_TOKEN}", 1)
    injected = f"{prefix} {COMMAND_TOKEN}"
    return f"{injected} {base}" if base else injected


def remove_injection(
    launch_options: str | None,
    *,
    stored_original: str | None = None,
    stored_injected: str | None = None,
) -> str:
    """Undo an injection, restoring the stored original when it still matches.

    Otherwise conservatively strip only our tokens; a result of exactly
    ``%command%`` means the field was ours alone, so it collapses to empty.
    """
    value = launch_options or ""
    if stored_injected is not None and value == stored_injected:
        return stored_original or ""
    stripped = strip_penguin_burner_tokens(value)
    if stripped == COMMAND_TOKEN:
        return ""
    return stripped


def launch_options_problems(launch_options: str) -> tuple[str, ...]:
    """Validation errors that would break the sh -c launch line."""
    problems: list[str] = []
    if launch_options.count(COMMAND_TOKEN) > 1:
        problems.append("more than one %command% token")
    try:
        shlex.split(launch_options, posix=True)
    except ValueError:
        problems.append("unbalanced quotes")
    return tuple(problems)
