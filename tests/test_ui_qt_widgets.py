"""Live-Qt coverage for the small UI glue that needs a real QApplication.

Covers ui/qt.py (module import shim + dark palette) and ui/controllers/command.py
(CommandController, which drives a real QProcess).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui

import ui.qt as ui_qt
from ui.controllers.command import CommandController


def test_import_qt_returns_qt_modules() -> None:
    qtcore, qtgui, qtwidgets, _pg = ui_qt.import_qt()
    assert qtcore is not None
    assert qtgui is not None
    assert qtwidgets is not None


def test_apply_dark_palette_runs(qapp) -> None:
    # Should configure the palette without raising; verify one role landed.
    ui_qt.apply_dark_palette(qapp, QtGui)
    palette = qapp.palette()
    assert palette.color(QtGui.QPalette.Window).isValid()


def test_command_controller_runs_command_and_reports(qtbot) -> None:
    controller = CommandController(QtCore=QtCore)
    outputs: list[str] = []
    finished: list[tuple] = []
    controller.on_output = outputs.append
    controller.on_finished = lambda kind, code, status: finished.append((kind, code))

    assert controller.start("echo", ["echo", "hello-pburn"]) is True
    assert controller.is_running() is True

    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)

    assert finished and finished[0][0] == "echo"
    # The echoed command banner and the program output both flow to on_output.
    assert any("echo hello-pburn" in text for text in outputs)
    assert any("hello-pburn" in text for text in outputs)


def test_command_controller_rejects_empty_command(qtbot) -> None:
    controller = CommandController(QtCore=QtCore)
    assert controller.start("noop", []) is False
    assert controller.is_running() is False


def test_command_controller_rejects_second_start_while_running(qtbot) -> None:
    controller = CommandController(QtCore=QtCore)
    assert controller.start("sleep", ["sleep", "0.3"]) is True
    # A second start is refused while the first command is still running.
    assert controller.start("echo", ["echo", "x"]) is False
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)


def test_command_controller_reports_failure_to_start(qtbot) -> None:
    controller = CommandController(QtCore=QtCore)
    outputs: list[str] = []
    finished: list[tuple] = []
    controller.on_output = outputs.append
    controller.on_finished = lambda kind, code, status: finished.append((kind, code))

    started = controller.start(
        "broken",
        ["/nonexistent/penguin-burner-binary"],
        fail_text="could not launch",
    )

    assert started is False
    assert controller.is_running() is False
    assert any("could not launch" in text for text in outputs)
    assert finished and finished[0][0] == "broken"
