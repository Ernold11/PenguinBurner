from __future__ import annotations

from pathlib import Path
import re

from penguin_burner_paths import default_user_config_dir


DEFAULT_VERIFY_DURATION_S = 600
MAX_VERIFY_DURATION_S = 3600
_ELAPSED_RE = re.compile(r"elapsed=([0-9]+(?:\.[0-9]+)?)s")


def stop_request_path() -> Path:
    return default_user_config_dir() / "profile-verify-stop-requested"


def elapsed_from_line(line: str) -> float | None:
    match = _ELAPSED_RE.search(str(line or ""))
    if match is None:
        return None
    # The pattern only ever captures a valid decimal, so float() cannot fail.
    return max(0.0, float(match.group(1)))


def progress_percent(elapsed_s, target_s) -> int:
    try:
        elapsed = max(0.0, float(elapsed_s))
        target = max(1.0, float(target_s))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, int(round((elapsed / target) * 100.0))))


def workload_label(*, q2rtx_enabled: bool = True, cuda_enabled: bool = True) -> str:
    if q2rtx_enabled and cuda_enabled:
        return "Q2RTX timedemo and CUDA compute test"
    if q2rtx_enabled:
        return "Q2RTX timedemo"
    if cuda_enabled:
        return "CUDA compute test"
    return "No workload"
