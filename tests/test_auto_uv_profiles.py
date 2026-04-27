from __future__ import annotations

import json
import os

import pytest

import auto_uv.profiles as profile_store
import penguin_burner_ui.app as ui_app
from auto_uv.profiles import (
    archive_auto_uv_profile,
    delete_auto_uv_profile_paths,
    delete_auto_uv_profiles,
    format_profile_table,
    profile_display_name,
    profile_summary,
    read_auto_uv_profile_summaries,
)
from penguin_burner_ui.app import (
    AFTERBURNER_PROFILE_ID,
    _candidate_id_from_result,
    _candidate_number,
    _candidate_status_text,
    _duration_minutes_for_control,
    _error_dialog_copy_text,
    _event_base_points,
    _format_duration_for_user,
    _final_profile_notice_text,
    _lact_export_output_path,
    _lact_gpu_id_from_config,
    _load_cached_base_curve_points,
    _profile_base_curve_points,
    _profile_curve_points,
    _profile_curve_tab_label,
    _profile_delete_confirmation_text,
    _profile_fan_curve_points,
    _profile_fan_curve_tab_label,
    _profile_fan_curve_target_point,
    _profile_fan_measurement_points,
    _profile_info_from_command_text,
    _profile_is_deletable,
    _process_failure_details,
    _reverify_elapsed_from_line,
    _reverify_progress_percent,
    _runtime_action_dialog_label,
    _runner_status_text,
    _save_cached_base_curve_points,
    _selected_profile_ids_include_selector,
    _fan_measurement_point,
    _fan_measurement_points,
    _stage_title,
    _sorted_unique_fan_points,
    _status_value,
    _top_status_text,
)
from penguin_burner_ui.components.profile_list import (
    PROFILE_SORTABLE_COLUMNS,
    ProfileList,
    _format_number,
    _format_profile_metric_delta,
    _format_profile_metric_with_delta,
    _metric_delta_percent,
    _profile_base_metric,
    _profile_metric_delta_color,
    _profile_sort_values,
    _profile_source_label,
    _promote_preferred_profile,
    _should_preserve_persist_toggle,
    _should_preserve_selection,
    _sort_value_less,
)


def test_profile_display_name_uses_clock_then_voltage() -> None:
    profile = {
        "profile_id": "20260427-120000-000000-875mv-2610mhz",
        "candidate_voltage_mv": 875,
        "lock_clock_mhz": 2610,
    }

    assert profile_display_name(profile) == "2610 MHz 875 mV"


def test_profile_table_keeps_date_separate_from_profile_name() -> None:
    profile = {
        "profile_id": "20260427-120000-000000-875mv-2610mhz",
        "profile_created_at": "2026-04-27T12:00:00+02:00",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
            "memory_offset_mhz": 500,
            "avg_core_clock_mhz": 2605.25,
            "efficiency_fps_per_w": 0.81234,
            "profile_source": "profile-store",
    }

    rendered = format_profile_table([profile])

    assert "2026-04-27 12:00:00" in rendered
    assert "2610 MHz 875 mV" in rendered
    assert "+500" in rendered
    assert "20260427-120000-000000-875mv-2610mhz" not in rendered


def test_profile_summary_keeps_base_metrics_for_profile_table_delta() -> None:
    summary = profile_summary(
        {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
            "memory_offset_mhz": 750,
            "avg_core_clock_mhz": 2605.0,
            "avg_power_w": 240.0,
            "efficiency_fps_per_w": 0.70,
            "base_candidate_voltage_mv": 1000,
            "base_lock_clock_mhz": 2700,
            "base_avg_core_clock_mhz": 2650.0,
            "base_avg_fps": 150.0,
            "base_avg_power_w": 300.0,
            "base_efficiency_fps_per_w": 0.50,
            "final_verified": True,
        }
    )

    assert summary["base_candidate_voltage_mv"] == 1000
    assert summary["base_lock_clock_mhz"] == 2700
    assert summary["memory_offset_mhz"] == 750
    assert summary["base_avg_core_clock_mhz"] == 2650.0
    assert summary["base_avg_fps"] == 150.0
    assert summary["base_avg_power_w"] == 300.0
    assert summary["base_efficiency_fps_per_w"] == 0.50


