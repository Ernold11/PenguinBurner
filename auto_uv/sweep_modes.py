from __future__ import annotations

from .efficiency import AUTO_UV_MODE_EFFICIENCY, AutoUvEfficiencyBehavior
from .performance import AUTO_UV_MODE_PERFORMANCE, AutoUvPerformanceBehavior
from .sweep_behavior import AutoUvSweepBehavior


AUTO_UV_MODES = (AUTO_UV_MODE_EFFICIENCY, AUTO_UV_MODE_PERFORMANCE)

_AUTO_UV_MODE_ALIASES = {
    "": AUTO_UV_MODE_EFFICIENCY,
    "balanced": AUTO_UV_MODE_EFFICIENCY,
    "efficiency": AUTO_UV_MODE_EFFICIENCY,
    "aggressive": AUTO_UV_MODE_PERFORMANCE,
    "performance": AUTO_UV_MODE_PERFORMANCE,
}


def normalize_auto_uv_mode(value: object | None) -> str:
    normalized = str(value or "").strip().lower()
    return _AUTO_UV_MODE_ALIASES.get(normalized, AUTO_UV_MODE_EFFICIENCY)


def make_auto_uv_sweep_behavior(
    mode: object | None,
    *,
    efficiency_stop_streak: int,
    min_efficiency_stop_voltage_drop_pct: float,
) -> AutoUvSweepBehavior:
    normalized = normalize_auto_uv_mode(mode)
    if normalized == AUTO_UV_MODE_PERFORMANCE:
        return AutoUvPerformanceBehavior()
    return AutoUvEfficiencyBehavior(
        efficiency_stop_streak=int(efficiency_stop_streak),
        min_efficiency_stop_voltage_drop_pct=float(
            min_efficiency_stop_voltage_drop_pct
        ),
    )
