from __future__ import annotations

from .curve_planning import _build_descended_plan, _choose_sustained_clock_target
from .models import AutoUvProbeSummary
from .tuning import (
    AUTO_UV_CURVE_TUNING,
    AUTO_UV_METRIC_TUNING,
    AUTO_UV_STALL_TUNING,
)


def _percent(value: float | int) -> float:
    return max(0.0, float(value) / 100.0)


def _target_core_clock_floor(
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
        floor_base_clock_mhz = max(
            floor_base_clock_mhz,
            float(initial_probe_clock_mhz),
        )
    return (
        floor_base_clock_mhz * _percent(float(min_performance_core_clock_pct)),
        floor_base_clock_mhz,
    )


def _real_probe_lock_clock_mhz(
    plan: list[dict],
    *,
    probe: AutoUvProbeSummary,
    previous_lock_clock_mhz: int,
) -> int:
    if probe.avg_core_clock_mhz is None:
        return int(previous_lock_clock_mhz)
    measured_lock_clock_mhz = _choose_sustained_clock_target(
        plan,
        float(probe.avg_core_clock_mhz),
    )
    return min(int(previous_lock_clock_mhz), int(measured_lock_clock_mhz))


def _real_clock_adjusted_stable_curve(
    source_plan: list[dict],
    *,
    candidate_voltage_mv: int,
    previous_lock_clock_mhz: int,
    probe: AutoUvProbeSummary,
) -> tuple[list[dict], int]:
    adjusted_lock_clock_mhz = _real_probe_lock_clock_mhz(
        source_plan,
        probe=probe,
        previous_lock_clock_mhz=int(previous_lock_clock_mhz),
    )
    if int(adjusted_lock_clock_mhz) == int(previous_lock_clock_mhz):
        return (
            _build_descended_plan(
                source_plan,
                lock_clock_mhz=int(previous_lock_clock_mhz),
                candidate_voltage_mv=int(candidate_voltage_mv),
            ),
            int(previous_lock_clock_mhz),
        )
    return (
        _build_descended_plan(
            source_plan,
            lock_clock_mhz=int(adjusted_lock_clock_mhz),
            candidate_voltage_mv=int(candidate_voltage_mv),
        ),
        int(adjusted_lock_clock_mhz),
    )


def _telemetry_sample_is_busy(sample, busy_power_floor_w: float | None) -> bool:
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


def _core_clock_below_floor(
    core_clock_mhz: float,
    floor_mhz: float,
) -> bool:
    return float(core_clock_mhz) < (
        float(floor_mhz) - float(AUTO_UV_CURVE_TUNING.clock_select_tolerance_mhz)
    )


def _probe_failure_should_mark_voltage_unsafe(reason: str) -> bool:
    if str(reason).startswith(("timedemo-live-stall", "telemetry-live-load-lost")):
        return False
    return True


def _final_failure_can_accept_budget_curve(reason: str) -> bool:
    return str(reason).startswith(
        (
            "telemetry-live-core_clock",
            "telemetry-live-core_clock-avg",
            "core_clock-regression",
        )
    )


def _is_power_up_efficiency_down_regression(
    previous_probe: AutoUvProbeSummary | None,
    candidate_probe: AutoUvProbeSummary | None,
    efficiency_delta: dict[str, float | bool | None],
) -> bool:
    if previous_probe is None or candidate_probe is None:
        return False
    measured_voltage_drop_mv = efficiency_delta.get("measured_voltage_drop_mv")
    if measured_voltage_drop_mv is None or float(measured_voltage_drop_mv) <= 0.0:
        return False
    previous_power_w = efficiency_delta.get("previous_power_w")
    candidate_power_w = efficiency_delta.get("candidate_power_w")
    previous_fps_per_w = efficiency_delta.get("previous_fps_per_w")
    candidate_fps_per_w = efficiency_delta.get("candidate_fps_per_w")
    if previous_power_w is None or candidate_power_w is None:
        return False
    if previous_fps_per_w is None or candidate_fps_per_w is None:
        return False
    fps_per_w_delta_pct = (
        (float(candidate_fps_per_w) - float(previous_fps_per_w))
        / float(previous_fps_per_w)
        * 100.0
        if float(previous_fps_per_w) != 0.0
        else 0.0
    )
    return float(candidate_power_w) > float(previous_power_w) and float(
        fps_per_w_delta_pct
    ) <= float(AUTO_UV_METRIC_TUNING.min_temp_normalized_fps_per_w_improvement_pct)