def test_profile_metric_delta_text_and_color_vs_base() -> None:
    assert _metric_delta_percent(0.75, 0.50) == 50.0
    assert _format_profile_metric_with_delta(0.75, 0.50, precision=2) == (
        "0.75 (+50.00%)"
    )
    assert _profile_metric_delta_color(0.75, 0.50) == "#55d27a"

    assert _format_profile_metric_with_delta(
        875,
        1000,
        precision=0,
        lower_is_better=True,
    ) == "875 (-12.50%)"
    assert _format_profile_metric_with_delta(
        2600.0,
        2650.0,
        precision=2,
    ) == "2600.00 (-1.89%)"
    assert _format_profile_metric_with_delta(
        240.0,
        300.0,
        precision=2,
        lower_is_better=True,
    ) == "240.00 (-20.00%)"
    assert (
        _profile_metric_delta_color(240.0, 300.0, lower_is_better=True)
        == "#55d27a"
    )
    assert (
        _profile_metric_delta_color(330.0, 300.0, lower_is_better=True)
        == "#ff6b6b"
    )


def test_profile_table_headers_and_sorting_scope() -> None:
    assert ProfileList.COLUMNS[2] == "Voltage mV"
    assert ProfileList.COLUMNS[3] == "Voltage vs base"
    assert ProfileList.COLUMNS[5] == "Memory Offset MHz"
    assert ProfileList.COLUMNS[9] == "FPS/W vs base"
    assert ProfileList.COLUMNS[13] == "Power vs base"
    assert ProfileList.COLUMNS[15] == "Autostart"
    assert PROFILE_SORTABLE_COLUMNS == frozenset(
        {0, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13}
    )


def test_profile_non_sort_columns_have_no_sort_keys() -> None:
    sort_values = _profile_sort_values(
        {
            "profile_created_at": "2026-04-27T12:00:00+02:00",
            "display_name": "2610 MHz 875 mV",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
            "avg_core_clock_mhz": 2605.25,
            "efficiency_fps_per_w": 0.80,
            "base_efficiency_fps_per_w": 0.50,
            "avg_fps": 160.0,
            "base_avg_fps": 150.0,
            "avg_power_w": 200.0,
            "base_avg_power_w": 250.0,
            "profile_source": "auto-uv-final",
        }
    )

    assert sort_values[1] == ""
    assert sort_values[5] == ""
    assert sort_values[9] == pytest.approx(60.0)
    assert sort_values[11] == pytest.approx(6.6666666667)
    assert sort_values[13] == -20.0
    assert sort_values[14] == ""
    assert sort_values[15] == ""


def test_profile_metric_delta_text_is_separate_from_absolute_value() -> None:
    assert _format_profile_metric_delta(0.75, 0.50) == "+50.00%"
    assert _format_profile_metric_delta(0.50, 0.50) == "ref"
    assert _format_profile_metric_delta(0.45, 0.50) == "-10.00%"
    assert _format_profile_metric_delta(
        240.0,
        300.0,
        lower_is_better=True,
    ) == "-20.00%"


def test_profile_table_keeps_regular_font_for_highlight_and_deltas() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    header = profile_list.table.horizontalHeader()
    assert not header.highlightSections()
    assert not header.font().bold()
    profile_list.set_profiles(
        [
            {
                "profile_id": "profile-a",
                "candidate_id": "875mv-2610mhz",
                "profile_created_at": "2026-04-27T12:00:00+02:00",
                "candidate_voltage_mv": 875,
                "base_candidate_voltage_mv": 1000,
                "lock_clock_mhz": 2610,
                "avg_core_clock_mhz": 2605.25,
                "base_avg_core_clock_mhz": 2650.0,
                "efficiency_fps_per_w": 0.80,
                "base_efficiency_fps_per_w": 0.50,
                "avg_fps": 160.0,
                "base_avg_fps": 150.0,
                "avg_power_w": 200.0,
                "base_avg_power_w": 250.0,
            }
        ],
        preferred_candidate_id="875mv-2610mhz",
        select_preferred=True,
    )

    for column in range(profile_list.table.columnCount()):
        item = profile_list.table.item(0, column)
        assert item is not None
        assert not item.font().bold()


