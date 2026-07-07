"""Atomic file writes shared by the persistence sites.

Writes land in a same-directory ``<name>.tmp`` file that is moved into place
with ``os.replace``, so readers never observe a partial file. Parent
directories are created, and files written by elevated runs are chowned back
to the desktop user (``claim_ownership``). ``durable`` additionally fsyncs the
file and its directory, for files that must survive a crash mid-scan.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from common.penguin_burner_paths import claim_desktop_user_ownership


def atomic_write_text(
    path: Path,
    text: str,
    *,
    durable: bool = False,
    claim_ownership: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if claim_ownership:
        claim_desktop_user_ownership(path.parent, include_parents=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        if durable:
            handle.flush()
            os.fsync(handle.fileno())
    temp_path.replace(path)
    if durable:
        _fsync_directory(path.parent)
    if claim_ownership:
        claim_desktop_user_ownership(path)
    return path


def atomic_write_json(
    path: Path,
    payload: dict,
    *,
    durable: bool = False,
    claim_ownership: bool = True,
) -> Path:
    return atomic_write_text(
        path,
        json.dumps(payload, indent=2) + "\n",
        durable=durable,
        claim_ownership=claim_ownership,
    )


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
