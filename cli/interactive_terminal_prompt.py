"""Interactive terminal prompts used before starting risky runtime actions.

The helpers stay tiny so command flow can ask for confirmation without owning input parsing.
"""

from __future__ import annotations

from typing import Callable


def prompt_yes_no(
    prompt: str,
    *,
    default: bool,
    debug_log: Callable[[str], None] | None = None,
) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        entered = input(f"{prompt} {suffix}: ").strip().lower()
        if debug_log is not None:
            debug_log(f"prompt={prompt} answer={entered or '<enter>'}")
        if not entered:
            return bool(default)
        if entered in ("y", "yes"):
            return True
        if entered in ("n", "no"):
            return False
        print("Please answer y or n.", flush=True)

