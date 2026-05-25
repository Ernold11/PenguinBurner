from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..auto_uv_types import BaseLoadTarget
from .base_load_flatten_target import CurveTiming, choose_base_load_flatten_target
from .base_load_telemetry import LoadedTelemetryRules
from .vf_curve_flattening import build_flatten_target_for_plan, build_flattened_plan


@dataclass(frozen=True, slots=True)
class BaseLoadCurvePlan:
    target: BaseLoadTarget
    flatten_target: dict
    flattened_plan: list[dict]


def plan_base_load_curve_from_telemetry(
    base_curve: list[dict],
    telemetry_samples: list[Any],
    *,
    start_voltage_mv: int,
    power_limit_w: int | None,
    fallback_clock_mhz: float | None,
    tail_rise_bins: int = 0,
    curve: CurveTiming = CurveTiming(),
    telemetry_rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> BaseLoadCurvePlan:
    target = choose_base_load_flatten_target(
        base_curve,
        telemetry_samples,
        power_limit_w=power_limit_w,
        fallback_clock_mhz=fallback_clock_mhz,
        curve=curve,
        rules=telemetry_rules,
    )
    flattened_plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(target.target_clock_mhz),
        candidate_voltage_mv=int(start_voltage_mv),
        tail_rise_bins=int(tail_rise_bins),
    )
    return BaseLoadCurvePlan(
        target=target,
        flatten_target=build_flatten_target_for_plan(
            base_curve,
            flattened_plan,
            lock_clock_mhz=int(target.target_clock_mhz),
            lock_voltage_mv=int(start_voltage_mv),
            tail_rise_bins=int(tail_rise_bins),
        ),
        flattened_plan=flattened_plan,
    )
