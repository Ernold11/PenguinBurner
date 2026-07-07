"""Coverage for common/atomic_write.py: the shared atomic tmp->replace writer.

Ownership/fsync are monkeypatched recorders; atomicity is asserted by failing a
write and checking the destination is untouched.
"""

from __future__ import annotations

import json
import os

import pytest

import common.atomic_write as atomic_write
from common.atomic_write import atomic_write_json, atomic_write_text


def test_atomic_write_json_writes_exact_bytes_and_key_order(tmp_path) -> None:
    payload = {"zulu": 1, "alpha": {"nested": [1, 2]}, "mid": "x"}
    target = tmp_path / "payload.json"

    result = atomic_write_json(target, payload)

    assert result == target
    assert target.read_bytes() == (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    # Insertion order preserved (no sort_keys drift).
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert list(json.loads(target.read_text(encoding="utf-8"))) == ["zulu", "alpha", "mid"]
    assert not target.with_name(target.name + ".tmp").exists()


def test_atomic_write_text_creates_parent_dirs(tmp_path) -> None:
    target = tmp_path / "a" / "b" / "state.txt"

    atomic_write_text(target, "key=value\n")

    assert target.read_text(encoding="utf-8") == "key=value\n"


def test_failed_write_leaves_existing_file_untouched(tmp_path) -> None:
    target = tmp_path / "config.txt"
    target.write_text("old content\n", encoding="utf-8")

    with pytest.raises(TypeError):
        atomic_write_text(target, 12345)  # type: ignore[arg-type]

    assert target.read_text(encoding="utf-8") == "old content\n"


def test_failed_write_creates_no_partial_destination(tmp_path) -> None:
    target = tmp_path / "fresh.txt"

    with pytest.raises(TypeError):
        atomic_write_text(target, 12345)  # type: ignore[arg-type]

    assert not target.exists()


def test_claim_ownership_covers_parent_chain_then_file(tmp_path, monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        atomic_write,
        "claim_desktop_user_ownership",
        lambda path, **kwargs: calls.append((path, kwargs)),
    )
    target = tmp_path / "owned.json"

    atomic_write_json(target, {"a": 1})

    assert calls == [
        (target.parent, {"include_parents": True}),
        (target, {}),
    ]


def test_claim_ownership_false_skips_chown(tmp_path, monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        atomic_write,
        "claim_desktop_user_ownership",
        lambda path, **kwargs: calls.append((path, kwargs)),
    )

    atomic_write_json(tmp_path / "unowned.json", {"a": 1}, claim_ownership=False)

    assert calls == []


def test_durable_fsyncs_file_and_directory(tmp_path, monkeypatch) -> None:
    fsynced: list[int] = []
    monkeypatch.setattr(os, "fsync", fsynced.append)

    atomic_write_json(tmp_path / "durable.json", {"a": 1}, durable=True)

    assert len(fsynced) == 2  # tmp file handle + parent directory


def test_non_durable_skips_fsync(tmp_path, monkeypatch) -> None:
    fsynced: list[int] = []
    monkeypatch.setattr(os, "fsync", fsynced.append)

    atomic_write_json(tmp_path / "fast.json", {"a": 1})

    assert fsynced == []
