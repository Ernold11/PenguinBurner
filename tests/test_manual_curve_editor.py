from __future__ import annotations

from auto_uv.manual_curve_editor import (
    manual_drag_anchor_edit,
    manual_flatten_from_existing_point,
    manual_nudge_selected_voltage,
    manual_nudge_selected_frequency,
    manual_offset_selected_range,
    manual_select_adjacent_point,
    manual_select_curve_point,
    manual_select_range_to_right,
    manual_tune_single_point_edit,
    user_edited_profile_payload,
)


def _plan() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": 800 + index * 10,
            "base_mhz": 2400 + index * 15,
            "target_mhz": 2400 + index * 15,
            "new_offset_mhz": 0,
        }
        for index in range(25)
    ]


def test_manual_drag_anchor_snaps_without_fixed_movement_caps() -> None:
    edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=1060,
        requested_clock_mhz=2807,
    )

    assert edit.anchor_voltage_mv == 1040
    assert edit.anchor_clock_mhz == 2805
    assert all(
        point["target_mhz"] == 2805
        for point in edit.plan
        if point["voltage_mv"] >= 1040
    )


def test_manual_drag_anchor_allows_moving_left_and_down() -> None:
    edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=840,
        requested_clock_mhz=2460,
    )

    assert edit.anchor_voltage_mv == 840
    assert edit.anchor_clock_mhz == 2460
    assert all(
        point["target_mhz"] == 2460
        for point in edit.plan
        if point["voltage_mv"] >= 840
    )


def test_manual_drag_anchor_keeps_small_upward_edits_unsmoothed() -> None:
    edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2625,
    )

    assert edit.anchor_voltage_mv == 900
    assert edit.anchor_clock_mhz == 2625
    assert _target_at(edit.plan, 890) == 2535
    assert _target_at(edit.plan, 900) == 2625


def test_manual_drag_anchor_smooths_left_side_for_large_upward_edits() -> None:
    edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2775,
    )

    assert edit.anchor_voltage_mv == 900
    assert edit.anchor_clock_mhz == 2775
    assert _target_at(edit.plan, 850) == 2475
    assert _target_at(edit.plan, 860) == 2535
    assert _target_at(edit.plan, 870) == 2595
    assert _target_at(edit.plan, 880) == 2655
    assert _target_at(edit.plan, 890) == 2715
    assert _target_at(edit.plan, 900) == 2775


def test_manual_drag_anchor_allows_lowering_while_moving_right() -> None:
    first_edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2775,
    )
    second_edit = manual_drag_anchor_edit(
        first_edit.plan,
        anchor_voltage_mv=first_edit.anchor_voltage_mv,
        anchor_clock_mhz=first_edit.anchor_clock_mhz,
        requested_voltage_mv=930,
        requested_clock_mhz=2550,
    )

    assert second_edit.anchor_voltage_mv == 930
    assert second_edit.anchor_clock_mhz == 2550
    assert all(
        point["target_mhz"] <= 2550
        for point in second_edit.plan
        if point["voltage_mv"] < 930
    )
    assert all(
        point["target_mhz"] == 2550
        for point in second_edit.plan
        if point["voltage_mv"] >= 930
    )


def test_manual_drag_anchor_moving_right_keeps_clock_when_requested() -> None:
    first_edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2775,
    )
    second_edit = manual_drag_anchor_edit(
        first_edit.plan,
        anchor_voltage_mv=first_edit.anchor_voltage_mv,
        anchor_clock_mhz=first_edit.anchor_clock_mhz,
        requested_voltage_mv=930,
        requested_clock_mhz=first_edit.anchor_clock_mhz,
    )

    assert second_edit.anchor_voltage_mv == 930
    assert second_edit.anchor_clock_mhz == 2775
    assert _target_at(second_edit.plan, 890) == 2715
    assert _target_at(second_edit.plan, 900) == 2730
    assert _target_at(second_edit.plan, 910) == 2745
    assert _target_at(second_edit.plan, 920) == 2760
    assert all(
        point["target_mhz"] == 2775
        for point in second_edit.plan
        if point["voltage_mv"] >= 930
    )


