from __future__ import annotations

from pathlib import Path

from afterburner.import_fan_curve import load_config
from auto_uv3.scan_mode import AUTO_UV_MODE_EFFICIENCY
from auto_uv3.scan_mode import AUTO_UV_MODE_PERFORMANCE
from auto_uv3.auto_uv_user_options import AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO
from auto_uv3.auto_uv_user_options import AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO
from penguin_burner_paths import default_runtime_config_path


DEFAULT_SHORT_VERIFICATION_BASE_S = 20
PERFORMANCE_BIAS_WARNING_PCT = 110.0
PERFORMANCE_BIAS_DANGER_PCT = 130.0
DEFAULT_AUTO_UV_PERFORMANCE_BIAS_PCT = 100.0
DEFAULT_AUTO_UV_MAX_DROP_PCT = 15.0
DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT = 10.0
MAX_OVERCLOCK_BUDGET_PCT = AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO * 100.0
YOLO_MAX_OVERCLOCK_BUDGET_PCT = AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO * 100.0
GPU_UNDERVOLTING_PURPOSE_TEXT = (
    "GPU undervolting is meant to make your graphics card consume significantly "
    "less power while giving up as little performance as possible. The practical "
    "result can be dead-silent fan operation, lower temperatures, and lower "
    "electricity bills. PenguinBurner automatically searches for the operating "
    "sweet spot of your Nvidia GPU, so you do not have to resort to trial and "
    "error or risk introducing avoidable system instability."
)
PERFORMANCE_BIAS_TOOLTIP_TEXT = (
    "Controls how strongly Auto-UV favors recovering clocks after lowering "
    "voltage. Moving toward Performance allows more aggressive clock recovery. "
    "The far Performance side can push the curve above the measured baseline "
    "clock and might hang your system. Changing this can result in instability; "
    "modify with care."
)


def auto_uv_mode_for_performance_bias(value_pct: float | int) -> str:
    if float(value_pct) >= 100.0:
        return AUTO_UV_MODE_PERFORMANCE
    return AUTO_UV_MODE_EFFICIENCY


def performance_bias_clock_recovery_pct(
    slider_position: float | int,
    *,
    max_pct: float | int = MAX_OVERCLOCK_BUDGET_PCT,
) -> float:
    clamped = max(0.0, min(100.0, float(slider_position)))
    if clamped <= 50.0:
        return clamped * 2.0
    right_span = max(0.0, float(max_pct) - 100.0)
    return 100.0 + ((clamped - 50.0) / 50.0 * right_span)


def performance_bias_slider_position(
    value_pct: float | int,
    *,
    max_pct: float | int = MAX_OVERCLOCK_BUDGET_PCT,
) -> int:
    clamped = max(0.0, min(float(max_pct), float(value_pct)))
    if clamped <= 100.0:
        return int(round(clamped / 2.0))
    right_span = max(1.0, float(max_pct) - 100.0)
    return int(round(50.0 + ((clamped - 100.0) / right_span * 50.0)))


def slider_value_from_click_position(
    *,
    position_px: float,
    width_px: float,
    minimum: int,
    maximum: int,
    inverted: bool = False,
) -> int:
    if int(maximum) <= int(minimum):
        return int(minimum)
    width = max(1.0, float(width_px) - 1.0)
    ratio = max(0.0, min(1.0, float(position_px) / width))
    if inverted:
        ratio = 1.0 - ratio
    return int(round(int(minimum) + ratio * (int(maximum) - int(minimum))))


def memory_offset_mhz_range() -> tuple[int, int]:
    fallback = (0, 2000)
    controller = None
    try:
        from nvml_gpu_policy import NvmlGpuPolicyController

        controller = NvmlGpuPolicyController(
            gpu_index=_runtime_gpu_index(default_runtime_config_path())
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


def _runtime_gpu_index(config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except Exception:
        return 0
    gpu = config.get("gpu", {}) if isinstance(config, dict) else {}
    try:
        return max(0, int(gpu.get("index", 0)))
    except (AttributeError, TypeError, ValueError):
        return 0
