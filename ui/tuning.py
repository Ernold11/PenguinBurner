from __future__ import annotations

from dataclasses import dataclass

from auto_uv.auto_uv_user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode import AUTO_UV_MODE_EFFICIENCY
from auto_uv.scan_mode import AUTO_UV_MODE_PERFORMANCE
from auto_uv.scan_mode.uv_limits import (
    AUTO_UV_PERFORMANCE_OC_PROFILE_ID,
    uv_limit_eco_to_max_clock_drop_pct_for_gpu,
    uv_limit_profile_target_for_gpu,
    uv_limit_voltage_floor_target_for_gpu,
    voltage_drop_pct,
)
from common.penguin_burner_paths import default_runtime_config_path

from .gpu_selection import runtime_gpu_index


DEFAULT_SHORT_VERIFICATION_BASE_S = 10
DEFAULT_AUTO_UV_MAX_DROP_PCT = AUTO_UV_DEFAULTS.max_drop_pct
GENERIC_AUTO_UV_MAX_DROP_PCT = DEFAULT_AUTO_UV_MAX_DROP_PCT
AUTO_UV_DROP_REFERENCE_VOLTAGE_MV = 1000
DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT = AUTO_UV_DEFAULTS.max_core_clock_drop_pct
DEFAULT_AUTO_UV_TAIL_RISE_BINS = 0
DEFAULT_AUTO_UV_BALANCED_TAIL_RISE_BINS = AUTO_UV_DEFAULTS.balanced_tail_rise_bins
DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS = AUTO_UV_DEFAULTS.performance_tail_rise_bins
MAX_AUTO_UV_TAIL_RISE_BINS = AUTO_UV_DEFAULTS.max_tail_rise_bins
AUTO_UV_PRESET_EFFICIENCY = "efficiency"
AUTO_UV_PRESET_BALANCED = "balanced"
AUTO_UV_PRESET_PERFORMANCE = "performance"
DEFAULT_AUTO_UV_PRESET = AUTO_UV_PRESET_BALANCED
GPU_UNDERVOLTING_PURPOSE_TEXT = (
    "GPU undervolting is meant to make your graphics card consume significantly "
    "less power while giving up as little performance as possible. The practical "
    "result can be dead-silent fan operation, lower temperatures, and lower "
    "electricity bills. PenguinBurner automatically searches for the operating "
    "sweet spot of your Nvidia GPU, so you do not have to resort to trial and "
    "error or risk introducing avoidable system instability."
)


@dataclass(frozen=True, slots=True)
class AutoUvVoltageDropDefault:
    value_pct: float
    gpu_name: str | None
    gpu_family: str | None
    floor_voltage_mv: int | None
    reference_voltage_mv: int
    preset_matched: bool


@dataclass(frozen=True, slots=True)
class AutoUvPerformanceTargetDefault:
    gpu_name: str | None
    gpu_family: str | None
    voltage_mv: int | None
    clock_mhz: int | None
    profile_id: str
    preset_matched: bool


@dataclass(frozen=True, slots=True)
class AutoUvClockDropDefault:
    value_pct: float
    gpu_name: str | None
    gpu_family: str | None
    preset_matched: bool


@dataclass(frozen=True, slots=True)
class AutoUvPreset:
    preset_id: str
    label: str
    auto_uv_mode: str
    tail_rise_bins: int


def auto_uv_preset(preset_id: object) -> AutoUvPreset:
    normalized = str(preset_id or DEFAULT_AUTO_UV_PRESET).strip().lower()
    if normalized == AUTO_UV_PRESET_EFFICIENCY:
        return AutoUvPreset(
            preset_id=AUTO_UV_PRESET_EFFICIENCY,
            label="Efficiency",
            auto_uv_mode=AUTO_UV_MODE_EFFICIENCY,
            tail_rise_bins=DEFAULT_AUTO_UV_TAIL_RISE_BINS,
        )
    if normalized == AUTO_UV_PRESET_PERFORMANCE:
        return AutoUvPreset(
            preset_id=AUTO_UV_PRESET_PERFORMANCE,
            label="Performance",
            auto_uv_mode=AUTO_UV_MODE_PERFORMANCE,
            tail_rise_bins=DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS,
        )
    return AutoUvPreset(
        preset_id=AUTO_UV_PRESET_BALANCED,
        label="Balanced",
        auto_uv_mode=AUTO_UV_MODE_EFFICIENCY,
        tail_rise_bins=DEFAULT_AUTO_UV_BALANCED_TAIL_RISE_BINS,
    )


def auto_uv_presets() -> tuple[AutoUvPreset, ...]:
    return (
        auto_uv_preset(AUTO_UV_PRESET_EFFICIENCY),
        auto_uv_preset(AUTO_UV_PRESET_BALANCED),
        auto_uv_preset(AUTO_UV_PRESET_PERFORMANCE),
    )


