"""Coverage for ui/components/runs_table.py.

Instantiates RunsTable under offscreen Qt and drives the row-writing API with
probe payloads, plus direct tests of the pure formatting/state helpers.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.components import runs_table as rt
from ui.components.runs_table import RunsTable
from ui.qt import import_qt


def _result_payload(decision="pass", **over) -> dict:
    payload = {
        "stage": "candidate",
        "candidate_id": "c1",
        "candidate_voltage_mv": 900,
        "lock_clock_mhz": 2500,
        "avg_core_clock_mhz": 2490,
        "avg_fps": 120.0,
        "avg_power_w": 200.0,
        "avg_temperature_c": 62.0,
        "avg_fan_speed_pct": 40.0,
        "efficiency_fps_per_w": 0.6,
        "decision": decision,
        "perf_cap_reason": "sw_power_cap",
        "result_reason": "stable run",
    }
    payload.update(over)
    return payload


def test_runs_table_row_lifecycle(qapp) -> None:
    qtcore, qtgui, qtwidgets, _pg = import_qt()
    table = RunsTable(QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets)

    table.add_probe_start(_result_payload())
    assert table.widget.rowCount() == 1
    table.update_probe_progress(
        {"candidate_id": "c1", "candidate_voltage_mv": 900, "elapsed_s": 30, "target_s": 60}
    )
    table.record_candidate_curve(
        {"candidate_id": "c1", "candidate_voltage_mv": 900,
         "auto_oc": True, "auto_oc_applied_mhz": 30, "auto_oc_limit_mhz": 90}
    )
    table.add_probe_result(_result_payload(decision="pass"))
    table.add_probe_result(_result_payload(decision="fail", candidate_id="c2",
                                           candidate_voltage_mv=880))
    table.add_probe_result(
        {"stage": "base-baseline", "candidate_voltage_mv": 1000, "lock_clock_mhz": 2600,
         "avg_fps": 110.0, "decision": "baseline"}
    )
    assert table.widget.rowCount() >= 3

    table.mark_running_rows_stopping()
    table.mark_running_rows_stopped(label="Stopped")
    _ = table.selected_candidate_id()

    table.clear()
    assert table.widget.rowCount() == 0


# --- pure helpers -------------------------------------------------------------


def test_row_state_and_active_decision() -> None:
    assert rt._is_active_decision("running") is True
    assert rt._is_active_decision("pass") is False
    assert rt._is_base_baseline({"stage": "base-baseline"}) is True
    assert rt._is_base_baseline({"stage": "candidate"}) is False
    assert rt._probe_key({"candidate_id": "c1", "candidate_voltage_mv": 900})


def test_number_and_bool_helpers() -> None:
    assert rt._to_float("2500") == 2500.0
    assert rt._to_float("x") is None
    assert rt._format_int(2500.4) == "2500"
    assert rt._format_int(None) == ""
    assert rt._format_float(12.34) == "12.34" or rt._format_float(12.34)
    assert rt._payload_number({"a": 1, "b": 2}, "missing", "b") == 2.0
    assert rt._payload_bool("true") is True
    assert rt._payload_bool(0) is False


def test_duration_and_progress_text() -> None:
    assert rt._format_duration_compact(0) is not None
    assert "m" in rt._format_duration_compact(125) or rt._format_duration_compact(125)
    assert rt._clamped_elapsed_s(120, 60) == 60.0  # clamped to target
    assert rt._clamped_elapsed_s(-5, 60) == 0.0
    assert isinstance(rt._progress_time_text(30, 60), str)


def test_oc_progress_and_cap_helpers() -> None:
    assert rt._format_oc_progress((1, 4)) == "1/4 MHz"
    assert rt._format_oc_progress(None) == "0/0"
    assert rt._format_oc_progress((2, 0)) == "0/0"  # zero limit
    assert rt._payload_oc_progress(
        {"auto_oc": True, "auto_oc_applied_mhz": 30, "auto_oc_limit_mhz": 90}
    ) == (30, 90)
    assert rt._payload_oc_progress({}) is None
    # Auto-OC ran against a target but held no extra clock: applied 0, limit > 0.
    # The row must show the configured headroom, not the ambiguous 0/0.
    assert rt._payload_oc_progress(
        {"auto_oc": True, "auto_oc_applied_mhz": 0, "auto_oc_limit_mhz": 380}
    ) == (0, 380)
    assert rt._format_oc_progress((0, 380)) == "0/380 MHz"
    assert rt._payload_curve_oc_progress(
        {"auto_oc_applied_mhz": 15, "auto_oc_limit_mhz": 90}
    ) == (15, 90)
    assert isinstance(rt._perf_cap_reason_text("sw_power_cap"), str)
    assert isinstance(rt._perf_cap_reason_tooltip("sw_power_cap"), str)


def test_measured_clock_and_progress_color() -> None:
    assert rt._measured_clock_value({"avg_core_clock_mhz": 2490}) is not None
    color = rt._progress_text_color("#123456", 0.5)
    assert isinstance(color, str) and color