def test_profile_table_defaults_to_newest_date_first() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profile_list.set_profiles(
        [
            {
                "profile_id": "old",
                "display_name": "Old",
                "profile_created_at": "2026-04-25T12:00:00+02:00",
            },
            {
                "profile_id": "new",
                "display_name": "New",
                "profile_created_at": "2026-04-27T12:00:00+02:00",
            },
            {
                "profile_id": "middle",
                "display_name": "Middle",
                "profile_created_at": "2026-04-26T12:00:00+02:00",
            },
        ],
    )

    assert profile_list.table.horizontalHeader().sortIndicatorSection() == 0
    assert (
        profile_list.table.horizontalHeader().sortIndicatorOrder()
        == QtCore.Qt.DescendingOrder
    )
    assert [
        profile_list.table.item(row, profile_list.PROFILE_COLUMN).text()
        for row in range(profile_list.table.rowCount())
    ] == ["New", "Middle", "Old"]


def test_process_error_details_are_copy_friendly() -> None:
    details = _process_failure_details(
        action_label="Auto-UV process",
        exit_code=1,
        exit_status="CrashExit",
        extra_details="Auto-UV exited without reporting a final result.",
        log_tail="[2026-04-27 16:30:06] unexpected traceback",
    )
    copy_text = _error_dialog_copy_text(
        "Auto-UV failed",
        "Auto-UV process stopped unexpectedly.",
        details=details,
    )

    assert "Auto-UV failed" in copy_text
    assert "Auto-UV process stopped unexpectedly." in copy_text
    assert "Action: Auto-UV process" in copy_text
    assert "Exit code: 1" in copy_text
    assert "Exit status: CrashExit" in copy_text
    assert "without reporting a final result" in copy_text
    assert "Recent logs:" in copy_text
    assert "unexpected traceback" in copy_text


def test_runtime_action_error_labels_use_autostart_language() -> None:
    assert _runtime_action_dialog_label("daemonize") == "Apply selected profile"
    assert (
        _runtime_action_dialog_label("install-systemd")
        == "Apply selected profile with autostart"
    )
    assert _runtime_action_dialog_label("uninstall-systemd") == "Remove autostart entry"


def test_profile_source_label_uses_user_facing_auto_uv_name() -> None:
    assert _profile_source_label({"profile_source": "auto-uv-final"}) == "Auto UV"
    assert _profile_source_label({"profile_source": "afterburner"}) == "afterburner"


def test_profile_sort_keeps_empty_metrics_at_bottom() -> None:
    assert _sort_value_less(1.0, "")
    assert not _sort_value_less("", 1.0)
    assert _sort_value_less("", 1.0, descending=True)
    assert not _sort_value_less(1.0, "", descending=True)


def test_profile_base_metric_reads_saved_base_fields() -> None:
    profile = {
        "base_candidate_voltage_mv": 1000,
        "base_avg_core_clock_mhz": 2650.0,
        "base_efficiency_fps_per_w": 0.50,
        "base_avg_power_w": 300.0,
    }

    assert _profile_base_metric(profile, "candidate_voltage_mv") == 1000
    assert _profile_base_metric(profile, "avg_core_clock_mhz") == 2650.0
    assert (
        _profile_base_metric(profile, "efficiency_fps_per_w")
        == 0.50
    )
    assert _profile_base_metric(profile, "avg_power_w") == 300.0


def test_profile_store_keeps_multiple_final_verified_profiles(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "avg_core_clock_mhz": 2595.0,
            "efficiency_fps_per_w": 0.70,
            "final_verified": True,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        }
    )
    archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2580,
            "avg_core_clock_mhz": 2575.0,
            "efficiency_fps_per_w": 0.78,
            "final_verified": True,
            "points": [{"voltage_mv": 875, "target_mhz": 2580}],
        }
    )

    summaries = read_auto_uv_profile_summaries()

    assert len(summaries) == 2
    assert {summary["candidate_id"] for summary in summaries} == {
        "900mv-2600mhz",
        "875mv-2580mhz",
    }


