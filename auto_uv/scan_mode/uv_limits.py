"""Borrowed GPU voltage/frequency targets used by Auto-UV."""

from __future__ import annotations

from dataclasses import dataclass


AUTO_UV_PERFORMANCE_OC_PROFILE_ID = "performance"

# Balanced blends the efficiency and performance clock-drop limits, weighted
# toward efficiency so the preset leans into power savings (0.6 efficiency /
# 0.4 performance) rather than sitting at the dead-center midpoint.
_BALANCED_EFFICIENCY_WEIGHT = 0.6


@dataclass(frozen=True, slots=True)
class UvTierTarget:
    gpu_family: str
    profile_id: str
    voltage_mv: int
    clock_mhz: int


_UV_LIMIT_TARGETS: tuple[dict[str, object], ...] = (
    {
        "family": "RTX 5090",
        "patterns": ("5090",),
        "efficiency": (900, 2700),
        "balanced": (950, 2900),
        "performance": (975, 3000),
        "clock_drop_ceiling_mhz": 3100,
    },
    {
        "family": "RTX 5080",
        "patterns": ("5080",),
        "efficiency": (850, 2800),
        "balanced": (900, 2800),
        "performance": (925, 2980),
        "clock_drop_ceiling_mhz": 3150,
    },
    {
        "family": "RTX 5070 Ti",
        "patterns": ("5070 TI", "5070TI"),
        "efficiency": (850, 2500),
        "balanced": (900, 2800),
        "performance": (925, 2950),
        "clock_drop_ceiling_mhz": 3000,
    },
    {
        "family": "RTX 5070",
        "patterns": ("5070",),
        "efficiency": (850, 2600),
        "balanced": (900, 2750),
        "performance": (940, 3000),
        "clock_drop_ceiling_mhz": 3150,
    },
    {
        "family": "RTX 5060 Ti",
        "patterns": ("5060 TI", "5060TI"),
        "efficiency": (800, 2500),
        "balanced": (875, 2700),
        "performance": (925, 2900),
        "clock_drop_ceiling_mhz": 3000,
    },
    {
        "family": "RTX 5060",
        "patterns": ("5060",),
        "efficiency": (875, 2300),
        "balanced": (900, 2450),
        "performance": (925, 2600),
        "clock_drop_ceiling_mhz": 2730,
    },
    {
        "family": "RTX 4070 Ti Super",
        "patterns": ("4070 TI SUPER", "4070TI SUPER"),
        "efficiency": (925, 2550),
        "balanced": (940, 2640),
        "performance": (950, 2730),
        "clock_drop_ceiling_mhz": 2820,
    },
    {
        "family": "RTX 4070 Ti",
        "patterns": ("4070 TI", "4070TI"),
        "efficiency": (925, 2550),
        "balanced": (940, 2640),
        "performance": (950, 2685),
        "clock_drop_ceiling_mhz": 2820,
    },
    {
        "family": "RTX 4070 Super",
        "patterns": ("4070 SUPER",),
        "efficiency": (900, 2400),
        "balanced": (925, 2550),
        "performance": (940, 2670),
        "clock_drop_ceiling_mhz": 2790,
    },
    {
        "family": "RTX 4070",
        "patterns": ("4070",),
        "efficiency": (900, 2400),
        "balanced": (925, 2550),
        "performance": (940, 2670),
        "clock_drop_ceiling_mhz": 2790,
    },
    {
        "family": "RTX 4060 Ti",
        "patterns": ("4060 TI", "4060TI"),
        "efficiency": (900, 2400),
        "balanced": (925, 2550),
        "performance": (950, 2650),
        "clock_drop_ceiling_mhz": 2750,
    },
    {
        "family": "RTX 4060",
        "patterns": ("4060",),
        "efficiency": (875, 2300),
        "balanced": (900, 2450),
        "performance": (925, 2600),
        "clock_drop_ceiling_mhz": 2730,
    },
    {
        "family": "RTX 4090",
        "patterns": ("4090",),
        "efficiency": (875, 2400),
        "balanced": (900, 2550),
        "performance": (925, 2670),
        "clock_drop_ceiling_mhz": 2745,
    },
    {
        "family": "RTX 4080",
        "patterns": ("4080",),
        "efficiency": (875, 2400),
        "balanced": (900, 2520),
        "performance": (925, 2640),
        "clock_drop_ceiling_mhz": 2700,
    },
    {
        "family": "RTX 3090 Ti",
        "patterns": ("3090 TI", "3090TI"),
        "efficiency": (825, 1700),
        "balanced": (875, 1830),
        "performance": (925, 1950),
        "clock_drop_ceiling_mhz": 2025,
    },
    {
        "family": "RTX 3090",
        "patterns": ("3090",),
        "efficiency": (800, 1700),
        "balanced": (875, 1830),
        "performance": (900, 1900),
        "clock_drop_ceiling_mhz": 1965,
    },
    {
        "family": "RTX 3080 Ti",
        "patterns": ("3080 TI", "3080TI"),
        "efficiency": (800, 1710),
        "balanced": (875, 1870),
        "performance": (900, 1920),
        "clock_drop_ceiling_mhz": 1980,
    },
    {
        "family": "RTX 3080 12GB",
        "patterns": ("3080 12GB", "3080 12 GB", "3080-12"),
        "efficiency": (800, 1700),
        "balanced": (875, 1860),
        "performance": (900, 1920),
        "clock_drop_ceiling_mhz": 2000,
    },
    {
        "family": "RTX 3080",
        "patterns": ("3080",),
        "efficiency": (800, 1750),
        "balanced": (875, 1890),
        "performance": (900, 1950),
        "clock_drop_ceiling_mhz": 2010,
    },
    {
        "family": "RTX 3070 Ti",
        "patterns": ("3070 TI", "3070TI"),
        "efficiency": (825, 1770),
        "balanced": (875, 1905),
        "performance": (900, 1950),
        "clock_drop_ceiling_mhz": 1995,
    },
    {
        "family": "RTX 3070",
        "patterns": ("3070",),
        "efficiency": (775, 1700),
        "balanced": (875, 1900),
        "performance": (925, 1950),
        "clock_drop_ceiling_mhz": 2010,
    },
    {
        "family": "RTX 3060 Ti",
        "patterns": ("3060 TI", "3060TI"),
        "efficiency": (800, 1750),
        "balanced": (875, 1875),
        "performance": (925, 1935),
        "clock_drop_ceiling_mhz": 1980,
    },
    {
        "family": "RTX 3060",
        "patterns": ("3060",),
        "efficiency": (800, 1750),
        "balanced": (850, 1840),
        "performance": (900, 1900),
        "clock_drop_ceiling_mhz": 1950,
    },
)


