"""Coverage for ui/features/curves/memory_offset_profiles.py's save wrapper.

Dependencies are monkeypatched so no live profile store is touched.
"""

from __future__ import annotations

from pathlib import Path

import ui.features.curves.memory_offset_profiles as mop


def test_save_edited_memory_offset_profile_reads_from_disk_and_archives(monkeypatch) -> None:
    monkeypatch.setattr(mop, "profile_payload_from_path", lambda profile: {"base": 1})
    monkeypatch.setattr(
        mop,
        "user_edited_memory_offset_profile_payload",
        lambda parent, new_memory_offset_mhz, **kw: {
            "edited": True,
            "parent": parent,
            "new_memory_offset_mhz": new_memory_offset_mhz,
            "kw": kw,
        },
    )
    archived = Path("/tmp/auto-uv-profile-mem-edit.json")
    monkeypatch.setattr(mop, "archive_auto_uv_profile", lambda payload: archived)

    path, payload = mop.save_edited_memory_offset_profile(
        {"path": "x"},
        400,
        original_memory_offset_mhz=200,
    )

    assert path == archived
    assert payload["edited"] is True
    # The parent payload came from the re-read (disk), not the trimmed summary dict.
    assert payload["parent"] == {"base": 1}
    assert payload["new_memory_offset_mhz"] == 400
    assert payload["kw"] == {"original_memory_offset_mhz": 200}


def test_save_edited_memory_offset_profile_falls_back_to_profile_dict(monkeypatch) -> None:
    monkeypatch.setattr(mop, "profile_payload_from_path", lambda profile: None)
    captured = {}

    def fake_builder(parent, new_memory_offset_mhz, **kw):
        captured["parent"] = parent
        return {"edited": True}

    monkeypatch.setattr(mop, "user_edited_memory_offset_profile_payload", fake_builder)
    monkeypatch.setattr(mop, "archive_auto_uv_profile", lambda payload: Path("/tmp/x.json"))

    mop.save_edited_memory_offset_profile({"path": "", "memory_offset_mhz": 100}, 250)

    assert captured["parent"] == {"path": "", "memory_offset_mhz": 100}
