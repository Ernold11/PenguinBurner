"""JSON event output for CLI subprocess integrations.

The UI reads these one-line JSON events from stdout while commands run as root.
"""

from __future__ import annotations

import json


def emit_cli_json_event(enabled: bool, event: str, **payload) -> None:
    if not enabled:
        return
    data = {"event": str(event)}
    data.update(json_without_none_values(payload))
    print(json.dumps(data, sort_keys=True), flush=True)


def json_without_none_values(value):
    if isinstance(value, dict):
        return {
            key: json_without_none_values(inner)
            for key, inner in value.items()
            if inner is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            json_without_none_values(inner)
            for inner in value
            if inner is not None
        ]
    return value

