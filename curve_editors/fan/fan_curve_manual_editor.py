from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math

from auto_uv.domain.user_options import AUTO_UV_FAN_TUNING


FAN_TEMP_STEP_C = 1.0
FAN_SPEED_STEP_PCT = 1.0
FAN_MIN_TEMP_C = 0.0
FAN_MAX_TEMP_C = 120.0
FAN_MIN_SPEED_PCT = 0.0
FAN_MAX_SPEED_PCT = 100.0
FAN_MAX_POINTS = max(2, int(AUTO_UV_FAN_TUNING.max_curve_points))
USER_EDITED_FAN_PROFILE_SOURCE = "user-edited"


@dataclass(frozen=True, slots=True)
class ManualFanCurveEdit:
    points: list[tuple[float, float]]
    edit_kind: str
    selected_index: int = 0


def manual_fan_curve_initial_edit(
    points: list[tuple[float, float]],
    *,
    selected_point: tuple[float, float] | None = None,
) -> ManualFanCurveEdit:
    normalized = _normalize_fan_points(points)
    selected_index = _nearest_point_index(normalized, selected_point)
    return ManualFanCurveEdit(
        points=normalized,
        edit_kind="unchanged",
        selected_index=int(selected_index),
    )


def manual_select_fan_point(
    edit: ManualFanCurveEdit,
    *,
    point_index: int | None = None,
    point: tuple[float, float] | None = None,
) -> ManualFanCurveEdit:
    points = _normalize_fan_points(edit.points)
    if not points:
        return ManualFanCurveEdit(points=[], edit_kind="select", selected_index=0)
    if point_index is None:
        selected_index = _nearest_point_index(points, point)
    else:
        selected_index = _clamp_int(point_index, 0, len(points) - 1)
    return ManualFanCurveEdit(
        points=points,
        edit_kind="select",
        selected_index=int(selected_index),
    )


def manual_select_adjacent_fan_point(
    edit: ManualFanCurveEdit,
    *,
    direction: int,
) -> ManualFanCurveEdit:
    points = _normalize_fan_points(edit.points)
    if not points:
        return ManualFanCurveEdit(points=[], edit_kind="select", selected_index=0)
    selected_index = _clamp_int(
        int(edit.selected_index) + (1 if int(direction) >= 0 else -1),
        0,
        len(points) - 1,
    )
    return ManualFanCurveEdit(
        points=points,
        edit_kind="select",
        selected_index=int(selected_index),
    )


def manual_drag_fan_point_edit(
    edit: ManualFanCurveEdit,
    *,
    requested_temp_c: float,
    requested_speed_pct: float,
    point_index: int | None = None,
) -> ManualFanCurveEdit:
    points = _normalize_fan_points(edit.points)
    if not points:
        return ManualFanCurveEdit(points=[], edit_kind="drag", selected_index=0)
    selected_index = _clamp_int(
        edit.selected_index if point_index is None else point_index,
        0,
        len(points) - 1,
    )
    temp_c = _clamp_temperature_for_index(
        points,
        selected_index,
        _snap_value(requested_temp_c, FAN_TEMP_STEP_C),
    )
    speed_pct = _clamp_speed_for_index(
        points,
        selected_index,
        _snap_value(requested_speed_pct, FAN_SPEED_STEP_PCT),
    )
    edited = list(points)
    edited[selected_index] = (float(temp_c), float(speed_pct))
    return ManualFanCurveEdit(
        points=edited,
        edit_kind="drag",
        selected_index=int(selected_index),
    )


