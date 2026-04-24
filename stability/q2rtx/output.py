from __future__ import annotations

from collections import deque
from pathlib import Path

from .constants import (
    FATAL_OUTPUT_PATTERNS,
    KNOWN_TIMEDEMO_FRAME_COUNTS,
    TIMEDEMO_LINE_RE,
)
from .models import Q2RTXStabilityConfig, TelemetrySample, TimedemoRun


def _format_live_progress_state(state: dict, *, prefix: str) -> str:
    elapsed_s = float(state.get("elapsed_s", 0.0))
    workload_name = str(state.get("workload_name") or "?")
    last_run = state.get("last_run")
    latest_sample = state.get("latest_sample")

    parts = [
        prefix,
        f"demo={workload_name}",
        f"elapsed={elapsed_s:.1f}s",
    ]
    if last_run is not None:
        parts.append(
            f"last={int(last_run.frames)}f@{float(last_run.fps):.1f}fps/{float(last_run.seconds):.2f}s"
        )
    if latest_sample is not None and latest_sample.power_w is not None:
        parts.append(f"power={float(latest_sample.power_w):.1f}W")
    if latest_sample is not None and latest_sample.core_clock_mhz is not None:
        parts.append(f"core_clock={float(latest_sample.core_clock_mhz):.0f}MHz")
    if latest_sample is not None and latest_sample.voltage_mv is not None:
        parts.append(f"volt={float(latest_sample.voltage_mv):.0f}mV")
    if latest_sample is not None and latest_sample.gpu_util_pct is not None:
        parts.append(f"gpu={float(latest_sample.gpu_util_pct):.0f}%")
    if latest_sample is not None and latest_sample.temperature_c is not None:
        parts.append(f"temp={float(latest_sample.temperature_c):.0f}C")
    if latest_sample is not None and latest_sample.fan_speed_pct is not None:
        parts.append(f"fan={float(latest_sample.fan_speed_pct):.0f}%")
    fatal_output_matches = list(state.get("fatal_output_matches") or [])
    if fatal_output_matches:
        parts.append("fatal=" + ",".join(fatal_output_matches))
    return " ".join(parts)


def _format_sample_metrics(sample: TelemetrySample | None) -> str:
    if sample is None:
        return ""
    parts: list[str] = []
    if sample.power_w is not None:
        parts.append(f"power={float(sample.power_w):.1f}W")
    if sample.core_clock_mhz is not None:
        parts.append(f"core_clock={float(sample.core_clock_mhz):.0f}MHz")
    if sample.voltage_mv is not None:
        parts.append(f"volt={float(sample.voltage_mv):.0f}mV")
    if sample.gpu_util_pct is not None:
        parts.append(f"gpu={float(sample.gpu_util_pct):.0f}%")
    if sample.temperature_c is not None:
        parts.append(f"temp={float(sample.temperature_c):.0f}C")
    if sample.fan_speed_pct is not None:
        parts.append(f"fan={float(sample.fan_speed_pct):.0f}%")
    return " ".join(parts)


def attach_stdout_progress(
    config: Q2RTXStabilityConfig,
    *,
    prefix: str = "Stability live:",
) -> Q2RTXStabilityConfig:
    previous_progress_callback = config.progress_callback

    def _progress_callback(state: dict) -> None:
        print(_format_live_progress_state(state, prefix=prefix), flush=True)
        if previous_progress_callback is not None:
            previous_progress_callback(state)

    config.progress_callback = _progress_callback
    return config


def _read_recent_output(log_path: Path, *, tail_lines: int = 40) -> list[str]:
    if not log_path.exists():
        return []

    tail: deque[str] = deque(maxlen=max(1, int(tail_lines)))
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            tail.append(line.rstrip())
    return list(tail)


def _scan_output_for_fatal_patterns(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []

    matches: list[str] = []
    text = log_path.read_text(encoding="utf-8", errors="replace")
    for pattern in FATAL_OUTPUT_PATTERNS:
        if pattern in text:
            matches.append(pattern)
    return matches


def _extract_timedemo_runs(log_path: Path) -> list[TimedemoRun]:
    if not log_path.exists():
        return []

    text = log_path.read_text(encoding="utf-8", errors="replace")
    runs: list[TimedemoRun] = []
    for index, match in enumerate(TIMEDEMO_LINE_RE.finditer(text), start=1):
        runs.append(
            TimedemoRun(
                run_index=index,
                frames=int(match.group("frames")),
                seconds=float(match.group("seconds")),
                fps=float(match.group("fps")),
            )
        )
    return runs


def _expected_timedemo_frames(workload_name: str) -> int | None:
    return KNOWN_TIMEDEMO_FRAME_COUNTS.get(str(workload_name).strip().lower())
