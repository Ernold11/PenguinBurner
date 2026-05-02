"""Score stable candidates when Auto-UV3 runs in performance mode.

FPS dominates the score; FPS/W only breaks ties when performance is already preserved.
"""

from __future__ import annotations


def performance_score_from_values(
    *,
    fps: float | None,
    baseline_fps: float | None,
    fps_per_w: float | None,
    baseline_fps_per_w: float | None,
) -> float | None:
    if (
        fps is None
        or baseline_fps is None
        or float(fps) <= 0.0
        or float(baseline_fps) <= 0.0
    ):
        return None
    fps_ratio = float(fps) / float(baseline_fps)
    fps_per_w_ratio = ratio_or_one(fps_per_w, baseline_fps_per_w)
    fps_weight = 8.0 if fps_ratio < 1.0 else 3.0
    return (
        100.0
        * min(max(fps_ratio, 0.0), 1.10) ** fps_weight
        * min(max(fps_per_w_ratio, 0.0), 1.45) ** 0.35
    )


def performance_candidate_sort_key(candidate: dict) -> tuple[bool, float, float, int, int]:
    score = float_or_none(candidate.get("performance_score"))
    fps = float_or_none(candidate.get("avg_fps"))
    return (
        score is None,
        -float(score or 0.0),
        -float(fps or 0.0),
        int(candidate.get("candidate_voltage_mv") or 99999),
        -int(candidate.get("lock_clock_mhz") or 0),
    )


def annotate_performance_candidate_scores(
    candidates: list[dict],
    *,
    baseline_fps: float | None,
    baseline_fps_per_w: float | None,
) -> None:
    for candidate in candidates:
        candidate["performance_score"] = performance_score_from_values(
            fps=float_or_none(candidate.get("avg_fps")),
            baseline_fps=baseline_fps,
            fps_per_w=float_or_none(candidate.get("efficiency_fps_per_w")),
            baseline_fps_per_w=baseline_fps_per_w,
        )


def ratio_or_one(value: float | None, baseline: float | None) -> float:
    if value is None or baseline is None or float(baseline) <= 0.0:
        return 1.0
    return float(value) / float(baseline)


def float_or_none(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
