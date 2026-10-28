"""Parse a value into a positive int, returning None when it isn't one."""

from __future__ import annotations

from typing import Any, cast


def positive_int(value: object | None) -> int | None:
    try:
        parsed = int(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
