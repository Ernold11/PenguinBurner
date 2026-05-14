"""Write the on-disk marker that lets Auto-UV detect an interrupted live probe.

Clean exits remove the marker; a later run can treat a stale marker as crash evidence after validation.
"""

from __future__ import annotations

from datetime import datetime
import os
import socket
from pathlib import Path

from .auto_uv_persisted_json_files import probe_in_progress_path, safe_json_write


def write_probe_in_progress_marker(
    *,
    phase: str,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    log_context: str | None = None,
    details: dict | None = None,
) -> Path:
    payload = {
        "format_version": 1,
        "state": "probing",
        "started_at": datetime.now().astimezone().isoformat(),
        "pid": int(os.getpid()),
        "host": socket.gethostname(),
        "phase": str(phase),
        "candidate_voltage_mv": int(candidate_voltage_mv),
        "lock_clock_mhz": int(lock_clock_mhz),
        "log_context": str(log_context).strip() if log_context is not None else None,
    }
    if details:
        payload["details"] = details
    return safe_json_write(probe_in_progress_path(), payload)


def clear_probe_in_progress_marker() -> None:
    try:
        probe_in_progress_path().unlink()
    except FileNotFoundError:
        return
