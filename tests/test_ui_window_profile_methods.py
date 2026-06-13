"""Coverage for ui/window.py profile edit/verify/export/delete methods.

Builds MainWindow offscreen and drives the profile-action methods with the
editor dialogs, command builders, store helpers, and controllers stubbed.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

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
    monkeypatch.setattr(window_mod, "systemd_unit_entry_exists", lambda: False)
    monkeypatch.setattr(
        window_mod, "running_auto_uv_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(window_mod, "penguin_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(window_mod, "persist_on_startup_from_runtime_config", lambda default=False: default)
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
    monkeypatch.setattr(window_mod, "profile_fan_curve_points", lambda profile: [])
    shown: list = []
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    window._edit_profile_fan_curve(PROFILE)
    assert shown


def test_edit_fan_curve_opens_and_saves(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "profile_fan_curve_points", lambda profile: [(30, 20)])
    monkeypatch.setattr(window_mod, "profile_fan_measurement_points", lambda profile: [])
    monkeypatch.setattr(window_mod, "profile_fan_curve_target_point", lambda profile: None)
    monkeypatch.setattr(
        window_mod,
        "save_edited_fan_profile",
        lambda profile, edit, original_points: (
            Path("/tmp/auto-uv-profile-x.json"),
            {"fan_curve_payload": {"points": [[35, 25]]}},
        ),
    )
    # The editor stub fires the save callback to exercise the closure.
    monkeypatch.setattr(
        window_mod, "open_fan_curve_editor_dialog", lambda **k: k["save_callback"](object())
    )
    window._edit_profile_fan_curve(PROFILE)


def test_edit_vf_curve_no_plan_shows_info(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "profile_curve_plan", lambda profile: [])
    monkeypatch.setattr(window_mod, "editable_anchor_from_profile", lambda profile: None)
    shown: list = []
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    window._edit_profile_vf_curve(PROFILE)
    assert shown


def test_edit_vf_curve_opens_and_saves(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window_mod, "profile_curve_plan",
        lambda profile: [{"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}],
    )
    monkeypatch.setattr(window_mod, "editable_anchor_from_profile", lambda profile: (900, 2500))
    monkeypatch.setattr(window_mod, "profile_base_curve_points", lambda profile: [(900, 2400)])
    monkeypatch.setattr(window_mod, "_manual_curve_control_voltage_mvs", lambda manual: ())
    monkeypatch.setattr(
        window_mod, "save_edited_curve_profile",
        lambda profile, edit, **kw: (Path("/tmp/auto-uv-profile-y.json"), {"candidate_id": "c9"}),
    )
    monkeypatch.setattr(
        window_mod, "open_vf_curve_editor_dialog", lambda **k: k["save_callback"](object())
    )
    window._edit_profile_vf_curve(PROFILE)
    assert window.last_auto_uv_candidate_id == "c9"


def test_export_lact_cancelled_and_no_gpu(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "")
    )
    window._export_lact_profile(PROFILE)  # cancelled -> early return

    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "/tmp/lact")
    )
    monkeypatch.setattr(window_mod, "detect_lact_gpu_id", lambda directory: "")
    shown: list = []
    monkeypatch.setattr(window.errors, "show", lambda title, msg: shown.append(title))
    window._export_lact_profile(PROFILE)
    assert shown  # no gpu id -> error surfaced


def test_export_lact_writes(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "/tmp/lact")
    )
    monkeypatch.setattr(window_mod, "detect_lact_gpu_id", lambda directory: "1002:abcd")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(
        window_mod, "write_lact_profile_config",
        lambda profile, **kw: (Path("/tmp/lact/config.yaml"), ["a warning"]),
    )
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    window._export_lact_profile(PROFILE)


def test_verify_profile_guards_and_runs(win) -> None:
    window, monkeypatch = win
    # Cannot verify (no path / not afterburner) -> early return.
    window._verify_profile({"profile_id": "x"})

    monkeypatch.setattr(window_mod, "select_verify_options", lambda **k: None)
    window._verify_profile(PROFILE)  # dialog cancelled -> return

    monkeypatch.setattr(
        window_mod, "select_verify_options",
        lambda **k: {"duration_s": 60, "q2rtx_enabled": True, "cuda_enabled": True},
    )
    monkeypatch.setattr(window_mod, "profile_verify_command", lambda **k: ["echo", "verify"])
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
        window_mod, "profile_delete_autostart_action", lambda *a: {"action": "keep"}
    )
    monkeypatch.setattr(MainWindow, "_confirm_profile_delete", lambda self, **k: True)
    deleted = []
    monkeypatch.setattr(
        window_mod, "delete_auto_uv_profile_paths", lambda paths: deleted.extend(paths) or list(paths)
    )
    window._delete_selected_profiles()
    assert deleted == ["/tmp/p1.json"]

    # Nothing selected -> early return.
    monkeypatch.setattr(window.profile_list, "selected_profile_paths", lambda: [])
    monkeypatch.setattr(window.profile_list, "selected_profile_ids", lambda: [])
    window._delete_selected_profiles()