def uv_limit_voltage_floor_target_for_gpu(
    gpu_name: object | None,
    auto_uv_mode: object | None = None,
) -> UvTierTarget | None:
    _ = auto_uv_mode
    return uv_limit_profile_target_for_gpu(gpu_name, "efficiency")


def uv_limit_clock_drop_pct_for_gpu(
    gpu_name: object | None,
    profile_id: object | None = "efficiency",
) -> float | None:
    entry = _uv_limit_entry_for_gpu(gpu_name)
    if entry is None:
        return None
    profile = str(profile_id or "efficiency").strip().lower()
    if profile == "balanced":
        # Balanced is a savings-biased blend of the efficiency and performance
        # clock-drop limits. Deriving it from a single clock ratio
        # (efficiency.clock / performance.clock) collapsed balanced toward
        # whichever neighbour the table's clock geometry sat closest to - the
        # RTX 5080 fell to ~6% (almost identical to performance) while the
        # RTX 5070 Ti rose to ~15% (almost identical to efficiency). Weighting it
        # toward efficiency keeps balanced centered-but-deeper on every GPU so it
        # actually saves power while staying short of the full efficiency drop.
        efficiency_pct = _derived_clock_drop_pct(entry, "efficiency")
        performance_pct = _derived_clock_drop_pct(entry, "performance")
        if efficiency_pct is None or performance_pct is None:
            return _derived_clock_drop_pct(entry, "balanced")
        return (
            efficiency_pct * _BALANCED_EFFICIENCY_WEIGHT
            + performance_pct * (1.0 - _BALANCED_EFFICIENCY_WEIGHT)
        )
    return _derived_clock_drop_pct(entry, profile)


