from __future__ import annotations

import sys

from .assets import application_icon
from .constants import APP_DESKTOP_ID
from .constants import APP_DISPLAY_NAME
from .qt import apply_dark_palette
from .qt import import_qt
from .window import MainWindow


def parse_gui_args(argv: list[str] | None = None) -> tuple[list[str], bool]:
    raw = list(sys.argv if argv is None else argv)
    if not raw:
        raw = ["penguin-burner-ui"]
    qt_argv = [raw[0]]
    auto_uv = False
    for arg in raw[1:]:
        if arg == "--new-ui":
            continue
        if arg == "--auto-uv3":
            auto_uv = True
            continue
        qt_argv.append(arg)
    return qt_argv, bool(auto_uv)


def run(argv: list[str] | None = None) -> int:
    qt_argv, auto_uv = parse_gui_args(sys.argv if argv is None else argv)
    try:
        qt_modules = import_qt()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _QtCore, QtGui, QtWidgets, _pg = qt_modules
    app = QtWidgets.QApplication(qt_argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    if hasattr(app, "setApplicationDisplayName"):
        app.setApplicationDisplayName(APP_DISPLAY_NAME)
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName(APP_DESKTOP_ID)
    icon = application_icon(QtGui)
    if not icon.isNull():
        app.setWindowIcon(icon)
    apply_dark_palette(app, QtGui)
    window = MainWindow(qt_modules, auto_uv=auto_uv)
    icon = application_icon(QtGui)
    if not icon.isNull():
        window.window.setWindowIcon(icon)
    window.show()
    return int(app.exec())


def main(argv: list[str] | None = None) -> int:
    return run(sys.argv if argv is None else argv)
