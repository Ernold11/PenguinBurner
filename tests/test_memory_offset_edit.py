from __future__ import annotations

from profiles.uv.memory_offset_edit import (
    editable_memory_offset_from_profile,
    user_edited_memory_offset_profile_payload,
)


def test_editable_memory_offset_from_profile_valid() -> None:
    assert editable_memory_offset_from_profile({"memory_offset_mhz": 400}) == 400
    assert editable_memory_offset_from_profile({"memory_offset_mhz": "400"}) == 400


def test_editable_memory_offset_from_profile_invalid() -> None:
    assert editable_memory_offset_from_profile({}) is None
    assert editable_memory_offset_from_profile({"memory_offset_mhz": None}) is None
    assert editable_memory_offset_from_profile({"memory_offset_mhz": "not-a-number"}) is None


def test_user_edited_memory_offset_profile_payload_requires_verification() -> None:
    payload = user_edited_memory_offset_profile_payload(
        {
            "profile_id": "parent",
            "path": "/tmp/parent.json",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2550,
            "memory_offset_mhz": 200,
            "plan": [{"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}],
            "points": [{"voltage_mv": 900, "clock_mhz": 2500}],
            "avg_fps": 120.0,
            "final_verified": True,
            "verification_status": "verified",
        },
        400,
        original_memory_offset_mhz=200,
    )

    assert payload["profile_source"] == "user-edited"
    assert payload["memory_offset_mhz"] == 400
    assert payload["final_verified"] is False
    assert payload["verification_status"] == "unverified"
    assert payload["requires_verification"] is True
    # Unrelated V/F curve and identity fields must pass through untouched.
    assert payload["candidate_voltage_mv"] == 900
    assert payload["lock_clock_mhz"] == 2550
    assert payload["plan"] == [
        {"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}
    ]
    assert "avg_fps" not in payload
    assert "profile_id" not in payload
    assert payload["manual_edit"] == {
        "edit_kind": "memory-offset",
        "parent_profile_id": "parent",
        "parent_path": "/tmp/parent.json",
        "original_memory_offset_mhz": 200,
        "new_memory_offset_mhz": 400,
    }


def test_user_edited_memory_offset_profile_payload_without_original() -> None:
    payload = user_edited_memory_offset_profile_payload(
        {"profile_id": "parent", "path": "/tmp/parent.json"},
        150,
    )
    assert payload["manual_edit"]["original_memory_offset_mhz"] is None
    assert payload["manual_edit"]["new_memory_offset_mhz"] == 150