def _derived_clock_drop_pct(
    entry: dict[str, object],
    profile_id: str,
) -> float | None:
    lower_clock_mhz, upper_clock_mhz = _uv_limit_clock_drop_bounds_mhz_from_entry(
        entry,
        profile_id,
    )
    if int(lower_clock_mhz) <= 0 or int(upper_clock_mhz) <= 0:
        return None
    drop_pct = 1.0 - (float(lower_clock_mhz) / float(upper_clock_mhz))
    return max(0.0, drop_pct * 100.0)


def uv_limit_profile_target_for_gpu(
    gpu_name: object | None,
    profile_id: str,
) -> UvTierTarget | None:
    entry = _uv_limit_entry_for_gpu(gpu_name)
    if entry is None:
        return None
    return _uv_limit_profile_target_from_entry(entry, profile_id)


def _uv_limit_entry_for_gpu(gpu_name: object | None) -> dict[str, object] | None:
    normalized_name = str(gpu_name or "").upper()
    if not normalized_name:
        return None

    for entry in _UV_LIMIT_TARGETS:
        patterns = tuple(str(pattern) for pattern in entry["patterns"])
        if any(pattern in normalized_name for pattern in patterns):
            return entry
    return None


def _uv_limit_profile_target_from_entry(
    entry: dict[str, object],
    profile_id: str,
) -> UvTierTarget | None:
    normalized_profile = str(profile_id or "").strip().lower()
    value = entry.get(normalized_profile)
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    voltage_mv, clock_mhz = value
    return UvTierTarget(
        gpu_family=str(entry["family"]),
        profile_id=normalized_profile,
        voltage_mv=int(voltage_mv),
        clock_mhz=int(clock_mhz),
    )


def _uv_limit_clock_drop_ceiling_mhz_from_entry(entry: dict[str, object]) -> int:
    value = entry.get("clock_drop_ceiling_mhz")
    if value is not None:
        return int(value)
    performance = _uv_limit_profile_target_from_entry(entry, "performance")
    if performance is None:
        return 0
    return int(performance.clock_mhz)


def _uv_limit_clock_drop_bounds_mhz_from_entry(
    entry: dict[str, object],
    profile_id: object | None,
) -> tuple[int, int]:
    profile = str(profile_id or "efficiency").strip().lower()
    efficiency = _uv_limit_profile_target_from_entry(entry, "efficiency")
    performance = _uv_limit_profile_target_from_entry(entry, "performance")
    ceiling_clock_mhz = _uv_limit_clock_drop_ceiling_mhz_from_entry(entry)
    if efficiency is None:
        return 0, 0
    if profile == "performance" and performance is not None:
        return int(performance.clock_mhz), int(ceiling_clock_mhz)
    if profile == "balanced" and performance is not None:
        return int(efficiency.clock_mhz), int(performance.clock_mhz)
    return int(efficiency.clock_mhz), int(ceiling_clock_mhz)


def voltage_drop_pct(*, start_voltage_mv: int, floor_voltage_mv: int) -> float:
    if int(start_voltage_mv) <= 0:
        return 0.0
    drop_mv = max(0, int(start_voltage_mv) - int(floor_voltage_mv))
    return float(drop_mv) / float(start_voltage_mv) * 100.0