def manual_add_fan_point_edit(
    edit: ManualFanCurveEdit,
    *,
    requested_temp_c: float,
    requested_speed_pct: float,
) -> ManualFanCurveEdit:
    points = _normalize_fan_points(edit.points)
    temp_c = _clamp_float(
        _snap_value(requested_temp_c, FAN_TEMP_STEP_C),
        FAN_MIN_TEMP_C,
        FAN_MAX_TEMP_C,
    )
    speed_pct = _clamp_float(
        _snap_value(requested_speed_pct, FAN_SPEED_STEP_PCT),
        FAN_MIN_SPEED_PCT,
        FAN_MAX_SPEED_PCT,
    )
    if not points:
        return ManualFanCurveEdit(
            points=[(float(temp_c), float(speed_pct))],
            edit_kind="add-point",
            selected_index=0,
        )
    for index, point in enumerate(points):
        if float(point[0]) == float(temp_c):
            return ManualFanCurveEdit(
                points=points,
                edit_kind="select",
                selected_index=int(index),
            )
    if len(points) >= FAN_MAX_POINTS:
        return manual_select_fan_point(edit, point=(float(temp_c), float(speed_pct)))
    insert_index = 0
    while insert_index < len(points) and float(points[insert_index][0]) < float(temp_c):
        insert_index += 1
    lower_speed = (
        FAN_MIN_SPEED_PCT
        if insert_index <= 0
        else float(points[insert_index - 1][1])
    )
    upper_speed = (
        FAN_MAX_SPEED_PCT
        if insert_index >= len(points)
        else float(points[insert_index][1])
    )
    speed_pct = _clamp_float(speed_pct, lower_speed, upper_speed)
    edited = list(points)
    edited.insert(insert_index, (float(temp_c), float(speed_pct)))
    return ManualFanCurveEdit(
        points=edited,
        edit_kind="add-point",
        selected_index=int(insert_index),
    )


def manual_nudge_selected_fan_temperature(
    edit: ManualFanCurveEdit,
    *,
    direction: int,
) -> ManualFanCurveEdit:
    points = _normalize_fan_points(edit.points)
    if not points:
        return ManualFanCurveEdit(points=[], edit_kind="nudge-temp", selected_index=0)
    selected_index = _clamp_int(edit.selected_index, 0, len(points) - 1)
    temp_c, speed_pct = points[selected_index]
    return manual_drag_fan_point_edit(
        edit,
        point_index=selected_index,
        requested_temp_c=float(temp_c)
        + (FAN_TEMP_STEP_C if int(direction) >= 0 else -FAN_TEMP_STEP_C),
        requested_speed_pct=float(speed_pct),
    )


def manual_nudge_selected_fan_speed(
    edit: ManualFanCurveEdit,
    *,
    direction: int,
) -> ManualFanCurveEdit:
    points = _normalize_fan_points(edit.points)
    if not points:
        return ManualFanCurveEdit(points=[], edit_kind="nudge-speed", selected_index=0)
    selected_index = _clamp_int(edit.selected_index, 0, len(points) - 1)
    temp_c, speed_pct = points[selected_index]
    return manual_drag_fan_point_edit(
        edit,
        point_index=selected_index,
        requested_temp_c=float(temp_c),
        requested_speed_pct=float(speed_pct)
        + (FAN_SPEED_STEP_PCT if int(direction) >= 0 else -FAN_SPEED_STEP_PCT),
    )


def user_edited_fan_curve_profile_payload(
    parent_profile: dict,
    fan_payload: dict,
    edit: ManualFanCurveEdit,
    *,
    original_points: list[tuple[float, float]] | None = None,
) -> dict:
    parent = dict(parent_profile)
    payload = {
        key: value
        for key, value in parent.items()
        if key not in {"profile_id", "profile_created_at", "path", "verified_at"}
    }
    parent_profile_id = str(parent.get("profile_id", "")).strip()
    parent_path = str(parent.get("path", "")).strip()
    points = _normalize_fan_points(edit.points)
    edited_fan_payload = _fan_payload_with_points(fan_payload, points)
    parent_candidate_id = str(
        parent.get("candidate_id") or parent.get("profile_id") or "profile"
    ).strip()
    voltage = parent.get("candidate_voltage_mv", parent.get("voltage_mv", "na"))
    clock = parent.get("lock_clock_mhz", parent.get("clock_mhz", "na"))
    parent_verified = bool(parent.get("final_verified", False))
    parent_requires_verification = bool(parent.get("requires_verification", False))
    payload.update(
        {
            "profile_source": USER_EDITED_FAN_PROFILE_SOURCE,
            "display_name": f"User edited fan curve {clock} MHz {voltage} mV",
            "candidate_id": f"user-edited-fan-{parent_candidate_id}",
            "final_verified": parent_verified,
            "requires_verification": (
                False if parent_verified else parent_requires_verification
            ),
            "verification_status": (
                "verified"
                if parent_verified
                else str(parent.get("verification_status") or "unverified")
            ),
            "fan_curve_payload": edited_fan_payload,
            "manual_fan_edit": {
                "edit_kind": str(edit.edit_kind),
                "parent_profile_id": parent_profile_id,
                "parent_path": parent_path,
                "selected_index": int(edit.selected_index),
                "original_points": _json_points(original_points or []),
                "points": _json_points(points),
            },
        }
    )
    return payload


