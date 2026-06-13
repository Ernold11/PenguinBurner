"""Parse a value into a positive int, returning None when it isn't one."""

from __future__ import annotations


def positive_int(value: object | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
