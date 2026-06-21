"""Manual fan-curve editing helpers used by the UI profile editor.

The package works on saved profile payloads and does not run Auto-UV probes.
"""

from .fan_curve_manual_editor import (
    FAN_MAX_POINTS,
    ManualFanCurveEdit,
    manual_add_fan_point_edit,
    manual_drag_fan_point_edit,
    manual_fan_curve_initial_edit,
    manual_nudge_selected_fan_speed,
    manual_nudge_selected_fan_temperature,
    manual_select_adjacent_fan_point,
    manual_select_fan_point,
    user_edited_fan_curve_profile_payload,
)

__all__ = [
    "FAN_MAX_POINTS",
    "ManualFanCurveEdit",
    "manual_add_fan_point_edit",
    "manual_drag_fan_point_edit",
    "manual_fan_curve_initial_edit",
    "manual_nudge_selected_fan_speed",
    "manual_nudge_selected_fan_temperature",
    "manual_select_adjacent_fan_point",
    "manual_select_fan_point",
    "user_edited_fan_curve_profile_payload",
]
