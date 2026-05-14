"""Live abort rules for a running Q2RTX/CUDA voltage probe.

These checks stop probes early on hard evidence: lost load, invalid timedemo output, stalls, or clocks below floor.
"""

from __future__ import annotations

from ..auto_uv_user_options import AUTO_UV_CURVE_TUNING, AUTO_UV_STALL_TUNING
from ..persistence.unsafe_voltage_cache import controlled_failure_reason


def percent(value: float | int) -> float:
    return max(0.0, float(value) / 100.0)


def target_core_clock_floor(
    *,
    lock_clock_mhz: int,
    initial_probe_clock_mhz: float | None,
    min_performance_core_clock_pct: float,
    enforce_target_core_clock_floor: bool,
) -> tuple[float | None, float | None]:
    if not enforce_target_core_clock_floor:
        return None, None
    floor_base_clock_mhz = float(lock_clock_mhz)
    if initial_probe_clock_mhz is not None:
        floor_base_clock_mhz = max(floor_base_clock_mhz, float(initial_probe_clock_mhz))
    return (
        floor_base_clock_mhz * percent(float(min_performance_core_clock_pct)),
        floor_base_clock_mhz,
    )


def telemetry_sample_is_busy(sample, busy_power_floor_w: float | None) -> bool:
    if sample is None:
        return False
    gpu_util_pct = getattr(sample, "gpu_util_pct", None)
    if (
        gpu_util_pct is not None
        and float(gpu_util_pct) >= AUTO_UV_STALL_TUNING.busy_gpu_util_pct
    ):
        return True
    power_w = getattr(sample, "power_w", None)
    return (
        power_w is not None
        and busy_power_floor_w is not None
        and float(power_w) >= float(busy_power_floor_w)
    )


def core_clock_below_floor(core_clock_mhz: float, floor_mhz: float) -> bool:
    return float(core_clock_mhz) < (
        float(floor_mhz) - float(AUTO_UV_CURVE_TUNING.clock_select_tolerance_mhz)
    )


def probe_failure_should_mark_voltage_unsafe(reason: str) -> bool:
    if controlled_failure_reason(reason):
        return False
    if str(reason).startswith(
        (
            "q2rtx-selected-nvidia-gpu-idle",
            "timedemo-live-stall",
            "user-stop-requested",
        )
    ):
        return False
    return True


def final_failure_can_accept_budget_curve(reason: str) -> bool:
    return str(reason).startswith(
        (
            "telemetry-live-core_clock",
            "telemetry-live-core_clock-avg",
            "core_clock-regression",
        )
    )