def auto_uv_voltage_drop_default(
    *,
    gpu_name: object | None = None,
    gpu_index: int | None = None,
    auto_uv_mode: object | None = None,
    reference_voltage_mv: int = AUTO_UV_DROP_REFERENCE_VOLTAGE_MV,
) -> AutoUvVoltageDropDefault:
    detected_name = str(gpu_name).strip() if gpu_name else _query_gpu_name(gpu_index)
    target = uv_limit_voltage_floor_target_for_gpu(
        detected_name,
        auto_uv_mode,
    )
    if target is None:
        floor_voltage_mv = int(
            round(
                float(reference_voltage_mv)
                * (1.0 - (float(DEFAULT_AUTO_UV_MAX_DROP_PCT) / 100.0))
            )
        )
        return AutoUvVoltageDropDefault(
            value_pct=float(DEFAULT_AUTO_UV_MAX_DROP_PCT),
            gpu_name=detected_name or None,
            gpu_family=None,
            floor_voltage_mv=floor_voltage_mv,
            reference_voltage_mv=int(reference_voltage_mv),
            preset_matched=False,
        )
    return AutoUvVoltageDropDefault(
        value_pct=voltage_drop_pct(
            start_voltage_mv=int(reference_voltage_mv),
            floor_voltage_mv=int(target.voltage_mv),
        ),
        gpu_name=detected_name or None,
        gpu_family=str(target.gpu_family),
        floor_voltage_mv=int(target.voltage_mv),
        reference_voltage_mv=int(reference_voltage_mv),
        preset_matched=True,
    )


def auto_uv_clock_drop_default(
    *,
    gpu_name: object | None = None,
    gpu_index: int | None = None,
) -> AutoUvClockDropDefault:
    detected_name = str(gpu_name).strip() if gpu_name else _query_gpu_name(gpu_index)
    value_pct = uv_limit_eco_to_max_clock_drop_pct_for_gpu(detected_name)
    target = uv_limit_profile_target_for_gpu(detected_name, "eco")
    if value_pct is None:
        return AutoUvClockDropDefault(
            value_pct=float(DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT),
            gpu_name=detected_name or None,
            gpu_family=None,
            preset_matched=False,
        )
    return AutoUvClockDropDefault(
        value_pct=float(value_pct),
        gpu_name=detected_name or None,
        gpu_family=str(target.gpu_family) if target is not None else None,
        preset_matched=True,
    )


def auto_uv_performance_target_default(
    *,
    gpu_name: object | None = None,
    gpu_index: int | None = None,
) -> AutoUvPerformanceTargetDefault:
    detected_name = str(gpu_name).strip() if gpu_name else _query_gpu_name(gpu_index)
    profile_id = AUTO_UV_PERFORMANCE_OC_PROFILE_ID
    target = uv_limit_profile_target_for_gpu(detected_name, profile_id)
    if target is None:
        return AutoUvPerformanceTargetDefault(
            gpu_name=detected_name or None,
            gpu_family=None,
            voltage_mv=None,
            clock_mhz=None,
            profile_id=str(profile_id),
            preset_matched=False,
        )
    return AutoUvPerformanceTargetDefault(
        gpu_name=detected_name or None,
        gpu_family=str(target.gpu_family),
        voltage_mv=int(target.voltage_mv),
        clock_mhz=int(target.clock_mhz),
        profile_id=str(target.profile_id),
        preset_matched=True,
    )


def auto_uv_performance_target_text(
    target: AutoUvPerformanceTargetDefault,
) -> str:
    if (
        target.preset_matched
        and target.voltage_mv is not None
        and target.clock_mhz is not None
    ):
        family = target.gpu_family or "GPU table"
        return (
            f"{int(target.voltage_mv)} mV / {int(target.clock_mhz)} MHz "
            f"({family} {target.profile_id})"
        )
    return "No GPU table target detected"


def auto_uv_performance_preset_label(_preview=None) -> str:
    return "Performance"


def auto_uv_performance_preset_tooltip(_preview=None) -> str:
    return (
        "Use the same undervolt search as Balanced, but let the tail of the "
        "curve rise 6 V/F bins up from the locked point."
    )


def memory_offset_mhz_range() -> tuple[int, int]:
    fallback = (0, 2000)
    controller = None
    try:
        from nvidia_driver.nvml_gpu_policy import NvmlGpuPolicyController

        controller = NvmlGpuPolicyController(
            gpu_index=runtime_gpu_index(default_runtime_config_path())
        )
        driver_range = controller.get_memory_clock_offset_range_mhz()
    except Exception:
        return fallback
    finally:
        if controller is not None:
            try:
                controller.close()
            except Exception:
                pass
    if not driver_range:
        return fallback
    _driver_min, driver_max = driver_range
    try:
        max_mhz = int(driver_max)
    except (TypeError, ValueError):
        return fallback
    return 0, max(0, min(fallback[1], max_mhz))


def _query_gpu_name(gpu_index: int | None = None) -> str | None:
    controller = None
    try:
        from nvidia_driver.nvml_gpu_policy import NvmlGpuPolicyController

        index = int(gpu_index) if gpu_index is not None else runtime_gpu_index(
            default_runtime_config_path()
        )
        controller = NvmlGpuPolicyController(gpu_index=index)
        name = controller.query_gpu_name()
    except Exception:
        return None
    finally:
        if controller is not None:
            try:
                controller.close()
            except Exception:
                pass
    return str(name).strip() if name else None


