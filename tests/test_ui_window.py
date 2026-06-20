"""Coverage for ui/window.py (MainWindow) and ui/main.py launch-option parsing.

MainWindow is built under offscreen Qt with its profile-store/systemd
dependencies monkeypatched, then its data-driven handler methods are called
directly with synthetic payloads. Dialog-opening paths get QDialog.exec /
select_final_candidate stubbed so nothing blocks.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import ui.window as window_mod
from ui.main import parse_gui_args, parse_gui_launch_options
from ui.qt import import_qt
from ui.window import MainWindow


# --- ui/main.py launch-option parsing (pure) ----------------------------------


def test_parse_gui_launch_options_gpu_and_tail_bins() -> None:
    opts = parse_gui_launch_options(
        ["pburn", "--new-ui", "--gpu-index", "2", "--auto-uv-tail-rise-bins", "4", "extra"]
    )
    assert opts.gpu_index == 2
    assert opts.auto_uv_options["auto_uv_tail_rise_bins"] == 4
    assert opts.auto_uv_options["auto_uv_tail_rise_bins_explicit"] is True
    assert "extra" in opts.qt_argv


def test_parse_gui_launch_options_equals_forms() -> None:
    opts = parse_gui_launch_options(["pburn", "--index=3", "--auto-uv-tail-rise-bins=6"])
    assert opts.gpu_index == 3
    assert opts.auto_uv_options["auto_uv_tail_rise_bins"] == 6


def test_parse_gui_launch_options_empty_argv_defaults() -> None:
    opts = parse_gui_launch_options([])
    assert opts.qt_argv == ["penguin-burner-ui"]
    assert opts.gpu_index is None
    assert opts.auto_uv_options is None


def test_parse_gui_launch_options_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        parse_gui_launch_options(["pburn", "--gpu-index", "x"])
    with pytest.raises(ValueError):
        parse_gui_launch_options(["pburn", "--gpu-index"])
    with pytest.raises(ValueError):
        parse_gui_launch_options(["pburn", "--auto-uv-tail-rise-bins=nope"])


def test_parse_gui_args_returns_qt_argv() -> None:
    assert parse_gui_args(["pburn", "--gpu-index", "1", "passthrough"]) == [
        "pburn",
        "passthrough",
    ]


# --- MainWindow ---------------------------------------------------------------


@pytest.fixture
def main_window(qapp, monkeypatch):
    monkeypatch.setattr(
        window_mod,
        "load_profile_summaries",
        lambda: [{"profile_id": "p1", "display_name": "P1", "candidate_id": "c1"}],
    )
    monkeypatch.setattr(
        window_mod,
        "systemd_autostart_profile_info",
        lambda: {"selector": "active", "silent_fan_curve": False},
    )
    monkeypatch.setattr(window_mod, "systemd_unit_entry_exists", lambda: True)
    monkeypatch.setattr(
        window_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(window_mod, "penguin_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(
        window_mod, "persist_on_startup_from_runtime_config", lambda default=False: default
    )
    qt_modules = import_qt()
    if qt_modules[3] is None:
        pytest.skip("pyqtgraph not available")
    # Never block on a modal dialog during tests.
    monkeypatch.setattr(qt_modules[2].QDialog, "exec", lambda self: 0)
    window = MainWindow(qt_modules)
    yield window
    window.window.close()


def test_window_handles_scan_events(main_window, monkeypatch) -> None:
    win = main_window
    win._handle_scan_event({"event": "auto_uv_start"})
    win._handle_scan_event({"event": "dependency_progress", "detail": "Fetching", "percent": 40})
    win._handle_scan_event(
        {"event": "probe_start", "stage": "candidate", "voltage_mv": 900, "clock_mhz": 2500}
    )
    win._handle_scan_event(
        {"event": "probe_result", "stage": "candidate", "candidate_voltage_mv": 900,
         "lock_clock_mhz": 2500, "temp_c": 60, "fan_pct": 40}
    )
    win._handle_scan_event({"event": "load_telemetry", "candidate_voltage_mv": 900})
    win._handle_scan_event(
        {"event": "source_curve", "points": [{"voltage_mv": 800, "base_mhz": 2000}]}
    )
    win._handle_scan_event(
        {"event": "candidate_curve", "candidate_id": "c1",
         "points": [{"voltage_mv": 850, "clock_mhz": 2300}]}
    )
    win._handle_scan_event(
        {"event": "fan_curve_suggested", "points": [{"temperature_c": 40, "fan_pct": 30}]}
    )
    win._handle_scan_event({"event": "final_choice_discarded"})
    win._handle_scan_event(
        {"event": "final_result", "candidate_voltage_mv": 900, "lock_clock_mhz": 2500}
    )
    assert win.pending_final_result_payload is not None


def test_window_final_choice_request_without_response_path(main_window, monkeypatch) -> None:
    monkeypatch.setattr(
        window_mod,
        "select_final_candidate",
        lambda **kwargs: (None, 600, "select"),
    )
    # No response_path -> handler returns after running the dialog selection.
    main_window._handle_scan_event(
        {"event": "final_choice_request", "candidates": [{"candidate_id": "c1"}]}
    )


def test_window_previous_crash_close_writes_abort(main_window, monkeypatch, tmp_path) -> None:
    response_path = tmp_path / "choice.json"
    monkeypatch.setattr(
        window_mod,
        "select_final_candidate",
        lambda **kwargs: (None, 600, "abort"),
    )

    main_window._handle_scan_event(
        {
            "event": "final_choice_request",
            "request_reason": "previous-crash",
            "response_path": str(response_path),
            "candidates": [{"candidate_id": "885mv-2873mhz"}],
        }
    )

    assert '"action": "abort"' in response_path.read_text(encoding="utf-8")
    assert main_window.final_choice_discarded is True


def test_window_previous_crash_start_over_writes_discard(
    main_window,
    monkeypatch,
    tmp_path,
) -> None:
    response_path = tmp_path / "choice.json"
    monkeypatch.setattr(
        window_mod,
        "select_final_candidate",
        lambda **kwargs: (None, 600, "discard"),
    )

    main_window._handle_scan_event(
        {
            "event": "final_choice_request",
            "request_reason": "previous-crash",
            "response_path": str(response_path),
            "candidates": [{"candidate_id": "885mv-2873mhz"}],
        }
    )

    assert '"action": "discard"' in response_path.read_text(encoding="utf-8")
    assert main_window.final_choice_discarded is False


def test_window_human_lines(main_window) -> None:
    main_window._handle_human_line("Starting final verification now")
    main_window._handle_human_line("candidate 900mV under test")
    main_window._handle_human_line("Auto-UV final state reached")
    main_window._handle_human_line("unrelated chatter")


def test_window_scan_finished_branches(main_window) -> None:
    win = main_window
    # stopped-by-user
    win._scan_finished(0, 0, True)
    # pending final result -> Complete
    win.pending_final_result_payload = {"candidate_id": "c1"}
    win._scan_finished(0, 0, False)
    # discarded
    win.final_choice_discarded = True
    win._scan_finished(0, 0, False)
    # failed (errors.show_process is dialog-guarded by patched exec)
    win._scan_finished(1, 0, False)
    # plain idle
    win._scan_finished(0, 0, False)


def test_window_verify_and_command_finished(main_window) -> None:
    main_window._verify_finished(0, 0, False)  # success
    main_window._verify_finished(1, 0, True)  # stopped
    main_window._verify_finished(1, 0, False)  # failed
    main_window._command_finished("delete", 0, 0)


def test_window_simple_helpers(main_window) -> None:
    win = main_window
    assert win._workflow_running() is False
    win._set_profile_actions_enabled(True)
    assert "Systemd autostart: Yes" in win._runtime_action_start_text("install-systemd")
    assert "Removing" in win._runtime_action_start_text("uninstall-systemd")
    assert "adaptive" in win._runtime_action_start_text(
        "daemonize", adaptive_auto_uv=True
    ).lower()
    win._load_profiles()
    win.show_about()


def test_window_tab_order_and_bins_visibility(main_window) -> None:
    win = main_window
    # Tabs are Auto-UV, Profiles, Overlay (no separate fan-curve tab).
    labels = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert labels == ["Auto-UV", "Profiles", "Overlay"]
    assert not hasattr(win, "fan_plot")

    # The undervolting-runs panel shows only on the Auto-UV tab.
    win.tabs.setCurrentIndex(win.auto_uv_tab_index)
    assert not win.table_panel.isHidden()
    win.tabs.setCurrentIndex(win.profiles_tab_index)
    assert win.table_panel.isHidden()
    win.tabs.setCurrentIndex(win.overlay_tab_index)
    assert win.table_panel.isHidden()
