from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="penguin-burner-overlay-display")
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = _OverlayWindow(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        text_file=Path(args.text_file).expanduser(),
        parent_pid=int(args.parent_pid or 0),
    )
    window.show()
    return int(app.exec())


class _OverlayWindow:
    def __init__(self, *, QtCore, QtGui, QtWidgets, text_file: Path, parent_pid: int):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.text_file = text_file
        self.parent_pid = int(parent_pid)
        self.last_text = ""
        self.window = QtWidgets.QWidget()
        flags = (
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.window.setWindowFlags(flags)
        self.window.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.window.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        layout = QtWidgets.QVBoxLayout(self.window)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QtWidgets.QLabel("")
        self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.label.setStyleSheet(
            "QLabel { color: white; background: rgba(0, 0, 0, 150); "
            "padding: 5px 8px; border-radius: 3px; }"
        )
        font = QtGui.QFont("Inter")
        font.setStyleHint(QtGui.QFont.StyleHint.SansSerif)
        font.setPixelSize(self._font_px())
        font.setBold(True)
        self.label.setFont(font)
        layout.addWidget(self.label)

        self.timer = QtCore.QTimer(self.window)
        self.timer.timeout.connect(self._tick)
        self.timer.start(250)
        self._tick()

    def show(self) -> None:
        self.window.show()
        self._position()

    def _font_px(self) -> int:
        screen = self.QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return 16
        width = int(screen.geometry().width())
        if width >= 3200:
            return 22
        if width >= 2400:
            return 18
        return 16

    def _position(self) -> None:
        screen = self.QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        self.window.adjustSize()
        margin = 14 if geometry.width() < 2400 else 22
        x = geometry.x() + geometry.width() - self.window.width() - margin
        y = geometry.y() + margin
        self.window.move(max(geometry.x(), x), max(geometry.y(), y))

    def _tick(self) -> None:
        if self.parent_pid > 0 and not _pid_exists(self.parent_pid):
            self.QtWidgets.QApplication.quit()
            return
        try:
            text = self.text_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        text = text.strip()
        if not text:
            text = "PB overlay waiting"
        if text != self.last_text:
            self.last_text = text
            self.label.setText(text)
            self._position()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    raise SystemExit(main())
