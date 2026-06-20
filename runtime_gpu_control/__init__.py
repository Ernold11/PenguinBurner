"""Runtime GPU control helpers used by the foreground process.

The main script owns orchestration; this package owns public NVML and V/F helpers.
"""

from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "AdaptiveAutoUvRuntimeController": (
        ".adaptive_profile_runtime",
        "AdaptiveAutoUvRuntimeController",
    ),
    "AdaptiveAutoUvRuntimeDependencies": (
        ".adaptive_profile_runtime",
        "AdaptiveAutoUvRuntimeDependencies",
    ),
    "AdaptiveAutoUvSwitchResult": (
        ".adaptive_profile_runtime",
        "AdaptiveAutoUvSwitchResult",
    ),
    "AdaptiveProfileController": (".adaptive_profile_policy", "AdaptiveProfileController"),
    "AdaptiveProfileDecision": (".adaptive_profile_policy", "AdaptiveProfileDecision"),
    "AdaptiveProfilePolicyConfig": (
        ".adaptive_profile_policy",
        "AdaptiveProfilePolicyConfig",
    ),
    "FlattenedClockCeilingController": (
        ".flattened_clock_ceiling",
        "FlattenedClockCeilingController",
    ),
    "NVML_CLOCK_GRAPHICS": (".nvml_return_code", "NVML_CLOCK_GRAPHICS"),
    "NVML_CLOCK_MEM": (".nvml_return_code", "NVML_CLOCK_MEM"),
    "NVML_SUCCESS": (".nvml_return_code", "NVML_SUCCESS"),
    "NVML_TEMPERATURE_GPU": (".nvml_return_code", "NVML_TEMPERATURE_GPU"),
    "NvmlRuntimeSession": (".nvml_runtime_session", "NvmlRuntimeSession"),
    "OverlayStatePublisher": (".overlay_state_publisher", "OverlayStatePublisher"),
    "ProcessCpuUsage": (".process_cpu_sampler", "ProcessCpuUsage"),
    "ProcessCpuUsageSampler": (".process_cpu_sampler", "ProcessCpuUsageSampler"),
    "RuntimeVfCurvePolicyDependencies": (
        ".vf_curve_runtime_policy",
        "RuntimeVfCurvePolicyDependencies",
    ),
    "RuntimeVfCurvePolicyResult": (
        ".vf_curve_runtime_policy",
        "RuntimeVfCurvePolicyResult",
    ),
    "check_nvml_return_code": (".nvml_return_code", "check_nvml_return_code"),
    "configure_runtime_vf_curve_policy": (
        ".vf_curve_runtime_policy",
        "configure_runtime_vf_curve_policy",
    ),
    "describe_current_gpu_policy_state": (
        ".gpu_policy_state_text",
        "describe_current_gpu_policy_state",
    ),
    "detect_vf_curve_reset": (".vf_curve_reset_guard", "detect_vf_curve_reset"),
    "format_clock_ceiling_state": (
        ".live_gpu_telemetry_text",
        "format_clock_ceiling_state",
    ),
    "format_clock_offsets": (".live_gpu_telemetry_text", "format_clock_offsets"),
    "format_telemetry": (".live_gpu_telemetry_text", "format_telemetry"),
    "format_vf_curve_comparison": (
        ".live_gpu_telemetry_text",
        "format_vf_curve_comparison",
    ),
    "format_vf_curve_mismatch_preview": (
        ".vf_curve_reset_guard",
        "format_vf_curve_mismatch_preview",
    ),
    "get_core_clock_mhz": (".live_gpu_telemetry_text", "get_core_clock_mhz"),
    "get_memory_clock_mhz": (".live_gpu_telemetry_text", "get_memory_clock_mhz"),
    "get_power_draw_w": (".live_gpu_telemetry_text", "get_power_draw_w"),
    "get_reported_fan_speeds": (
        ".live_gpu_telemetry_text",
        "get_reported_fan_speeds",
    ),
    "khz_to_mhz": (".gpu_policy_state_text", "khz_to_mhz"),
    "select_expected_vf_samples": (
        ".vf_curve_reset_guard",
        "select_expected_vf_samples",
    ),
}


def __getattr__(name: str):
    export = _LAZY_EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = export
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    "FlattenedClockCeilingController",
    "AdaptiveAutoUvRuntimeController",
    "AdaptiveAutoUvRuntimeDependencies",
    "AdaptiveAutoUvSwitchResult",
    "AdaptiveProfileController",
    "AdaptiveProfileDecision",
    "AdaptiveProfilePolicyConfig",
    "NVML_CLOCK_GRAPHICS",
    "NVML_CLOCK_MEM",
    "NVML_SUCCESS",
    "NVML_TEMPERATURE_GPU",
    "NvmlRuntimeSession",
    "OverlayStatePublisher",
    "ProcessCpuUsage",
    "ProcessCpuUsageSampler",
    "RuntimeVfCurvePolicyDependencies",
    "RuntimeVfCurvePolicyResult",
    "check_nvml_return_code",
    "configure_runtime_vf_curve_policy",
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
    "select_expected_vf_samples",
]
