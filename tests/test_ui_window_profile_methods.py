"""Coverage for ui/window.py profile edit/verify/export/delete methods.

Builds MainWindow offscreen and drives the profile-action methods with the
editor dialogs, command builders, store helpers, and controllers stubbed.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import ui.features.profiles.profile_actions as actions_mod
import ui.window as window_mod
from ui.qt import import_qt
from ui.window import MainWindow


class _FakeController:
    def __init__(self) -> None:
        self._running = False
        self.started: list = []

    def is_running(self) -> bool:
        return self._running

    def start(self, *args, **kwargs) -> bool:
        self.started.append((args, kwargs))
        return True

    def stop(self) -> None:
        self._running = False


@pytest.fixture
def win(qapp, monkeypatch):
    monkeypatch.setattr(window_mod, "load_profile_summaries", lambda: [])
    monkeypatch.setattr(
        window_mod, "systemd_autostart_profile_info", lambda: {"selector": "", "silent_fan_curve": False}
    )
    monkeypatch.setattr(
        window_mod, "running_auto_uv_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(window_mod, "penguin_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)
    modules = import_qt()
    if modules[3] is None:
        pytest.skip("pyqtgraph not available")
    monkeypatch.setattr(modules[2].QDialog, "exec", lambda self: 0)
    window = MainWindow(modules)
    monkeypatch.setattr(window.profile_list, "select_profile", lambda pid: None)
    yield window, monkeypatch
    window.window.close()


PROFILE = {"profile_id": "p1", "path": "/tmp/p1.json", "final_verified": True}


def test_edit_fan_curve_no_curve_shows_info(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "profile_fan_curve_points", lambda profile: [])
    shown: list = []
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    window._edit_profile_fan_curve(PROFILE)
    assert shown


def test_edit_fan_curve_opens_and_saves(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "profile_fan_curve_points", lambda profile: [(30, 20)])
    monkeypatch.setattr(actions_mod, "profile_fan_measurement_points", lambda profile: [])
    monkeypatch.setattr(actions_mod, "profile_fan_curve_target_point", lambda profile: None)
    monkeypatch.setattr(
        actions_mod,
        "save_edited_fan_profile",
        lambda profile, edit, original_points: (
            Path("/tmp/auto-uv-profile-x.json"),
            {"fan_curve_payload": {"points": [[35, 25]]}},
        ),
    )
    # The editor stub fires the save callback to exercise the closure.
    monkeypatch.setattr(
        actions_mod, "open_fan_curve_editor_dialog", lambda **k: k["save_callback"](object())
    )
    window._edit_profile_fan_curve(PROFILE)


def test_edit_vf_curve_no_plan_shows_info(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "profile_curve_plan", lambda profile: [])
    monkeypatch.setattr(actions_mod, "editable_anchor_from_profile", lambda profile: None)
    shown: list = []
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    window._edit_profile_vf_curve(PROFILE)
    assert shown


def test_edit_vf_curve_opens_and_saves(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        actions_mod, "profile_curve_plan",
        lambda profile: [{"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}],
    )
    monkeypatch.setattr(actions_mod, "editable_anchor_from_profile", lambda profile: (900, 2500))
    monkeypatch.setattr(actions_mod, "profile_base_curve_points", lambda profile: [(900, 2400)])
    monkeypatch.setattr(actions_mod, "_manual_curve_control_voltage_mvs", lambda manual: ())
    monkeypatch.setattr(
        actions_mod, "save_edited_curve_profile",
        lambda profile, edit, **kw: (Path("/tmp/auto-uv-profile-y.json"), {"candidate_id": "c9"}),
    )
    monkeypatch.setattr(
        actions_mod, "open_vf_curve_editor_dialog", lambda **k: k["save_callback"](object())
    )
    window._edit_profile_vf_curve(PROFILE)
    assert window.last_auto_uv_candidate_id == "c9"


def test_edit_memory_offset_no_value_shows_info(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "editable_memory_offset_from_profile", lambda profile: None)
    shown: list = []
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    window._edit_profile_memory_offset(PROFILE)
    assert shown


def test_edit_memory_offset_opens_and_saves(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "editable_memory_offset_from_profile", lambda profile: 200)
    monkeypatch.setattr(actions_mod, "memory_offset_mhz_range", lambda gpu_index: (0, 2000))
    monkeypatch.setattr(
        actions_mod,
        "save_edited_memory_offset_profile",
        lambda profile, new_memory_offset_mhz, **kw: (
            Path("/tmp/auto-uv-profile-mem.json"),
            {"memory_offset_mhz": new_memory_offset_mhz},
        ),
    )
    # The editor stub fires the save callback to exercise the closure.
    monkeypatch.setattr(
        actions_mod, "open_memory_offset_editor_dialog", lambda **k: k["save_callback"](400)
    )
    window._edit_profile_memory_offset(PROFILE)


def test_export_lact_cancelled_and_no_gpu(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "")
    )
    window._export_lact_profile(PROFILE)  # cancelled -> early return

    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "/tmp/lact")
    )
    monkeypatch.setattr(actions_mod, "detect_lact_gpu_id", lambda directory: "")
    shown: list = []
    monkeypatch.setattr(window.errors, "show", lambda title, msg: shown.append(title))
    window._export_lact_profile(PROFILE)
    assert shown  # no gpu id -> error surfaced


def test_export_lact_writes(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "/tmp/lact")
    )
    monkeypatch.setattr(actions_mod, "detect_lact_gpu_id", lambda directory: "1002:abcd")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(
        actions_mod, "write_lact_profile_config",
        lambda profile, **kw: (Path("/tmp/lact/config.yaml"), ["a warning"]),
    )
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    window._export_lact_profile(PROFILE)


def test_verify_profile_guards_and_runs(win) -> None:
    window, monkeypatch = win
    # Cannot verify (no path / not afterburner) -> early return.
    window._verify_profile({"profile_id": "x"})

    monkeypatch.setattr(actions_mod, "select_verify_options", lambda **k: None)
    window._verify_profile(PROFILE)  # dialog cancelled -> return

    monkeypatch.setattr(
        actions_mod, "select_verify_options",
        lambda **k: {"duration_s": 60},
    )
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(actions_mod, "profile_verify_command", lambda **k: ["echo", "verify"])
    fake = _FakeController()
    window.verify_controller = fake
    window._verify_profile(PROFILE)
    assert fake.started


def test_delete_selected_profiles(win) -> None:
    window, monkeypatch = win
    window.profile_summaries = [PROFILE]
    monkeypatch.setattr(window.profile_list, "selected_profile_ids", lambda: ["p1"])
    monkeypatch.setattr(window.profile_list, "selected_profile_paths", lambda: ["/tmp/p1.json"])
    monkeypatch.setattr(
        actions_mod, "profile_delete_autostart_action", lambda *a: {"action": "keep"}
    )
    monkeypatch.setattr(MainWindow, "_confirm_profile_delete", lambda self, **k: True)
    deleted = []
    monkeypatch.setattr(
        actions_mod, "delete_auto_uv_profile_paths", lambda paths: deleted.extend(paths) or list(paths)
    )
    window._delete_selected_profiles()
    assert deleted == ["/tmp/p1.json"]

    # Nothing selected -> early return.
    monkeypatch.setattr(window.profile_list, "selected_profile_paths", lambda: [])
    monkeypatch.setattr(window.profile_list, "selected_profile_ids", lambda: [])
    window._delete_selected_profiles()


def test_delete_running_session_only_profile_restores_stock(win) -> None:
    # Session-only applies (Apply-on-startup unticked) leave no boot entry, so
    # deleting the actively running profile must still restore stock instead
    # of leaving an orphaned curve applied.
    window, monkeypatch = win
    window.profile_summaries = [PROFILE]
    monkeypatch.setattr(window.profile_list, "selected_profile_ids", lambda: ["p1"])
    monkeypatch.setattr(
        window.profile_list, "selected_profile_paths", lambda: ["/tmp/p1.json"]
    )
    monkeypatch.setattr(
        actions_mod,
        "systemd_autostart_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(actions_mod, "penguin_burner_runtime_is_active", lambda: True)
    monkeypatch.setattr(
        actions_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "p1", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    confirmations: list[dict] = []
    monkeypatch.setattr(
        MainWindow,
        "_confirm_profile_delete",
        lambda self, **kwargs: confirmations.append(kwargs) or False,
    )

    window._delete_selected_profiles()

    assert confirmations and confirmations[0]["restore_stock"] is True


def test_unticking_boot_apply_clears_saved_boot_profile(win, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client_mod

    window, _mp = win
    cleared: list[bool] = []
    saved: list[bool] = []
    monkeypatch.setattr(
        window_mod, "persist_on_startup_to_runtime_config", lambda v: saved.append(bool(v))
    )
    monkeypatch.setattr(
        daemon_client_mod, "clear_boot_runtime_spec", lambda **_k: cleared.append(True)
    )

    window.profile_list.set_boot_apply_checked(True)  # blocked signals: no side effects
    assert cleared == [] and saved == []

    window.profile_list.boot_apply_checkbox.setChecked(False)
    assert saved == [False]
    assert cleared == [True]

    # Ticking arms boot persistence for the next Apply but clears nothing.
    window.profile_list.boot_apply_checkbox.setChecked(True)
    assert saved == [False, True]
    assert cleared == [True]


def test_delete_boot_profile_falls_back_to_persisted_stock(win) -> None:
    window, monkeypatch = win
    restored: list[bool] = []
    monkeypatch.setattr(
        window,
        "_restore_gpu_defaults",
        lambda: restored.append(True),
    )

    handled = window._run_delete_autostart_followup(restore_stock=True)

    assert handled is True
    assert restored == [True]


def test_silent_fan_tick_survives_discarded_run(win, monkeypatch) -> None:
    # Regression: after a discarded/aborted Auto-UV run the runtime/autostart no
    # longer carry the silent-fan flag, but the user's persisted choice must keep
    # the tick checked. (The win fixture already reports both as False.)
    window, _mp = win
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: True)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)

    window.profile_list.silent_fan_checkbox.setChecked(False)
    window._load_profiles()

    assert window.profile_list.silent_fan_enabled() is True


def test_scan_completion_restores_pre_scan_silent_fan(win, monkeypatch) -> None:
    # A foreground scan resets the GPU to stock, so the completion reload sees
    # a fan-off running state. The auto-applied final profile must still carry
    # the silent-fan curve the user had running before the scan, without any
    # toggle change from them.
    window, _mp = win
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)
    monkeypatch.setattr(window_mod, "penguin_burner_runtime_is_active", lambda: True)
    monkeypatch.setattr(
        window_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "p1", "silent_fan_curve": True, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(
        window_mod, "ensure_daemon_ready_for_privileged_action", lambda **_k: True
    )
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    window.scan_controller = _FakeController()

    # Start a scan: the pre-scan silent-fan intent (live running profile) is
    # captured even though the checkbox and config are both off.
    window.start_scan()
    assert window._pre_scan_silent_fan is True

    # After the scan the running profile reads as stock (fan off).
    monkeypatch.setattr(
        window_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    applied = []
    monkeypatch.setattr(
        window, "_run_runtime_action", lambda action: applied.append(action)
    )
    # Drive the completion path with a pending final result (auto-apply).
    window.pending_final_result_payload = {"candidate_id": "c1"}
    window._scan_finished(0, 0, False)
    window.QtCore.QCoreApplication.processEvents()

    assert window.profile_list.silent_fan_enabled() is True
    assert applied == ["daemonize"]


def test_silent_fan_tick_stays_unchecked_when_not_persisted(win, monkeypatch) -> None:
    window, _mp = win
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)

    window.profile_list.silent_fan_checkbox.setChecked(True)
    window._load_profiles()

    assert window.profile_list.silent_fan_enabled() is False


def test_apply_profile_persists_silent_fan_choice(win, monkeypatch) -> None:
    # Applying a profile with the tick on must seed the durable preference so it
    # survives a later discarded Auto-UV run even without a manual toggle.
    window, _mp = win
    saved: list[bool] = []
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: saved.append(bool(v)))
    monkeypatch.setattr(actions_mod, "profile_for_selector", lambda summaries, pid: dict(PROFILE))
    monkeypatch.setattr(actions_mod, "profile_can_apply", lambda p: True)
    monkeypatch.setattr(actions_mod, "sync_profile_fan_payload", lambda p: True)
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(actions_mod, "runtime_profile_command", lambda *a, **k: ["pb"])
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "p1")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: True)
    window.profile_summaries = [PROFILE]

    window._run_runtime_action("daemonize")

    assert saved and saved[-1] is True


def test_scan_finish_leaves_exact_runtime_restoration_to_daemon(win, monkeypatch) -> None:
    # burnerd restores the exact active RuntimeSpec (which may differ from the
    # boot profile). The UI must not issue a second runtime action after abort.
    window, _mp = win
    started: list = []
    monkeypatch.setattr(
        window.command_controller, "start",
        lambda *a, **k: started.append((a, k)) or True,
    )
    window.final_choice_aborted = True

    window._scan_finished(1, 0, False)

    assert started == []
