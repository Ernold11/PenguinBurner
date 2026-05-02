"""Turn a stop-request file into a stability abort callback.

The UI can create this file to stop long profile verification without killing the process.
"""

from __future__ import annotations

from pathlib import Path


def stability_stop_request_path(args) -> Path | None:
    text = str(getattr(args, "stability_stop_request_file", "") or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


def stability_stop_request_abort_callback(
    stop_request_path: Path,
    *,
    previous_callback=None,
):
    def abort_callback(state: dict) -> str | None:
        if previous_callback is not None:
            reason = previous_callback(state)
            if reason:
                return str(reason)
        try:
            if Path(stop_request_path).exists():
                return "user-stop-requested"
        except OSError:
            return None
        return None

    return abort_callback
