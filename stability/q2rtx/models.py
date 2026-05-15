from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .constants import (
    DEFAULT_DEMO_NAME,
    DEFAULT_DURATION_S,
    DEFAULT_HIDE_WINDOW,
    DEFAULT_HEIGHT,
    DEFAULT_LOG_DIR,
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_SINGLE_PASS_TIMEOUT_S,
    DEFAULT_WIDTH,
)


class StabilityTestError(RuntimeError):
    pass


@dataclass(slots=True)
class TelemetrySample:
    elapsed_s: float
    gpu_util_pct: float | None
    power_w: float | None
    core_clock_mhz: float | None
    temperature_c: float | None
    voltage_mv: float | None
    fan_speed_pct: float | None
    perf_cap_reason: str | None = None


@dataclass(slots=True)
class TimedemoRun:
    run_index: int
    frames: int
    seconds: float
    fps: float


@dataclass(slots=True)
class Q2RTXStabilityConfig:
    duration_s: int = DEFAULT_DURATION_S
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    hide_window: bool = DEFAULT_HIDE_WINDOW
    demo_name: str = DEFAULT_DEMO_NAME
    timedemo_loops: int | None = None
    gpu_index: int = 0
    q2rtx_dir: Path | None = None
    q2rtx_binary: Path | None = None
    log_dir: Path = DEFAULT_LOG_DIR
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    single_pass_timeout_s: float = DEFAULT_SINGLE_PASS_TIMEOUT_S
    progress_callback: Callable[[dict], None] | None = None
    abort_callback: Callable[[dict], str | None] | None = None
    companion_command: tuple[str, ...] | None = None
    use_headless_gamescope: bool = True


@dataclass(slots=True)
class Q2RTXStabilityResult:
    success: bool
    reason: str
    workload_kind: str
    workload_name: str
    command: list[str]
    executable_path: Path
    workdir: Path
    duration_requested_s: int
    timedemo_loops_requested: int | None
    duration_observed_s: float
    demo_path: Path | None
    log_path: Path
    process_exit_code: int | None
    shutdown_mode: str
    fatal_output_matches: list[str]
    xid_messages: list[str]
    timedemo_runs: list[TimedemoRun]
    telemetry_samples: list[TelemetrySample]
    companion_telemetry_samples: list[TelemetrySample]
    output_tail: list[str]

    def telemetry_summary(self) -> dict[str, float | int]:
        samples = self.telemetry_samples
        summary: dict[str, float | int] = {
            "sample_count": len(samples),
        }
        if not samples:
            return summary

        def _values(attr: str) -> list[float]:
            values: list[float] = []
            for sample in samples:
                value = getattr(sample, attr)
                if value is not None:
                    values.append(float(value))
            return values

        for attr, prefix in (
            ("gpu_util_pct", "gpu_util"),
            ("power_w", "power"),
            ("core_clock_mhz", "core_clock"),
            ("temperature_c", "temperature"),
            ("voltage_mv", "voltage"),
            ("fan_speed_pct", "fan"),
        ):
            values = _values(attr)
            if values:
                summary[f"{prefix}_avg"] = sum(values) / len(values)
                summary[f"{prefix}_max"] = max(values)

        return summary


@dataclass(slots=True)
class Q2RTXInstallResult:
    version: str
    asset_name: str
    asset_url: str
    archive_path: Path
    install_dir: Path
    executable_path: Path
