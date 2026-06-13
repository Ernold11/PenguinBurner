"""Coverage for ui/window.py action methods: scan start/stop and runtime actions.

Builds MainWindow offscreen, then drives the dialog/process-backed methods with
the dialog functions, command builders, and controllers stubbed so nothing
blocks or launches a real process.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import ui.window as window_mod
from ui.qt import import_qt
from ui.window import MainWindow


class _FakeController:
    def __init__(self) -> None:
        self._running = False
        self.started: list = []
        self.stopped = 0

    def is_running(self) -> bool:
        return self._running

    def start(self, *args, **kwargs) -> bool:
        self.started.append((args, kwargs))
        return True

    def stop(self) -> None:
        self.stopped += 1


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
    yield window, monkeypatch
    window.window.close()


def test_start_scan_cancelled(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: None)
    fake = _FakeController()
    window.scan_controller = fake
    window.start_scan()
    assert fake.started == []  # dialog cancelled -> nothing started


def test_start_scan_runs(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    fake = _FakeController()
    window.scan_controller = fake
    window.start_scan()
    assert fake.started  # scan command launched


def test_start_scan_switches_to_auto_uv_tab(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    window.scan_controller = _FakeController()
    # Start from a different tab; the scan must pull the user to Auto-UV.
    window.tabs.setCurrentIndex(window.profiles_tab_index)
    assert window.tabs.currentIndex() != window.auto_uv_tab_index
    window.start_scan()
    assert window.tabs.currentIndex() == window.auto_uv_tab_index


def test_start_scan_gpu_persist_error(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})

    def _boom(_idx):
        raise RuntimeError("cannot save")

    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", _boom)
    fake = _FakeController()
    window.scan_controller = fake
    window.start_scan()
    assert fake.started == []  # aborted on persist error


def test_stop_scan_paths(win) -> None:
    window, _monkeypatch = win
    verify = _FakeController()
    scan = _FakeController()
    window.verify_controller = verify
    window.scan_controller = scan

    # Nothing running -> no-op.
    window.stop_scan()
    assert verify.stopped == 0 and scan.stopped == 0

    # Verify running takes priority.
    verify._running = True
    window.stop_scan()
    assert verify.stopped == 1

    # Otherwise the scan controller is stopped.
    verify._running = False
    scan._running = True
    window.stop_scan()
    assert scan.stopped == 1


def test_run_runtime_action_no_profile(win) -> None:
    window, monkeypatch = win
    window.profile_summaries = []
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    fake = _FakeController()
    window.command_controller = fake
    window._run_runtime_action("daemonize")
    assert fake.started == []  # no profile -> nothing launched


def test_run_runtime_action_launches(win) -> None:
    window, monkeypatch = win
    window.profile_summaries = [
        {"profile_id": "p1", "final_verified": True, "path": "/tmp/p1.json"}
    ]
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "p1")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(window.profile_list, "set_runtime_actions_enabled", lambda enabled: None)
    monkeypatch.setattr(window_mod, "runtime_profile_command", lambda *a, **k: ["echo", "run"])
    fake = _FakeController()
    window.command_controller = fake
    window._run_runtime_action("daemonize")
    assert fake.started  # runtime command launched


def test_run_runtime_action_blocked_when_busy(win) -> None:
    window, _monkeypatch = win
    busy = _FakeController()
    busy._running = True
    window.command_controller = busy
    window._run_runtime_action("daemonize")
    # _workflow_running() short-circuits; start was never called again.
    assert busy.started == []


def test_run_adaptive_insufficient_tiers(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "adaptive_profile_tier_labels", lambda profs: ["Efficiency"])
    shown: list = []
    monkeypatch.setattr(window.errors, "show", lambda title, msg: shown.append(title))
    fake = _FakeController()
    window.command_controller = fake
    window._run_runtime_action("daemonize", adaptive_auto_uv=True)
    assert shown  # error surfaced, nothing launched
    assert fake.started == []