def test_profile_store_ignores_short_verified_candidates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "avg_core_clock_mhz": 2595.0,
            "profile_source": "verified-candidate",
            "final_verified": False,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        }
    )

    assert read_auto_uv_profile_summaries() == []


def test_profile_summary_uses_real_file_path_not_payload_path(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2580,
            "points": [{"voltage_mv": 875, "target_mhz": 2580}],
            "final_verified": True,
            "path": str(tmp_path / "stale.json"),
        }
    )

    summaries = read_auto_uv_profile_summaries()

    assert summaries[0]["path"] == str(stored_path)


def test_delete_auto_uv_profiles_removes_only_profile_store_files(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2580,
            "points": [{"voltage_mv": 875, "target_mhz": 2580}],
            "final_verified": True,
        }
    )
    active_path = tmp_path / "auto-uv-final-curve.json"
    active_path.write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 900,
                "lock_clock_mhz": 2600,
                "final_verified": True,
                "points": [{"voltage_mv": 900, "target_mhz": 2600}],
            }
        ),
        encoding="utf-8",
    )
    outside_path = tmp_path / "outside-profile.json"
    outside_path.write_text("{}", encoding="utf-8")

    deleted = delete_auto_uv_profile_paths([stored_path, active_path, outside_path])

    assert {path.name for path in deleted} == {stored_path.name}
    assert not stored_path.exists()
    assert active_path.exists()
    assert outside_path.exists()


def test_delete_auto_uv_profiles_accepts_profile_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "profile_id": "profile-a",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2580,
            "final_verified": True,
            "points": [{"voltage_mv": 875, "target_mhz": 2580}],
        }
    )

    deleted = delete_auto_uv_profiles(["profile-a"])

    assert deleted == [stored_path.resolve()]
    assert read_auto_uv_profile_summaries() == []


def test_profile_list_ignores_legacy_saved_uv_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    legacy_dir = tmp_path / "saved-uv"
    legacy_dir.mkdir()
    (legacy_dir / "auto-uv-best-undervolt-old.json").write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 865,
                "lock_clock_mhz": 2610,
                "points": [{"voltage_mv": 865, "target_mhz": 2610}],
            }
        ),
        encoding="utf-8",
    )

    assert read_auto_uv_profile_summaries() == []


def test_profile_list_ignores_legacy_active_final_curve_file(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    (tmp_path / "auto-uv-final-curve.json").write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 865,
                "lock_clock_mhz": 2610,
                "final_verified": True,
                "points": [{"voltage_mv": 865, "target_mhz": 2610}],
            }
        ),
        encoding="utf-8",
    )

    assert read_auto_uv_profile_summaries() == []


def test_promote_preferred_profile_moves_one_matching_candidate_to_top() -> None:
    profiles = [
        {"profile_id": "old", "candidate_id": "900mv-2500mhz"},
        {"profile_id": "chosen", "candidate_id": "875mv-2610mhz"},
        {"profile_id": "duplicate", "candidate_id": "875mv-2610mhz"},
    ]

    promoted = _promote_preferred_profile(
        profiles,
        preferred_candidate_id="875mv-2610mhz",
    )

    assert [profile["profile_id"] for profile in promoted] == [
        "chosen",
        "old",
        "duplicate",
    ]


def test_profile_refresh_preserves_user_selection_over_preferred_profile() -> None:
    assert _should_preserve_selection(
        ["profile-a", "profile-b", "profile-c"],
        preferred_profile_id="profile-a",
    )
    assert _should_preserve_selection(
        ["profile-a"],
        preferred_profile_id="profile-b",
    )
    assert not _should_preserve_selection(
        [],
        preferred_profile_id="profile-b",
    )


