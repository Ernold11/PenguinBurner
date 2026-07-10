"""Abort a live Q2RTX/CUDA probe only on strong instability evidence.

Rules cover lost load, low clocks, and selected-GPU idle conditions.
"""

from __future__ import annotations

from auto_uv.domain.user_options import (
    AUTO_UV_CURVE_TUNING,
    AUTO_UV_METRIC_TUNING,
    AUTO_UV_STALL_TUNING,
)
from .runtime_guardrails import core_clock_below_floor, telemetry_sample_is_busy
from .summary import mean


def telemetry_live_abort_reason(
    state: dict,
    *,
    busy_power_floor_w: float | None,
    proper_run_power_floor_w: float | None,
    target_core_clock_floor_mhz: float | None,
    progress_state: dict,
) -> str | None:
    samples = list(state.get("telemetry_samples") or [])
    busy_samples = [s for s in samples if telemetry_sample_is_busy(s, busy_power_floor_w)]
    clocks = [
        float(sample.core_clock_mhz)
        for sample in busy_samples
        if sample is not None and sample.core_clock_mhz is not None
    ]
    latest = state.get("latest_sample")
    latest_busy = telemetry_sample_is_busy(latest, busy_power_floor_w)
    lost = load_lost_abort_reason(
        state,
        latest_sample=latest,
        proper_run_power_floor_w=proper_run_power_floor_w,
        busy_power_floor_w=busy_power_floor_w,
        live_sample_is_busy=latest_busy,
        progress_state=progress_state,
    )
    if lost is not None:
        return lost
    selected_gpu_idle = selected_nvidia_gpu_idle_abort_reason(state)
    if selected_gpu_idle is not None:
        return selected_gpu_idle
    live_clock = (
        float(latest.core_clock_mhz)
        if latest is not None and latest.core_clock_mhz is not None
        else None
    )
    low_live = low_live_clock_abort_reason(
        live_core_clock_mhz=live_clock,
        live_sample_is_busy=latest_busy,
        core_clock_samples=clocks,
        target_core_clock_floor_mhz=target_core_clock_floor_mhz,
        progress_state=progress_state,
    )
    if low_live is not None:
        return low_live
    avg_clock = mean(clocks)
    if (
        target_core_clock_floor_mhz is not None
        and avg_clock is not None
        and len(clocks) >= AUTO_UV_STALL_TUNING.avg_core_clock_abort_min_samples
        and core_clock_below_floor(avg_clock, target_core_clock_floor_mhz)
    ):
        return (
            f"telemetry-live-core_clock-avg current={avg_clock:.1f}MHz "
            f"floor={target_core_clock_floor_mhz:.1f}MHz "
            f"tolerance={AUTO_UV_CURVE_TUNING.clock_select_tolerance_mhz:.1f}MHz"
        )
    return None


def selected_nvidia_gpu_idle_abort_reason(state: dict) -> str | None:
    if float(state.get("elapsed_s", 0.0)) < float(
        AUTO_UV_STALL_TUNING.selected_gpu_idle_min_s
    ):
        return None
    samples = [
        sample
        for sample in list(state.get("telemetry_samples") or [])
        if sample is not None
        and getattr(sample, "elapsed_s", None) is not None
        and float(sample.elapsed_s)
        >= float(AUTO_UV_METRIC_TUNING.loaded_sample_warmup_s)
    ]
    if len(samples) < int(AUTO_UV_STALL_TUNING.selected_gpu_idle_min_samples):
        return None
    util_values = [
        float(sample.gpu_util_pct)
        for sample in samples
        if getattr(sample, "gpu_util_pct", None) is not None
    ]
    power_values = [
        float(sample.power_w)
        for sample in samples
        if getattr(sample, "power_w", None) is not None
    ]
    if not util_values and not power_values:
        return None
    max_util = max(util_values) if util_values else None
    max_power = max(power_values) if power_values else None
    util_idle = (
        max_util is not None
        and float(max_util)
        <= float(AUTO_UV_STALL_TUNING.selected_gpu_idle_max_util_pct)
    )
    power_idle = (
        max_power is not None
        and float(max_power)
        <= float(AUTO_UV_STALL_TUNING.selected_gpu_idle_max_power_w)
    )
    if max_util is not None and not util_idle:
        return None
    if max_power is not None and not power_idle:
        return None
    # At least one metric is present (guarded above) and every present metric is
    # idle, so reaching here always means the selected GPU is idle.
    util_text = f"{float(max_util):.1f}%" if max_util is not None else "n/a"
    power_text = f"{float(max_power):.1f}W" if max_power is not None else "n/a"
    return (
        "q2rtx-selected-nvidia-gpu-idle "
        f"max_util={util_text} max_power={power_text} "
        "hint=Q2RTX may be rendering on another GPU; on multi-GPU systems "
        "select the display-attached card with --gpu-index"
    )


def load_lost_abort_reason(
    state: dict,
    *,
    latest_sample,
    proper_run_power_floor_w: float | None,
    busy_power_floor_w: float | None,
    live_sample_is_busy: bool,
    progress_state: dict,
) -> str | None:
    if (
        proper_run_power_floor_w is None
        or latest_sample is None
        or latest_sample.power_w is None
        or len(list(state.get("telemetry_samples") or []))
        < AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
        or float(state.get("elapsed_s", 0.0))
        < float(AUTO_UV_METRIC_TUNING.loaded_sample_warmup_s)
    ):
        return None
    progress_state["low_power_streak"] = (
        int(progress_state.get("low_power_streak", 0)) + 1
        if not live_sample_is_busy
        and float(latest_sample.power_w) < float(proper_run_power_floor_w)
        else 0
    )
    if (
        int(progress_state["low_power_streak"])
        < AUTO_UV_METRIC_TUNING.target_core_clock_low_streak_samples
    ):
        return None
    busy_text = (
        f"{float(busy_power_floor_w):.1f}W"
        if busy_power_floor_w is not None
        else "n/a"
    )
    return (
        f"telemetry-live-load-lost current={float(latest_sample.power_w):.1f}W "
        f"floor={float(proper_run_power_floor_w):.1f}W "
        f"busy-floor={busy_text} "
        f"load-floor={AUTO_UV_METRIC_TUNING.min_proper_run_power_pct:.1f}%"
    )


def low_live_clock_abort_reason(
    *,
    live_core_clock_mhz: float | None,
    live_sample_is_busy: bool,
    core_clock_samples: list[float],
    target_core_clock_floor_mhz: float | None,
    progress_state: dict,
) -> str | None:
    if (
        target_core_clock_floor_mhz is None
        or live_core_clock_mhz is None
        or len(core_clock_samples) < AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
    ):
        return None
    progress_state["low_core_clock_streak"] = (
        int(progress_state.get("low_core_clock_streak", 0)) + 1
        if live_sample_is_busy
        and core_clock_below_floor(live_core_clock_mhz, target_core_clock_floor_mhz)
        else 0
    )
    if (
        int(progress_state["low_core_clock_streak"])
        < AUTO_UV_METRIC_TUNING.target_core_clock_low_streak_samples
    ):
        return None
    return (
        f"telemetry-live-core_clock current={live_core_clock_mhz:.1f}MHz "
        f"floor={target_core_clock_floor_mhz:.1f}MHz "
        f"tolerance={AUTO_UV_CURVE_TUNING.clock_select_tolerance_mhz:.1f}MHz"
    )
