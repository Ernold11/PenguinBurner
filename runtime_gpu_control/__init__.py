"""Runtime GPU control helpers used by the foreground process.

The main script owns orchestration; this package holds small NVML and V/F helpers.
"""

from .flattened_clock_ceiling import FlattenedClockCeilingController
from .gpu_policy_state_text import describe_current_gpu_policy_state, khz_to_mhz
from .live_gpu_telemetry_text import (
    format_clock_ceiling_state,
    format_clock_offsets,
    format_telemetry,
    format_vf_curve_comparison,
    get_core_clock_mhz,
    get_memory_clock_mhz,
    get_power_draw_w,
    get_reported_fan_speeds,
)
from .nvidia_smi_command import apply_gpu_base_policy, run_nvidia_smi_command
from .nvml_return_code import (
    NVML_CLOCK_GRAPHICS,
    NVML_CLOCK_MEM,
    NVML_SUCCESS,
    NVML_TEMPERATURE_GPU,
    check_nvml_return_code,
)
from .vf_curve_reset_guard import (
    detect_vf_curve_reset,
    format_vf_curve_mismatch_preview,
    select_expected_vf_samples,
)

__all__ = [
    "FlattenedClockCeilingController",
    "NVML_CLOCK_GRAPHICS",
    "NVML_CLOCK_MEM",
    "NVML_SUCCESS",
    "NVML_TEMPERATURE_GPU",
    "apply_gpu_base_policy",
    "check_nvml_return_code",
    "detect_vf_curve_reset",
    "describe_current_gpu_policy_state",
    "format_clock_ceiling_state",
    "format_clock_offsets",
    "format_telemetry",
    "format_vf_curve_comparison",
    "format_vf_curve_mismatch_preview",
    "get_core_clock_mhz",
    "get_memory_clock_mhz",
    "get_power_draw_w",
    "get_reported_fan_speeds",
    "khz_to_mhz",
    "run_nvidia_smi_command",
    "select_expected_vf_samples",
]
