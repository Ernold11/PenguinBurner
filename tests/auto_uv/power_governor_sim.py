"""Test-only power-governor simulation for power-bound scan scenarios.

Closes the loop the plain probe fakes cannot express: given an APPLIED V/F
plan, a power model, and a board-power limit, compute where the governor
settles and synthesize the telemetry a probe would collect there. This is
what lets the suite state "measured clock is below target BECAUSE of the
power cap" instead of merely asserting a number.

Model: P(f, V) = idle_w + k * f * (V/1000)^2, plus an explicit synthetic
spike-reserve margin. Tests exercise several coefficient/margin combinations
and assert only regime invariants; this helper is not fitted or presented as
a hardware prediction for any particular 5090.

Test infrastructure only — never product code (the scan must not depend on
a governor model; it reads real telemetry).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GovernorPowerModel:
    dynamic_w_per_mhz_v2: float
    idle_w: float = 40.0
    spike_margin_mv: float = 15.0


def scenario_5090_power_models() -> tuple[GovernorPowerModel, ...]:
    """Distinct synthetic response curves used to test invariant behavior."""
    return (
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.18,
            idle_w=30.0,
            spike_margin_mv=0.0,
        ),
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.20,
            idle_w=40.0,
            spike_margin_mv=15.0,
        ),
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.22,
            idle_w=50.0,
            spike_margin_mv=25.0,
        ),
    )


def point_power_w(
    model: GovernorPowerModel,
    *,
    clock_mhz: float,
    voltage_mv: float,
) -> float:
    volts = float(voltage_mv) / 1000.0
    return model.idle_w + model.dynamic_w_per_mhz_v2 * float(clock_mhz) * volts * volts


def settle_operating_point(
    plan: list[dict],
    *,
    model: GovernorPowerModel,
    power_limit_w: float,
) -> dict:
    """Where the governor parks on ``plan`` under ``power_limit_w``.

    The governor runs the highest-voltage curve point whose power fits the
    limit; when the limit binds, it additionally backs off by the model's
    spike margin. Returns voltage_mv/clock_mhz/power_w plus power_capped.
    """
    points = sorted(
        (
            (int(point["voltage_mv"]), int(point["target_mhz"]))
            for point in plan
            if point.get("voltage_mv") is not None
            and point.get("target_mhz") is not None
        ),
        key=lambda item: item[0],
    )
    if not points:
        raise ValueError("plan has no usable V/F points")

    def fits(point: tuple[int, int]) -> bool:
        voltage_mv, clock_mhz = point
        return (
            point_power_w(model, clock_mhz=clock_mhz, voltage_mv=voltage_mv)
            <= float(power_limit_w)
        )

    feasible = [point for point in points if fits(point)]
    power_capped = len(feasible) < len(points)
    if not feasible:
        settle_voltage_mv, settle_clock_mhz = points[0]
    elif not power_capped:
        settle_voltage_mv, settle_clock_mhz = points[-1]
    else:
        top_feasible_voltage_mv = feasible[-1][0]
        margin_ceiling_mv = top_feasible_voltage_mv - float(model.spike_margin_mv)
        margined = [point for point in feasible if point[0] <= margin_ceiling_mv]
        settle_voltage_mv, settle_clock_mhz = (
            margined[-1] if margined else feasible[0]
        )
    return {
        "voltage_mv": int(settle_voltage_mv),
        "clock_mhz": int(settle_clock_mhz),
        "power_w": point_power_w(
            model,
            clock_mhz=settle_clock_mhz,
            voltage_mv=settle_voltage_mv,
        ),
        "power_capped": bool(power_capped),
    }


def synthesize_busy_samples(
    operating_point: dict,
    *,
    count: int = 8,
    first_elapsed_s: float = 6.0,
) -> list[dict]:
    """Busy telemetry a probe would collect at the settled operating point.

    Power-capped points carry the driver's sw-power perf-cap reason — the
    same evidence the real scan reads to distinguish governor descent from
    silent reliability demotion.
    """
    return [
        {
            "elapsed_s": float(first_elapsed_s) + float(index),
            "gpu_util_pct": 99.0,
            "power_w": float(operating_point["power_w"]),
            "core_clock_mhz": float(operating_point["clock_mhz"]),
            "voltage_mv": float(operating_point["voltage_mv"]),
            "perf_cap_reason": (
                "sw-power" if operating_point["power_capped"] else "none"
            ),
        }
        for index in range(int(count))
    ]