def test_manual_drag_anchor_can_lower_current_flattened_baseline() -> None:
    flattened = manual_flatten_from_existing_point(
        _plan(),
        anchor_voltage_mv=900,
        clicked_voltage_mv=870,
    )
    lowered = manual_drag_anchor_edit(
        flattened.plan,
        anchor_voltage_mv=flattened.anchor_voltage_mv,
        anchor_clock_mhz=flattened.anchor_clock_mhz,
        requested_voltage_mv=870,
        requested_clock_mhz=2460,
        allow_lower_clock=True,
    )

    assert lowered.anchor_voltage_mv == 870
    assert lowered.anchor_clock_mhz == 2460
    assert _target_at(lowered.plan, 860) == 2460
    assert all(
        point["target_mhz"] == 2460
        for point in lowered.plan
        if point["voltage_mv"] >= 870
    )


def test_manual_drag_anchor_moving_right_interpolates_old_flattened_range() -> None:
    flattened = manual_flatten_from_existing_point(
        _plan(),
        anchor_voltage_mv=900,
        clicked_voltage_mv=870,
    )
    moved_right = manual_drag_anchor_edit(
        flattened.plan,
        anchor_voltage_mv=flattened.anchor_voltage_mv,
        anchor_clock_mhz=flattened.anchor_clock_mhz,
        requested_voltage_mv=900,
        requested_clock_mhz=2505,
        allow_lower_clock=True,
        preserve_clock_on_right=True,
    )

    assert moved_right.anchor_voltage_mv == 900
    assert moved_right.anchor_clock_mhz == 2505
    assert _target_at(moved_right.plan, 870) == 2490
    assert _target_at(moved_right.plan, 880) == 2490
    assert _target_at(moved_right.plan, 890) == 2505
    assert all(
        point["target_mhz"] == 2505
        for point in moved_right.plan
        if point["voltage_mv"] >= 900
    )


def test_manual_flatten_from_existing_point_uses_clicked_curve_bin() -> None:
    edit = manual_flatten_from_existing_point(
        _plan(),
        anchor_voltage_mv=900,
        clicked_voltage_mv=870,
    )

    assert edit.anchor_voltage_mv == 870
    assert edit.anchor_clock_mhz == 2505
    assert all(
        point["target_mhz"] == 2505
        for point in edit.plan
        if point["voltage_mv"] >= 870
    )
    assert edit.plan[0]["target_mhz"] == 2400


def test_manual_tune_single_left_point_updates_only_that_bin() -> None:
    edit = manual_tune_single_point_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        point_voltage_mv=880,
        requested_clock_mhz=2599,
    )

    assert edit.anchor_voltage_mv == 900
    assert edit.anchor_clock_mhz == 2550
    assert edit.selected_voltage_mv == 880
    assert edit.selected_clock_mhz == 2550
    assert _target_at(edit.plan, 870) == 2505
    assert _target_at(edit.plan, 880) == 2550
    assert _target_at(edit.plan, 890) == 2535


def test_manual_tune_single_left_point_snaps_clicked_voltage_and_clock() -> None:
    edit = manual_tune_single_point_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        point_voltage_mv=876,
        requested_clock_mhz=2532,
    )

    assert edit.selected_voltage_mv == 880
    assert edit.selected_clock_mhz == 2535
    assert _target_at(edit.plan, 880) == 2535


def test_manual_tune_single_left_point_can_move_to_another_voltage_bin() -> None:
    first_edit = manual_tune_single_point_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        point_voltage_mv=860,
        requested_clock_mhz=2460,
    )
    edit = manual_tune_single_point_edit(
        first_edit.plan,
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        point_voltage_mv=860,
        requested_voltage_mv=890,
        requested_clock_mhz=2530,
    )

    assert edit.anchor_voltage_mv == 900
    assert edit.anchor_clock_mhz == 2550
    assert edit.selected_voltage_mv == 890
    assert edit.selected_clock_mhz == 2535
    assert _target_at(edit.plan, 860) == 2490
    assert _target_at(edit.plan, 890) == 2535


