"""Runtime fan-control helpers.

The package owns fan curve math and saved Auto-UV fan curve validation.
"""

from .auto_uv_saved_fan_curve import (
    load_auto_uv_fan_curve,
    validate_auto_uv_fan_curve_safety,
)
from .afterburner_imported_fan_curve import load_runtime_afterburner_fan_config
from .fan_curve_runtime_rules import (
    apply_hysteresis,
    build_effective_manual_curve,
    clamp,
    describe_fan_curve_state,
    format_curve_points,
    format_curve_temp,
    limit_speed_change,
    speed_for_temp,
    validate_curve,
)
from .runtime_loop import RuntimeFanLoopDependencies, run_runtime_fan_control_loop

__all__ = [
    "RuntimeFanLoopDependencies",
    "apply_hysteresis",
    "build_effective_manual_curve",
    "clamp",
    "describe_fan_curve_state",
    "format_curve_points",
    "format_curve_temp",
    "limit_speed_change",
    "load_auto_uv_fan_curve",
    "load_runtime_afterburner_fan_config",
    "run_runtime_fan_control_loop",
    "speed_for_temp",
    "validate_auto_uv_fan_curve_safety",
    "validate_curve",
]