def test_profile_refresh_preserves_persist_toggle_for_same_single_selection() -> None:
    assert _should_preserve_persist_toggle(["profile-a"], ["profile-a"])
    assert not _should_preserve_persist_toggle(["profile-a"], ["profile-b"])
    assert not _should_preserve_persist_toggle(
        ["profile-a", "profile-b"],
        ["profile-a", "profile-b"],
    )
    assert not _should_preserve_persist_toggle([], [])


def test_selected_profile_ids_include_persisted_selector() -> None:
    profiles = [
        {"profile_id": "profile-a", "candidate_id": "875mv-2610mhz"},
        {"profile_id": "profile-b", "candidate_id": "865mv-2625mhz"},
    ]

    assert _selected_profile_ids_include_selector(
        profiles,
        ["profile-b"],
        "865mv-2625mhz",
    )
    assert _selected_profile_ids_include_selector(
        profiles,
        ["profile-a"],
        "latest",
    )
    assert not _selected_profile_ids_include_selector(
        profiles,
        ["profile-b"],
        "latest",
    )


def test_profile_delete_confirmation_warns_when_systemd_entry_is_removed() -> None:
    message = _profile_delete_confirmation_text(
        ["2625 MHz 865 mV"],
        removes_systemd=True,
    )

    assert "Delete Auto-UV profile 2625 MHz 865 mV?" in message
    assert "currently persisted on startup" in message
    assert "remove the Systemd autostart entry" in message


def test_afterburner_profile_is_deletable_without_profile_path() -> None:
    assert _profile_is_deletable(
        {
            "profile_id": AFTERBURNER_PROFILE_ID,
            "runtime_source": "afterburner",
            "path": "",
        }
    )


def test_lact_export_output_uses_lact_config_filename(tmp_path) -> None:
    assert _lact_export_output_path(tmp_path) == tmp_path / "config.yaml"


