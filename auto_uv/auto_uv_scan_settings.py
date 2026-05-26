"""Hold the knobs that shape one Auto-UV scan.

Settings are plain data so tests and UI code can construct a scan without knowing loop internals.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AutoUvScanSettings:
    start_voltage_mv: int
    min_search_voltage_mv: int | None
    preserve_base_below_mv: int | None
    baseline_core_clock_mhz: float | None
    auto_uv_mode: str = "efficiency"
    min_core_clock_pct: float = 90.0
    reference_actual_voltage_mv: float | None = None
    efficiency_stop_streak: int = 2
    min_efficiency_stop_voltage_drop_pct: float = 10.0
    tail_rise_bins: int = 0
