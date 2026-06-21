from __future__ import annotations

from curve_editors.fan.fan_curve_manual_editor import (
    FAN_MAX_POINTS,
    manual_add_fan_point_edit,
    manual_drag_fan_point_edit,
    manual_fan_curve_initial_edit,
    manual_nudge_selected_fan_speed,
    manual_nudge_selected_fan_temperature,
    manual_select_adjacent_fan_point,
    user_edited_fan_curve_profile_payload,
)


def test_manual_fan_curve_drag_snaps_and_preserves_order() -> None:
    edit = manual_fan_curve_initial_edit(
        [(45.0, 0.0), (60.0, 30.0), (75.0, 55.0)],
        selected_point=(60.0, 30.0),
    )

    dragged = manual_drag_fan_point_edit(
        edit,
        requested_temp_c=74.6,
        requested_speed_pct=58.8,
    )

    assert dragged.points == [(45.0, 0.0), (74.0, 55.0), (75.0, 55.0)]
    assert dragged.selected_index == 1


def test_manual_fan_curve_arrow_nudges_selected_axis() -> None:
    edit = manual_fan_curve_initial_edit(
        [(45.0, 0.0), (60.0, 30.0), (75.0, 55.0)],
        selected_point=(60.0, 30.0),
    )

    edit = manual_nudge_selected_fan_temperature(edit, direction=1)
    edit = manual_nudge_selected_fan_speed(edit, direction=-1)

    assert edit.points[1] == (61.0, 29.0)


def test_manual_fan_curve_select_adjacent_point() -> None:
    edit = manual_fan_curve_initial_edit(
        [(45.0, 0.0), (60.0, 30.0), (75.0, 55.0)],
        selected_point=(60.0, 30.0),
    )

    assert manual_select_adjacent_fan_point(edit, direction=1).selected_index == 2
    assert manual_select_adjacent_fan_point(edit, direction=-1).selected_index == 0


def test_manual_fan_curve_add_point_snaps_and_clamps_between_neighbors() -> None:
    edit = manual_fan_curve_initial_edit(
        [(45.0, 0.0), (60.0, 30.0), (75.0, 55.0)],
        selected_point=(60.0, 30.0),
    )

    added = manual_add_fan_point_edit(
        edit,
        requested_temp_c=64.6,
        requested_speed_pct=71.2,
    )

    assert added.points == [
        (45.0, 0.0),
        (60.0, 30.0),
        (65.0, 55.0),
        (75.0, 55.0),
    ]
    assert added.selected_index == 2


def test_manual_fan_curve_add_point_selects_existing_snapped_temperature() -> None:
    edit = manual_fan_curve_initial_edit(
        [(45.0, 0.0), (60.0, 30.0), (75.0, 55.0)],
        selected_point=(45.0, 0.0),
    )

    added = manual_add_fan_point_edit(
        edit,
        requested_temp_c=60.1,
        requested_speed_pct=42.0,
    )

    assert added.points == edit.points
    assert added.selected_index == 1


def test_manual_fan_curve_add_point_respects_runtime_point_limit() -> None:
    points = [(float(40 + index), float(index * 5)) for index in range(FAN_MAX_POINTS)]
    edit = manual_fan_curve_initial_edit(points, selected_point=points[0])

    added = manual_add_fan_point_edit(
        edit,
        requested_temp_c=55.0,
        requested_speed_pct=50.0,
    )

    assert len(added.points) == FAN_MAX_POINTS
    assert added.points == points


def test_user_edited_fan_curve_profile_payload_keeps_verified_vf_curve() -> None:
    edit = manual_fan_curve_initial_edit(
        [(45.0, 0.0), (60.0, 32.0), (75.0, 55.0)],
        selected_point=(60.0, 32.0),
    )

    payload = user_edited_fan_curve_profile_payload(
        {
            "profile_id": "parent",
            "path": "/tmp/parent.json",
            "candidate_id": "875mv-2610mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
            "final_verified": True,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 875,
                    "base_mhz": 2500,
                    "target_mhz": 2610,
                    "new_offset_mhz": 110,
                }
            ],
        },
        {
            "loaded_temperature_c": 75.0,
            "observed_fan_speed_pct": 55.0,
            "fan": {"curve": [[45.0, 0.0], [60.0, 30.0], [75.0, 55.0]]},
        },
        edit,
        original_points=[(45.0, 0.0), (60.0, 30.0), (75.0, 55.0)],
    )

    assert payload["profile_source"] == "user-edited"
    assert payload["final_verified"] is True
    assert payload["requires_verification"] is False
    assert payload["candidate_id"] == "user-edited-fan-875mv-2610mhz"
    assert payload["fan_curve_payload"]["fan"]["curve"] == [
        [45.0, 0.0],
        [60.0, 32.0],
        [75.0, 55.0],
    ]
    assert payload["manual_fan_edit"]["parent_profile_id"] == "parent"