def test_manual_tune_single_left_point_cannot_move_right_of_flat_anchor() -> None:
    edit = manual_tune_single_point_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        point_voltage_mv=860,
        requested_voltage_mv=940,
        requested_clock_mhz=2500,
    )

    assert edit.selected_voltage_mv == 890
    assert edit.selected_clock_mhz == 2505
    assert _target_at(edit.plan, 890) == 2505
    assert _target_at(edit.plan, 900) == 2550


def test_manual_tune_single_left_point_cannot_exceed_flat_clock() -> None:
    raised = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2775,
    )

    edit = manual_tune_single_point_edit(
        raised.plan,
        anchor_voltage_mv=raised.anchor_voltage_mv,
        anchor_clock_mhz=raised.anchor_clock_mhz,
        point_voltage_mv=880,
        requested_clock_mhz=3000,
    )

    assert edit.selected_voltage_mv == 880
    assert edit.selected_clock_mhz == 2775
    assert _target_at(edit.plan, 880) == 2775
    assert _target_at(edit.plan, 900) == 2775


def test_manual_nudge_selected_frequency_updates_flat_anchor() -> None:
    edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2775,
    )

    nudged = manual_nudge_selected_frequency(edit, direction=1)

    assert nudged.anchor_voltage_mv == 900
    assert nudged.anchor_clock_mhz == 2790
    assert nudged.selected_voltage_mv == 900
    assert nudged.selected_clock_mhz == 2790
    assert _target_at(nudged.plan, 890) == 2715
    assert _target_at(nudged.plan, 900) == 2790
    assert _target_at(nudged.plan, 1040) == 2790


def test_manual_nudge_selected_frequency_updates_single_left_point() -> None:
    edit = manual_tune_single_point_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        point_voltage_mv=880,
        requested_clock_mhz=2490,
    )

    nudged = manual_nudge_selected_frequency(edit, direction=1)

    assert nudged.anchor_voltage_mv == 900
    assert nudged.anchor_clock_mhz == 2550
    assert nudged.selected_voltage_mv == 880
    assert nudged.selected_clock_mhz == 2505
    assert _target_at(nudged.plan, 870) == 2505
    assert _target_at(nudged.plan, 880) == 2505
    assert _target_at(nudged.plan, 890) == 2535


def test_manual_nudge_selected_voltage_updates_flat_anchor() -> None:
    edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2775,
    )

    nudged = manual_nudge_selected_voltage(edit, direction=1)

    assert nudged.anchor_voltage_mv == 910
    assert nudged.anchor_clock_mhz == 2775
    assert nudged.selected_voltage_mv == 910
    assert nudged.selected_clock_mhz == 2775
    assert _target_at(nudged.plan, 900) == 2745
    assert _target_at(nudged.plan, 910) == 2775


def test_manual_nudge_selected_voltage_updates_single_left_point() -> None:
    edit = manual_tune_single_point_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        point_voltage_mv=870,
        requested_clock_mhz=2490,
    )

    nudged = manual_nudge_selected_voltage(edit, direction=1)

    assert nudged.anchor_voltage_mv == 900
    assert nudged.anchor_clock_mhz == 2550
    assert nudged.selected_voltage_mv == 880
    assert nudged.selected_clock_mhz == 2490
    assert _target_at(nudged.plan, 870) == 2490
    assert _target_at(nudged.plan, 880) == 2490
    assert _target_at(nudged.plan, 900) == 2550


def test_manual_nudge_selected_voltage_left_interpolates_without_low_kink() -> None:
    edit = manual_tune_single_point_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        point_voltage_mv=880,
        requested_clock_mhz=2550,
    )

    nudged = manual_nudge_selected_voltage(edit, direction=-1)

    assert nudged.selected_voltage_mv == 870
    assert nudged.selected_clock_mhz == 2550
    assert _target_at(nudged.plan, 870) == 2550
    assert _target_at(nudged.plan, 880) == 2550
    assert _target_at(nudged.plan, 890) == 2535


def test_manual_select_curve_point_clamps_flat_tail_to_anchor() -> None:
    edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2775,
    )

    selected = manual_select_curve_point(edit, voltage_mv=980)

    assert selected.anchor_voltage_mv == 900
    assert selected.anchor_clock_mhz == 2775
    assert selected.selected_voltage_mv == 900
    assert selected.selected_clock_mhz == 2775


