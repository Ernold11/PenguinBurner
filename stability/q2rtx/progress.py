from __future__ import annotations

from typing import Callable

DependencyProgressCallback = Callable[[dict], None]


def _emit_dependency_progress(
    callback: DependencyProgressCallback | None,
    percent: float,
    detail: str,
    **payload,
) -> None:
    if callback is None:
        return
    try:
        percent_value = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        percent_value = 0.0
    data = {
        "label": "Downloading dependencies",
        "percent": round(percent_value, 1),
        "detail": str(detail),
    }
    data.update(payload)
    callback(data)


def _progress_range_value(
    start_pct: float,
    end_pct: float,
    local_percent: float,
) -> float:
    start = max(0.0, min(100.0, float(start_pct)))
    end = max(0.0, min(100.0, float(end_pct)))
    local = max(0.0, min(100.0, float(local_percent)))
    return start + ((end - start) * (local / 100.0))
