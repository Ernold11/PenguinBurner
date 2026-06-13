"""Coverage for ui/controllers/scan.py and ui/controllers/verify.py.

Line parsing / json response are tested directly; the QProcess-backed
start/stop/read/finish paths run real short-lived commands under qtbot.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore

from ui.controllers.scan import ScanController
from ui.controllers.verify import VerifyController


# --- ScanController: pure-ish line handling -----------------------------------


def test_scan_handle_line_routes_events_and_human_text() -> None:
    controller = ScanController(QtCore=QtCore)
    events: list[dict] = []
    humans: list[str] = []
    controller.on_event = events.append
    controller.on_human_line = humans.append

    controller._handle_line('{"event": "probe_start"}')
    controller._handle_line("plain human progress line")
    controller._handle_line("{bad json")  # starts with { but won't parse
    controller._handle_line("   ")  # blank -> ignored

    assert events == [{"event": "probe_start"}]
    assert humans == ["plain human progress line", "{bad json"]


def test_scan_write_json_response(tmp_path) -> None:
    controller = ScanController(QtCore=QtCore)
    out = tmp_path / "resp.json"
    controller.write_json_response(out, {"candidate_id": "c1"})
    assert "candidate_id" in out.read_text(encoding="utf-8")
    # Empty path is a no-op.
    controller.write_json_response("   ", {"x": 1})


def test_scan_start_rejects_empty_and_busy(qtbot) -> None:
    controller = ScanController(QtCore=QtCore)
    assert controller.start([]) is False
    assert controller.start([sys.executable, "-c", "import time; time.sleep(0.4)"]) is True
    assert controller.start([sys.executable, "-c", "pass"]) is False  # already running
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)


def test_scan_runs_command_and_parses_output(qtbot, tmp_path) -> None:
    controller = ScanController(QtCore=QtCore, stop_request_path=tmp_path / "stop")
    events: list[dict] = []
    humans: list[str] = []
    finished: list[tuple] = []
    controller.on_event = events.append
    controller.on_human_line = humans.append
    controller.on_finished = lambda code, status, stopped: finished.append((code, stopped))

    script = "print('{\"event\": \"auto_uv_start\"}'); print('human tail line')"
    assert controller.start([sys.executable, "-c", script]) is True
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)

    assert {"event": "auto_uv_start"} in events
    assert any("human tail line" in line for line in humans)
    assert finished and finished[0][1] is False


def test_scan_failed_start_reports(qtbot) -> None:
    controller = ScanController(QtCore=QtCore)
    outputs: list[str] = []
    controller.on_output = outputs.append
    assert controller.start(["/nonexistent/pburn-binary"]) is False
    assert any("Failed to start" in text for text in outputs)


def test_scan_stop_writes_request_and_marks_stopped(qtbot, tmp_path) -> None:
    stop_path = tmp_path / "stop-requested"
    controller = ScanController(QtCore=QtCore, stop_request_path=stop_path)
    finished: list[tuple] = []
    controller.on_finished = lambda code, status, stopped: finished.append(stopped)

    assert controller.start([sys.executable, "-c", "import time; time.sleep(10)"]) is True
    controller.stop()
    assert stop_path.exists()
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=6000)
    assert finished and finished[0] is True
    # kill_if_running is a no-op once the process has exited.
    controller.kill_if_running()


# --- VerifyController ---------------------------------------------------------


def test_verify_start_rejects_empty() -> None:
    controller = VerifyController(QtCore=QtCore, stop_request_path=None)  # type: ignore[arg-type]
    assert controller.start([], duration_s=60) is False


def test_verify_runs_and_reports_progress(qtbot, tmp_path) -> None:
    controller = VerifyController(QtCore=QtCore, stop_request_path=tmp_path / "vstop")
    progress: list[tuple] = []
    finished: list[tuple] = []
    controller.on_progress = lambda pct, elapsed, target, detail: progress.append((pct, elapsed))
    controller.on_finished = lambda code, status, stopped: finished.append((code, stopped))

    script = "print('status elapsed=30.0s running'); print('status elapsed=60.0s done')"
    assert controller.start([sys.executable, "-c", script], duration_s=60) is True
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)

    assert controller.target_duration_s == 60
    assert progress and progress[-1][0] == 100  # 60/60 -> 100%
    assert finished and finished[0][1] is False
    assert not (tmp_path / "vstop").exists()  # cleaned up on finish


def test_verify_failed_start_reports(qtbot, tmp_path) -> None:
    controller = VerifyController(QtCore=QtCore, stop_request_path=tmp_path / "vstop")
    outputs: list[str] = []
    controller.on_output = outputs.append
    assert controller.start(["/nonexistent/pburn-verify"], duration_s=10) is False
    assert any("Failed to start" in text for text in outputs)


def test_verify_stop_marks_stopped(qtbot, tmp_path) -> None:
    stop_path = tmp_path / "vstop"
    controller = VerifyController(QtCore=QtCore, stop_request_path=stop_path)
    finished: list[bool] = []
    controller.on_finished = lambda code, status, stopped: finished.append(stopped)

    assert controller.start([sys.executable, "-c", "import time; time.sleep(10)"], duration_s=60) is True
    controller.stop()
    assert stop_path.exists()
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=6000)
    assert finished and finished[0] is True
