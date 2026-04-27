from __future__ import annotations

from auto_uv.events import emit_event


def test_emit_event_drops_none_values_from_payload() -> None:
    received = []

    emit_event(
        lambda event, payload: received.append((event, payload)),
        "load_telemetry",
        target_duration_s=None,
        elapsed_s=1.25,
        nested={"known": 1, "unknown": None},
        values=[1, None, {"kept": True, "dropped": None}],
    )

    assert received == [
        (
            "load_telemetry",
            {
                "elapsed_s": 1.25,
                "nested": {"known": 1},
                "values": [1, {"kept": True}],
            },
        )
    ]
