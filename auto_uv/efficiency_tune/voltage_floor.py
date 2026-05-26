from __future__ import annotations

from ..scan_mode.uv_limits import uv_limit_voltage_floor_target_for_gpu


def min_search_voltage_mv(
    *,
    start_voltage_mv: int,
    configured_min_voltage_mv: int | None,
    configured_max_drop_pct: float,
    gpu_name: object | None,
) -> int:
    explicit_floor = positive_int(configured_min_voltage_mv)
    if explicit_floor is not None:
        return int(explicit_floor)

    table_target = uv_limit_voltage_floor_target_for_gpu(gpu_name)
    if table_target is not None:
        return int(table_target.voltage_mv)

    return percent_floor_voltage_mv(
        start_voltage_mv=int(start_voltage_mv),
        configured_max_drop_pct=float(configured_max_drop_pct),
    )


def percent_floor_voltage_mv(*, start_voltage_mv: int, configured_max_drop_pct: float) -> int:
    return max(
        0,
        int(
            round(
                float(start_voltage_mv)
                * (1.0 - (float(configured_max_drop_pct) / 100.0))
            )
        ),
    )


def positive_int(value: object | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
