"""Emit Auto-UV3 JSON events to the UI callback.

The writer strips None values so every UI payload stays compact and predictable.
"""

from __future__ import annotations

from collections.abc import Callable


AutoUvEventCallback = Callable[[str, dict], None]


def emit_ui_json_event(
    callback: AutoUvEventCallback | None,
    event: str,
    **payload,
) -> None:
    if callback is None:
        return
    callback(str(event), _without_none_values(payload))


def _without_none_values(value):
    if isinstance(value, dict):
        return {
            key: _without_none_values(inner)
            for key, inner in value.items()
            if inner is not None
        }
    if isinstance(value, (list, tuple)):
        return [_without_none_values(inner) for inner in value if inner is not None]
    return value