def test_manual_select_adjacent_point_tabs_right_and_left() -> None:
    base_edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2550,
    )
    edit = manual_select_curve_point(base_edit, voltage_mv=880)

    right = manual_select_adjacent_point(edit, direction=1)
    left = manual_select_adjacent_point(right, direction=-1)

    assert right.selected_voltage_mv == 890
    assert right.selected_clock_mhz == 2535
    assert left.selected_voltage_mv == 880
    assert left.selected_clock_mhz == 2520


def test_manual_select_range_to_right_offsets_points_through_flat_tail() -> None:
    base_edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2550,
    )
    selected = manual_select_curve_point(base_edit, voltage_mv=870)
    ranged = manual_select_range_to_right(selected)

    nudged = manual_nudge_selected_frequency(ranged, direction=1)

    assert nudged.selected_range_start_voltage_mv == 870
    assert nudged.selected_voltage_mv == 870
    assert nudged.selected_clock_mhz == 2520
    assert nudged.anchor_voltage_mv == 900
    assert nudged.anchor_clock_mhz == 2565
    assert _target_at(nudged.plan, 860) == 2490
    assert _target_at(nudged.plan, 870) == 2520
    assert _target_at(nudged.plan, 880) == 2535
    assert _target_at(nudged.plan, 890) == 2550
    assert _target_at(nudged.plan, 900) == 2565
    assert _target_at(nudged.plan, 1040) == 2565


def test_manual_drag_selected_range_offsets_from_dragged_reference_point() -> None:
    base_edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2550,
    )
    selected = manual_select_curve_point(base_edit, voltage_mv=870)
    ranged = manual_select_range_to_right(selected)

    dragged = manual_offset_selected_range(
        ranged,
        reference_voltage_mv=880,
        requested_clock_mhz=2580,
    )

    assert dragged.selected_range_start_voltage_mv == 870
    assert dragged.selected_voltage_mv == 870
    assert dragged.selected_clock_mhz == 2565
    assert dragged.anchor_clock_mhz == 2610
    assert _target_at(dragged.plan, 860) == 2490
    assert _target_at(dragged.plan, 870) == 2565
    assert _target_at(dragged.plan, 880) == 2580
    assert _target_at(dragged.plan, 890) == 2595
    assert _target_at(dragged.plan, 900) == 2610
    assert _target_at(dragged.plan, 1040) == 2610


def test_manual_select_single_point_clears_range_selection() -> None:
    base_edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=900,
        requested_clock_mhz=2550,
    )
    ranged = manual_select_range_to_right(
        manual_select_curve_point(base_edit, voltage_mv=870)
    )

    selected = manual_select_curve_point(ranged, voltage_mv=880)

    assert selected.selected_voltage_mv == 880
    assert selected.selected_range_start_voltage_mv is None


def test_user_edited_profile_payload_requires_verification() -> None:
    edit = manual_drag_anchor_edit(
        _plan(),
        anchor_voltage_mv=900,
        anchor_clock_mhz=2550,
        requested_voltage_mv=930,
        requested_clock_mhz=2600,
    )
    payload = user_edited_profile_payload(
        {
            "profile_id": "parent",
            "path": "/tmp/parent.json",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2550,
            "avg_fps": 120.0,
            "final_verified": True,
        },
        edit,
        original_anchor_voltage_mv=900,
        original_anchor_clock_mhz=2550,
    )

    assert payload["profile_source"] == "user-edited"
    assert payload["final_verified"] is False
    assert payload["requires_verification"] is True
    assert payload["candidate_voltage_mv"] == 930
    assert payload["lock_clock_mhz"] == 2595
    assert "avg_fps" not in payload
    assert payload["manual_edit"]["parent_profile_id"] == "parent"


def _target_at(plan: list[dict], voltage_mv: int) -> int:
    return next(
        int(point["target_mhz"])
        for point in plan
        if int(point["voltage_mv"]) == int(voltage_mv)
    )