def test_lact_gpu_id_parser_reads_first_gpu_key(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "daemon:",
                "  log_level: info",
                "gpus:",
                "  10DE:2C02-10DE:2095-0000:2b:00.0:",
                "    fan_control_enabled: false",
                "profiles: {}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert _lact_gpu_id_from_config(path) == "10DE:2C02-10DE:2095-0000:2b:00.0"


def test_profile_delete_confirmation_describes_afterburner_config_entry() -> None:
    message = _profile_delete_confirmation_text(
        ["MSI Afterburner Profile1"],
        includes_afterburner=True,
    )

    assert "Delete profile MSI Afterburner Profile1?" in message
    assert "Afterburner import entries are removed" in message


def test_reverify_progress_parses_stability_live_elapsed() -> None:
    assert (
        _reverify_elapsed_from_line(
            "Stability live: demo=q2demo1 elapsed=123.4s power=250.0W"
        )
        == 123.4
    )
    assert _reverify_elapsed_from_line("Stability test: PASS") is None
    assert _reverify_progress_percent(150, 600) == 25
    assert _reverify_progress_percent(700, 600) == 100


def test_runner_status_text_shows_running_profile_and_autostart_state() -> None:
    profiles = [
        {
            "profile_id": "profile-a",
            "candidate_id": "875mv-2610mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
        }
    ]

    status = _runner_status_text(
        profiles,
        running_selector="profile-a",
        autostart_selector="profile-a",
        running_silent_fan=True,
        autostart_silent_fan=True,
    )

    assert "Currently running profile: 2610 MHz 875 mV" in status
    assert "Systemd autostart: Yes" in status
    assert "Silent fan curve: On" in status


def test_runner_status_text_has_clear_empty_state() -> None:
    assert (
        _runner_status_text([], running_selector="", autostart_selector="")
        == "No running/autostart profile available yet."
    )


def test_final_profile_notice_names_saved_profile_and_profiles_tab() -> None:
    profiles = [
        {
            "profile_id": "profile-a",
            "candidate_id": "865mv-2625mhz",
            "candidate_voltage_mv": 865,
            "lock_clock_mhz": 2625,
        }
    ]

    assert (
        _final_profile_notice_text(
            profiles,
            profile_id="profile-a",
            candidate_id="865mv-2625mhz",
            result_payload={"voltage_mv": 865, "clock_mhz": 2625},
        )
        == "Final verification complete. Profile 2625 MHz 865 mV is saved and "
        "highlighted in Profiles."
    )


def test_profile_curve_points_use_embedded_afterburner_points() -> None:
    profile = {
        "profile_id": AFTERBURNER_PROFILE_ID,
        "display_name": "MSI Afterburner Profile1 2100 MHz 900 mV",
        "curve_points": [[900, 2100], [925, 2115]],
    }

    assert _profile_curve_points(profile) == [(900.0, 2100.0), (925.0, 2115.0)]
    assert _profile_curve_tab_label(profile) == (
        "MSI Afterburner Profile1 2100 MHz 900 mV"
    )


def test_profile_curve_points_read_saved_auto_uv_profile_path(tmp_path) -> None:
    profile_path = tmp_path / "auto-uv-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 875,
                "lock_clock_mhz": 2610,
                "points": [
                    {"voltage_mv": 875, "target_mhz": 2610},
                    {"voltage_mv": 900, "target_mhz": 2625},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert _profile_curve_points({"path": str(profile_path)}) == [
        (875.0, 2610.0),
        (900.0, 2625.0),
    ]


def test_profile_base_curve_points_read_saved_auto_uv_profile_path(tmp_path) -> None:
    profile_path = tmp_path / "auto-uv-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "points": [
                    {"voltage_mv": 875, "base_mhz": 2550, "target_mhz": 2610},
                    {"voltage_mv": 900, "base_mhz": 2580, "target_mhz": 2625},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert _profile_base_curve_points({"path": str(profile_path)}) == [
        (875.0, 2550.0),
        (900.0, 2580.0),
    ]


def test_profile_fan_curve_points_use_embedded_auto_uv_payload() -> None:
    profile = {
        "display_name": "2715 MHz 850 mV",
        "fan_curve_payload": {
            "fan": {"curve": [[45.0, 0.0], [60.0, 30.0], [75.0, 42.7]]},
            "telemetry": {
                "measured_fan_points": [
                    {
                        "temperature_c": 58.0,
                        "fan_speed_pct": 34.0,
                        "voltage_mv": 850,
                        "clock_mhz": 2715,
                    }
                ]
            },
            "load_anchor_temperature_c": 75.0,
            "load_anchor_fan_speed_pct": 42.7,
        },
    }

    assert _profile_fan_curve_points(profile) == [
        (45.0, 0.0),
        (60.0, 30.0),
        (75.0, 42.7),
    ]
    assert _profile_fan_measurement_points(profile) == [(58.0, 34.0)]
    assert _profile_fan_curve_target_point(profile) == (75.0, 42.7)
    assert _profile_fan_curve_tab_label(profile) == "2715 MHz 850 mV Fan Curve"


def test_profile_fan_curve_points_fall_back_to_matching_current_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ui_app, "default_user_config_dir", lambda: tmp_path)
    (tmp_path / "auto-uv-fan-curve.json").write_text(
        json.dumps(
            {
                "fan": {"curve": [[45.0, 0.0], [70.0, 40.0]]},
                "telemetry": {
                    "measured_fan_points": [
                        {
                            "temperature_c": 66.0,
                            "fan_speed_pct": 35.0,
                            "voltage_mv": 850,
                            "clock_mhz": 2715,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    matching_profile = {"candidate_voltage_mv": 850, "lock_clock_mhz": 2715}
    other_profile = {"candidate_voltage_mv": 875, "lock_clock_mhz": 2715}

    assert _profile_fan_curve_points(matching_profile) == [
        (45.0, 0.0),
        (70.0, 40.0),
    ]
    assert _profile_fan_measurement_points(matching_profile) == [(66.0, 35.0)]
    assert _profile_fan_curve_points(other_profile) == []


def test_event_base_points_prefer_base_clock_over_target_clock() -> None:
    assert _event_base_points(
        {
            "points": [
                {"voltage_mv": 875, "base_mhz": 2550, "clock_mhz": 2610},
                {"voltage_mv": 900, "base_mhz": 2580, "clock_mhz": 2625},
            ]
        }
    ) == [(875.0, 2550.0), (900.0, 2580.0)]


def test_cached_base_curve_points_roundtrip_for_current_gpu(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ui_app, "default_user_config_dir", lambda: tmp_path)
    monkeypatch.setattr(ui_app, "_runtime_gpu_index", lambda _path: 0)

    _save_cached_base_curve_points([(900, 2100), (925, 2115)])

    assert _load_cached_base_curve_points() == [
        (900.0, 2100.0),
        (925.0, 2115.0),
    ]


def test_profile_info_from_command_text_parses_selector_and_silent_fan_flag() -> None:
    info = _profile_info_from_command_text(
        "/usr/bin/bash /opt/penguin_burner.sh --foreground "
        "--auto-uv-profile profile-a --silent-fan-curve",
        default_if_present=True,
    )

    assert info == {"selector": "profile-a", "silent_fan_curve": True}


def test_profile_info_from_command_text_parses_afterburner_preference() -> None:
    info = _profile_info_from_command_text(
        "/usr/bin/bash /opt/penguin_burner.sh --foreground "
        "--prefer-afterburner-curve --silent-fan-curve",
        default_if_present=True,
    )

    assert info == {
        "selector": AFTERBURNER_PROFILE_ID,
        "silent_fan_curve": True,
    }


def test_candidate_id_from_final_result_uses_voltage_and_clock() -> None:
    assert (
        _candidate_id_from_result({"voltage_mv": 875.0, "clock_mhz": 2610.0})
        == "875mv-2610mhz"
    )


def test_final_choice_candidate_table_helpers_show_metrics_and_default() -> None:
    candidate = {
        "candidate_voltage_mv": 865,
        "lock_clock_mhz": 2625,
        "efficiency_fps_per_w": 0.73123,
    }

    assert _candidate_number(candidate["candidate_voltage_mv"], precision=0) == "865"
    assert _candidate_number(candidate["efficiency_fps_per_w"], precision=4) == "0.73"
    assert _format_number(candidate["efficiency_fps_per_w"], precision=4) == "0.73"
    assert _candidate_status_text(candidate, True) == "Best FPS/W | Passed short probe"


def test_top_status_text_rounds_gui_decimals_to_two_places() -> None:
    assert _status_value(2625.12345) == "2625.12"
    assert _status_value(865.0) == "865"
    assert (
        _top_status_text(
            "candidate 865.0000mV measured=2625.123456MHz fps=178.98765"
        )
        == "candidate 865.00mV measured=2625.12MHz fps=178.99"
    )


def test_final_verification_duration_control_uses_minutes() -> None:
    assert _format_duration_for_user(600) == "10 min"
    assert _format_duration_for_user(90) == "1 min 30 sec"
    assert _duration_minutes_for_control(90) == 2
    assert _duration_minutes_for_control(3600) == 60


def test_stage_title_simplifies_base_baseline() -> None:
    assert _stage_title("base-baseline") == "Baseline"
    assert _stage_title("stock-baseline") == "Baseline"


def test_fan_measurement_helpers_read_probe_result_payloads() -> None:
    assert _fan_measurement_point({"temp_c": 62.4, "fan_pct": 34.2}) == (
        62.4,
        34.2,
    )
    assert _fan_measurement_point({"temp_c": 62.4, "fan_pct": None}) is None
    assert _fan_measurement_points(
        [
            {"temperature_c": 63.1, "fan_speed_pct": 35.0},
            [64.0, 36.0],
            {"temperature_c": 65.0, "fan_speed_pct": 110.0},
        ]
    ) == [(63.1, 35.0), (64.0, 36.0)]


def test_fan_measurement_points_are_sorted_and_deduplicated_for_plotting() -> None:
    points = _sorted_unique_fan_points(
        [(64.0, 35.0), (62.0, 34.0), (64.0, 35.0), (63.0, 34.5)]
    )

    assert points == [(62.0, 34.0), (63.0, 34.5), (64.0, 35.0)]