def _fan_payload_with_points(
    fan_payload: dict,
    points: list[tuple[float, float]],
) -> dict:
    payload = dict(fan_payload)
    fan = dict(payload.get("fan") if isinstance(payload.get("fan"), dict) else {})
    fan["curve"] = _json_points(points)
    payload["fan"] = fan
    payload["manual_edit_source"] = "penguin-burner-ui"
    payload["manual_edited_at"] = datetime.now().astimezone().isoformat()
    return payload


def _json_points(points: list[tuple[float, float]]) -> list[list[float]]:
    return [[float(temp_c), float(speed_pct)] for temp_c, speed_pct in points]


def _normalize_fan_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    normalized = []
    seen: set[tuple[float, float]] = set()
    for raw in points or []:
        try:
            temp_c = float(raw[0])
            speed_pct = float(raw[1])
        except (IndexError, TypeError, ValueError):
            continue
        if not math.isfinite(temp_c) or not math.isfinite(speed_pct):
            continue
        temp_c = _clamp_float(
            _snap_value(temp_c, FAN_TEMP_STEP_C),
            FAN_MIN_TEMP_C,
            FAN_MAX_TEMP_C,
        )
        speed_pct = _clamp_float(
            _snap_value(speed_pct, FAN_SPEED_STEP_PCT),
            FAN_MIN_SPEED_PCT,
            FAN_MAX_SPEED_PCT,
        )
        key = (float(temp_c), float(speed_pct))
        if key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return sorted(normalized, key=lambda point: (point[0], point[1]))


def _nearest_point_index(
    points: list[tuple[float, float]],
    point: tuple[float, float] | None,
) -> int:
    if not points:
        return 0
    if point is None:
        return len(points) - 1
    try:
        wanted_temp = float(point[0])
        wanted_speed = float(point[1])
    except (IndexError, TypeError, ValueError):
        return len(points) - 1
    best_index = 0
    best_distance = None
    for index, (temp_c, speed_pct) in enumerate(points):
        distance = (float(temp_c) - wanted_temp) ** 2 + (
            float(speed_pct) - wanted_speed
        ) ** 2
        if best_distance is None or distance < best_distance:
            best_index = index
            best_distance = distance
    return int(best_index)


def _clamp_temperature_for_index(
    points: list[tuple[float, float]],
    selected_index: int,
    requested_temp_c: float,
) -> float:
    minimum = FAN_MIN_TEMP_C
    maximum = FAN_MAX_TEMP_C
    if selected_index > 0:
        minimum = float(points[selected_index - 1][0]) + FAN_TEMP_STEP_C
    if selected_index < len(points) - 1:
        maximum = float(points[selected_index + 1][0]) - FAN_TEMP_STEP_C
    if maximum < minimum:
        return float(points[selected_index][0])
    return _clamp_float(requested_temp_c, minimum, maximum)


def _clamp_speed_for_index(
    points: list[tuple[float, float]],
    selected_index: int,
    requested_speed_pct: float,
) -> float:
    minimum = FAN_MIN_SPEED_PCT
    maximum = FAN_MAX_SPEED_PCT
    if selected_index > 0:
        minimum = float(points[selected_index - 1][1])
    if selected_index < len(points) - 1:
        maximum = float(points[selected_index + 1][1])
    if maximum < minimum:
        return float(points[selected_index][1])
    return _clamp_float(requested_speed_pct, minimum, maximum)


def _snap_value(value: float, step: float) -> float:
    step_value = max(0.001, float(step))
    return round(float(value) / step_value) * step_value


def _clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(float(minimum), min(float(maximum), float(value)))


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    if maximum < minimum:
        maximum = minimum
    return max(int(minimum), min(int(maximum), int(value)))
