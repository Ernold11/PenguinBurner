"""Summarize Q2RTX/CUDA probe output into the Auto-UV probe data model.

The summary uses only loaded telemetry samples so flattening and stability checks reflect actual GPU load.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..auto_uv_types import AutoUvProbeSummary
from ..auto_uv_user_options import AUTO_UV_METRIC_TUNING
from ..curve.base_load_telemetry import (
    derive_active_power_floor_w,
    decision_samples,
    saturated_tail_samples,
)


def mean(values: Sequence[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def max_or_none(values: Sequence[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return max(usable) if usable else None


def sample_mean(samples: list, attr: str) -> float | None:
    return mean(
        [
            getattr(sample, attr)
            for sample in decision_samples(samples, rules=AUTO_UV_METRIC_TUNING)
            if sample is not None and getattr(sample, attr, None) is not None
        ]
    )


def sample_max(samples: list, attr: str) -> float | None:
    return max_or_none(
        [
            getattr(sample, attr)
            for sample in samples
            if sample is not None and getattr(sample, attr, None) is not None
        ]
    )


def decision_timedemo_runs(
    timedemo_runs: Sequence,
    *,
    timedemo_warmup_runs: int = 0,
) -> list:
    runs = list(timedemo_runs or [])
    warmup_runs = max(0, int(timedemo_warmup_runs or 0))
    min_remaining_runs = max(
        1,
        int(AUTO_UV_METRIC_TUNING.timedemo_warmup_min_remaining_runs),
    )
    if warmup_runs <= 0 or len(runs) < warmup_runs + min_remaining_runs:
        return runs
    return runs[warmup_runs:]


def history_average(history: list[AutoUvProbeSummary], attr: str) -> float | None:
    return mean([getattr(item, attr) for item in history])


def loaded_telemetry_means(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool = False,
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    int,
    float | None,
]:
    samples = decision_samples(telemetry_samples, rules=AUTO_UV_METRIC_TUNING)
    active_power_floor_w = derive_active_power_floor_w(
        samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        rules=AUTO_UV_METRIC_TUNING,
    )
    if active_power_floor_w is None:
        return None, None, None, None, None, 0, None

    active_samples = [
        sample
        for sample in samples
        if sample is not None
        and getattr(sample, "power_w", None) is not None
        and float(sample.power_w) >= float(active_power_floor_w)
    ]
    if not active_samples:
        return None, None, None, None, None, 0, float(active_power_floor_w)

    return (
        mean([getattr(sample, "power_w", None) for sample in active_samples]),
        mean([getattr(sample, "core_clock_mhz", None) for sample in active_samples]),
        mean([getattr(sample, "voltage_mv", None) for sample in active_samples]),
        mean([getattr(sample, "temperature_c", None) for sample in active_samples]),
        mean([getattr(sample, "fan_speed_pct", None) for sample in active_samples]),
        len(active_samples),
        float(active_power_floor_w),
    )


def summarize_q2rtx_cuda_probe(
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    live_voltage_before_mv: int | None,
    live_voltage_after_mv: int | None,
    used_companion_load: bool,
    power_limit_w: int | None,
    result,
    telemetry_samples: list | None = None,
    use_power_limit_floor: bool = False,
    timedemo_warmup_runs: int = 0,
) -> AutoUvProbeSummary:
    timedemo_runs = decision_timedemo_runs(
        result.timedemo_runs,
        timedemo_warmup_runs=int(timedemo_warmup_runs),
    )
    fps_values = [float(run.fps) for run in timedemo_runs]
    frame_values = [int(run.frames) for run in timedemo_runs]
    q2rtx_samples = (
        list(telemetry_samples)
        if telemetry_samples is not None
        else list(result.telemetry_samples)
    )
    telemetry = result.telemetry_summary()
    if telemetry_samples is not None:
        telemetry = {
            "sample_count": len(q2rtx_samples),
            "power_max": sample_max(q2rtx_samples, "power_w"),
            "temperature_max": sample_max(q2rtx_samples, "temperature_c"),
            "fan_max": sample_max(q2rtx_samples, "fan_speed_pct"),
        }
    companion_samples = list(getattr(result, "companion_telemetry_samples", []) or [])
    max_power_w = max_or_none(
        [telemetry.get("power_max"), sample_max(companion_samples, "power_w")]
    )
    max_temperature_c = max_or_none(
        [
            telemetry.get("temperature_max"),
            sample_max(companion_samples, "temperature_c"),
        ]
    )
    max_fan_speed_pct = max_or_none(
        [telemetry.get("fan_max"), sample_max(companion_samples, "fan_speed_pct")]
    )
    loaded = loaded_telemetry_means(
        q2rtx_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
    )
    cuda_loaded = loaded_telemetry_means(
        companion_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
    )
    loaded_power_w, loaded_clock_mhz, loaded_voltage_mv = loaded[:3]
    loaded_temperature_c, loaded_fan_speed_pct = loaded[3:5]
    _cuda_power_w, cuda_loaded_clock_mhz, cuda_loaded_voltage_mv = cuda_loaded[:3]
    q2rtx_summary_clock_mhz = loaded_clock_mhz or sample_mean(
        q2rtx_samples,
        "core_clock_mhz",
    )
    q2rtx_summary_voltage_mv = loaded_voltage_mv or sample_mean(
        q2rtx_samples,
        "voltage_mv",
    )
    cuda_summary_clock_mhz = cuda_loaded_clock_mhz or sample_mean(
        companion_samples,
        "core_clock_mhz",
    )
    cuda_summary_voltage_mv = cuda_loaded_voltage_mv or sample_mean(
        companion_samples,
        "voltage_mv",
    )
    avg_fps = mean(fps_values)
    summary_power_w = loaded_power_w or telemetry.get("power_avg")
    summary_clock_mhz = loaded_clock_mhz or telemetry.get("core_clock_avg")
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(candidate_voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        live_voltage_before_mv=live_voltage_before_mv,
        live_voltage_after_mv=live_voltage_after_mv,
        avg_voltage_mv=loaded_voltage_mv or telemetry.get("voltage_avg"),
        frames_per_run=frame_values[0] if frame_values else None,
        avg_seconds_per_run=mean([float(run.seconds) for run in timedemo_runs]),
        avg_fps=avg_fps,
        min_fps=min(fps_values) if fps_values else None,
        max_fps=max(fps_values) if fps_values else None,
        avg_power_w=summary_power_w,
        max_power_w=float(max_power_w) if max_power_w is not None else None,
        avg_temperature_c=loaded_temperature_c or telemetry.get("temperature_avg"),
        max_temperature_c=(
            float(max_temperature_c) if max_temperature_c is not None else None
        ),
        avg_fan_speed_pct=loaded_fan_speed_pct or telemetry.get("fan_avg"),
        max_fan_speed_pct=(
            float(max_fan_speed_pct) if max_fan_speed_pct is not None else None
        ),
        avg_core_clock_mhz=summary_clock_mhz,
        efficiency_fps_per_w=(
            avg_fps / float(summary_power_w)
            if avg_fps is not None
            and summary_power_w not in (None, 0, 0.0)
            and fps_values
            else None
        ),
        efficiency_mhz_per_w=(
            float(summary_clock_mhz) / float(summary_power_w)
            if summary_clock_mhz is not None and summary_power_w not in (None, 0, 0.0)
            else None
        ),
        watts_per_mhz=(
            float(summary_power_w) / float(summary_clock_mhz)
            if summary_clock_mhz not in (None, 0, 0.0) and summary_power_w is not None
            else None
        ),
        used_companion_load=bool(used_companion_load),
        result_reason=str(result.reason),
        log_path=Path(result.log_path),
        q2rtx_avg_voltage_mv=q2rtx_summary_voltage_mv,
        q2rtx_avg_core_clock_mhz=q2rtx_summary_clock_mhz,
        cuda_avg_voltage_mv=cuda_summary_voltage_mv,
        cuda_avg_core_clock_mhz=cuda_summary_clock_mhz,
    )


def saturated_probe_tail_samples(telemetry_samples: list) -> list:
    return saturated_tail_samples(telemetry_samples, rules=AUTO_UV_METRIC_TUNING)
